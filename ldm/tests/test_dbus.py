"""The daemon on a private D-Bus session bus, driven by a dbus-fast client and by the CLI."""

import asyncio
import random
import shutil

import pytest
from dbus_fast import DBusError, Variant
from dbus_fast.aio import MessageBus

from ldm.cli import main
from ldm.daemon import AlreadyRunning, serve
from ldm.dbus_service import BUS_NAME, INTERFACE, OBJECT_PATH

pytestmark = pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="dbus-daemon not installed")


async def client(address):
    bus = await MessageBus(bus_address=address).connect()
    introspection = await bus.introspect(BUS_NAME, OBJECT_PATH)
    return bus, bus.get_proxy_object(BUS_NAME, OBJECT_PATH, introspection).get_interface(INTERFACE)


def test_add_over_dbus_emits_progress_and_finishes(daemon, http_server, tmp_path):
    data = random.Random(7).randbytes(1_000_000)
    cfg, url = http_server(data=data, delay=0.02)

    async def go():
        bus, iface = await client(daemon)
        states, batches = [], []
        finished = asyncio.Event()

        def on_state(i, state, error):
            states.append(state)
            if state == "done":
                finished.set()

        iface.on_state_changed(on_state)
        iface.on_progress(batches.append)
        download_id = await iface.call_add(url, {"dir": Variant("s", str(tmp_path / "out")),
                                                 "connections": Variant("x", 4)})
        await asyncio.wait_for(finished.wait(), 15)
        info = {k: v.value for k, v in (await iface.call_get(download_id)).items()}
        listed = await iface.call_list()
        bus.disconnect()
        return states, batches, info, listed

    states, batches, info, listed = asyncio.run(go())
    assert states[0] == "queued" and states[-1] == "done"
    assert batches, "no Progress signal received"
    assert (info["state"], info["size"], info["done"]) == ("done", len(data), len(data))
    assert (tmp_path / "out" / info["name"]).read_bytes() == data
    assert len(listed) == 1


def test_errors_are_dbus_errors(daemon):
    async def go():
        bus, iface = await client(daemon)
        try:
            with pytest.raises(DBusError) as not_found:
                await iface.call_pause(99)
            with pytest.raises(DBusError) as bad_url:
                await iface.call_add("ftp://example.com/x", {})
            with pytest.raises(DBusError) as bad_setting:
                await iface.call_set_settings({"max_parallel": Variant("x", 0)})
        finally:
            bus.disconnect()
        return not_found.value.type, bad_url.value.type, bad_setting.value.type

    assert asyncio.run(go()) == ("io.github.ldm.Error.NotFound",) + ("io.github.ldm.Error.InvalidArgument",) * 2


def test_second_daemon_refuses_to_start(daemon, tmp_path):
    with pytest.raises(AlreadyRunning):
        asyncio.run(serve(bus_address=daemon, db_path=tmp_path / "other.db", stop=asyncio.Event(),
                          system_bus_address=daemon, manage_autostart=False))


def test_cli_talks_to_daemon(daemon, http_server, tmp_path, capsys):
    data = random.Random(8).randbytes(300_000)
    cfg, url = http_server(data=data)
    assert main(["settings", "max_parallel", "2"]) == 0
    assert main(["host-rule", "127.0.0.1", "--max-downloads", "1"]) == 0
    assert main(["add", url, "-o", str(tmp_path / "out"), "--wait"]) == 0
    assert (tmp_path / "out" / "test.bin").read_bytes() == data
    capsys.readouterr()
    assert main(["list"]) == 0
    assert main(["settings"]) == 0
    assert main(["host-rules"]) == 0
    out = capsys.readouterr().out
    assert "done" in out and "test.bin" in out
    assert "max_parallel = 2" in out
    assert "127.0.0.1: max_connections=0 max_downloads=1" in out


def test_cli_without_daemon(bus_address, capsys):
    assert main(["list"]) == 1
    assert "daemon is not running" in capsys.readouterr().out


def test_probe_over_dbus(daemon, http_server):
    cfg, url = http_server(data=b"x" * 5000, ranges=False)

    async def go():
        bus, iface = await client(daemon)
        try:
            return {k: v.value for k, v in (await iface.call_probe(url, {})).items()}
        finally:
            bus.disconnect()

    info = asyncio.run(go())
    assert (info["size"], info["resumable"], info["filename"]) == (5000, False, "test.bin")


def test_probe_media_over_dbus(daemon, http_server):
    cfg, url = http_server(data=b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.0,\nseg.ts\n#EXT-X-ENDLIST\n")
    url = url.replace("test.bin", "list.m3u8")

    async def go():
        bus, iface = await client(daemon)
        try:
            return {k: v.value for k, v in (await iface.call_probe_media(url, {})).items()}
        finally:
            bus.disconnect()

    info = asyncio.run(go())
    assert info["title"] == "list"
    assert info["choices"][0]["id"].value == "best"  # nested a{sv} entries arrive as Variants


def test_probe_torrent_over_dbus(daemon, http_server, tmp_path):
    lt = pytest.importorskip("libtorrent")
    src = tmp_path / "pack"
    src.mkdir()
    (src / "one.txt").write_bytes(b"x" * 50_000)
    fs = lt.file_storage()
    lt.add_files(fs, str(src))
    ct = lt.create_torrent(fs, 16 * 1024)
    lt.set_piece_hashes(ct, str(tmp_path))
    data = lt.bencode(ct.generate())
    cfg, _ = http_server(data=b"", files={"/pack.torrent": data})

    async def go():
        bus, iface = await client(daemon)
        try:
            return {k: v.value for k, v in (await iface.call_probe_torrent(f"{cfg.base}/pack.torrent", {})).items()}
        finally:
            bus.disconnect()

    info = asyncio.run(go())
    assert info["name"] == "pack" and info["size"] == 50_000 and info["metadata"] == data
    assert [f["path"].value for f in info["files"]] == ["pack/one.txt"]
