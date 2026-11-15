"""The download list as a Qt table model, plus the filter and sort the sidebar, search and header drive."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QSortFilterProxyModel, Qt, Signal

from ..ui.item import LIBRARY, Download, when_text
from ..ui.text import describe_window

COLUMNS = ("Name", "Size", "Progress", "Left", "Speed", "Added")
NAME, SIZE, PROGRESS, LEFT, SPEED, ADDED = range(len(COLUMNS))
ITEM = Qt.ItemDataRole.UserRole  # the Download itself

Index = QModelIndex | QPersistentModelIndex


def queue_key(d: Download) -> str | None:
    """Which sidebar queue a download is in: "main", "schedule:<window>", or None (finished or a problem)."""
    if d.on_schedule:
        return f"schedule:{d.schedule_start}|{d.schedule_stop}|{d.schedule_days}"
    return "main" if d.state in {"queued", "running", "processing"} else None


def queue_label(key: str) -> str:
    if key == "main":
        return "Main queue"
    start, stop, days = key.removeprefix("schedule:").split("|")
    return describe_window(start, stop, days) if start and stop else "On the schedule"


class DownloadsModel(QAbstractTableModel):
    """All downloads in queue order (position, then id), one row each."""

    changed = Signal()  # anything that the sidebar counts or the status bar shows

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[Download] = []
        self._by_id: dict[int, Download] = {}

    # --- Qt model ----------------------------------------------------------------------------

    def rowCount(self, parent: Index = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent: Index = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.TextAlignmentRole:
            right = section in (SIZE, LEFT, SPEED)
            return (Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft) | Qt.AlignmentFlag.AlignVCenter
        return None

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        d = self._items[index.row()]
        if role == ITEM:
            return d
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return cell_text(d, index.column())
        if role == Qt.ItemDataRole.ToolTipRole and index.column() == NAME:
            return f"{d.title}\n{d.status}"
        return None

    # --- updates from the service -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def get(self, download_id: int) -> Download | None:
        return self._by_id.get(download_id)

    def urls(self) -> list[str]:
        return [d.url for d in self._items]

    def load(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._items = sorted((Download(row) for row in rows), key=_order)
        self._by_id = {d.id: d for d in self._items}
        self.endResetModel()
        self.changed.emit()

    def upsert(self, row: dict[str, Any]) -> Download:
        d = self._by_id.get(row["id"])
        if d is None:
            d = Download(row)
            at = self._insert_at(d)
            self.beginInsertRows(QModelIndex(), at, at)
            self._items.insert(at, d)
            self._by_id[d.id] = d
            self.endInsertRows()
        else:
            before = _order(d)
            d.update(row)
            if _order(d) != before:
                self._move_into_place(d)
            self._row_changed(d)
        self.changed.emit()
        return d

    def remove(self, download_id: int) -> None:
        d = self._by_id.pop(download_id, None)
        if d is None:
            return
        at = self._items.index(d)
        self.beginRemoveRows(QModelIndex(), at, at)
        del self._items[at]
        self.endRemoveRows()
        self.changed.emit()

    def apply_state(self, download_id: int, state: str, error: str) -> None:
        d = self._by_id.get(download_id)
        if d is not None:
            d.update({"state": state, "error": error})
            self._row_changed(d)
            self.changed.emit()

    def apply_progress(self, batch: list[tuple[int, int, int, float]]) -> None:
        for download_id, done, size, speed in batch:
            d = self._by_id.get(download_id)
            if d is not None:
                d.update({"done": done, "size": size, "speed": speed})
                self._row_changed(d)
        if batch:
            self.changed.emit()

    def index_of(self, download_id: int, column: int = 0) -> QModelIndex:
        d = self._by_id.get(download_id)
        return self.index(self._items.index(d), column) if d is not None else QModelIndex()

    # --- helpers -------------------------------------------------------------------------------

    def _insert_at(self, d: Download) -> int:
        key = _order(d)
        return next((i for i, other in enumerate(self._items) if _order(other) > key), len(self._items))

    def _move_into_place(self, d: Download) -> None:
        source = self._items.index(d)
        others = [o for o in self._items if o is not d]
        target = next((i for i, o in enumerate(others) if _order(o) > _order(d)), len(others))
        if target == source:
            return
        # Qt's destination counts rows before the move: moving down lands one past the target.
        self.beginMoveRows(QModelIndex(), source, source, QModelIndex(), target + (1 if target > source else 0))
        self._items = others[:target] + [d] + others[target:]
        self.endMoveRows()

    def _row_changed(self, d: Download) -> None:
        row = self._items.index(d)
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))


def _order(d: Download) -> tuple[int, int]:
    return (d.position, d.id)


def cell_text(d: Download, column: int) -> str:
    if column == NAME:
        return d.title
    if column == SIZE:
        return d.size_text
    if column == PROGRESS:
        return d.state_word
    if column == LEFT:
        return d.left_text
    if column == SPEED:
        return d.speed_text
    return when_text(d.added_at)


SORT_KEYS = {
    NAME: lambda d: d.title.casefold(),
    SIZE: lambda d: d.size,
    PROGRESS: lambda d: d.fraction,
    LEFT: lambda d: (d.size - d.done) / d.speed if d.speed > 0 and d.size > 0 else float("inf"),
    SPEED: lambda d: d.speed,
    ADDED: lambda d: d.added_at,
}


class DownloadsFilter(QSortFilterProxyModel):
    """What the table shows: one library view, category or queue, narrowed by the search text."""

    def __init__(self, source: DownloadsModel, parent=None):
        super().__init__(parent)
        self.setSourceModel(source)
        self.view = "all"     # a LIBRARY key
        self.group = ""       # a file-type group, "" = any
        self.queue = ""       # a queue_key, "" = any
        self.text = ""
        self.setDynamicSortFilter(True)

    def show(self, view: str = "all", group: str = "", queue: str = "") -> None:
        self._refilter(lambda: setattr(self, "filters", (view, group, queue)))

    def set_text(self, text: str) -> None:
        self._refilter(lambda: setattr(self, "text", text.strip().casefold()))

    def _refilter(self, change) -> None:
        """Qt 6.10 and newer want the change wrapped in begin/endFilterChange; older ones (Debian 13 has
        6.8) only have invalidateRowsFilter()."""
        if hasattr(self, "beginFilterChange"):
            self.beginFilterChange()
            change()
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            change()
            self.invalidateRowsFilter()

    @property
    def filters(self) -> tuple[str, str, str]:
        return self.view, self.group, self.queue

    @filters.setter
    def filters(self, value: tuple[str, str, str]) -> None:
        self.view, self.group, self.queue = value

    def accepts(self, d: Download) -> bool:
        if not LIBRARY[self.view][1](d):
            return False
        if self.group and d.group != self.group:
            return False
        if self.queue and queue_key(d) != self.queue:
            return False
        return not self.text or self.text in d.title.casefold() or self.text in d.url.casefold()

    def filterAcceptsRow(self, row: int, parent: Index) -> bool:
        return self.accepts(self.sourceModel().index(row, 0, parent).data(ITEM))

    def lessThan(self, left: Index, right: Index) -> bool:
        a, b = left.data(ITEM), right.data(ITEM)
        key = SORT_KEYS.get(left.column(), SORT_KEYS[ADDED])
        ka, kb = key(a), key(b)
        return ka < kb if ka != kb else _order(a) < _order(b)  # ties: queue order

    def item(self, row: int) -> Download | None:
        return self.index(row, 0).data(ITEM) if 0 <= row < self.rowCount() else None
