"""Local HTTP test server with byte-range support and fault injection."""

from __future__ import annotations

import re
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
    bytes_sent: int = 0
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
        if cfg.status is not None:
            self.send_response(cfg.status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        data, size = cfg.data, len(cfg.data)
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
        self.send_response(206 if partial else 200)
        if cfg.ranges:
            self.send_header("Accept-Ranges", "bytes")
        if cfg.etag:
            self.send_header("ETag", cfg.etag)
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if cfg.disposition:
            self.send_header("Content-Disposition", cfg.disposition)
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
        return cfg, f"http://127.0.0.1:{server.server_port}/files/test.bin"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()
