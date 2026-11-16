"""The service's local channel (ldm.channel, ldm.socket_service): messages, errors, privacy, one service only,
and the token on Windows' TCP channel (tried here on Linux too)."""

import asyncio
import os
import stat

import pytest

from ldm import channel
from ldm.api import METHODS, Api
from ldm.manager import Manager
from ldm.passwords import MemoryPasswords
from ldm.socket_service import AlreadyListening, SocketService
from ldm.store import Store


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


def test_both_channels_offer_the_same_methods():
    from ldm.api import SIGNALS
    from ldm.dbus_service import ManagerInterface

    dbus_methods = {name for name in vars(ManagerInterface) if name[0].isupper()} - set(SIGNALS)
    assert dbus_methods == METHODS


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
    assert file_mode == 0o600  # the token: readable by the user only
    assert not path.exists()  # removed when the service stops
