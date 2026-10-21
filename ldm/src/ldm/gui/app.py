"""The ldm desktop app (`ldm-gui`). Pure D-Bus client: closing it never stops downloads."""

from __future__ import annotations

import json
import os
import sys

from gi.repository import Adw, Gio, GLib, Gtk

from .. import __version__
from .client import DaemonClient
from .model import DownloadList
from .preferences import PreferencesDialog
from .window import MainWindow

APP_ID = "io.github.adiashishz.LDM"  # must differ from the daemon's bus name io.github.adiashishz.LDM.Service
NOTIFY_STATES = {"done": "Download finished", "error": "Download failed", "expired": "Download link expired"}


class Application(Adw.Application):
    def __init__(self, bus_address: str | None = None, flags: Gio.ApplicationFlags = Gio.ApplicationFlags.HANDLES_OPEN):
        super().__init__(application_id=APP_ID, flags=flags)
        self.bus_address = bus_address or os.environ.get("LDM_BUS_ADDRESS")
        self.client: DaemonClient | None = None
        self.downloads = DownloadList()
        self._was_running: set[int] = set()
        self._pending_requests: list[dict] = []  # browser add-requests waiting for the daemon

    # --- lifecycle -------------------------------------------------------------------------

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self.client = DaemonClient.for_address(self.bus_address) if self.bus_address else DaemonClient()
        self.client.connect("notify::connected", self._on_connection)
        self.client.connect("added", lambda _c, i: self.refresh(i))
        self.client.connect("removed", lambda _c, i: self.downloads.remove(i))
        self.client.connect("state-changed", self._on_state)
        self.client.connect("progress", lambda _c, batch: self.downloads.apply_progress(batch))

        for name, handler, accels, param in (
            ("add", lambda *_: self.window().open_add_dialog(), ["<Control>n"], None),
            ("preferences", self._preferences, ["<Control>comma"], None),
            ("shortcuts", self._shortcuts, ["<Control>question"], None),
            ("search", lambda *_: self.window().toggle_search(), ["<Control>f"], None),
            ("about", self._about, [], None),
            ("show", lambda *_: self.activate(), [], None),
            ("open-file", self._open_file, [], GLib.VariantType.new("t")),
            ("add-request", self._add_request, [], GLib.VariantType.new("s")),
            ("quit", lambda *_: self.quit(), ["<Control>q"], None),
        ):
            action = Gio.SimpleAction.new(name, param)
            action.connect("activate", handler)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        self.set_accels_for_action("window.close", ["<Control>w"])
        self._on_connection()
        GLib.timeout_add_seconds(2, self._refresh_torrents)

    def do_activate(self) -> None:
        self.window().present()

    def do_open(self, files, n_files, hint) -> None:
        """Links and files handed over by the desktop: http(s) URLs, magnet: links, .torrent files."""
        window = self.window()
        window.present()
        for file in files:
            uri = file.get_uri()
            torrent = uri.startswith("magnet:") or uri.lower().endswith(".torrent")
            window.request_download(uri, kind="torrent" if torrent else "")

    def window(self) -> MainWindow:
        return self.get_active_window() or MainWindow(self)

    # --- daemon sync -----------------------------------------------------------------------

    def _on_connection(self, *_) -> None:
        self.lookup_action("add").set_enabled(self.client.connected)
        if self.client.connected:
            self.client.list(lambda rows, error: rows is not None and self.downloads.load(rows))
            while self._pending_requests:
                self._show_request(self._pending_requests.pop(0))

    def _add_request(self, _action, parameter) -> None:
        """From the browser bridge: open the add dialog prefilled (JSON: url, filename, headers)."""
        try:
            request = json.loads(parameter.get_string())
        except ValueError:
            return
        self.window().present()
        if self.client.connected:
            self._show_request(request)
            return
        self._pending_requests.append(request)
        GLib.timeout_add_seconds(10, self._request_timeout, request)

    def _show_request(self, request: dict) -> None:
        self.window().request_download(request.get("url", ""), name=request.get("filename") or "",
                                       headers=request.get("headers") or {}, kind=request.get("kind") or "",
                                       captures=request.get("captures") or [])

    def _request_timeout(self, request: dict) -> bool:
        if request in self._pending_requests:
            self._pending_requests.remove(request)
            self.window().toast("Could not reach the download service for the browser's download")
        return GLib.SOURCE_REMOVE

    def _refresh_torrents(self) -> bool:
        """Peers / upload speed / ratio for active torrent rows (not part of the Progress signal)."""
        if self.client.connected:
            store = self.downloads.store
            for i in range(store.get_n_items()):
                item = store.get_item(i)
                if item.kind == "torrent" and item.state in ("running", "seeding"):
                    self.refresh(item.id)
        return GLib.SOURCE_CONTINUE

    def refresh(self, download_id: int) -> None:
        self.client.get(download_id, lambda row, error: row is not None and self.downloads.upsert(row))

    def _on_state(self, _client, download_id: int, state: str, error: str) -> None:
        self.downloads.apply_state(download_id, state, error)
        self.refresh(download_id)  # name/size become known after the first probe
        if state == "running":
            self._was_running.add(download_id)
        elif state in NOTIFY_STATES and download_id in self._was_running:
            self._was_running.discard(download_id)
            self._notify(download_id, state, error)

    def _notify(self, download_id: int, state: str, error: str) -> None:
        item = self.downloads.get(download_id)
        notification = Gio.Notification.new(NOTIFY_STATES[state])
        notification.set_body(item.title if item else error)
        if state == "done":
            target = GLib.Variant("t", download_id)
            notification.set_default_action_and_target("app.open-file", target)
            notification.add_button_with_target("Open", "app.open-file", target)
        else:
            notification.set_default_action("app.show")
        self.send_notification(f"download-{download_id}", notification)

    # --- actions ---------------------------------------------------------------------------

    def _open_file(self, _action, parameter) -> None:
        item = self.downloads.get(parameter.get_uint64())
        if item:
            self.window().open_file(item)

    def _preferences(self, *_) -> None:
        PreferencesDialog(self.client).present(self.window())

    def _shortcuts(self, *_) -> None:
        dialog = Adw.AlertDialog(heading="Keyboard Shortcuts",
                                 body="Ctrl+N  Add download\nCtrl+F  Search\nCtrl+,  Preferences\nCtrl+W  Close window\n"
                                      "Ctrl+Q  Quit (downloads continue in the background)")
        dialog.add_response("close", "Close")
        dialog.present(self.window())

    def _about(self, *_) -> None:
        Adw.AboutDialog(application_name="LDM", application_icon=APP_ID,
                        version=__version__, comments="Download manager for Linux: files, videos and torrents",
                        developers=["LDM contributors"]).present(self.window())


def main() -> int:
    Gtk.Window.set_default_icon_name("folder-download-symbolic")
    return Application().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
