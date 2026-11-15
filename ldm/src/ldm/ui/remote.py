"""The window's connection to the background service, for any toolkit.

D-Bus runs on a thread of its own, in its own asyncio event loop (dbus-fast, as the service uses). Replies
and signals go back to the window's thread through `post`, a function the toolkit supplies that runs a
callable there (Qt: a queued signal). Calls are asynchronous: each takes an optional
`callback(result, error)`, called on the window's thread; `error` is a `DaemonError` or None.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from dbus_fast import Message, MessageFlag, MessageType
from dbus_fast.aio import MessageBus

from ..busnames import BUS_NAME, ERROR_PREFIX, INTERFACE, OBJECT_PATH
from ..variants import to_vardict, unwrap

log = logging.getLogger(__name__)

Callback = Callable[[Any, "DaemonError | None"], None]
Post = Callable[[Callable[[], None]], None]

DEFAULT_TIMEOUT = 25.0  # seconds, as D-Bus itself defaults to
NO_TIMEOUT = None       # wait as long as it takes (hashing or moving big files)
BUS = ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus")
NO_OWNER = {"org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NameHasNoOwner"}


class DaemonError(Exception):
    def __init__(self, name: str, message: str):
        super().__init__(message)
        self.name = name  # short name, e.g. "NotFound", "SizeMismatch", "NotRunning"
        self.message = message


def not_running() -> DaemonError:
    return DaemonError("NotRunning", "the download service is not running")


class Remote:
    """The D-Bus connection: watches whether the service runs, forwards its signals, makes calls.

    `on_connected(bool)` and `on_signal(name, args)` are called on the window's thread.
    """

    def __init__(self, post: Post, on_connected: Callable[[bool], None],
                 on_signal: Callable[[str, list[Any]], None], address: str | None = None):
        self.connected = False  # as the window's thread last heard
        self._post, self._on_connected, self._on_signal = post, on_connected, on_signal
        self._closed = False
        self._owner: str | None = None  # the service's unique bus name; bus thread only
        self._bus: MessageBus | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="ldm-dbus", daemon=True)
        self._thread.start()
        self._ready = asyncio.run_coroutine_threadsafe(self._connect(address), self._loop)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop).result(5)
        except Exception:  # noqa: BLE001 (closing anyway)
            log.debug("closing the D-Bus connection failed", exc_info=True)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        self._loop.close()

    def call(self, method: str, signature: str, args: list[Any], callback: Callback | None,
             timeout: float | None = DEFAULT_TIMEOUT) -> None:
        if self._closed:
            return
        asyncio.run_coroutine_threadsafe(self._call(method, signature, args, callback, timeout), self._loop)

    # --- window's thread -------------------------------------------------------------------

    def _deliver(self, function: Callable[[], None]) -> None:
        """Run `function` on the window's thread, unless the connection was closed by then."""
        self._post(lambda: None if self._closed else function())

    def _set_connected(self, connected: bool) -> None:
        if connected != self.connected:
            self.connected = connected
            self._on_connected(connected)

    # --- bus thread ------------------------------------------------------------------------

    async def _connect(self, address: str | None) -> None:
        try:
            self._bus = await MessageBus(bus_address=address).connect()
        except Exception as e:  # noqa: BLE001 (no session bus: stay disconnected)
            log.warning("cannot connect to the session bus: %s", e)
            return
        self._bus.add_message_handler(self._on_message)
        # Watch first, then ask: the service may start or stop between the two.
        await self._add_match(f"type='signal',sender='{BUS[0]}',interface='{BUS[2]}',"
                              f"member='NameOwnerChanged',arg0='{BUS_NAME}'")
        await self._add_match(f"type='signal',sender='{BUS_NAME}',path='{OBJECT_PATH}',interface='{INTERFACE}'")
        reply = await self._bus.call(Message(destination=BUS[0], path=BUS[1], interface=BUS[2],
                                             member="GetNameOwner", signature="s", body=[BUS_NAME]))
        if reply and reply.message_type == MessageType.METHOD_RETURN:
            self._set_owner(reply.body[0])

    async def _disconnect(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    async def _add_match(self, rule: str) -> None:
        await self._bus.call(Message(destination=BUS[0], path=BUS[1], interface=BUS[2],
                                     member="AddMatch", signature="s", body=[rule]))

    def _set_owner(self, owner: str | None) -> None:
        self._owner = owner or None
        connected = self._owner is not None
        self._deliver(lambda: self._set_connected(connected))

    def _on_message(self, message: Message) -> None:
        if message.message_type != MessageType.SIGNAL:
            return
        if message.interface == BUS[2] and message.member == "NameOwnerChanged":
            if message.body[0] == BUS_NAME:
                self._set_owner(message.body[2])
        elif message.interface == INTERFACE and message.path == OBJECT_PATH and message.sender == self._owner:
            name, args = message.member, unwrap(message.body)
            self._deliver(lambda: self._on_signal(name, args))

    async def _call(self, method: str, signature: str, args: list[Any], callback: Callback | None,
                    timeout: float | None) -> None:
        result, error = None, None
        if self._bus is None or self._owner is None:
            error = not_running()
        else:
            try:
                message = Message(destination=BUS_NAME, path=OBJECT_PATH, interface=INTERFACE, member=method,
                                  signature=signature, body=args, flags=MessageFlag.NO_AUTOSTART)
                reply = await asyncio.wait_for(self._bus.call(message), timeout)
            except asyncio.TimeoutError:
                error = DaemonError("Timeout", "the download service did not answer")
            except Exception as e:  # noqa: BLE001 (a value D-Bus can't carry, or the connection broke)
                log.warning("calling %s failed", method, exc_info=True)
                error = DaemonError("Failed", str(e))
            else:
                if reply.message_type == MessageType.ERROR:
                    error = _error(reply)
                elif len(reply.body) == 1:
                    result = unwrap(reply.body[0])
        if callback is not None:
            self._deliver(lambda: callback(result, error))


def _error(reply: Message) -> DaemonError:
    name = reply.error_name or ""
    if name in NO_OWNER:
        return not_running()
    message = reply.body[0] if reply.body and isinstance(reply.body[0], str) else name
    return DaemonError(name.removeprefix(ERROR_PREFIX) if name.startswith(ERROR_PREFIX) else name or "Failed",
                       message)


def start_daemon() -> None:
    """Start `ldm daemon` in its own session so it outlives the window. Raises OSError on failure."""
    log_dir = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "ldm"
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "daemon.log", "ab") as log_file:
        process = subprocess.Popen([sys.executable, "-m", "ldm", "daemon"], stdin=subprocess.DEVNULL,
                                   stdout=log_file, stderr=log_file, start_new_session=True)
    threading.Thread(target=process.wait, name="ldm-daemon-reaper", daemon=True).start()  # reap on exit


class ServiceCalls:
    """The service's methods, one Python method each. Subclasses provide `_call` (and the transport)."""

    def _call(self, method: str, signature: str, args: list[Any], callback: Callback | None,
              timeout: float | None = DEFAULT_TIMEOUT) -> None:
        raise NotImplementedError

    def add(self, url: str, options: dict[str, Any] | None = None, callback: Callback | None = None) -> None:
        self._call("Add", "sa{sv}", [url, to_vardict(options or {})], callback)

    def pause(self, download_id: int, callback: Callback | None = None) -> None:
        self._call("Pause", "t", [download_id], callback)

    def resume(self, download_id: int, callback: Callback | None = None) -> None:
        self._call("Resume", "t", [download_id], callback)

    def remove(self, download_id: int, delete_files: bool, callback: Callback | None = None) -> None:
        self._call("Remove", "tb", [download_id, delete_files], callback)

    def move(self, download_id: int, position: int, callback: Callback | None = None) -> None:
        self._call("Move", "tu", [download_id, position], callback)

    def set_url(self, download_id: int, url: str, callback: Callback | None = None) -> None:
        self._call("SetUrl", "ts", [download_id, url], callback)

    def probe_torrent(self, source: str, headers: dict[str, str], callback: Callback) -> None:
        self._call("ProbeTorrent", "sa{ss}", [source, headers], callback)

    def torrent_details(self, download_id: int, callback: Callback) -> None:
        self._call("TorrentDetails", "t", [download_id], callback)

    def probe_media(self, url: str, headers: dict[str, str], callback: Callback) -> None:
        self._call("ProbeMedia", "sa{ss}", [url, headers], callback)

    def probe(self, url: str, headers: dict[str, str], callback: Callback) -> None:
        self._call("Probe", "sa{ss}", [url, headers], callback)

    def set_schedule(self, download_id: int, scheduled: bool, window: dict[str, str],
                     callback: Callback | None = None) -> None:
        self._call("SetSchedule", "tba{ss}", [download_id, scheduled, window], callback)

    def set_speed_limit(self, download_id: int, rate: int, callback: Callback | None = None) -> None:
        self._call("SetSpeedLimit", "tt", [download_id, rate], callback)

    def list(self, callback: Callback) -> None:
        self._call("List", "", [], callback)

    def get(self, download_id: int, callback: Callback) -> None:
        self._call("Get", "t", [download_id], callback)

    def get_settings(self, callback: Callback) -> None:
        self._call("GetSettings", "", [], callback)

    def set_settings(self, values: dict[str, Any], callback: Callback | None = None) -> None:
        self._call("SetSettings", "a{sv}", [to_vardict(values)], callback)

    def set_host_rule(self, host: str, max_connections: int, max_downloads: int, include_subdomains: bool,
                      callback: Callback | None = None) -> None:
        self._call("SetHostRule", "suub", [host, max_connections, max_downloads, include_subdomains], callback)

    def remove_host_rule(self, host: str, callback: Callback | None = None) -> None:
        self._call("RemoveHostRule", "s", [host], callback)

    def list_host_rules(self, callback: Callback) -> None:
        self._call("ListHostRules", "", [], callback)

    def set_login(self, host: str, username: str, password: str, include_subdomains: bool,
                  callback: Callback | None = None) -> None:
        self._call("SetLogin", "sssb", [host, username, password, include_subdomains], callback)

    def remove_login(self, host: str, callback: Callback | None = None) -> None:
        self._call("RemoveLogin", "s", [host], callback)

    def list_logins(self, callback: Callback) -> None:
        self._call("ListLogins", "", [], callback)

    def create_backup(self, target: str, password: str, callback: Callback) -> None:
        self._call("CreateBackup", "ss", [target, password], callback, NO_TIMEOUT)

    def restore_backup(self, source: str, password: str, callback: Callback) -> None:
        self._call("RestoreBackup", "ss", [source, password], callback, NO_TIMEOUT)

    def pause_all(self, callback: Callback | None = None) -> None:
        self._call("PauseAll", "", [], callback)

    def resume_all(self, callback: Callback | None = None) -> None:
        self._call("ResumeAll", "", [], callback)

    def reset_data_usage(self, callback: Callback | None = None) -> None:
        self._call("ResetDataUsage", "", [], callback)

    def get_parts(self, download_id: int, callback: Callback) -> None:
        self._call("GetParts", "t", [download_id], callback)

    def get_log(self, download_id: int, callback: Callback) -> None:
        self._call("GetLog", "t", [download_id], callback)

    def find_duplicates(self, url: str, callback: Callback) -> None:
        self._call("FindDuplicates", "s", [url], callback)

    def export_downloads(self, unfinished_only: bool, callback: Callback) -> None:
        self._call("ExportDownloads", "b", [unfinished_only], callback)

    def import_downloads(self, text: str, callback: Callback | None = None) -> None:
        self._call("ImportDownloads", "s", [text], callback)

    def update_ytdlp(self, callback: Callback) -> None:
        self._call("UpdateYtDlp", "", [], callback, NO_TIMEOUT)

    def checksum(self, download_id: int, algorithm: str, callback: Callback) -> None:
        self._call("Checksum", "ts", [download_id, algorithm], callback, NO_TIMEOUT)

    def rename(self, download_id: int, name: str, callback: Callback | None = None) -> None:
        self._call("Rename", "ts", [download_id, name], callback, NO_TIMEOUT)

    def move_to(self, download_id: int, folder: str, callback: Callback | None = None) -> None:
        self._call("MoveTo", "ts", [download_id, folder], callback, NO_TIMEOUT)
