"""What to do when all downloads finished (1DM): quit LDM, or suspend / power off after a countdown.

Suspend and power off first show a notification with Cancel; a download starting also cancels.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from .notify import GUI_ID, GUI_PATH, NOTIFICATIONS
from .system import LOGIND, SystemMonitor

log = logging.getLogger(__name__)

COUNTDOWN = 60.0
ACTIONS = {"suspend": ("Suspend", "Suspending"), "poweroff": ("PowerOff", "Powering off")}


class Finisher:
    def __init__(self, bus: MessageBus, monitor: SystemMonitor, stop: Callable[[], None],
                 countdown: float = COUNTDOWN):
        self.bus, self.monitor, self.stop, self.countdown = bus, monitor, stop, countdown
        self._pending: asyncio.Task | None = None
        self._notification = 0
        bus.add_message_handler(self._on_message)

    def run(self, action: str) -> None:
        if action == "quit":
            asyncio.get_running_loop().create_task(self._quit())
        elif action in ACTIONS and self._pending is None:
            self._pending = asyncio.get_running_loop().create_task(self._count_down(action))

    def cancel(self, *_) -> None:
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None

    async def _quit(self) -> None:
        """Close the window if it's open, then stop the service (downloads are all finished)."""
        try:
            if (await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                 "NameHasOwner", "s", [GUI_ID])).body[0]:
                await self._call(GUI_ID, GUI_PATH, "org.freedesktop.Application", "ActivateAction", "sava{sv}",
                                 ["quit", [], {}])
        except RuntimeError as e:
            log.info("could not close the window: %s", e)
        self.stop()

    async def _count_down(self, action: str) -> None:
        method, doing = ACTIONS[action]
        try:
            try:
                reply = await self._call(*NOTIFICATIONS, "Notify", "susssasa{sv}i",
                                         ["LDM", 0, GUI_ID, "All downloads finished",
                                          f"{doing} in {int(self.countdown)} seconds", ["cancel", "Cancel"],
                                          {"desktop-entry": Variant("s", GUI_ID)}, -1])
                self._notification = reply.body[0]
            except RuntimeError as e:
                log.info("no countdown notification: %s", e)
            await asyncio.sleep(self.countdown)
            await self.monitor._call(*LOGIND, method, "b", [False])
            log.info("all downloads finished: %s", method)
        except asyncio.CancelledError:
            log.info("%s cancelled", action)
            raise
        except Exception as e:
            log.warning("could not %s: %s", action, e)
        finally:
            self._pending = None
            if self._notification:
                try:
                    await self._call(*NOTIFICATIONS, "CloseNotification", "u", [self._notification])
                except Exception:
                    pass
                self._notification = 0

    def _on_message(self, msg: Message) -> None:
        if (msg.message_type == MessageType.SIGNAL and msg.interface == NOTIFICATIONS[2]
                and msg.member == "ActionInvoked" and self._notification and msg.body[0] == self._notification):
            self.cancel()

    async def _call(self, destination, path, interface, member, signature="", body=None) -> Message:
        reply = await self.bus.call(Message(destination=destination, path=path, interface=interface,
                                            member=member, signature=signature, body=body or []))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {reply.error_name}")
        return reply
