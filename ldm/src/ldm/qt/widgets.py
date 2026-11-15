"""Small pieces of the window: the message bar under the toolbar, toasts, and the empty-list message."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout, QWidget

from .theme import Colors, icon


class MessageBar(QFrame):
    """A line of text with one button and, optionally, a close button (service not running, copied link)."""

    button_clicked = Signal()

    def __init__(self, button_label: str, colors: Callable[[], Colors], closable: bool):
        super().__init__()
        self.setObjectName("banner")
        self.colors = colors
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 6, 8, 6)
        layout.setSpacing(12)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.button = QPushButton(button_label)
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.clicked.connect(self.button_clicked)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.button)
        self.close_button = QToolButton()
        self.close_button.setAccessibleName("Dismiss")
        self.close_button.setToolTip("Dismiss")
        self.close_button.setFixedSize(28, 28)
        self.close_button.clicked.connect(self.hide)
        self.close_button.setVisible(closable)
        layout.addWidget(self.close_button)
        self.restyle()
        self.hide()

    def restyle(self) -> None:
        self.close_button.setIcon(icon("close", self.colors().dim, 14, 2))

    def show_text(self, text: str) -> None:
        self.label.setText(text)
        self.label.setToolTip(text)
        self.show()


class Toast(QFrame):
    """A short message floating at the bottom of the list, with an optional button (Undo, Add again)."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("toast")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 8, 8)
        layout.setSpacing(8)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.button = QPushButton()
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.clicked.connect(self._clicked)
        layout.addWidget(self.label)
        layout.addWidget(self.button)
        self.timer = QTimer(self, singleShot=True, interval=4000)
        self.timer.timeout.connect(self.hide)
        self._action: Callable[[], None] | None = None
        parent.installEventFilter(self)
        self.hide()

    def show_message(self, text: str, button: str = "", action: Callable[[], None] | None = None) -> None:
        self.label.setText(text)
        self.button.setText(button)
        self.button.setVisible(bool(button))
        self._action = action
        self.adjustSize()
        self._place()
        self.show()
        self.raise_()
        self.timer.start(6000 if button else 4000)

    def _clicked(self) -> None:
        self.hide()
        if self._action:
            self._action()

    def _place(self) -> None:
        parent = self.parentWidget()
        width = min(self.sizeHint().width(), parent.width() - 32)
        self.resize(width, self.sizeHint().height())
        self.move((parent.width() - width) // 2, parent.height() - self.height() - 20)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Resize and self.isVisible():
            self._place()
        return False


class EmptyState(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("empty")
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        self.title = QLabel()
        self.title.setObjectName("title")
        self.description = QLabel()
        self.description.setWordWrap(True)
        for label in (self.title, self.description):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(label)
        layout.addStretch(2)

    def set_text(self, title: str, description: str) -> None:
        self.title.setText(title)
        self.description.setText(description)
