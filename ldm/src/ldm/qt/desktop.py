"""The window and the Linux desktop: its bus name and actions (browser, clipboard watcher, tray, launcher),
one window only, and notifications while it is open. See ldm.ui.desktop for the D-Bus side."""

from __future__ import annotations

import logging
import os
from concurrent.futures import TimeoutError as FutureTimeout
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication

from ..ui.desktop import Notifier, add_request, claim, forward
from .client import DaemonClient

if TYPE_CHECKING:
    from .window import MainWindow

log = logging.getLogger(__name__)

NOTIFY_STATES = {"done": "Download finished", "error": "Download failed", "expired": "Download link expired"}
REQUEST_TIMEOUT_MS = 10_000  # how long a browser request waits for the service before giving up


class Desktop(QObject):
    def __init__(self, client: DaemonClient):
        super().__init__()
        self.client = client
        self.remote = client.remote
        self.window: MainWindow | None = None
        self.pending: list[tuple[str, Any]] = []  # links and requests waiting for the service
        self.was_running: set[int] = set()
        self.notifier = Notifier(lambda key, action: self.remote.post(lambda: self._clicked(key, action)))
        client.connected_changed.connect(self._connected)
        client.state_changed.connect(self._state_changed)

    # --- one window -----------------------------------------------------------------------------

    def claim(self) -> bool | None:
        """Take the window's bus name: True; False when another LDM window has it; None without a bus."""
        try:
            return self.remote.run(lambda bus: claim(bus, self._from_bus))
        except (ConnectionError, FutureTimeout, OSError) as e:
            log.warning("not on the session bus, so other programs can't reach the window: %s", e)
            return None

    def forward(self, uris: list[str], token: str = "") -> bool:
        """Give this start's links to the window that is open already (and bring it to the front)."""
        data = {"activation-token": token} if token else {}
        try:
            self.remote.run(lambda bus: forward(bus, uris, data))
            return True
        except Exception as e:  # noqa: BLE001 (the other window didn't answer)
            log.warning("could not reach the open LDM window: %s", e)
            return False

    # --- calls from the desktop -----------------------------------------------------------------

    def _from_bus(self, name: str, arguments: list, platform_data: dict) -> None:
        self.remote.post(lambda: self.handle(name, arguments, platform_data))

    def handle(self, name: str, arguments: list, platform_data: dict | None = None) -> None:
        """On the window's thread: an activation, links to open, or an action."""
        window = self.window
        if window is None:
            return
        token = (platform_data or {}).get("activation-token") or (platform_data or {}).get("desktop-startup-id")
        if name in ("activate", "show"):
            self.present(token)
        elif name == "open":
            self.present(token)
            for uri in arguments:
                self._when_connected("link", uri)
        elif name == "add-request":
            request = add_request(arguments)
            if request:
                self.present(token)
                self._when_connected("request", request)
        elif name == "open-file" and arguments:
            download = window.downloads.get(int(arguments[0]))
            if download is not None:
                window.open_file(download)
        elif name == "quit":
            window.close()
            QApplication.quit()

    def present(self, token: str | None = None) -> None:
        """Show the window and bring it to the front. On Wayland the compositor decides; the activation token
        from the caller (if any) is what lets it."""
        if token:
            os.environ["XDG_ACTIVATION_TOKEN"] = token  # read by Qt's Wayland support when activating
        self.window.present()

    def _when_connected(self, kind: str, value: Any) -> None:
        if self.client.connected:
            self._run(kind, value)
            return
        self.pending.append((kind, value))
        QTimer.singleShot(REQUEST_TIMEOUT_MS, lambda: self._give_up(kind, value))

    def _run(self, kind: str, value: Any) -> None:
        if kind == "link":
            self.window.request_download(value)
        else:
            self.window.show_request(value)

    def _give_up(self, kind: str, value: Any) -> None:
        if (kind, value) in self.pending:
            self.pending.remove((kind, value))
            self.window.toast("Could not reach the download service for that download")

    def _connected(self, connected: bool) -> None:
        if connected:
            while self.pending:
                self._run(*self.pending.pop(0))

    # --- notifications --------------------------------------------------------------------------

    def _state_changed(self, download_id: int, state: str, error: str) -> None:
        """Downloads that ran while the window was open: tell when they finish, fail or expire."""
        if state == "running":
            self.was_running.add(download_id)
        elif state in NOTIFY_STATES and download_id in self.was_running:
            self.was_running.discard(download_id)
            download = self.window.downloads.get(download_id) if self.window else None
            body = download.title if download else error
            if state != "done" and error and download:
                body = f"{download.title}: {error}"
            actions = ["default", "Open", "open", "Open"] if state == "done" else ["default", "Show"]
            self.remote.submit(lambda bus: self.notifier.notify(bus, (download_id, state), NOTIFY_STATES[state],
                                                                body, actions))

    def _clicked(self, key: tuple[int, str], action: str) -> None:
        download_id, state = key
        if state == "done":  # the notification itself or its Open button: open the file
            self.handle("open-file", [download_id])
        else:
            self.handle("show", [])
