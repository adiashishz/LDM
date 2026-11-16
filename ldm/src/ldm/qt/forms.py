"""Building blocks for LDM's dialogs, in the window's style: cards of rows (title, dim subtitle, a control on
the right), switches, entry, spin and choice rows, a folding row, and a dialog with a footer of buttons and
toasts. Nothing here talks to the service."""

from __future__ import annotations

from typing import Any, Callable, Iterable

from PySide6.QtCore import QPropertyAnimation, QRectF, QSize, Qt, Signal, Property, QEasingCurve
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (QAbstractButton, QComboBox, QDialog, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QToolButton,
                               QVBoxLayout, QWidget)

from ..schedule import DAYS, parse_days, parse_time
from ..ui.text import WINDOW_KEYS, describe_window
from .theme import icon
from .widgets import Toast


def colors():
    from .theme import current

    return current()


class Switch(QAbstractButton):
    """An on/off switch, like GNOME's and macOS's."""

    def __init__(self, checked: bool = False):
        super().__init__()
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(40, 22)
        self._offset = 1.0 if checked else 0.0
        self._animation = QPropertyAnimation(self, b"offset", self)
        self._animation.setDuration(120)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = value
        self.update()

    offset = Property(float, _get_offset, _set_offset)

    def _animate(self, checked: bool) -> None:
        self._animation.stop()
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def setChecked(self, checked: bool) -> None:  # noqa: N802 (Qt name)
        super().setChecked(checked)
        if not self.signalsBlocked():
            return
        self._offset = 1.0 if checked else 0.0  # set quietly: no animation
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(40, 22)

    def paintEvent(self, _event) -> None:
        c = colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QColor(c.accent) if self.isChecked() else QColor(c.track)
        if not self.isEnabled():
            track.setAlphaF(0.5)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, 40, 22), 11, 11)
        painter.fillPath(path, track)
        knob = QPainterPath()
        knob.addEllipse(QRectF(2 + 18 * self._offset, 2, 18, 18))
        painter.fillPath(knob, QColor("#FFFFFF"))
        if self.hasFocus():
            painter.setPen(QColor(c.accent))
            painter.drawRoundedRect(QRectF(0.5, 0.5, 39, 21), 11, 11)


class Row(QFrame):
    """Title and dim subtitle on the left, controls on the right."""

    def __init__(self, title: str, subtitle: str = "", *controls: QWidget):
        super().__init__()
        self.setObjectName("row")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 9, 14, 9)
        layout.setSpacing(12)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.title = QLabel(title)
        self.title.setObjectName("rowtitle")
        self.title.setWordWrap(True)
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("rowsub")
        self.subtitle.setWordWrap(True)
        self.subtitle.setVisible(bool(subtitle))
        self.subtitle.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        texts.addWidget(self.title)
        texts.addWidget(self.subtitle)
        layout.addLayout(texts, 1)
        self.controls = QHBoxLayout()
        self.controls.setSpacing(6)
        layout.addLayout(self.controls)
        for control in controls:
            self.add_control(control)

    def add_control(self, control: QWidget) -> None:
        self.controls.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_title(self, text: str) -> None:
        self.title.setText(text)

    def set_subtitle(self, text: str) -> None:
        self.subtitle.setText(text)
        self.subtitle.setVisible(bool(text))


class SwitchRow(Row):
    toggled = Signal(bool)

    def __init__(self, title: str, subtitle: str = "", checked: bool = False):
        self.switch = Switch(checked)
        super().__init__(title, subtitle, self.switch)
        self.switch.toggled.connect(self.toggled)
        self.switch.setAccessibleName(title)

    def mouseReleaseEvent(self, event) -> None:
        if self.isEnabled() and event.button() == Qt.MouseButton.LeftButton:
            self.switch.toggle()

    def is_on(self) -> bool:
        return self.switch.isChecked()

    def set_on(self, on: bool, quiet: bool = False) -> None:
        self.switch.blockSignals(quiet)
        self.switch.setChecked(on)
        self.switch.blockSignals(False)


class EntryRow(QFrame):
    """A labelled text field. `applied(text)` when the user presses Enter or leaves the field after a change."""

    applied = Signal(str)
    changed = Signal(str)

    def __init__(self, title: str, text: str = "", placeholder: str = "", password: bool = False):
        super().__init__()
        self.setObjectName("row")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 10)
        layout.setSpacing(4)
        self.title = QLabel(title)
        self.title.setObjectName("rowsub")
        self.title.setWordWrap(True)
        self.edit = QLineEdit(text)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setAccessibleName(title)
        if password:
            self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        line = QHBoxLayout()
        line.setSpacing(6)
        line.addWidget(self.edit, 1)
        self.suffix = line
        layout.addWidget(self.title)
        layout.addLayout(line)
        self._applied_text = text
        self.edit.textChanged.connect(self.changed)
        self.edit.editingFinished.connect(self._finish)

    def _finish(self) -> None:
        if self.edit.text() != self._applied_text:
            self._applied_text = self.edit.text()
            self.applied.emit(self.edit.text())

    def text(self) -> str:
        return self.edit.text()

    def set_text(self, text: str) -> None:
        self._applied_text = text
        self.edit.setText(text)

    def set_title(self, text: str) -> None:
        self.title.setText(text)

    def set_error(self, error: bool) -> None:
        self.edit.setProperty("error", error)
        self.edit.style().unpolish(self.edit)
        self.edit.style().polish(self.edit)


class SpinRow(Row):
    changed = Signal(object)

    def __init__(self, title: str, subtitle: str, low: float, high: float, step: float = 1, digits: int = 0):
        self.spin = QDoubleSpinBox() if digits else QSpinBox()
        self.spin.setRange(low, high)
        self.spin.setSingleStep(step)
        if digits:
            self.spin.setDecimals(digits)
        self.spin.setMinimumWidth(96)
        self.spin.setAccessibleName(title)
        super().__init__(title, subtitle, self.spin)
        self.spin.valueChanged.connect(lambda value: self.changed.emit(value))

    def value(self) -> float:
        return self.spin.value()

    def set_value(self, value: float, quiet: bool = False) -> None:
        self.spin.blockSignals(quiet)
        self.spin.setValue(value)
        self.spin.blockSignals(False)


class ComboRow(Row):
    """A choice among (value, label) pairs; `chosen(value)` on change."""

    chosen = Signal(object)

    def __init__(self, title: str, choices: Iterable[tuple[Any, str]], subtitle: str = ""):
        self.combo = QComboBox()
        self.combo.setAccessibleName(title)
        self.values: list[Any] = []
        super().__init__(title, subtitle, self.combo)
        self.set_choices(choices)
        self.combo.currentIndexChanged.connect(
            lambda i: 0 <= i < len(self.values) and self.chosen.emit(self.values[i]))

    def set_choices(self, choices: Iterable[tuple[Any, str]]) -> None:
        self.combo.blockSignals(True)
        self.combo.clear()
        self.values = []
        for value, label in choices:
            self.values.append(value)
            self.combo.addItem(label)
        self.combo.blockSignals(False)

    def value(self) -> Any:
        index = self.combo.currentIndex()
        return self.values[index] if 0 <= index < len(self.values) else None

    def index(self) -> int:
        return self.combo.currentIndex()

    def set_value(self, value: Any, quiet: bool = True) -> None:
        if value in self.values:
            self.set_index(self.values.index(value), quiet)

    def set_index(self, index: int, quiet: bool = True) -> None:
        self.combo.blockSignals(quiet)
        self.combo.setCurrentIndex(index)
        self.combo.blockSignals(False)


class Card(QFrame):
    """A titled group of rows, separated by thin lines."""

    def __init__(self, title: str = "", description: str = "", *rows: QWidget):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        head = QHBoxLayout()
        head.setContentsMargins(4, 0, 0, 0)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.title = QLabel(title)
        self.title.setObjectName("cardtitle")
        self.title.setVisible(bool(title))
        self.description = QLabel(description)
        self.description.setObjectName("rowsub")
        self.description.setWordWrap(True)
        self.description.setVisible(bool(description))
        texts.addWidget(self.title)
        texts.addWidget(self.description)
        head.addLayout(texts, 1)
        self.head_controls = QHBoxLayout()
        head.addLayout(self.head_controls)
        outer.addLayout(head)
        self.box = QFrame()
        self.box.setObjectName("card")
        self.rows = QVBoxLayout(self.box)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(0)
        outer.addWidget(self.box)
        for row in rows:
            self.add(row)

    def add(self, row: QWidget) -> QWidget:
        if self.rows.count():
            line = QFrame()
            line.setObjectName("rowline")
            line.setFixedHeight(1)
            self.rows.addWidget(line)
        self.rows.addWidget(row)
        return row

    def clear(self) -> None:
        while self.rows.count():
            widget = self.rows.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()

    def add_head_control(self, widget: QWidget) -> None:
        self.head_controls.addWidget(widget)

    def sync_lines(self) -> None:
        """Hide separator lines next to hidden rows."""
        widgets = [self.rows.itemAt(i).widget() for i in range(self.rows.count())]
        shown_before = False
        for index, widget in enumerate(widgets):
            if widget.objectName() == "rowline":
                after = next((w for w in widgets[index + 1:] if w.objectName() != "rowline"), None)
                widget.setVisible(shown_before and after is not None and not after.isHidden())
            else:
                shown_before = shown_before or not widget.isHidden()


class Folding(QFrame):
    """A row that opens to show more rows; optionally with a switch that also enables them."""

    toggled = Signal(bool)

    def __init__(self, title: str, subtitle: str = "", with_switch: bool = False):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.arrow = QToolButton()
        self.arrow.setAutoRaise(True)
        self.arrow.setFixedSize(24, 24)
        self.arrow.setAccessibleName(f"Show {title}")
        self.arrow.clicked.connect(lambda: self.set_open(not self.body.isVisible()))
        self.switch = Switch() if with_switch else None
        controls = [w for w in (self.switch, self.arrow) if w is not None]
        self.head = Row(title, subtitle, *controls)
        self.head.mouseReleaseEvent = lambda e: self.set_open(not self.body.isVisible())
        layout.addWidget(self.head)
        self.body = QFrame()
        self.body.setObjectName("foldbody")
        self.rows = QVBoxLayout(self.body)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(0)
        layout.addWidget(self.body)
        if self.switch:
            self.switch.toggled.connect(self._switched)
        self.set_open(False)

    def add(self, row: QWidget) -> QWidget:
        line = QFrame()
        line.setObjectName("rowline")
        line.setFixedHeight(1)
        self.rows.addWidget(line)
        self.rows.addWidget(row)
        return row

    def set_open(self, open_: bool) -> None:
        self.body.setVisible(open_)
        self.arrow.setIcon(icon("chevron-down" if open_ else "chevron-right", colors().dim, 14, 2))

    def is_on(self) -> bool:
        return self.switch.isChecked() if self.switch else True

    def set_on(self, on: bool) -> None:
        if self.switch:
            self.switch.setChecked(on)

    def _switched(self, on: bool) -> None:
        if on:
            self.set_open(True)
        self.toggled.emit(on)


class TwoDigits(QSpinBox):
    """09, not 9."""

    def textFromValue(self, value: int) -> str:  # noqa: N802 (Qt name)
        return f"{value:02d}"


class TimeEdit(QWidget):
    changed = Signal()

    def __init__(self):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.hours, self.minutes = TwoDigits(), TwoDigits()
        for spin, top, step, name in ((self.hours, 23, 1, "Hours"), (self.minutes, 59, 5, "Minutes")):
            spin.setRange(0, top)
            spin.setSingleStep(step)
            spin.setWrapping(True)
            spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
            spin.setMinimumWidth(56)
            spin.setAccessibleName(name)
            spin.valueChanged.connect(self.changed)
        layout.addWidget(self.hours)
        layout.addWidget(QLabel(":"))
        layout.addWidget(self.minutes)

    def time(self) -> str:
        return f"{self.hours.value():02d}:{self.minutes.value():02d}"

    def set_time(self, text: str) -> None:
        minutes = parse_time(text)
        self.hours.setValue(minutes // 60)
        self.minutes.setValue(minutes % 60)


class DaysEdit(QWidget):
    """Weekday toggles; none chosen means every day."""

    changed = Signal()

    def __init__(self):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.buttons: list[QPushButton] = []
        for day in DAYS:
            button = QPushButton(day[:2].capitalize())
            button.setObjectName("day")
            button.setCheckable(True)
            button.setToolTip(day.capitalize())
            button.setFixedWidth(40)
            button.toggled.connect(self.changed)
            layout.addWidget(button)
            self.buttons.append(button)

    def days(self) -> str:
        return ",".join(day for day, button in zip(DAYS, self.buttons) if button.isChecked())

    def set_days(self, text: str) -> None:
        chosen = parse_days(text) if text else set()
        for index, button in enumerate(self.buttons):
            button.blockSignals(True)
            button.setChecked(index in chosen)
            button.blockSignals(False)
        self.changed.emit()


class ScheduleRows:
    """Start, stop and days, for the default schedule (Preferences) or one download's own times."""

    def __init__(self):
        self.start, self.stop, self.days = TimeEdit(), TimeEdit(), DaysEdit()
        self.start_row = Row("Start at", "", self.start)
        self.stop_row = Row("Stop at", "Earlier than the start time runs overnight", self.stop)
        self.days_row = Row("Days", "None chosen means every day", self.days)

    def rows(self) -> list[QWidget]:
        return [self.start_row, self.stop_row, self.days_row]

    def values(self) -> tuple[str, str, str]:
        return self.start.time(), self.stop.time(), self.days.days()

    def load(self, start: str, stop: str, days: str) -> None:
        self.start.set_time(start)
        self.stop.set_time(stop)
        self.days.set_days(days)

    def on_change(self, callback: Callable[[], None]) -> None:
        for widget in (self.start, self.stop, self.days):
            widget.changed.connect(callback)


class ScheduleEditor(Folding):
    """"Download on a schedule" with this download's start, stop and days (prefilled with the defaults)."""

    def __init__(self):
        super().__init__("Download on a schedule", with_switch=True)
        self.defaults = ("02:00", "06:00", "")
        self.times = ScheduleRows()
        for row in self.times.rows():
            self.add(row)
        reset = QPushButton("Use default times")
        reset.clicked.connect(lambda: self.times.load(*self.defaults))
        self.reset_row = self.add(Row("Default times", "", reset))
        self.times.on_change(self._update)
        self.times.load(*self.defaults)

    def set_defaults(self, settings: dict[str, Any]) -> None:
        follows = self.times.values() == self.defaults
        self.defaults = tuple(settings.get(k, d) for k, d in zip(WINDOW_KEYS, self.defaults))
        if follows:
            self.times.load(*self.defaults)
        self._update()

    def window(self) -> dict[str, str]:
        """For the service; equal to the defaults means "follow the defaults" (it stores nothing then)."""
        return dict(zip(WINDOW_KEYS, self.times.values()))

    def _update(self) -> None:
        own = self.times.values() != self.defaults
        self.head.set_subtitle(("Own times: " if own else "Default times: ") + describe_window(*self.times.values()))
        self.reset_row.set_subtitle(describe_window(*self.defaults))
        self.reset_row.setVisible(own)
        line = self.rows.itemAt(self.rows.indexOf(self.reset_row) - 1).widget()
        line.setVisible(own)


class Page(QScrollArea):
    """A scrolling column of cards."""

    def __init__(self, *cards: QWidget):
        super().__init__()
        self.setObjectName("page")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("pagebody")
        self.column = QVBoxLayout(body)
        self.column.setContentsMargins(20, 18, 20, 18)
        self.column.setSpacing(18)
        self.column.addStretch(1)
        self.setWidget(body)
        for card in cards:
            self.add(card)

    def add(self, card: QWidget) -> QWidget:
        self.column.insertWidget(self.column.count() - 1, card)
        return card


class Dialog(QDialog):
    """A window-modal dialog: an optional message bar, a page of cards, and a footer with buttons."""

    def __init__(self, parent: QWidget | None, title: str, width: int = 560, height: int = 0,
                 action: str = "", cancel: str = "Cancel"):
        super().__init__(parent)
        self.setObjectName("dialog")
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.bars = QVBoxLayout()
        self.bars.setSpacing(0)
        layout.addLayout(self.bars)
        self.page = Page()
        layout.addWidget(self.page, 1)
        footer = QFrame()
        footer.setObjectName("footer")
        self.footer = QHBoxLayout(footer)
        self.footer.setContentsMargins(16, 10, 16, 10)
        self.footer.setSpacing(8)
        self.footer.addStretch(1)
        layout.addWidget(footer)
        self.cancel_button = self.add_button(cancel if action else "Close", self.reject)
        self.action_button = self.add_button(action, self.accept, primary=True) if action else None
        self.toast_widget = Toast(self.page)
        self.resize(width, height or 600)

    def add_button(self, label: str, handler: Callable[[], None], primary: bool = False) -> QPushButton:
        button = QPushButton(label)
        button.clicked.connect(handler)
        if primary:
            button.setObjectName("primary")
            button.setDefault(True)
        self.footer.addWidget(button)
        return button

    def toast(self, text: str) -> None:
        self.toast_widget.show_message(text)


def fill_width(widget: QWidget) -> QWidget:
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, widget.sizePolicy().verticalPolicy())
    return widget
