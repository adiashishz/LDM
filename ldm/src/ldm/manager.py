"""Download manager: queue, parallel limit, per-host rules, speed limits, auto-retry.

Runs every download as an asyncio task in one event loop. Front ends (D-Bus service, tests)
drive it through its methods and observe it through the `on_*` callbacks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import os
import time
from collections import Counter, deque
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from . import backup, files, media, torrent, transfer
from .engine import DownloadError, Downloader, new_client, probe
from .hlskey import aes_hex
from .media import MediaDownloader
from . import net
from .net import DOH_PRESETS, NetConfig, check_proxy_url, with_login
from .passwords import open_store
from .torrent import TorrentDownloader, TorrentSession
from .ratelimit import TokenBucket
from .schedule import describe_start, in_window, next_start, parse_days, parse_time
from .system import Conditions
from .store import DEFAULT_SETTINGS, DownloadRow, HostRule, Store, default_db_path

RESUMABLE_STATES = {"paused", "error", "expired", "changed"}
MAX_CONNECTIONS = 32
MEDIA_EXTENSIONS = (".m3u8", ".mpd")  # stream playlists always go through yt-dlp
MEDIA_OPTIONS = {"format", "format_label", "subtitles", "title", "page_url", "hls_key", "hls_iv"}
TORRENT_OPTIONS = {"priorities", "sequential", "metadata", "peers"}
WINDOW_KEYS = ("schedule_start", "schedule_stop", "schedule_days")  # a download's own times override the defaults
RUN_OPTIONS = {"scheduled", "unmetered_only", *WINDOW_KEYS}  # stored in options_json for every kind
CONDITION_TICK = 30.0
CLEANUP_INTERVAL = 3600.0  # seconds between "remove old entries" checks
BACKUP_INTERVALS = {"daily": 86400, "weekly": 7 * 86400}
LOG_LINES = 300  # diagnostics kept per download
SCHEMES = {"http": ("http", "https"), "media": ("http", "https"), "torrent": ("http", "https", "magnet", "file")}

log = logging.getLogger(__name__)


class ManagerError(Exception):
    pass


class NotFound(ManagerError):
    pass


class InvalidArgument(ManagerError):
    pass


class SizeMismatch(ManagerError):
    pass


class Busy(ManagerError):
    pass


class Duplicate(ManagerError):
    """The torrent is already in the list (its trackers may have been added to it)."""


def _link_key(url: str) -> str:
    """Same link: ignore the #fragment and a trailing slash."""
    return url.split("#", 1)[0].rstrip("/")


class Manager:
    def __init__(self, store: Store, *, downloader_factory: Callable[..., Downloader] = Downloader,
                 media_factory: Callable[..., MediaDownloader] = MediaDownloader,
                 downloader_options: dict[str, Any] | None = None, progress_interval: float = 0.5,
                 torrent_listen_host: str = "0.0.0.0", conditions: Conditions | None = None,
                 inhibitor=None, clock: Callable[[], datetime] = datetime.now, passwords=None,
                 passwords_path: Path | None = None):
        self.store = store
        # Keyring (or file) for proxy and site-login passwords: opened only when one is needed.
        self._passwords = passwords
        self._passwords_path = passwords_path or default_db_path("passwords.json")
        self._secrets: dict[tuple[str, str], str] = {}
        self._logs: dict[int, deque[str]] = {}  # download id -> diagnostics lines
        self._background: set[asyncio.Task] = set()  # after-finish moves, cleanup removals
        self._last_cleanup = float("-inf")
        self._tracker_fetch: asyncio.Task | None = None
        self.conditions = conditions or Conditions()  # metered / battery (system.SystemMonitor)
        self.inhibitor = inhibitor                     # system.SleepInhibitor or None
        self._clock = clock
        self._waiting: dict[int, str] = {}             # queued id -> why it isn't starting
        self._throttled_hosts: dict[str, int] = {}     # host -> connections it tolerated (HTTP 429/503)
        self._holding: set[int] = set()                # being stopped by a condition, not paused
        self._tick_task: asyncio.Task | None = None
        self._factories = {"http": downloader_factory, "media": media_factory,
                           "torrent": partial(TorrentDownloader, session=self._torrent_session)}
        self._torrent: TorrentSession | None = None
        self._torrent_listen_host = torrent_listen_host
        self._options = downloader_options or {}
        self._progress_interval = progress_interval
        self._tasks: dict[int, asyncio.Task] = {}
        self._rule_keys: dict[int, str | None] = {}  # running id -> host rule it counts against
        self._buckets: dict[int, TokenBucket] = {}
        self._downloaders: dict[int, Any] = {}       # running id -> engine (live parts)
        self.session_bytes = 0                         # received since the service started (data limit)
        self._last_done: dict[int, int] = {}           # running id -> bytes done at the last progress report
        self._progress: dict[int, tuple[int, int | None, float]] = {}
        self._attempts: dict[int, int] = {}
        self._retry_timers: dict[int, asyncio.TimerHandle] = {}
        self._global = TokenBucket()
        self._progress_task: asyncio.Task | None = None
        self._closing = False
        # Event callbacks, set by the front end.
        self.on_added: Callable[[int], None] | None = None
        self.on_removed: Callable[[int], None] | None = None
        self.on_state: Callable[[int, str, str], None] | None = None
        self.on_progress_batch: Callable[[list[tuple[int, int, int, float]]], None] | None = None
        self.on_autostart: Callable[[bool], None] | None = None
        self.on_all_finished: Callable[[str], None] | None = None  # finish_action, when the queue is empty
        self.on_started: Callable[[int], None] | None = None       # a download started (cancels a countdown)

    # --- lifecycle -------------------------------------------------------------------------

    async def start(self) -> None:
        await self._load_secrets()
        self._apply_dns()
        self.store.requeue_interrupted()
        self._global.set_rate(self.store.get_settings()["speed_limit"])
        self._progress_task = asyncio.create_task(self._progress_loop())
        self._tick_task = asyncio.create_task(self._condition_loop())
        for d in self.store.list():
            if d.state == "seeding":
                self._resume_seeding(d)
        self._schedule()

    # --- network, passwords, logins, diagnostics ----------------------------------------------

    def _net(self) -> NetConfig:
        """Proxy, DNS, timeouts, user agent and site logins from the current settings."""
        return NetConfig.from_settings(self.store.get_settings(), self._secrets.get(("proxy", "proxy")),
                                       self._login_for)

    def _apply_dns(self) -> None:
        """DNS-over-HTTPS also for yt-dlp (it resolves names itself): see net.use_doh_for_process."""
        settings = self.store.get_settings()
        doh = settings["dns_mode"] == "doh" and net.doh_available()
        net.use_doh_for_process(net.doh_endpoint(settings["doh_url"]) if doh else None,
                                float(settings["connect_timeout"]))

    def _login_for(self, host: str) -> tuple[str, str] | None:
        found = self.store.login_for(host)
        if found is None:
            return None
        saved_under, user = found
        return user, self._secrets.get(("login", saved_under), "")

    def _torrent_proxy(self) -> str | None:
        """libtorrent only uses a manual proxy (it has no desktop proxy lookup)."""
        net = self._net()
        return net.proxy if net.proxy_mode == "manual" else None

    async def _load_secrets(self) -> None:
        """Read the passwords that are in use; the keyring is not opened when there are none."""
        keys = [("login", login["host"]) for login in self.store.list_logins()]
        settings = self.store.get_settings()
        if settings["proxy_user"] or self._passwords is not None:
            keys.append(("proxy", "proxy"))
        if settings["backup_schedule"] != "off" or self._passwords is not None:
            keys.append(("backup", "backup"))
        if not keys:
            return

        def read() -> dict[tuple[str, str], str]:
            store = self._open_passwords()
            return {key: value for key in keys if (value := store.get(*key)) is not None}

        try:
            self._secrets.update(await asyncio.to_thread(read))
        except Exception as e:  # locked keyring that the user declined to unlock, …
            log.warning("could not read saved passwords: %s", e)

    def _open_passwords(self):
        if self._passwords is None:
            self._passwords = open_store(self._passwords_path)
        return self._passwords

    def _set_secret(self, kind: str, key: str, password: str) -> None:
        """Remember now; save to the keyring in the background ("" deletes)."""
        if password:
            self._secrets[(kind, key)] = password
        else:
            self._secrets.pop((kind, key), None)

        def save() -> None:
            store = self._open_passwords()
            store.set(kind, key, password) if password else store.delete(kind, key)

        task = asyncio.get_running_loop().run_in_executor(None, save)
        task.add_done_callback(lambda t: t.exception() and log.warning("could not save password: %s", t.exception()))

    def set_login(self, host: str, username: str, password: str, include_subdomains: bool = True) -> None:
        host = host.strip().lower()
        if not host or "/" in host or not username:
            raise InvalidArgument("a login needs a host name (e.g. example.com) and a user name")
        self.store.set_login(host, username, include_subdomains)
        self._set_secret("login", host, password)

    def remove_login(self, host: str) -> None:
        host = host.strip().lower()
        self.store.remove_login(host)
        self._set_secret("login", host, "")

    def list_logins(self) -> list[dict[str, Any]]:
        return self.store.list_logins()  # never the passwords

    def _log(self, download_id: int, text: str) -> None:
        lines = self._logs.setdefault(download_id, deque(maxlen=LOG_LINES))
        lines.append(f"{time.strftime('%H:%M:%S')} {text}")

    def get_log(self, download_id: int) -> list[str]:
        self._get(download_id)
        return list(self._logs.get(download_id, ()))

    def _torrent_session(self) -> TorrentSession:
        if self._torrent is None:
            self._torrent = TorrentSession(self.store.get_settings(), self._torrent_listen_host,
                                           self._torrent_proxy())
            self._torrent.start()
        return self._torrent

    def _resume_seeding(self, d: DownloadRow) -> None:
        row = self.store.get_torrent(d.id)
        if not torrent.available() or not row or not row.resume_data:
            self.store.set_state(d.id, "done")
            return
        try:
            self._torrent_session().seed(d.id, row.resume_data, str(d.dir))
        except Exception as e:  # damaged resume data: just stop seeding
            log.warning("cannot seed download %s again: %s", d.id, e)
            self.store.set_state(d.id, "done")

    async def shutdown(self) -> None:
        """Stop all transfers; downloads that were running are queued again for the next start."""
        self._closing = True
        for timer in self._retry_timers.values():
            timer.cancel()
        self._retry_timers.clear()
        if self._progress_task:
            self._progress_task.cancel()
        if self._tick_task:
            self._tick_task.cancel()
        if self.inhibitor:
            self.inhibitor.close()
        for task in list(self._background):
            task.cancel()
        net.use_doh_for_process(None)
        running = list(self._tasks)
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for download_id in running:
            # Interrupted by the shutdown (now "paused" by the engine): queue again for the next start.
            # One that failed or finished while stopping keeps that state.
            if self.store.get(download_id).state in ("paused", "running", "processing"):
                self.store.set_state(download_id, "queued")
        if self._torrent is not None:  # seeding torrents stay "seeding" and restart next time
            for download_id in list(self._torrent.seeding):
                handle = self._torrent.handles.get(download_id)
                if handle is not None:
                    data = await self._torrent.save_resume(handle)
                    if data:
                        self.store.save_torrent(download_id, resume_data=data)
            await self._torrent.close()

    # --- commands --------------------------------------------------------------------------

    def add(self, url: str, *, dir: str | None = None, name: str | None = None,
            connections: int | None = None, headers: dict[str, str] | None = None,
            speed_limit: int = 0, start: bool = True, kind: str | None = None, **media_options) -> int:
        """`kind`: "http" (ldm's engine) or "media" (yt-dlp); default: media for .m3u8/.mpd links.
        Media options: format (yt-dlp selector), format_label, subtitles ("en,hi"), title, page_url."""
        unknown = set(media_options) - MEDIA_OPTIONS - TORRENT_OPTIONS - RUN_OPTIONS
        if unknown:
            raise InvalidArgument(f"unknown options: {', '.join(sorted(unknown))}")
        if not kind:
            path = urlsplit(url).path.lower()
            kind = ("torrent" if torrent.is_torrent_source(url) else
                    "media" if path.endswith(MEDIA_EXTENSIONS) else "http")
        if kind not in self._factories:
            raise InvalidArgument(f"unknown download kind: {kind}")
        if urlsplit(url).scheme not in SCHEMES[kind]:
            raise InvalidArgument(f"unsupported URL: {url}")
        if kind == "torrent" and not torrent.available():
            raise InvalidArgument(torrent.INSTALL_HINT)
        torrent_options = {k: media_options.pop(k) for k in list(media_options) if k in TORRENT_OPTIONS}
        settings = self.store.get_settings()
        if kind == "torrent":
            self._refuse_duplicate_torrent(url, torrent_options.get("metadata"), settings)
        connections = connections or settings["default_connections"]
        if not 1 <= connections <= MAX_CONNECTIONS:
            raise InvalidArgument(f"connections must be 1-{MAX_CONNECTIONS}")
        options = {k: v for k, v in media_options.items()
                   if v not in (None, "") and k not in WINDOW_KEYS}  # "" days = every day: handled below
        options.update(self._own_window(media_options, settings))
        if "scheduled" not in options and settings["schedule_new_links"]:
            options["scheduled"] = True
        for field in ("hls_key", "hls_iv"):  # stored as hex
            if options.get(field):
                try:
                    options[field] = aes_hex(options[field], "key" if field == "hls_key" else "IV")
                except ValueError as e:
                    raise InvalidArgument(str(e)) from None
        if kind == "media":
            options["custom_name"] = bool(name)
            name = name or options.get("title")
        if torrent_options.get("peers"):
            options["peers"] = list(torrent_options["peers"])
        download_id = self.store.add(url, dir or settings["download_dir"], name=name, headers=headers,
                                     connections=connections, state="queued" if start else "paused",
                                     kind=kind, options=options)
        try:
            self._save_torrent_options(download_id, kind, torrent_options, settings)
        except Exception:
            self.store.delete(download_id)  # all or nothing: no half-added downloads in the queue
            raise
        if speed_limit:
            self.store.update(download_id, speed_limit=speed_limit)
        self._emit(self.on_added, download_id)
        self._emit_state(download_id)
        self._schedule()
        return download_id

    def _save_torrent_options(self, download_id: int, kind: str, torrent_options: dict[str, Any],
                              settings: dict[str, Any]) -> None:
        if kind == "torrent":
            priorities = torrent_options.get("priorities") or []
            if isinstance(priorities, str):
                priorities = json.loads(priorities)
            metadata = torrent_options.get("metadata") or None
            if isinstance(metadata, list):  # PyGObject clients unpack D-Bus byte arrays into lists
                metadata = bytes(metadata)
            self.store.save_torrent(download_id, metadata=metadata,
                                    priorities=[int(p) for p in priorities],
                                    sequential=bool(torrent_options.get("sequential",
                                                                        settings["torrent_sequential"])))

    def pause(self, download_id: int) -> None:
        d = self._get(download_id)
        self._cancel_retry(download_id)
        self._holding.discard(download_id)  # the user's pause wins over a condition hold
        self._waiting.pop(download_id, None)
        if d.state == "seeding":  # pausing a seeding torrent stops seeding
            self._stop_seeding(download_id)
            return
        if self._active(download_id):
            self._tasks[download_id].cancel()  # the engine saves progress and marks it paused
        elif d.state in ("queued", "error"):
            self.store.set_state(download_id, "paused")
            self._emit_state(download_id)

    def pause_all(self) -> int:
        """Pause everything that runs or waits to run (seeding torrents keep seeding)."""
        ids = [d.id for d in self.store.list() if d.state in ("queued", "running", "processing")]
        for download_id in ids:
            self.pause(download_id)
        return len(ids)

    def resume_all(self) -> int:
        """Resume paused downloads and retry failed ones (expired links and changed files need a decision)."""
        ids = [d.id for d in self.store.list() if d.state in ("paused", "error")]
        for download_id in ids:
            self.resume(download_id)
        return len(ids)

    def resume(self, download_id: int) -> None:
        d = self._get(download_id)
        if d.state not in RESUMABLE_STATES:
            return
        self._cancel_retry(download_id)
        self._attempts.pop(download_id, None)
        if d.state == "changed":
            self._reset(download_id)  # remote file changed: start over
        self.store.set_state(download_id, "queued")
        self._emit_state(download_id)
        self._schedule()

    async def remove(self, download_id: int, delete_files: bool = False) -> None:
        d = self._get(download_id)
        self._cancel_retry(download_id)
        self._holding.discard(download_id)
        self._waiting.pop(download_id, None)
        if self._torrent is not None:
            self._torrent.remove(download_id)
        task = self._tasks.pop(download_id, None)
        if task:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            self._forget(download_id)
        self.store.delete(download_id)
        self._logs.pop(download_id, None)
        if delete_files:
            if d.kind == "torrent" and d.name:
                target = d.dir / d.name
                shutil.rmtree(target, ignore_errors=True) if target.is_dir() else target.unlink(missing_ok=True)
            elif d.kind == "media":
                shutil.rmtree(media.media_temp_dir(d), ignore_errors=True)
            elif d.name:
                d.temp_path.unlink(missing_ok=True)
            if d.state == "done" and d.name:
                d.dest.unlink(missing_ok=True)
        self._emit(self.on_removed, download_id)
        self._schedule()

    def move(self, download_id: int, position: int) -> None:
        self._get(download_id)
        self.store.move(download_id, position)
        self._schedule()

    async def set_url(self, download_id: int, url: str) -> None:
        """Give a download a new link (e.g. after it expired), keeping downloaded data if possible."""
        d = self._get(download_id)
        if self._active(download_id):
            raise Busy("pause the download before changing its URL")
        if urlsplit(url).scheme not in ("http", "https"):
            raise InvalidArgument(f"unsupported URL: {url}")
        if d.final_url is None or d.size is None:
            self.store.update(download_id, url=url)
            self._reset(download_id)
        else:
            async with new_client(d.headers, net=await self._net().resolve(url)) as client:
                p = await probe(client, url)
            if p.size != d.size:
                raise SizeMismatch(f"new link is {p.size} bytes, download is {d.size} bytes")
            self.store.update(download_id, url=url, final_url=p.final_url, etag=p.etag,
                              last_modified=p.last_modified, resumable=p.resumable)
        if d.state != "done":
            self.store.set_state(download_id, "queued")
            self._emit_state(download_id)
            self._schedule()

    async def probe(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        """Look at a link without downloading it (for the add dialog): size, resume support, name."""
        if urlsplit(url).scheme not in ("http", "https"):
            raise InvalidArgument(f"unsupported URL: {url}")
        try:
            net = await self._net().resolve(url)
            async with new_client(headers, net=net) as client:
                p = await probe(client, url)
        except httpx.HTTPError as e:
            raise DownloadError(f"could not reach the server: {e}") from e
        return {"final_url": p.final_url, "size": p.size, "resumable": p.resumable,
                "filename": p.filename, "content_type": p.content_type}

    async def probe_torrent(self, source: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        """Name, size and file list of a magnet, .torrent URL or local .torrent file."""
        if not torrent.available():
            raise InvalidArgument(torrent.INSTALL_HINT)
        if urlsplit(source).scheme == "magnet":
            params = torrent.lt.parse_magnet_uri(source)
            result = {"name": params.name or "", "size": None, "files": [], "is_magnet": True,
                      "info_hash": str(params.info_hashes.get_best()), "trackers": list(params.trackers)}
            if self.store.get_settings()["torrent_fetch_metadata"]:
                info = await self._torrent_session().fetch_metadata(source)
                result.update(torrent.describe_info(info), metadata=torrent.metadata_bytes(info))
            return result
        try:
            data = await torrent.read_source(source, headers, self._net())
            info = torrent.lt.torrent_info(torrent.lt.bdecode(data))
        except (OSError, httpx.HTTPError) as e:
            raise DownloadError(f"could not read the torrent: {e}") from e
        except RuntimeError as e:
            raise DownloadError(f"not a valid .torrent file: {e}") from e
        return {**torrent.describe_info(info), "metadata": data, "is_magnet": False}

    def torrent_details(self, download_id: int) -> dict[str, Any]:
        d = self._get(download_id)
        live = self._torrent.details(download_id) if self._torrent else None
        if live is not None:
            return live
        row = self.store.get_torrent(download_id)
        info = {"peers": 0, "seeds": 0, "download_speed": 0, "upload_speed": 0, "seeding": False,
                "uploaded": row.uploaded if row else 0, "peer_list": [], "trackers": []}
        if row and row.metadata and torrent.available():
            described = torrent.describe_info(torrent.lt.torrent_info(torrent.lt.bdecode(row.metadata)))
            info["files"] = [{**f, "done": f["size"] if d.state == "done" else 0,
                              "wanted": not row.priorities or row.priorities[f["index"]] > 0}
                             for f in described["files"]]
            info["trackers"] = [{"url": t, "message": ""} for t in described["trackers"]]
        return info

    def _stop_seeding(self, download_id: int) -> None:
        if self._torrent is not None:
            self._torrent.remove(download_id)
        self.store.set_state(download_id, "done")
        self._emit_state(download_id)
        self._after_finish(download_id)
        self._check_all_finished()

    async def probe_media(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        """Title, qualities and subtitles yt-dlp can get from a page or stream playlist."""
        if urlsplit(url).scheme not in ("http", "https"):
            raise InvalidArgument(f"unsupported URL: {url}")
        return await asyncio.to_thread(media.probe_media, url, headers, self.store.get_settings(), self._net())

    def set_schedule(self, download_id: int, scheduled: bool, window: dict[str, str] | None = None) -> None:
        """Put a download on the schedule or take it off. `window` (schedule_start/stop/days) gives it its
        own times; without it the download follows the default times from the settings."""
        d = self._get(download_id)
        unknown = set(window or {}) - set(WINDOW_KEYS)
        if unknown:
            raise InvalidArgument(f"unknown schedule keys: {', '.join(sorted(unknown))}")
        options = {k: v for k, v in d.options.items() if k not in WINDOW_KEYS}
        options.update(self._own_window(window or {}, self.store.get_settings()), scheduled=bool(scheduled))
        self.store.update(download_id, options_json=json.dumps(options))
        self.conditions_changed()

    def _own_window(self, values: dict[str, Any], settings: dict[str, Any]) -> dict[str, str]:
        """Validated own times; {} when none are given or they equal the defaults (it then follows them)."""
        if not any(values.get(k) is not None for k in WINDOW_KEYS):
            return {}
        window = {k: values[k] if values.get(k) is not None else settings[k] for k in WINDOW_KEYS}
        for key, value in window.items():
            self._check_setting(key, value)
        if (parse_days(window["schedule_days"]) == parse_days(settings["schedule_days"])
                and all(parse_time(window[k]) == parse_time(settings[k]) for k in WINDOW_KEYS[:2])):
            return {}
        return window

    @staticmethod
    def _window(d: DownloadRow, settings: dict[str, Any]) -> tuple[str, str, str]:
        return tuple(d.options.get(k, settings[k]) for k in WINDOW_KEYS)

    def conditions_changed(self) -> None:
        """Network/battery/schedule changed: hold or release downloads accordingly."""
        if self._closing:
            return
        for download_id in list(self._tasks):
            if not self._active(download_id):
                continue
            reason = self._hold_reason(self.store.get(download_id))
            if reason:
                self._holding.add(download_id)
                self._waiting[download_id] = reason
                self._tasks[download_id].cancel()
        self._schedule()

    def set_speed_limit(self, download_id: int, rate: int) -> None:
        self._get(download_id)
        self.store.update(download_id, speed_limit=rate)
        if download_id in self._buckets:
            self._buckets[download_id].set_rate(rate)

    def settings(self) -> dict[str, Any]:
        settings = self.store.get_settings()
        runtime = media.find_js_runtime(settings["js_runtime"])  # read-only, shown in preferences
        settings["js_runtime_detected"] = f"{runtime[0]}: {runtime[1]}" if runtime else ""
        settings["proxy_password_set"] = ("proxy", "proxy") in self._secrets  # read-only
        settings["data_used"] = self.session_bytes                          # read-only
        settings["backup_password_set"] = ("backup", "backup") in self._secrets
        settings["backup_folder"] = str(self._backup_folder(settings))
        settings["passwords_in"] = getattr(self._passwords, "where", "")     # keyring / file / memory
        return settings

    def update_settings(self, values: dict[str, Any]) -> None:
        values = dict(values)
        password = values.pop("proxy_password", None)  # not settings: they go to the keyring
        backup_password = values.pop("backup_password", None)
        for secret in (password, backup_password):
            if secret is not None and not isinstance(secret, str):
                raise InvalidArgument("passwords must be strings")
        if backup_password is not None:
            self._set_secret("backup", "backup", backup_password)
        for key, value in values.items():
            self._check_setting(key, value)
        for key, value in values.items():
            self.store.set_setting(key, value)
        if password is not None:
            self._set_secret("proxy", "proxy", password)
        if any(k in values for k in ("dns_mode", "doh_url", "connect_timeout")):
            self._apply_dns()
        if self._torrent is not None and (password is not None or any(k.startswith("proxy_") for k in values)):
            self._torrent.apply(self.store.get_settings(), self._torrent_proxy())
        if "speed_limit" in values:
            self._global.set_rate(values["speed_limit"])
        if self._torrent is not None and any(k.startswith("torrent_") for k in values):
            self._torrent.apply(self.store.get_settings(), self._torrent_proxy())
        if any(k in values for k in ("torrent_tracker_list", "torrent_tracker_list_url")):
            self._refresh_tracker_list(force=True)
        if any(k.startswith("schedule_") or k in ("pause_on_metered", "battery_limit", "prevent_sleep", "data_limit")
               for k in values):
            self.conditions_changed()
        if "autostart" in values and self.on_autostart:
            self.on_autostart(values["autostart"])
        self._schedule()

    @staticmethod
    def _check_setting(key: str, value: Any) -> None:
        """Values must match the type of the default; a few settings have narrower rules."""
        if key not in DEFAULT_SETTINGS:
            raise InvalidArgument(f"unknown setting: {key}")
        if key == "dns_mode" and value == "doh" and not net.doh_available():
            raise InvalidArgument("DNS-over-HTTPS needs python3-dns: sudo dnf install python3-dns")
        choices = {"media_container": ("mp4", "mkv"), "torrent_encryption": ("prefer", "require", "off"),
                   "proxy_mode": ("system", "none", "manual"), "dns_mode": ("system", "doh"),
                   "finish_action": ("nothing", "quit", "suspend", "poweroff"),
                   "backup_schedule": ("off", "daily", "weekly")}
        if key in choices:
            if value not in choices[key]:
                raise InvalidArgument(f"{key} must be one of: {', '.join(choices[key])}")
        elif key == "media_quality":
            if not (value in ("best", "audio") or (isinstance(value, int) and not isinstance(value, bool)
                                                  and value > 0) or (isinstance(value, str) and value.isdigit())):
                raise InvalidArgument("media_quality must be best, audio or a height such as 1080")
        elif key == "download_dir":
            if not isinstance(value, str) or not value:
                raise InvalidArgument("download_dir must be a path")
        elif key in ("schedule_start", "schedule_stop", "schedule_days"):
            try:
                (parse_days if key == "schedule_days" else parse_time)(value)
            except (ValueError, AttributeError) as e:
                raise InvalidArgument(f"{key}: {e}") from None
        elif key == "battery_limit" and not (isinstance(value, int) and 0 <= value <= 100):
            raise InvalidArgument("battery_limit must be 0-100")
        elif key == "proxy_url" and value:
            try:
                check_proxy_url(value)
            except (ValueError, AttributeError) as e:
                raise InvalidArgument(str(e)) from None
        elif key == "doh_url" and not (value in DOH_PRESETS or (isinstance(value, str) and
                                       urlsplit(value).scheme in ("https", "http") and urlsplit(value).hostname)):
            raise InvalidArgument(f"doh_url must be {', '.join(DOH_PRESETS)} or an https:// URL")
        elif key == "torrent_tracker_list_url" and not (isinstance(value, str) and urlsplit(value).scheme
                                                         in ("http", "https") and urlsplit(value).hostname):
            raise InvalidArgument("the tracker list address must be an http(s) link")
        elif key == "user_agent" and not (isinstance(value, str) and value.strip()):
            raise InvalidArgument("user_agent must be default, firefox, chrome, mobile or a custom string")
        elif key in ("connect_timeout", "read_timeout") and not (isinstance(value, int) and 1 <= value <= 600):
            raise InvalidArgument(f"{key} must be 1-600 seconds")
        elif key in ("move_finished_dir", "move_finished_torrent_dir", "backup_dir") and value and not (
                isinstance(value, str) and Path(value).expanduser().is_absolute()):
            raise InvalidArgument(f"{key} must be an absolute folder path or empty")
        else:
            default = DEFAULT_SETTINGS[key]
            if isinstance(default, bool):
                ok = isinstance(value, bool)
            elif isinstance(default, int):
                ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
            elif isinstance(default, float):
                ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
            else:
                ok = isinstance(value, str)
            if not ok:
                raise InvalidArgument(f"{key} must be {type(default).__name__}"
                                      + (" ≥ 0" if isinstance(default, (int, float)) and not isinstance(default, bool) else ""))
        if key == "max_parallel" and value < 1:
            raise InvalidArgument("max_parallel must be at least 1")
        if key == "default_connections" and not 1 <= value <= MAX_CONNECTIONS:
            raise InvalidArgument(f"default_connections must be 1-{MAX_CONNECTIONS}")

    def set_host_rule(self, host: str, max_connections: int = 0, max_downloads: int = 0,
                      include_subdomains: bool = True) -> None:
        if not host or "/" in host:
            raise InvalidArgument(f"invalid host: {host!r}")
        if not 0 <= max_connections <= MAX_CONNECTIONS or max_downloads < 0:
            raise InvalidArgument("limits out of range")
        self.store.set_host_rule(host, max_connections=max_connections, max_downloads=max_downloads,
                                 include_subdomains=include_subdomains)
        self._schedule()

    def remove_host_rule(self, host: str) -> None:
        self.store.remove_host_rule(host)
        self._schedule()

    def host_rules(self) -> list[HostRule]:
        return self.store.list_host_rules()

    # --- duplicates, export/import -------------------------------------------------------------

    def find_duplicates(self, url: str) -> list[dict[str, Any]]:
        """Downloads of the same link (1DM: "Download link already exists!"); torrents by info hash."""
        key = _link_key(url)
        found = [d.id for d in self.store.list() if key in (_link_key(d.url), _link_key(d.final_url or ""))]
        if not found and urlsplit(url).scheme == "magnet":
            existing = self._torrent_with_hash(torrent.identity(url)[0])
            found = [existing] if existing else []
        return [self.describe(i) for i in found]

    # --- torrent duplicates and tracker lists ----------------------------------------------

    def _torrent_hash(self, d: DownloadRow) -> str | None:
        row = self.store.get_torrent(d.id)
        if row and row.info_hash:
            return row.info_hash
        return torrent.identity(d.url, row.metadata if row else None)[0]

    def _torrent_with_hash(self, info_hash: str | None) -> int | None:
        if not info_hash:
            return None
        return next((d.id for d in self.store.list() if d.kind == "torrent" and self._torrent_hash(d) == info_hash),
                    None)

    def _refuse_duplicate_torrent(self, url: str, metadata: Any, settings: dict[str, Any]) -> None:
        """1DM: "add trackers to duplicates" — a torrent already in the list gets the new trackers instead."""
        if isinstance(metadata, list):
            metadata = bytes(metadata)
        info_hash, trackers = torrent.identity(url, metadata or None)
        existing = self._torrent_with_hash(info_hash)
        if existing is None:
            return
        added = self._merge_trackers(existing, trackers) if settings["torrent_merge_duplicates"] else 0
        name = self.store.get(existing).name or "this torrent"
        raise Duplicate(f"“{name}” is already in your list"
                        + (f" — added {added} new tracker{'s' if added != 1 else ''} to it" if added else ""))

    def _merge_trackers(self, download_id: int, trackers: list[str]) -> int:
        d = self.store.get(download_id)
        row = self.store.get_torrent(download_id)
        known = set(torrent.identity(d.url, row.metadata if row else None)[1]) | set(d.options.get("trackers", []))
        new = [t for t in dict.fromkeys(trackers) if t not in known]
        if new:
            self.store.update(download_id, options_json=json.dumps(
                {**d.options, "trackers": d.options.get("trackers", []) + new}))
            if self._torrent is not None:
                self._torrent.add_trackers(new, download_id)
        return len(new)

    def _refresh_tracker_list(self, force: bool = False) -> None:
        """The public tracker list (when on): fetched daily, added to running torrents too."""
        settings = self.store.get_settings()
        if not settings["torrent_tracker_list"] or self._tracker_fetch is not None:
            return  # off, or a fetch is already running
        now = self._clock().timestamp()
        if not force and settings["torrent_tracker_list_cache"] and now - settings["torrent_tracker_list_fetched"] < 86400:
            return
        self.store.set_setting("torrent_tracker_list_fetched", now)

        async def fetch() -> None:
            url = settings["torrent_tracker_list_url"]
            try:
                async with new_client(net=await self._net().resolve(url)) as client:
                    response = await client.get(url)
                    response.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("could not fetch the tracker list: %s", e)
                return
            trackers = [t for t in torrent.tracker_lines(response.text)
                        if urlsplit(t).scheme in ("udp", "http", "https", "ws", "wss")]
            self.store.set_setting("torrent_tracker_list_cache", "\n".join(trackers))
            log.info("tracker list: %d trackers", len(trackers))
            if self._torrent is not None:
                self._torrent.add_trackers(trackers)

        self._tracker_fetch = asyncio.get_running_loop().create_task(fetch())
        self._background.add(self._tracker_fetch)
        self._tracker_fetch.add_done_callback(self._tracker_fetch_done)

    def _tracker_fetch_done(self, task: asyncio.Task) -> None:
        self._background.discard(task)
        self._tracker_fetch = None

    def export_downloads(self, unfinished_only: bool = True) -> str:
        return json.dumps(transfer.export_downloads(self.store, unfinished_only), indent=1)

    def import_downloads(self, text: str) -> list[int]:
        """Downloads from an export file; they arrive paused (finished ones as finished)."""
        try:
            ids = transfer.import_downloads(self.store, text)
        except (ValueError, KeyError, TypeError) as e:
            raise InvalidArgument(f"could not import: {e}") from None
        for download_id in ids:
            self._emit(self.on_added, download_id)
            self._emit_state(download_id)
        return ids

    # --- queries ---------------------------------------------------------------------------

    def describe(self, download_id: int) -> dict[str, Any]:
        d = self._get(download_id)
        if download_id in self._progress:
            done, _, speed = self._progress[download_id]
        else:
            done = (d.size if d.state == "done" else d.done_bytes if d.kind != "http"
                    else sum(p.done for p in self.store.load_parts(download_id)))
            speed = 0.0
        extra = {}
        if d.kind == "torrent" and self._torrent is not None:
            live = self._torrent.details(download_id)
            if live:
                extra = {k: live[k] for k in ("peers", "seeds", "upload_speed", "ratio")}
        if d.state == "queued" and download_id in self._waiting:
            extra["waiting"] = self._waiting[download_id]
        extra.update(zip(WINDOW_KEYS, self._window(d, self.store.get_settings())))
        extra["schedule_custom"] = any(k in d.options for k in WINDOW_KEYS)
        return {
            **extra,
            "scheduled": bool(d.options.get("scheduled")), "unmetered_only": bool(d.options.get("unmetered_only")),
            "id": d.id, "url": d.url, "kind": d.kind, "quality": d.options.get("format_label"),
            "name": d.name, "dir": str(d.dir), "state": d.state,
            "error": d.error, "size": d.size, "done": done or 0, "speed": speed,
            "resumable": d.resumable, "connections": d.connections, "position": d.position,
            "speed_limit": d.speed_limit, "added_at": d.added_at, "finished_at": d.finished_at,
        }

    def list(self) -> list[dict[str, Any]]:
        return [self.describe(d.id) for d in self.store.list()]

    @property
    def running(self) -> set[int]:
        return set(self._tasks)

    def summary(self) -> tuple[int, int, int, float]:
        """(running downloads, bytes done, bytes total of those with a known size, speed) for the tray."""
        running = [i for i in self._tasks if self._active(i)]
        done = total = 0
        speed = 0.0
        for download_id in running:
            got, size, rate = self._progress.get(download_id, (0, None, 0.0))
            speed += rate
            if size:
                done, total = done + got, total + size
        return len(running), done, total, speed

    # --- internals -------------------------------------------------------------------------

    def _active(self, download_id: int) -> bool:
        """Running, as opposed to finished but not yet collected by `_finished`."""
        task = self._tasks.get(download_id)
        return task is not None and not task.done()

    def _get(self, download_id: int) -> DownloadRow:
        try:
            return self.store.get(download_id)
        except KeyError:
            raise NotFound(f"no download with id {download_id}") from None

    def _reset(self, download_id: int) -> None:
        self.store.save_parts(download_id, [])
        self.store.update(download_id, final_url=None, size=None, etag=None, last_modified=None,
                          resumable=False)

    def _rule_key(self, d: DownloadRow) -> tuple[str | None, HostRule | None]:
        rule = self.store.host_rule_for(d.host)
        return (rule.host if rule else None), rule

    def _hold_reason(self, d: DownloadRow) -> str | None:
        """Why `d` may not run right now (schedule window, metered network, battery), or None."""
        settings = self.store.get_settings()
        if d.options.get("scheduled"):
            now = self._clock()
            args = self._window(d, settings)
            if not in_window(now, *args):
                when = next_start(now, *args)
                return f"scheduled — starts {describe_start(when, now)}" if when else "scheduled"
        if (settings["pause_on_metered"] or d.options.get("unmetered_only")) and self.conditions.metered:
            return "metered connection"
        limit = settings["battery_limit"]
        if limit and self.conditions.on_battery and self.conditions.battery < limit:
            return f"battery below {limit}%"
        if settings["data_limit"] and self.session_bytes >= settings["data_limit"]:
            return "data limit reached"
        return None

    def _schedule(self) -> None:
        if self._closing:
            return
        max_parallel = self.store.get_settings()["max_parallel"]
        per_rule = Counter(key for key in self._rule_keys.values() if key)
        for d in self.store.queued():
            reason = self._hold_reason(d)
            if reason:
                if self._waiting.get(d.id) != reason:
                    self._waiting[d.id] = reason
                    self._emit_state(d.id)  # front ends re-read the row and show the reason
                continue
            if self._waiting.pop(d.id, None) is not None:
                self._emit_state(d.id)
            if len(self._tasks) >= max_parallel:
                break
            key, rule = self._rule_key(d)
            if rule and rule.max_downloads and per_rule[key] >= rule.max_downloads:
                continue
            per_rule[key] += 1
            self._start(d, key, rule)
        self._update_inhibitor()

    def _update_inhibitor(self) -> None:
        if self.inhibitor is not None:
            self.inhibitor.set_active(bool(self._tasks) and self.store.get_settings()["prevent_sleep"])

    def _start(self, d: DownloadRow, key: str | None, rule: HostRule | None) -> None:
        rate = d.speed_limit
        settings = self.store.get_settings()
        if d.options.get("scheduled") and settings["schedule_speed_limit"]:
            rate = min(r for r in (rate, settings["schedule_speed_limit"]) if r)
        bucket = TokenBucket(rate)
        extra = {"on_state": lambda state, i=d.id: self._emit_state(i)} if d.kind in ("media", "torrent") else {}
        extra["on_event"] = partial(self._log, d.id)
        self._log(d.id, f"starting ({d.kind}) {d.url}")
        if d.kind in ("http", "media"):
            extra["net"] = self._net()
        if d.kind == "http":
            extra["restart_non_resumable"] = settings["restart_non_resumable"]
        connections = (rule.max_connections or None) if rule else None
        if d.kind == "http":
            host = (urlsplit(d.final_url or d.url).hostname or "").lower()
            extra["on_throttled"] = lambda n, h=host: self._throttled_hosts.__setitem__(h, n)
            if host in self._throttled_hosts:  # this session it said "too many": start with what worked
                connections = min(c for c in (connections, self._throttled_hosts[host]) if c)
        downloader = self._factories[d.kind](
            self.store, d.id, limiters=[self._global, bucket],
            connections=connections,
            on_progress=partial(self._on_progress, d.id), progress_interval=self._progress_interval,
            **extra, **self._options,
        )
        self.store.set_state(d.id, "running")
        self._emit(self.on_started, d.id)
        task = asyncio.create_task(downloader.run())
        self._tasks[d.id] = task
        self._downloaders[d.id] = downloader
        # data usage counts from here (bytes downloaded in earlier sessions don't count)
        self._last_done[d.id] = (sum(p.done for p in self.store.load_parts(d.id)) if d.kind == "http"
                                 else d.done_bytes)
        self._rule_keys[d.id] = key
        self._buckets[d.id] = bucket
        task.add_done_callback(partial(self._finished, d.id))
        self._emit_state(d.id)

    def _check_all_finished(self) -> None:
        """1DM's "app behavior after all downloads are finished": called when a download ends by itself
        (finished or failed), never because the user paused one."""
        if self._closing or self._tasks or self._retry_timers:
            return  # still running, or a failed one will be retried
        action = self.store.get_settings()["finish_action"]
        if action == "nothing":
            return
        if any(d.state in ("queued", "running", "processing") for d in self.store.list()):
            return  # still something to do (also: waiting for the schedule or a condition)
        self._emit(self.on_all_finished, action)

    def _save_progress(self, download_id: int) -> None:
        """yt-dlp and torrent downloads only report progress live: keep the last numbers for a paused row."""
        if download_id not in self._progress:
            return
        try:
            d = self.store.get(download_id)
        except KeyError:
            return
        if d.kind == "http" or d.state == "done":
            return
        done, size, _ = self._progress[download_id]
        fields = {"done_bytes": int(done)}
        if d.size is None and size:
            fields["size"] = int(size)
        self.store.update(download_id, **fields)

    def parts(self, download_id: int) -> list[dict[str, Any]]:
        """The byte ranges of a multi-part download and how far each got (1DM: "show multi-part")."""
        d = self._get(download_id)
        if d.kind != "http":
            return []
        engine = self._downloaders.get(download_id)
        live = getattr(engine, "parts", None)
        parts = list(live) if live else self.store.load_parts(download_id)
        return [{"first": p.first, "last": p.last, "done": p.done, "active": bool(p.active)} for p in parts]

    def _forget(self, download_id: int) -> None:
        self._downloaders.pop(download_id, None)
        self._last_done.pop(download_id, None)
        self._rule_keys.pop(download_id, None)
        self._buckets.pop(download_id, None)
        self._progress.pop(download_id, None)

    def _finished(self, download_id: int, task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()  # the engine already recorded it in the store
        if self._tasks.get(download_id) is not task:
            return  # removed while running
        del self._tasks[download_id]
        self._save_progress(download_id)
        self._forget(download_id)
        if self._closing:
            return
        try:
            d = self.store.get(download_id)
        except KeyError:
            return
        if download_id in self._holding:  # stopped by a condition: back to the queue, not paused
            self._holding.discard(download_id)
            if d.state in ("paused", "running", "processing"):
                self.store.set_state(download_id, "queued")
        elif d.state in ("running", "processing"):  # cancelled before the engine started
            self.store.set_state(download_id, "paused")
        elif d.state == "error" and not (d.error or "").startswith("login required"):
            settings = self.store.get_settings()
            attempt = self._attempts.get(download_id, 0) + 1
            if settings["always_retry"] or attempt <= settings["retry_count"]:
                self._attempts[download_id] = attempt
                self._retry_timers[download_id] = asyncio.get_running_loop().call_later(
                    settings["retry_interval"], self._auto_retry, download_id)
        elif d.state == "done":
            self._attempts.pop(download_id, None)
        self._emit_state(download_id)
        if d.state == "done":
            self._after_finish(download_id)
        self._schedule()
        if d.state in ("done", "seeding", "error", "expired", "changed"):  # ended by itself, not paused
            self._check_all_finished()

    async def _condition_loop(self) -> None:
        """Schedule windows open and close with the clock: re-check twice a minute."""
        while True:
            self._cleanup()
            self._scheduled_backup()
            self._refresh_tracker_list()
            await asyncio.sleep(CONDITION_TICK)
            self.conditions_changed()

    # --- files -----------------------------------------------------------------------------

    async def checksum(self, download_id: int, algorithm: str = "sha256") -> str:
        d = self._get(download_id)
        if d.state not in ("done", "seeding") or not d.name:
            raise InvalidArgument("the download isn't finished")
        try:
            return await asyncio.to_thread(files.checksum, d.dest, algorithm)
        except (ValueError, OSError) as e:
            raise InvalidArgument(str(e)) from None

    async def rename(self, download_id: int, name: str) -> str:
        try:
            return await self._relocate(download_id, name=files.check_name(name))
        except ValueError as e:
            raise InvalidArgument(str(e)) from None

    async def move_to(self, download_id: int, folder: str) -> str:
        if not folder or not Path(folder).expanduser().is_absolute():
            raise InvalidArgument("choose a folder (an absolute path)")
        return await self._relocate(download_id, folder=Path(folder).expanduser())

    async def _relocate(self, download_id: int, *, name: str | None = None, folder: Path | None = None) -> str:
        """Rename/move a download: the finished file, or the partial data of one that isn't running."""
        d = self._get(download_id)
        task = self._tasks.get(download_id)
        if task is not None and d.state == "done":  # finished, still closing its connections
            await asyncio.wait([task], timeout=10)
        if self._active(download_id) or d.state in ("running", "processing", "seeding"):
            raise Busy("pause the download first" if d.state != "seeding" else "stop seeding first")
        folder = folder or d.dir
        name = name or d.name
        if d.state == "done" and d.name:
            try:
                final = await asyncio.to_thread(files.move, d.dest, folder / name)
            except OSError as e:
                raise InvalidArgument(f"could not move the file: {e}") from None
            self.store.update(download_id, dir=str(final.parent), name=final.name)
        elif d.kind == "torrent" and d.name:
            raise InvalidArgument("torrents can be moved or renamed after they finish")
        else:
            moved = DownloadRow(**{**vars(d), "dir": folder, "name": name})
            try:
                if d.kind == "http" and d.name and d.temp_path.exists() and d.temp_path != moved.temp_path:
                    await asyncio.to_thread(files.move, d.temp_path, moved.temp_path)
                elif d.kind == "media" and folder != d.dir and media.media_temp_dir(d).is_dir():
                    await asyncio.to_thread(files.move, media.media_temp_dir(d), media.media_temp_dir(moved))
            except OSError as e:
                raise InvalidArgument(f"could not move the partial download: {e}") from None
            options = {**d.options, "custom_name": True} if d.kind == "media" and name != d.name else d.options
            self.store.update(download_id, dir=str(folder), name=name, options_json=json.dumps(options))
        self._emit_state(download_id)
        d = self.store.get(download_id)
        return str(d.dir / (d.name or ""))

    def _after_finish(self, download_id: int) -> None:
        """1DM's "move files after download finishes" and "remove link when download finishes"."""
        settings = self.store.get_settings()
        d = self.store.get(download_id)
        target = settings["move_finished_torrent_dir" if d.kind == "torrent" else "move_finished_dir"]
        move = bool(target and d.name and Path(target).expanduser() != d.dir)
        if not move and not settings["remove_finished"]:
            return

        async def run() -> None:
            if move:
                try:
                    final = await asyncio.to_thread(files.move, d.dest, Path(target).expanduser() / d.name)
                    self.store.update(download_id, dir=str(final.parent), name=final.name)
                    self._log(download_id, f"moved to {final}")
                    self._emit_state(download_id)
                except (OSError, KeyError) as e:
                    self._log(download_id, f"could not move to {target}: {e}")
            if settings["remove_finished"]:
                with contextlib.suppress(KeyError, ManagerError):
                    await self.remove(download_id)

        self._spawn(run())

    def _cleanup(self) -> None:
        """Remove list entries older than `cleanup_days` (finished files stay; partial data goes)."""
        if time.monotonic() - self._last_cleanup < CLEANUP_INTERVAL:
            return
        self._last_cleanup = time.monotonic()
        settings = self.store.get_settings()
        if not settings["cleanup_days"]:
            return
        cutoff = self._clock().timestamp() - settings["cleanup_days"] * 86400
        for d in self.store.list():
            if d.state == "done":
                if (d.finished_at or d.added_at) < cutoff:
                    self._spawn(self.remove(d.id))
            elif (not settings["cleanup_finished_only"] and d.added_at < cutoff and not self._active(d.id)
                  and d.state != "seeding"):
                self._spawn(self.remove(d.id, delete_files=True))

    # --- backup and restore ----------------------------------------------------------------

    def _backup_folder(self, settings: dict[str, Any]) -> Path:
        return Path(settings["backup_dir"]).expanduser() if settings["backup_dir"] else backup.default_folder()

    def _backup_data(self, with_secrets: bool) -> dict[str, Any]:
        settings = {k: v for k, v in self.store.get_settings().items()
                    if k in DEFAULT_SETTINGS and k not in backup.NOT_RESTORED}
        logins = self.store.list_logins()
        data: dict[str, Any] = {
            "format": backup.FORMAT, "version": backup.VERSION, "created": time.time(),
            "settings": settings, "host_rules": [vars(rule) for rule in self.store.list_host_rules()],
            "logins": logins, "downloads": transfer.export_downloads(self.store, unfinished_only=False)["downloads"],
        }
        if with_secrets:  # only into encrypted backups
            for login in logins:
                login["password"] = self._secrets.get(("login", login["host"]), "")
            data["proxy_password"] = self._secrets.get(("proxy", "proxy"), "")
        return data

    async def create_backup(self, target: str = "", password: str = "") -> str:
        """Write a backup; `target` is a file, a folder, or "" for the backup folder. Returns the file."""
        path = Path(target).expanduser() if target else self._backup_folder(self.store.get_settings())
        if not target or path.is_dir() or path.suffix not in (".json", ".ldmbackup"):  # a folder
            path = path / backup.file_name(bool(password))
        blob = backup.encode(self._backup_data(with_secrets=bool(password)), password)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(blob)

        try:
            await asyncio.to_thread(write)
        except OSError as e:
            raise InvalidArgument(f"could not write the backup: {e}") from None
        return str(path)

    async def restore_backup(self, source: str, password: str = "") -> dict[str, int]:
        """Settings, host rules and logins from a backup; downloads that aren't in the list are added (paused)."""
        try:
            blob = await asyncio.to_thread(Path(source).expanduser().read_bytes)
            data = backup.decode(blob, password)
        except OSError as e:
            raise InvalidArgument(f"could not read the backup: {e}") from None
        except backup.BackupError as e:
            raise InvalidArgument(str(e)) from None
        restored = {"settings": 0, "host_rules": 0, "logins": 0, "downloads": 0}
        for key, value in (data.get("settings") or {}).items():
            if key in DEFAULT_SETTINGS and key not in backup.NOT_RESTORED:
                try:
                    self.update_settings({key: value})
                    restored["settings"] += 1
                except InvalidArgument as e:  # e.g. a folder that doesn't exist here
                    log.info("backup setting %s not restored: %s", key, e)
        for rule in data.get("host_rules") or []:
            self.store.set_host_rule(rule["host"], max_connections=rule.get("max_connections", 0),
                                     max_downloads=rule.get("max_downloads", 0),
                                     include_subdomains=rule.get("include_subdomains", True))
            restored["host_rules"] += 1
        for login in data.get("logins") or []:
            self.store.set_login(login["host"], login["username"], login.get("include_subdomains", True))
            if login.get("password"):
                self._set_secret("login", login["host"], login["password"])
            restored["logins"] += 1
        if data.get("proxy_password"):
            self._set_secret("proxy", "proxy", data["proxy_password"])
        known = {_link_key(d.url) for d in self.store.list()}
        new = [entry for entry in data.get("downloads") or [] if _link_key(entry["url"]) not in known]
        if new:
            restored["downloads"] = len(self.import_downloads(json.dumps(
                {"format": transfer.FORMAT, "version": transfer.VERSION, "downloads": new})))
        return restored

    def _scheduled_backup(self) -> None:
        settings = self.store.get_settings()
        interval = BACKUP_INTERVALS.get(settings["backup_schedule"])
        now = self._clock().timestamp()
        if not interval or now - settings["backup_last"] < interval:
            return
        self.store.set_setting("backup_last", now)
        folder = self._backup_folder(settings)

        async def run() -> None:
            try:
                path = await self.create_backup(str(folder), self._secrets.get(("backup", "backup"), ""))
                await asyncio.to_thread(backup.prune, folder, settings["backup_keep"])
                log.info("automatic backup: %s", path)
            except ManagerError as e:
                log.warning("automatic backup failed: %s", e)

        self._spawn(run())

    def _spawn(self, coroutine) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _auto_retry(self, download_id: int) -> None:
        self._retry_timers.pop(download_id, None)
        try:
            d = self.store.get(download_id)
        except KeyError:
            return
        if d.state == "error":
            self.store.set_state(download_id, "queued")
            self._emit_state(download_id)
            self._schedule()

    def _cancel_retry(self, download_id: int) -> None:
        timer = self._retry_timers.pop(download_id, None)
        if timer:
            timer.cancel()

    def _on_progress(self, download_id: int, done: int, size: int | None, speed: float) -> None:
        self._progress[download_id] = (done, size, speed)
        before = self._last_done.get(download_id)
        self._last_done[download_id] = done
        if before is not None and done > before:
            limit = self.store.get_settings()["data_limit"]
            crossed = limit and self.session_bytes < limit <= self.session_bytes + done - before
            self.session_bytes += done - before
            if crossed:
                self.conditions_changed()  # hold everything until the session is reset

    def reset_data_usage(self) -> None:
        """Start a new data-usage session (1DM: reset the session)."""
        self.session_bytes = 0
        self.conditions_changed()

    async def _progress_loop(self) -> None:
        while True:
            await asyncio.sleep(self._progress_interval)
            if self._torrent is not None and self._torrent.seeding:
                for download_id in self._torrent.finished_seeding(self.store.get_settings()):
                    self._stop_seeding(download_id)
            if self._progress and self.on_progress_batch:
                batch = [(i, done, size if size is not None else -1, speed)
                         for i, (done, size, speed) in self._progress.items()]
                try:
                    self.on_progress_batch(batch)
                except Exception:
                    log.exception("progress callback failed")

    def _emit_state(self, download_id: int) -> None:
        d = self.store.get(download_id)
        self._emit(self.on_state, download_id, d.state, d.error or "")

    @staticmethod
    def _emit(callback: Callable | None, *args) -> None:
        if callback:
            callback(*args)
