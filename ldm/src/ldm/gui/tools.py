"""Dialogs for list tools: batch download, import links, checksum, diagnostics log, site login."""

from __future__ import annotations

from typing import Any, Callable

from gi.repository import Adw, GLib, Gtk

from ..batch import expand
from ..files import algorithm_for
from ..format import human
from .client import DaemonClient
from .schedule_rows import ScheduleEditor


def _group(*rows: Gtk.Widget, title: str = "") -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup(title=title)
    for row in rows:
        group.add(row)
    return group


def _dialog(title: str, *groups: Gtk.Widget, action: str = "", on_action: Callable[[], None] | None = None,
            width: int = 520) -> tuple[Adw.Dialog, Gtk.Button | None, Adw.ToastOverlay]:
    """Header bar with Cancel/action, a scrolling page of groups, and toasts."""
    dialog = Adw.Dialog(title=title, content_width=width)
    header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=not action)
    button = None
    if action:
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dialog.close())
        header.pack_start(cancel)
        button = Gtk.Button(label=action)
        button.add_css_class("suggested-action")
        button.connect("clicked", lambda *_: on_action())
        header.pack_end(button)
    page = Adw.PreferencesPage()
    for group in groups:
        page.add(group)
    toasts = Adw.ToastOverlay(child=page)
    view = Adw.ToolbarView(content=toasts)
    view.add_top_bar(header)
    dialog.set_child(view)
    return dialog, button, toasts


class BatchDialog:
    """1DM batch download: one * in the link, replaced by a range of numbers or letters."""

    def __init__(self, client: DaemonClient, parent: Gtk.Widget, on_done: Callable[[str], None]):
        self.client, self.on_done = client, on_done
        self.url = Adw.EntryRow(title="Link with * where the number goes, e.g. https://site/img*.jpg")
        self.kind = Adw.ComboRow(title="Replace * with", model=Gtk.StringList.new(["Numbers", "Letters"]))
        self.first = Adw.SpinRow.new_with_range(0, 999_999, 1)
        self.first.set_title("From")
        self.first.set_value(1)
        self.last = Adw.SpinRow.new_with_range(0, 999_999, 1)
        self.last.set_title("To")
        self.last.set_value(10)
        self.pad = Adw.SpinRow.new_with_range(0, 10, 1)
        self.pad.set_title("Zero padding")
        self.pad.set_subtitle("Digits per number, e.g. 3 gives 001")
        self.first_letter = Adw.EntryRow(title="From letter", text="a", visible=False)
        self.last_letter = Adw.EntryRow(title="To letter", text="z", visible=False)
        self.preview = Adw.ActionRow(title="Preview", subtitle="Type a link with one *", subtitle_selectable=True)
        self.schedule = ScheduleEditor()
        self.start = Adw.SwitchRow(title="Start now", active=True)
        for row in (self.url, self.first_letter, self.last_letter):
            row.connect("changed", self._update)
        for row in (self.first, self.last, self.pad):
            row.connect("notify::value", self._update)
        self.kind.connect("notify::selected", self._update)
        self.dialog, self.add_button, self.toasts = _dialog(
            "Batch Download",
            _group(self.url, self.kind, self.first, self.last, self.pad, self.first_letter, self.last_letter),
            _group(self.preview, title="Links"),
            _group(self.schedule, self.start, title="Options"),
            action="Add", on_action=self._add)
        self.links: list[str] = []
        client.get_settings(lambda settings, error: settings and self.schedule.set_defaults(settings))
        self._update()
        self.dialog.present(parent)

    def _update(self, *_) -> None:
        letters = self.kind.get_selected() == 1
        for row in (self.first, self.last, self.pad):
            row.set_visible(not letters)
        for row in (self.first_letter, self.last_letter):
            row.set_visible(letters)
        try:
            if letters:
                self.links = expand(self.url.get_text(), letters=(self.first_letter.get_text().strip(),
                                                                   self.last_letter.get_text().strip()))
            else:
                self.links = expand(self.url.get_text(), numbers=(int(self.first.get_value()),
                                                                   int(self.last.get_value()), int(self.pad.get_value())))
            first, last = self.links[0], self.links[-1]
            self.preview.set_title(f"{len(self.links)} links")
            self.preview.set_subtitle(first if len(self.links) == 1 else f"{first}\n…\n{last}")
        except ValueError as e:
            self.links = []
            self.preview.set_title("Preview")
            self.preview.set_subtitle(str(e)[0].upper() + str(e)[1:] if self.url.get_text() else "Type a link with one *")
        self.add_button.set_sensitive(bool(self.links))

    def options(self) -> dict[str, Any]:
        options: dict[str, Any] = {"start": self.start.get_active(),
                                   "scheduled": self.schedule.get_enable_expansion()}
        if self.schedule.get_enable_expansion():
            options.update(self.schedule.window())
        return options

    def _add(self) -> None:
        links, options = list(self.links), self.options()
        for link in links:
            self.client.add(link, options)
        self.dialog.close()
        self.on_done(f"{len(links)} downloads added")


class ImportLinksDialog:
    """Links from a text file, with a checkbox each (1DM: import download links)."""

    def __init__(self, client: DaemonClient, parent: Gtk.Widget, links: list[str], known: list[str],
                 on_done: Callable[[str], None]):
        self.client, self.on_done = client, on_done
        group = Adw.PreferencesGroup(title=f"{len(links)} links",
                                     description="Links already in your list are unchecked.")
        self.checks: list[tuple[str, Gtk.CheckButton]] = []
        for link in links[:2000]:
            check = Gtk.CheckButton(active=link not in known, valign=Gtk.Align.CENTER)
            row = Adw.ActionRow(title=link, title_lines=1, activatable_widget=check)
            row.add_prefix(check)
            group.add(row)
            self.checks.append((link, check))
        self.start = Adw.SwitchRow(title="Start now", active=False)
        self.dialog, _, _ = _dialog("Import Links", _group(self.start), group, action="Add", on_action=self._add)
        self.dialog.present(parent)

    def _add(self) -> None:
        chosen = [link for link, check in self.checks if check.get_active()]
        for link in chosen:
            self.client.add(link, {"start": self.start.get_active(),
                                   **({"kind": "torrent"} if link.startswith("magnet:") else {})})
        self.dialog.close()
        self.on_done(f"{len(chosen)} downloads added")


class ChecksumDialog:
    """MD5 and SHA-256 of a finished file, with "Compare with clipboard" (1DM)."""

    def __init__(self, client: DaemonClient, parent: Gtk.Widget, download_id: int, title: str):
        self.client = client
        self.download_id = download_id
        self.values: dict[str, str] = {}
        self.rows: dict[str, Adw.ActionRow] = {}
        group = Adw.PreferencesGroup(title=title)
        for algorithm, label in (("md5", "MD5"), ("sha256", "SHA-256")):
            row = Adw.ActionRow(title=label, subtitle="Calculating…", subtitle_selectable=True)
            row.add_css_class("property")
            copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy", valign=Gtk.Align.CENTER)
            copy.add_css_class("flat")
            copy.connect("clicked", lambda _b, a=algorithm: self._copy(a))
            row.add_suffix(copy)
            group.add(row)
            self.rows[algorithm] = row
            client.checksum(download_id, algorithm, lambda value, error, a=algorithm: self._got(a, value, error))
        self.result = Adw.ActionRow(title="Compare with a checksum you copied")
        compare = Gtk.Button(label="Compare with Clipboard", valign=Gtk.Align.CENTER)
        compare.connect("clicked", self._compare)
        self.result.add_suffix(compare)
        self.dialog, _, self.toasts = _dialog("Checksum", group, _group(self.result), width=620)
        self.dialog.present(parent)

    def _got(self, algorithm: str, value: str | None, error) -> None:
        self.values[algorithm] = value or ""
        self.rows[algorithm].set_subtitle(value if value else f"Failed: {error.message}")

    def _copy(self, algorithm: str) -> None:
        if self.values.get(algorithm):
            self.dialog.get_clipboard().set(self.values[algorithm])
            self.toasts.add_toast(Adw.Toast(title="Copied"))

    def _compare(self, _button) -> None:
        clipboard = self.dialog.get_clipboard()
        clipboard.read_text_async(None, lambda cb, result: self.show_comparison(cb.read_text_finish(result)))

    def show_comparison(self, text: str | None) -> None:
        expected = (text or "").strip().lower()
        algorithm = algorithm_for(expected)
        if algorithm is None:
            self.result.set_subtitle("The clipboard doesn't hold an MD5, SHA-1 or SHA-256 checksum")
            return
        if algorithm not in self.values:  # SHA-1 isn't shown: calculate it now, then compare
            self.result.set_subtitle(f"Calculating {algorithm.upper()}…")
            self.client.checksum(self.download_id, algorithm,
                                 lambda value, error: self._got_extra(algorithm, value, error, text))
            return
        same = self.values[algorithm] == expected
        self.result.set_subtitle(f"✓ Matches ({algorithm.upper()})" if same else f"✗ Does not match ({algorithm.upper()})")


    def _got_extra(self, algorithm: str, value: str | None, error, text: str) -> None:
        if error:
            self.result.set_subtitle(f"Failed: {error.message}")
            return
        self.values[algorithm] = value
        self.show_comparison(text)


class LogDialog:
    """Download diagnostics: requests, responses, retries (1DM: download diagnostics)."""

    def __init__(self, client: DaemonClient, parent: Gtk.Widget, download_id: int, title: str):
        self.view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 top_margin=12, bottom_margin=12, left_margin=12, right_margin=12)
        self.dialog = Adw.Dialog(title=f"Diagnostics — {title}", content_width=760, content_height=520)
        header = Adw.HeaderBar()
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy All")
        copy.connect("clicked", lambda *_: self.dialog.get_clipboard().set(self.text()))
        header.pack_start(copy)
        view = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=self.view))
        view.add_top_bar(header)
        self.dialog.set_child(view)
        client.get_log(download_id, self._show)
        self.dialog.present(parent)

    def text(self) -> str:
        buffer = self.view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def _show(self, lines: list[str] | None, error) -> None:
        if error:
            text = f"Could not read the log: {error.message}"
        else:
            text = "\n".join(lines) if lines else "Nothing recorded yet — the log covers downloads started since " \
                                                  "the download service last started."
        self.view.get_buffer().set_text(text)


def ask_login(parent: Gtk.Widget, host: str, username: str, on_save: Callable[[str, str, str, bool], None],
              subdomains: bool = True) -> Adw.AlertDialog:
    """Site login (HTTP Basic/Digest) for a host; the password goes to the keyring."""
    dialog = Adw.AlertDialog(heading="Site Login", body="Used when the site asks for a user name and password.")
    host_row = Adw.EntryRow(title="Site (e.g. example.com)", text=host)
    user_row = Adw.EntryRow(title="User name", text=username)
    password_row = Adw.PasswordEntryRow(title="Password")
    subdomain_row = Adw.SwitchRow(title="Also for subdomains", active=subdomains)
    rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    rows.add_css_class("boxed-list")
    for row in (host_row, user_row, password_row, subdomain_row):
        rows.append(row)
    dialog.set_extra_child(rows)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("save", "Save")
    dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda d, response: response == "save" and on_save(
        host_row.get_text().strip(), user_row.get_text().strip(), password_row.get_text(),
        subdomain_row.get_active()))
    dialog.present(parent)
    return dialog


class PartsDialog:
    """1DM's multi-part view: where each connection is in the file, live."""

    def __init__(self, client: DaemonClient, parent: Gtk.Widget, download_id: int, title: str):
        self.client, self.download_id = client, download_id
        self.parts: list[dict[str, Any]] = []
        self.bar = Gtk.DrawingArea(content_height=28, hexpand=True)
        self.bar.set_draw_func(self._draw)
        self.summary = Gtk.Label(xalign=0, css_classes=["dim-label"])
        self.rows = Adw.PreferencesGroup(title="Parts")
        self.row_widgets: list[Adw.ActionRow] = []
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=6)
        box.append(self.bar)
        box.append(self.summary)
        head = Adw.PreferencesGroup(title=title)
        head.add(box)
        self.dialog, _, _ = _dialog("Parts", head, self.rows, width=620)
        self.dialog.connect("closed", lambda *_: self._stop())
        self._timer = GLib.timeout_add(500, self._refresh)
        self._refresh()
        self.dialog.present(parent)

    def _stop(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _refresh(self) -> bool:
        self.client.get_parts(self.download_id, self._show)
        return GLib.SOURCE_CONTINUE

    def _show(self, parts: list[dict[str, Any]] | None, error) -> None:
        if error:
            self.summary.set_label(error.message)
            return
        self.parts = parts or []
        if not self.parts:
            self.summary.set_label("No parts: the download hasn't started, or it is a video or torrent")
        else:
            total = self.parts[-1]["last"] + 1 if self.parts[-1]["last"] < 1 << 61 else 0
            done = sum(p["done"] for p in self.parts)
            active = sum(1 for p in self.parts if p["active"])
            self.summary.set_label(f"{len(self.parts)} parts · {active} downloading · "
                                   + (f"{human(done)} of {human(total)}" if total else human(done)))
        while len(self.row_widgets) < len(self.parts):
            row = Adw.ActionRow()
            row.add_css_class("property")
            self.rows.add(row)
            self.row_widgets.append(row)
        for index, row in enumerate(self.row_widgets):
            row.set_visible(index < len(self.parts))
            if index < len(self.parts):
                p = self.parts[index]
                size = p["last"] - p["first"] + 1
                state = "downloading" if p["active"] else ("finished" if p["done"] >= size else "waiting")
                row.set_title(f"Part {index + 1} · {state}")
                row.set_subtitle(f"{human(p['done'])} of {human(size)} · from byte {p['first']:,}")
        self.bar.queue_draw()

    def _draw(self, area, cr, width: int, height: int) -> None:
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        fg = area.get_color()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        if not self.parts or self.parts[-1]["last"] >= 1 << 61:
            return
        total = self.parts[-1]["last"] + 1
        for p in self.parts:
            x = p["first"] / total * width
            cr.set_source_rgba(accent.red, accent.green, accent.blue, 1.0 if p["active"] else 0.7)
            cr.rectangle(x, 0, p["done"] / total * width, height)
            cr.fill()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.35)  # part boundary
            cr.rectangle(x, 0, 1, height)
            cr.fill()
