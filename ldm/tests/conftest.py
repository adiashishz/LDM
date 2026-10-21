"""Local HTTP test server with byte-range support and fault injection."""

from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


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
    bytes_sent: int = 0
    active_ranges: int = 0          # bounded range requests being served right now
    max_active_ranges: int = 0
    range_limit: int = 0            # more simultaneous range requests than this get HTTP 429 ...
    retry_after: str | None = None  # ... with this Retry-After header
    throttled: int = 0              # 429 responses sent
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
        if self.path in cfg.redirects:
            self.send_response(302)
            self.send_header("Location", cfg.redirects[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status = 403 if self.path in cfg.deny else cfg.status
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
def daemon(bus_address, tmp_path):
    """Run `serve()` in its own thread and event loop."""
    loop = asyncio.new_event_loop()
    stop, ready = asyncio.Event(), threading.Event()
    options = {"progress_interval": 0.05, "downloader_options": {"min_part": 64 * 1024}}
    from ldm.daemon import serve

    async def run():
        started = asyncio.Event()
        task = asyncio.create_task(serve(bus_address=bus_address, db_path=tmp_path / "daemon.db",
                                         stop=stop, ready=started, manager_options=options,
                                         system_bus_address=bus_address,  # never the real system bus
                                         manage_autostart=False))          # nor the user's systemd
        await started.wait()
        ready.set()
        await task

    thread = threading.Thread(target=loop.run_until_complete, args=(run(),), daemon=True)
    thread.start()
    assert ready.wait(10), "daemon did not start"
    yield bus_address
    loop.call_soon_threadsafe(stop.set)
    thread.join(10)
