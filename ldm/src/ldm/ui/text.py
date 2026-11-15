"""Text for display and checks of what the user typed or pasted."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..format import human, parse_rate
from ..hlskey import aes_hex
from ..sandbox import host_path
from ..schedule import DAYS, parse_days, parse_time

WINDOW_KEYS = ("schedule_start", "schedule_stop", "schedule_days")


def is_url(text: str) -> bool:
    parts = urlsplit(text.strip())
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def is_torrent_link(text: str) -> bool:
    parts = urlsplit(text.strip())
    if parts.scheme == "magnet":
        return "xt=" in parts.query
    return parts.scheme in ("http", "https", "file") and parts.path.lower().endswith(".torrent")


def looks_like_login(url: str) -> bool:
    """A download link that ended on the site's sign-in page (the site wants a logged-in browser)."""
    return bool(re.search(r"/(log-?in|sign-?in|auth|account/login)(/|$|\?)", urlsplit(url).path.lower() + "/"))


def key_valid(text: str) -> bool:
    """An HLS key field: empty, or a key LDM can use."""
    if not text.strip():
        return True
    try:
        aes_hex(text)
        return True
    except ValueError:
        return False


def parse_limit(text: str) -> int | None:
    """Speed-limit entry text -> bytes/s; '' = unlimited (0); None if invalid."""
    if not text.strip():
        return 0
    try:
        return parse_rate(text)
    except ValueError:
        return None


def limit_text(rate: int) -> str:
    return "0" if not rate else human(rate).replace(" ", "").replace("iB", "")


def short_path(path: str) -> str:
    """For display: ~ for the home folder, and a folder's real location rather than the document portal's
    path for it (folders chosen inside a Flatpak)."""
    path, home = host_path(path), str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def describe_window(start: str, stop: str, days: str) -> str:
    chosen = parse_days(days) if days else set()
    names = "every day" if not chosen or len(chosen) == 7 else " ".join(DAYS[i].capitalize() for i in sorted(chosen))
    overnight = " (overnight)" if parse_time(stop) < parse_time(start) else ""
    return f"{start}–{stop}{overnight}, {names}"


def duration_text(seconds: float | None) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def capture_details(capture: dict[str, Any]) -> str:
    """One line about a video or stream the browser extension caught."""
    kind = {"hls": "HLS stream", "dash": "DASH stream", "video": "Video", "audio": "Audio"}.get(capture.get("kind"), "")
    host = urlsplit(capture["url"]).hostname or ""
    return " · ".join(p for p in (kind, human(capture["size"]) if capture.get("size") else "", host) if p)


def torrent_extra(state: str, peers: int, upload_speed: float, ratio: float) -> str:
    if state == "running":
        return f" · {peers} peer{'s' if peers != 1 else ''}" + (f" · ↑ {human(upload_speed)}/s" if upload_speed else "")
    if state == "seeding":
        return f" · ratio {ratio:.2f}" + (f" · ↑ {human(upload_speed)}/s" if upload_speed else "")
    return ""
