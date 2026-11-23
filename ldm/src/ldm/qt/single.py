"""One LDM window per user where there is no D-Bus (Windows, macOS): the first window listens on a local
server (a named pipe on Windows); a second start hands its links over and exits.

Linux does this through the window's bus name (ldm.qt.desktop). Messages are one JSON line:
{"links": ["https://…", "magnet:…", "file:///…torrent"]}; an empty list just brings the window forward.
"""

from __future__ import annotations

import getpass
import json
import logging
import sys

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

CONNECT_MS = 500
WRITE_MS = 2_000


def server_name() -> str:
    """Per user: Windows' pipe names are shared by everyone on the computer."""
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return f"io.github.adiashishz.LDM-{user}"


class SingleInstance(QObject):
    received = Signal(list)  # links from a second start

    def __init__(self, name: str | None = None):
        super().__init__()
        self.name = name or server_name()
        self.server: QLocalServer | None = None
        self._buffers: dict[QLocalSocket, bytes] = {}

    def forward(self, links: list[str]) -> bool:
        """Hand the links to the window that runs already; False when there is none (this start is first)."""
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(CONNECT_MS):
            return False
        if sys.platform == "win32":  # let the running window come to the front (Windows' focus rules)
            import ctypes

            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        socket.write(json.dumps({"links": links}).encode() + b"\n")
        socket.flush()
        # Windows may write it right away, and then waitForBytesWritten() reports False: what counts is that
        # nothing is left to write.
        while socket.bytesToWrite() and socket.waitForBytesWritten(WRITE_MS):
            pass
        sent = socket.bytesToWrite() == 0
        socket.disconnectFromServer()
        if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            socket.waitForDisconnected(WRITE_MS)
        return sent

    def listen(self) -> bool:
        """Become the window others hand their links to."""
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)  # this user only
        if not self.server.listen(self.name):
            QLocalServer.removeServer(self.name)  # left behind by a window that crashed (Unix sockets)
            if not self.server.listen(self.name):
                log.warning("can't make this the only LDM window: %s", self.server.errorString())
                return False
        self.server.newConnection.connect(self._accept)
        return True

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self._buffers[socket] = b""
            socket.readyRead.connect(self._ready)
            socket.disconnected.connect(self._gone)

    def _ready(self) -> None:
        socket = self.sender()
        if socket in self._buffers:
            self._read(socket)

    def _gone(self) -> None:
        socket = self.sender()
        if socket in self._buffers:
            self._read(socket)  # what arrived with the close
            del self._buffers[socket]
            socket.deleteLater()

    def _read(self, socket: QLocalSocket) -> None:
        self._buffers[socket] += bytes(socket.readAll())
        while b"\n" in self._buffers[socket]:
            line, self._buffers[socket] = self._buffers[socket].split(b"\n", 1)
            try:
                links = json.loads(line)["links"]
            except (ValueError, KeyError, TypeError):
                log.info("ignored a message from another LDM start: %r", line[:200])
                continue
            self.received.emit([str(link) for link in links])

    def close(self) -> None:
        """Stop listening and drop the connections now (not later, when their owner may be gone)."""
        for socket in list(self._buffers):
            socket.readyRead.disconnect(self._ready)
            socket.disconnected.disconnect(self._gone)
            socket.abort()
            socket.setParent(None)
        self._buffers.clear()
        if self.server is not None:
            self.server.close()
