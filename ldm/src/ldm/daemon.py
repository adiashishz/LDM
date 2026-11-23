"""The ldm background service: owns all downloads and serves them on the local channel and, on Linux, the
session bus."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Any

from .api import Api
from .socket_service import AlreadyListening, SocketService
from .manager import Manager
from .finish import LocalFinisher
from .store import Store, default_db_path
from .integration import set_autostart
from .conditions import Conditions

log = logging.getLogger(__name__)


class AlreadyRunning(Exception):
    pass


async def serve(*, bus_address: str | None = None, db_path: Path | None = None,
                stop: asyncio.Event | None = None, ready: asyncio.Event | None = None,
                manager_options: dict[str, Any] | None = None, system_bus_address: str | None = None,
                manage_autostart: bool = True, socket_path: Path | None = None, dbus: bool | None = None,
                desktop: bool = True) -> bool:
    """Run until `stop` is set (default: SIGINT/SIGTERM). Running downloads resume on next start.

    `dbus`: also serve on the session bus, with the Linux desktop's notifications, tray icon, network and
    battery conditions and power actions (default: on Linux); without it, only the local channel, and on
    Windows its own tray icon, notifications, battery and power actions (ldm.winsys, ldm.wintray).
    `system_bus_address` replaces the system bus (NetworkManager, UPower, logind) — tests use a private one;
    `manage_autostart` lets the "autostart" setting start the service at login or not (Linux: the systemd
    user unit; Windows: the Run key);
    `desktop=False` leaves out Windows' tray icon, notifications and power actions (tests);
    `socket_path` is where the local channel listens (default: ldm.channel's place for this user).
    Returns True when the service should start again (to load a newly fetched yt-dlp)."""
    if dbus is None:
        dbus = sys.platform.startswith("linux")
    windows = not dbus and sys.platform == "win32"
    if dbus:
        from .system import SleepInhibitor, SystemMonitor

        monitor = SystemMonitor(system_bus_address)
        await monitor.start()
        inhibitor = SleepInhibitor(monitor)
    elif windows:
        from .winsys import KeepAwake, PowerMonitor

        monitor = PowerMonitor()
        await monitor.start()
        inhibitor = KeepAwake()
    else:
        monitor, inhibitor = Conditions(), None
    manager = Manager(Store(db_path or default_db_path()), conditions=monitor, inhibitor=inhibitor,
                      **(manager_options or {}))
    if dbus or windows:
        monitor.on_change = manager.conditions_changed
    # systemd or the Flatpak portal; Windows' Run key
    manage_autostart = manage_autostart and (sys.platform.startswith("linux") or sys.platform == "win32")
    if manage_autostart:
        manager.on_autostart = set_autostart
    api = Api(manager)
    listener = SocketService(api, socket_path)
    bus = None
    try:
        if dbus:
            bus = await _serve_on_bus(bus_address, api, manager)
            # The same methods on the local channel too (what Windows and macOS have instead of D-Bus).
            try:
                await listener.start()
            except (AlreadyListening, OSError) as e:
                log.warning("the local channel is not available (D-Bus still is): %s", e)
        else:
            try:
                await listener.start()
            except AlreadyListening as e:
                raise AlreadyRunning(str(e)) from e
        if stop is None:
            stop = asyncio.Event()
            _stop_on_signals(stop)
        restart = False

        def request_restart() -> None:
            nonlocal restart
            restart = True
            stop.set()

        manager.on_restart = request_restart

        def quit_from_tray() -> None:
            manager.pause_all()
            stop.set()

        tray = None

        def tray_enabled() -> bool:
            return manager.store.get_settings()["tray_icon"]

        if bus is not None:
            from .finish import Finisher
            from .tray import Tray

            finisher = Finisher(bus, monitor, stop.set)  # "when all downloads finished"
            tray = Tray(bus, manager.summary, manager.pause_all, manager.resume_all, quit_from_tray,
                        enabled=tray_enabled)
        elif windows and desktop:
            from .winsys import open_window, power
            from .wintray import WindowsTray, notify_on_state

            def show_window() -> None:
                if not listener.tell_windows("Show"):
                    open_window()

            def quit_everything() -> None:  # the tray is how Windows users quit LDM: the window goes too
                listener.tell_windows("Quit")
                quit_from_tray()

            tray = WindowsTray(manager.summary, show_window, manager.pause_all, manager.resume_all,
                               quit_everything, enabled=tray_enabled)
            notify_on_state(manager, tray)
            finisher = LocalFinisher(lambda: listener.tell_windows("Quit"), stop.set, power,
                                     announce=tray.countdown)
            tray.on_cancel = finisher.cancel
        else:
            finisher = LocalFinisher(lambda: listener.tell_windows("Quit"), stop.set)
        manager.on_all_finished = finisher.run
        manager.on_started = finisher.cancel
        await manager.start()
        if manage_autostart:  # e.g. installed from a package: enable the unit the first time
            asyncio.get_running_loop().run_in_executor(None, set_autostart,
                                                       manager.store.get_settings()["autostart"])
        if tray is not None:
            await tray.start()
        if ready:
            ready.set()
        waits = {asyncio.create_task(stop.wait())}
        if bus is not None:
            waits.add(asyncio.create_task(bus.wait_for_disconnect()))  # session ended: don't linger
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waits:
                task.cancel()
            if tray is not None:
                tray.close()
            await listener.close()
            await manager.shutdown()
        return restart
    finally:
        if bus is not None:
            bus.disconnect()
        await monitor.close()


async def _serve_on_bus(bus_address: str | None, api: Api, manager: Manager):
    """Export the service on the session bus and take its name; returns the connection."""
    from dbus_fast import NameFlag, RequestNameReply
    from dbus_fast.aio import MessageBus

    from .dbus_service import BUS_NAME, OBJECT_PATH, ManagerInterface
    from .notify import Notifier

    bus = await MessageBus(bus_address=bus_address).connect()
    try:
        bus.export(OBJECT_PATH, ManagerInterface(api))
        await Notifier(bus, manager).start()
        reply = await bus.request_name(BUS_NAME, NameFlag.DO_NOT_QUEUE)
        if reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
            raise AlreadyRunning(f"{BUS_NAME} is already owned on this bus")
    except BaseException:
        bus.disconnect()
        raise
    return bus


def _stop_on_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:  # Windows: a plain handler, handing over to the loop
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
