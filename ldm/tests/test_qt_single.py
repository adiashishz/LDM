"""One LDM window per user without D-Bus (Windows, macOS): a second start hands its links to the first."""

import subprocess
import sys
import time
import uuid

import pytest

pytest.importorskip("PySide6.QtNetwork")
from ldm.qt.single import SingleInstance, server_name  # noqa: E402


def spin_until(app, condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        app.processEvents()
        time.sleep(0.01)


SECOND_START = """
import sys
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from ldm.qt.single import SingleInstance
sys.exit(0 if SingleInstance(sys.argv[1]).forward(sys.argv[2:]) else 1)
"""


def second_start(app, name, links):
    """A second LDM start, as in real use: another process. (In this one, Windows' pipe would wait for the
    window to read while the window waits for the write.)"""
    process = subprocess.Popen([sys.executable, "-c", SECOND_START, name, *links])
    spin_until(app, lambda: process.poll() is not None, timeout=60)
    return process.returncode == 0


def test_second_start_hands_its_links_to_the_first(qt_app):
    name = f"ldm-test-{uuid.uuid4().hex}"
    first = SingleInstance(name)
    assert not first.forward(["https://example.com/a.zip"])  # nobody listens: this one is first
    assert first.listen()
    received = []
    first.received.connect(received.append)
    try:
        assert second_start(qt_app, name, ["https://example.com/a.zip", "magnet:?xt=urn:btih:abc"])
        assert second_start(qt_app, name, [])  # a plain second start: just bring the window forward
        spin_until(qt_app, lambda: len(received) == 2)
    finally:
        first.close()
    assert received == [["https://example.com/a.zip", "magnet:?xt=urn:btih:abc"], []]
    assert not SingleInstance(name).forward(["x"])  # closed: the next start is first again


def test_garbage_is_ignored(qt_app):
    from PySide6.QtNetwork import QLocalSocket

    name = f"ldm-test-{uuid.uuid4().hex}"
    first = SingleInstance(name)
    assert first.listen()
    received = []
    first.received.connect(received.append)
    try:
        socket = QLocalSocket()
        socket.connectToServer(name)
        assert socket.waitForConnected(1000)
        socket.write(b"not json\n{\"links\": [\"https://example.com/b\"]}\n")
        socket.flush()  # (no blocking wait: the reading end is this thread)
        spin_until(qt_app, lambda: received)
        socket.disconnectFromServer()
    finally:
        first.close()
    assert received == [["https://example.com/b"]]


def test_the_name_is_per_user():
    assert server_name().startswith("io.github.adiashishz.LDM-") and len(server_name()) > 25
