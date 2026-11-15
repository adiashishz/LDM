"""The sidebar: library views, folding Categories and Queues sections, and the total-speed graph."""

from __future__ import annotations

from collections import Counter, deque
from typing import Callable, Iterable

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractButton, QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QToolButton,
                               QVBoxLayout, QWidget)

from ..format import human
from ..ui.item import CATEGORIES, LIBRARY, Download
from .model import queue_key, queue_label
from .theme import GROUP_COLORS, Colors, icon

SIDEBAR_WIDTH = 232
HISTORY = 60  # seconds of total speed in the graph


class NavItem(QAbstractButton):
    """One sidebar entry: icon, label and count; the chosen entry is filled with the accent colour."""

    def __init__(self, key: str, label: str, icon_name: str, icon_color: str | None, colors: Callable[[], Colors],
                 indent: int = 0, height: int = 30):
        super().__init__()
        self.key, self.label, self.icon_name, self.icon_color = key, label, icon_name, icon_color
        self.colors, self.indent, self.count = colors, indent, 0
        self.setCheckable(True)
        self.setAutoExclusive(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(height)
        self.setAccessibleName(label)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_count(self, count: int) -> None:
        if count != self.count:
            self.count = count
            self.setAccessibleDescription(str(count))
            self.update()

    def sizeHint(self) -> QSize:
        return QSize(SIDEBAR_WIDTH - 20, self.height())

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        c = self.colors()
        on = self.isChecked()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        if on or self.underMouse():
            path = QPainterPath()
            path.addRoundedRect(rect, 7, 7)
            painter.fillPath(path, QColor(c.accent if on else c.press))
        if self.hasFocus():
            painter.setPen(QPen(QColor(c.accent if not on else "#FFFFFF"), 1.5))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 6, 6)
        x = 8 + self.indent
        size = 16 if not self.indent else 15
        color = "#FFFFFF" if on else (self.icon_color or c.accent)
        icon(self.icon_name, color, size, 1.9).paint(painter, int(x), int((self.height() - size) / 2), size, size)
        font = QFont(self.font())
        if on:
            font.setWeight(QFont.Weight.Medium)
        painter.setFont(font)
        painter.setPen(QColor("#FFFFFF" if on else c.fg))
        text_rect = rect.adjusted(x + size + 9, 0, -36, 0)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         painter.fontMetrics().elidedText(self.label, Qt.TextElideMode.ElideRight,
                                                          int(text_rect.width())))
        small = QFont(self.font())
        small.setPixelSize(12)
        painter.setFont(small)
        painter.setPen(QColor(255, 255, 255, 216) if on else QColor(c.dim))
        painter.drawText(rect.adjusted(0, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                         str(self.count))


class Section(QWidget):
    """A folding group of entries under a small heading."""

    def __init__(self, title: str, colors: Callable[[], Colors]):
        super().__init__()
        self.colors = colors
        self.toggle = QToolButton()
        self.toggle.setObjectName("sectiontoggle")
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.toggled.connect(self._folded)
        self.body = QWidget()
        self.rows = QVBoxLayout(self.body)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.body)
        self.restyle()

    def restyle(self) -> None:
        c = self.colors()
        self.toggle.setIcon(icon("chevron-down" if self.toggle.isChecked() else "chevron-right", c.dim, 10, 2.4))
        self.toggle.setStyleSheet(f"QToolButton {{ border: 0; color: {c.dim}; font-size: 11px; font-weight: 600; "
                                  f"padding: 0 8px 2px 6px; background: transparent; }}")

    def _folded(self, open_: bool) -> None:
        self.body.setVisible(open_)
        self.restyle()


class SpeedGraph(QWidget):
    def __init__(self, colors: Callable[[], Colors]):
        super().__init__()
        self.colors = colors
        self.samples: deque[float] = deque([0.0] * HISTORY, maxlen=HISTORY)
        self.setFixedHeight(30)

    def add(self, speed: float) -> None:
        self.samples.append(speed)
        self.update()

    def paintEvent(self, _event) -> None:
        c = self.colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        peak = max(max(self.samples), 1.0)
        width, height = self.width(), self.height()
        step = width / (HISTORY - 1)
        points = [QPointF(i * step, height - 2 - (height - 4) * s / peak) for i, s in enumerate(self.samples)]
        line = QPainterPath(points[0])
        for point in points[1:]:
            line.lineTo(point)
        area = QPainterPath(line)
        area.lineTo(width, height)
        area.lineTo(0, height)
        area.closeSubpath()
        fill = QColor(c.accent)
        fill.setAlphaF(0.14)
        painter.fillPath(area, fill)
        painter.setPen(QPen(QColor(c.accent), 1.6))
        painter.drawPath(line)


class Sidebar(QFrame):
    """Emits `chosen(view, group, queue)` when the user picks an entry; only one entry is chosen at a time."""

    chosen = Signal(str, str, str)

    def __init__(self, colors: Callable[[], Colors]):
        super().__init__()
        self.setObjectName("sidebar")
        self.setFixedWidth(SIDEBAR_WIDTH)
        self.colors = colors
        self.items: list[NavItem] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 12, 10, 12)
        layout.setSpacing(14)

        scroll_body = QWidget()
        body = QVBoxLayout(scroll_body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(14)
        library = QVBoxLayout()
        library.setSpacing(1)
        self.library: dict[str, NavItem] = {}
        for key, (label, _test) in LIBRARY.items():
            item = self._item(f"view:{key}", label, key, None)
            self.library[key] = item
            library.addWidget(item)
        body.addLayout(library)

        self.categories = Section("Categories", colors)
        self.category_items: dict[str, NavItem] = {}
        for group, label in CATEGORIES.items():
            item = self._item(f"group:{group}", label, group, GROUP_COLORS[group], indent=16, height=28)
            self.category_items[group] = item
            self.categories.rows.addWidget(item)
        body.addWidget(self.categories)

        self.queues = Section("Queues", colors)
        self.queue_items: dict[str, NavItem] = {}
        body.addWidget(self.queues)
        body.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(scroll_body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        layout.addWidget(scroll, 1)

        card = QFrame()
        card.setObjectName("speedcard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 12, 12, 12)
        card_layout.setSpacing(8)
        top = QHBoxLayout()
        top.addWidget(QLabel("Total speed"))
        top.addStretch(1)
        self.speed_label = QLabel("0 B/s")
        self.speed_label.setObjectName("speed")
        top.addWidget(self.speed_label)
        card_layout.addLayout(top)
        self.graph = SpeedGraph(colors)
        card_layout.addWidget(self.graph)
        layout.addWidget(card)
        self.current = "view:all"
        self.library["all"].setChecked(True)

    def _item(self, key: str, label: str, icon_name: str, color: str | None, indent: int = 0,
              height: int = 30) -> NavItem:
        item = NavItem(key, label, icon_name, color, self.colors, indent, height)
        item.clicked.connect(lambda _checked=False, k=key: self.choose(k))
        self.items.append(item)
        return item

    def choose(self, key: str) -> None:
        self.current = key
        for item in self.items:
            item.setChecked(item.key == key)
        kind, _, value = key.partition(":")
        self.chosen.emit(value if kind == "view" else "all", value if kind == "group" else "",
                         value if kind == "queue" else "")

    def update_counts(self, downloads: Iterable[Download]) -> None:
        downloads = list(downloads)
        for key, item in self.library.items():
            item.set_count(sum(1 for d in downloads if LIBRARY[key][1](d)))
        groups = Counter(d.group for d in downloads)
        for group, item in self.category_items.items():
            item.set_count(groups[group])
            # Pictures and Other only when there are some; the design's six are always there.
            item.setVisible(group not in ("image", "other") or groups[group] > 0 or item.isChecked())
        queues = Counter(k for k in map(queue_key, downloads) if k)
        queues.setdefault("main", 0)
        for key in [k for k in self.queue_items if k not in queues and f"queue:{k}" != self.current]:
            item = self.queue_items.pop(key)
            self.items.remove(item)
            item.deleteLater()
        for key in sorted(queues, key=lambda k: (k != "main", k)):
            if key not in self.queue_items:
                item = self._item(f"queue:{key}", queue_label(key), "queue" if key == "main" else "schedule",
                                  self.colors().dim, indent=16, height=28)
                item.setChecked(f"queue:{key}" == self.current)
                self.queue_items[key] = item
                self.queues.rows.addWidget(item)
            self.queue_items[key].set_count(queues.get(key, 0))

    def add_speed(self, speed: float) -> None:
        self.speed_label.setText(f"{human(speed)}/s")
        self.graph.add(speed)

    def restyle(self) -> None:
        self.categories.restyle()
        self.queues.restyle()
        for item in self.items:
            if item.key.startswith("queue:"):
                item.icon_color = self.colors().dim
        self.update()
