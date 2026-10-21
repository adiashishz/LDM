"""Human-readable sizes, rates and download status, shared by the CLI and the GUI."""

from __future__ import annotations

import re

STATE_LABELS = {
    "queued": "Queued",
    "running": "Downloading",
    "processing": "Processing",
    "seeding": "Seeding",
    "paused": "Paused",
    "done": "Finished",
    "error": "Failed",
    "expired": "Link expired",
    "changed": "File changed on server",
}


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1000:  # switch units early: "1.0 MiB", not "1023.0 KiB"
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def parse_rate(text: str) -> int:
    """'500K', '2M', '1G' (binary units) or plain bytes per second; 0 = unlimited."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMG]?)(?:i?B)?(?:/s)?", text.strip(), re.IGNORECASE)
    if not match:
        raise ValueError(f"invalid rate: {text}")
    factor = 1024 ** " KMG".index(match.group(2).upper() or " ")
    return int(float(match.group(1)) * factor)


def eta_text(remaining: int, speed: float) -> str:
    if speed <= 0 or remaining <= 0:
        return ""
    seconds = int(remaining / speed)
    if seconds < 60:
        return f"{seconds} s left"
    if seconds < 3600:
        return f"{seconds // 60} min left"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min left"


def progress_line(done: int, size: int | None, speed: float) -> str:
    """One-line terminal progress."""
    if size and size > 0:
        eta = f"{(size - done) / speed:.0f}s" if speed > 0 else "--"
        return f"{done * 100 / size:5.1f}%  {human(done)} / {human(size)}  {human(speed)}/s  ETA {eta}"
    return f"{human(done)}  {human(speed)}/s"


def status_text(state: str, done: int, size: int | None, speed: float = 0.0, error: str | None = None) -> str:
    """Status line for a download row."""
    amount = f"{human(done)} of {human(size)}" if size else human(done)
    if state == "running":
        parts = [f"{done * 100 // size}%" if size else None, amount, f"{human(speed)}/s",
                 eta_text(size - done, speed) if size else ""]
        return " · ".join(p for p in parts if p)
    if state == "processing":
        return f"{error}…" if error else "Processing…"  # detail such as "Merging audio and video"
    if state == "seeding":
        return "Seeding"
    if state == "done":
        return f"Finished — {human(size or done)}"
    if state in ("queued", "paused"):
        return f"{STATE_LABELS[state]} — {amount}" if done else STATE_LABELS[state]
    if state == "error":
        return f"Failed: {error}" if error else "Failed"
    if state == "expired":
        return "Link expired — set a new link"
    if state == "changed":
        return "File changed on server — resuming starts over"
    return STATE_LABELS.get(state, state)
