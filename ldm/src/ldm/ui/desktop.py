"""The window on the session bus (Linux), as GTK's GApplication offered it, so the rest of LDM can reach it.

The window owns `io.github.adiashishz.LDM` and answers at /io/github/adiashishz/LDM:
- org.freedesktop.Application: Activate, Open (the .desktop file is D-Bus activatable) and ActivateAction;
- org.gtk.Actions: Activate (the browser bridge calls it with `gdbus`).
Actions: `add-request` (JSON from the browser or the clipboard watcher), `show`, `open-file` (a download id)
and `quit`. Owning the name also tells the service that the window is open, so the service leaves the
notifications to it.

Annotations are D-Bus signature strings read by dbus-fast, so this module must not use
`from __future__ import annotations`.
"""

import json
import logging
from typing import Any, Callable

from dbus_fast import Message, MessageType, NameFlag, RequestNameReply, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

log = logging.getLogger(__name__)

GUI_ID = "io.github.adiashishz.LDM"
GUI_PATH = "/io/github/adiashishz/LDM"
NOTIFICATIONS = ("org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications")
ACTIONS = ("add-request", "show", "open-file", "quit")

# handler(name, arguments, platform data): name is "activate", "open" (arguments: the URIs) or an action
Handler = Callable[[str, list, dict], None]


def _plain(platform_data: dict) -> dict:
    return {k: v.value for k, v in platform_data.items()}


class Application(ServiceInterface):
    def __init__(self, handler: Handler):
        super().__init__("org.freedesktop.Application")
        self.handler = handler

    @method()
    def Activate(self, platform_data: "a{sv}"):  # noqa: N802 (D-Bus name)
        self.handler("activate", [], _plain(platform_data))

    @method()
    def Open(self, uris: "as", hint: "s", platform_data: "a{sv}"):  # noqa: N802
        self.handler("open", list(uris), _plain(platform_data))

    @method()
    def ActivateAction(self, name: "s", parameter: "av", platform_data: "a{sv}"):  # noqa: N802
        self.handler(name, [v.value for v in parameter], _plain(platform_data))


class Actions(ServiceInterface):
    """The part of GTK's org.gtk.Actions that callers use: Activate, and the list for introspecting tools."""

    def __init__(self, handler: Handler):
        super().__init__("org.gtk.Actions")
        self.handler = handler

    @method()
    def Activate(self, name: "s", parameter: "av", platform_data: "a{sv}"):  # noqa: N802
        self.handler(name, [v.value for v in parameter], _plain(platform_data))

    @method()
    def List(self) -> "as":  # noqa: N802
        return list(ACTIONS)


async def claim(bus: MessageBus, handler: Handler) -> bool:
    """Offer the window's interfaces and take its bus name. False: another LDM window has it already."""
    bus.export(GUI_PATH, Application(handler))
    bus.export(GUI_PATH, Actions(handler))
    reply = await bus.request_name(GUI_ID, NameFlag.DO_NOT_QUEUE)
    if reply in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
        return True
    bus.unexport(GUI_PATH)
    return False


async def forward(bus: MessageBus, uris: list[str], platform_data: dict | None = None) -> None:
    """Hand this start's links (or just "come to the front") to the window that is already open."""
    data = {k: Variant("s", v) for k, v in (platform_data or {}).items()}
    if uris:
        message = Message(destination=GUI_ID, path=GUI_PATH, interface="org.freedesktop.Application",
                          member="Open", signature="assa{sv}", body=[uris, "", data])
    else:
        message = Message(destination=GUI_ID, path=GUI_PATH, interface="org.freedesktop.Application",
                          member="Activate", signature="a{sv}", body=[data])
    reply = await bus.call(message)
    if reply.message_type == MessageType.ERROR:
        raise ConnectionError(f"{reply.error_name}: {reply.body[0] if reply.body else ''}")


class Notifier:
    """Desktop notifications from the window (the service stays quiet while the window owns its name).

    `on_action(key, action)` runs when the user clicks one: `action` is "default" or a button's id.
    """

    def __init__(self, on_action: Callable[[Any, str], None]):
        self.on_action = on_action
        self.shown: dict[int, Any] = {}  # notification id -> the caller's key (a download id)
        self._watching = False

    async def notify(self, bus: MessageBus, key: Any, summary: str, body: str, actions: list[str]) -> None:
        if not self._watching:
            self._watching = True
            bus.add_message_handler(self._on_message)
            await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                   interface="org.freedesktop.DBus", member="AddMatch", signature="s",
                                   body=[f"type='signal',interface='{NOTIFICATIONS[2]}'"]))
        reply = await bus.call(Message(destination=NOTIFICATIONS[0], path=NOTIFICATIONS[1],
                                       interface=NOTIFICATIONS[2], member="Notify", signature="susssasa{sv}i",
                                       body=["LDM", 0, GUI_ID, summary, body, actions,
                                             {"desktop-entry": Variant("s", GUI_ID)}, -1]))
        if reply.message_type == MessageType.METHOD_RETURN:
            self.shown[reply.body[0]] = key
        else:
            log.info("could not show a notification: %s", reply.error_name)

    def _on_message(self, message: Message) -> None:
        if message.message_type != MessageType.SIGNAL or message.interface != NOTIFICATIONS[2]:
            return
        if message.member == "NotificationClosed":
            self.shown.pop(message.body[0], None)
        elif message.member == "ActionInvoked" and message.body[0] in self.shown:
            self.on_action(self.shown.pop(message.body[0]), message.body[1])


def add_request(arguments: list) -> dict | None:
    """The request in an `add-request` action: {"url", "filename", "headers", "kind", "captures"}."""
    try:
        request = json.loads(arguments[0]) if arguments else None
    except (TypeError, ValueError):
        return None
    return request if isinstance(request, dict) and request.get("url") else None
