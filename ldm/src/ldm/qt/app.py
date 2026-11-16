"""`ldm-gui --qt` (for now): LDM's new window, a client of the background service.

    ldm-gui [LINK…]                   open the window (links: http(s), magnet: or .torrent files)
    ldm-gui --gapplication-service    started by D-Bus activation: wait hidden for a call from the desktop

Only one window runs: a second start hands its links to the open window and exits.
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtWidgets import QApplication

from .. import __version__
from .client import DaemonClient
from .theme import ui_font
from .window import MainWindow

APP_ID = "io.github.adiashishz.LDM"
SERVICE_FLAG = "--gapplication-service"  # the name GTK's GApplication used; the .service file passes it
SERVICE_WAIT_MS = 20_000  # started by D-Bus activation but never asked to show: quit after this


def main(argv: list[str] | None = None) -> int:
    argv = [a for a in (argv if argv is not None else sys.argv) if a != "--qt"]
    service = SERVICE_FLAG in argv
    links = [a for a in argv[1:] if not a.startswith("--")]
    QCoreApplication.setOrganizationName("LDM")
    QCoreApplication.setApplicationName("LDM")
    QCoreApplication.setApplicationVersion(__version__)
    QApplication.setDesktopFileName(APP_ID)  # Wayland: the window's icon and name come from the .desktop file
    app = QApplication([a for a in argv if a != SERVICE_FLAG])
    app.setStyle("Fusion")  # the same widgets on every system; the colours come from the design
    app.setFont(ui_font())
    client = DaemonClient(os.environ.get("LDM_BUS_ADDRESS"))

    desktop = None
    if sys.platform.startswith("linux"):
        from .desktop import Desktop

        desktop = Desktop(client)
        if desktop.claim() is False:  # an LDM window is open already: it takes over this start
            token = os.environ.get("XDG_ACTIVATION_TOKEN") or os.environ.get("DESKTOP_STARTUP_ID", "")
            forwarded = desktop.forward([to_uri(link) for link in links], token)
            client.close()
            return 0 if forwarded else 1

    window = MainWindow(client)
    if desktop is not None:
        desktop.window = window
    if not service:
        window.show()
    elif desktop is not None:
        QTimer.singleShot(SERVICE_WAIT_MS, lambda: window.isVisible() or app.quit())
    for link in links:
        if desktop is not None:
            desktop.handle("open", [to_uri(link)])
        else:
            window.request_download(to_uri(link))
    return app.exec()


def to_uri(link: str) -> str:
    """A path on the command line (a .torrent file) as a file:// link; links stay as they are."""
    if "://" in link or link.startswith("magnet:"):
        return link
    from pathlib import Path

    return Path(link).expanduser().resolve().as_uri()


if __name__ == "__main__":
    sys.exit(main())
