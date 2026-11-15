"""One download as the windows show it, kept in sync with the service's rows and signals."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlsplit

from ..filetypes import group_of
from ..format import STATE_LABELS, eta_text, human, status_text
from .text import torrent_extra

ACTIVE = {"running", "processing", "seeding"}
PROBLEMS = {"error", "expired", "changed"}

# Library views in the sidebar: name -> (label, test)
LIBRARY = {
    "all": ("All downloads", lambda d: True),
    "downloading": ("Downloading", lambda d: d.state in {"running", "processing", "queued", "paused"}
                    and not d.on_schedule),
    "scheduled": ("Scheduled", lambda d: d.on_schedule),
    "finished": ("Finished", lambda d: d.state in {"done", "seeding"}),
    "problems": ("Needs attention", lambda d: d.state in PROBLEMS),
}
# Sidebar categories, in the design's order: group -> label
CATEGORIES = {"archive": "Archives", "document": "Documents", "audio": "Music", "program": "Programs",
              "video": "Videos", "image": "Pictures", "torrent": "Torrents", "other": "Other"}


class Download:
    FIELDS = ("id", "url", "name", "dir", "state", "error", "size", "done", "speed", "position", "speed_limit",
              "kind", "peers", "upload_speed", "ratio", "scheduled", "schedule_start", "schedule_stop",
              "schedule_days", "added_at", "finished_at", "resumable", "connections", "quality")

    def __init__(self, row: dict[str, Any] | None = None):
        self.id = 0
        self.url = self.name = self.dir = self.error = self.waiting = self.quality = ""
        self.state = "queued"
        self.kind = "http"
        self.size = -1  # unknown
        self.done = self.position = self.speed_limit = self.peers = self.connections = 0
        self.speed = self.upload_speed = self.ratio = self.added_at = self.finished_at = 0.0
        self.scheduled = self.resumable = False
        self.schedule_start = self.schedule_stop = self.schedule_days = ""
        self.title = self.status = ""
        self.group = "other"
        self.fraction = 0.0
        if row:
            self.update(row)

    def update(self, values: dict[str, Any]) -> None:
        for key in self.FIELDS:
            if values.get(key) is not None:
                setattr(self, key, values[key])
        if values.get("state") is not None and values["state"] != "running":
            self.speed = 0.0
        if "id" in values:  # a full row from Get/List: no "waiting" key means not waiting
            self.waiting = values.get("waiting") or ""
        if self.state == "done" and self.size >= 0:
            self.done = self.size
        self.title = self.name or unquote(urlsplit(self.url).path.rsplit("/", 1)[-1]) or self.url
        self.group = group_of(self.kind, self.name, self.url)
        self.fraction = min(self.done / self.size, 1.0) if self.size > 0 else 0.0
        self.status = status_text(self.state, self.done, self.size if self.size >= 0 else None,
                                  self.speed, self.error, self.waiting)
        if self.kind == "torrent":
            self.status += torrent_extra(self.state, self.peers, self.upload_speed, self.ratio)

    # --- for display ------------------------------------------------------------------------

    @property
    def path(self) -> str | None:
        return f"{self.dir}/{self.name}" if self.dir and self.name else None

    @property
    def on_schedule(self) -> bool:
        return self.scheduled and self.state not in {"done", "seeding"}

    @property
    def extension(self) -> str:
        """Up to four letters for the file tile: "ZIP", "MP4", "TOR"."""
        if self.kind == "torrent":
            return "TOR"
        stem, dot, suffix = self.title.rpartition(".")
        return suffix[:4].upper() if dot and stem and suffix.isalnum() else ("VID" if self.kind == "media" else "")

    @property
    def size_text(self) -> str:
        return human(self.size) if self.size >= 0 else ""

    @property
    def speed_text(self) -> str:
        return f"{human(self.speed)}/s" if self.state == "running" and self.speed > 0 else ""

    @property
    def left_text(self) -> str:
        if self.state != "running" or self.size <= 0:
            return ""
        return eta_text(self.size - self.done, self.speed).removesuffix(" left")

    @property
    def state_word(self) -> str:
        """The short word beside the progress bar."""
        if self.state == "running":
            return f"{int(self.fraction * 100)}%" if self.size > 0 else human(self.done)
        if self.state == "queued" and self.on_schedule:
            return "Scheduled"
        return {"done": "Done", "error": "Failed", "expired": "Expired", "changed": "Changed"}.get(
            self.state, STATE_LABELS.get(self.state, self.state))

    @property
    def tone(self) -> str:
        """How to colour the bar: active, muted, done or bad."""
        if self.state in PROBLEMS:
            return "bad"
        if self.state in {"done", "seeding"}:
            return "done"
        return "active" if self.state in ACTIVE else "muted"


def when_text(timestamp: float, now: datetime | None = None) -> str:
    """"Today 10:42", "Yesterday", "Mon 10:42" (this week) or "3 Nov"."""
    if not timestamp:
        return ""
    now = now or datetime.now()
    when = datetime.fromtimestamp(timestamp)
    days = (now.date() - when.date()).days
    if days == 0:
        return when.strftime("Today %H:%M")
    if days == 1:
        return "Yesterday"
    if 1 < days < 7:
        return when.strftime("%a %H:%M")
    return f"{when.day} {when.strftime('%b')}" + (f" {when.year}" if when.year != now.year else "")
