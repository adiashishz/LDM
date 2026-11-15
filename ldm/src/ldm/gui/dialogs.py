"""Add-download dialog and small prompts."""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, GLib, Gtk

from ..ui.text import is_url, limit_text, parse_limit, short_path  # noqa: F401 (re-exported)


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


def ask_text(parent: Gtk.Widget, heading: str, body: str, action: str, text: str,
             placeholder: str, on_submit: Callable[[str], None], password: bool = False) -> Adw.AlertDialog:
    """One-entry prompt; `on_submit(text)` is called when the user confirms (a password is not trimmed)."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    entry = (Gtk.PasswordEntry(text=text, show_peek_icon=True, activates_default=True) if password
             else Gtk.Entry(text=text, placeholder_text=placeholder, activates_default=True))
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("ok", action)
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("ok")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda d, response: response == "ok" and on_submit(
        entry.get_text() if password else entry.get_text().strip()))
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


