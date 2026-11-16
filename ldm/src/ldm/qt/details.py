"""The details panel on the right: the selected download's progress, its connections and facts, and the
action that fits its state."""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlsplit

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolButton,
                               QVBoxLayout, QWidget)

from ..format import STATE_LABELS, eta_text, human
from ..ui.item import Download, when_text
from ..ui.text import describe_window, short_path
from .theme import Colors, icon

PANEL_WIDTH = 320
PANEL_MIN, PANEL_MAX = 280, 520
MAX_SEGMENTS = 32

# The main button per state: (label, action name, red)
PRIMARY = {
    "running": ("Pause", "pause", False),
    "processing": ("Pause", "pause", False),
    "queued": ("Move to top", "move_to_top", False),  # the service has no "start now" yet
    "paused": ("Resume", "resume", False),
    "seeding": ("Stop seeding", "pause", False),
    "error": ("Retry", "resume", False),
    "changed": ("Start over", "resume", False),
    "expired": ("Set a new link", "ask_new_url", True),
    "done": ("Open", "open_file", False),
}


class Bar(QWidget):
    """A progress bar, or a strip of segments that each fill from their own start."""

    def __init__(self, colors: Callable[[], Colors], height: int):
        super().__init__()
        self.colors = colors
        self.fractions: list[float] = [0.0]
        self.tone = "active"
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set(self, fractions: list[float], tone: str) -> None:
        self.fractions, self.tone = fractions or [0.0], tone
        self.update()

    def paintEvent(self, _event) -> None:
        c = self.colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        radius = 4 if len(self.fractions) == 1 else 3
        clip.addRoundedRect(QRectF(self.rect()), radius, radius)
        painter.setClipPath(clip)
        gap = 2 if len(self.fractions) > 1 else 0
        width = (self.width() - gap * (len(self.fractions) - 1)) / len(self.fractions)
        for i, fraction in enumerate(self.fractions):
            x = i * (width + gap)
            painter.fillRect(QRectF(x, 0, width, self.height()), QColor(c.track))
            if fraction > 0:
                painter.fillRect(QRectF(x, 0, width * min(fraction, 1.0), self.height()), QColor(c.bar(self.tone)))


class DetailsPanel(QFrame):
    """`action(name)` asks the window to act on the shown download (pause, resume, open_file, …)."""

    action = Signal(str)
    more_requested = Signal(object)  # global position for the download's menu

    def __init__(self, colors: Callable[[], Colors]):
        super().__init__()
        self.setObjectName("details")
        self.setMinimumWidth(PANEL_MIN)
        self.setMaximumWidth(PANEL_MAX)
        self.colors = colors
        self.download: Download | None = None
        self.parts: list[dict[str, Any]] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head = QFrame()
        head.setObjectName("detailshead")
        head.setFixedHeight(44)
        head_layout = QHBoxLayout(head)
        head_layout.setContentsMargins(18, 0, 18, 0)
        title = QLabel("Details")
        title.setStyleSheet("font-weight: 600")
        head_layout.addWidget(title)
        head_layout.addStretch(1)
        outer.addWidget(head)

        self.empty = QLabel("Select a download to see its progress, connections and details.")
        self.empty.setObjectName("dim")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setContentsMargins(24, 24, 24, 24)

        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(18, 18, 18, 18)
        body.setSpacing(16)
        heading = QVBoxLayout()
        heading.setSpacing(4)
        self.status = QLabel()
        self.name = QLabel()
        self.name.setObjectName("name")
        self.name.setWordWrap(True)
        self.name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        heading.addWidget(self.status)
        heading.addWidget(self.name)
        body.addLayout(heading)

        progress = QVBoxLayout()
        progress.setSpacing(8)
        numbers = QHBoxLayout()
        numbers.setSpacing(8)
        self.percent = QLabel()
        self.percent.setObjectName("percent")
        self.amount = QLabel()
        self.amount.setObjectName("dim")
        numbers.addWidget(self.percent, 0, Qt.AlignmentFlag.AlignBaseline)
        numbers.addWidget(self.amount, 0, Qt.AlignmentFlag.AlignBaseline)
        numbers.addStretch(1)
        progress.addLayout(numbers)
        self.bar = Bar(colors, 8)
        progress.addWidget(self.bar)
        body.addLayout(progress)

        self.strip_box = QWidget()
        strip = QVBoxLayout(self.strip_box)
        strip.setContentsMargins(0, 0, 0, 0)
        strip.setSpacing(6)
        strip_head = QHBoxLayout()
        self.parts_label = QLabel()
        self.parts_label.setObjectName("partslabel")
        self.parts_note = QLabel()
        self.parts_note.setObjectName("dim")
        self.parts_note.setStyleSheet("font-size: 12px")
        strip_head.addWidget(self.parts_label)
        strip_head.addStretch(1)
        strip_head.addWidget(self.parts_note)
        strip.addLayout(strip_head)
        self.strip = Bar(colors, 14)
        strip.addWidget(self.strip)
        body.addWidget(self.strip_box)

        self.facts = QGridLayout()
        self.facts.setHorizontalSpacing(14)
        self.facts.setVerticalSpacing(8)
        self.facts.setColumnStretch(1, 1)
        body.addLayout(self.facts)
        body.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.primary = QPushButton()
        self.primary.setObjectName("primary")
        self.primary.setCursor(Qt.CursorShape.PointingHandCursor)
        self.primary.clicked.connect(lambda: self.action.emit(self._primary_action))
        self.folder = QPushButton("Open folder")
        self.folder.setCursor(Qt.CursorShape.PointingHandCursor)
        self.folder.clicked.connect(lambda: self.action.emit("open_folder"))
        self.more = QToolButton()
        self.more.setAccessibleName("More actions")
        self.more.setToolTip("More actions")
        self.more.setFixedSize(36, 36)
        self.more.clicked.connect(lambda: self.more_requested.emit(self.more.mapToGlobal(self.more.rect().bottomLeft())))
        for button in (self.primary, self.folder):
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            buttons.addWidget(button)
        buttons.addWidget(self.more)
        body.addLayout(buttons)

        outer.addWidget(self.empty, 1)
        outer.addWidget(self.body, 1)
        self._primary_action = ""
        self.restyle()
        self.show_download(None)

    def restyle(self) -> None:
        c = self.colors()
        self.more.setIcon(icon("more", c.fg, 18, 2.6))
        self.more.setStyleSheet(f"QToolButton {{ border: 1px solid {c.line}; background: {c.card}; }}")
        self.show_download(self.download)

    def show_download(self, d: Download | None, parts: list[dict[str, Any]] | None = None) -> None:
        if d is not self.download:
            self.parts = []
        if parts is not None:
            self.parts = parts
        self.download = d
        self.empty.setVisible(d is None)
        self.body.setVisible(d is not None)
        if d is None:
            return
        c = self.colors()
        status = STATE_LABELS.get(d.state, d.state)
        if d.state == "queued" and d.on_schedule:
            status = "Scheduled"
        self.status.setText(status.upper())
        self.status.setStyleSheet(f"color: {c.text(d.tone)}; font-size: 11px; font-weight: 600; "
                                  "letter-spacing: 0.06em")
        self.name.setText(d.title)
        if d.size > 0:
            self.percent.setText(f"{int(d.fraction * 100)}%")
            self.amount.setText(f"{human(d.done)} of {human(d.size)}" if d.state != "done" else human(d.size))
        else:
            self.percent.setText("—" if not d.done else human(d.done))
            self.amount.setText("size unknown" if d.state != "done" else "")
        self.bar.set([d.fraction if d.size > 0 else (1.0 if d.state == "done" else 0.0)], d.tone)
        self._show_parts(d)
        self._show_facts(d)
        label, name, red = PRIMARY.get(d.state, ("Resume", "resume", False))
        self.primary.setText(label)
        self.primary.setProperty("danger", red)
        self.primary.style().unpolish(self.primary)
        self.primary.style().polish(self.primary)
        self._primary_action = name
        self.folder.setEnabled(bool(d.path) and d.state in {"done", "seeding"})

    def _show_parts(self, d: Download) -> None:
        if d.kind == "http" and self.parts:
            sizes = [p["last"] - p["first"] + 1 for p in self.parts]
            fractions = [p["done"] / size if size > 0 else 0.0 for p, size in zip(self.parts, sizes)]
            if len(fractions) > MAX_SEGMENTS:  # merge neighbours so each segment stays visible
                per = -(-len(fractions) // MAX_SEGMENTS)
                fractions = [sum(fractions[i:i + per]) / len(fractions[i:i + per])
                             for i in range(0, len(fractions), per)]
            active = sum(1 for p in self.parts if p.get("active"))
            self.parts_label.setText(f"{len(self.parts)} part{'s' if len(self.parts) != 1 else ''}")
            self.parts_note.setText(f"{active} downloading" if active else "each fills from its own start")
            self.strip.set(fractions, d.tone)
            self.strip_box.show()
        elif d.kind == "http" and d.state == "done":
            self.parts_label.setText("Finished")
            self.parts_note.setText("all parts done")
            self.strip.set([1.0], "done")
            self.strip_box.show()
        else:
            self.strip_box.hide()

    def _show_facts(self, d: Download) -> None:
        while self.facts.count():
            widget = self.facts.takeAt(0).widget()
            if widget is not None:
                widget.hide()  # deleteLater() alone would leave it painted until the next event loop turn
                widget.deleteLater()
        c = self.colors()
        parts = urlsplit(d.url)
        address = (parts.netloc + parts.path) if parts.scheme in ("http", "https") else d.url
        facts: list[tuple[str, str, str]] = [("Address", address, "")]
        if d.state in PROBLEM_STATES and d.error:
            facts.append(("Error", d.error, "bad"))
        if d.quality:
            facts.append(("Quality", d.quality, ""))
        if d.kind == "torrent" and d.state in {"running", "seeding"}:
            facts.append(("Peers", str(d.peers), ""))
            facts.append(("Ratio", f"{d.ratio:.2f}", ""))
        if d.state == "running":
            facts.append(("Speed", f"{human(d.speed)}/s", ""))
            if d.size > 0 and d.speed > 0:
                facts.append(("Time left", eta_text(d.size - d.done, d.speed).removesuffix(" left"), ""))
        if d.on_schedule and d.schedule_start and d.schedule_stop:
            facts.append(("Schedule", describe_window(d.schedule_start, d.schedule_stop, d.schedule_days), ""))
        if d.speed_limit:
            facts.append(("Speed limit", f"{human(d.speed_limit)}/s", ""))
        if d.dir:
            facts.append(("Saved to", short_path(d.dir), ""))
        facts.append(("Added", when_text(d.added_at), ""))
        if d.finished_at:
            facts.append(("Finished", when_text(d.finished_at), ""))
        if d.state not in {"done", "seeding"} and d.kind == "http":
            facts.append(("Can resume", "Yes" if d.resumable else "No, starts over",
                          "good" if d.resumable else ""))
        for row, (key, value, tone) in enumerate(facts):
            key_label = QLabel(key)
            key_label.setObjectName("factkey")
            value_label = QLabel()
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            value_label.setToolTip(value)
            value_label.setMinimumWidth(10)
            value_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            value_label.setText(value_label.fontMetrics().elidedText(value, Qt.TextElideMode.ElideMiddle, 200))
            color = c.green if tone == "good" else c.red if tone == "bad" else c.fg
            for label in (key_label, value_label):
                label.setStyleSheet(f"font-size: 12.5px;{'' if label is key_label else f' color: {color};'}")
            self.facts.addWidget(key_label, row, 0)
            self.facts.addWidget(value_label, row, 1)


PROBLEM_STATES = {"error", "expired", "changed"}
