"""Torrents through libtorrent (the engine 1DM uses via jlibtorrent).

One TorrentSession per daemon holds every active torrent. TorrentDownloader has the same interface
as engine.Downloader so the manager queues, pauses and resumes torrents like other downloads;
finished torrents may keep seeding inside the session, outside the parallel-download limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import unquote, urlsplit

from . import __version__
from .engine import DownloadError, ProgressCallback, new_client
from .net import NetConfig
from .ratelimit import TokenBucket
from .store import DownloadRow, Store

try:
    import libtorrent as lt
except ImportError:  # optional: `sudo dnf install rb_libtorrent-python3`
    lt = None

log = logging.getLogger(__name__)
INSTALL_HINT = "torrent support needs libtorrent: sudo dnf install rb_libtorrent-python3"
METADATA_TIMEOUT = 60.0


def available() -> bool:
    return lt is not None


def _require() -> None:
    if lt is None:
        raise DownloadError(INSTALL_HINT)


def is_torrent_source(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "magnet" or parts.path.lower().endswith(".torrent")


# --- configuration -------------------------------------------------------------------------

def proxy_settings(proxy: str | None) -> dict[str, Any]:
    """libtorrent settings for a manual proxy URL (http:// or socks5://, optionally with a login)."""
    if not proxy:
        return {"proxy_type": int(lt.proxy_type_t.none)}
    parts = urlsplit(proxy)
    socks = parts.scheme.startswith("socks")
    kind = (("socks5_pw" if parts.username else "socks5") if socks else ("http_pw" if parts.username else "http"))
    return {
        "proxy_type": int(getattr(lt.proxy_type_t, kind)),
        "proxy_hostname": parts.hostname, "proxy_port": parts.port,
        "proxy_username": unquote(parts.username or ""), "proxy_password": unquote(parts.password or ""),
        "proxy_peer_connections": True, "proxy_tracker_connections": True, "proxy_hostnames": True,
    }


def session_settings(settings: dict[str, Any], listen_host: str = "0.0.0.0", proxy: str | None = None) -> dict[str, Any]:
    encryption = {"prefer": lt.enc_policy.pe_enabled, "require": lt.enc_policy.pe_forced,
                  "off": lt.enc_policy.pe_disabled}[settings["torrent_encryption"]]
    port = settings["torrent_listen_port"]
    interfaces = f"{listen_host}:{port}" + (f",[::]:{port}" if listen_host == "0.0.0.0" else "")
    return {
        "user_agent": f"ldm/{__version__} libtorrent/{lt.__version__}",
        "listen_interfaces": interfaces,
        "enable_dht": settings["torrent_dht"],
        "enable_lsd": settings["torrent_lsd"],
        "enable_upnp": settings["torrent_upnp"],
        "enable_natpmp": settings["torrent_upnp"],
        "enable_outgoing_utp": settings["torrent_utp"],
        "enable_incoming_utp": settings["torrent_utp"],
        "out_enc_policy": int(encryption),
        "in_enc_policy": int(encryption),
        "connections_limit": settings["torrent_max_connections"],
        "upload_rate_limit": settings["torrent_upload_limit"],
        "alert_mask": int(lt.alert_category.error | lt.alert_category.status | lt.alert_category.storage),
        **proxy_settings(proxy),
    }


def load_ip_filter(path: str) -> "lt.ip_filter | None":
    """P2P (`name:1.2.3.0-1.2.3.255`) or DAT (`1.2.3.0 - 1.2.3.255 , 0 , name`) block lists."""
    if not path:
        return None
    flt = lt.ip_filter()
    rules = 0
    for line in Path(path).read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        span = line.rsplit(":", 1)[-1] if "," not in line else line.split(",")[0]
        if line.count(",") >= 1 and int((line.split(",")[1].strip() or "0")) > 127:
            continue  # DAT: access level > 127 means allowed
        try:
            start, end = (str(ipaddress.ip_address(p.strip())) for p in span.split("-"))
        except ValueError:
            continue
        flt.add_rule(start, end, 1)
        rules += 1
    log.info("ip filter: %d rules from %s", rules, path)
    return flt


def metadata_bytes(info: "lt.torrent_info") -> bytes:
    return lt.bencode(lt.create_torrent(info).generate())


def is_pad_file(files: "lt.file_storage", index: int) -> bool:
    """Alignment padding inserted by v2/hybrid torrents: never shown or selectable."""
    return bool(files.file_flags(index) & lt.file_storage.flag_pad_file)


def full_priorities(priorities: list[int], num_files: int) -> list[int]:
    """Priority lists may be shorter than the file list: missing files are skipped, not downloaded."""
    return (list(priorities) + [0] * num_files)[:num_files]


def describe_info(info: "lt.torrent_info") -> dict[str, Any]:
    files = info.files()
    real = [i for i in range(files.num_files()) if not is_pad_file(files, i)]
    return {
        "name": info.name(),
        "size": sum(files.file_size(i) for i in real),
        "num_files": files.num_files(),  # including padding: priority lists use this length
        "files": [{"index": i, "path": files.file_path(i), "size": files.file_size(i)} for i in real],
        "info_hash": str(info.info_hashes().get_best()),
        "trackers": [t.url for t in info.trackers()],
    }


async def read_source(url: str, headers: dict[str, str] | None = None, net: NetConfig | None = None) -> bytes:
    """Bytes of a .torrent from an http(s) URL, file:// URL or local path."""
    parts = urlsplit(url)
    if parts.scheme in ("http", "https"):
        net = await (net or NetConfig()).resolve(url)
        async with new_client(headers, net=net) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content
    path = unquote(parts.path) if parts.scheme == "file" else url
    return Path(path).read_bytes()


# --- session -------------------------------------------------------------------------------

class TorrentSession:
    """The daemon's libtorrent session: settings, alert pump, active handles, seeding."""

    def __init__(self, settings: dict[str, Any], listen_host: str = "0.0.0.0", proxy: str | None = None):
        _require()
        self.listen_host = listen_host
        self.ses = lt.session(session_settings(settings, listen_host, proxy))
        self.handles: dict[int, Any] = {}     # download id -> handle
        self.seeding: dict[int, float] = {}   # download id -> monotonic time seeding started
        self._waiters: list[tuple[Callable[[Any], bool], asyncio.Future]] = []
        self._pump_task: asyncio.Task | None = None
        self.apply_ip_filter(settings)

    def start(self) -> None:
        if self._pump_task is None:
            self._pump_task = asyncio.create_task(self._pump())

    async def close(self) -> None:
        if self._pump_task:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump_task
        self.ses.pause()

    def apply(self, settings: dict[str, Any], proxy: str | None = None) -> None:
        self.ses.apply_settings(session_settings(settings, self.listen_host, proxy))
        self.apply_ip_filter(settings)

    def apply_ip_filter(self, settings: dict[str, Any]) -> None:
        try:
            flt = load_ip_filter(settings["torrent_ip_filter"])
        except OSError as e:
            log.warning("ip filter not loaded: %s", e)
            return
        if flt is not None:
            self.ses.set_ip_filter(flt)

    async def _pump(self) -> None:
        while True:
            for alert in self.ses.pop_alerts():
                for waiter in list(self._waiters):
                    predicate, future = waiter
                    if not future.done() and predicate(alert):
                        future.set_result(alert)
                        self._waiters.remove(waiter)
            await asyncio.sleep(0.2)

    async def wait_for(self, predicate: Callable[[Any], bool], timeout: float) -> Any:
        future = asyncio.get_running_loop().create_future()
        entry = (predicate, future)
        self._waiters.append(entry)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            if entry in self._waiters:
                self._waiters.remove(entry)

    async def save_resume(self, handle) -> bytes | None:
        if not handle.is_valid() or not handle.status().has_metadata:
            return None
        handle.save_resume_data(lt.save_resume_flags_t.save_info_dict)
        try:
            alert = await self.wait_for(
                lambda a: isinstance(a, (lt.save_resume_data_alert, lt.save_resume_data_failed_alert))
                and a.handle == handle, timeout=15)
        except asyncio.TimeoutError:
            return None
        if isinstance(alert, lt.save_resume_data_failed_alert):
            return None
        return lt.write_resume_data_buf(alert.params)

    def remove(self, download_id: int) -> None:
        handle = self.handles.pop(download_id, None)
        self.seeding.pop(download_id, None)
        if handle is not None and handle.is_valid():
            self.ses.remove_torrent(handle)

    async def fetch_metadata(self, magnet: str, timeout: float = METADATA_TIMEOUT) -> "lt.torrent_info":
        """File list of a magnet from peers, without downloading any file data."""
        params = lt.parse_magnet_uri(magnet)
        with tempfile.TemporaryDirectory(prefix="ldm-magnet-") as scratch:
            params.save_path = scratch
            params.flags |= lt.torrent_flags.upload_mode
            params.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)  # added paused otherwise
            handle = self.ses.add_torrent(params)
            try:
                deadline = time.monotonic() + timeout
                while not handle.status().has_metadata:
                    if time.monotonic() > deadline:
                        raise DownloadError("no peer sent the file list in time")
                    await asyncio.sleep(0.25)
                return handle.torrent_file()
            finally:
                self.ses.remove_torrent(handle)

    def seed(self, download_id: int, resume_data: bytes, save_path: str) -> None:
        """Seed a finished torrent again (e.g. after a daemon restart)."""
        params = lt.read_resume_data(resume_data)
        params.save_path = save_path
        params.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
        self.handles[download_id] = self.ses.add_torrent(params)
        self.seeding[download_id] = time.monotonic()

    def finished_seeding(self, settings: dict[str, Any]) -> list[int]:
        """Seeding torrents that reached the ratio or time limit."""
        done = []
        for download_id, started in self.seeding.items():
            handle = self.handles.get(download_id)
            if handle is None or not handle.is_valid():
                done.append(download_id)
                continue
            st = handle.status()
            ratio = st.all_time_upload / max(st.total_wanted, 1)
            minutes = (time.monotonic() - started) / 60
            if ((settings["torrent_seed_ratio"] and ratio >= settings["torrent_seed_ratio"])
                    or (settings["torrent_seed_minutes"] and minutes >= settings["torrent_seed_minutes"])):
                done.append(download_id)
        return done

    def details(self, download_id: int) -> dict[str, Any] | None:
        handle = self.handles.get(download_id)
        if handle is None or not handle.is_valid():
            return None
        st = handle.status()
        info = {"peers": st.num_peers, "seeds": st.num_seeds, "download_speed": st.download_rate,
                "upload_speed": st.upload_rate, "uploaded": st.all_time_upload,
                "downloaded": st.all_time_download, "progress": st.progress,
                "ratio": st.all_time_upload / max(st.total_wanted, 1),
                "seeding": download_id in self.seeding}
        info["peer_list"] = [{"ip": f"{p.ip[0]}:{p.ip[1]}", "client": p.client.decode(errors="replace")
                              if isinstance(p.client, bytes) else str(p.client),
                              "down": p.down_speed, "up": p.up_speed, "progress": p.progress}
                             for p in handle.get_peer_info()]
        info["trackers"] = [{"url": t["url"], "message": ""} for t in handle.trackers()]
        if st.has_metadata:
            files = handle.torrent_file().files()
            progress = handle.file_progress()
            priorities = handle.get_file_priorities()
            info["files"] = [{"index": i, "path": files.file_path(i), "size": files.file_size(i),
                              "done": progress[i], "wanted": priorities[i] > 0}
                             for i in range(files.num_files()) if not is_pad_file(files, i)]
        return info


# --- downloading ---------------------------------------------------------------------------

class TorrentDownloader:
    """Downloads one torrent; cancelling the task pauses it (resume data is kept)."""

    def __init__(self, store: Store, download_id: int, *, session: Callable[[], TorrentSession],
                 on_progress: ProgressCallback | None = None, on_state: Callable[[str], None] | None = None,
                 limiters: Sequence[TokenBucket] = (), connections: int | None = None,
                 progress_interval: float = 0.5, **_engine_options):
        self.store = store
        self.id = download_id
        self.get_session = session
        self.on_progress = on_progress
        self.on_state = on_state
        self.limiters = limiters
        self.progress_interval = progress_interval

    def _set_state(self, state: str, detail: str | None = None) -> None:
        self.store.set_state(self.id, state, detail)
        if self.on_state:
            self.on_state(state)

    async def run(self) -> Path:
        _require()
        session = self.get_session()
        d = self.store.get(self.id)
        self.store.set_state(self.id, "running")
        handle = None
        try:
            params = await self._params(d, session)
            handle = session.ses.add_torrent(params)
            session.handles[self.id] = handle
            self._apply_limits(handle, d)
            for peer in d.options.get("peers", []):
                host, _, port = peer.rpartition(":")
                handle.connect_peer((host, int(port)))
            path = await self._download(handle, d, session)
        except asyncio.CancelledError:
            if handle is not None:
                await self._save(session, handle)
                session.remove(self.id)
            self.store.set_state(self.id, "paused")
            raise
        except DownloadError as e:
            if handle is not None:
                session.remove(self.id)
            self.store.set_state(self.id, "error", str(e))
            raise
        return path

    async def _params(self, d: DownloadRow, session: TorrentSession):
        row = self.store.get_torrent(self.id)
        settings = self.store.get_settings()
        if row and row.resume_data:
            params = lt.read_resume_data(row.resume_data)
        elif row and row.metadata:
            params = lt.add_torrent_params()
            params.ti = lt.torrent_info(lt.bdecode(row.metadata))
        elif urlsplit(d.url).scheme == "magnet":
            params = lt.parse_magnet_uri(d.url)
        else:
            data = await read_source(d.url, d.headers)
            try:
                info = lt.torrent_info(lt.bdecode(data))
            except RuntimeError as e:
                raise DownloadError(f"not a valid .torrent file: {e}") from e
            self.store.save_torrent(self.id, metadata=data, info_hash=str(info.info_hashes().get_best()))
            params = lt.add_torrent_params()
            params.ti = info
        params.save_path = str(d.dir)
        params.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
        sequential = row.sequential if row else settings["torrent_sequential"]
        if sequential:
            params.flags |= lt.torrent_flags.sequential_download
        if not settings["torrent_pex"]:
            params.flags |= lt.torrent_flags.disable_pex
        params.max_connections = settings["torrent_max_peers"]
        extra = [t.strip() for t in settings["torrent_trackers"].splitlines() if t.strip()]
        if extra:
            params.trackers = list(params.trackers) + extra
        if row and row.priorities and params.ti is not None:
            params.file_priorities = full_priorities(row.priorities, params.ti.num_files())
        d.dir.mkdir(parents=True, exist_ok=True)
        return params

    def _apply_limits(self, handle, d: DownloadRow) -> None:
        rates = [b.rate for b in self.limiters if b.rate] + ([d.speed_limit] if d.speed_limit else [])
        handle.set_download_limit(int(min(rates)) if rates else -1)

    async def _download(self, handle, d: DownloadRow, session: TorrentSession) -> Path:
        if not handle.status().has_metadata:
            self._set_state("processing", "Getting torrent info")
            while not handle.status().has_metadata:
                await self._check_error(handle)
                await asyncio.sleep(0.25)
            info = handle.torrent_file()
            self.store.save_torrent(self.id, metadata=metadata_bytes(info),
                                    info_hash=str(info.info_hashes().get_best()))
            row = self.store.get_torrent(self.id)
            if row and row.priorities:
                handle.prioritize_files(full_priorities(row.priorities, info.num_files()))
        name = handle.status().name
        if d.name != name:
            self.store.update(self.id, name=name)
        self._set_state("running")
        while True:
            st = handle.status()
            await self._check_error(handle)
            if self.on_progress:
                self.on_progress(st.total_wanted_done, st.total_wanted or None, float(st.download_rate))
            if st.is_finished or st.is_seeding:
                break
            await asyncio.sleep(self.progress_interval)
        await self._save(session, handle)
        path = Path(d.dir) / name
        settings = self.store.get_settings()
        self.store.finish(self.id, name, st.total_wanted)
        if settings["torrent_seed"]:
            session.seeding[self.id] = time.monotonic()
            self._set_state("seeding")
        else:
            session.remove(self.id)
        return path

    async def _check_error(self, handle) -> None:
        st = handle.status()
        if st.errc.value():
            raise DownloadError(st.errc.message())

    async def _save(self, session: TorrentSession, handle) -> None:
        data = await session.save_resume(handle)
        if data:
            st = handle.status()
            self.store.save_torrent(self.id, resume_data=data, uploaded=st.all_time_upload)
