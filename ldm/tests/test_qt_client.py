"""DaemonClient (Qt) against the daemon, over D-Bus (a private bus) and over the local channel (its socket)."""

import random
import time

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication  # noqa: E402

from conftest import HAS_DBUS  # noqa: E402
from ldm.qt.client import DaemonClient  # noqa: E402


@pytest.fixture
def app(qt_app):
    return qt_app


@pytest.fixture(params=["dbus", "socket"])
def transport(request):
    if request.param == "dbus" and not HAS_DBUS:
        pytest.skip("needs a D-Bus session bus (Linux)")
    return request.param


def connect(transport, bus_address):
    """A client of the test service over `transport` (the socket: the test's own runtime folder)."""
    from ldm import channel

    return DaemonClient(bus_address if transport == "dbus" else str(channel.address()), transport=transport)


def spin_until(condition, timeout=15.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        QCoreApplication.processEvents()
        time.sleep(0.005)


def call(method, *args):
    """Run an async client call to completion; returns (result, error)."""
    box = []
    method(*args, lambda result, error: box.append((result, error)))
    spin_until(lambda: box)
    return box[0]


def test_client_drives_a_download(app, daemon, transport, http_server, tmp_path):
    data = random.Random(4).randbytes(1_000_000)
    cfg, url = http_server(data=data, delay=0.02)
    client = connect(transport, daemon)
    spin_until(lambda: client.connected)

    added, states, progress = [], [], []
    client.added.connect(added.append)
    client.state_changed.connect(lambda i, state, error: states.append(state))
    client.progress.connect(progress.extend)

    download_id, error = call(client.add, url, {"dir": str(tmp_path / "out"), "connections": 4, "name": None})
    assert error is None
    spin_until(lambda: "done" in states)
    info, _ = call(client.get, download_id)
    rows, _ = call(client.list)
    settings, _ = call(client.get_settings)
    client.close()

    assert added == [download_id]
    assert progress and progress[0][0] == download_id and isinstance(progress[0], tuple)
    assert (info["state"], info["size"]) == ("done", len(data))
    assert (tmp_path / "out" / info["name"]).read_bytes() == data
    assert [r["id"] for r in rows] == [download_id]
    assert settings["max_parallel"] == 3


def test_errors_and_settings(app, daemon, transport):
    client = connect(transport, daemon)
    spin_until(lambda: client.connected)
    _, error = call(client.pause, 99)
    assert (error.name, error.message) == ("NotFound", "no download with id 99")
    _, error = call(client.set_settings, {"max_parallel": 0})
    assert error.name == "InvalidArgument"
    assert call(client.set_settings, {"max_parallel": 5}) == (None, None)
    assert call(client.set_host_rule, "example.com", 4, 1, True) == (None, None)
    rules, _ = call(client.list_host_rules)
    client.close()
    assert rules == [{"host": "example.com", "max_connections": 4, "max_downloads": 1, "include_subdomains": True}]


def test_not_connected_without_daemon(app, session_bus, transport):
    client = connect(transport, session_bus)
    changes = []
    client.connected_changed.connect(changes.append)
    _, error = call(client.list)
    client.close()
    assert not client.connected and changes == []
    assert error.name == "NotRunning"


def test_follows_the_service_starting_and_stopping(app, session_bus, transport, tmp_path):
    """The window learns when the service appears (on the bus, or its socket) and when it goes away."""
    import asyncio
    import threading

    from ldm.daemon import serve
    from ldm.passwords import MemoryPasswords

    client = connect(transport, session_bus)
    changes = []
    client.connected_changed.connect(changes.append)

    loop = asyncio.new_event_loop()
    stop, ready = asyncio.Event(), threading.Event()

    async def run():
        started = asyncio.Event()
        task = asyncio.create_task(serve(bus_address=session_bus, db_path=tmp_path / "daemon.db", stop=stop,
                                         ready=started, manager_options={"passwords": MemoryPasswords()},
                                         system_bus_address=session_bus, manage_autostart=False,
                                         dbus=HAS_DBUS))
        await started.wait()
        ready.set()
        await task

    thread = threading.Thread(target=loop.run_until_complete, args=(run(),), daemon=True)
    thread.start()
    assert ready.wait(10)
    spin_until(lambda: client.connected)
    loop.call_soon_threadsafe(stop.set)
    thread.join(10)
    spin_until(lambda: not client.connected)
    client.close()
    assert changes == [True, False]


@pytest.mark.skipif(not HAS_DBUS, reason="needs a D-Bus session bus (Linux)")
def test_no_session_bus(app, tmp_path):
    client = DaemonClient(f"unix:path={tmp_path}/missing")
    _, error = call(client.list)
    client.close()
    assert error.name == "NotRunning"


def test_torrent_bytes_survive_the_round_trip(app, daemon, transport, http_server, tmp_path):
    lt = pytest.importorskip("libtorrent")
    src = tmp_path / "pack"
    src.mkdir()
    (src / "one.txt").write_bytes(b"y" * 40_000)
    fs = lt.file_storage()
    lt.add_files(fs, str(src))
    ct = lt.create_torrent(fs, 16 * 1024)
    lt.set_piece_hashes(ct, str(tmp_path))
    data = lt.bencode(ct.generate())
    cfg, _ = http_server(data=b"", files={"/pack.torrent": data})
    client = connect(transport, daemon)
    spin_until(lambda: client.connected)
    info, error = call(client.probe_torrent, f"{cfg.base}/pack.torrent", {})
    assert error is None and info["metadata"] == data
    download_id, error = call(client.add, f"{cfg.base}/pack.torrent",
                              {"kind": "torrent", "metadata": info["metadata"], "priorities": [1],
                               "start": False})
    client.close()
    assert error is None and download_id == 1
