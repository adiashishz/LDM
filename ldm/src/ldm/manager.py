"""Download manager: queue, parallel limit, per-host rules, speed limits, auto-retry.

Runs every download as an asyncio task in one event loop. Front ends (D-Bus service, tests)
drive it through its methods and observe it through the `on_*` callbacks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections import Counter
from functools import partial
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from . import media
from .engine import DownloadError, Downloader, new_client, probe
from .media import MediaDownloader
from .ratelimit import TokenBucket
from .store import DEFAULT_SETTINGS, DownloadRow, HostRule, Store

RESUMABLE_STATES = {"paused", "error", "expired", "changed"}
MAX_CONNECTIONS = 32
MEDIA_EXTENSIONS = (".m3u8", ".mpd")  # stream playlists always go through yt-dlp
MEDIA_OPTIONS = {"format", "format_label", "subtitles", "title", "page_url"}

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


class Manager:
    def __init__(self, store: Store, *, downloader_factory: Callable[..., Downloader] = Downloader,
                 media_factory: Callable[..., MediaDownloader] = MediaDownloader,
                 downloader_options: dict[str, Any] | None = None, progress_interval: float = 0.5):
        self.store = store
        self._factories = {"http": downloader_factory, "media": media_factory}
        self._options = downloader_options or {}
        self._progress_interval = progress_interval
        self._tasks: dict[int, asyncio.Task] = {}
        self._rule_keys: dict[int, str | None] = {}  # running id -> host rule it counts against
        self._buckets: dict[int, TokenBucket] = {}
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

    # --- lifecycle -------------------------------------------------------------------------

    async def start(self) -> None:
        self.store.requeue_interrupted()
        self._global.set_rate(self.store.get_settings()["speed_limit"])
        self._progress_task = asyncio.create_task(self._progress_loop())
        self._schedule()

    async def shutdown(self) -> None:
        """Stop all transfers; downloads that were running are queued again for the next start."""
        self._closing = True
        for timer in self._retry_timers.values():
            timer.cancel()
        self._retry_timers.clear()
        if self._progress_task:
            self._progress_task.cancel()
        running = list(self._tasks)
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for download_id in running:
            self.store.set_state(download_id, "queued")

    # --- commands --------------------------------------------------------------------------

    def add(self, url: str, *, dir: str | None = None, name: str | None = None,
            connections: int | None = None, headers: dict[str, str] | None = None,
            speed_limit: int = 0, start: bool = True, kind: str | None = None, **media_options) -> int:
        """`kind`: "http" (ldm's engine) or "media" (yt-dlp); default: media for .m3u8/.mpd links.
        Media options: format (yt-dlp selector), format_label, subtitles ("en,hi"), title, page_url."""
        if urlsplit(url).scheme not in ("http", "https"):
            raise InvalidArgument(f"unsupported URL: {url}")
        if set(media_options) - MEDIA_OPTIONS:
            raise InvalidArgument(f"unknown options: {', '.join(sorted(set(media_options) - MEDIA_OPTIONS))}")
        kind = kind or ("media" if urlsplit(url).path.lower().endswith(MEDIA_EXTENSIONS) else "http")
        if kind not in self._factories:
            raise InvalidArgument(f"unknown download kind: {kind}")
        settings = self.store.get_settings()
        connections = connections or settings["default_connections"]
        if not 1 <= connections <= MAX_CONNECTIONS:
            raise InvalidArgument(f"connections must be 1-{MAX_CONNECTIONS}")
        options = {k: v for k, v in media_options.items() if v not in (None, "")}
        if kind == "media":
            options["custom_name"] = bool(name)
            name = name or options.get("title")
        download_id = self.store.add(url, dir or settings["download_dir"], name=name, headers=headers,
                                     connections=connections, state="queued" if start else "paused",
                                     kind=kind, options=options)
        if speed_limit:
            self.store.update(download_id, speed_limit=speed_limit)
        self._emit(self.on_added, download_id)
        self._emit_state(download_id)
        self._schedule()
        return download_id

    def pause(self, download_id: int) -> None:
        d = self._get(download_id)
        self._cancel_retry(download_id)
        if self._active(download_id):
            self._tasks[download_id].cancel()  # the engine saves progress and marks it paused
        elif d.state in ("queued", "error"):
            self.store.set_state(download_id, "paused")
            self._emit_state(download_id)

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
        task = self._tasks.pop(download_id, None)
        if task:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            self._forget(download_id)
        self.store.delete(download_id)
        if delete_files:
            if d.kind == "media":
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
            async with new_client(d.headers) as client:
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
            async with new_client(headers) as client:
                p = await probe(client, url)
        except httpx.HTTPError as e:
            raise DownloadError(f"could not reach the server: {e}") from e
        return {"final_url": p.final_url, "size": p.size, "resumable": p.resumable,
                "filename": p.filename, "content_type": p.content_type}

    async def probe_media(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        """Title, qualities and subtitles yt-dlp can get from a page or stream playlist."""
        if urlsplit(url).scheme not in ("http", "https"):
            raise InvalidArgument(f"unsupported URL: {url}")
        return await asyncio.to_thread(media.probe_media, url, headers, self.store.get_settings())

    def set_speed_limit(self, download_id: int, rate: int) -> None:
        self._get(download_id)
        self.store.update(download_id, speed_limit=rate)
        if download_id in self._buckets:
            self._buckets[download_id].set_rate(rate)

    def settings(self) -> dict[str, Any]:
        settings = self.store.get_settings()
        runtime = media.find_js_runtime(settings["js_runtime"])  # read-only, shown in preferences
        settings["js_runtime_detected"] = f"{runtime[0]}: {runtime[1]}" if runtime else ""
        return settings

    def update_settings(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS:
                raise InvalidArgument(f"unknown setting: {key}")
            if key == "download_dir":
                if not isinstance(value, str) or not value:
                    raise InvalidArgument("download_dir must be a path")
            elif key == "media_quality":
                if not (value in ("best", "audio") or (isinstance(value, int) and value > 0)
                        or (isinstance(value, str) and value.isdigit())):
                    raise InvalidArgument("media_quality must be best, audio or a height such as 1080")
            elif key == "media_container":
                if value not in ("mp4", "mkv"):
                    raise InvalidArgument("media_container must be mp4 or mkv")
            elif key in ("subtitles", "js_runtime"):
                if not isinstance(value, str):
                    raise InvalidArgument(f"{key} must be text")
            elif not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise InvalidArgument(f"{key} must be a non-negative integer")
            elif key == "max_parallel" and value < 1:
                raise InvalidArgument("max_parallel must be at least 1")
            elif key == "default_connections" and not 1 <= value <= MAX_CONNECTIONS:
                raise InvalidArgument(f"default_connections must be 1-{MAX_CONNECTIONS}")
        for key, value in values.items():
            self.store.set_setting(key, value)
        if "speed_limit" in values:
            self._global.set_rate(values["speed_limit"])
        self._schedule()

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

    # --- queries ---------------------------------------------------------------------------

    def describe(self, download_id: int) -> dict[str, Any]:
        d = self._get(download_id)
        if download_id in self._progress:
            done, _, speed = self._progress[download_id]
        else:
            done = d.size if d.state == "done" else sum(p.done for p in self.store.load_parts(download_id))
            speed = 0.0
        return {
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

    def _schedule(self) -> None:
        if self._closing:
            return
        max_parallel = self.store.get_settings()["max_parallel"]
        per_rule = Counter(key for key in self._rule_keys.values() if key)
        for d in self.store.queued():
            if len(self._tasks) >= max_parallel:
                return
            key, rule = self._rule_key(d)
            if rule and rule.max_downloads and per_rule[key] >= rule.max_downloads:
                continue
            per_rule[key] += 1
            self._start(d, key, rule)

    def _start(self, d: DownloadRow, key: str | None, rule: HostRule | None) -> None:
        bucket = TokenBucket(d.speed_limit)
        extra = {"on_state": lambda state, i=d.id: self._emit_state(i)} if d.kind == "media" else {}
        downloader = self._factories[d.kind](
            self.store, d.id, limiters=[self._global, bucket],
            connections=(rule.max_connections or None) if rule else None,
            on_progress=partial(self._on_progress, d.id), progress_interval=self._progress_interval,
            **extra, **self._options,
        )
        self.store.set_state(d.id, "running")
        task = asyncio.create_task(downloader.run())
        self._tasks[d.id] = task
        self._rule_keys[d.id] = key
        self._buckets[d.id] = bucket
        task.add_done_callback(partial(self._finished, d.id))
        self._emit_state(d.id)

    def _forget(self, download_id: int) -> None:
        self._rule_keys.pop(download_id, None)
        self._buckets.pop(download_id, None)
        self._progress.pop(download_id, None)

    def _finished(self, download_id: int, task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()  # the engine already recorded it in the store
        if self._tasks.get(download_id) is not task:
            return  # removed while running
        del self._tasks[download_id]
        self._forget(download_id)
        if self._closing:
            return
        try:
            d = self.store.get(download_id)
        except KeyError:
            return
        if d.state in ("running", "processing"):  # cancelled before the engine started
            self.store.set_state(download_id, "paused")
        elif d.state == "error":
            settings = self.store.get_settings()
            attempt = self._attempts.get(download_id, 0) + 1
            if attempt <= settings["retry_count"]:
                self._attempts[download_id] = attempt
                self._retry_timers[download_id] = asyncio.get_running_loop().call_later(
                    settings["retry_interval"], self._auto_retry, download_id)
        elif d.state == "done":
            self._attempts.pop(download_id, None)
        self._emit_state(download_id)
        self._schedule()

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

    async def _progress_loop(self) -> None:
        while True:
            await asyncio.sleep(self._progress_interval)
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
