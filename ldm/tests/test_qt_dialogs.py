"""The Qt dialogs, drawn offscreen against a fake service; and the Add dialog once against the real one."""

import random
import time

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QWidget  # noqa: E402

from qt_fake import FakeClient, real_client  # noqa: E402

from ldm.variants import unwrap  # noqa: E402

MB = 1024 * 1024
PROBE = {"size": 10 * MB, "resumable": True, "content_type": "application/zip", "filename": "server-name.zip"}
MEDIA = {"title": "A talk", "site": "Example", "duration": 75, "subtitles": ["en", "de"],
         "choices": [{"id": "1080", "label": "1080p", "selector": "bv*[height<=1080]+ba", "size": 300 * MB},
                     {"id": "720", "label": "720p", "selector": "bv*[height<=720]+ba", "size": 150 * MB},
                     {"id": "audio", "label": "Audio only", "selector": "ba", "size": 0}]}
TORRENT = {"name": "pack", "num_files": 3, "metadata": b"d4:infod4:name4:packee",
           "files": [{"index": 0, "path": "pack/a.txt", "size": 100}, {"index": 1, "path": "pack/b.txt", "size": 200},
                     {"index": 2, "path": "pack/c.txt", "size": 300}]}


@pytest.fixture
def app(qt_app):
    return qt_app


@pytest.fixture
def parent(app):
    widget = QWidget()
    yield widget
    widget.close()


def spin(app, seconds=0.05):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()


def spin_until(app, condition, timeout=15.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        app.processEvents()
        time.sleep(0.005)


def add_dialog(parent, client, url="", **kwargs):
    from ldm.qt.add_dialog import AddDialog

    dialog = AddDialog(parent, client, url, **kwargs)
    dialog.show()
    return dialog


def test_add_checks_the_link_and_builds_options(app, parent):
    client = FakeClient([], Probe=PROBE)
    dialog = add_dialog(parent, client, "https://example.com/file.zip", headers={"Referer": "https://example.com/",
                                                                                "Accept-Language": "en"})
    assert client.called("Probe")[0][0] == "https://example.com/file.zip"
    assert dialog.name.text() == "server-name.zip" and dialog.badge.text() == "Can resume"
    assert "10.0 MiB" in dialog.size.subtitle.text()
    dialog.limit.edit.setText("nonsense")
    assert not dialog.start_button.isEnabled()
    dialog.limit.edit.setText("2M")
    assert dialog.start_button.isEnabled()
    options = dialog.options(start=True)
    assert options["name"] == "server-name.zip" and options["speed_limit"] == 2 * MB
    assert options["headers"] == {"Referer": "https://example.com/", "Accept-Language": "en"}
    assert options["dir"] == "/tmp" and options["scheduled"] is False
    dialog.start_button.click()
    url, sent = client.called("Add")[0]
    assert url == "https://example.com/file.zip" and unwrap(sent["start"]) is True


def test_add_finds_a_video_on_a_page(app, parent):
    client = FakeClient([], Probe={"content_type": "text/html", "final_url": "https://example.com/watch"},
                        ProbeMedia=MEDIA)
    client.settings["media_quality"] = "720"
    dialog = add_dialog(parent, client, "https://example.com/watch?v=1")
    assert dialog.badge.text() == "Video" and not dialog.media_card.isHidden()
    assert dialog.quality.index() == 1  # the highest choice not above 720p
    assert dialog.name.text() == "A talk"
    options = dialog.options(start=True)
    assert (options["kind"], options["format"], options["format_label"]) == ("media", "bv*[height<=720]+ba", "720p")
    assert options["name"] is None  # yt-dlp names the file after the title


def test_add_torrent_with_chosen_files(app, parent):
    client = FakeClient([], ProbeTorrent=TORRENT)
    dialog = add_dialog(parent, client, "magnet:?xt=urn:btih:abc")
    assert dialog.badge.text() == "Torrent" and len(dialog.file_checks) == 3
    dialog.file_checks[1][1].setChecked(False)
    assert dialog.files.head.title.text() == "Files (2 of 3 selected)"
    options = dialog.options(start=False)
    assert options["kind"] == "torrent" and options["priorities"] == [1, 0, 1] and "connections" not in options
    dialog.select_all.setChecked(False)
    assert not dialog.start_button.isEnabled()  # nothing chosen


def test_add_offers_an_existing_download(app, parent):
    shown = []
    client = FakeClient([], Probe=PROBE, FindDuplicates=[{"id": 5, "state": "paused", "size": 10 * MB}])
    dialog = add_dialog(parent, client, "https://example.com/file.zip", on_show=shown.append)
    assert not dialog.duplicate_bar.isHidden() and dialog.duplicate_bar.button.text() == "Resume"
    dialog.duplicate_bar.button.click()
    assert client.called("Resume") == [[5]] and shown == [5]


def test_schedule_editor_follows_the_defaults(app):
    from ldm.qt.forms import ScheduleEditor

    editor = ScheduleEditor()
    editor.set_defaults({"schedule_start": "01:00", "schedule_stop": "05:00", "schedule_days": ""})
    assert editor.window() == {"schedule_start": "01:00", "schedule_stop": "05:00", "schedule_days": ""}
    assert editor.head.subtitle.text().startswith("Default times") and editor.reset_row.isHidden()
    editor.times.start.set_time("23:30")
    editor.times.days.buttons[0].setChecked(True)
    assert editor.window() == {"schedule_start": "23:30", "schedule_stop": "05:00", "schedule_days": "mon"}
    assert editor.head.subtitle.text() == "Own times: 23:30–05:00 (overnight), Mon"
    assert not editor.reset_row.isHidden()


def test_preferences_save_each_change(app, parent):
    from ldm.qt.preferences import PreferencesDialog

    client = FakeClient([], ListHostRules=[{"host": "example.com", "max_connections": 4, "max_downloads": 0,
                                           "include_subdomains": False}])
    dialog = PreferencesDialog(parent, client)
    dialog.show()
    assert all(page.isEnabled() for page in dialog.pages.values())
    switch = next(r for r in dialog.findChildren(QWidget) if getattr(r, "title", None) is not None
                  and hasattr(r, "switch") and r.title.text() == "Add without asking")
    switch.switch.toggle()
    assert client.settings["skip_add_dialog"] is True
    dialog.proxy_mode.combo.setCurrentIndex(2)  # manual
    assert client.settings["proxy_mode"] == "manual" and not dialog.proxy_url.isHidden()
    dialog.schedule_times.start.set_time("03:15")
    assert client.settings["schedule_start"] == "03:15"
    rule = dialog.rules.rows.itemAt(0).widget()
    assert rule.title.text() == "example.com" and rule.subtitle.text() == "4 connections · exact host"
    dialog.close()


def test_batch_preview_and_add(app, parent):
    from ldm.qt.tools import BatchDialog

    messages = []
    client = FakeClient([])
    dialog = BatchDialog(parent, client, messages.append)
    dialog.url.edit.setText("https://example.com/img*.jpg")
    dialog.last.set_value(3)
    dialog.pad.set_value(2)
    assert dialog.preview.title.text() == "3 links"
    assert dialog.preview.subtitle.text() == "https://example.com/img01.jpg\n…\nhttps://example.com/img03.jpg"
    dialog.accept()
    assert [args[0] for args in client.called("Add")] == [f"https://example.com/img0{i}.jpg" for i in (1, 2, 3)]
    assert messages == ["3 downloads added"]


def test_checksum_compares_with_the_clipboard(app, parent):
    from ldm.qt.tools import ChecksumDialog

    md5 = "0" * 32
    client = FakeClient([], Checksum=md5)
    dialog = ChecksumDialog(parent, client, 1, "file.zip")
    assert dialog.rows["md5"].subtitle.text() == md5
    dialog.show_comparison(md5.upper())
    assert dialog.result.subtitle.text() == "✓ Matches (MD5)"
    dialog.show_comparison("not a checksum")
    assert "doesn't hold" in dialog.result.subtitle.text()


def test_add_dialog_with_the_real_service(app, parent, daemon, http_server, tmp_path):

    data = random.Random(9).randbytes(300_000)
    cfg, url = http_server(data=data)
    client = real_client(daemon)
    spin_until(app, lambda: client.connected)
    added = []
    dialog = add_dialog(parent, client, url, on_added=added.append)
    spin_until(app, lambda: dialog.badge.text() == "Can resume")
    dialog.custom_folder = str(tmp_path / "out")
    dialog.folder_row.set_index(len(dialog.folder_row.values) - 1)
    dialog.start_button.click()
    spin_until(app, lambda: added)
    rows = []
    client.list(lambda result, error: rows.extend(result or []))
    spin_until(app, lambda: rows)
    client.close()
    assert rows[0]["id"] == added[0] and rows[0]["dir"] == str(tmp_path / "out")
