"""Video/audio streams and site pages, downloaded with yt-dlp.

Plain files use ldm's own engine (engine.py). Everything yt-dlp handles better — HLS/DASH playlists
(incl. AES-128), sites such as YouTube, separate audio+video merged with FFmpeg, subtitles — goes
through MediaDownloader, which has the same interface as engine.Downloader so the manager can queue,
pause and resume both alike.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.utils import DownloadCancelled
from yt_dlp.utils import DownloadError as YtDlpError

from .engine import DownloadError, LinkExpired, ProgressCallback
from .ratelimit import TokenBucket
from .store import DownloadRow, Store

log = logging.getLogger(__name__)

RUNTIMES = ("deno", "node", "bun")
PROCESSORS = {"Merger": "Merging audio and video", "FFmpegVideoRemuxer": "Converting",
              "FFmpegVideoConvertor": "Converting", "FFmpegFixupM3u8": "Fixing the stream",
              "FFmpegFixupM4a": "Fixing the audio", "FFmpegSubtitlesConvertor": "Converting subtitles"}


# --- configuration -------------------------------------------------------------------------

def find_js_runtime(configured: str = "") -> tuple[str, str] | None:
    """(name, path) of a JavaScript runtime for YouTube's challenges, or None.

    The daemon is started by D-Bus/systemd, so it can't rely on the user's shell PATH (nvm, bun
    and deno install into the home directory): look in the usual install locations too.
    """
    home = Path.home()
    candidates = [configured] if configured else []
    candidates += [shutil.which(name) or "" for name in RUNTIMES]
    candidates += [str(home / ".deno/bin/deno"), str(home / ".bun/bin/bun")]
    candidates += sorted(glob.glob(str(home / ".nvm/versions/node/*/bin/node")), key=_node_version, reverse=True)
    for path in filter(None, candidates):
        name = Path(path).name.removesuffix("js") if Path(path).name == "nodejs" else Path(path).name
        if name in RUNTIMES and os.access(path, os.X_OK):
            return name, path
    return None


def _node_version(path: str) -> tuple[int, ...]:
    match = re.search(r"/v(\d+)\.(\d+)\.(\d+)/", path)
    return tuple(int(n) for n in match.groups()) if match else (0,)


class _Logger:
    """Route yt-dlp's messages to logging instead of stdout/stderr."""

    def debug(self, message):
        if not message.startswith("[debug] "):
            log.debug(message)

    def info(self, message):
        log.debug(message)

    def warning(self, message):
        log.info("yt-dlp: %s", message)

    def error(self, message):
        log.info("yt-dlp: %s", message)


def base_options(settings: dict[str, Any], headers: dict[str, str] | None) -> dict[str, Any]:
    options = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
               "logger": _Logger(), "http_headers": dict(headers or {})}
    runtime = find_js_runtime(settings.get("js_runtime", ""))
    if runtime:
        options["js_runtimes"] = {runtime[0]: {"path": runtime[1]}}
    return options


def default_selector(quality: Any) -> str:
    if quality == "audio":
        return "ba/b"
    if isinstance(quality, int) or str(quality).isdigit():
        return f"bv*[height<={int(quality)}]+ba/b[height<={int(quality)}]/b"
    return "bv*+ba/b"


def _error(exc: Exception) -> DownloadError:
    message = re.sub(r"^ERROR: (\[[^\]]+\] )?", "", str(exc)).strip()
    if re.search(r"HTTP Error (401|403|404|410)", message):
        return LinkExpired(message)
    if "Unsupported URL" in message:
        return DownloadError("this site or link is not supported by yt-dlp")
    return DownloadError(message)


# --- probing -------------------------------------------------------------------------------

def _size(fmt: dict | None, duration: float | None = None) -> int | None:
    """Exact or approximate size; else bitrate × duration."""
    fmt = fmt or {}
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if not size and fmt.get("tbr") and duration:
        size = int(fmt["tbr"] * 1000 / 8 * duration)
    return size


STANDARD_HEIGHTS = (144, 240, 360, 480, 720, 1080, 1440, 2160, 4320)


def _quality_name(fmt: dict) -> str:
    """Usual name ("1080p") even for widescreen video whose pixel height is e.g. 1012."""
    note = re.match(r"(\d{3,4}p)", fmt.get("format_note") or "")
    if note:
        return note.group(1)
    height, width = fmt["height"], fmt.get("width") or 0
    short, long = min(height, width or height), max(height, width)
    base = max(short, round(long * 9 / 16))  # 16:9 equivalent: 3840x2026 -> 2160, 1080x1920 -> 1080
    nearest = min(STANDARD_HEIGHTS, key=lambda h: abs(h - base))
    return f"{nearest}p" if abs(nearest - base) <= nearest * 0.15 else f"{height}p"


def build_choices(formats: list[dict], duration: float | None = None) -> list[dict[str, Any]]:
    """Quality presets like 1DM's list: Best, each resolution, Audio only — with estimated sizes."""
    videos = [f for f in formats if f.get("height") and f.get("vcodec") != "none"]
    audios = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    best_audio = max(audios, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
    choices = []
    for height in sorted({f["height"] for f in videos}, reverse=True):
        best = max((f for f in videos if f["height"] == height), key=lambda f: f.get("tbr") or 0)
        name = _quality_name(best)
        if choices and choices[-1]["label"].split()[0] == name:
            continue  # same named quality at another pixel height
        size = _size(best, duration)
        if size and best.get("acodec") in (None, "none") and _size(best_audio, duration):
            size += _size(best_audio, duration)
        fps = f" {int(best['fps'])}fps" if (best.get("fps") or 0) > 30 else ""
        choices.append({"id": name.removesuffix("p"), "label": f"{name}{fps}",
                        "selector": default_selector(height), "size": size})
    best_choice = {"id": "best", "label": "Best quality", "selector": "bv*+ba/b",
                   "size": choices[0]["size"] if choices else _size(max(formats, key=lambda f: f.get("tbr") or 0,
                                                                         default=None), duration)}
    choices.insert(0, best_choice)
    if best_audio:
        choices.append({"id": "audio", "label": "Audio only", "selector": "ba/b", "size": _size(best_audio, duration)})
    return choices


def probe_media(url: str, headers: dict[str, str] | None, settings: dict[str, Any]) -> dict[str, Any]:
    """What yt-dlp can download from `url` (blocking; run in a thread)."""
    try:
        with yt_dlp.YoutubeDL(base_options(settings, headers)) as ydl:
            info = ydl.extract_info(url, download=False)
    except YtDlpError as e:
        raise _error(e) from e
    if info.get("_type") == "playlist" or info.get("entries"):
        raise DownloadError("playlists are not supported yet — open a single video")
    formats = info.get("formats") or [info]
    return {
        "title": info.get("title") or Path(urlsplit(url).path).stem or "video",
        "site": info.get("extractor_key") or urlsplit(url).hostname or "",
        "duration": info.get("duration"),
        "is_live": bool(info.get("is_live")),
        "thumbnail": info.get("thumbnail") or "",
        "subtitles": sorted(k for k in (info.get("subtitles") or {}) if k != "live_chat"),
        "choices": build_choices(formats, info.get("duration")),
        "webpage_url": info.get("webpage_url") or url,
    }


# --- downloading ---------------------------------------------------------------------------

def media_temp_dir(d: DownloadRow) -> Path:
    return d.dir / f".ldm-media-{d.id}"


class MediaDownloader:
    """Runs one yt-dlp download; cancelling the task pauses it (yt-dlp resumes from its .part files)."""

    def __init__(self, store: Store, download_id: int, *, on_progress: ProgressCallback | None = None,
                 limiters: Sequence[TokenBucket] = (), connections: int | None = None,
                 on_state: Callable[[str], None] | None = None, progress_interval: float = 0.5,
                 **_engine_options):
        self.store = store
        self.id = download_id
        self.on_progress = on_progress
        self.limiters = limiters
        self.max_connections = connections
        self.on_state = on_state
        self.progress_interval = progress_interval
        self._stop = threading.Event()

    async def run(self) -> Path:
        d = self.store.get(self.id)
        self.store.set_state(self.id, "running")
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, self._download, d, self.store.get_settings(), loop)
        try:
            path = await asyncio.shield(future)
        except asyncio.CancelledError:
            self._stop.set()
            with contextlib.suppress(BaseException):
                await future
            self.store.set_state(self.id, "paused")
            raise
        except LinkExpired as e:
            self.store.set_state(self.id, "expired", str(e))
            raise
        except Exception as e:
            self.store.set_state(self.id, "error", str(e))
            raise
        self.store.finish(self.id, path.name, path.stat().st_size)
        if path.parent != d.dir:
            self.store.update(self.id, dir=str(path.parent))
        return path

    def _rate_limit(self, d: DownloadRow) -> int | None:
        rates = [bucket.rate for bucket in self.limiters if bucket.rate] + ([d.speed_limit] if d.speed_limit else [])
        return int(min(rates)) if rates else None

    def _output_template(self, d: DownloadRow) -> str:
        if d.options.get("custom_name") and d.name:
            stem = Path(d.name).stem if Path(d.name).suffix else d.name
            return stem.replace("%", "%%") + ".%(ext)s"
        return "%(title).180B [%(id)s].%(ext)s"

    def _download(self, d: DownloadRow, settings: dict[str, Any], loop: asyncio.AbstractEventLoop) -> Path:
        files: dict[str, tuple[int, int]] = {}
        last_report = [0.0]

        def progress(event: dict) -> None:
            # Pause only mid-transfer: stopping in a "finished" event (a fragment fully downloaded but
            # not yet appended) leaves state yt-dlp cannot resume ("The downloaded file is empty").
            if self._stop.is_set() and event.get("status") == "downloading":
                raise DownloadCancelled("paused")
            if event.get("status") not in ("downloading", "finished"):
                return
            name = event.get("filename") or ""
            total = event.get("total_bytes") or event.get("total_bytes_estimate") or 0
            done = event.get("downloaded_bytes") or (total if event["status"] == "finished" else 0)
            files[name] = (int(done), int(total))
            now = time.monotonic()
            if self.on_progress and (now - last_report[0] >= self.progress_interval or event["status"] == "finished"):
                last_report[0] = now
                done_sum = sum(f[0] for f in files.values())
                size_sum = sum(f[1] for f in files.values()) or None
                loop.call_soon_threadsafe(self.on_progress, done_sum, size_sum, float(event.get("speed") or 0))

        def processing(event: dict) -> None:
            if event.get("status") == "started" and event.get("postprocessor") in PROCESSORS:
                loop.call_soon_threadsafe(self._set_processing, PROCESSORS[event["postprocessor"]])

        options = base_options(settings, d.headers)
        subtitles = d.options.get("subtitles", "")
        options.update({
            "format": d.options.get("format") or default_selector(settings["media_quality"]),
            "outtmpl": {"default": self._output_template(d)},
            # Partial files live in a private folder, so cleanup never touches other downloads.
            "paths": {"home": str(d.dir), "temp": str(media_temp_dir(d))},
            "concurrent_fragment_downloads": min(d.connections, self.max_connections or d.connections),
            "continuedl": True, "retries": 10, "fragment_retries": 10,
            "merge_output_format": settings["media_container"],
            "ratelimit": self._rate_limit(d),
            "writesubtitles": bool(subtitles), "subtitleslangs": [s for s in subtitles.split(",") if s],
            "progress_hooks": [progress], "postprocessor_hooks": [processing],
        })
        d.dir.mkdir(parents=True, exist_ok=True)
        for attempt in (1, 2):
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(d.url, download=True)
                break
            except DownloadCancelled:
                raise asyncio.CancelledError
            except YtDlpError as e:
                if self._stop.is_set():
                    raise asyncio.CancelledError
                if attempt == 1 and "downloaded file is empty" in str(e) and self._discard_partial(d):
                    log.info("download %s: partial files could not be resumed, starting over", d.id)
                    continue
                raise _error(e) from e
        shutil.rmtree(media_temp_dir(d), ignore_errors=True)  # leftovers such as fragments/thumbnails
        downloads = info.get("requested_downloads") or [info]
        path = downloads[0].get("filepath") or downloads[0].get("_filename")
        if not path or not Path(path).exists():
            raise DownloadError("yt-dlp finished but the output file is missing")
        return Path(path)

    @staticmethod
    def _discard_partial(d: DownloadRow) -> bool:
        """Remove this download's private temp folder (yt-dlp resume files); True if it existed."""
        temp = media_temp_dir(d)
        existed = temp.exists()
        shutil.rmtree(temp, ignore_errors=True)
        return existed

    def _set_processing(self, label: str) -> None:
        self.store.set_state(self.id, "processing", label)
        if self.on_state:
            self.on_state("processing")
