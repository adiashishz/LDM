"""The Qt window on a private session bus: its name and actions, as the browser bridge, the clipboard watcher,
the tray and the launcher use them; one window only; notifications."""

import shutil
import subprocess
import time

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QSettings  # noqa: E402



@pytest.fixture
def app(qt_app):
    return qt_app


def spin_until(app, condition, timeout=15.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        app.processEvents()
        time.sleep(0.005)


@pytest.fixture
def ldm_window(app, bus_address, daemon, tmp_path):
    """The window as `ldm-gui` starts it: its client, its place on the bus, then the window (hidden)."""
    from ldm.qt.client import DaemonClient
    from ldm.qt.desktop import Desktop
    from ldm.qt.window import MainWindow

    client = DaemonClient(daemon)
    desktop = Desktop(client)
    assert desktop.claim() is True
    window = MainWindow(client, QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    desktop.window = window
    spin_until(app, lambda: client.connected)
    yield desktop, window
    window.close()


def add_dialogs(window):
    from ldm.qt.add_dialog import AddDialog

    return window.findChildren(AddDialog)


def test_a_second_start_hands_its_link_to_the_open_window(app, daemon, ldm_window):
    from ldm.qt.client import DaemonClient
    from ldm.qt.desktop import Desktop

    _desktop, window = ldm_window
    second = Desktop(DaemonClient(daemon))
    assert second.claim() is False  # the name is taken: this start must not open another window
    assert second.forward(["https://example.com/file.iso"])
    second.client.close()
    spin_until(app, lambda: add_dialogs(window))
    assert add_dialogs(window)[0].url.text() == "https://example.com/file.iso"
    assert window.isVisible()


@pytest.mark.skipif(shutil.which("gdbus") is None, reason="gdbus not installed")
def test_the_browser_bridge_opens_the_add_dialog(app, ldm_window, monkeypatch, daemon):
    """The native host's own call (gdbus → org.gtk.Actions.Activate "add-request")."""
    from ldm import nmhost

    _desktop, window = ldm_window
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", daemon)
    nmhost.add_dialog({"url": "https://example.com/video.mp4", "filename": "talk.mp4",
                       "referer": "https://example.com/page"})
    spin_until(app, lambda: add_dialogs(window))
    dialog = add_dialogs(window)[0]
    assert dialog.url.text() == "https://example.com/video.mp4" and dialog.name.text() == "talk.mp4"
    assert dialog.referer.text() == "https://example.com/page"
    reply = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path",
                            "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.NameHasOwner",
                            "io.github.adiashishz.LDM"], capture_output=True, text=True, timeout=10,
                           env={"DBUS_SESSION_BUS_ADDRESS": daemon, "PATH": "/usr/bin:/bin"})
    assert reply.stdout.startswith("(true")  # so the service leaves the notifications to the window


def test_show_action_brings_the_window_forward(app, ldm_window, daemon):
    import asyncio

    from dbus_fast import Message
    from dbus_fast.aio import MessageBus

    _desktop, window = ldm_window
    assert not window.isVisible()

    async def call():  # as the tray and the service's notifications do
        bus = await MessageBus(bus_address=daemon).connect()
        reply = await bus.call(Message(destination="io.github.adiashishz.LDM", path="/io/github/adiashishz/LDM",
                                       interface="org.freedesktop.Application", member="ActivateAction",
                                       signature="sava{sv}", body=["show", [], {}]))
        bus.disconnect()
        return reply.message_type.name

    assert asyncio.run(call()) == "METHOD_RETURN"
    spin_until(app, window.isVisible)


def test_notifies_when_a_download_it_saw_running_finishes(app, ldm_window, monkeypatch):
    desktop, window = ldm_window
    sent = []
    monkeypatch.setattr(desktop.remote, "submit", lambda job: sent.append(job))
    window.downloads.upsert({"id": 1, "url": "https://example.com/a.zip", "name": "a.zip", "state": "running"})
    desktop._state_changed(1, "done", "")  # finished without the window seeing it run: no notification
    assert sent == []
    desktop._state_changed(1, "running", "")
    desktop._state_changed(1, "done", "")
    assert len(sent) == 1
    opened = []
    window.open_file = opened.append
    desktop._clicked((1, "done"), "open")
    assert [d.id for d in opened] == [1]


def test_add_request_parsing():
    from ldm.ui.desktop import add_request

    assert add_request(['{"url": "https://x/a", "filename": "a"}']) == {"url": "https://x/a", "filename": "a"}
    assert add_request(["not json"]) is None
    assert add_request(['{"filename": "no url"}']) is None
    assert add_request([]) is None


def test_browser_request_reaches_the_window_over_the_socket(app, daemon, tmp_path, monkeypatch):
    """Where there is no D-Bus (Windows, macOS): the browser bridge → the service → the window's Add dialog."""
    from ldm import channel, nmhost
    from ldm.qt.client import DaemonClient
    from ldm.qt.window import MainWindow

    monkeypatch.setenv("LDM_TRANSPORT", "socket")
    client = DaemonClient(str(channel.address()), transport="socket")
    window = MainWindow(client, QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    spin_until(app, lambda: client.connected)
    spin_until(app, lambda: nmhost.channel_call("WindowOpen"))  # registered as the window
    nmhost.add_dialog({"url": "https://example.com/over-socket.iso", "filename": "over-socket.iso"})
    spin_until(app, lambda: add_dialogs(window))
    dialog = add_dialogs(window)[0]
    assert dialog.url.text() == "https://example.com/over-socket.iso" and dialog.name.text() == "over-socket.iso"
    assert window.isVisible()
    window.close()
