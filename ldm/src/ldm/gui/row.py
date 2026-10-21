"""One row of the download list."""

from __future__ import annotations

from typing import TYPE_CHECKING

from gi.repository import Gio, GLib, GObject, Gtk, Pango

from .model import DownloadItem

if TYPE_CHECKING:
    from .window import MainWindow

# Icon candidates, best first: not every icon theme (e.g. Breeze) has every name.
PAUSE = ("media-playback-pause-symbolic",)
START = ("media-playback-start-symbolic",)
RETRY = ("view-refresh-symbolic", "object-rotate-right-symbolic")
LINK = ("insert-link-symbolic", "edit-paste-symbolic", "document-edit-symbolic", "view-refresh-symbolic")
OPEN = ("document-open-symbolic", "folder-open-symbolic", "folder-symbolic")
GENERIC_FILE = ("text-x-generic-symbolic", "document-symbolic", "folder-download-symbolic")

# Primary button per state: (icons, tooltip, handler name on the window)
PRIMARY = {
    "running": (PAUSE, "Pause", "pause"),
    "processing": (PAUSE, "Pause", "pause"),
    "seeding": (PAUSE, "Stop seeding", "pause"),
    "queued": (PAUSE, "Pause", "pause"),
    "paused": (START, "Resume", "resume"),
    "error": (RETRY, "Retry", "resume"),
    "changed": (RETRY, "Start over", "resume"),
    "expired": (LINK, "Set new link", "ask_new_url"),
    "done": (OPEN, "Open", "open_file"),
}

# Menu actions: name -> (label, handler, states where enabled; None = always)
ACTIONS = {
    "open": ("Open", "open_file", {"done", "seeding"}),
    "open-folder": ("Open Folder", "open_folder", {"done", "seeding"}),
    "details": ("Torrent Details…", "show_torrent_details", None),
    "parts": ("Parts…", "show_parts", {"queued", "running", "paused", "error", "expired", "changed"}),
    "copy-link": ("Copy Link", "copy_link", None),
    "set-url": ("Set New Link…", "ask_new_url", {"paused", "error", "expired", "changed", "queued"}),
    "limit": ("Speed Limit…", "ask_speed_limit", {"queued", "running", "paused", "error"}),
    "schedule": ("Schedule…", "ask_schedule", {"queued", "running", "paused", "error"}),
    "move-top": ("Move to Top", "move_to_top", {"queued", "paused"}),
    "rename": ("Rename…", "ask_rename", {"queued", "paused", "error", "expired", "changed", "done"}),
    "move-to": ("Move to Folder…", "ask_move", {"queued", "paused", "error", "expired", "changed", "done"}),
    "checksum": ("Checksum…", "show_checksum", {"done", "seeding"}),
    "login": ("Add Login…", "ask_login", {"error"}),  # only for "login required" errors
    "log": ("Diagnostics…", "show_log", None),
    "remove": ("Remove from List", "remove", None),
    "delete": ("Remove and Delete File", "remove_and_delete", None),
}


def first_icon(widget: Gtk.Widget, names) -> str:
    theme = Gtk.IconTheme.get_for_display(widget.get_display())
    return next((n for n in names if theme.has_icon(n)), names[-1])


def file_icon_names(name: str) -> list[str]:
    content_type, _ = Gio.content_type_guess(name or None, None)
    return [*Gio.content_type_get_symbolic_icon(content_type).get_names(), *GENERIC_FILE]


class DownloadRow(Gtk.Box):
    def __init__(self, item: DownloadItem, window: MainWindow):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                         margin_top=10, margin_bottom=10, margin_start=12, margin_end=8)
        self.item = item
        self.window = window

        self.icon = Gtk.Image(pixel_size=32, valign=Gtk.Align.CENTER)
        self.append(self.icon)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.title.add_css_class("heading")
        self.progress = Gtk.ProgressBar()
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.status.add_css_class("caption")
        self.status.add_css_class("dim-label")
        for widget in (self.title, self.progress, self.status):
            text.append(widget)
        self.append(text)

        self.primary = Gtk.Button(valign=Gtk.Align.CENTER)
        self.primary.add_css_class("flat")
        self.primary.add_css_class("circular")
        self.primary.connect("clicked", self._on_primary)
        self.append(self.primary)

        menu = Gio.Menu()
        main, removal = Gio.Menu(), Gio.Menu()
        for name, (label, _, _) in ACTIONS.items():
            entry = Gio.MenuItem.new(label, f"dl.{name}")
            if name in ("details", "login", "parts"):  # only torrents / login errors / file downloads
                entry.set_attribute_value("hidden-when", GLib.Variant("s", "action-disabled"))
            (removal if name in ("remove", "delete") else main).append_item(entry)
        menu.append_section(None, main)
        menu.append_section(None, removal)
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, valign=Gtk.Align.CENTER,
                              tooltip_text="More")
        more.add_css_class("flat")
        self.append(more)

        self.actions = Gio.SimpleActionGroup()
        for name, (_, handler, _) in ACTIONS.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, h=handler: getattr(self.window, h)(self.item))
            self.actions.add_action(action)
        self.insert_action_group("dl", self.actions)

        sync = GObject.BindingFlags.SYNC_CREATE
        item.bind_property("title", self.title, "label", sync)
        item.bind_property("title", self.title, "tooltip-text", sync)
        item.bind_property("status", self.status, "label", sync)
        item.bind_property("fraction", self.progress, "fraction", sync)
        item.connect("notify::state", self._sync)
        item.connect("notify::error", self._sync)
        item.connect("notify::title", self._sync_icon)
        self._sync()
        self._sync_icon()

    def _sync(self, *_) -> None:
        state = self.item.state
        icons, tooltip, _ = PRIMARY.get(state, PRIMARY["paused"])
        self.primary.set_icon_name(first_icon(self, icons))
        self.primary.set_tooltip_text(tooltip)
        self.progress.set_visible(state != "done")
        if state in ("error", "expired", "changed"):
            self.status.remove_css_class("dim-label")
            self.status.add_css_class("error")
        else:
            self.status.remove_css_class("error")
            self.status.add_css_class("dim-label")
        for name, (_, _, states) in ACTIONS.items():
            enabled = states is None or state in states
            if name == "details":
                enabled = self.item.kind == "torrent"
            elif name == "parts":
                enabled = enabled and self.item.kind == "http"
            elif name == "login":
                enabled = state == "error" and self.item.error.startswith("login required")
            elif name in ("rename", "move-to") and self.item.kind == "torrent":
                enabled = state == "done"
            elif name in ("set-url", "limit") and self.item.kind == "torrent":
                enabled = enabled and name == "limit"
            self.actions.lookup_action(name).set_enabled(enabled)

    def _sync_icon(self, *_) -> None:
        self.icon.set_from_icon_name(first_icon(self, file_icon_names(self.item.title)))

    def _on_primary(self, _button) -> None:
        _, _, handler = PRIMARY.get(self.item.state, PRIMARY["paused"])
        getattr(self.window, handler)(self.item)
