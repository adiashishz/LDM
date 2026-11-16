"""The background service as a Qt object: its calls, and its signals as Qt signals.

The connection itself is `ldm.ui.remote.Remote` (D-Bus, on Linux) or `SocketRemote` (the local channel,
elsewhere, or with LDM_TRANSPORT=socket); this class hands their replies and signals to Qt's event loop, so
callbacks and slots always run on the window's thread.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, Signal, Slot

from ..ui.remote import DEFAULT_TIMEOUT, Callback, DaemonError, Remote, ServiceCalls, SocketRemote, start_daemon

__all__ = ["DaemonClient", "DaemonError"]


class DaemonClient(QObject, ServiceCalls):
    added = Signal("qulonglong")
    removed = Signal("qulonglong")
    state_changed = Signal("qulonglong", str, str)  # id, state, error
    progress = Signal(object)                        # [(id, done, size|-1, speed)]
    connected_changed = Signal(bool)
    add_request = Signal(object)  # the browser's Add request (local channel), as {"url", "filename", …}

    _posted = Signal(object)  # a callable from the D-Bus thread, run on this object's thread

    def __init__(self, address: str | None = None, parent: QObject | None = None, transport: str | None = None):
        """`address`: the D-Bus address, or the socket's path (default: the user's bus or socket);
        `transport`: "dbus" or "socket" (default: LDM_TRANSPORT, else D-Bus on Linux and the socket elsewhere)."""
        super().__init__(parent)
        self._posted.connect(self._run_posted, Qt.ConnectionType.QueuedConnection)
        self.transport = transport or os.environ.get("LDM_TRANSPORT") or (
            "dbus" if sys.platform.startswith("linux") else "socket")
        if self.transport == "socket":
            self._remote = SocketRemote(self._posted.emit, self.connected_changed.emit, self._on_signal, address,
                                        register_window=True)
        else:
            self._remote = Remote(self._posted.emit, self.connected_changed.emit, self._on_signal, address)

    @property
    def connected(self) -> bool:
        return self._remote.connected

    @property
    def remote(self) -> Remote | SocketRemote:
        """The connection itself, for the window's own place on the bus (ldm.qt.desktop; D-Bus only)."""
        return self._remote

    def close(self) -> None:
        self._remote.close()

    def start_daemon(self) -> None:
        """Start `ldm daemon` in its own session so it outlives the window. Raises OSError on failure."""
        start_daemon()

    @Slot(object)
    def _run_posted(self, function: Callable[[], None]) -> None:
        function()

    def _on_signal(self, name: str, args: list[Any]) -> None:
        if name == "Added":
            self.added.emit(args[0])
        elif name == "Removed":
            self.removed.emit(args[0])
        elif name == "StateChanged":
            self.state_changed.emit(*args)
        elif name == "Progress":
            self.progress.emit([tuple(entry) for entry in args[0]])
        elif name == "AddRequest" and args and isinstance(args[0], dict):
            self.add_request.emit(args[0])

    def _call(self, method: str, signature: str, args: list[Any], callback: Callback | None,
              timeout: float | None = DEFAULT_TIMEOUT) -> None:
        self._remote.call(method, signature, args, callback, timeout)
