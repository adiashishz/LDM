"""Client for the ldm daemon on the GLib main loop (Gio D-Bus).

Calls are asynchronous: each takes an optional `callback(result, error)`; `error` is a
`DaemonError` or None.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from gi.repository import Gio, GLib, GObject

from ..busnames import BUS_NAME, ERROR_PREFIX, INTERFACE, OBJECT_PATH

Callback = Callable[[Any, "DaemonError | None"], None]


class DaemonError(Exception):
    def __init__(self, name: str, message: str):
        super().__init__(message)
        self.name = name  # short name, e.g. "NotFound", "SizeMismatch", "NotRunning"
        self.message = message


def _error_from_glib(error: GLib.Error) -> DaemonError:
    remote = Gio.DBusError.get_remote_error(error) or ""
    message = error.message.removeprefix(f"GDBus.Error:{remote}: ") if remote else error.message
    name = remote.removeprefix(ERROR_PREFIX) if remote.startswith(ERROR_PREFIX) else remote or "Failed"
    return DaemonError(name, message)


def to_variant(value: Any) -> GLib.Variant:
    if isinstance(value, bool):
        return GLib.Variant("b", value)
    if isinstance(value, int):
        return GLib.Variant("x", value)
    if isinstance(value, float):
        return GLib.Variant("d", value)
    if isinstance(value, str):
        return GLib.Variant("s", value)
    if isinstance(value, dict):
        return GLib.Variant("a{ss}", {str(k): str(v) for k, v in value.items()})
    raise TypeError(f"cannot send {type(value).__name__} over D-Bus")


class DaemonClient(GObject.Object):
    __gsignals__ = {
        "added": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_UINT64,)),
        "removed": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_UINT64,)),
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_UINT64, str, str)),
        "progress": (GObject.SignalFlags.RUN_FIRST, None, (object,)),  # [(id, done, size|-1, speed)]
    }

    connected = GObject.Property(type=bool, default=False)

    def __init__(self, connection: Gio.DBusConnection | None = None):
        super().__init__()
        self._connection = connection or Gio.bus_get_sync(Gio.BusType.SESSION)
        self._proxy: Gio.DBusProxy | None = None
        self._watch = Gio.bus_watch_name_on_connection(
            self._connection, BUS_NAME, Gio.BusNameWatcherFlags.NONE, self._on_appeared, self._on_vanished)

    @classmethod
    def for_address(cls, address: str) -> DaemonClient:
        flags = (Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                 | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
        return cls(Gio.DBusConnection.new_for_address_sync(address, flags, None, None))

    def close(self) -> None:
        Gio.bus_unwatch_name(self._watch)
        self._proxy = None

    # --- connection state ------------------------------------------------------------------

    def _on_appeared(self, connection, name, owner) -> None:
        # DO_NOT_AUTO_START: the GUI only starts the daemon when the user asks.
        flags = Gio.DBusProxyFlags.DO_NOT_AUTO_START | Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES
        self._proxy = Gio.DBusProxy.new_sync(connection, flags, None, BUS_NAME, OBJECT_PATH, INTERFACE, None)
        self._proxy.connect("g-signal", self._on_signal)
        self.connected = True

    def _on_vanished(self, connection, name) -> None:
        self._proxy = None
        self.connected = False

    def _on_signal(self, proxy, sender, signal_name, parameters) -> None:
        args = parameters.unpack()
        if signal_name == "Added":
            self.emit("added", *args)
        elif signal_name == "Removed":
            self.emit("removed", *args)
        elif signal_name == "StateChanged":
            self.emit("state-changed", *args)
        elif signal_name == "Progress":
            self.emit("progress", list(args[0]))

    def start_daemon(self) -> None:
        """Start `ldm daemon` in its own session so it outlives the GUI. Raises OSError on failure."""
        log_dir = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "ldm"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "daemon.log", "ab") as log:
            process = subprocess.Popen([sys.executable, "-m", "ldm", "daemon"], stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, start_new_session=True)
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, process.pid, lambda *_: None)  # reap on exit

    # --- calls -----------------------------------------------------------------------------

    def _call(self, method: str, signature: str, args: tuple, callback: Callback | None) -> None:
        if self._proxy is None:
            if callback:
                GLib.idle_add(callback, None, DaemonError("NotRunning", "the download service is not running"))
            return
        parameters = GLib.Variant(f"({signature})", args) if signature else None

        def done(proxy, result):
            try:
                value = proxy.call_finish(result).unpack()
            except GLib.Error as e:
                if callback:
                    callback(None, _error_from_glib(e))
                return
            if callback:
                callback(value[0] if len(value) == 1 else None, None)

        self._proxy.call(method, parameters, Gio.DBusCallFlags.NO_AUTO_START, -1, None, done)

    def add(self, url: str, options: dict[str, Any] | None = None, callback: Callback | None = None) -> None:
        variants = {k: to_variant(v) for k, v in (options or {}).items() if v is not None}
        self._call("Add", "sa{sv}", (url, variants), callback)

    def pause(self, download_id: int, callback: Callback | None = None) -> None:
        self._call("Pause", "t", (download_id,), callback)

    def resume(self, download_id: int, callback: Callback | None = None) -> None:
        self._call("Resume", "t", (download_id,), callback)

    def remove(self, download_id: int, delete_files: bool, callback: Callback | None = None) -> None:
        self._call("Remove", "tb", (download_id, delete_files), callback)

    def move(self, download_id: int, position: int, callback: Callback | None = None) -> None:
        self._call("Move", "tu", (download_id, position), callback)

    def set_url(self, download_id: int, url: str, callback: Callback | None = None) -> None:
        self._call("SetUrl", "ts", (download_id, url), callback)

    def set_speed_limit(self, download_id: int, rate: int, callback: Callback | None = None) -> None:
        self._call("SetSpeedLimit", "tt", (download_id, rate), callback)

    def list(self, callback: Callback) -> None:
        self._call("List", "", (), callback)

    def get(self, download_id: int, callback: Callback) -> None:
        self._call("Get", "t", (download_id,), callback)

    def get_settings(self, callback: Callback) -> None:
        self._call("GetSettings", "", (), callback)

    def set_settings(self, values: dict[str, Any], callback: Callback | None = None) -> None:
        self._call("SetSettings", "a{sv}", ({k: to_variant(v) for k, v in values.items()},), callback)

    def set_host_rule(self, host: str, max_connections: int, max_downloads: int, include_subdomains: bool,
                      callback: Callback | None = None) -> None:
        self._call("SetHostRule", "suub", (host, max_connections, max_downloads, include_subdomains), callback)

    def remove_host_rule(self, host: str, callback: Callback | None = None) -> None:
        self._call("RemoveHostRule", "s", (host,), callback)

    def list_host_rules(self, callback: Callback) -> None:
        self._call("ListHostRules", "", (), callback)
