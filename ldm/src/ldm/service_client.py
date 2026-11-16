"""The service for the command line: its methods with plain Python values, over D-Bus or the local channel.

    client = await connect_service()        # D-Bus on Linux, the socket elsewhere (LDM_TRANSPORT overrides)
    download_id = await client.call("Add", url, {"dir": "/tmp"})
    client.on("StateChanged", lambda download_id, state, error: ...)
    await client.close()

Errors are ServiceError with the names the service uses ("NotFound", "InvalidArgument", …).
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

from . import channel

NO_OWNER = ("org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NameHasNoOwner")
VARDICT_PARAMS = {"Add": 1, "SetSettings": 0}  # parameters D-Bus carries as a{sv}


class DaemonNotRunning(Exception):
    pass


class ServiceError(Exception):
    def __init__(self, name: str, message: str):
        super().__init__(message)
        self.name, self.message = name, message


def default_transport() -> str:
    return os.environ.get("LDM_TRANSPORT") or ("dbus" if sys.platform.startswith("linux") else "socket")


async def connect_service(transport: str | None = None, address: str | None = None) -> ServiceClient:
    """Connect to the running service; raises DaemonNotRunning when it isn't."""
    client = SocketClient(address) if (transport or default_transport()) == "socket" else DBusClient(address)
    await client.open()
    return client


class ServiceClient:
    async def open(self) -> None:
        raise NotImplementedError

    async def call(self, method: str, *params: Any) -> Any:
        raise NotImplementedError

    def on(self, signal: str, handler: Callable[..., None]) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


def _snake(name: str) -> str:
    """dbus-fast's names for a method: "UpdateYtDlp" -> "update_yt_dlp"."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


class DBusClient(ServiceClient):
    def __init__(self, address: str | None = None):
        self.address = address
        self.bus = self.interface = None

    async def open(self) -> None:
        from dbus_fast import DBusError
        from dbus_fast.aio import MessageBus

        from .busnames import BUS_NAME, INTERFACE, OBJECT_PATH

        try:
            self.bus = await MessageBus(bus_address=self.address).connect()
        except (OSError, ValueError) as e:
            raise DaemonNotRunning(f"no session bus: {e}") from e
        try:
            introspection = await self.bus.introspect(BUS_NAME, OBJECT_PATH)
        except DBusError as e:
            self.bus.disconnect()
            if e.type in NO_OWNER:
                raise DaemonNotRunning from e
            raise
        self.interface = self.bus.get_proxy_object(BUS_NAME, OBJECT_PATH, introspection).get_interface(INTERFACE)

    async def call(self, method: str, *params: Any) -> Any:
        from dbus_fast import DBusError

        from .busnames import ERROR_PREFIX
        from .variants import to_vardict, unwrap

        params = list(params)
        if method in VARDICT_PARAMS:
            index = VARDICT_PARAMS[method]
            params[index] = to_vardict(params[index])
        try:
            return unwrap(await getattr(self.interface, "call_" + _snake(method))(*params))
        except DBusError as e:
            raise ServiceError(e.type.removeprefix(ERROR_PREFIX), e.text) from e

    def on(self, signal: str, handler: Callable[..., None]) -> None:
        from .variants import unwrap

        getattr(self.interface, "on_" + _snake(signal))(lambda *args: handler(*unwrap(list(args))))

    async def close(self) -> None:
        if self.bus is not None:
            self.bus.disconnect()


class SocketClient(ServiceClient):
    def __init__(self, address: str | None = None):
        self.path = Path(address) if address else (channel.tcp_file() if channel.TCP else channel.socket_path())
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.handlers: dict[str, list[Callable[..., None]]] = {}
        self.next_id = 1
        self.task: asyncio.Task | None = None

    async def open(self) -> None:
        try:
            if channel.TCP:
                port, token = channel.read_tcp_file(self.path)
                self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port, limit=channel.MAX_LINE)
                self.writer.write(channel.encode({"hello": token}))
            else:
                self.reader, self.writer = await asyncio.open_unix_connection(str(self.path), limit=channel.MAX_LINE)
        except (OSError, ValueError, KeyError) as e:
            raise DaemonNotRunning(str(e)) from e
        self.task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            while line := await self.reader.readline():
                message = channel.decode(line)
                if "signal" in message:
                    for handler in self.handlers.get(message["signal"], []):
                        handler(*(message.get("args") or []))
                elif (future := self.pending.pop(message.get("id"), None)) is not None and not future.done():
                    future.set_result(message)
        except (ConnectionError, ValueError, asyncio.LimitOverrunError):
            pass
        for future in self.pending.values():
            if not future.done():
                future.set_exception(DaemonNotRunning("the service went away"))

    async def call(self, method: str, *params: Any) -> Any:
        call_id, self.next_id = self.next_id, self.next_id + 1
        future = asyncio.get_running_loop().create_future()
        self.pending[call_id] = future
        self.writer.write(channel.encode({"id": call_id, "method": method, "params": list(params)}))
        reply = await future
        if "error" in reply:
            raise ServiceError(reply["error"].get("name", "Failed"), reply["error"].get("message", ""))
        return reply.get("result")

    def on(self, signal: str, handler: Callable[..., None]) -> None:
        self.handlers.setdefault(signal, []).append(handler)

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
        if self.task is not None:
            self.task.cancel()
