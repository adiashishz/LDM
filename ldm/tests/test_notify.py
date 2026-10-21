"""Daemon notifications: only while the LDM window isn't running; actions open the file or show LDM."""

import asyncio
import shutil

import pytest
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method, signal

from ldm.manager import Manager
from ldm.notify import Notifier
from ldm.store import Store

pytestmark = pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="dbus-daemon not installed")


class FakeNotifications(ServiceInterface):
    def __init__(self):
        super().__init__("org.freedesktop.Notifications")
        self.shown = []

    @method()
    def Notify(self, app: "s", replaces: "u", icon: "s", summary: "s", body: "s", actions: "as",
               hints: "a{sv}", timeout: "i") -> "u":
        self.shown.append((app, icon, summary, body, actions, hints["desktop-entry"].value))
        return len(self.shown)

    @signal()
    def ActionInvoked(self, notification_id, action) -> "us":
        return [notification_id, action]


class FakeGui(ServiceInterface):
    def __init__(self):
        super().__init__("org.freedesktop.Application")
        self.actions, self.parameters = [], []

    @method()
    def ActivateAction(self, name: "s", parameter: "av", platform_data: "a{sv}"):
        self.actions.append(name)
        self.parameters.extend(v.value for v in parameter)


async def until(condition, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


def test_notifications_only_without_gui(bus_address, tmp_path):
    async def scenario():
        services = await MessageBus(bus_address=bus_address).connect()
        notifications, gui = FakeNotifications(), FakeGui()
        services.export("/org/freedesktop/Notifications", notifications)
        services.export("/io/github/ldm/Gui", gui)
        await services.request_name("org.freedesktop.Notifications")

        manager = Manager(Store(tmp_path / "state.db"))
        manager.store.set_setting("download_dir", str(tmp_path))
        bus = await MessageBus(bus_address=bus_address).connect()
        opened = []
        notifier = Notifier(bus, manager, opener=opened.append)
        await notifier.start()

        first = manager.add("http://127.0.0.1:9/movie.mkv", name="movie.mkv", start=False)
        manager.on_state(first, "done", "")
        await until(lambda: notifications.shown)
        assert notifications.shown[0] == ("LDM", "io.github.ldm.Gui", "Download finished", "movie.mkv — 0 B",
                                          ["default", "Show in LDM", "open", "Open"], "io.github.ldm.Gui")
        notifications.ActionInvoked(1, "open")
        await until(lambda: opened)
        assert opened == [str(tmp_path / "movie.mkv")]

        manager.on_state(first, "error", "HTTP 404")
        await until(lambda: len(notifications.shown) == 2)
        assert notifications.shown[1][2:4] == ("Download failed", "movie.mkv: HTTP 404")
        await services.request_name("io.github.ldm.Gui")  # what D-Bus activation would do
        notifications.ActionInvoked(2, "default")
        await until(lambda: gui.actions)
        assert gui.actions == ["show"]

        manager.on_state(first, "done", "")  # the window is running now: it notifies by itself
        await asyncio.sleep(0.2)
        assert len(notifications.shown) == 2
        await services.release_name("io.github.ldm.Gui")
        manager.store.set_setting("notify_when_closed", False)
        manager.on_state(first, "done", "")
        await asyncio.sleep(0.2)
        assert len(notifications.shown) == 2

        bus.disconnect()
        services.disconnect()

    asyncio.run(scenario())
