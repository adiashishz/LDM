"""D-Bus interface io.github.adiashishz.LDM.Manager1: the service's methods (ldm.api) on the session bus.

Annotations are D-Bus signature strings read by dbus-fast, so this module must not use
`from __future__ import annotations`.
"""

import contextlib

from dbus_fast import DBusError
from dbus_fast.service import ServiceInterface, method, signal

from .api import ADD_OPTIONS, Api, error_name  # noqa: F401 (ADD_OPTIONS re-exported)
from .busnames import BUS_NAME, ERROR_PREFIX, INTERFACE, OBJECT_PATH  # noqa: F401 (re-exported)
from .manager import Manager
from .variants import from_vardict, to_variant, to_vardict  # noqa: F401 (to_variant re-exported)


@contextlib.contextmanager
def _errors():
    try:
        yield
    except DBusError:
        raise
    except Exception as e:
        name = error_name(e)
        if name is None:
            raise
        raise DBusError(ERROR_PREFIX + name, str(e)) from e


class ManagerInterface(ServiceInterface):
    def __init__(self, api: Api | Manager):
        super().__init__(INTERFACE)
        self.api = api if isinstance(api, Api) else Api(api)
        self.manager = self.api.manager
        self.api.listeners.append(self._signal)

    def _signal(self, name: str, args: list) -> None:
        getattr(self, name)(*args)

    @method()
    def Add(self, url: "s", options: "a{sv}") -> "t":
        with _errors():
            return self.api.Add(url, from_vardict(options))

    @method()
    def Pause(self, download_id: "t"):
        with _errors():
            self.api.Pause(download_id)

    @method()
    def Resume(self, download_id: "t"):
        with _errors():
            self.api.Resume(download_id)

    @method()
    async def Remove(self, download_id: "t", delete_files: "b"):
        with _errors():
            await self.api.Remove(download_id, delete_files)

    @method()
    def Move(self, download_id: "t", position: "u"):
        with _errors():
            self.api.Move(download_id, position)

    @method()
    async def SetUrl(self, download_id: "t", url: "s"):
        with _errors():
            await self.api.SetUrl(download_id, url)

    @method()
    async def Probe(self, url: "s", headers: "a{ss}") -> "a{sv}":
        with _errors():
            return to_vardict(await self.api.Probe(url, headers))

    @method()
    async def ProbeMedia(self, url: "s", headers: "a{ss}") -> "a{sv}":
        with _errors():
            return to_vardict(await self.api.ProbeMedia(url, headers))

    @method()
    async def ProbeTorrent(self, source: "s", headers: "a{ss}") -> "a{sv}":
        with _errors():
            return to_vardict(await self.api.ProbeTorrent(source, headers))

    @method()
    def TorrentDetails(self, download_id: "t") -> "a{sv}":
        with _errors():
            return to_vardict(self.api.TorrentDetails(download_id))

    @method()
    def SetSchedule(self, download_id: "t", scheduled: "b", window: "a{ss}"):
        """`window`: schedule_start / schedule_stop / schedule_days for its own times; {} = the defaults."""
        with _errors():
            self.api.SetSchedule(download_id, scheduled, window)

    @method()
    def SetSpeedLimit(self, download_id: "t", bytes_per_second: "t"):
        with _errors():
            self.api.SetSpeedLimit(download_id, bytes_per_second)

    @method()
    def List(self) -> "aa{sv}":
        with _errors():
            return [to_vardict(d) for d in self.api.List()]

    @method()
    def Get(self, download_id: "t") -> "a{sv}":
        with _errors():
            return to_vardict(self.api.Get(download_id))

    @method()
    def GetSettings(self) -> "a{sv}":
        return to_vardict(self.api.GetSettings())

    @method()
    def SetSettings(self, values: "a{sv}"):
        with _errors():
            self.api.SetSettings(from_vardict(values))

    @method()
    def SetHostRule(self, host: "s", max_connections: "u", max_downloads: "u", include_subdomains: "b"):
        with _errors():
            self.api.SetHostRule(host, max_connections, max_downloads, include_subdomains)

    @method()
    def RemoveHostRule(self, host: "s"):
        with _errors():
            self.api.RemoveHostRule(host)

    @method()
    def ListHostRules(self) -> "aa{sv}":
        return [to_vardict(rule) for rule in self.api.ListHostRules()]

    @method()
    def SetLogin(self, host: "s", username: "s", password: "s", include_subdomains: "b"):
        """Site login (HTTP Basic/Digest); the password goes to the keyring."""
        with _errors():
            self.api.SetLogin(host, username, password, include_subdomains)

    @method()
    def RemoveLogin(self, host: "s"):
        with _errors():
            self.api.RemoveLogin(host)

    @method()
    def ListLogins(self) -> "aa{sv}":
        return [to_vardict(login) for login in self.api.ListLogins()]

    @method()
    async def UpdateYtDlp(self) -> "s":
        """Fetch a newer yt-dlp for this user; returns its version, "" when already up to date."""
        with _errors():
            return await self.api.UpdateYtDlp()

    @method()
    async def Checksum(self, download_id: "t", algorithm: "s") -> "s":
        with _errors():
            return await self.api.Checksum(download_id, algorithm)

    @method()
    async def Rename(self, download_id: "t", name: "s") -> "s":
        """Returns the new path (a taken name gets a number)."""
        with _errors():
            return await self.api.Rename(download_id, name)

    @method()
    async def MoveTo(self, download_id: "t", folder: "s") -> "s":
        with _errors():
            return await self.api.MoveTo(download_id, folder)

    @method()
    async def CreateBackup(self, target: "s", password: "s") -> "s":
        """Back up to a file or folder ("" = the backup folder); a password encrypts it. Returns the file."""
        with _errors():
            return await self.api.CreateBackup(target, password)

    @method()
    async def RestoreBackup(self, source: "s", password: "s") -> "a{sv}":
        """Restore a backup file; returns how many settings, rules, logins and downloads were restored."""
        with _errors():
            return to_vardict(await self.api.RestoreBackup(source, password))

    @method()
    def PauseAll(self) -> "u":
        return self.api.PauseAll()

    @method()
    def ResumeAll(self) -> "u":
        return self.api.ResumeAll()

    @method()
    def Quit(self) -> "u":
        """Stop the service and close LDM's windows; returns its process id."""
        with _errors():
            return self.api.Quit()

    @method()
    def ResetDataUsage(self):
        """Start a new data-usage session; downloads held by the data limit continue."""
        self.api.ResetDataUsage()

    @method()
    def GetParts(self, download_id: "t") -> "aa{sv}":
        """Byte ranges (first, last), bytes done and whether a connection works on each part."""
        with _errors():
            return [to_vardict(p) for p in self.api.GetParts(download_id)]

    @method()
    def FindDuplicates(self, url: "s") -> "aa{sv}":
        return [to_vardict(d) for d in self.api.FindDuplicates(url)]

    @method()
    def ExportDownloads(self, unfinished_only: "b") -> "s":
        """JSON text of the downloads (the caller saves it to a file)."""
        return self.api.ExportDownloads(unfinished_only)

    @method()
    def ImportDownloads(self, text: "s") -> "at":
        with _errors():
            return self.api.ImportDownloads(text)

    @method()
    def GetLog(self, download_id: "t") -> "as":
        """Diagnostics for one download (requests, responses, retries) since the service started."""
        with _errors():
            return self.api.GetLog(download_id)

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
        return batch
