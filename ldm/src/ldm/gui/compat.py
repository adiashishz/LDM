"""Stand-ins for libadwaita widgets newer than the oldest supported release.

LDM supports libadwaita 1.5 (Ubuntu 24.04 LTS, Linux Mint 22). Newer widgets are used when the
running libadwaita has them, and plain GTK widgets that behave the same way otherwise.
"""

from __future__ import annotations

from typing import Iterable

from gi.repository import Adw, GObject, Gtk


def spinner(**props) -> Gtk.Widget:
    """Adw.Spinner (libadwaita 1.6) or a spinning Gtk.Spinner."""
    if hasattr(Adw, "Spinner"):
        return Adw.Spinner(**props)
    return Gtk.Spinner(spinning=True, **props)


class LinkedToggles(Gtk.Box):
    """The part of Adw.ToggleGroup's API LDM uses, built from linked Gtk.ToggleButtons."""

    active_name = GObject.Property(type=str, default="")

    def __init__(self, items: Iterable[tuple[str, str]]):
        super().__init__(css_classes=["linked"], valign=Gtk.Align.CENTER)
        self._buttons: dict[str, Gtk.ToggleButton] = {}
        for name, label in items:
            button = Gtk.ToggleButton(label=label)
            if self._buttons:
                button.set_group(next(iter(self._buttons.values())))
            button.connect("toggled", self._on_toggled, name)
            self.append(button)
            self._buttons[name] = button

    def _on_toggled(self, button: Gtk.ToggleButton, name: str) -> None:
        if button.get_active() and self.active_name != name:
            self.active_name = name

    def get_active_name(self) -> str:
        return self.active_name

    def set_active_name(self, name: str) -> None:
        self._buttons[name].set_active(True)


def toggle_group(items: Iterable[tuple[str, str]]) -> Gtk.Widget:
    """Adw.ToggleGroup (libadwaita 1.7) or LinkedToggles, with get/set_active_name and notify::active-name."""
    if not hasattr(Adw, "ToggleGroup"):
        return LinkedToggles(items)
    group = Adw.ToggleGroup()
    for name, label in items:
        group.add(Adw.Toggle(name=name, label=label))
    return group
