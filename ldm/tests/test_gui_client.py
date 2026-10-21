"""DaemonClient (Gio) against the daemon on a private bus."""

import random
import shutil
import time

import pytest

gi = pytest.importorskip("gi")
from gi.repository import GLib  # noqa: E402

from ldm.gui.client import DaemonClient  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="dbus-daemon not installed")


def spin_until(condition, timeout=15.0):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)


def call(method, *args):
    """Run an async client call to completion; returns (result, error)."""
    box = []
    method(*args, lambda result, error: box.append((result, error)))
    spin_until(lambda: box)
    return box[0]


def test_client_drives_a_download(daemon, http_server, tmp_path):
    data = random.Random(4).randbytes(1_000_000)
    cfg, url = http_server(data=data, delay=0.02)
    client = DaemonClient.for_address(daemon)
    spin_until(lambda: client.connected)

    states, progress = [], []
    client.connect("state-changed", lambda c, i, state, error: states.append(state))
    client.connect("progress", lambda c, batch: progress.extend(batch))

    download_id, error = call(client.add, url, {"dir": str(tmp_path / "out"), "connections": 4})
    assert error is None
    spin_until(lambda: "done" in states)
    info, _ = call(client.get, download_id)
    rows, _ = call(client.list)
    settings, _ = call(client.get_settings)
    client.close()

    assert progress and progress[0][0] == download_id
    assert (info["state"], info["size"]) == ("done", len(data))
    assert (tmp_path / "out" / info["name"]).read_bytes() == data
    assert [r["id"] for r in rows] == [download_id]
    assert settings["max_parallel"] == 3


def test_errors_and_settings(daemon):
    client = DaemonClient.for_address(daemon)
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


def test_not_connected_without_daemon(bus_address):
    client = DaemonClient.for_address(bus_address)
    spin_until(lambda: True)
    assert not client.connected
    _, error = call(client.list)
    client.close()
    assert error.name == "NotRunning"
