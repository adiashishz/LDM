"""Copied links (1DM's clipboard monitor / "smart download").

The window offers a copied link when it gets focus (works everywhere). `ldm clipboard-watch` watches in
the background where the desktop allows it: wlroots/KDE Wayland via `wl-paste --watch` (data-control
protocol), X11 by polling `xclip`. GNOME on Wayland has no way to watch the clipboard from the background.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections.abc import AsyncIterator, Iterable
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

log = logging.getLogger(__name__)

SCHEMES = ("http", "https", "ftp", "magnet")
MAX_LINK = 8192


def candidate(text: str | None, exclude: str = "") -> str | None:
    """The link in `text` if it is a single downloadable link not excluded by extension, else None."""
    text = (text or "").strip()
    if not text or len(text) > MAX_LINK or any(c.isspace() for c in text):
        return None
    parts = urlsplit(text)
    if parts.scheme.lower() not in SCHEMES:
        return None
    if parts.scheme.lower() == "magnet":
        return text if "xt=" in parts.query else None
    if not parts.hostname:
        return None
    suffix = PurePosixPath(unquote(parts.path)).suffix.lstrip(".").lower()
    excluded = {e.strip().lstrip(".").lower() for e in exclude.split(",") if e.strip()}
    return None if suffix and suffix in excluded else text


class Offer:
    """Asks about each copied link once, and never about links already in the list."""

    def __init__(self):
        self.last: str | None = None

    def check(self, text: str | None, known: Iterable[str], exclude: str = "") -> str | None:
        link = candidate(text, exclude)
        if link is None or link == self.last or link in set(known):
            return None
        self.last = link
        return link


def background_method() -> str | None:
    """"wayland" (wl-paste --watch), "x11" (xclip polling), or None when not possible here."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if os.environ.get("WAYLAND_DISPLAY"):
        if "gnome" in desktop:  # Mutter has no data-control protocol
            return None
        return "wayland" if shutil.which("wl-paste") else None
    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        return "x11"
    return None


async def watch(method: str, poll_interval: float = 1.0) -> AsyncIterator[str]:
    """Yield the clipboard text every time it changes."""
    if method == "wayland":
        # wl-paste runs the command with the new contents on stdin; NUL separates entries.
        proc = await asyncio.create_subprocess_exec(
            "wl-paste", "--no-newline", "--type", "text", "--watch", "sh", "-c", "cat; printf '\\0'",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        buffer, first = b"", True
        try:
            while chunk := await proc.stdout.read(65536):
                buffer += chunk
                *entries, buffer = buffer.split(b"\0")
                for entry in entries:
                    if not first:  # the first entry is what was copied before we started
                        yield entry.decode(errors="replace")
                    first = False
        finally:
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()
        return
    previous = None
    while True:
        proc = await asyncio.create_subprocess_exec(
            "xclip", "-o", "-selection", "clipboard", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        text = out.decode(errors="replace") if proc.returncode == 0 else None
        if text is not None and text != previous:
            if previous is not None:  # don't offer whatever was copied before we started
                yield text
            previous = text
        await asyncio.sleep(poll_interval)


async def run_watcher(bus_address: str | None = None, method: str | None = None,
                      texts: AsyncIterator[str] | None = None) -> int:
    """`ldm clipboard-watch`: offer copied links with a notification; "Download" opens LDM's add dialog."""
    from dbus_fast import Message, MessageFlag, MessageType, Variant
    from dbus_fast.aio import MessageBus

    from .busnames import BUS_NAME, INTERFACE, OBJECT_PATH
    from .notify import GUI_ID, GUI_PATH, NOTIFICATIONS
    from .store import DEFAULT_SETTINGS

    method = method or background_method()
    if texts is None and method is None:
        log.warning("this desktop doesn't allow watching the clipboard in the background; "
                    "LDM offers copied links when its window gets focus")
        return 0
    bus = await MessageBus(bus_address=bus_address).connect()
    offers: dict[int, str] = {}  # notification id -> link

    async def call(destination, path, interface, member, signature="", body=None, auto_start=True):
        reply = await bus.call(Message(destination=destination, path=path, interface=interface, member=member,
                                       signature=signature, body=body or [],
                                       flags=MessageFlag.NONE if auto_start else MessageFlag.NO_AUTOSTART))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {reply.error_name}")
        return reply.body

    async def daemon_state() -> tuple[dict, list[str]]:
        """Settings and known links; defaults when the service isn't running (don't start it for this)."""
        try:
            settings = {k: v.value for k, v in (await call(BUS_NAME, OBJECT_PATH, INTERFACE, "GetSettings",
                                                           auto_start=False))[0].items()}
            rows = (await call(BUS_NAME, OBJECT_PATH, INTERFACE, "List", auto_start=False))[0]
            return settings, [row["url"].value for row in rows]
        except RuntimeError:
            return dict(DEFAULT_SETTINGS), []

    def on_message(msg: Message) -> None:
        if (msg.message_type == MessageType.SIGNAL and msg.interface == NOTIFICATIONS[2]
                and msg.member == "ActionInvoked" and msg.body[0] in offers):
            link = offers.pop(msg.body[0])  # clicking the notification or its button: both open the dialog
            request = json.dumps({"url": link, "kind": "torrent" if link.startswith("magnet:") else ""})
            asyncio.get_running_loop().create_task(
                call(GUI_ID, GUI_PATH, "org.freedesktop.Application", "ActivateAction", "sava{sv}",
                     ["add-request", [Variant("s", request)], {}]))

    await call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch", "s",
               [f"type='signal',interface='{NOTIFICATIONS[2]}',member='ActionInvoked'"])
    bus.add_message_handler(on_message)
    offer = Offer()
    try:
        async for text in texts if texts is not None else watch(method):
            settings, known = await daemon_state()
            if not settings["clipboard_watch"]:
                continue
            link = offer.check(text, known, settings["clipboard_exclude"])
            if link is None:
                continue
            try:
                (notification_id,) = await call(*NOTIFICATIONS, "Notify", "susssasa{sv}i",
                                                ["LDM", 0, GUI_ID, "Download copied link?", link,
                                                 ["default", "Download", "download", "Download"],
                                                 {"desktop-entry": Variant("s", GUI_ID)},
                                                 -1])
                offers[notification_id] = link
            except RuntimeError as e:
                log.info("could not offer %s: %s", link, e)
    finally:
        bus.disconnect()
    return 0
