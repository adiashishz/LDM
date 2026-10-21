"""View model: one DownloadItem per download, kept in sync with daemon results and signals."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlsplit

from gi.repository import Gio, GObject, Gtk

from ..filetypes import group_of
from ..format import human, status_text

FINISH_ACTIONS = [("Do Nothing", "nothing"), ("Quit LDM", "quit"), ("Suspend", "suspend"), ("Power Off", "poweroff")]
VIEWS = {
    "all": None,
    "downloading": {"running", "processing", "queued", "paused"},
    "finished": {"done", "seeding"},
    "problems": {"error", "expired", "changed"},
}


class DownloadItem(GObject.Object):
    id = GObject.Property(type=GObject.TYPE_UINT64)
    url = GObject.Property(type=str, default="")
    name = GObject.Property(type=str, default="")
    dir = GObject.Property(type=str, default="")
    state = GObject.Property(type=str, default="queued")
    error = GObject.Property(type=str, default="")
    size = GObject.Property(type=GObject.TYPE_INT64, default=-1)  # -1 = unknown
    done = GObject.Property(type=GObject.TYPE_INT64, default=0)
    speed = GObject.Property(type=float, default=0.0)
    position = GObject.Property(type=GObject.TYPE_INT64, default=0)
    speed_limit = GObject.Property(type=GObject.TYPE_INT64, default=0)
    kind = GObject.Property(type=str, default="http")
    peers = GObject.Property(type=int, default=0)                  # torrents only
    upload_speed = GObject.Property(type=float, default=0.0)
    ratio = GObject.Property(type=float, default=0.0)
    scheduled = GObject.Property(type=bool, default=False)
    schedule_start = GObject.Property(type=str, default="")         # effective window (own or default)
    schedule_stop = GObject.Property(type=str, default="")
    schedule_days = GObject.Property(type=str, default="")
    waiting = GObject.Property(type=str, default="")               # why a queued download isn't starting
    added_at = GObject.Property(type=float, default=0.0)           # unix time
    finished_at = GObject.Property(type=float, default=0.0)        # 0 = not finished
    # Derived, for binding to widgets:
    title = GObject.Property(type=str, default="")
    fraction = GObject.Property(type=float, default=0.0)
    status = GObject.Property(type=str, default="")
    group = GObject.Property(type=str, default="other")            # file type for the list filter

    FIELDS = ("id", "url", "name", "dir", "state", "error", "size", "done", "speed", "position", "speed_limit",
              "kind", "peers", "upload_speed", "ratio", "scheduled",
              "schedule_start", "schedule_stop", "schedule_days", "added_at", "finished_at")

    def update(self, values: dict[str, Any]) -> None:
        self.freeze_notify()
        try:
            for key in self.FIELDS:
                if key in values and values[key] is not None and self.get_property(key) != values[key]:
                    self.set_property(key, values[key])
            if values.get("state") is not None and values["state"] != "running":
                self.speed = 0.0
            if "id" in values:  # a full row from Get/List: no "waiting" key means not waiting
                self.waiting = values.get("waiting") or ""
            if self.state == "done" and self.size >= 0:
                self.done = self.size
            self.title = self.name or unquote(urlsplit(self.url).path.rsplit("/", 1)[-1]) or self.url
            self.group = group_of(self.kind, self.name, self.url)
            self.fraction = self.done / self.size if self.size > 0 else 0.0
            self.status = status_text(self.state, self.done, self.size if self.size >= 0 else None,
                                      self.speed, self.error, self.waiting)
            if self.kind == "torrent":
                self.status += torrent_extra(self.state, self.peers, self.upload_speed, self.ratio)
        finally:
            self.thaw_notify()

    @property
    def path(self) -> str | None:
        return f"{self.dir}/{self.name}" if self.dir and self.name else None


def torrent_extra(state: str, peers: int, upload_speed: float, ratio: float) -> str:
    if state == "running":
        return f" · {peers} peer{'s' if peers != 1 else ''}" + (f" · ↑ {human(upload_speed)}/s" if upload_speed else "")
    if state == "seeding":
        return f" · ratio {ratio:.2f}" + (f" · ↑ {human(upload_speed)}/s" if upload_speed else "")
    return ""


def _order(a: DownloadItem, b: DownloadItem, *_) -> int:
    ka, kb = (a.position, a.id), (b.position, b.id)
    return (ka > kb) - (ka < kb)


SORTS = {  # key -> (label, compare); every list is in queue order unless sorted otherwise
    "queue": ("Queue order", _order),
    "name": ("Name", lambda a, b, *_: _cmp(a.title.casefold(), b.title.casefold()) or _order(a, b)),
    "size": ("Size", lambda a, b, *_: _cmp(b.size, a.size) or _order(a, b)),
    "added": ("Date added", lambda a, b, *_: _cmp(b.added_at, a.added_at) or _order(a, b)),
    "progress": ("Progress", lambda a, b, *_: _cmp(b.fraction, a.fraction) or _order(a, b)),
}


def _cmp(a, b) -> int:
    return (a > b) - (a < b)


class DownloadList(GObject.Object):
    """`store` is a Gio.ListStore of DownloadItem sorted by queue position; views filter it by state and by
    the shared query (search text, file type, added since) and sort it by `sort_key`."""

    def __init__(self):
        super().__init__()
        self.store = Gio.ListStore(item_type=DownloadItem)
        self._items: dict[int, DownloadItem] = {}
        self._filters: list[Gtk.CustomFilter] = []
        self._sorters: list[Gtk.CustomSorter] = []
        self.text = ""
        self.group = ""        # "" = all types
        self.since = 0.0       # unix time; 0 = any date
        self.sort_key = "queue"

    def __len__(self) -> int:
        return len(self._items)

    def get(self, download_id: int) -> DownloadItem | None:
        return self._items.get(download_id)

    def urls(self) -> list[str]:
        return [item.url for item in self._items.values()]

    def load(self, rows: list[dict[str, Any]]) -> None:
        self.store.remove_all()
        self._items.clear()
        for row in rows:
            self.upsert(row)

    def upsert(self, row: dict[str, Any]) -> DownloadItem:
        item = self._items.get(row["id"])
        if item is None:
            item = DownloadItem()
            item.update(row)
            self._items[item.id] = item
            self.store.insert_sorted(item, _order)
        else:
            old_order = (item.position, item.id)
            item.update(row)
            if (item.position, item.id) != old_order:
                self._resort(item)
        self._filters_changed()
        return item

    def remove(self, download_id: int) -> None:
        item = self._items.pop(download_id, None)
        if item is not None:
            found, index = self.store.find(item)
            if found:
                self.store.remove(index)

    def apply_state(self, download_id: int, state: str, error: str) -> None:
        item = self._items.get(download_id)
        if item is not None:
            item.update({"state": state, "error": error})
            self._filters_changed()

    def apply_progress(self, batch: list[tuple[int, int, int, float]]) -> None:
        for download_id, done, size, speed in batch:
            item = self._items.get(download_id)
            if item is not None:
                item.update({"done": done, "size": size, "speed": speed})

    def view(self, name: str) -> Gtk.SortListModel:
        """A filtered, sorted view of the store for one of VIEWS."""
        states = VIEWS[name]
        custom = Gtk.CustomFilter.new(lambda item: (states is None or item.state in states) and self.matches(item))
        self._filters.append(custom)
        sorter = Gtk.CustomSorter.new(lambda a, b, *_: SORTS[self.sort_key][1](a, b))
        self._sorters.append(sorter)
        return Gtk.SortListModel.new(Gtk.FilterListModel.new(self.store, custom), sorter)

    def matches(self, item: DownloadItem) -> bool:
        if self.group and item.group != self.group:
            return False
        if self.since and item.added_at < self.since:
            return False
        text = self.text.casefold()
        return not text or text in item.title.casefold() or text in item.url.casefold()

    def set_query(self, text: str | None = None, group: str | None = None, since: float | None = None) -> None:
        if text is not None:
            self.text = text.strip()
        if group is not None:
            self.group = group
        if since is not None:
            self.since = since
        self._filters_changed()

    def set_sort(self, key: str) -> None:
        self.sort_key = key
        for sorter in self._sorters:
            sorter.changed(Gtk.SorterChange.DIFFERENT)

    def _resort(self, item: DownloadItem) -> None:
        found, index = self.store.find(item)
        if found:
            self.store.remove(index)
        self.store.insert_sorted(item, _order)

    def _filters_changed(self) -> None:
        for custom in self._filters:
            custom.changed(Gtk.FilterChange.DIFFERENT)
