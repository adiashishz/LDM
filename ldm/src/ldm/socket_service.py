"""The service on the local channel (ldm.channel): the same methods and signals as on D-Bus, for systems
without a session bus (Windows, macOS) and, next to D-Bus, on Linux.

Three more methods, for what D-Bus does by the window's own bus name:
    RegisterWindow()          the caller is LDM's window: it gets the AddRequest signal
    WindowOpen() -> bool      whether a window is connected (the browser bridge's "ping")
    ShowAddDialog(request) -> int   sends {"signal": "AddRequest", "args": [request]} to the windows;
                                    returns how many got it (0: start the window, then ask again)
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
from pathlib import Path

from . import channel
from .api import Api, error_name

log = logging.getLogger(__name__)

WINDOW_METHODS = {"RegisterWindow", "WindowOpen", "ShowAddDialog"}


class AlreadyListening(Exception):
    """Another service answers on the channel already."""


class SocketService:
    def __init__(self, api: Api, path: Path | None = None):
        self.api = api
        self.tcp = channel.TCP
        self.path = path or (channel.tcp_file() if self.tcp else channel.socket_path())
        self.token = ""
        self.server: asyncio.base_events.Server | None = None
        self.clients: set[asyncio.StreamWriter] = set()      # connected (on TCP: after the token)
        self.connections: set[asyncio.StreamWriter] = set()  # every open connection, to close on stop
        self.windows: set[asyncio.StreamWriter] = set()      # connections of LDM's window (RegisterWindow)
        api.listeners.append(self._signal)

    async def start(self) -> None:
        if await self._answering():
            raise AlreadyListening(f"another LDM service answers at {self.path}")
        channel.private_folder(self.path.parent)
        if self.tcp:
            self.server = await asyncio.start_server(self._client, "127.0.0.1", 0, limit=channel.MAX_LINE)
            port = self.server.sockets[0].getsockname()[1]
            self.token = channel.write_tcp_file(self.path, port)
        else:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()  # left by a service that didn't stop cleanly (nothing answered above)
            old = os.umask(0o177)  # the socket is created readable and writable by the user only
            try:
                self.server = await asyncio.start_unix_server(self._client, str(self.path), limit=channel.MAX_LINE)
            finally:
                os.umask(old)
        log.info("listening at %s", self.path)

    async def close(self) -> None:
        if self.api is not None and self._signal in self.api.listeners:
            self.api.listeners.remove(self._signal)
        if self.server is not None:
            self.server.close()
            for writer in list(self.connections):
                writer.close()
            for writer in list(self.connections):  # wait, so no connection outlives the server
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), 2)
            await self.server.wait_closed()
            self.server = None
            with contextlib.suppress(OSError):
                self.path.unlink()

    async def _answering(self) -> bool:
        """Whether a service already listens at our address (a stale socket or file doesn't count)."""
        try:
            if self.tcp:
                port, _ = channel.read_tcp_file(self.path)
                _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 2)
            else:
                _, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self.path)), 2)
        except (OSError, ValueError, KeyError, asyncio.TimeoutError):
            return False
        writer.close()
        return True

    # --- clients --------------------------------------------------------------------------------

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tasks: set[asyncio.Task] = set()
        self.connections.add(writer)
        try:
            if self.tcp and not await self._hello(reader):
                return
            self.clients.add(writer)
            while line := await reader.readline():
                try:
                    message = channel.decode(line)
                except ValueError:
                    log.info("ignored a message that isn't JSON")
                    continue
                task = asyncio.create_task(self._call(message, writer))  # long calls don't hold up others
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except (ConnectionError, asyncio.LimitOverrunError, ValueError) as e:
            log.info("a client's connection ended: %s", e)
        finally:
            self.clients.discard(writer)
            self.connections.discard(writer)
            self.windows.discard(writer)
            for task in tasks:
                task.cancel()
            writer.close()

    async def _hello(self, reader: asyncio.StreamReader) -> bool:
        """TCP only: the first message must carry the token from the user's own file."""
        try:
            hello = channel.decode(await asyncio.wait_for(reader.readline(), 10))
        except (ValueError, asyncio.TimeoutError):
            return False
        return isinstance(hello.get("hello"), str) and hmac.compare_digest(hello["hello"], self.token)

    async def _call(self, message: dict, writer: asyncio.StreamWriter) -> None:
        call_id = message.get("id")
        method, params = message.get("method"), message.get("params") or []
        try:
            if not isinstance(method, str) or not isinstance(params, list):
                raise KeyError(method)
            if method in WINDOW_METHODS:
                result = self._window_call(method, params, writer)
            else:
                result = await self.api.call(method, params)
            reply = {"id": call_id, "result": result}
        except KeyError as e:
            reply = {"id": call_id, "error": {"name": "UnknownMethod", "message": f"no method {e.args[0]!r}"}}
        except Exception as e:  # noqa: BLE001 (every error goes back to the caller)
            name = error_name(e)
            if name is None:
                log.exception("%s failed", method)
            reply = {"id": call_id, "error": {"name": name or "Failed", "message": str(e)}}
        self._send(writer, reply)

    def _window_call(self, method: str, params: list, writer: asyncio.StreamWriter) -> object:
        if method == "RegisterWindow":
            self.windows.add(writer)
            return True
        if method == "WindowOpen":
            return bool(self.windows)
        request = params[0] if params else None
        if not isinstance(request, dict) or not isinstance(request.get("url"), str):
            raise ValueError("ShowAddDialog needs a request with a url")
        for window in list(self.windows):
            self._send(window, {"signal": "AddRequest", "args": [request]})
        return len(self.windows)

    def _signal(self, name: str, args: list) -> None:
        message = {"signal": name, "args": args}
        for writer in list(self.clients):
            self._send(writer, message)

    def _send(self, writer: asyncio.StreamWriter, message: dict) -> None:
        if writer.is_closing():
            return
        try:
            writer.write(channel.encode(message))
        except (ConnectionError, TypeError) as e:
            log.info("could not send to a client: %s", e)

