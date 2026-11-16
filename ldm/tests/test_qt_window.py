"""The Qt window, drawn offscreen: model, filters, sidebar, details, actions; and once against a real service."""

import random
import time

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QSettings, Qt  # noqa: E402

from ldm.qt.model import ADDED, NAME, DownloadsFilter, DownloadsModel, queue_key  # noqa: E402
from qt_fake import FakeClient, real_client  # noqa: E402

MB = 1024 * 1024


@pytest.fixture
def app(qt_app):
    return qt_app


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


def row(i, name, state="queued", size=10 * MB, done=0, speed=0.0, **extra):
    return {"id": i, "url": f"https://example.com/{name}", "name": name, "dir": "/tmp", "state": state, "size": size,
            "done": done, "speed": speed, "kind": extra.pop("kind", "http"), "position": extra.pop("position", i),
            "added_at": 1_700_000_000 + i, **extra}


ROWS = [row(1, "a.iso", "running", done=5 * MB, speed=MB), row(2, "b.pdf", "done", done=10 * MB),
        row(3, "c.zip", "paused", done=MB), row(4, "d.mp3", "error", error="boom"),
        row(5, "e.zip", "queued", scheduled=True, schedule_start="01:00", schedule_stop="06:00", schedule_days="")]


def test_model_keeps_queue_order(app):
    model = DownloadsModel()
    model.load([row(2, "b", position=5), row(1, "a", position=9)])
    assert [d.id for d in model] == [2, 1]
    model.upsert(row(3, "c", position=1))
    assert [d.id for d in model] == [3, 2, 1]
    model.upsert(row(1, "a", position=0))  # moved to the top
    assert [d.id for d in model] == [1, 3, 2]
    model.upsert(row(1, "a", position=7))  # and down again
    assert [d.id for d in model] == [3, 2, 1]
    model.apply_progress([(2, 4 * MB, 10 * MB, 2.0 * MB)])
    assert model.get(2).fraction == 0.4
    model.apply_state(2, "paused", "")
    assert (model.get(2).state, model.get(2).speed) == ("paused", 0.0)
    model.remove(3)
    assert [d.id for d in model] == [2, 1] and model.get(3) is None


def test_filter_views_groups_queues_and_search(app):
    model = DownloadsModel()
    model.load(ROWS)
    proxy = DownloadsFilter(model)

    def shown():
        return [proxy.item(r).id for r in range(proxy.rowCount())]

    assert shown() == [1, 2, 3, 4, 5]
    proxy.show("finished")
    assert shown() == [2]
    proxy.show("problems")
    assert shown() == [4]
    proxy.show("all", group="archive")
    assert shown() == [1, 3, 5]
    proxy.show("all", queue="main")
    assert shown() == [1]
    proxy.show("all", queue=queue_key(model.get(5)))
    assert shown() == [5]
    proxy.show("all")
    proxy.set_text("ZIP")
    assert shown() == [3, 5]
    proxy.set_text("")
    proxy.sort(NAME, Qt.SortOrder.DescendingOrder)
    assert shown() == [5, 4, 3, 2, 1]
    proxy.sort(ADDED, Qt.SortOrder.AscendingOrder)
    assert shown() == [1, 2, 3, 4, 5]


@pytest.fixture
def window(app, tmp_path):
    from ldm.qt.window import MainWindow

    client = FakeClient([dict(r) for r in ROWS])
    w = MainWindow(client, QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    w.show()
    spin(app)
    yield w
    w.close()


def test_sidebar_counts_and_choosing_a_view(app, window):
    library = window.sidebar.library
    assert {k: item.count for k, item in library.items()} == {
        "all": 5, "downloading": 2, "scheduled": 1, "finished": 1, "problems": 1}
    assert window.sidebar.category_items["archive"].count == 3
    assert set(window.sidebar.queue_items) == {"main", queue_key(window.downloads.get(5))}
    window.sidebar.choose("view:problems")
    assert window.proxy.rowCount() == 1 and window.stack.currentWidget() is window.table
    window.sidebar.choose("view:scheduled")
    window.search.setText("nothing like this")
    assert window.stack.currentWidget() is window.empty
    assert window.empty.title.text() == "No matching downloads"


def test_details_follow_the_selection(app, window):
    window.table.select_id(4)
    assert window.details.download.id == 4
    assert window.details.primary.text() == "Retry"
    assert "Error" in [window.details.facts.itemAtPosition(r, 0).widget().text()
                       for r in range(window.details.facts.rowCount()) if window.details.facts.itemAtPosition(r, 0)]
    window.table.select_id(2)
    assert window.details.primary.text() == "Open"
    window.details_action.trigger()
    assert not window.details.isVisible()
    assert window.settings.value("details", type=bool) is False


def test_actions_follow_the_selection(app, window):
    window.table.select_id(3)  # paused
    assert window.resume_action.isEnabled() and not window.pause_action.isEnabled()
    window.resume_action.trigger()
    assert ("Resume", [3]) in window.client.calls
    window.table.select_id(1)  # running
    assert window.pause_action.isEnabled() and not window.resume_action.isEnabled()
    window.pause_action.trigger()
    assert ("Pause", [1]) in window.client.calls


def test_pasted_link_opens_the_add_dialog(app, window):
    from ldm.qt.add_dialog import AddDialog

    window.search.setText("https://example.com/new.iso")
    assert window.proxy.text == ""  # a link is not a search
    window.search.returnPressed.emit()
    assert window.search.text() == ""
    dialog = window.findChild(AddDialog)
    assert dialog is not None and dialog.url.text() == "https://example.com/new.iso"
    dialog.reject()


def test_pasted_link_is_added_without_asking(app, window):
    window.service["skip_add_dialog"] = True
    window.search.setText("https://example.com/new.iso")
    window.search.returnPressed.emit()
    assert [m for m, _ in window.client.calls if m in ("FindDuplicates", "Add")] == ["FindDuplicates", "Add"]
    assert window.toast_widget.isVisible() and window.toast_widget.label.text().startswith("Added")


def test_service_bar_and_theme(app, window):
    window._connection_changed(False)
    assert window.service_bar.isVisible() and not window.add_action.isEnabled()
    window.set_theme("dark")
    assert window.c.dark and window.settings.value("theme") == "dark"
    window.set_theme("light")
    assert not window.c.dark


def test_window_with_the_real_service(app, daemon, http_server, tmp_path):
    """A download added through the service shows up in the window, progresses and finishes."""
    from ldm.qt.window import MainWindow

    data = random.Random(7).randbytes(600_000)
    cfg, url = http_server(data=data, delay=0.02)
    client = real_client(daemon)
    w = MainWindow(client, QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    spin_until(app, lambda: client.connected)
    client.add(url, {"dir": str(tmp_path / "out"), "connections": 2}, None)
    spin_until(app, lambda: len(w.downloads) == 1)
    spin_until(app, lambda: next(iter(w.downloads)).state == "done")
    d = next(iter(w.downloads))
    assert (d.size, d.fraction, d.state_word) == (len(data), 1.0, "Done")
    assert w.sidebar.library["finished"].count == 1
    w.close()


def test_toolbar_icons_survive_toggling(app, window):
    """A toolbar button copies its action's icon whenever the action changes: the icons live on the actions."""
    button = next(b for b, name in window.tool_buttons if name == "details")
    for _ in range(2):
        window.details_action.trigger()
        assert not button.icon().isNull() and button.text() == "Details"
    window.table.select_id(3)  # enabling and disabling actions keeps the icons too
    assert all(not b.icon().isNull() for b, _ in window.tool_buttons)


def test_details_panel_resizes_and_remembers(app, window):
    sizes = window.splitter.sizes()
    window.splitter.setSizes([sizes[0] - 60, sizes[1] + 60])
    window.splitter.splitterMoved.emit(0, 1)
    assert window.settings.value("details_width", type=int) == window.details.width() >= 280


def test_add_link_skips_a_copied_link_already_in_the_list(app, window):
    from PySide6.QtGui import QGuiApplication

    QGuiApplication.clipboard().setText("https://example.com/c.zip")  # in ROWS
    dialog = window.ask_add()
    assert dialog.url.text() == ""
    dialog.reject()
    QGuiApplication.clipboard().setText("https://example.com/new.zip")
    dialog = window.ask_add()
    assert dialog.url.text() == "https://example.com/new.zip"
    dialog.reject()


def test_queue_actions_only_when_something_to_act_on(app, tmp_path):
    from qt_fake import FakeClient

    from ldm.qt.window import MainWindow

    w = MainWindow(FakeClient([row(1, "a.pdf", "done"), row(2, "b.zip", "error")]),
                   QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    assert not w.pause_all_action.isEnabled() and not w.stop_queue_action.isEnabled()
    assert not w.start_queue_action.isEnabled() and w.resume_all_action.isEnabled()  # the failed one
    w.downloads.upsert(row(3, "c.iso", "running"))
    assert w.pause_all_action.isEnabled()
    w.close()


def test_details_show_queue_place_and_checksum_link(app, window):
    window.downloads.upsert(row(6, "f.zip", "queued", position=6))
    window.table.select_id(6)

    def facts():
        grid = window.details.facts
        return {grid.itemAtPosition(r, 0).widget().text(): grid.itemAtPosition(r, 1).widget()
                for r in range(grid.rowCount()) if grid.itemAtPosition(r, 0)}

    assert facts()["Queue"].toolTip() == "Main queue, 1 of 1"  # the full value (the label may be shortened)
    assert window.details.parts_label.text().endswith("connections") or window.details.parts_label.text() == "1 connection"
    window.table.select_id(2)  # finished
    link = facts()["Checksum"]
    assert "Calculate" in link.text()
    opened = []
    window.show_checksum = opened.append
    link.linkActivated.emit("checksum")
    assert opened and opened[0].id == 2


def test_paused_video_can_resume(app, window):
    window.downloads.upsert(row(7, "talk", "paused", kind="media", quality="Best quality"))
    window.table.select_id(7)
    grid = window.details.facts
    facts = {grid.itemAtPosition(r, 0).widget().text(): grid.itemAtPosition(r, 1).widget().toolTip()
             for r in range(grid.rowCount()) if grid.itemAtPosition(r, 0)}  # full values (labels may be shortened)
    assert facts["Can resume"] == "Yes" and facts["Type"] == "Video, through yt-dlp"
    assert window.details.strip_box.isHidden()  # yt-dlp doesn't report connections
