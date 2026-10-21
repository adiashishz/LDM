"""Add-download dialog and small prompts."""

from __future__ import annotations

from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from gi.repository import Adw, GLib, Gtk

from ..format import human, parse_rate


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
