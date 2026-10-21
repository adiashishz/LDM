"""HTTP download engine: probe, multi-part transfer into one pre-allocated file, resume.

Design notes (see notes/10-linux-design.md):
- One destination file written with pwrite at each part's offset; no part files, no merge step.
- Dynamic segmentation: an idle connection splits the part with the most bytes left.
- Progress is persisted every `persist_interval` seconds after an fsync, so a crash never records
  bytes that are not on disk.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Awaitable, Callable, Sequence
from urllib.parse import unquote, urlsplit

import httpx

from .ratelimit import TokenBucket
from .segments import Part, split_even, steal
from .store import DownloadRow, Store

UNKNOWN_LAST = 1 << 62  # `last` of the single part when the size is unknown; EOF ends it
DEFAULT_USER_AGENT = "ldm/0.1"

_CONTENT_RANGE = re.compile(r"bytes\s+(?:(\d+)-(\d+)|\*)/(\d+|\*)")

ProgressCallback = Callable[[int, int | None, float], None]


class DownloadError(Exception):
    pass


class LinkExpired(DownloadError):
    """The server refused the URL (401/403/404/410); a fresh link is needed."""


class RemoteChanged(DownloadError):
    """The remote file no longer matches the partial download."""


class _Retryable(Exception):
    pass


@dataclass
class ProbeResult:
    final_url: str
    size: int | None
    resumable: bool
    filename: str
    etag: str | None
    last_modified: str | None
    content_type: str = ""


def sanitize_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\0", "").strip().lstrip(".")
    name = name.encode()[:240].decode(errors="ignore")
    return name or "download"


def filename_for(response: httpx.Response) -> str:
    name = None
    disposition = response.headers.get("content-disposition")
    if disposition:
        msg = Message()
        msg["content-disposition"] = disposition
        name = msg.get_filename()
    if not name:
        name = unquote(Path(urlsplit(str(response.url)).path).name)
    return sanitize_filename(name)


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    n = 1
    while (candidate := path.with_name(f"{path.stem} ({n}){path.suffix}")).exists():
        n += 1
    return candidate


def new_client(headers: dict[str, str] | None = None, max_connections: int = 2) -> httpx.AsyncClient:
    """HTTP client with ldm's defaults; `headers` (Referer, Cookie, …) override them."""
    return httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": DEFAULT_USER_AGENT, "Accept-Encoding": "identity", **(headers or {})},
        timeout=httpx.Timeout(30.0, connect=15.0),
        limits=httpx.Limits(max_connections=max_connections),
    )


def _check_status(r: httpx.Response) -> None:
    if r.status_code in (401, 403, 404, 410):
        raise LinkExpired(f"HTTP {r.status_code}")
    if r.status_code == 429 or r.status_code >= 500:
        raise _Retryable(f"HTTP {r.status_code}")


async def probe(client: httpx.AsyncClient, url: str) -> ProbeResult:
    """GET with `Range: bytes=0-`: 206 means the server supports ranges (splittable, resumable)."""
    async with client.stream("GET", url, headers={"Range": "bytes=0-"}) as r:
        _check_status(r)
        size, resumable = None, False
        content_range = _CONTENT_RANGE.match(r.headers.get("content-range", ""))
        if r.status_code == 206 and content_range and content_range.group(3) != "*":
            size, resumable = int(content_range.group(3)), True
        elif r.status_code == 200:
            if "content-length" in r.headers:
                size = int(r.headers["content-length"])
        elif r.status_code == 416 and content_range and content_range.group(3) == "0":
            size = 0  # empty file
        else:
            raise DownloadError(f"unexpected HTTP {r.status_code} while probing")
        return ProbeResult(str(r.url), size, resumable, filename_for(r), r.headers.get("etag"),
                           r.headers.get("last-modified"),
                           r.headers.get("content-type", "").split(";")[0].strip())


class Downloader:
    """Runs one download to completion (or until cancelled, which pauses it)."""

    def __init__(self, store: Store, download_id: int, *, min_part: int = 1 << 20,
                 chunk_size: int = 1 << 16, persist_interval: float = 2.0, max_retries: int = 5,
                 retry_base_delay: float = 0.5, on_progress: ProgressCallback | None = None,
                 limiters: Sequence[TokenBucket] = (), connections: int | None = None,
                 progress_interval: float = 0.5):
        """`limiters` throttle every received chunk; `connections` caps the download's own setting."""
        self.store = store
        self.id = download_id
        self.limiters = limiters
        self.max_connections = connections
        self.min_part = min_part
        self.chunk_size = chunk_size
        self.persist_interval = persist_interval
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.on_progress = on_progress
        self.progress_interval = progress_interval
        self.parts: list[Part] = []
        self._refresh_lock = asyncio.Lock()

    def _connections(self, d: DownloadRow) -> int:
        return min(d.connections, self.max_connections or d.connections)

    async def run(self) -> Path:
        d = self.store.get(self.id)
        self.store.set_state(self.id, "running")
        try:
            async with new_client(d.headers, self._connections(d) + 1) as client:
                self.client = client
                return await self._run(d)
        except asyncio.CancelledError:
            self.store.set_state(self.id, "paused")
            raise
        except LinkExpired as e:
            self.store.set_state(self.id, "expired", str(e))
            raise
        except RemoteChanged as e:
            self.store.set_state(self.id, "changed", str(e))
            raise
        except Exception as e:
            self.store.set_state(self.id, "error", str(e))
            raise

    async def _run(self, d: DownloadRow) -> Path:
        parts = self.store.load_parts(self.id)
        fresh = not parts and d.final_url is None
        if fresh:
            p = await self._retrying(lambda: probe(self.client, d.url))
            d = self.store.update(self.id, final_url=p.final_url, size=p.size, resumable=p.resumable,
                                  name=d.name or p.filename, etag=p.etag, last_modified=p.last_modified)
            parts = self._initial_parts(d)
        d.dir.mkdir(parents=True, exist_ok=True)
        if not fresh and not d.temp_path.exists():
            parts = self._initial_parts(d)  # downloaded data was removed: start over
        if not d.resumable:
            parts = self._initial_parts(d)  # cannot continue mid-file

        self.d, self.parts = d, parts
        self.store.save_parts(self.id, self._snapshot())

        fd = os.open(d.temp_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if not d.resumable:
                os.ftruncate(fd, 0)
            if d.size and os.fstat(fd).st_size < d.size:
                _preallocate(fd, d.size)
            self.fd = fd
            await self._transfer()
            size = d.size if d.size is not None else self.parts[0].done
            os.ftruncate(fd, size)
            os.fsync(fd)
        finally:
            os.close(fd)

        final = unique_path(d.dest)
        os.rename(d.temp_path, final)
        self.store.finish(self.id, final.name, size)
        return final

    def _initial_parts(self, d: DownloadRow) -> list[Part]:
        if d.size == 0:
            return []
        if d.size is None:
            return [Part(0, UNKNOWN_LAST)]
        if d.resumable:
            return split_even(d.size, self._connections(d), self.min_part)
        return [Part(0, d.size - 1)]

    async def _transfer(self) -> None:
        workers = self._connections(self.d) if self.d.resumable else 1
        background = [asyncio.create_task(self._persist_loop())]
        if self.on_progress:
            background.append(asyncio.create_task(self._progress_loop()))
        try:
            async with asyncio.TaskGroup() as tg:
                for _ in range(workers):
                    tg.create_task(self._worker())
        except BaseExceptionGroup as group:
            raise group.exceptions[0] from None
        finally:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            self._flush()

    async def _worker(self) -> None:
        while (part := self._next_part()) is not None:
            part.active = True
            try:
                await self._fetch(part)
            finally:
                part.active = False

    def _next_part(self) -> Part | None:
        for part in self.parts:
            if not part.active and not part.complete:
                return part
        if self.d.resumable:
            return steal(self.parts, self.min_part)
        return None

    async def _fetch(self, part: Part) -> None:
        attempt = 0
        refreshed = False
        while not part.complete:
            before = part.done
            url = self.d.final_url
            try:
                await self._stream(part)
            except LinkExpired:
                # The stored redirect target (often a signed CDN URL) may have expired while the
                # original URL is still valid: resolve it again once.
                if refreshed or not url or url == self.d.url:
                    raise
                refreshed = True
                await self._refresh_final_url(url)
            except (httpx.TransportError, _Retryable) as e:
                attempt = 1 if part.done > before else attempt + 1
                if attempt > self.max_retries:
                    raise DownloadError(f"bytes {part.first}-{part.last}: {e}") from e
                if not self.d.resumable:
                    part.done = 0
                await asyncio.sleep(self.retry_base_delay * 2 ** (attempt - 1))

    async def _stream(self, part: Part) -> None:
        headers = {}
        if self.d.resumable:
            headers["Range"] = f"bytes={part.pos}-{part.last}"
            validator = self._validator()
            if validator:
                headers["If-Range"] = validator
        async with self.client.stream("GET", self.d.final_url or self.d.url, headers=headers) as r:
            _check_status(r)
            if self.d.resumable:
                if r.status_code == 200:
                    raise RemoteChanged("server sent the whole file instead of the requested range")
                content_range = _CONTENT_RANGE.match(r.headers.get("content-range", ""))
                if r.status_code != 206 or not content_range or int(content_range.group(1)) != part.pos:
                    raise DownloadError(f"unexpected response to range request: HTTP {r.status_code}")
            elif r.status_code != 200:
                raise DownloadError(f"unexpected HTTP {r.status_code}")
            async for chunk in r.aiter_raw(self.chunk_size):
                room = part.last - part.pos + 1  # `last` shrinks when another connection steals
                if room <= 0:
                    return
                data = chunk[:room]
                os.pwrite(self.fd, data, part.pos)
                part.done += len(data)
                if len(data) < len(chunk):
                    return
                for limiter in self.limiters:
                    await limiter.consume(len(data))
        if part.last == UNKNOWN_LAST:
            part.last = part.pos - 1  # unknown size: EOF is the end
        elif not part.complete:
            raise _Retryable("connection closed before the range was complete")

    async def _refresh_final_url(self, failed_url: str) -> None:
        async with self._refresh_lock:
            if self.d.final_url != failed_url:
                return  # another connection already refreshed it
            p = await self._retrying(lambda: probe(self.client, self.d.url))
            if p.size != self.d.size:
                raise LinkExpired("the original URL now points to a different file")
            self.d = self.store.update(self.id, final_url=p.final_url)

    def _validator(self) -> str | None:
        """If-Range needs a strong ETag; fall back to Last-Modified."""
        if self.d.etag and not self.d.etag.startswith("W/"):
            return self.d.etag
        return self.d.last_modified

    async def _retrying(self, make: Callable[[], Awaitable]) -> object:
        for attempt in range(self.max_retries + 1):
            try:
                return await make()
            except (httpx.TransportError, _Retryable) as e:
                if attempt == self.max_retries:
                    raise DownloadError(str(e)) from e
                await asyncio.sleep(self.retry_base_delay * 2 ** attempt)

    def _snapshot(self) -> list[tuple[int, int, int]]:
        return [(p.first, p.last, p.done) for p in self.parts]

    def _flush(self) -> None:
        snapshot = self._snapshot()
        os.fsync(self.fd)
        self.store.save_parts(self.id, snapshot)

    async def _persist_loop(self) -> None:
        while True:
            await asyncio.sleep(self.persist_interval)
            snapshot = self._snapshot()  # taken before fsync: never claims unsynced bytes
            await asyncio.to_thread(os.fsync, self.fd)
            self.store.save_parts(self.id, snapshot)

    async def _progress_loop(self) -> None:
        last_done, last_time = self.done, time.monotonic()
        while True:
            await asyncio.sleep(self.progress_interval)
            now, done = time.monotonic(), self.done
            self.on_progress(done, self.d.size, (done - last_done) / (now - last_time))
            last_done, last_time = done, now

    @property
    def done(self) -> int:
        return sum(p.done for p in self.parts)


def _preallocate(fd: int, size: int) -> None:
    try:
        os.posix_fallocate(fd, 0, size)
    except OSError:
        os.ftruncate(fd, size)  # filesystem without fallocate support: sparse file
