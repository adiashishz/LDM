"""Fallbacks for libadwaita widgets newer than 1.5, and the toolkit version check."""

from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from ldm import gui  # noqa: E402
from ldm.gui import compat  # noqa: E402

ITEMS = [("all", "All"), ("done", "Done"), ("bad", "Problems")]


def test_linked_toggles_act_like_a_toggle_group():
    group = compat.LinkedToggles(ITEMS)
    seen = []
    group.connect("notify::active-name", lambda g, _p: seen.append(g.get_active_name()))
    group.set_active_name("all")
    assert group.get_active_name() == "all"
    buttons = []
    child = group.get_first_child()
    while child:
        buttons.append(child)
        child = child.get_next_sibling()
    assert [b.get_label() for b in buttons] == ["All", "Done", "Problems"]
    buttons[2].set_active(True)  # a click
    assert group.get_active_name() == "bad"
    assert [b.get_active() for b in buttons] == [False, False, True]  # one at a time
    group.set_active_name("done")
    assert seen == ["all", "bad", "done"]


def test_helpers_pick_the_fallback_on_old_libadwaita(monkeypatch):
    # PyGObject resolves Adw attributes lazily from the typelib, so delattr on Adw itself doesn't stick:
    # give compat an Adw without the 1.6/1.7 widgets instead.
    monkeypatch.setattr(compat, "Adw", SimpleNamespace())
    assert isinstance(compat.toggle_group(ITEMS), compat.LinkedToggles)
    assert isinstance(compat.spinner(visible=False), Gtk.Spinner)


def test_helpers_use_libadwaita_when_it_has_the_widgets():
    if not hasattr(Adw, "ToggleGroup"):
        pytest.skip("libadwaita older than 1.7")
    group = compat.toggle_group(ITEMS)
    assert isinstance(group, Adw.ToggleGroup) and isinstance(compat.spinner(), Adw.Spinner)
    group.set_active_name("done")
    assert group.get_active_name() == "done"


def test_version_check(monkeypatch):
    assert gui.too_old() is None  # whatever runs the tests is supported
    monkeypatch.setattr(Adw, "get_minor_version", lambda: 2)
    monkeypatch.setattr(Gtk, "get_minor_version", lambda: 8)
    message = gui.too_old()
    assert "GTK 4.12 or newer (this system has 4.8)" in message
    assert "libadwaita 1.5 or newer (this system has 1.2)" in message
