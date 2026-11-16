"""The download table: striped rows, a file tile, a progress bar with a state word, and the selected rows
filled with the accent colour."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QModelIndex, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableView

from ..ui.item import Download
from .model import ADDED, COLUMNS, ITEM, LEFT, NAME, PROGRESS, SIZE, SPEED, DownloadsFilter, cell_text
from .theme import GROUP_COLORS, Colors

ROW_HEIGHT = 36
PAD = 10
COLUMN = 104  # every column after Name is this wide, so the header reads evenly
WIDTHS = dict.fromkeys((SIZE, PROGRESS, LEFT, SPEED, ADDED), COLUMN)
RIGHT = {SIZE, LEFT, SPEED}


class RowDelegate(QStyledItemDelegate):
    def __init__(self, view: DownloadTable, colors: Callable[[], Colors]):
        super().__init__(view)
        self.view, self.colors = view, colors

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(WIDTHS.get(index.column(), 200), ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        d: Download = index.data(ITEM)
        c = self.colors()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(option.rect)
        # The row's background is one rounded shape across all cells; each cell paints its own slice.
        row = QRectF(0, option.rect.top() + 1, self.view.viewport().width(), option.rect.height() - 2)
        background = (c.accent if selected else c.press if hovered else c.stripe if index.row() % 2 else None)
        if background:
            path = QPainterPath()
            path.addRoundedRect(row, 7, 7)
            painter.fillPath(path, QColor(background))
        fg = QColor("#FFFFFF") if selected else QColor(c.fg)
        sub = QColor(255, 255, 255, 235) if selected else QColor(c.dim)
        rect = option.rect.adjusted(PAD, 0, -PAD, 0)
        column = index.column()
        if column == NAME:
            self._name(painter, rect, d, fg, option.font)
        elif column == PROGRESS:
            self._progress(painter, rect, d, c, selected, option.font)
        else:
            painter.setPen(sub)
            painter.setFont(option.font)
            align = (Qt.AlignmentFlag.AlignRight if column in RIGHT else Qt.AlignmentFlag.AlignLeft)
            text = painter.fontMetrics().elidedText(cell_text(d, column), Qt.TextElideMode.ElideRight, rect.width())
            painter.drawText(rect, align | Qt.AlignmentFlag.AlignVCenter, text)
        painter.restore()

    def _name(self, painter: QPainter, rect: QRect, d: Download, fg: QColor, font: QFont) -> None:
        tile = QRectF(rect.left(), rect.center().y() - 10, 22, 22)
        path = QPainterPath()
        path.addRoundedRect(tile, 6, 6)
        painter.fillPath(path, QColor(GROUP_COLORS.get(d.group, GROUP_COLORS["other"])))
        small = QFont(font)
        small.setPixelSize(8 if len(d.extension) < 4 else 7)
        small.setBold(True)
        painter.setFont(small)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(tile, Qt.AlignmentFlag.AlignCenter, d.extension)
        painter.setFont(font)
        painter.setPen(fg)
        text_rect = rect.adjusted(31, 0, 0, 0)
        title = painter.fontMetrics().elidedText(d.title, Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)

    def _progress(self, painter: QPainter, rect: QRect, d: Download, c: Colors, selected: bool, font: QFont) -> None:
        # The bar above, the state word under it: the column stays as narrow as the others.
        track = QRectF(rect.left(), rect.top() + 9, rect.width(), 4)
        path = QPainterPath()
        path.addRoundedRect(track, 2, 2)
        painter.fillPath(path, QColor(255, 255, 255, 90) if selected else QColor(c.track))
        if d.fraction > 0:
            fill = QPainterPath()
            fill.addRoundedRect(QRectF(track.left(), track.top(), max(track.width() * d.fraction, 4), 4), 2, 2)
            painter.fillPath(fill, QColor("#FFFFFF") if selected else QColor(c.bar(d.tone)))
        small = QFont(font)
        small.setPixelSize(11)
        painter.setFont(small)
        painter.setPen(QColor("#FFFFFF") if selected else QColor(c.red if d.tone == "bad" else c.dim))
        word = QRect(rect.left(), rect.top() + 15, rect.width(), rect.height() - 17)
        painter.drawText(word, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(d.state_word, Qt.TextElideMode.ElideRight, rect.width()))


class DownloadTable(QTableView):
    """Selecting rows: click, Ctrl+click and Shift+click. Clicking a header sorts by it; a third click
    goes back to queue order."""

    context_menu = Signal(object)  # global position
    activated_item = Signal(object)  # a Download, double-clicked or Enter

    def __init__(self, proxy: DownloadsFilter, colors: Callable[[], Colors], parent=None):
        super().__init__(parent)
        self.proxy = proxy
        self.setModel(proxy)
        self.setItemDelegate(RowDelegate(self, colors))
        self.setShowGrid(False)
        self.setMouseTracking(True)
        self.setWordWrap(False)
        self.setFrameShape(QTableView.Shape.NoFrame)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda pos: self.context_menu.emit(self.viewport().mapToGlobal(pos)))
        self.doubleClicked.connect(lambda index: self.activated_item.emit(index.data(ITEM)))
        vertical = self.verticalHeader()
        vertical.hide()
        vertical.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vertical.setDefaultSectionSize(ROW_HEIGHT)
        header = self.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(False)
        header.setSectionResizeMode(NAME, QHeaderView.ResizeMode.Stretch)
        for column, width in WIDTHS.items():
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            header.resizeSection(column, width)
        header.setMinimumSectionSize(40)
        header.setFixedHeight(32)
        header.sectionClicked.connect(self._sort_by)
        self.sort_column, self.sort_order = -1, Qt.SortOrder.AscendingOrder

    def _sort_by(self, column: int) -> None:
        """First click: the natural order for that column (A→Z, newest, biggest…), then reversed, then queue."""
        natural = Qt.SortOrder.AscendingOrder if column == NAME or column == LEFT else Qt.SortOrder.DescendingOrder
        if column != self.sort_column:
            self.set_sort(column, natural)
        elif self.sort_order == natural:
            self.set_sort(column, Qt.SortOrder.AscendingOrder if natural == Qt.SortOrder.DescendingOrder
                          else Qt.SortOrder.DescendingOrder)
        else:
            self.set_sort(-1)

    def set_sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        self.sort_column, self.sort_order = column, order
        header = self.horizontalHeader()
        header.setSortIndicatorShown(column >= 0)
        if column >= 0:
            header.setSortIndicator(column, order)
        self.proxy.sort(column, order)

    def selected_items(self) -> list[Download]:
        rows = sorted({index.row() for index in self.selectionModel().selectedRows()})
        return [self.proxy.item(row) for row in rows if self.proxy.item(row) is not None]

    def current_item(self) -> Download | None:
        index = self.currentIndex()
        return index.data(ITEM) if index.isValid() else None

    def select_id(self, download_id: int) -> bool:
        for row in range(self.proxy.rowCount()):
            if self.proxy.item(row).id == download_id:
                self.selectRow(row)
                self.scrollTo(self.proxy.index(row, 0))
                return True
        return False

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.current_item() is not None:
            self.activated_item.emit(self.current_item())
            return
        super().keyPressEvent(event)


__all__ = ["COLUMNS", "DownloadTable"]
