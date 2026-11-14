"""Desktop conditions for the scheduler: metered network, battery, and keeping the computer awake.

1DM's "Wi-Fi only" becomes "unmetered connections only" (NetworkManager's Metered property), its
battery limit uses UPower, and its wake lock becomes a logind sleep inhibitor (the Inhibit portal in a
Flatpak, which can't reach logind). Missing services are treated as unmetered / on mains power, so LDM
keeps working without them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, Callable

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from . import sandbox

log = logging.getLogger(__name__)

NM = ("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager", "org.freedesktop.NetworkManager")
UPOWER = ("org.freedesktop.UPower", "/org/freedesktop/UPower", "org.freedesktop.UPower")
PORTAL_INHIBIT = ("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                  "org.freedesktop.portal.Inhibit")
INHIBIT_SUSPEND_AND_IDLE = 4 | 8  # the portal's flags
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
    """Keeps the computer from suspending while downloads run: a logind inhibitor lock, or in a Flatpak a
    request to the Inhibit portal (`session_bus_address` replaces the session bus: tests use a private one)."""

    def __init__(self, monitor: SystemMonitor, session_bus_address: str | None = None):
        self.monitor = monitor
        self.fd: int | None = None
        self.portal_request: str | None = None
        self.session_bus_address = session_bus_address
        self._session: MessageBus | None = None
        self._wanted = False
        self._busy = asyncio.Lock()

    def set_active(self, active: bool) -> None:
        if active != self._wanted:
            self._wanted = active
            asyncio.get_running_loop().create_task(self._sync())

    async def _sync(self) -> None:
        if sandbox.flatpak():
            async with self._busy:
                await self._sync_portal()
            return
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

    async def _sync_portal(self) -> None:
        if self._wanted and self.portal_request is None:
            try:
                if self._session is None:
                    self._session = await MessageBus(bus_address=self.session_bus_address).connect()
                reply = await self._session.call(Message(
                    destination=PORTAL_INHIBIT[0], path=PORTAL_INHIBIT[1], interface=PORTAL_INHIBIT[2],
                    member="Inhibit", signature="sua{sv}",
                    body=["", INHIBIT_SUSPEND_AND_IDLE, {"reason": Variant("s", "Downloading files")}]))
                if reply.message_type == MessageType.ERROR:
                    raise RuntimeError(reply.error_name)
                self.portal_request = reply.body[0]
                log.info("preventing sleep while downloading")
            except Exception as e:
                log.info("could not prevent sleep: %s", e)
        elif not self._wanted and self.portal_request is not None:
            request, self.portal_request = self.portal_request, None
            with contextlib.suppress(Exception):  # the portal also drops it when LDM disconnects
                await self._session.call(Message(destination=PORTAL_INHIBIT[0], path=request,
                                                 interface="org.freedesktop.portal.Request", member="Close"))

    def close(self) -> None:
        self._wanted = False
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None
        if self._session is not None:  # disconnecting ends a portal inhibition
            self._session.disconnect()
            self._session, self.portal_request = None, None
