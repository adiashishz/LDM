"""Main window: download list with filters, empty states and the service banner."""

from __future__ import annotations

from typing import TYPE_CHECKING

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from ..format import parse_rate
from .add_dialog import AddDialog
from .dialogs import ask_text, confirm, is_url, limit_text, read_clipboard_url
from .model import DownloadItem
from .row import DownloadRow
from .torrent_details import TorrentDetailsDialog

if TYPE_CHECKING:
    from .app import Application

VIEW_LABELS = [("all", "All"), ("downloading", "Downloading"), ("finished", "Finished"), ("problems", "Problems")]
EMPTY = {
    "all": ("No Downloads", "Add a link with the + button, paste one, or drop it here"),
    "downloading": ("Nothing Downloading", "Active, queued and paused downloads appear here"),
    "finished": ("No Finished Downloads", "Completed files appear here"),
    "problems": ("No Problems", "Failed downloads and expired links appear here"),
}


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Application):
        super().__init__(application=app, title="Downloads", default_width=760, default_height=560)
        self.app = app
        self.client = app.client
        self.views = {name: app.downloads.view(name) for name, _ in VIEW_LABELS}

        header = Adw.HeaderBar()
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add Download", action_name="app.add")
        header.pack_start(add)
        menu = Gio.Menu()
        menu.append("Preferences", "app.preferences")
        menu.append("Keyboard Shortcuts", "app.shortcuts")
        menu.append("About Downloads", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                       tooltip_text="Main Menu"))
        self.filters = Adw.ToggleGroup()
        for name, label in VIEW_LABELS:
            self.filters.add(Adw.Toggle(name=name, label=label))
        self.filters.set_active_name("all")
        self.filters.connect("notify::active-name", self._show_view)
        header.set_title_widget(self.filters)

        self.banner = Adw.Banner(title="The download service is not running", button_label="Start")
        self.banner.connect("button-clicked", self._start_daemon)
        self.client.bind_property("connected", self.banner, "revealed",
                                  GObject.BindingFlags.SYNC_CREATE | GObject.BindingFlags.INVERT_BOOLEAN)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START,
                                margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.list.add_css_class("boxed-list")
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      child=Adw.Clamp(maximum_size=900, child=self.list))
        self.empty = Adw.StatusPage(icon_name="folder-download-symbolic")
        self.stack = Gtk.Stack()
        self.stack.add_named(scrolled, "list")
        self.stack.add_named(self.empty, "empty")

        self.toasts = Adw.ToastOverlay(child=self.stack)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        view.add_top_bar(self.banner)
        self.set_content(view)

        drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        for model in self.views.values():
            model.connect("items-changed", self._sync_empty)
        self._show_view()

    # --- view ------------------------------------------------------------------------------

    def _show_view(self, *_) -> None:
        name = self.filters.get_active_name() or "all"
        self.list.bind_model(self.views[name], lambda item: DownloadRow(item, self))
        self._sync_empty()

    def _sync_empty(self, *_) -> None:
        name = self.filters.get_active_name() or "all"
        title, description = EMPTY[name]
        self.empty.set_title(title)
        self.empty.set_description(description)
        self.stack.set_visible_child_name("list" if self.views[name].get_n_items() else "empty")

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text)))

    def _report(self, _result, error) -> None:
        if error:
            self.toast(error.message)

    # --- service ---------------------------------------------------------------------------

    def _start_daemon(self, _banner) -> None:
        try:
            self.client.start_daemon()
        except OSError as e:
            self.toast(f"Could not start the download service: {e}")
            return
        GLib.timeout_add_seconds(5, self._check_started)

    def _check_started(self) -> bool:
        if not self.client.connected:
            self.toast("The download service did not start — see ~/.local/state/ldm/daemon.log")
        return GLib.SOURCE_REMOVE

    # --- adding ----------------------------------------------------------------------------

    def open_add_dialog(self, url: str = "", name: str = "", headers: dict[str, str] | None = None,
                        kind: str = "") -> None:
        if not self.client.connected:
            self.toast("Start the download service first")
            return
        AddDialog(self.client, url, name=name, headers=headers, kind=kind).present(self)

    def _on_drop(self, _target, value: str, _x, _y) -> bool:
        url = next((line.strip() for line in value.splitlines()
                    if is_url(line) or line.strip().startswith(("magnet:", "file:"))), "")
        if not url:
            return False
        self.open_add_dialog(url)
        return True

    # --- row actions (called by DownloadRow) -----------------------------------------------

    def pause(self, item: DownloadItem) -> None:
        self.client.pause(item.id, self._report)

    def resume(self, item: DownloadItem) -> None:
        self.client.resume(item.id, self._report)

    def open_file(self, item: DownloadItem) -> None:
        if item.path:
            Gtk.FileLauncher.new(Gio.File.new_for_path(item.path)).launch(self, None, self._launched)

    def open_folder(self, item: DownloadItem) -> None:
        if item.path:
            Gtk.FileLauncher.new(Gio.File.new_for_path(item.path)).open_containing_folder(
                self, None, self._folder_opened)

    def _launched(self, launcher, result) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as e:
            self.toast(f"Could not open the file: {e.message}")

    def _folder_opened(self, launcher, result) -> None:
        try:
            launcher.open_containing_folder_finish(result)
        except GLib.Error as e:
            self.toast(f"Could not open the folder: {e.message}")

    def copy_link(self, item: DownloadItem) -> None:
        self.get_clipboard().set(item.url)
        self.toast("Link copied")

    def ask_new_url(self, item: DownloadItem) -> None:
        dialog = ask_text(self, "Set New Link",
                          "Paste a fresh link to the same file. Downloaded data is kept when the size matches.",
                          "Set Link", "", "https://…", lambda url: self._set_url(item, url))
        entry = dialog.get_extra_child()
        read_clipboard_url(self, lambda url: entry.get_text() or entry.set_text(url))

    def _set_url(self, item: DownloadItem, url: str) -> None:
        if not is_url(url):
            self.toast("That is not an http(s) link")
            return
        self.client.set_url(item.id, url, self._report)

    def ask_speed_limit(self, item: DownloadItem) -> None:
        ask_text(self, "Speed Limit", "Maximum speed for this download, e.g. 500K or 2M. 0 means unlimited.",
                 "Set", limit_text(item.speed_limit), "0", lambda text: self._set_limit(item, text))

    def _set_limit(self, item: DownloadItem, text: str) -> None:
        try:
            rate = parse_rate(text or "0")
        except ValueError:
            self.toast(f"Invalid speed limit: {text}")
            return
        item.speed_limit = rate
        self.client.set_speed_limit(item.id, rate, self._report)

    def show_torrent_details(self, item: DownloadItem) -> None:
        TorrentDetailsDialog(self.client, item).present(self)

    def move_to_top(self, item: DownloadItem) -> None:
        # The daemon sends no signal for position changes, so fetch the row to re-sort it.
        self.client.move(item.id, 0, lambda r, e: self._report(r, e) if e else self.app.refresh(item.id))

    def remove(self, item: DownloadItem) -> None:
        self.client.remove(item.id, False, self._report)

    def remove_and_delete(self, item: DownloadItem) -> None:
        confirm(self, "Delete File?", f"“{item.title}” will be removed from the list and deleted from disk.",
                "Delete", lambda: self.client.remove(item.id, True, self._report))
