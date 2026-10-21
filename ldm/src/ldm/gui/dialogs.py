"""Add-download dialog and small prompts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from gi.repository import Adw, Gio, GLib, Gtk

from ..format import human, parse_rate
from .client import DaemonClient


def is_url(text: str) -> bool:
    parts = urlsplit(text.strip())
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def read_clipboard_url(widget: Gtk.Widget, apply: Callable[[str], None]) -> None:
    """Call `apply(url)` if the clipboard holds an http(s) URL (allowed: our window has focus)."""
    clipboard = widget.get_clipboard()

    def done(cb, result):
        try:
            text = cb.read_text_finish(result)
        except GLib.Error:
            return
        if text and is_url(text):
            apply(text.strip())

    clipboard.read_text_async(None, done)


def parse_limit(text: str) -> int | None:
    """Speed-limit entry text -> bytes/s; '' = unlimited (0); None if invalid."""
    if not text.strip():
        return 0
    try:
        return parse_rate(text)
    except ValueError:
        return None


class AddDialog(Adw.Dialog):
    def __init__(self, client: DaemonClient, url: str = "", on_added: Callable[[int], None] | None = None,
                 name: str = "", headers: dict[str, str] | None = None):
        super().__init__(title="Add Download", content_width=520)
        self.client = client
        self.on_added = on_added
        self.folder: str | None = None

        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        self.add_button = Gtk.Button(label="Add", sensitive=False)
        self.add_button.add_css_class("suggested-action")
        self.add_button.connect("clicked", self._on_add)
        header.pack_start(cancel)
        header.pack_end(self.add_button)

        page = Adw.PreferencesPage()
        link = Adw.PreferencesGroup()
        self.url = Adw.EntryRow(title="Link", text=url)
        self.url.connect("changed", self._validate)
        self.url.connect("entry-activated", self._on_add)
        self.name = Adw.EntryRow(title="File name (optional)", text=name or "")
        link.add(self.url)
        link.add(self.name)

        options = Adw.PreferencesGroup()
        self.folder_row = Adw.ActionRow(title="Folder", subtitle="…")
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._choose_folder)
        self.folder_row.add_suffix(choose)
        self.connections = Adw.SpinRow.new_with_range(1, 32, 1)
        self.connections.set_title("Connections")
        self.connections.set_subtitle("Parallel parts used for this file")
        self.limit = Adw.EntryRow(title="Speed limit (e.g. 2M, empty = unlimited)")
        self.limit.connect("changed", self._validate)
        self.paused = Adw.SwitchRow(title="Add paused")
        for row in (self.folder_row, self.connections, self.limit, self.paused):
            options.add(row)

        headers = Adw.PreferencesGroup()
        expander = Adw.ExpanderRow(title="Request headers",
                                   subtitle="For links that need a login or a referring page")
        headers = headers or {}
        self.referer = Adw.EntryRow(title="Referer", text=headers.get("Referer", ""))
        self.cookie = Adw.PasswordEntryRow(title="Cookie", text=headers.get("Cookie", ""))
        self.user_agent = Adw.EntryRow(title="User-Agent", text=headers.get("User-Agent", ""))
        for row in (self.referer, self.cookie, self.user_agent):
            expander.add_row(row)
        expander.set_expanded(bool(headers))
        headers.add(expander)

        for group in (link, options, headers):
            page.add(group)
        self.toasts = Adw.ToastOverlay(child=page)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        self.set_child(view)

        client.get_settings(self._on_settings)
        if not url:
            read_clipboard_url(self, self._prefill)
        self._validate()

    def _prefill(self, url: str) -> None:
        if not self.url.get_text():
            self.url.set_text(url)

    def _on_settings(self, settings: dict[str, Any] | None, error) -> None:
        if settings:
            self.folder = self.folder or settings["download_dir"]
            self.folder_row.set_subtitle(short_path(self.folder))
            self.connections.set_value(settings["default_connections"])

    def _choose_folder(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Download Folder")
        if self.folder:
            dialog.set_initial_folder(Gio.File.new_for_path(self.folder))

        def done(d, result):
            try:
                folder = d.select_folder_finish(result)
            except GLib.Error:
                return  # cancelled
            self.folder = folder.get_path()
            self.folder_row.set_subtitle(short_path(self.folder))

        dialog.select_folder(self.get_root(), None, done)

    def _validate(self, *_) -> bool:
        url_ok = is_url(self.url.get_text())
        limit_ok = parse_limit(self.limit.get_text()) is not None
        for row, ok in ((self.url, url_ok or not self.url.get_text()), (self.limit, limit_ok)):
            (row.remove_css_class if ok else row.add_css_class)("error")
        self.add_button.set_sensitive(url_ok and limit_ok)
        return url_ok and limit_ok

    def _on_add(self, *_) -> None:
        if not self._validate():
            return
        headers = {k: row.get_text().strip() for k, row in
                   (("Referer", self.referer), ("Cookie", self.cookie), ("User-Agent", self.user_agent))
                   if row.get_text().strip()}
        options = {
            "name": self.name.get_text().strip() or None,
            "dir": self.folder,
            "connections": int(self.connections.get_value()),
            "speed_limit": parse_limit(self.limit.get_text()) or None,
            "headers": headers or None,
            "start": not self.paused.get_active(),
        }
        self.add_button.set_sensitive(False)
        self.client.add(self.url.get_text().strip(), options, self._on_result)

    def _on_result(self, download_id, error) -> None:
        if error:
            self.add_button.set_sensitive(True)
            self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(error.message)))
            return
        if self.on_added:
            self.on_added(download_id)
        self.close()


def ask_text(parent: Gtk.Widget, heading: str, body: str, action: str, text: str,
             placeholder: str, on_submit: Callable[[str], None]) -> Adw.AlertDialog:
    """One-entry prompt; `on_submit(text)` is called when the user confirms."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    entry = Gtk.Entry(text=text, placeholder_text=placeholder, activates_default=True)
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("ok", action)
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("ok")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda d, response: response == "ok" and on_submit(entry.get_text().strip()))
    dialog.present(parent)
    return dialog


def confirm(parent: Gtk.Widget, heading: str, body: str, action: str, on_confirm: Callable[[], None]) -> None:
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("ok", action)
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda d, response: response == "ok" and on_confirm())
    dialog.present(parent)


def limit_text(rate: int) -> str:
    return "0" if not rate else human(rate).replace(" ", "").replace("iB", "")


def short_path(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path
