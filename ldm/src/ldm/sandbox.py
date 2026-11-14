"""Running as a Flatpak: folder access, real folder names, and start at login through the portal.

Outside a Flatpak these helpers do nothing special, so callers use them unconditionally.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

APP_ID = "io.github.adiashishz.LDM"
PORTAL = ("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop")
BACKGROUND_REASON = "Keep downloading after the window is closed, and start the scheduled downloads at login"


def flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID")) or Path("/.flatpak-info").exists()


def host_path(path: str | os.PathLike) -> str:
    """The real location of a folder shared through the document portal (what the folder chooser returns
    inside a Flatpak): /run/user/1000/doc/1a2b3c4d/Videos -> /home/me/Videos. Other paths are unchanged."""
    text = str(path)
    if text.startswith("/run/user/") and "/doc/" in text:
        try:
            return os.getxattr(text, "user.document-portal.host-path").decode().rstrip("\0") or text
        except (OSError, AttributeError, UnicodeDecodeError):
            pass
    return text


def folder_problem(path: str | os.PathLike) -> str | None:
    """Why LDM can't save in `path`, or None when it can. Folders that don't exist yet are created later,
    so the nearest existing parent is what has to be writable."""
    folder = Path(path).expanduser()
    existing = folder
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    shown = host_path(folder)
    if existing.exists() and not existing.is_dir():
        return f"{host_path(existing)} is a file, not a folder"
    writable = os.access(existing, os.W_OK | os.X_OK)
    if writable and flatpak() and _sandbox_only(existing):
        writable = False  # it would land in the sandbox's own temporary space and vanish
    if writable:
        return None
    if flatpak():
        return (f"LDM has no access to {shown}. Choose the folder in LDM (Preferences or the Add dialog) to "
                "allow it, or give LDM access to it with Flatseal")
    return f"can't save in {shown}: permission denied"


def _sandbox_only(folder: Path) -> bool:
    """Inside a Flatpak, folders the app wasn't given still exist on the sandbox's own root (a temporary
    filesystem); shared folders are mounts from the real system, on another device."""
    try:
        return folder.stat().st_dev == Path("/").stat().st_dev
    except OSError:
        return False


# --- start at login -------------------------------------------------------------------------


def request_autostart(enabled: bool, timeout: float = 120) -> bool:
    """Ask the Background portal to start the download service at login (or to stop doing so).
    Returns whether the desktop agreed. The first time, the desktop may ask the user."""
    try:
        return asyncio.run(_request_background(enabled, timeout))
    except Exception:  # no portal, no session bus, timeout: start at login stays as it was
        return False


async def _request_background(autostart: bool, timeout: float) -> bool:
    from dbus_fast import Message, MessageType, Variant
    from dbus_fast.aio import MessageBus

    bus = await MessageBus().connect()
    try:
        token = f"ldm_autostart_{os.getpid()}"
        sender = bus.unique_name.lstrip(":").replace(".", "_")
        request = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        answer: asyncio.Future = asyncio.get_running_loop().create_future()

        def on_message(message: Message) -> None:
            if (message.message_type == MessageType.SIGNAL and message.path == request
                    and message.member == "Response" and not answer.done()):
                answer.set_result(message.body)

        bus.add_message_handler(on_message)
        # Listen before asking, so a quick answer isn't missed.
        await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                               interface="org.freedesktop.DBus", member="AddMatch", signature="s",
                               body=[f"type='signal',interface='org.freedesktop.portal.Request',"
                                     f"member='Response',path='{request}'"]))
        reply = await bus.call(Message(
            destination=PORTAL[0], path=PORTAL[1], interface="org.freedesktop.portal.Background",
            member="RequestBackground", signature="sa{sv}",
            body=["", {"handle_token": Variant("s", token), "reason": Variant("s", BACKGROUND_REASON),
                       "autostart": Variant("b", autostart), "commandline": Variant("as", ["ldm", "daemon"]),
                       "dbus-activatable": Variant("b", False)}]))
        if reply.message_type == MessageType.ERROR:
            return False
        response, results = await asyncio.wait_for(answer, timeout)
        granted = results.get("autostart")
        return response == 0 and granted is not None and granted.value == autostart
    finally:
        bus.disconnect()
