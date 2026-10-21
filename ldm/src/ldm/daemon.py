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
from .store import Store, default_db_path


class AlreadyRunning(Exception):
    pass


async def serve(*, bus_address: str | None = None, db_path: Path | None = None,
                stop: asyncio.Event | None = None, ready: asyncio.Event | None = None,
                manager_options: dict[str, Any] | None = None) -> None:
    """Run until `stop` is set (default: SIGINT/SIGTERM). Running downloads resume on next start."""
    manager = Manager(Store(db_path or default_db_path()), **(manager_options or {}))
    bus = await MessageBus(bus_address=bus_address).connect()
    try:
        bus.export(OBJECT_PATH, ManagerInterface(manager))
        reply = await bus.request_name(BUS_NAME, NameFlag.DO_NOT_QUEUE)
        if reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
            raise AlreadyRunning(f"{BUS_NAME} is already owned on this bus")
        await manager.start()
        if stop is None:
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, stop.set)
        if ready:
            ready.set()
        stopped = asyncio.create_task(stop.wait())
        bus_lost = asyncio.create_task(bus.wait_for_disconnect())  # session ended: don't linger
        try:
            await asyncio.wait({stopped, bus_lost}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (stopped, bus_lost):
                task.cancel()
            await manager.shutdown()
    finally:
        bus.disconnect()
