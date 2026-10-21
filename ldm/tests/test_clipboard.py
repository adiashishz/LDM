"""Copied-link detection and the background watcher's notification flow."""

import asyncio
import json
import shutil

import pytest
from dbus_fast.aio import MessageBus

from ldm.clipboard import Offer, background_method, candidate, run_watcher

from test_notify import FakeGui, FakeNotifications, until


@pytest.mark.parametrize("text, expected", [
    ("https://example.com/file.zip", "https://example.com/file.zip"),
    ("  https://example.com/a.iso\n", "https://example.com/a.iso"),
    ("magnet:?xt=urn:btih:abc&dn=x", "magnet:?xt=urn:btih:abc&dn=x"),
    ("magnet:?dn=no-hash", None),
    ("see https://example.com/a.zip", None),   # prose, not a link
    ("file:///etc/passwd", None),
    ("https:///nohost", None),
    ("", None),
    (None, None),
])
def test_candidate(text, expected):
    assert candidate(text) == expected


def test_excluded_extensions_and_offer_once():
    assert candidate("https://example.com/index.HTML", "html, .php") is None
    assert candidate("https://example.com/watch?v=1", "html") == "https://example.com/watch?v=1"
    offer = Offer()
    known = ["https://example.com/old.zip"]
    assert offer.check("https://example.com/old.zip", known) is None
    assert offer.check("https://example.com/new.zip", known) == "https://example.com/new.zip"
    assert offer.check("https://example.com/new.zip", known) is None  # asked already


def test_background_method(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    assert background_method() is None
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    assert background_method() == "wayland"
    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.setenv("DISPLAY", ":0")
    assert background_method() == "x11"


@pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="dbus-daemon not installed")
def test_watcher_offers_new_links_and_opens_the_add_dialog(bus_address):
    async def scenario():
        services = await MessageBus(bus_address=bus_address).connect()
        notifications, gui = FakeNotifications(), FakeGui()
        services.export("/org/freedesktop/Notifications", notifications)
        services.export("/io/github/adiashishz/LDM", gui)
        await services.request_name("org.freedesktop.Notifications")
        await services.request_name("io.github.adiashishz.LDM")
        copied: asyncio.Queue = asyncio.Queue()

        async def texts():
            while (text := await copied.get()) is not None:
                yield text

        watcher = asyncio.create_task(run_watcher(bus_address, texts=texts()))
        for text in ("hello", "magnet:?xt=urn:btih:abc", "magnet:?xt=urn:btih:abc", "https://e.com/page.html"):
            await copied.put(text)
        await until(lambda: notifications.shown)
        await asyncio.sleep(0.2)
        assert [n[2:5] for n in notifications.shown] == [
            ("Download copied link?", "magnet:?xt=urn:btih:abc", ["default", "Download", "download", "Download"]),
            ("Download copied link?", "https://e.com/page.html", ["default", "Download", "download", "Download"])]
        notifications.ActionInvoked(1, "download")
        await until(lambda: gui.actions)
        await copied.put(None)
        await watcher
        services.disconnect()
        return gui

    gui = asyncio.run(scenario())
    assert gui.actions == ["add-request"]
    assert json.loads(gui.parameters[0]) == {"url": "magnet:?xt=urn:btih:abc", "kind": "torrent"}
