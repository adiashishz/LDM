"""Desktop notifications from the daemon while the LDM window isn't running (1DM's download notifications).

The GUI notifies on its own when it runs; the daemon only speaks up when `io.github.adiashishz.LDM` has no
owner on the bus. "Open" opens the file directly, clicking the notification shows LDM.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from typing import Callable

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from .format import human
from .manager import Manager

log = logging.getLogger(__name__)

GUI_ID = "io.github.adiashishz.LDM"
GUI_PATH = "/io/github/adiashishz/LDM"
NOTIFICATIONS = ("org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications")
NOTIFY_STATES = {"done", "seeding", "error", "expired"}


def open_path(path: str) -> None:
    command = ["gio", "open", path] if shutil.which("gio") else ["xdg-open", path]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


class Notifier:
    def __init__(self, bus: MessageBus, manager: Manager, opener: Callable[[str], None] = open_path):
        self.bus = bus
        self.manager = manager
        self.opener = opener
        self._shown: dict[int, int] = {}   # notification id -> download id
        self._notified: set[int] = set()  # torrents announced when seeding started
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        for member in ("ActionInvoked", "NotificationClosed"):
            await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch",
                             "s", [f"type='signal',interface='{NOTIFICATIONS[2]}',member='{member}'"])
        self.bus.add_message_handler(self._on_message)
        previous = self.manager.on_state

        def on_state(download_id: int, state: str, error: str) -> None:
            if previous:
                previous(download_id, state, error)
            self.on_state(download_id, state, error)

        self.manager.on_state = on_state

    def on_state(self, download_id: int, state: str, error: str) -> None:
        if state not in NOTIFY_STATES or (state == "done" and download_id in self._notified):
            return
        if state == "seeding":
            self._notified.add(download_id)
        task = asyncio.get_running_loop().create_task(self._notify(download_id, state, error))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _notify(self, download_id: int, state: str, error: str) -> None:
        if not self.manager.store.get_settings()["notify_when_closed"]:
            return
        try:
            gui_running = (await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                            "NameHasOwner", "s", [GUI_ID])).body[0]
            if gui_running:
                return
            d = self.manager.store.get(download_id)
            name = d.name or d.url
            if state in ("done", "seeding"):
                summary, body = "Download finished", f"{name} — {human(d.size or 0)}"
                actions = ["default", "Show in LDM", "open", "Open"]
            elif state == "expired":
                summary, body, actions = "Link expired", f"{name}: set a new link in LDM", ["default", "Show in LDM"]
            else:
                summary, body, actions = "Download failed", f"{name}: {error}" if error else name, ["default", "Show in LDM"]
            reply = await self._call(*NOTIFICATIONS, "Notify", "susssasa{sv}i",
                                     ["LDM", 0, GUI_ID, summary, body, actions,
                                      {"desktop-entry": Variant("s", GUI_ID)}, -1])
            self._shown[reply.body[0]] = download_id
        except Exception as e:  # no notification server, download removed meanwhile, …
            log.info("could not show a notification: %s", e)

    def _on_message(self, msg: Message) -> None:
        if msg.message_type != MessageType.SIGNAL or msg.interface != NOTIFICATIONS[2]:
            return
        if msg.member == "NotificationClosed":
            self._shown.pop(msg.body[0], None)
        elif msg.member == "ActionInvoked" and msg.body[0] in self._shown:
            download_id = self._shown.pop(msg.body[0])
            if msg.body[1] == "open":
                try:
                    d = self.manager.store.get(download_id)
                except KeyError:
                    return
                self.opener(str(d.dest))
            else:
                task = asyncio.get_running_loop().create_task(self._show_gui())
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _show_gui(self) -> None:
        """Start (D-Bus activation) or raise the LDM window."""
        try:
            await self._call(GUI_ID, GUI_PATH, "org.freedesktop.Application", "ActivateAction", "sava{sv}",
                             ["show", [], {}])
        except Exception as e:
            log.info("could not show LDM: %s", e)

    async def _call(self, destination, path, interface, member, signature="", body=None) -> Message:
        reply = await self.bus.call(Message(destination=destination, path=path, interface=interface,
                                            member=member, signature=signature, body=body or []))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {reply.error_name}")
        return reply
