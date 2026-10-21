"""Progress while the window is closed (1DM's persistent notification): a tray icon, and in Flatpak the
GNOME "Background Apps" status line.

The tray icon is a StatusNotifierItem (KDE, Xfce, Cinnamon, Budgie, …; GNOME with the AppIndicator
extension) with a com.canonical.dbusmenu menu: Show LDM, Pause All, Resume All, Quit. Without a tray
host nothing is shown. The portal status line only works for sandboxed (Flatpak) apps.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable

from dbus_fast import Message, MessageType, PropertyAccess, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_property, method, signal

from .format import human
from .notify import GUI_ID, GUI_PATH

log = logging.getLogger(__name__)

WATCHER = ("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher")
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
PORTAL = ("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop", "org.freedesktop.portal.Background")
UPDATE_INTERVAL = 2.0


def summary_text(running: int, done: int, total: int, speed: float) -> str:
    if not running:
        return "No downloads running"
    parts = [f"{running} downloading"]
    if total:
        parts.append(f"{done * 100 // total}%")
    parts.append(f"{human(speed)}/s")
    return " · ".join(parts)


class StatusItem(ServiceInterface):
    def __init__(self, on_activate: Callable[[], None]):
        super().__init__("org.kde.StatusNotifierItem")
        self.on_activate = on_activate
        self.text = "No downloads running"
        self.active = False

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        return "ldm"

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        return "LDM"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "Active" if self.active else "Passive"

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        return GUI_ID

    @dbus_property(access=PropertyAccess.READ)
    def IconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> "(sa(iiay)ss)":
        return [GUI_ID, [], "LDM", self.text]

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        return MENU_PATH

    @method()
    def Activate(self, x: "i", y: "i"):
        self.on_activate()

    @method()
    def SecondaryActivate(self, x: "i", y: "i"):
        self.on_activate()

    @method()
    def ContextMenu(self, x: "i", y: "i"):
        pass

    @method()
    def Scroll(self, delta: "i", orientation: "s"):
        pass

    @signal()
    def NewToolTip(self):
        pass

    @signal()
    def NewStatus(self, status) -> "s":
        return status


class Menu(ServiceInterface):
    """com.canonical.dbusmenu with a fixed list of items."""

    def __init__(self, items: list[tuple[int, str, Callable[[], None] | None]]):
        super().__init__("com.canonical.dbusmenu")
        self.items = items  # (id, label, action); label "" = separator

    def _props(self, label: str) -> dict[str, Variant]:
        if not label:
            return {"type": Variant("s", "separator")}
        return {"label": Variant("s", label), "enabled": Variant("b", True), "visible": Variant("b", True)}

    @method()
    def GetLayout(self, parent_id: "i", recursion_depth: "i", property_names: "as") -> "u(ia{sv}av)":
        children = [Variant("(ia{sv}av)", [item_id, self._props(label), []]) for item_id, label, _ in self.items]
        if parent_id:
            children = []
        return [1, [parent_id, {"children-display": Variant("s", "submenu")}, children]]

    @method()
    def GetGroupProperties(self, ids: "ai", property_names: "as") -> "a(ia{sv})":
        return [[item_id, self._props(label)] for item_id, label, _ in self.items if not ids or item_id in ids]

    @method()
    def GetProperty(self, item_id: "i", name: "s") -> "v":
        props = next((self._props(label) for i, label, _ in self.items if i == item_id), {})
        return props.get(name, Variant("s", ""))

    @method()
    def Event(self, item_id: "i", event_id: "s", data: "v", timestamp: "u"):
        if event_id == "clicked":
            self._run(item_id)

    @method()
    def EventGroup(self, events: "a(isvu)") -> "ai":
        for item_id, event_id, _data, _timestamp in events:
            if event_id == "clicked":
                self._run(item_id)
        return []

    @method()
    def AboutToShow(self, item_id: "i") -> "b":
        return False

    @method()
    def AboutToShowGroup(self, ids: "ai") -> "aiai":
        return [[], []]

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> "s":
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "normal"

    @signal()
    def LayoutUpdated(self) -> "ui":
        return [1, 0]

    def _run(self, item_id: int) -> None:
        action = next((a for i, _, a in self.items if i == item_id), None)
        if action:
            action()


class Tray:
    """Keeps the tray icon (and the Flatpak background status) up to date with the downloads."""

    def __init__(self, bus: MessageBus, summary: Callable[[], tuple[int, int, int, float]],
                 pause_all: Callable[[], object], resume_all: Callable[[], object], quit: Callable[[], None],
                 enabled: Callable[[], bool] = lambda: True):
        self.bus, self.summary, self.enabled = bus, summary, enabled
        self.item = StatusItem(self.show)
        self.menu = Menu([(1, "Show LDM", self.show), (2, "", None), (3, "Pause All", pause_all),
                          (4, "Resume All", resume_all), (5, "", None), (6, "Quit LDM", quit)])
        self.sandboxed = bool(os.environ.get("FLATPAK_ID"))
        self._task: asyncio.Task | None = None
        self._last_status = ""

    async def start(self) -> None:
        self.bus.export(ITEM_PATH, self.item)
        self.bus.export(MENU_PATH, self.menu)
        await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch", "s",
                         [f"type='signal',interface='org.freedesktop.DBus',member='NameOwnerChanged',"
                          f"arg0='{WATCHER[0]}'"])
        self.bus.add_message_handler(self._on_message)
        await self._register()
        self._task = asyncio.get_running_loop().create_task(self._loop())

    def close(self) -> None:
        if self._task:
            self._task.cancel()

    def show(self) -> None:
        asyncio.get_running_loop().create_task(self._show())

    async def _show(self) -> None:
        try:
            await self._call(GUI_ID, GUI_PATH, "org.freedesktop.Application", "ActivateAction", "sava{sv}",
                             ["show", [], {}])
        except RuntimeError as e:
            log.info("could not show LDM: %s", e)

    async def _register(self) -> None:
        try:
            await self._call(*WATCHER, "RegisterStatusNotifierItem", "s", [self.bus.unique_name])
            log.info("tray icon registered")
        except RuntimeError:
            pass  # no tray on this desktop (e.g. GNOME without the AppIndicator extension)

    def _on_message(self, msg: Message) -> None:
        if (msg.message_type == MessageType.SIGNAL and msg.member == "NameOwnerChanged"
                and msg.body[0] == WATCHER[0] and msg.body[2]):
            asyncio.get_running_loop().create_task(self._register())  # a tray host appeared later

    async def _loop(self) -> None:
        while True:
            self.update()
            await asyncio.sleep(UPDATE_INTERVAL)

    def update(self) -> None:
        running, done, total, speed = self.summary() if self.enabled() else (0, 0, 0, 0.0)
        text = summary_text(running, done, total, speed)
        if text != self.item.text:
            self.item.text = text
            self.item.NewToolTip()
        if self.item.active != bool(running):
            self.item.active = bool(running)
            self.item.NewStatus("Active" if running else "Passive")
        status = text if running else ""
        if self.sandboxed and status != self._last_status:
            self._last_status = status
            asyncio.get_running_loop().create_task(self._portal_status(status))

    async def _portal_status(self, message: str) -> None:
        try:
            await self._call(*PORTAL, "SetStatus", "a{sv}", [{"message": Variant("s", message[:96])}])
        except RuntimeError as e:
            log.info("no background status: %s", e)

    async def _call(self, destination, path, interface, member, signature="", body=None) -> Message:
        reply = await self.bus.call(Message(destination=destination, path=path, interface=interface,
                                            member=member, signature=signature, body=body or []))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {reply.error_name}")
        return reply
