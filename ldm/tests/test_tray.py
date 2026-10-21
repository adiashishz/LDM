"""Tray icon (StatusNotifierItem + dbusmenu) against a fake tray host on a private bus."""

import asyncio
import shutil

import pytest
from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

from ldm.tray import ITEM_PATH, MENU_PATH, Tray, summary_text

from test_notify import FakeGui, until

pytestmark = pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="dbus-daemon not installed")


class FakeWatcher(ServiceInterface):
    def __init__(self):
        super().__init__("org.kde.StatusNotifierWatcher")
        self.items = []

    @method()
    def RegisterStatusNotifierItem(self, service: "s"):
        self.items.append(service)


def test_summary_text():
    assert summary_text(0, 0, 0, 0.0) == "No downloads running"
    assert summary_text(3, 450, 1000, 2 * 1024 * 1024) == "3 downloading · 45% · 2.0 MiB/s"


def test_tray_registers_shows_progress_and_runs_menu_items(bus_address):
    async def scenario():
        host = await MessageBus(bus_address=bus_address).connect()
        bus = await MessageBus(bus_address=bus_address).connect()
        progress = [(0, 0, 0, 0.0)]
        actions = []
        tray = Tray(bus, lambda: progress[0], lambda: actions.append("pause"), lambda: actions.append("resume"),
                    lambda: actions.append("quit"))
        await tray.start()  # no tray host yet: nothing to register with

        watcher, gui = FakeWatcher(), FakeGui()
        host.export("/StatusNotifierWatcher", watcher)
        host.export("/io/github/adiashishz/LDM", gui)
        await host.request_name("io.github.adiashishz.LDM")
        await host.request_name("org.kde.StatusNotifierWatcher")  # the host appears later: LDM registers
        await until(lambda: watcher.items)
        assert watcher.items == [bus.unique_name]

        async def call(path, interface, member, signature="", body=None):
            reply = await host.call(Message(destination=bus.unique_name, path=path, interface=interface,
                                            member=member, signature=signature, body=body or []))
            assert reply.message_type == MessageType.METHOD_RETURN, reply.body
            return reply.body

        async def prop(name):
            return (await call(ITEM_PATH, "org.freedesktop.DBus.Properties", "Get", "ss",
                               ["org.kde.StatusNotifierItem", name]))[0].value

        assert await prop("Status") == "Passive" and await prop("IconName") == "io.github.adiashishz.LDM"
        progress[0] = (2, 300, 1000, 1024.0)
        tray.update()
        assert await prop("Status") == "Active"
        assert (await prop("ToolTip"))[3] == "2 downloading · 30% · 1.0 KiB/s"

        revision, (root, props, children) = await call(MENU_PATH, "com.canonical.dbusmenu", "GetLayout", "iias",
                                                       [0, -1, []])
        labels = [c.value[1].get("label", Variant("s", "—")).value for c in children]
        assert labels == ["Show LDM", "—", "Pause All", "Resume All", "—", "Quit LDM"]
        for item_id in (3, 4, 6):
            await call(MENU_PATH, "com.canonical.dbusmenu", "Event", "isvu", [item_id, "clicked", Variant("i", 0), 0])
        assert actions == ["pause", "resume", "quit"]
        await call(ITEM_PATH, "org.kde.StatusNotifierItem", "Activate", "ii", [0, 0])
        await until(lambda: gui.actions)
        assert gui.actions == ["show"]

        tray.close()
        bus.disconnect()
        host.disconnect()

    asyncio.run(scenario())
