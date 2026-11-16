"""Native messaging host: framing, GVariant text, and real calls against a private-bus daemon."""

import io
import json
import os
import random
import shutil
import signal
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
    assert host.request(cmd="ping") == {"id": 1, "ok": True, "daemon": True, "gui": False, "host_version": nmhost.VERSION}
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


# The window's own D-Bus side (what ldm-gui offers), without the window: the host's gdbus call must reach it.
FAKE_GUI = textwrap.dedent("""
    import asyncio, sys
    from dbus_fast.aio import MessageBus
    from ldm.ui.desktop import claim

    async def main():
        bus = await MessageBus().connect()
        done = asyncio.get_running_loop().create_future()

        def handler(name, arguments, platform_data):
            if name == "add-request" and not done.done():
                done.set_result(arguments[0])

        assert await claim(bus, handler)
        open(sys.argv[1], "w").write(await asyncio.wait_for(done, 20))
        bus.disconnect()

    asyncio.run(main())
""")


@needs_dbus
def test_dialog_mode_activates_gui_action(daemon, tmp_path):
    out = tmp_path / "request.json"
    gui = subprocess.Popen([sys.executable, "-c", FAKE_GUI, str(out)])
    host = Host()
    wait_for(lambda: host.request(cmd="ping")["gui"])
    capture = {"url": "https://cdn.example.com/v.m3u8", "kind": "hls", "size": None}
    reply = host.request(cmd="add", url="https://example.com/a.iso", filename="a.iso",
                         referer="https://example.com/", cookies="k=v", captures=[capture])
    host.close()
    gui.wait(10)
    assert reply["ok"] and reply["dialog"]
    assert json.loads(out.read_text()) == {
        "url": "https://example.com/a.iso", "filename": "a.iso",
        "headers": {"Referer": "https://example.com/", "Cookie": "k=v"}, "size": None, "page_url": None,
        "kind": None, "captures": [capture]}  # media found on the page reaches the add dialog


def running(pid: int) -> bool:
    """Whether a process is alive. An exited process nobody reaped yet (a zombie: the activated daemon is
    the bus's child and goes to PID 1, which in CI containers never reaps) counts as exited."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state not in ("Z", "X")


@needs_dbus
def test_daemon_is_started_by_dbus_activation(tmp_path, http_server, monkeypatch):
    """No daemon running: the host's add() starts it through an activation file."""
    services = tmp_path / "data" / "dbus-1" / "services"
    services.mkdir(parents=True)
    (services / "io.github.adiashishz.LDM.Service.service").write_text(
        f"[D-BUS Service]\nName=io.github.adiashishz.LDM.Service\nExec={sys.executable} -m ldm --db {tmp_path / 'auto.db'} daemon\n")
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
                              "org.freedesktop.DBus.GetConnectionUnixProcessID", "io.github.adiashishz.LDM.Service"],
                             capture_output=True, text=True).stdout
        bus.terminate()
        bus.wait()
        if pid.strip().startswith("(uint32"):
            daemon_pid = int(pid.strip().strip("(),").split()[-1])
            for _ in range(100):  # the daemon exits by itself once its bus is gone
                if not running(daemon_pid):
                    break
                time.sleep(0.05)
            else:
                os.kill(daemon_pid, signal.SIGTERM)
                pytest.fail("daemon kept running after its bus went away")


def test_headers_for_maps_browser_fields():
    from ldm.nmhost import headers_for
    assert headers_for({"referer": "https://site/", "origin": "https://site", "cookies": "a=1", "user_agent": "UA",
                        "url": "x"}) == {"Referer": "https://site/", "Origin": "https://site", "Cookie": "a=1",
                                         "User-Agent": "UA"}
    assert headers_for({"origin": ""}) == {}


def test_headers_for_keeps_what_the_browser_sent():
    from ldm.nmhost import headers_for
    item = {"headers": {"user-agent": "Brave UA", "accept-language": "en-US,en;q=0.8", "accept": "*/*",
                        "sec-fetch-mode": "cors", "referer": "https://site/"},
            "referer": "https://site/", "user_agent": "Brave UA", "cookies": "a=1"}
    assert headers_for(item) == {"User-Agent": "Brave UA", "Accept-Language": "en-US,en;q=0.8", "Accept": "*/*",
                                 "Sec-Fetch-Mode": "cors", "Referer": "https://site/", "Cookie": "a=1"}


# --- the local channel (Windows, macOS; here with LDM_TRANSPORT=socket) ----------------------------

# Stands in for LDM's window on the local channel: registers, then writes the first Add request it gets.
FAKE_WINDOW = textwrap.dedent("""
    import json, os, socket, sys
    path = os.path.join(os.environ["XDG_RUNTIME_DIR"], "ldm", "service.sock")
    sock = socket.socket(socket.AF_UNIX)
    sock.connect(path)
    sock.sendall(b'{"id": 1, "method": "RegisterWindow", "params": []}\\n')
    for line in sock.makefile("rb"):
        message = json.loads(line)
        if message.get("signal") == "AddRequest":
            open(sys.argv[1], "w").write(json.dumps(message["args"][0]))
            break
""")


def test_socket_ping_direct_add_and_dialog(daemon, http_server, tmp_path, monkeypatch):
    monkeypatch.setenv("LDM_TRANSPORT", "socket")
    data = random.Random(13).randbytes(150_000)
    cfg, url = http_server(data=data)
    host = Host()
    assert host.request(cmd="ping") == {"id": 1, "ok": True, "daemon": True, "gui": False,
                                        "host_version": nmhost.VERSION}
    reply = host.request(cmd="add", mode="direct", url=url, filename="over-socket.bin", referer="https://e.com/")
    assert reply["ok"] and reply["download_id"] == 1
    wait_for(lambda: "done" in ldm_list())
    out = tmp_path / "request.json"
    window = subprocess.Popen([sys.executable, "-c", FAKE_WINDOW, str(out)])
    wait_for(lambda: host.request(cmd="ping")["gui"])
    reply = host.request(cmd="add", url="https://example.com/a.iso", filename="a.iso", cookies="k=v")
    assert reply["ok"] and reply["dialog"]
    window.wait(10)
    host.close()
    assert json.loads(out.read_text()) == {"url": "https://example.com/a.iso", "filename": "a.iso",
                                           "headers": {"Cookie": "k=v"}, "size": None, "page_url": None,
                                           "kind": None, "captures": []}


def test_socket_dialog_starts_the_window(daemon, tmp_path, monkeypatch):
    """No window open: the host starts it (LDM_GUI_COMMAND) and the request still arrives."""
    script, out = tmp_path / "window.py", tmp_path / "request.json"
    script.write_text(FAKE_WINDOW)
    monkeypatch.setenv("LDM_TRANSPORT", "socket")
    monkeypatch.setenv("LDM_GUI_COMMAND", f"{sys.executable} {script} {out}")
    host = Host()
    reply = host.request(cmd="add", url="https://example.com/b.iso")
    host.close()
    assert reply["ok"] and reply["dialog"]
    wait_for(out.exists)
    assert json.loads(out.read_text())["url"] == "https://example.com/b.iso"


@needs_dbus
def test_socket_starts_the_service(bus_address, http_server, tmp_path, monkeypatch):
    """No service running and no D-Bus activation (as on Windows and macOS): the host starts `ldm daemon`."""
    monkeypatch.setenv("LDM_TRANSPORT", "socket")
    monkeypatch.setenv("LDM_COMMAND", f"{sys.executable} -m ldm --db {tmp_path / 'started.db'}")
    cfg, url = http_server(data=random.Random(14).randbytes(50_000))
    host = Host()
    assert host.request(cmd="ping")["daemon"] is False
    reply = host.request(cmd="add", mode="direct", url=url, filename="started.bin")
    assert reply["ok"], reply
    assert host.request(cmd="ping")["daemon"] is True
    host.close()
    wait_for(lambda: "done" in ldm_list())
    # The started service ends with its bus (the bus_address fixture stops it).
