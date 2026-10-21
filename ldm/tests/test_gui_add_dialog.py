"""AddDialog without a daemon: a fake client answers settings/probe/add synchronously."""

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

if not Gtk.init_check() or Gdk.Display.get_default() is None:
    pytest.skip("no display available", allow_module_level=True)
Adw.init()

from ldm.gui.add_dialog import AddDialog  # noqa: E402

SETTINGS = {"download_dir": "/tmp/ldm-test-downloads", "default_connections": 8}


class FakeClient:
    def __init__(self):
        self.probes, self.added = [], []

    def get_settings(self, callback):
        callback(dict(SETTINGS), None)

    def probe(self, url, headers, callback):
        self.probes.append((url, headers, callback))

    def add(self, url, options, callback):
        self.added.append((url, options))
        callback(1, None)


def test_browser_request_prefills_headers_and_name():
    client = FakeClient()
    headers = {"Referer": "https://site/page", "Cookie": "a=1", "User-Agent": "UA"}
    dialog = AddDialog(client, "https://site/file.iso", name="file.iso", headers=headers)
    assert (dialog.name.get_text(), dialog.headers()) == ("file.iso", headers)
    assert client.probes[0][:2] == ("https://site/file.iso", headers)  # link checked with the same headers
    assert dialog.start_button.get_sensitive()


def test_probe_result_fills_details_like_1dm():
    client = FakeClient()
    dialog = AddDialog(client, "https://site/download?id=7")
    callback = client.probes[-1][2]
    callback({"size": 78169341, "resumable": True, "filename": "Holiday trip 2026.mkv",
              "content_type": "video/x-matroska"}, None)
    assert dialog.resume.get_label() == "Resume: Yes"
    assert dialog.size.get_subtitle() == "74.5 MiB (78,169,341 bytes) · video/x-matroska"
    assert dialog.name.get_text() == "Holiday trip 2026.mkv"
    assert dialog.parts.get_sensitive() and dialog.parts.get_value() == 8


def test_server_without_resume_forces_one_part():
    client = FakeClient()
    dialog = AddDialog(client, "https://site/stream")
    client.probes[-1][2]({"size": None, "resumable": False, "filename": "stream"}, None)
    assert dialog.resume.get_label() == "Resume: No"
    assert dialog.size.get_subtitle() == "Unknown size"
    assert not dialog.parts.get_sensitive() and dialog.parts.get_value() == 1


def test_add_to_list_and_start():
    client = FakeClient()
    dialog = AddDialog(client, "https://site/a.zip", name="a.zip")
    dialog.limit.set_text("2M")
    dialog._submit(start=False)
    url, options = client.added[0]
    assert url == "https://site/a.zip"
    assert options == {"name": "a.zip", "dir": "/tmp/ldm-test-downloads", "connections": 8,
                       "speed_limit": 2 * 1024 * 1024, "headers": None, "start": False}


def test_plain_add_dialog_without_arguments():
    """Ctrl+N / + button: crashed in M4 before the fix (headers parameter shadowed)."""
    dialog = AddDialog(FakeClient())
    assert not dialog.start_button.get_sensitive()
