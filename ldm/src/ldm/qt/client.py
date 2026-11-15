"""The background service as a Qt object: its calls, and its signals as Qt signals.

The connection itself is `ldm.ui.remote.Remote`; this class hands its replies and signals to Qt's
event loop, so callbacks and slots always run on the window's thread.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, Signal, Slot

from ..ui.remote import DEFAULT_TIMEOUT, Callback, DaemonError, Remote, ServiceCalls, start_daemon

__all__ = ["DaemonClient", "DaemonError"]


class DaemonClient(QObject, ServiceCalls):
    added = Signal("qulonglong")
    removed = Signal("qulonglong")
    state_changed = Signal("qulonglong", str, str)  # id, state, error
    progress = Signal(object)                        # [(id, done, size|-1, speed)]
    connected_changed = Signal(bool)

    _posted = Signal(object)  # a callable from the D-Bus thread, run on this object's thread

    def __init__(self, address: str | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self._posted.connect(self._run_posted, Qt.ConnectionType.QueuedConnection)
        self._remote = Remote(self._posted.emit, self.connected_changed.emit, self._on_signal, address)

    @property
    def connected(self) -> bool:
        return self._remote.connected

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

    def _call(self, method: str, signature: str, args: list[Any], callback: Callback | None,
              timeout: float | None = DEFAULT_TIMEOUT) -> None:
        self._remote.call(method, signature, args, callback, timeout)
