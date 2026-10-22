"""Selection mode of the download list: which downloads are ticked, for actions on several at once."""

from __future__ import annotations

from gi.repository import Gio, GObject

from .model import DownloadItem

PAUSABLE = {"running", "processing", "queued", "seeding"}
RESUMABLE = {"paused", "error", "changed"}


class Selection(GObject.Object):
    """Ticked download ids within the shown view. Leaving selection mode clears them; downloads that leave
    the view (another filter, a search, removed) are unticked."""

    active = GObject.Property(type=bool, default=False)
    count = GObject.Property(type=int, default=0)
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        super().__init__()
        self.ids: set[int] = set()
        self.view: Gio.ListModel | None = None
        self._handler = 0
        self.connect("notify::active", lambda *_: self.active or self.clear())

    def set_view(self, view: Gio.ListModel) -> None:
        if self.view is not None:
            self.view.disconnect(self._handler)
        self.view = view
        self._handler = view.connect("items-changed", lambda *_: self._prune())
        self._prune()

    def items(self) -> list[DownloadItem]:
        """The ticked downloads, in list order."""
        return [item for item in self._shown() if item.id in self.ids]

    def is_selected(self, download_id: int) -> bool:
        return download_id in self.ids

    def set_selected(self, download_id: int, selected: bool) -> None:
        if selected != (download_id in self.ids):
            (self.ids.add if selected else self.ids.discard)(download_id)
            self._changed()

    def select_all(self) -> None:
        self.ids = {item.id for item in self._shown()}
        self._changed()

    def clear(self) -> None:
        if self.ids:
            self.ids = set()
            self._changed()

    def all_selected(self) -> bool:
        return bool(self.ids) and len(self.ids) == len(self._shown())

    def _shown(self) -> list[DownloadItem]:
        view = self.view
        return [view.get_item(i) for i in range(view.get_n_items())] if view is not None else []

    def _prune(self) -> None:
        shown = {item.id for item in self._shown()}
        if self.ids - shown:
            self.ids &= shown
            self._changed()

    def _changed(self) -> None:
        self.count = len(self.ids)
        self.emit("changed")
