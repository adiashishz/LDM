"""The toolkit-free download item: what the table and details show for each state."""

from datetime import datetime

from ldm.ui.item import LIBRARY, Download, when_text

MB = 1024 * 1024


def test_running_download():
    d = Download({"id": 1, "url": "https://example.com/files/big%20file.iso", "state": "running", "size": 100 * MB,
                  "done": 25 * MB, "speed": 5 * MB, "kind": "http"})
    assert d.title == "big file.iso" and d.extension == "ISO" and d.group == "archive"
    assert (d.state_word, d.tone, d.speed_text) == ("25%", "active", "5.0 MiB/s")
    assert d.left_text == "15 s"


def test_states_and_words():
    paused = Download({"id": 2, "url": "https://x/a.zip", "state": "paused", "size": 10, "done": 5, "speed": 3.0})
    assert (paused.state_word, paused.tone, paused.speed, paused.speed_text) == ("Paused", "muted", 0.0, "")
    done = Download({"id": 3, "url": "https://x/a.pdf", "state": "done", "size": 10, "done": 0})
    assert (done.state_word, done.tone, done.fraction) == ("Done", "done", 1.0)
    expired = Download({"id": 4, "url": "https://x/a", "state": "expired"})
    assert (expired.state_word, expired.tone, expired.extension) == ("Expired", "bad", "")
    torrent = Download({"id": 5, "url": "magnet:?xt=urn:btih:a", "kind": "torrent", "name": "linux"})
    assert (torrent.extension, torrent.group) == ("TOR", "torrent")
    scheduled = Download({"id": 6, "url": "https://x/b.zip", "state": "queued", "scheduled": True})
    assert scheduled.state_word == "Scheduled"


def test_library_views():
    rows = [Download({"id": i, "url": f"https://x/{i}", "state": s, "scheduled": sch})
            for i, (s, sch) in enumerate([("running", False), ("queued", True), ("done", True), ("error", False)])]

    def names(view):
        return [d.id for d in rows if LIBRARY[view][1](d)]

    assert names("all") == [0, 1, 2, 3]
    assert names("downloading") == [0]
    assert names("scheduled") == [1]  # finished downloads leave the schedule
    assert names("finished") == [2]
    assert names("problems") == [3]


def test_when_text():
    now = datetime(2026, 11, 16, 12, 0)
    assert when_text(0) == ""
    assert when_text(datetime(2026, 11, 16, 10, 42).timestamp(), now) == "Today 10:42"
    assert when_text(datetime(2026, 11, 15, 23, 0).timestamp(), now) == "Yesterday"
    assert when_text(datetime(2026, 11, 13, 9, 5).timestamp(), now) == "Fri 09:05"
    assert when_text(datetime(2026, 10, 3, 9, 5).timestamp(), now) == "3 Oct"
    assert when_text(datetime(2025, 10, 3, 9, 5).timestamp(), now) == "3 Oct 2025"
