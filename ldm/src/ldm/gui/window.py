"""Main window: download list with filters, empty states and the service banner."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlsplit

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from ..batch import parse_link_list
from ..clipboard import Offer
from ..filetypes import LABELS as FILE_TYPE_LABELS
from ..format import parse_rate
from .add_dialog import AddDialog
from .dialogs import ask_text, confirm, is_url, limit_text, read_clipboard_url, short_path
from .model import FINISH_ACTIONS, SORTS, DownloadItem
from .row import DownloadRow, first_icon
from .schedule_rows import ask_schedule
from .selection import PAUSABLE, RESUMABLE, Selection
from .tools import BatchDialog, ChecksumDialog, ImportLinksDialog, LogDialog, PartsDialog, ask_login
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
# Like Adw.Banner, which has no close button.
OFFER_CSS = ".offer-bar { background-color: alpha(currentColor, 0.1); padding: 6px 6px 6px 18px; }"


class OfferBar(Gtk.Revealer):
    """A banner with a button and a close button, for the copied-link offer."""

    __gsignals__ = {"button-clicked": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, button_label: str):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.label = Gtk.Label(hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.label.add_css_class("heading")
        button = Gtk.Button(label=button_label, valign=Gtk.Align.CENTER)
        button.add_css_class("pill")
        button.connect("clicked", lambda _b: self.emit("button-clicked"))
        close = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Dismiss", valign=Gtk.Align.CENTER)
        close.add_css_class("flat")
        close.add_css_class("circular")
        close.connect("clicked", lambda _b: self.set_revealed(False))
        box = Gtk.Box(spacing=12)
        box.add_css_class("offer-bar")
        for widget in (self.label, button, close):
            box.append(widget)
        self.set_child(box)

    def set_title(self, title: str) -> None:
        self.label.set_text(title)
        self.label.set_tooltip_text(title)

    def get_title(self) -> str:
        return self.label.get_text()

    def set_revealed(self, revealed: bool) -> None:
        self.set_reveal_child(revealed)

    def get_revealed(self) -> bool:
        return self.get_reveal_child()


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Application):
        super().__init__(application=app, title="LDM", default_width=760, default_height=560)
        self.app = app
        self.client = app.client
        self.views = {name: app.downloads.view(name) for name, _ in VIEW_LABELS}
        self.selection = Selection()
        css = Gtk.CssProvider()
        css.load_from_string(OFFER_CSS)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        header = Adw.HeaderBar()
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add Download", action_name="app.add")
        header.pack_start(add)
        self.search_button = Gtk.ToggleButton(icon_name="system-search-symbolic", tooltip_text="Search (Ctrl+F)")
        header.pack_start(self.search_button)
        self.search_bar = self._build_search_bar()
        self.search_button.bind_property("active", self.search_bar, "search-mode-enabled",
                                         GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        menu = Gio.Menu()
        tools = Gio.Menu()
        running = Gio.Menu()
        running.append("Pause All", "win.pause-all")
        running.append("Resume All", "win.resume-all")
        menu.append_section(None, running)
        for label, action in (("Batch Download…", "win.batch"), ("Import Links…", "win.import-links"),
                              ("Export Links…", "win.export-links"), ("Export Unfinished…", "win.export-unfinished"),
                              ("Import Unfinished…", "win.import-unfinished")):
            tools.append(label, action)
        app_items = Gio.Menu()
        app_items.append("Preferences", "app.preferences")
        app_items.append("Keyboard Shortcuts", "app.shortcuts")
        app_items.append("About LDM", "app.about")
        finish = Gio.Menu()
        for label, value in FINISH_ACTIONS:
            finish.append(label, f"win.finish-action::{value}")
        menu.append_section(None, tools)
        menu.append_submenu("When All Finished", finish)
        menu.append_section(None, app_items)
        self.finish_action = Gio.SimpleAction.new_stateful("finish-action", GLib.VariantType.new("s"),
                                                           GLib.Variant("s", "nothing"))
        self.finish_action.connect("change-state", self._on_finish_action)
        self.add_action(self.finish_action)
        self.client.connect("notify::connected", lambda *_: self._load_finish_action())
        self._load_finish_action()
        for name, handler in (("pause-all", lambda: self.client.pause_all(self._report)),
                              ("resume-all", lambda: self.client.resume_all(self._report)),
                              ("batch", self.batch_download), ("import-links", self.import_links),
                              ("export-links", self.export_links), ("export-unfinished", self.export_unfinished),
                              ("import-unfinished", self.import_unfinished)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_, h=handler: h())
            self.add_action(action)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                       tooltip_text="Main Menu"))
        self.select_button = Gtk.ToggleButton(tooltip_text="Select")
        self.select_button.set_icon_name(first_icon(self, ("selection-mode-symbolic", "object-select-symbolic")))
        self.select_button.bind_property("active", self.selection, "active",
                                         GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        header.pack_end(self.select_button)
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
        self.clipboard_banner = OfferBar("Download")
        self.clipboard_banner.connect("button-clicked", self._download_offered)
        self.connect("notify::is-active", self._on_focus)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START,
                                margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.list.add_css_class("boxed-list")
        self.list.connect("row-activated", self._on_row_activated)
        self.selection.connect("changed", self._on_selection_changed)
        self.selection.connect("notify::active", self._on_selection_changed)
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      child=Adw.Clamp(maximum_size=900, child=self.list))
        self.empty = Adw.StatusPage(icon_name="folder-download-symbolic")
        self.stack = Gtk.Stack()
        self.stack.add_named(scrolled, "list")
        self.stack.add_named(self.empty, "empty")

        self.toasts = Adw.ToastOverlay(child=self.stack)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        view.add_top_bar(self.search_bar)
        view.add_top_bar(self.banner)
        view.add_top_bar(self.clipboard_banner)
        view.add_bottom_bar(self._build_selection_bar())
        self.set_content(view)
        shortcuts = Gtk.ShortcutController()
        for trigger, callback in (("Escape", self._leave_selection), ("<Control>a", self._select_all_shortcut)):
            shortcuts.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(trigger),
                                                    Gtk.CallbackAction.new(callback)))
        self.add_controller(shortcuts)

        drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        for model in self.views.values():
            model.connect("items-changed", self._sync_empty)
        self._show_view()

    # --- when all finished -----------------------------------------------------------------

    def _load_finish_action(self) -> None:
        if self.client.connected:
            self.client.get_settings(lambda settings, error: settings and self.finish_action.set_state(
                GLib.Variant("s", settings.get("finish_action", "nothing"))))

    def _on_finish_action(self, action, value) -> None:
        action.set_state(value)
        self.client.set_settings({"finish_action": value.get_string()}, self._report)
        if value.get_string() != "nothing":
            label = dict((v, l) for l, v in FINISH_ACTIONS)[value.get_string()]
            self.toast(f"When all downloads finish: {label}")

    # --- search, filters, sorting ----------------------------------------------------------

    TYPE_CHOICES = [("", "All types")] + [(g, l) for g, l in FILE_TYPE_LABELS.items()]
    DATE_CHOICES = [(0, "Any date"), (1, "Today"), (7, "Last 7 days"), (30, "Last 30 days")]

    def _build_search_bar(self) -> Gtk.SearchBar:
        self.search_entry = Gtk.SearchEntry(placeholder_text="Search names and links", hexpand=True)
        self.search_entry.connect("search-changed", lambda e: self.app.downloads.set_query(text=e.get_text()))
        self.type_filter = Gtk.DropDown.new_from_strings([label for _, label in self.TYPE_CHOICES])
        self.type_filter.set_tooltip_text("File type")
        self.type_filter.connect("notify::selected", lambda d, _p: self.app.downloads.set_query(
            group=self.TYPE_CHOICES[d.get_selected()][0]))
        self.date_filter = Gtk.DropDown.new_from_strings([label for _, label in self.DATE_CHOICES])
        self.date_filter.set_tooltip_text("Added")
        self.date_filter.connect("notify::selected", self._on_date_filter)
        self.sort_choice = Gtk.DropDown.new_from_strings([label for label, _ in SORTS.values()])
        self.sort_choice.set_tooltip_text("Sort by")
        self.sort_choice.connect("notify::selected", lambda d, _p: self.app.downloads.set_sort(
            list(SORTS)[d.get_selected()]))
        box = Gtk.Box(spacing=6)
        for widget in (self.search_entry, self.type_filter, self.date_filter, self.sort_choice):
            box.append(widget)
        bar = Gtk.SearchBar(child=Adw.Clamp(maximum_size=900, child=box), show_close_button=True)
        bar.connect_entry(self.search_entry)
        bar.set_key_capture_widget(self)  # start typing anywhere to search
        bar.connect("notify::search-mode-enabled", self._on_search_mode)
        return bar

    def _on_date_filter(self, dropdown, _param) -> None:
        days = self.DATE_CHOICES[dropdown.get_selected()][0]
        if not days:
            since = 0.0
        else:
            start = GLib.DateTime.new_now_local()
            start = GLib.DateTime.new_local(start.get_year(), start.get_month(), start.get_day_of_month(), 0, 0, 0)
            since = start.add_days(1 - days).to_unix()
        self.app.downloads.set_query(since=since)

    def _on_search_mode(self, bar, _param) -> None:
        if not bar.get_search_mode():  # closing the bar clears the search (filters and sort stay)
            self.search_entry.set_text("")

    def toggle_search(self) -> None:
        self.search_bar.set_search_mode(not self.search_bar.get_search_mode())

    # --- view ------------------------------------------------------------------------------

    def _show_view(self, *_) -> None:
        name = self.filters.get_active_name() or "all"
        self.selection.set_view(self.views[name])
        self.list.bind_model(self.views[name], lambda item: DownloadRow(item, self))
        self._sync_empty()

    def _sync_empty(self, *_) -> None:
        name = self.filters.get_active_name() or "all"
        downloads = self.app.downloads
        filtered = downloads.text or downloads.group or downloads.since
        title, description = ("No Matching Downloads", "Try other words or filters") if filtered else EMPTY[name]
        self.empty.set_title(title)
        self.empty.set_description(description)
        self.stack.set_visible_child_name("list" if self.views[name].get_n_items() else "empty")

    # --- selection mode -------------------------------------------------------------------

    def _build_selection_bar(self) -> Gtk.ActionBar:
        bar = Gtk.ActionBar()
        self.selection.bind_property("active", bar, "revealed", GObject.BindingFlags.SYNC_CREATE)
        self.select_all_button = Gtk.Button(label="Select All")
        self.select_all_button.connect("clicked", lambda _b: self.selection.clear() if self.selection.all_selected()
                                       else self.selection.select_all())
        bar.pack_start(self.select_all_button)
        self.selection_label = Gtk.Label(margin_start=6)
        bar.pack_start(self.selection_label)
        self.selection_buttons = []
        for label, handler, style in (("Delete Files…", self._delete_selected, "destructive-action"),
                                      ("Remove…", self._remove_selected, None),
                                      ("Resume", self._resume_selected, None),
                                      ("Pause", self._pause_selected, None)):
            button = Gtk.Button(label=label)
            if style:
                button.add_css_class(style)
            button.connect("clicked", lambda _b, h=handler: h())
            bar.pack_end(button)
            self.selection_buttons.append(button)
        return bar

    def start_selection(self, item: DownloadItem) -> None:
        """Right-click or long-press on a row: selection mode with that download ticked."""
        self.selection.active = True
        self.selection.set_selected(item.id, True)

    def _leave_selection(self, *_) -> bool:
        if not self.selection.active:
            return False
        self.selection.active = False
        return True

    def _select_all_shortcut(self, *_) -> bool:
        if not self.selection.active:
            return False
        self.selection.select_all()
        return True

    def _on_row_activated(self, _list, row: Gtk.ListBoxRow) -> None:
        if self.selection.active:
            check = row.get_child().check
            check.set_active(not check.get_active())

    def _on_selection_changed(self, *_) -> None:
        count = self.selection.count
        self.selection_label.set_text(f"{count} selected" if count else "Select downloads")
        self.select_all_button.set_label("Select None" if self.selection.all_selected() else "Select All")
        for button in self.selection_buttons:
            button.set_sensitive(count > 0)
        index = 0
        while row := self.list.get_row_at_index(index):  # rows of the shown view follow the selection
            check = row.get_child().check
            selected = self.selection.is_selected(row.get_child().item.id)
            if check.get_active() != selected:
                check.set_active(selected)
            index += 1

    def _pause_selected(self) -> None:
        items = [i for i in self.selection.items() if i.state in PAUSABLE]
        self._for_each(items, self.client.pause, "None of the selected downloads is running or queued")

    def _resume_selected(self) -> None:
        items = [i for i in self.selection.items() if i.state in RESUMABLE]
        self._for_each(items, self.client.resume, "None of the selected downloads is paused or failed")

    def _remove_selected(self) -> None:
        items = self.selection.items()
        heading = f"Remove {len(items)} Downloads?" if len(items) > 1 else "Remove Download?"
        confirm(self, heading, "Removed from the list. Downloaded files are kept.", "Remove",
                lambda: self._remove(items, False))

    def _delete_selected(self) -> None:
        items = self.selection.items()
        heading = f"Delete {len(items)} Files?" if len(items) > 1 else "Delete File?"
        confirm(self, heading, "Removed from the list and deleted from disk, including unfinished data.",
                "Delete", lambda: self._remove(items, True))

    def _remove(self, items: list[DownloadItem], delete_files: bool) -> None:
        self.selection.active = False
        self._for_each(items, lambda download_id, callback: self.client.remove(download_id, delete_files, callback))

    def _for_each(self, items: list[DownloadItem], call: Callable, nothing: str = "") -> None:
        """Call `call(id, callback)` for each download; one toast for any failures."""
        if not items:
            if nothing:
                self.toast(nothing)
            return
        pending, errors = len(items), []

        def done(_result, error) -> None:
            nonlocal pending
            pending -= 1
            if error:
                errors.append(error.message)
            if not pending and errors:
                self.toast(errors[0] if len(errors) == 1 else f"{len(errors)} downloads failed: {errors[0]}")

        for item in items:
            call(item.id, done)

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
            self.clipboard_banner.set_title(f"Download copied link? {shown}")
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

    def show_parts(self, item: DownloadItem) -> None:
        PartsDialog(self.client, self, item.id, item.title)

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
