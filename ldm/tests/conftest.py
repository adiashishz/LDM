"""Local HTTP test server with byte-range support and fault injection."""

from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

# A session bus for the tests: Linux with dbus-daemon. Elsewhere (Windows, macOS), or with LDM_TEST_NO_DBUS=1
# (to try that on Linux), the test service runs on the local channel only and D-Bus tests are skipped.
HAS_DBUS = (sys.platform.startswith("linux") and bool(shutil.which("dbus-daemon"))
            and not os.environ.get("LDM_TEST_NO_DBUS"))
if not HAS_DBUS:
    # Test files for the Linux desktop's D-Bus parts (they import dbus-fast, which Windows can't load).
    collect_ignore = ["test_dbus.py", "test_finish.py", "test_notify.py", "test_system.py", "test_tray.py"]
if os.environ.get("LDM_TEST_NO_DBUS"):
    import importlib.abc

    class _NoDbusFast(importlib.abc.MetaPathFinder):
        """As on Windows: dbus-fast can't be imported, so nothing shared with Windows may need it."""

        def find_spec(self, name, path=None, target=None):
            if name == "dbus_fast" or name.startswith("dbus_fast."):
                raise ImportError(f"{name} is blocked (LDM_TEST_NO_DBUS)")
            return None

    sys.meta_path.insert(0, _NoDbusFast())
needs_dbus = pytest.mark.skipif(not HAS_DBUS, reason="needs a D-Bus session bus (Linux)")
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux desktop only")


@pytest.fixture(autouse=True)
def private_folders(tmp_path_factory, monkeypatch):
    """Never the user's real folders: downloads without an explicit folder (and backups) go here.
    Daemons the tests start, in-process or as subprocesses, inherit these."""
    folders = tmp_path_factory.mktemp("user-folders")
    monkeypatch.setenv("XDG_DOWNLOAD_DIR", str(folders / "Downloads"))
    monkeypatch.setenv("XDG_DOCUMENTS_DIR", str(folders / "Documents"))
    # The local channel's socket: never next to the user's running service.
    (folders / "runtime").mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(folders / "runtime"))
    monkeypatch.setenv("LOCALAPPDATA", str(folders / "LocalAppData"))  # Windows: the channel's file, data
    monkeypatch.delenv("LDM_SOCKET", raising=False)
    # The daily yt-dlp check must not reach GitHub (nothing listens on port 9).
    monkeypatch.setenv("LDM_YTDLP_RELEASES", "http://127.0.0.1:9/releases")
    monkeypatch.setattr("ldm.ytdlp.RELEASES", "http://127.0.0.1:9/releases")
    return folders


@dataclass
class ServerConfig:
    data: bytes
    ranges: bool = True             # honour Range requests (206)
    etag: str | None = '"v1"'
    send_length: bool = True        # False: no Content-Length, close to end the body
    disposition: str | None = None  # Content-Disposition header
    status: int | None = None       # answer every request with this status
    chunk: int = 16 * 1024
    delay: float = 0.0              # sleep after each chunk
    slow_from: int = 1 << 62        # responses starting at/after this offset ...
    slow_delay: float = 0.0         # ... sleep this much extra per chunk
    drops: int = 0                  # number of responses to cut short
    drop_after: int = 0             # bytes sent before cutting a response
    redirects: dict[str, str] = field(default_factory=dict)  # path -> Location (302)
    files: dict[str, bytes] = field(default_factory=dict)    # static files: path -> content (others 404)
    deny: set[str] = field(default_factory=set)              # paths answered with 403
    require: dict[str, str] = field(default_factory=dict)    # headers that must match exactly, else 403
    bytes_sent: int = 0
    active_ranges: int = 0          # bounded range requests being served right now
    max_active_ranges: int = 0
    range_limit: int = 0            # more simultaneous range requests than this get HTTP 429 ...
    retry_after: str | None = None  # ... with this Retry-After header
    throttled: int = 0              # 429 responses sent
    auth: tuple[str, str, str] | None = None  # ("basic" | "digest", user, password): else HTTP 401
    requests: list[dict[str, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        cfg: ServerConfig = self.server.cfg
        with cfg.lock:
            cfg.requests.append(dict(self.headers))
        if cfg.auth and not _authorized(cfg.auth, self.command, self.headers.get("Authorization", "")):
            self.send_response(401)
            scheme = cfg.auth[0]
            self.send_header("WWW-Authenticate", 'Basic realm="ldm"' if scheme == "basic" else
                             f'Digest realm="ldm", qop="auth", nonce="{DIGEST_NONCE}", algorithm=MD5')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path in cfg.redirects:
            self.send_response(302)
            self.send_header("Location", cfg.redirects[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        refused = self.path in cfg.deny or any(self.headers.get(k) != v for k, v in cfg.require.items())
        status = 403 if refused else cfg.status
        if status is not None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        data = cfg.data
        if cfg.files:
            data = cfg.files.get(self.path.split("?")[0])
            if data is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        size = len(data)
        start, end, partial = 0, size - 1, False
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        if_range = self.headers.get("If-Range")
        if match and cfg.ranges and (if_range is None or if_range == cfg.etag):
            start = int(match.group(1))
            end = min(int(match.group(2)) if match.group(2) else size - 1, size - 1)
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            partial = True

        length = max(0, end - start + 1)
        bounded = bool(match and match.group(2))  # part request, not the open-ended probe
        with cfg.lock:
            refuse = bool(bounded and cfg.range_limit and cfg.active_ranges >= cfg.range_limit)
            if refuse:
                cfg.throttled += 1
            elif bounded:
                cfg.active_ranges += 1
                cfg.max_active_ranges = max(cfg.max_active_ranges, cfg.active_ranges)
        if refuse:
            self.send_response(429)
            if cfg.retry_after is not None:
                self.send_header("Retry-After", cfg.retry_after)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(206 if partial else 200)
        if cfg.ranges:
            self.send_header("Accept-Ranges", "bytes")
        if cfg.etag:
            self.send_header("ETag", cfg.etag)
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if cfg.disposition:
            self.send_header("Content-Disposition", cfg.disposition)
        if cfg.files:
            self.send_header("Content-Type", mimetypes.guess_type(self.path.split("?")[0])[0]
                             or ("application/vnd.apple.mpegurl" if ".m3u8" in self.path else "application/octet-stream"))
        if cfg.send_length:
            self.send_header("Content-Length", str(length))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()

        cut = None
        with cfg.lock:
            if cfg.drops > 0 and length > cfg.drop_after:
                cfg.drops -= 1
                cut = cfg.drop_after
        try:
            self._send_body(cfg, data, start, length, cut)
        finally:
            if bounded:
                with cfg.lock:
                    cfg.active_ranges -= 1

    def _send_body(self, cfg: ServerConfig, data: bytes, start: int, length: int, cut: int | None) -> None:
        sent = 0
        try:
            while sent < length:
                n = min(cfg.chunk, length - sent)
                if cut is not None and sent + n > cut:
                    self.wfile.write(data[start + sent:start + cut])
                    self._count(cut - sent)
                    self.close_connection = True
                    return
                self.wfile.write(data[start + sent:start + sent + n])
                self._count(n)
                sent += n
                pause = cfg.delay + (cfg.slow_delay if start >= cfg.slow_from else 0)
                if pause:
                    time.sleep(pause)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _count(self, n: int) -> None:
        cfg = self.server.cfg
        with cfg.lock:
            cfg.bytes_sent += n


DIGEST_NONCE = "a1b2c3d4"


def _authorized(auth: tuple[str, str, str], method: str, header: str) -> bool:
    import base64
    import hashlib
    scheme, user, password = auth
    if scheme == "basic":
        return header == "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    if not header.startswith("Digest "):
        return False
    fields = dict(re.findall(r'(\w+)="?([^",]*)"?', header[7:]))
    md5 = lambda text: hashlib.md5(text.encode()).hexdigest()  # noqa: E731
    ha1, ha2 = md5(f"{user}:ldm:{password}"), md5(f"{method}:{fields.get('uri')}")
    expected = md5(f"{ha1}:{DIGEST_NONCE}:{fields.get('nc')}:{fields.get('cnonce')}:auth:{ha2}")
    return fields.get("username") == user and fields.get("response") == expected


@pytest.fixture
def http_server():
    """Start test servers: `cfg, url = http_server(data=b"...", ranges=False, ...)`."""
    servers = []

    def start(**kwargs) -> tuple[ServerConfig, str]:
        cfg = ServerConfig(**kwargs)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.cfg = cfg
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        cfg.base = f"http://127.0.0.1:{server.server_port}"
        return cfg, f"{cfg.base}/files/test.bin"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


# --- private D-Bus session bus + daemon -------------------------------------------------

@pytest.fixture
def bus_address(monkeypatch, tmp_path_factory):
    if not HAS_DBUS:
        pytest.skip("needs a D-Bus session bus (Linux)")
    # Own XDG_DATA_HOME: the bus must not see the user's real ldm activation files.
    env = {**os.environ, "XDG_DATA_HOME": str(tmp_path_factory.mktemp("bus-data"))}
    proc = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address"],
                            stdout=subprocess.PIPE, text=True, env=env)
    address = proc.stdout.readline().strip()
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)
    yield address
    proc.terminate()
    proc.wait()


@pytest.fixture
def daemon(request, tmp_path):
    """Run `serve()` in its own thread and event loop: on D-Bus and the local channel, or the channel only
    where there is no D-Bus (then the fixture's value is None, not the bus address)."""
    bus_address = request.getfixturevalue("bus_address") if HAS_DBUS else None
    loop = asyncio.new_event_loop()
    stop, ready = asyncio.Event(), threading.Event()
    from ldm.passwords import MemoryPasswords
    options = {"progress_interval": 0.05, "downloader_options": {"min_part": 64 * 1024},
               "passwords": MemoryPasswords()}  # never the user's keyring
    from ldm.daemon import serve

    async def run():
        started = asyncio.Event()
        task = asyncio.create_task(serve(bus_address=bus_address, db_path=tmp_path / "daemon.db",
                                         stop=stop, ready=started, manager_options=options,
                                         system_bus_address=bus_address,  # never the real system bus
                                         manage_autostart=False,           # nor the user's systemd
                                         dbus=HAS_DBUS, desktop=False))    # nor Windows' tray or power
        await started.wait()
        ready.set()
        await task

    thread = threading.Thread(target=loop.run_until_complete, args=(run(),), daemon=True)
    thread.start()
    assert ready.wait(10), "daemon did not start"
    yield bus_address
    loop.call_soon_threadsafe(stop.set)
    thread.join(10)


def use_service(monkeypatch, daemon) -> None:
    """Clients (the command line, the browser bridge) find the test service: on its bus, or without D-Bus
    on the local channel."""
    if daemon:
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", daemon)
    else:
        monkeypatch.setenv("LDM_TRANSPORT", "socket")


@pytest.fixture
def session_bus(request):
    """The test bus's address where there is D-Bus, else None (for tests that run either way)."""
    return request.getfixturevalue("bus_address") if HAS_DBUS else None


@pytest.fixture(params=["dbus", "socket"])
def cli_transport(request, monkeypatch):
    """Run a command-line test over D-Bus, then over the local channel."""
    if request.param == "dbus" and not HAS_DBUS:
        pytest.skip("needs a D-Bus session bus (Linux)")
    monkeypatch.setenv("LDM_TRANSPORT", request.param)
    return request.param


@pytest.fixture
def service_socket(daemon):
    """The running test service's local channel (a Unix socket in the test's own runtime folder; on Windows
    the file with its port and token)."""
    from ldm import channel

    return str(channel.address())


# --- proxies and DNS-over-HTTPS ---------------------------------------------------------

@pytest.fixture
def proxy_server():
    """HTTP forward proxy: `proxy = proxy_server(login=("u", "p"))`; `proxy.url`, `proxy.requests`."""
    from netservers import ForwardProxy
    proxies = []

    def start(login=None):
        proxy = ForwardProxy(login)
        proxies.append(proxy)
        return proxy

    yield start
    for proxy in proxies:
        proxy.close()


@pytest.fixture
def socks_server():
    from netservers import Socks5Proxy
    proxy = Socks5Proxy()
    yield proxy
    proxy.close()


@pytest.fixture
def doh_server():
    """Fake DNS-over-HTTPS server (plain HTTP): `doh = doh_server({"files.test": "127.0.0.1"})`."""
    pytest.importorskip("dns", reason="python3-dns (dnspython) not installed")
    from netservers import DohServer
    servers = []

    def start(names):
        server = DohServer(names)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


# --- Qt -------------------------------------------------------------------------------------

@pytest.fixture(scope="session")
def qt_app():
    """One QApplication for every Qt test, drawn offscreen and on Qt's own event loop (not GLib's, which
    other tests' GLib work, such as the proxy lookup, could leave sources on)."""
    pytest.importorskip("PySide6")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_NO_GLIB"] = "1"
    from PySide6.QtWidgets import QApplication

    from ldm.qt.theme import ui_font

    app = QApplication.instance() or QApplication([])
    app.setFont(ui_font())  # the bundled Inter, as the app uses: the same text widths on every system
    return app
