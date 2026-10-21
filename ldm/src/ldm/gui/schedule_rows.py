"""Schedule widgets: time and weekday rows, and the per-download schedule editor.

Preferences hold the default times; every download on the schedule follows them unless it has its
own (set in the add dialog or from the download's "Schedule…" menu item).
"""

from __future__ import annotations

from typing import Any, Callable

from gi.repository import Adw, GObject, Gtk

from ..schedule import DAYS, parse_days, parse_time

WINDOW_KEYS = ("schedule_start", "schedule_stop", "schedule_days")


def describe_window(start: str, stop: str, days: str) -> str:
    chosen = parse_days(days) if days else set()
    names = "every day" if not chosen or len(chosen) == 7 else " ".join(DAYS[i].capitalize() for i in sorted(chosen))
    overnight = " (overnight)" if parse_time(stop) < parse_time(start) else ""
    return f"{start}–{stop}{overnight}, {names}"


class TimeRow(Adw.ActionRow):
    """HH:MM with two spin buttons."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, title: str, subtitle: str = ""):
        super().__init__(title=title, subtitle=subtitle)
        box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self.hours = self._spin(23, 1)
        self.minutes = self._spin(59, 5)
        box.append(self.hours)
        box.append(Gtk.Label(label=":"))
        box.append(self.minutes)
        self.add_suffix(box)

    def _spin(self, top: int, page: int) -> Gtk.SpinButton:
        spin = Gtk.SpinButton.new_with_range(0, top, 1)
        spin.set_increments(1, page)
        spin.set_wrap(True)
        spin.set_numeric(True)
        spin.set_width_chars(2)
        spin.connect("output", lambda s: s.set_text(f"{int(s.get_value()):02d}") or True)
        spin.connect("value-changed", lambda *_: self.emit("changed"))
        return spin

    def get_time(self) -> str:
        return f"{int(self.hours.get_value()):02d}:{int(self.minutes.get_value()):02d}"

    def set_time(self, text: str) -> None:
        minutes = parse_time(text)
        self.hours.set_value(minutes // 60)
        self.minutes.set_value(minutes % 60)


class DaysRow(Adw.PreferencesRow):
    """Weekday toggles under the title (fits narrow dialogs); none selected means every day."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        super().__init__(title="Days", activatable=False)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                          margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        title = Gtk.Label(label="Days", xalign=0)
        hint = Gtk.Label(label="None selected means every day", xalign=0)
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        box = Gtk.Box(homogeneous=True)
        box.add_css_class("linked")
        self.buttons = []
        for day in DAYS:
            button = Gtk.ToggleButton(label=day[:2].capitalize(), tooltip_text=day.capitalize())
            button.connect("toggled", lambda *_: self.emit("changed"))
            box.append(button)
            self.buttons.append(button)
        for widget in (title, box, hint):
            content.append(widget)
        self.set_child(content)

    def get_days(self) -> str:
        return ",".join(day for day, button in zip(DAYS, self.buttons) if button.get_active())

    def set_days(self, text: str) -> None:
        chosen = parse_days(text) if text else set()
        for index, button in enumerate(self.buttons):
            button.set_active(index in chosen)


class ScheduleEditor(Adw.ExpanderRow):
    """Switch "Download on a schedule" + this download's start/stop/days (prefilled with the defaults)."""

    def __init__(self):
        super().__init__(title="Download on a schedule", show_enable_switch=True, enable_expansion=False)
        self.defaults = ("02:00", "06:00", "")
        self.start = TimeRow("Start at")
        self.stop = TimeRow("Stop at", "Earlier than the start time runs overnight")
        self.days = DaysRow()
        reset = Gtk.Button(label="Use default times", valign=Gtk.Align.CENTER)
        reset.add_css_class("flat")
        reset.connect("clicked", lambda *_: self.load(*self.defaults))
        self.reset_row = Adw.ActionRow(title="Default times", subtitle="")
        self.reset_row.add_suffix(reset)
        for row in (self.start, self.stop, self.days, self.reset_row):
            self.add_row(row)
        for row in (self.start, self.stop, self.days):
            row.connect("changed", self._update)
        self.load(*self.defaults)

    def set_defaults(self, settings: dict[str, Any]) -> None:
        """Default times from the settings; also shown in the editor until the user changes them."""
        follows = self.window_values() == self.defaults
        self.defaults = tuple(settings.get(k, d) for k, d in zip(WINDOW_KEYS, self.defaults))
        if follows:
            self.load(*self.defaults)
        self._update()

    def load(self, start: str, stop: str, days: str) -> None:
        self.start.set_time(start)
        self.stop.set_time(stop)
        self.days.set_days(days)
        self._update()

    def window_values(self) -> tuple[str, str, str]:
        return self.start.get_time(), self.stop.get_time(), self.days.get_days()

    def window(self) -> dict[str, str]:
        """For the daemon; equal to the defaults means "follow the defaults" (it stores nothing then)."""
        return dict(zip(WINDOW_KEYS, self.window_values()))

    def _update(self, *_) -> None:
        own = self.window_values() != self.defaults
        self.set_subtitle(("Own times: " if own else "Default times: ") + describe_window(*self.window_values()))
        self.reset_row.set_subtitle(describe_window(*self.defaults))
        self.reset_row.set_visible(own)


def ask_schedule(parent: Gtk.Widget, title: str, values: dict[str, Any], defaults: dict[str, Any],
                 on_save: Callable[[bool, dict[str, str]], None]) -> Adw.AlertDialog:
    """Edit one download's schedule; `values` is its row from the daemon (scheduled + effective times)."""
    dialog = Adw.AlertDialog(heading="Schedule", body=title)
    editor = ScheduleEditor()
    editor.set_defaults(defaults)
    editor.load(*(values[k] for k in WINDOW_KEYS))
    editor.set_enable_expansion(bool(values.get("scheduled")))
    editor.set_expanded(True)
    rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    rows.add_css_class("boxed-list")
    rows.append(editor)
    dialog.set_extra_child(rows)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("save", "Save")
    dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("save")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda d, response: response == "save"
                   and on_save(editor.get_enable_expansion(), editor.window()))
    dialog.present(parent)
    return dialog
