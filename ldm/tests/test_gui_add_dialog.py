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

SETTINGS = {"download_dir": "/tmp/ldm-test-downloads", "default_connections": 8, "media_quality": 720,
            "subtitles": "hi"}


class FakeClient:
    def __init__(self):
        self.probes, self.media_probes, self.added = [], [], []

    def probe_media(self, url, headers, callback):
        self.media_probes.append((url, headers, callback))

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


VIDEO = {"title": "Trailer 2", "site": "Youtube", "duration": 154, "is_live": False, "subtitles": ["en", "hi"],
         "choices": [{"id": "best", "label": "Best quality", "selector": "bv*+ba/b", "size": 90_000_000},
                     {"id": "1080", "label": "1080p", "selector": "bv*[height<=1080]+ba/b[height<=1080]/b",
                      "size": 90_000_000},
                     {"id": "720", "label": "720p", "selector": "bv*[height<=720]+ba/b[height<=720]/b",
                      "size": 40_000_000},
                     {"id": "audio", "label": "Audio only", "selector": "ba/b", "size": 3_000_000}]}


def test_video_page_from_browser_shows_quality_choice():
    client = FakeClient()
    dialog = AddDialog(client, "https://www.youtube.com/watch?v=x", kind="media",
                       headers={"Cookie": "c=1"})
    assert not client.probes and client.media_probes[0][:2] == ("https://www.youtube.com/watch?v=x", {"Cookie": "c=1"})
    client.media_probes[0][2](VIDEO, None)
    assert dialog.media_group.get_visible()
    assert dialog.media_title.get_subtitle() == "Youtube · 2:34"
    assert dialog.quality.get_selected() == 2  # 720p from settings
    assert dialog.quality.get_model().get_string(2) == "720p · ~38.1 MiB"
    assert dialog.name.get_text() == "Trailer 2"
    assert dialog.subtitles.get_visible() and dialog.subtitles.get_active()
    dialog._submit(start=True)
    _, options = client.added[0]
    assert options["kind"] == "media" and options["format_label"] == "720p"
    assert options["format"] == "bv*[height<=720]+ba/b[height<=720]/b"
    assert options["subtitles"] == "hi" and options["name"] is None and options["title"] == "Trailer 2"


def test_html_page_switches_to_video_mode():
    client = FakeClient()
    dialog = AddDialog(client, "https://example.com/watch/123")
    client.probes[-1][2]({"size": 5000, "resumable": False, "filename": "123", "content_type": "text/html"}, None)
    assert client.media_probes, "a web page should be checked for video"
    client.media_probes[-1][2](None, type("E", (), {"message": "Unsupported URL"})())
    assert not dialog.media_group.get_visible()
    assert dialog.size.get_subtitle() == "No downloadable video found: Unsupported URL"
