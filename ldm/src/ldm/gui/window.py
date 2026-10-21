"""Main window: download list with filters, empty states and the service banner."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlsplit

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from ..batch import parse_link_list
from ..clipboard import Offer
from ..format import parse_rate
from .add_dialog import AddDialog
from .dialogs import ask_text, confirm, is_url, limit_text, read_clipboard_url, short_path
from .model import DownloadItem
from .row import DownloadRow
from .schedule_rows import ask_schedule
from .tools import BatchDialog, ChecksumDialog, ImportLinksDialog, LogDialog, ask_login
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
        super().__init__(application=app, title="LDM", default_width=760, default_height=560)
        self.app = app
        self.client = app.client
        self.views = {name: app.downloads.view(name) for name, _ in VIEW_LABELS}

        header = Adw.HeaderBar()
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add Download", action_name="app.add")
        header.pack_start(add)
        menu = Gio.Menu()
        tools = Gio.Menu()
        for label, action in (("Batch Download…", "win.batch"), ("Import Links…", "win.import-links"),
                              ("Export Links…", "win.export-links"), ("Export Unfinished…", "win.export-unfinished"),
                              ("Import Unfinished…", "win.import-unfinished")):
            tools.append(label, action)
        app_items = Gio.Menu()
        app_items.append("Preferences", "app.preferences")
        app_items.append("Keyboard Shortcuts", "app.shortcuts")
        app_items.append("About LDM", "app.about")
        menu.append_section(None, tools)
        menu.append_section(None, app_items)
        for name, handler in (("batch", self.batch_download), ("import-links", self.import_links),
                              ("export-links", self.export_links), ("export-unfinished", self.export_unfinished),
                              ("import-unfinished", self.import_unfinished)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_, h=handler: h())
            self.add_action(action)
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
        # 1DM's clipboard monitor: offer a copied link when the window gets focus.
        self.offer, self.offered = Offer(), ""
        self.clipboard_banner = Adw.Banner(button_label="Download")
        self.clipboard_banner.connect("button-clicked", self._download_offered)
        self.connect("notify::is-active", self._on_focus)

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
        view.add_top_bar(self.clipboard_banner)
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

    # --- clipboard -------------------------------------------------------------------------

    def _on_focus(self, *_) -> None:
        if self.is_active() and self.client.connected:
            self.client.get_settings(self._check_clipboard)

    def _check_clipboard(self, settings, error) -> None:
        if error is None and settings.get("clipboard_watch", True):
            self.get_clipboard().read_text_async(None, self._clipboard_text, settings.get("clipboard_exclude", ""))

    def _clipboard_text(self, clipboard, result, exclude: str) -> None:
        try:
            text = clipboard.read_text_finish(result)
        except GLib.Error:  # empty clipboard or not text
            return
        known = self.app.downloads.urls()
        if self.offered in known:  # added meanwhile (e.g. pasted into the add dialog)
            self.clipboard_banner.set_revealed(False)
        link = self.offer.check(text, known, exclude)
        if link:
            self.offered = link
            shown = link if len(link) <= 70 else link[:67] + "…"
            self.clipboard_banner.set_title(f"Download copied link? {GLib.markup_escape_text(shown)}")
            self.clipboard_banner.set_revealed(True)

    def _download_offered(self, banner) -> None:
        banner.set_revealed(False)
        self.request_download(self.offered, kind="torrent" if self.offered.startswith("magnet:") else "")

    # --- adding ----------------------------------------------------------------------------

    def open_add_dialog(self, url: str = "", name: str = "", headers: dict[str, str] | None = None,
                        kind: str = "", captures: list[dict] | None = None) -> None:
        if not self.client.connected:
            self.toast("Start the download service first")
            return
        AddDialog(self.client, url, name=name, headers=headers, kind=kind, on_show=self.show_download,
                  captures=captures).present(self)

    def request_download(self, url: str, name: str = "", headers: dict[str, str] | None = None,
                         kind: str = "", captures: list[dict] | None = None) -> None:
        """A link from the browser, clipboard, a drop or the desktop: the add dialog, or added at once when
        "Add without asking" is on (1DM's "skip editor")."""
        if not self.client.connected:
            self.toast("Start the download service first")
            return
        self.client.get_settings(lambda settings, error: self._quick_add(url, name, headers, kind)
                                 if settings and settings.get("skip_add_dialog")
                                 else self.open_add_dialog(url, name, headers, kind, captures))

    def _quick_add(self, url: str, name: str, headers: dict[str, str] | None, kind: str) -> None:
        options = {"name": name or None, "headers": headers or None, "kind": kind or None, "start": True}

        def add(*_) -> None:
            self.client.add(url, options, added)

        def added(download_id, error) -> None:
            if error:
                self._report(None, error)
                return
            toast = Adw.Toast(title=GLib.markup_escape_text(f"Added {name or url}"), button_label="Undo")
            toast.connect("button-clicked", lambda *_: self.client.remove(download_id, True, self._report))
            self.toasts.add_toast(toast)

        def checked(found, error) -> None:
            if not found:
                add()
                return
            toast = Adw.Toast(title="Already in your list", button_label="Add Again")
            toast.connect("button-clicked", add)
            self.toasts.add_toast(toast)
            self.show_download(found[0]["id"])

        self.client.find_duplicates(url, checked)

    def show_download(self, download_id: int) -> None:
        """Scroll to a download and focus its row."""
        self.filters.set_active_name("all")
        view = self.views["all"]
        for index in range(view.get_n_items()):
            if view.get_item(index).id == download_id:
                row = self.list.get_row_at_index(index)
                if row is not None:
                    row.grab_focus()
                return

    def _on_drop(self, _target, value: str, _x, _y) -> bool:
        url = next((line.strip() for line in value.splitlines()
                    if is_url(line) or line.strip().startswith(("magnet:", "file:"))), "")
        if not url:
            return False
        self.request_download(url)
        return True

    # --- tools (main menu) -----------------------------------------------------------------

    def batch_download(self) -> BatchDialog | None:
        return BatchDialog(self.client, self, self.toast) if self._need_service() else None

    def import_links(self) -> None:
        if self._need_service():
            self._choose_file("Import Links", False, self._read_links)

    def _read_links(self, file: Gio.File) -> None:
        try:
            text = file.load_contents(None)[1].decode(errors="replace")
        except GLib.Error as e:
            self.toast(f"Could not read the file: {e.message}")
            return
        links = parse_link_list(text)
        if not links:
            self.toast("No links found in that file")
            return
        ImportLinksDialog(self.client, self, links, self.app.downloads.urls(), self.toast)

    def export_links(self) -> None:
        name = self.filters.get_active_name() or "all"
        view = self.views[name]
        links = [view.get_item(i).url for i in range(view.get_n_items())]
        if not links:
            self.toast("No downloads to export")
            return
        self._choose_file("Export Links", True, lambda file: self._write(file, "".join(f"{u}\n" for u in links),
                                                                         f"{len(links)} links exported"),
                          "ldm-links.txt")

    def export_unfinished(self) -> None:
        if not self._need_service():
            return

        def got(text, error) -> None:
            if error:
                self._report(None, error)
                return
            count = len(json.loads(text)["downloads"])
            self._choose_file("Export Unfinished Downloads", True,
                              lambda file: self._write(file, text, f"{count} downloads exported"),
                              "ldm-unfinished.json")

        self.client.export_downloads(True, got)

    def import_unfinished(self) -> None:
        if not self._need_service():
            return

        def chosen(file: Gio.File) -> None:
            try:
                text = file.load_contents(None)[1].decode()
            except (GLib.Error, UnicodeDecodeError) as e:
                self.toast(f"Could not read the file: {getattr(e, 'message', e)}")
                return
            self.client.import_downloads(text, lambda ids, error: self._report(None, error) if error else
                                         self.toast(f"{len(ids)} downloads imported — they are paused"))

        self._choose_file("Import Unfinished Downloads", False, chosen)

    def _need_service(self) -> bool:
        if not self.client.connected:
            self.toast("Start the download service first")
        return self.client.connected

    def _choose_file(self, title: str, save: bool, on_file: Callable[[Gio.File], None], name: str = "") -> None:
        dialog = Gtk.FileDialog(title=title, initial_name=name or None)

        def done(d, result) -> None:
            try:
                file = d.save_finish(result) if save else d.open_finish(result)
            except GLib.Error:
                return  # cancelled
            on_file(file)

        (dialog.save if save else dialog.open)(self, None, done)

    def _write(self, file: Gio.File, text: str, message: str) -> None:
        try:
            file.replace_contents(text.encode(), None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None)
            self.toast(message)
        except GLib.Error as e:
            self.toast(f"Could not save: {e.message}")

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

    def ask_schedule(self, item: DownloadItem) -> None:
        def with_row(row, error):
            if error:
                self._report(None, error)
                return
            self.client.get_settings(lambda settings, error: self._report(None, error) if error else
                                     ask_schedule(self, item.title, row, settings, save))

        def save(scheduled: bool, window: dict[str, str]) -> None:
            self.client.set_schedule(item.id, scheduled, window,
                                     lambda r, e: self._report(r, e) if e else self.app.refresh(item.id))

        self.client.get(item.id, with_row)

    def show_checksum(self, item: DownloadItem) -> None:
        ChecksumDialog(self.client, self, item.id, item.title)

    def show_log(self, item: DownloadItem) -> None:
        LogDialog(self.client, self, item.id, item.title)

    def ask_rename(self, item: DownloadItem) -> None:
        ask_text(self, "Rename", "A new name for the file. An existing file is never replaced.", "Rename",
                 item.name or item.title, "", lambda name: name and self.client.rename(item.id, name, self._moved))

    def ask_move(self, item: DownloadItem) -> None:
        dialog = Gtk.FileDialog(title="Move to Folder")
        if item.dir:
            dialog.set_initial_folder(Gio.File.new_for_path(item.dir))

        def done(d, result) -> None:
            try:
                folder = d.select_folder_finish(result)
            except GLib.Error:
                return
            self.client.move_to(item.id, folder.get_path(), self._moved)

        dialog.select_folder(self, None, done)

    def _moved(self, path, error) -> None:
        if error:
            self._report(None, error)
        else:
            self.toast(f"Now at {short_path(path)}")

    def ask_login(self, item: DownloadItem) -> None:
        host = urlsplit(item.url).hostname or ""
        ask_login(self, host, "", lambda h, user, password, subdomains: self.client.set_login(
            h, user, password, subdomains, lambda r, e: self._report(r, e) if e else self.resume(item)))

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
