"""List tool dialogs with a fake daemon client."""

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk  # noqa: E402

from ldm.gui.tools import BatchDialog, PartsDialog  # noqa: E402


class FakeClient:
    def __init__(self, parts=None):
        self.parts, self.added = parts or [], []

    def get_parts(self, download_id, callback):
        callback(self.parts, None)

    def get_settings(self, callback):
        callback({"schedule_start": "02:00", "schedule_stop": "06:00", "schedule_days": ""}, None)

    def add(self, url, options, callback=None):
        self.added.append((url, options))


def test_parts_dialog_shows_each_connection():
    parts = [{"first": 0, "last": 499, "done": 500, "active": False},
             {"first": 500, "last": 999, "done": 120, "active": True}]
    window = Gtk.Window()
    dialog = PartsDialog(FakeClient(parts), window, 1, "file.iso")
    assert dialog.summary.get_label() == "2 parts · 1 downloading · 620 B of 1.0 KiB"
    assert [r.get_title() for r in dialog.row_widgets] == ["Part 1 · finished", "Part 2 · downloading"]
    dialog._stop()


def test_batch_dialog_adds_every_link():
    client = FakeClient()
    messages = []
    batch = BatchDialog(client, Gtk.Window(), messages.append)
    batch.url.set_text("https://site/img*.jpg")
    batch.first.set_value(1)
    batch.last.set_value(3)
    batch.pad.set_value(2)
    assert batch.preview.get_title() == "3 links"
    batch._add()
    assert [u for u, _ in client.added] == ["https://site/img01.jpg", "https://site/img02.jpg", "https://site/img03.jpg"]
    assert client.added[0][1] == {"start": True, "scheduled": False} and messages == ["3 downloads added"]
