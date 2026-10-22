"""Selection mode of the download list."""

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from ldm.gui.model import DownloadList  # noqa: E402
from ldm.gui.row import DownloadRow  # noqa: E402
from ldm.gui.selection import Selection  # noqa: E402


def row(i, state="queued"):
    return {"id": i, "url": f"http://h/files/f{i}.bin", "dir": "/tmp", "state": state, "position": i, "done": 0}


def test_selecting_counts_and_keeps_list_order():
    downloads = DownloadList()
    downloads.load([row(1), row(2), row(3)])
    selection = Selection()
    selection.set_view(downloads.view("all"))
    selection.active = True
    selection.set_selected(3, True)
    selection.set_selected(1, True)
    assert selection.count == 2 and [i.id for i in selection.items()] == [1, 3]
    assert not selection.all_selected()
    selection.select_all()
    assert selection.count == 3 and selection.all_selected()


def test_leaving_selection_mode_or_the_view_unticks():
    downloads = DownloadList()
    downloads.load([row(1, "running"), row(2, "done")])
    selection = Selection()
    selection.set_view(downloads.view("all"))
    selection.active = True
    selection.select_all()
    selection.set_view(downloads.view("finished"))  # another filter: only what is shown stays ticked
    assert [i.id for i in selection.items()] == [2]
    downloads.remove(2)
    assert selection.count == 0
    selection.select_all()
    selection.active = False
    assert selection.ids == set()


class FakeWindow:
    def __init__(self):
        self.selection = Selection()
        self.started = []

    def start_selection(self, item):
        self.started.append(item.id)


def test_row_shows_a_tick_box_only_while_selecting():
    downloads = DownloadList()
    downloads.load([row(1)])
    window = FakeWindow()
    window.selection.set_view(downloads.view("all"))
    widget = DownloadRow(downloads.get(1), window)
    assert not widget.check.get_visible() and widget.primary.get_visible()
    window.selection.active = True
    assert widget.check.get_visible() and not widget.primary.get_visible()
    widget.check.set_active(True)
    assert window.selection.is_selected(1)
