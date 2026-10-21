"""Native messaging host: framing, GVariant text, and real calls against a private-bus daemon."""

import io
import json
import os
import random
import shutil
import signal
import struct
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from ldm import busnames, nmhost

HOST = Path(nmhost.__file__)
needs_dbus = pytest.mark.skipif(shutil.which("dbus-daemon") is None or shutil.which("gdbus") is None,
                                reason="dbus-daemon/gdbus not installed")


def test_bus_names_match_package():
    assert (nmhost.BUS_NAME, nmhost.OBJECT_PATH, nmhost.INTERFACE) == \
        (busnames.BUS_NAME, busnames.OBJECT_PATH, busnames.INTERFACE)


def test_host_uses_only_the_standard_library():
    imports = [line for line in HOST.read_text().splitlines() if line.startswith(("import ", "from "))]
    assert all(line.split()[1].split(".")[0] in sys.stdlib_module_names for line in imports), imports


def test_framing_round_trip():
    buffer = io.BytesIO()
    nmhost.write_message(buffer, {"id": 1, "text": "héllo"})
    buffer.seek(0)
    assert nmhost.read_message(buffer) == {"id": 1, "text": "héllo"}
    assert nmhost.read_message(buffer) is None


def test_gvariant_text():
    assert nmhost.gv_str('a "b" \\ c\n') == '"a \\"b\\" \\\\ c\\n"'
    assert nmhost.gv_vardict({}) == "@a{sv} {}"
    assert nmhost.gv_vardict({"name": "x.iso", "skip": None, "n": 3, "headers": {"Referer": "r"}}) == \
        '{"name": <"x.iso">, "n": <int64 3>, "headers": <{"Referer": "r"}>}'
    # gdbus itself must accept what we build
    if shutil.which("gdbus"):
        out = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
                              "--object-path", "/x", "--method", "x.y", nmhost.gv_str('q"\\é')],
                             capture_output=True, text=True)
        assert "parse" not in out.stderr.lower()


class Host:
    """The host script as a subprocess, speaking native messaging."""

    def __init__(self):
        self.proc = subprocess.Popen([sys.executable, str(HOST)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.next_id = 0

    def request(self, **message):
        self.next_id += 1
        nmhost.write_message(self.proc.stdin, {"id": self.next_id, **message})
        reply = nmhost.read_message(self.proc.stdout)
        assert reply["id"] == self.next_id
        return reply

    def close(self):
        self.proc.stdin.close()
        assert self.proc.wait(10) == 0


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def ldm_list():
    out = subprocess.run([sys.executable, "-m", "ldm", "list"], capture_output=True, text=True)
    return out.stdout


@needs_dbus
def test_ping_and_direct_add(daemon, http_server, tmp_path):
    data = random.Random(11).randbytes(200_000)
    cfg, url = http_server(data=data)
    host = Host()
    assert host.request(cmd="ping") == {"id": 1, "ok": True, "daemon": True, "gui": False, "host_version": "0.1.0"}
    reply = host.request(cmd="add", mode="direct", url=url, filename="from-browser.bin",
                         referer="https://example.com/page", cookies="session=abc", user_agent="TestBrowser/1")
    assert reply["ok"] and reply["download_id"] == 1
    wait_for(lambda: "done" in ldm_list())
    bad = host.request(cmd="add", mode="direct", url="ftp://nope/x")
    many = host.request(cmd="add_many", items=[{"url": url, "filename": "a.bin"}, {"url": "nope"}])
    host.close()

    assert bad == {"id": 3, "ok": False, "error": "unsupported URL: ftp://nope/x"}
    assert [("download_id" in r, "error" in r) for r in many["results"]] == [(True, False), (False, True)]
    part_requests = [r for r in cfg.requests if r.get("Referer")]
    assert part_requests and all(r["Cookie"] == "session=abc" and r["User-Agent"] == "TestBrowser/1"
                                 for r in part_requests)


FAKE_GUI = textwrap.dedent("""
    import sys
    from gi.repository import Gio, GLib
    app = Gio.Application(application_id="io.github.ldm.Gui", flags=Gio.ApplicationFlags.IS_SERVICE)
    action = Gio.SimpleAction.new("add-request", GLib.VariantType.new("s"))
    def got(_a, param):
        open(sys.argv[1], "w").write(param.get_string())
        app.quit()
    action.connect("activate", got)
    app.add_action(action)
    app.set_inactivity_timeout(20000)
    app.hold()
    app.run([])
""")


@needs_dbus
def test_dialog_mode_activates_gui_action(daemon, tmp_path):
    out = tmp_path / "request.json"
    gui = subprocess.Popen([sys.executable, "-c", FAKE_GUI, str(out)])
    host = Host()
    wait_for(lambda: host.request(cmd="ping")["gui"])
    reply = host.request(cmd="add", url="https://example.com/a.iso", filename="a.iso",
                         referer="https://example.com/", cookies="k=v")
    host.close()
    gui.wait(10)
    assert reply["ok"] and reply["dialog"]
    assert json.loads(out.read_text()) == {
        "url": "https://example.com/a.iso", "filename": "a.iso",
        "headers": {"Referer": "https://example.com/", "Cookie": "k=v"}, "size": None, "page_url": None,
        "kind": None}


@needs_dbus
def test_daemon_is_started_by_dbus_activation(tmp_path, http_server, monkeypatch):
    """No daemon running: the host's add() starts it through an activation file."""
    services = tmp_path / "data" / "dbus-1" / "services"
    services.mkdir(parents=True)
    (services / "io.github.ldm.service").write_text(
        f"[D-BUS Service]\nName=io.github.ldm\nExec={sys.executable} -m ldm --db {tmp_path / 'auto.db'} daemon\n")
    bus = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address"],
                           stdout=subprocess.PIPE, text=True,
                           env={**os.environ, "XDG_DATA_HOME": str(tmp_path / "data")})
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", bus.stdout.readline().strip())
    data = random.Random(12).randbytes(100_000)
    cfg, url = http_server(data=data)
    try:
        host = Host()
        assert host.request(cmd="ping")["daemon"] is False
        reply = host.request(cmd="add", mode="direct", url=url, filename="auto.bin")
        assert reply["ok"], reply
        assert host.request(cmd="ping")["daemon"] is True
        host.close()
        wait_for(lambda: "done" in ldm_list())
    finally:
        pid = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path",
                              "/org/freedesktop/DBus", "--method",
                              "org.freedesktop.DBus.GetConnectionUnixProcessID", "io.github.ldm"],
                             capture_output=True, text=True).stdout
        bus.terminate()
        bus.wait()
        if pid.strip().startswith("(uint32"):
            daemon_pid = int(pid.strip().strip("(),").split()[-1])
            for _ in range(100):  # the daemon exits by itself once its bus is gone
                try:
                    os.kill(daemon_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                os.kill(daemon_pid, signal.SIGTERM)
                pytest.fail("daemon kept running after its bus went away")
