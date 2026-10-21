"""When all downloads finished: quit, or suspend/power off after a countdown that can be cancelled."""

import asyncio
import shutil

import pytest
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

from ldm.finish import Finisher
from ldm.manager import Manager
from ldm.store import Store
from ldm.system import SystemMonitor

from test_manager import payload, run, wait_state
from test_notify import FakeGui, FakeNotifications, until

pytestmark = pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="dbus-daemon not installed")


class FakeLogind(ServiceInterface):
    def __init__(self):
        super().__init__("org.freedesktop.login1.Manager")
        self.calls = []

    @method()
    def Suspend(self, interactive: "b"):
        self.calls.append(("Suspend", interactive))

    @method()
    def PowerOff(self, interactive: "b"):
        self.calls.append(("PowerOff", interactive))


def test_manager_reports_when_everything_finished(tmp_path, http_server):
    data = payload(100_000)
    cfg, url = http_server(data=data)
    store = Store(tmp_path / "s.db")
    store.set_setting("download_dir", str(tmp_path))
    store.set_setting("finish_action", "suspend")
    manager = Manager(store)
    reported = []
    manager.on_all_finished = reported.append

    async def scenario():
        first = manager.add(url, name="a.bin")
        second = manager.add(url, name="b.bin")
        await wait_state(manager, first, "done")
        await wait_state(manager, second, "done")
        await asyncio.sleep(0.05)

    run(manager, scenario)
    assert reported == ["suspend"]  # once, after the last one


def test_countdown_suspends_unless_cancelled(bus_address):
    async def scenario():
        services = await MessageBus(bus_address=bus_address).connect()
        notifications, logind, gui = FakeNotifications(), FakeLogind(), FakeGui()
        services.export("/org/freedesktop/Notifications", notifications)
        services.export("/org/freedesktop/login1", logind)
        services.export("/io/github/ldm/Gui", gui)
        for name in ("org.freedesktop.Notifications", "org.freedesktop.login1", "io.github.ldm.Gui"):
            await services.request_name(name)
        bus = await MessageBus(bus_address=bus_address).connect()
        await bus.call(__import__("dbus_fast").Message(
            destination="org.freedesktop.DBus", path="/org/freedesktop/DBus", interface="org.freedesktop.DBus",
            member="AddMatch", signature="s",
            body=["type='signal',interface='org.freedesktop.Notifications',member='ActionInvoked'"]))
        monitor = SystemMonitor(bus_address)
        await monitor.start()
        stopped = []
        finisher = Finisher(bus, monitor, lambda: stopped.append(True), countdown=0.3)

        finisher.run("suspend")
        await until(lambda: notifications.shown)
        assert notifications.shown[0][2:5] == ("All downloads finished", "Suspending in 0 seconds", ["cancel", "Cancel"])
        await until(lambda: logind.calls)
        assert logind.calls == [("Suspend", False)]

        finisher.run("poweroff")
        await until(lambda: len(notifications.shown) == 2)
        notifications.ActionInvoked(2, "cancel")  # the user pressed Cancel
        await asyncio.sleep(0.5)
        assert logind.calls == [("Suspend", False)]

        finisher.run("poweroff")
        await asyncio.sleep(0.05)
        finisher.cancel()  # a download started
        await asyncio.sleep(0.5)
        assert logind.calls == [("Suspend", False)]

        finisher.run("quit")
        await until(lambda: stopped)
        assert gui.actions == ["quit"]
        await monitor.close()
        bus.disconnect()
        services.disconnect()

    asyncio.run(scenario())
