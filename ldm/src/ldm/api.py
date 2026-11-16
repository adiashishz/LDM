"""The service's methods and signals, whatever carries them (D-Bus, the local socket).

Methods have their D-Bus names and take and return plain Python values; errors are the manager's
exceptions, named for clients by `error_name`. Signals (Added, Removed, StateChanged, Progress) go to every
listener.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from .engine import DownloadError
from .manager import Busy, Duplicate, InvalidArgument, Manager, NotFound, SizeMismatch

ADD_OPTIONS = {"dir", "name", "connections", "headers", "speed_limit", "start",
               "kind", "format", "format_label", "subtitles", "title", "page_url",
               "priorities", "sequential", "metadata", "peers", "scheduled", "unmetered_only",
               "schedule_start", "schedule_stop", "schedule_days", "hls_key", "hls_iv"}

# The manager's exceptions as clients see them: the short name of the D-Bus error (after ERROR_PREFIX).
ERRORS: list[tuple[type[Exception] | tuple[type[Exception], ...], str]] = [
    (NotFound, "NotFound"),
    ((InvalidArgument, ValueError, TypeError), "InvalidArgument"),
    (SizeMismatch, "SizeMismatch"),
    (Busy, "Busy"),
    (Duplicate, "Duplicate"),
    (DownloadError, "Failed"),
]

Listener = Callable[[str, list], None]


def error_name(error: Exception) -> str | None:
    """The short name clients know an error by, or None for an unexpected one."""
    return next((name for kinds, name in ERRORS if isinstance(error, kinds)), None)


class Api:
    def __init__(self, manager: Manager):
        self.manager = manager
        self.listeners: list[Listener] = []
        manager.on_added = lambda download_id: self._emit("Added", [download_id])
        manager.on_removed = lambda download_id: self._emit("Removed", [download_id])
        manager.on_state = lambda download_id, state, error: self._emit("StateChanged", [download_id, state, error])
        manager.on_progress_batch = lambda batch: self._emit(
            "Progress", [[[i, done, size, float(speed)] for i, done, size, speed in batch]])

    def _emit(self, name: str, args: list) -> None:
        for listener in list(self.listeners):
            listener(name, args)

    async def call(self, method: str, params: list[Any]) -> Any:
        """Run a method by its name (for the socket); raises KeyError for an unknown one."""
        if method.startswith("_") or method not in METHODS:
            raise KeyError(method)
        result = getattr(self, method)(*params)
        return await result if inspect.isawaitable(result) else result

    # --- methods ---------------------------------------------------------------------------------

    def Add(self, url: str, options: dict) -> int:  # noqa: N802 (the D-Bus names)
        unknown = set(options) - ADD_OPTIONS
        if unknown:
            raise InvalidArgument(f"unknown options: {', '.join(sorted(unknown))}")
        return self.manager.add(url, **options)

    def Pause(self, download_id: int) -> None:  # noqa: N802
        self.manager.pause(download_id)

    def Resume(self, download_id: int) -> None:  # noqa: N802
        self.manager.resume(download_id)

    async def Remove(self, download_id: int, delete_files: bool) -> None:  # noqa: N802
        await self.manager.remove(download_id, delete_files)

    def Move(self, download_id: int, position: int) -> None:  # noqa: N802
        self.manager.move(download_id, position)

    async def SetUrl(self, download_id: int, url: str) -> None:  # noqa: N802
        await self.manager.set_url(download_id, url)

    async def Probe(self, url: str, headers: dict) -> dict:  # noqa: N802
        return await self.manager.probe(url, headers)

    async def ProbeMedia(self, url: str, headers: dict) -> dict:  # noqa: N802
        return await self.manager.probe_media(url, headers)

    async def ProbeTorrent(self, source: str, headers: dict) -> dict:  # noqa: N802
        return await self.manager.probe_torrent(source, headers)

    def TorrentDetails(self, download_id: int) -> dict:  # noqa: N802
        return self.manager.torrent_details(download_id)

    def SetSchedule(self, download_id: int, scheduled: bool, window: dict) -> None:  # noqa: N802
        """`window`: schedule_start / schedule_stop / schedule_days for its own times; {} = the defaults."""
        self.manager.set_schedule(download_id, scheduled, window)

    def SetSpeedLimit(self, download_id: int, bytes_per_second: int) -> None:  # noqa: N802
        self.manager.set_speed_limit(download_id, bytes_per_second)

    def List(self) -> list[dict]:  # noqa: N802
        return self.manager.list()

    def Get(self, download_id: int) -> dict:  # noqa: N802
        return self.manager.describe(download_id)

    def GetSettings(self) -> dict:  # noqa: N802
        return self.manager.settings()

    def SetSettings(self, values: dict) -> None:  # noqa: N802
        self.manager.update_settings(values)

    def SetHostRule(self, host: str, max_connections: int, max_downloads: int,  # noqa: N802
                    include_subdomains: bool) -> None:
        self.manager.set_host_rule(host, max_connections, max_downloads, include_subdomains)

    def RemoveHostRule(self, host: str) -> None:  # noqa: N802
        self.manager.remove_host_rule(host)

    def ListHostRules(self) -> list[dict]:  # noqa: N802
        return [vars(rule) for rule in self.manager.host_rules()]

    def SetLogin(self, host: str, username: str, password: str, include_subdomains: bool) -> None:  # noqa: N802
        """Site login (HTTP Basic/Digest); the password goes to the keyring."""
        self.manager.set_login(host, username, password, include_subdomains)

    def RemoveLogin(self, host: str) -> None:  # noqa: N802
        self.manager.remove_login(host)

    def ListLogins(self) -> list[dict]:  # noqa: N802
        return self.manager.list_logins()

    async def UpdateYtDlp(self) -> str:  # noqa: N802
        """Fetch a newer yt-dlp for this user; returns its version, "" when already up to date."""
        return await self.manager.update_ytdlp()

    async def Checksum(self, download_id: int, algorithm: str) -> str:  # noqa: N802
        return await self.manager.checksum(download_id, algorithm)

    async def Rename(self, download_id: int, name: str) -> str:  # noqa: N802
        """Returns the new path (a taken name gets a number)."""
        return await self.manager.rename(download_id, name)

    async def MoveTo(self, download_id: int, folder: str) -> str:  # noqa: N802
        return await self.manager.move_to(download_id, folder)

    async def CreateBackup(self, target: str, password: str) -> str:  # noqa: N802
        """Back up to a file or folder ("" = the backup folder); a password encrypts it. Returns the file."""
        return await self.manager.create_backup(target, password)

    async def RestoreBackup(self, source: str, password: str) -> dict:  # noqa: N802
        """Restore a backup file; returns how many settings, rules, logins and downloads were restored."""
        return await self.manager.restore_backup(source, password)

    def PauseAll(self) -> int:  # noqa: N802
        return self.manager.pause_all()

    def ResumeAll(self) -> int:  # noqa: N802
        return self.manager.resume_all()

    def ResetDataUsage(self) -> None:  # noqa: N802
        """Start a new data-usage session; downloads held by the data limit continue."""
        self.manager.reset_data_usage()

    def GetParts(self, download_id: int) -> list[dict]:  # noqa: N802
        """Byte ranges (first, last), bytes done and whether a connection works on each part."""
        return self.manager.parts(download_id)

    def FindDuplicates(self, url: str) -> list[dict]:  # noqa: N802
        return self.manager.find_duplicates(url)

    def ExportDownloads(self, unfinished_only: bool) -> str:  # noqa: N802
        """JSON text of the downloads (the caller saves it to a file)."""
        return self.manager.export_downloads(unfinished_only)

    def ImportDownloads(self, text: str) -> list[int]:  # noqa: N802
        return self.manager.import_downloads(text)

    def GetLog(self, download_id: int) -> list[str]:  # noqa: N802
        """Diagnostics for one download (requests, responses, retries) since the service started."""
        return self.manager.get_log(download_id)


METHODS = {name for name, _ in inspect.getmembers(Api, inspect.isfunction) if name[0].isupper()}
SIGNALS = ("Added", "Removed", "StateChanged", "Progress")
