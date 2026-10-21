"""SystemMonitor and SleepInhibitor against fake NetworkManager, UPower and logind on a private bus."""

import asyncio
import os
import shutil

import pytest
from dbus_fast import PropertyAccess
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_property, method

from ldm.system import SleepInhibitor, SystemMonitor

pytestmark = pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="dbus-daemon not installed")


class FakeNM(ServiceInterface):
    def __init__(self):
        super().__init__("org.freedesktop.NetworkManager")
        self.metered = 4  # NM_METERED_GUESS_NO

    @dbus_property(access=PropertyAccess.READ)
    def Metered(self) -> "u":
        return self.metered


class FakeUPower(ServiceInterface):
    def __init__(self):
        super().__init__("org.freedesktop.UPower")
        self.on_battery = False

    @dbus_property(access=PropertyAccess.READ)
    def OnBattery(self) -> "b":
        return self.on_battery


class FakeBattery(ServiceInterface):
    def __init__(self):
        super().__init__("org.freedesktop.UPower.Device")
        self.percentage = 80.0

    @dbus_property(access=PropertyAccess.READ)
    def Percentage(self) -> "d":
        return self.percentage


class FakeLogind(ServiceInterface):
    def __init__(self):
        super().__init__("org.freedesktop.login1.Manager")
        self.locks = []  # our ends of the pipes handed out

    @method()
    def Inhibit(self, what: "s", who: "s", why: "s", mode: "s") -> "h":
        ours, theirs = os.pipe()
        self.locks.append((ours, what, who, mode))
        # Like logind, keep only our end once the reply (with a copy of theirs) has been sent.
        asyncio.get_running_loop().call_later(0.05, os.close, theirs)
        return theirs


def released(fd: int) -> bool:
    """The client closed its end of the lock pipe."""
    os.set_blocking(fd, False)
    try:
        return os.read(fd, 1) == b""
    except BlockingIOError:
        return False


def test_monitor_follows_properties_and_inhibitor_holds_a_lock(bus_address):
    async def scenario():
        service = await MessageBus(bus_address=bus_address, negotiate_unix_fd=True).connect()
        nm, upower, battery, logind = FakeNM(), FakeUPower(), FakeBattery(), FakeLogind()
        service.export("/org/freedesktop/NetworkManager", nm)
        service.export("/org/freedesktop/UPower", upower)
        service.export("/org/freedesktop/UPower/devices/DisplayDevice", battery)
        service.export("/org/freedesktop/login1", logind)
        for name in ("org.freedesktop.NetworkManager", "org.freedesktop.UPower", "org.freedesktop.login1"):
            await service.request_name(name)

        changes = []
        monitor = SystemMonitor(bus_address, on_change=lambda: changes.append(
            (monitor.metered, monitor.on_battery, monitor.battery)))
        await monitor.start()
        assert (monitor.metered, monitor.on_battery, monitor.battery) == (False, False, 80.0)

        nm.metered = 1  # NM_METERED_YES
        nm.emit_properties_changed({"Metered": nm.metered})
        upower.on_battery = True
        upower.emit_properties_changed({"OnBattery": True})
        battery.percentage = 15.0
        battery.emit_properties_changed({"Percentage": 15.0})
        for _ in range(100):
            if len(changes) == 3:
                break
            await asyncio.sleep(0.02)
        assert changes == [(True, False, 80.0), (True, True, 80.0), (True, True, 15.0)]

        inhibitor = SleepInhibitor(monitor)
        inhibitor.set_active(True)
        for _ in range(100):
            if inhibitor.fd is not None:
                break
            await asyncio.sleep(0.02)
        assert inhibitor.fd is not None and inhibitor.fd > 2  # a real lock fd, never stdin/stdout/stderr
        ours, what, who, mode = logind.locks[0]
        assert (what, who, mode) == ("sleep:idle", "LDM", "block")
        assert not released(ours)
        inhibitor.set_active(False)
        await asyncio.sleep(0.1)
        assert inhibitor.fd is None and released(ours)

        await monitor.close()
        service.disconnect()

    asyncio.run(scenario())


def test_missing_services_mean_unmetered_on_mains(bus_address):
    async def scenario():
        monitor = SystemMonitor(bus_address)
        await monitor.start()
        inhibitor = SleepInhibitor(monitor)
        inhibitor.set_active(True)
        await asyncio.sleep(0.1)
        state = (monitor.metered, monitor.on_battery, monitor.battery, inhibitor.fd)
        inhibitor.close()
        await monitor.close()
        return state

    assert asyncio.run(scenario()) == (False, False, 100.0, None)
