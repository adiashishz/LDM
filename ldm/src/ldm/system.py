"""Desktop conditions for the scheduler: metered network, battery, and keeping the computer awake.

1DM's "Wi-Fi only" becomes "unmetered connections only" (NetworkManager's Metered property), its
battery limit uses UPower, and its wake lock becomes a logind sleep inhibitor. Missing services are
treated as unmetered / on mains power, so LDM keeps working without them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, Callable

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus

log = logging.getLogger(__name__)

NM = ("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager", "org.freedesktop.NetworkManager")
UPOWER = ("org.freedesktop.UPower", "/org/freedesktop/UPower", "org.freedesktop.UPower")
UPOWER_DISPLAY = ("org.freedesktop.UPower", "/org/freedesktop/UPower/devices/DisplayDevice",
                  "org.freedesktop.UPower.Device")
LOGIND = ("org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager")
METERED = {1, 3}  # NM_METERED_YES, NM_METERED_GUESS_YES


class Conditions:
    """Current state; also the fallback when the system bus or its services are unavailable."""

    metered = False
    on_battery = False
    battery = 100.0


class SystemMonitor(Conditions):
    def __init__(self, bus_address: str | None = None, on_change: Callable[[], None] | None = None):
        self.bus_address = bus_address
        self.on_change = on_change
        self.bus: MessageBus | None = None

    async def start(self) -> None:
        try:
            if self.bus_address:
                self.bus = await MessageBus(bus_address=self.bus_address, negotiate_unix_fd=True).connect()
            else:
                self.bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        except Exception as e:  # no system bus (containers, tests): keep the defaults
            log.info("system bus unavailable, ignoring network/battery conditions: %s", e)
            return
        await self._refresh()
        for target in (NM, UPOWER, UPOWER_DISPLAY):
            await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch",
                             "s", [f"type='signal',interface='org.freedesktop.DBus.Properties',"
                                   f"member='PropertiesChanged',path='{target[1]}'"])
        self.bus.add_message_handler(self._on_message)

    async def close(self) -> None:
        if self.bus:
            self.bus.disconnect()
            self.bus = None

    async def _refresh(self) -> None:
        metered = await self._get(NM, "Metered")
        on_battery = await self._get(UPOWER, "OnBattery")
        battery = await self._get(UPOWER_DISPLAY, "Percentage")
        self.metered = metered in METERED if metered is not None else False
        self.on_battery = bool(on_battery) if on_battery is not None else False
        self.battery = float(battery) if battery is not None else 100.0

    async def _call(self, destination, path, interface, member, signature="", body=None):
        reply = await self.bus.call(Message(destination=destination, path=path, interface=interface,
                                            member=member, signature=signature, body=body or []))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {reply.error_name}")
        return reply

    async def _get(self, target: tuple[str, str, str], prop: str) -> Any:
        try:
            reply = await self._call(target[0], target[1], "org.freedesktop.DBus.Properties", "Get", "ss",
                                     [target[2], prop])
        except RuntimeError:
            return None
        return reply.body[0].value

    def _on_message(self, msg: Message) -> None:
        if msg.message_type != MessageType.SIGNAL or msg.member != "PropertiesChanged":
            return
        interface, changed = msg.body[0], msg.body[1]
        before = (self.metered, self.on_battery, round(self.battery))
        if interface == NM[2] and "Metered" in changed:
            self.metered = changed["Metered"].value in METERED
        elif interface == UPOWER[2] and "OnBattery" in changed:
            self.on_battery = bool(changed["OnBattery"].value)
        elif interface == UPOWER_DISPLAY[2] and "Percentage" in changed:
            self.battery = float(changed["Percentage"].value)
        if self.on_change and before != (self.metered, self.on_battery, round(self.battery)):
            self.on_change()


class SleepInhibitor:
    """Keeps the computer from suspending while downloads run (logind inhibitor lock)."""

    def __init__(self, monitor: SystemMonitor):
        self.monitor = monitor
        self.fd: int | None = None
        self._wanted = False
        self._busy = asyncio.Lock()

    def set_active(self, active: bool) -> None:
        if active != self._wanted:
            self._wanted = active
            asyncio.get_running_loop().create_task(self._sync())

    async def _sync(self) -> None:
        async with self._busy:
            if self._wanted and self.fd is None and self.monitor.bus is not None:
                try:
                    reply = await self.monitor._call(*LOGIND, "Inhibit", "ssss",
                                                     ["sleep:idle", "LDM", "Downloading files", "block"])
                    # The body holds an index into the message's file descriptors, not the fd itself.
                    self.fd = reply.unix_fds[reply.body[0]]
                    log.info("preventing sleep while downloading")
                except Exception as e:
                    log.info("could not prevent sleep: %s", e)
            elif not self._wanted and self.fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self.fd)
                self.fd = None

    def close(self) -> None:
        self._wanted = False
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None
