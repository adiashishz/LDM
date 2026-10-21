"""The ldm background service: owns all downloads and serves them on the session bus."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Any

from dbus_fast import NameFlag, RequestNameReply
from dbus_fast.aio import MessageBus

from .dbus_service import BUS_NAME, OBJECT_PATH, ManagerInterface
from .manager import Manager
from .finish import Finisher
from .notify import Notifier
from .tray import Tray
from .store import Store, default_db_path
from .integration import set_autostart
from .system import SleepInhibitor, SystemMonitor


class AlreadyRunning(Exception):
    pass


async def serve(*, bus_address: str | None = None, db_path: Path | None = None,
                stop: asyncio.Event | None = None, ready: asyncio.Event | None = None,
                manager_options: dict[str, Any] | None = None, system_bus_address: str | None = None,
                manage_autostart: bool = True) -> None:
    """Run until `stop` is set (default: SIGINT/SIGTERM). Running downloads resume on next start.

    `system_bus_address` replaces the system bus (NetworkManager, UPower, logind) — tests use a private one;
    `manage_autostart` lets the "autostart" setting enable/disable the systemd user unit."""
    monitor = SystemMonitor(system_bus_address)
    await monitor.start()
    manager = Manager(Store(db_path or default_db_path()), conditions=monitor, inhibitor=SleepInhibitor(monitor),
                      **(manager_options or {}))
    monitor.on_change = manager.conditions_changed
    if manage_autostart:
        manager.on_autostart = set_autostart
    bus = await MessageBus(bus_address=bus_address).connect()
    try:
        bus.export(OBJECT_PATH, ManagerInterface(manager))
        await Notifier(bus, manager).start()
        reply = await bus.request_name(BUS_NAME, NameFlag.DO_NOT_QUEUE)
        if reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
            raise AlreadyRunning(f"{BUS_NAME} is already owned on this bus")
        if stop is None:
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, stop.set)
        finisher = Finisher(bus, monitor, stop.set)  # "when all downloads finished"

        def quit_from_tray() -> None:
            manager.pause_all()
            stop.set()

        tray = Tray(bus, manager.summary, manager.pause_all, manager.resume_all, quit_from_tray,
                    enabled=lambda: manager.store.get_settings()["tray_icon"])
        manager.on_all_finished = finisher.run
        manager.on_started = finisher.cancel
        await manager.start()
        if manage_autostart:  # e.g. installed from a package: enable the unit the first time
            asyncio.get_running_loop().run_in_executor(None, set_autostart,
                                                       manager.store.get_settings()["autostart"])
        await tray.start()
        if ready:
            ready.set()
        stopped = asyncio.create_task(stop.wait())
        bus_lost = asyncio.create_task(bus.wait_for_disconnect())  # session ended: don't linger
        try:
            await asyncio.wait({stopped, bus_lost}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (stopped, bus_lost):
                task.cancel()
            tray.close()
            await manager.shutdown()
    finally:
        bus.disconnect()
        await monitor.close()
