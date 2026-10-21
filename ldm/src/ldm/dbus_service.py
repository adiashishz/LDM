"""D-Bus interface io.github.ldm.Manager1 exposing the download manager.

Annotations are D-Bus signature strings read by dbus-fast, so this module must not use
`from __future__ import annotations`.
"""

import contextlib
from typing import Any

from dbus_fast import DBusError, Variant
from dbus_fast.service import ServiceInterface, method, signal

from .busnames import BUS_NAME, ERROR_PREFIX, INTERFACE, OBJECT_PATH  # noqa: F401 (re-exported)
from .engine import DownloadError
from .manager import Busy, InvalidArgument, Manager, NotFound, SizeMismatch

ADD_OPTIONS = {"dir", "name", "connections", "headers", "speed_limit", "start",
               "kind", "format", "format_label", "subtitles", "title", "page_url",
               "priorities", "sequential", "metadata", "peers", "scheduled", "unmetered_only",
               "schedule_start", "schedule_stop", "schedule_days"}


def to_variant(value: Any) -> Variant:
    if isinstance(value, (bytes, bytearray)):
        return Variant("ay", bytes(value))
    if isinstance(value, bool):
        return Variant("b", value)
    if isinstance(value, int):
        return Variant("x", value)
    if isinstance(value, float):
        return Variant("d", value)
    if isinstance(value, str):
        return Variant("s", value)
    if isinstance(value, dict):
        return Variant("a{ss}", {str(k): str(v) for k, v in value.items()})
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        return Variant("aa{sv}", [to_vardict(v) for v in value])
    if isinstance(value, list) and value and all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        return Variant("ax", value)
    if isinstance(value, list):
        return Variant("as", [str(v) for v in value])
    raise TypeError(f"cannot send {type(value).__name__} over D-Bus")


def to_vardict(values: dict[str, Any]) -> dict[str, Variant]:
    """None values are omitted: clients treat a missing key as unknown."""
    return {k: to_variant(v) for k, v in values.items() if v is not None}


def from_vardict(values: dict[str, Variant]) -> dict[str, Any]:
    return {k: v.value for k, v in values.items()}


@contextlib.contextmanager
def _errors():
    try:
        yield
    except NotFound as e:
        raise DBusError(ERROR_PREFIX + "NotFound", str(e)) from e
    except (InvalidArgument, ValueError, TypeError) as e:
        raise DBusError(ERROR_PREFIX + "InvalidArgument", str(e)) from e
    except SizeMismatch as e:
        raise DBusError(ERROR_PREFIX + "SizeMismatch", str(e)) from e
    except Busy as e:
        raise DBusError(ERROR_PREFIX + "Busy", str(e)) from e
    except DownloadError as e:
        raise DBusError(ERROR_PREFIX + "Failed", str(e)) from e


class ManagerInterface(ServiceInterface):
    def __init__(self, manager: Manager):
        super().__init__(INTERFACE)
        self.manager = manager
        manager.on_added = self.Added
        manager.on_removed = self.Removed
        manager.on_state = self.StateChanged
        manager.on_progress_batch = self.Progress

    @method()
    def Add(self, url: "s", options: "a{sv}") -> "t":
        with _errors():
            opts = from_vardict(options)
            unknown = set(opts) - ADD_OPTIONS
            if unknown:
                raise InvalidArgument(f"unknown options: {', '.join(sorted(unknown))}")
            return self.manager.add(url, **opts)

    @method()
    def Pause(self, download_id: "t"):
        with _errors():
            self.manager.pause(download_id)

    @method()
    def Resume(self, download_id: "t"):
        with _errors():
            self.manager.resume(download_id)

    @method()
    async def Remove(self, download_id: "t", delete_files: "b"):
        with _errors():
            await self.manager.remove(download_id, delete_files)

    @method()
    def Move(self, download_id: "t", position: "u"):
        with _errors():
            self.manager.move(download_id, position)

    @method()
    async def SetUrl(self, download_id: "t", url: "s"):
        with _errors():
            await self.manager.set_url(download_id, url)

    @method()
    async def Probe(self, url: "s", headers: "a{ss}") -> "a{sv}":
        with _errors():
            return to_vardict(await self.manager.probe(url, headers))

    @method()
    async def ProbeMedia(self, url: "s", headers: "a{ss}") -> "a{sv}":
        with _errors():
            return to_vardict(await self.manager.probe_media(url, headers))

    @method()
    async def ProbeTorrent(self, source: "s", headers: "a{ss}") -> "a{sv}":
        with _errors():
            return to_vardict(await self.manager.probe_torrent(source, headers))

    @method()
    def TorrentDetails(self, download_id: "t") -> "a{sv}":
        with _errors():
            return to_vardict(self.manager.torrent_details(download_id))

    @method()
    def SetSchedule(self, download_id: "t", scheduled: "b", window: "a{ss}"):
        """`window`: schedule_start / schedule_stop / schedule_days for its own times; {} = the defaults."""
        with _errors():
            self.manager.set_schedule(download_id, scheduled, window)

    @method()
    def SetSpeedLimit(self, download_id: "t", bytes_per_second: "t"):
        with _errors():
            self.manager.set_speed_limit(download_id, bytes_per_second)

    @method()
    def List(self) -> "aa{sv}":
        with _errors():
            return [to_vardict(d) for d in self.manager.list()]

    @method()
    def Get(self, download_id: "t") -> "a{sv}":
        with _errors():
            return to_vardict(self.manager.describe(download_id))

    @method()
    def GetSettings(self) -> "a{sv}":
        return to_vardict(self.manager.settings())

    @method()
    def SetSettings(self, values: "a{sv}"):
        with _errors():
            self.manager.update_settings(from_vardict(values))

    @method()
    def SetHostRule(self, host: "s", max_connections: "u", max_downloads: "u", include_subdomains: "b"):
        with _errors():
            self.manager.set_host_rule(host, max_connections, max_downloads, include_subdomains)

    @method()
    def RemoveHostRule(self, host: "s"):
        with _errors():
            self.manager.remove_host_rule(host)

    @method()
    def ListHostRules(self) -> "aa{sv}":
        return [to_vardict(vars(rule)) for rule in self.manager.host_rules()]

    @method()
    def SetLogin(self, host: "s", username: "s", password: "s", include_subdomains: "b"):
        """Site login (HTTP Basic/Digest); the password goes to the keyring."""
        with _errors():
            self.manager.set_login(host, username, password, include_subdomains)

    @method()
    def RemoveLogin(self, host: "s"):
        with _errors():
            self.manager.remove_login(host)

    @method()
    def ListLogins(self) -> "aa{sv}":
        return [to_vardict(login) for login in self.manager.list_logins()]

    @method()
    async def Checksum(self, download_id: "t", algorithm: "s") -> "s":
        with _errors():
            return await self.manager.checksum(download_id, algorithm)

    @method()
    async def Rename(self, download_id: "t", name: "s") -> "s":
        """Returns the new path (a taken name gets a number)."""
        with _errors():
            return await self.manager.rename(download_id, name)

    @method()
    async def MoveTo(self, download_id: "t", folder: "s") -> "s":
        with _errors():
            return await self.manager.move_to(download_id, folder)

    @method()
    async def CreateBackup(self, target: "s", password: "s") -> "s":
        """Back up to a file or folder ("" = the backup folder); a password encrypts it. Returns the file."""
        with _errors():
            return await self.manager.create_backup(target, password)

    @method()
    async def RestoreBackup(self, source: "s", password: "s") -> "a{sv}":
        """Restore a backup file; returns how many settings, rules, logins and downloads were restored."""
        with _errors():
            return to_vardict(await self.manager.restore_backup(source, password))

    @method()
    def PauseAll(self) -> "u":
        return self.manager.pause_all()

    @method()
    def ResumeAll(self) -> "u":
        return self.manager.resume_all()

    @method()
    def ResetDataUsage(self):
        """Start a new data-usage session; downloads held by the data limit continue."""
        self.manager.reset_data_usage()

    @method()
    def GetParts(self, download_id: "t") -> "aa{sv}":
        """Byte ranges (first, last), bytes done and whether a connection works on each part."""
        with _errors():
            return [to_vardict(p) for p in self.manager.parts(download_id)]

    @method()
    def FindDuplicates(self, url: "s") -> "aa{sv}":
        return [to_vardict(d) for d in self.manager.find_duplicates(url)]

    @method()
    def ExportDownloads(self, unfinished_only: "b") -> "s":
        """JSON text of the downloads (the caller saves it to a file)."""
        return self.manager.export_downloads(unfinished_only)

    @method()
    def ImportDownloads(self, text: "s") -> "at":
        with _errors():
            return self.manager.import_downloads(text)

    @method()
    def GetLog(self, download_id: "t") -> "as":
        """Diagnostics for one download (requests, responses, retries) since the service started."""
        with _errors():
            return self.manager.get_log(download_id)

    @signal()
    def Added(self, download_id) -> "t":
        return download_id

    @signal()
    def Removed(self, download_id) -> "t":
        return download_id

    @signal()
    def StateChanged(self, download_id, state, error) -> "tss":
        return [download_id, state, error]

    @signal()
    def Progress(self, batch) -> "a(ttxd)":
        """Batched every 0.5 s: (id, done bytes, size or -1, bytes/s) for running downloads."""
        return [[i, done, size, float(speed)] for i, done, size, speed in batch]
