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
        self.probes, self.media_probes, self.torrent_probes, self.added = [], [], [], []

    def probe_torrent(self, source, headers, callback):
        self.torrent_probes.append((source, headers, callback))

    def probe_media(self, url, headers, callback):
        self.media_probes.append((url, headers, callback))

    settings_override: dict = {}

    def get_settings(self, callback):
        callback({**SETTINGS, **self.settings_override}, None)

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
    dialog.unmetered_only.set_active(True)
    dialog._submit(start=False)
    url, options = client.added[0]
    assert url == "https://site/a.zip"
    assert options == {"name": "a.zip", "dir": "/tmp/ldm-test-downloads", "connections": 8,
                       "speed_limit": 2 * 1024 * 1024, "headers": None, "start": False,
                       "scheduled": False, "unmetered_only": True}


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


TORRENT = {"name": "Debian ISO", "size": 700_000_000, "num_files": 4, "is_magnet": True, "metadata": b"d4:infoe",
           "files": [{"index": 0, "path": "Debian ISO/debian.iso", "size": 690_000_000},
                     {"index": 2, "path": "Debian ISO/SHA256SUMS", "size": 10_000_000}]}


def test_magnet_shows_file_list_and_sends_priorities():
    client = FakeClient()
    dialog = AddDialog(client, "magnet:?xt=urn:btih:abc&dn=Debian")
    assert not client.probes and client.torrent_probes  # torrents skip the HTTP check
    assert dialog.without_list.get_visible()             # magnet: offer to skip the file list
    client.torrent_probes[-1][2](TORRENT, None)
    assert dialog.torrent_group.get_visible() and not dialog.parts_row.get_visible()
    assert dialog.files_row.get_title() == "Files (2 of 2 selected)"
    dialog.file_checks[1][1].set_active(False)              # skip SHA256SUMS
    assert dialog.files_row.get_title() == "Files (1 of 2 selected)"
    dialog.sequential.set_active(True)
    dialog._submit(start=True)
    _, options = client.added[0]
    assert options["kind"] == "torrent" and options["sequential"] is True
    assert options["priorities"] == [1, 0, 0, 0] and options["metadata"] == b"d4:infoe"
    assert "connections" not in options and options["name"] is None


def test_magnet_without_file_list():
    client = FakeClient()
    dialog = AddDialog(client, "magnet:?xt=urn:btih:abc")
    dialog._on_without_list(None)
    client.torrent_probes[-1][2](TORRENT, None)   # arrives late: ignored
    assert not dialog.file_checks
    dialog._submit(start=True)
    _, options = client.added[0]
    assert options["kind"] == "torrent" and "priorities" not in options


def test_nothing_selected_disables_start():
    client = FakeClient()
    dialog = AddDialog(client, "https://site/files.torrent")
    client.torrent_probes[-1][2](TORRENT, None)
    dialog.select_all.set_active(False)
    assert not dialog.start_button.get_sensitive()


def test_schedule_this_download_with_own_times():
    client = FakeClient()
    dialog = AddDialog(client, "https://site/big.iso", name="big.iso")
    editor = dialog.schedule
    assert not editor.get_enable_expansion()
    assert editor.get_subtitle() == "Default times: 02:00–06:00, every day"
    editor.set_enable_expansion(True)
    editor.start.set_time("22:30")
    editor.days.set_days("sat,sun")
    assert editor.get_subtitle() == "Own times: 22:30–06:00 (overnight), Sat Sun"
    assert editor.reset_row.get_visible()
    dialog._submit(start=True)
    _, options = client.added[0]
    assert options["scheduled"] is True
    assert (options["schedule_start"], options["schedule_stop"], options["schedule_days"]) == \
        ("22:30", "06:00", "sat,sun")


def test_schedule_follows_new_link_setting_and_defaults():
    client = FakeClient()
    client.settings_override = {"schedule_new_links": True, "schedule_start": "01:00", "schedule_stop": "05:00"}
    dialog = AddDialog(client, "https://site/a.zip", name="a.zip")
    assert dialog.schedule.get_enable_expansion()
    assert dialog.schedule.window_values() == ("01:00", "05:00", "")
    dialog._submit(start=True)
    _, options = client.added[0]
    assert options["scheduled"] is True and options["schedule_start"] == "01:00"  # = defaults: daemon follows them
