"""The service's local channel (ldm.channel, ldm.socket_service): messages, errors, privacy, one service only,
and the token on Windows' TCP channel (tried here on Linux too)."""

import asyncio
import os
import random
import stat
import sys

import pytest

from conftest import HAS_DBUS
from ldm import channel
from ldm.api import METHODS, Api
from ldm.manager import Manager
from ldm.passwords import MemoryPasswords
from ldm.socket_service import AlreadyListening, SocketService
from ldm.store import Store


unix_only = pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets (Windows uses TCP)")


def run(coroutine):
    return asyncio.run(asyncio.wait_for(coroutine, 20))


def new_api(tmp_path) -> Api:
    store = Store(tmp_path / "state.db")
    store.set_setting("download_dir", str(tmp_path / "dl"))
    return Api(Manager(store, passwords=MemoryPasswords()))


async def ask(reader, writer, **message):
    writer.write(channel.encode(message))
    return channel.decode(await reader.readline())


def test_codec_keeps_bytes():
    message = {"id": 1, "result": {"metadata": b"\x00\xffd4:infoe", "files": (1, 2)}}
    assert channel.decode(channel.encode(message)) == {"id": 1, "result": {"metadata": b"\x00\xffd4:infoe",
                                                                           "files": [1, 2]}}
    with pytest.raises(ValueError):
        channel.decode(b"[1, 2]")


@pytest.mark.skipif(not HAS_DBUS, reason="needs dbus-fast (Linux)")
def test_both_channels_offer_the_same_methods():
    from ldm.api import SIGNALS
    from ldm.dbus_service import ManagerInterface

    dbus_methods = {name for name in vars(ManagerInterface) if name[0].isupper()} - set(SIGNALS)
    assert dbus_methods == METHODS


@unix_only
def test_calls_errors_and_privacy(tmp_path):
    path = tmp_path / "rt" / "ldm" / "service.sock"

    async def scenario():
        service = SocketService(new_api(tmp_path), path)
        await service.start()
        reader, writer = await asyncio.open_unix_connection(str(path))
        settings = await ask(reader, writer, id=1, method="GetSettings", params=[])
        missing = await ask(reader, writer, id=2, method="Pause", params=[99])
        unknown = await ask(reader, writer, id=3, method="_emit", params=["x", []])
        writer.write(b"not json\n")
        listed = await ask(reader, writer, id=4, method="List", params=[])
        writer.close()
        await service.close()
        return settings, missing, unknown, listed

    settings, missing, unknown, listed = run(scenario())
    assert settings["id"] == 1 and settings["result"]["max_parallel"] == 3
    assert missing == {"id": 2, "error": {"name": "NotFound", "message": "no download with id 99"}}
    assert unknown["error"]["name"] == "UnknownMethod"  # only the service's methods, nothing internal
    assert listed == {"id": 4, "result": []}  # a broken line doesn't end the connection
    assert not path.exists()  # removed when the service stops


@unix_only
def test_socket_is_private_and_one_service_only(tmp_path):
    path = tmp_path / "rt" / "ldm" / "service.sock"

    async def scenario():
        first = SocketService(new_api(tmp_path), path)
        await first.start()
        modes = stat.S_IMODE(os.stat(path.parent).st_mode), stat.S_IMODE(os.stat(path).st_mode)
        second = SocketService(new_api(tmp_path), path)
        with pytest.raises(AlreadyListening):
            await second.start()
        await first.close()
        path.write_text("")  # a stale file, as a crash leaves: replaced
        third = SocketService(new_api(tmp_path), path)
        await third.start()
        await third.close()
        return modes

    folder_mode, socket_mode = run(scenario())
    assert folder_mode == 0o700 and socket_mode & 0o077 == 0


def test_tcp_channel_needs_the_token(tmp_path, monkeypatch):
    """Windows: 127.0.0.1 and a token from a file only the user can read."""
    monkeypatch.setattr(channel, "TCP", True)
    path = tmp_path / "rt" / "service.json"

    async def scenario():
        service = SocketService(new_api(tmp_path), path)
        await service.start()
        port, token = channel.read_tcp_file(path)
        file_mode = stat.S_IMODE(os.stat(path).st_mode)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(channel.encode({"hello": "wrong"}))
        writer.write(channel.encode({"id": 1, "method": "List", "params": []}))
        refused = await reader.read()  # closed without an answer
        writer.close()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(channel.encode({"hello": token}))
        listed = await ask(reader, writer, id=1, method="List", params=[])
        writer.close()
        await service.close()
        return refused, listed, file_mode

    refused, listed, file_mode = run(scenario())
    assert refused == b"" and listed == {"id": 1, "result": []}
    if sys.platform != "win32":  # (Windows: LocalAppData is the user's own; modes don't apply)
        assert file_mode == 0o600  # the token: readable by the user only
    assert not path.exists()  # removed when the service stops


def test_service_without_dbus(tmp_path, http_server):
    """Windows and macOS: the service on the local channel only. A second one refuses to start, and when
    all downloads finished with "quit" chosen, the window is told to close and the service stops."""
    from ldm.daemon import AlreadyRunning, serve
    from ldm.service_client import connect_service

    data = random.Random(21).randbytes(80_000)
    cfg, url = http_server(data=data)
    options = {"passwords": MemoryPasswords()}

    async def scenario():
        ready = asyncio.Event()
        service = asyncio.create_task(serve(db_path=tmp_path / "a.db", stop=asyncio.Event(), ready=ready,
                                            manager_options=options, manage_autostart=False, dbus=False,
                                            desktop=False))
        await ready.wait()
        with pytest.raises(AlreadyRunning):
            await serve(db_path=tmp_path / "b.db", stop=asyncio.Event(), manager_options=options,
                        manage_autostart=False, dbus=False, desktop=False)
        window = await connect_service("socket")
        told = asyncio.Event()
        window.on("Quit", told.set)
        assert await window.call("RegisterWindow") is True
        await window.call("SetSettings", {"finish_action": "quit"})
        await window.call("Add", url, {"dir": str(tmp_path / "out")})
        await asyncio.wait_for(told.wait(), 15)
        stopped = await asyncio.wait_for(service, 15)
        await window.close()
        return stopped

    assert run(scenario()) is False  # stopped, not restarting
    assert (tmp_path / "out" / "test.bin").read_bytes() == data


def test_local_finisher():
    """Without D-Bus: "quit" closes the windows, then the service; suspend and power off wait for the
    countdown, which a starting download cancels; without the system's power calls they do nothing."""
    from ldm.finish import LocalFinisher

    async def scenario():
        events = []

        async def power(action):
            events.append(action)

        finisher = LocalFinisher(lambda: events.append("windows"), lambda: events.append("stop"), power,
                                 countdown=0.05)
        finisher.run("quit")
        finisher.run("suspend")
        finisher.cancel()
        await asyncio.sleep(0.1)
        finisher.run("poweroff")
        await asyncio.sleep(0.1)
        LocalFinisher(lambda: None, lambda: None).run("suspend")  # logged, nothing else
        return events

    assert run(scenario()) == ["windows", "stop", "poweroff"]
