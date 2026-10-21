import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from ldm.gui.model import DownloadList  # noqa: E402


def row(i, state="queued", **extra):
    return {"id": i, "url": f"http://h/files/f{i}.bin", "dir": "/tmp", "state": state, "position": i,
            "done": 0, **extra}


def ids(model):
    return [model.get_item(n).id for n in range(model.get_n_items())]


def test_load_sorts_by_position_and_derives_fields():
    downloads = DownloadList()
    downloads.load([row(2, position=5), row(1, position=9, name="one.iso", size=100, done=25)])
    assert ids(downloads.store) == [2, 1]
    one = downloads.get(1)
    assert (one.title, one.fraction, one.status) == ("one.iso", 0.25, "Queued — 25 B of 100 B")
    assert downloads.get(2).title == "f2.bin"


def test_state_progress_and_removal():
    downloads = DownloadList()
    downloads.load([row(1, size=1000)])
    downloads.apply_state(1, "running", "")
    downloads.apply_progress([(1, 500, 1000, 100.0)])
    item = downloads.get(1)
    assert (item.state, item.done, item.fraction) == ("running", 500, 0.5)
    assert item.status.startswith("50% · ")
    downloads.apply_state(1, "done", "")
    assert (item.done, item.speed, item.status) == (1000, 0.0, "Finished — 1.0 KiB")
    downloads.remove(1)
    assert downloads.store.get_n_items() == 0 and downloads.get(1) is None


def test_views_follow_state_changes():
    downloads = DownloadList()
    downloads.load([row(1, "running"), row(2, "done"), row(3, "expired"), row(4, "paused")])
    views = {name: downloads.view(name) for name in ("all", "downloading", "finished", "problems")}
    assert ids(views["all"]) == [1, 2, 3, 4]
    assert ids(views["downloading"]) == [1, 4]
    assert ids(views["finished"]) == [2]
    assert ids(views["problems"]) == [3]
    downloads.apply_state(1, "done", "")
    downloads.upsert(row(5))
    assert ids(views["finished"]) == [1, 2]
    assert ids(views["downloading"]) == [4, 5]


def test_moving_resorts():
    downloads = DownloadList()
    downloads.load([row(1), row(2), row(3)])
    downloads.upsert(row(3, position=0))
    assert ids(downloads.store) == [3, 1, 2]


def test_waiting_reason_comes_and_goes():
    downloads = DownloadList()
    item = downloads.upsert(row(1, waiting="metered connection", scheduled=True))
    assert item.status == "Waiting: metered connection" and item.scheduled
    downloads.upsert(row(1))
    assert item.status == "Queued" and item.waiting == ""
    assert downloads.urls() == ["http://h/files/f1.bin"]
