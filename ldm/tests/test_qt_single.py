"""One LDM window per user without D-Bus (Windows, macOS): a second start hands its links to the first."""

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


def test_second_start_hands_its_links_to_the_first(qt_app):
    name = f"ldm-test-{uuid.uuid4().hex}"
    first, second = SingleInstance(name), SingleInstance(name)
    assert not first.forward(["https://example.com/a.zip"])  # nobody listens: this one is first
    assert first.listen()
    received = []
    first.received.connect(received.append)
    try:
        assert second.forward(["https://example.com/a.zip", "magnet:?xt=urn:btih:abc"])
        assert SingleInstance(name).forward([])  # a plain second start: just bring the window forward
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
        socket.waitForBytesWritten(1000)
        spin_until(qt_app, lambda: received)
        socket.disconnectFromServer()
    finally:
        first.close()
    assert received == [["https://example.com/b"]]


def test_the_name_is_per_user():
    assert server_name().startswith("io.github.adiashishz.LDM-") and len(server_name()) > 25
