"""`ldm-gui --qt` (for now): LDM's new window, a client of the background service."""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from .. import __version__
from .client import DaemonClient
from .theme import ui_font
from .window import MainWindow

APP_ID = "io.github.adiashishz.LDM"


def main(argv: list[str] | None = None) -> int:
    argv = [a for a in (argv if argv is not None else sys.argv) if a != "--qt"]
    QCoreApplication.setOrganizationName("LDM")
    QCoreApplication.setApplicationName("LDM")
    QCoreApplication.setApplicationVersion(__version__)
    QApplication.setDesktopFileName(APP_ID)  # Wayland: the window's icon and name come from the .desktop file
    app = QApplication(argv)
    app.setStyle("Fusion")  # the same widgets on every system; the colours come from the design
    app.setFont(ui_font())
    client = DaemonClient(os.environ.get("LDM_BUS_ADDRESS"))
    window = MainWindow(client)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
