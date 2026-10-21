from ldm.format import eta_text, human, status_text

MIB = 1024 * 1024


def test_human():
    assert human(0) == "0 B"
    assert human(1536) == "1.5 KiB"
    assert human(10 * MIB) == "10.0 MiB"


def test_eta_text():
    assert eta_text(100, 0) == ""
    assert eta_text(0, 10) == ""
    assert eta_text(30, 1) == "30 s left"
    assert eta_text(600, 1) == "10 min left"
    assert eta_text(7500, 1) == "2 h 5 min left"


def test_status_text():
    assert status_text("running", 5 * MIB, 10 * MIB, MIB) == "50% · 5.0 MiB of 10.0 MiB · 1.0 MiB/s · 5 s left"
    assert status_text("running", 3 * MIB, None, 0) == "3.0 MiB · 0 B/s"
    assert status_text("paused", MIB, 10 * MIB) == "Paused — 1.0 MiB of 10.0 MiB"
    assert status_text("queued", 0, None) == "Queued"
    assert status_text("done", 10 * MIB, 10 * MIB) == "Finished — 10.0 MiB"
    assert status_text("error", 0, None, error="HTTP 500") == "Failed: HTTP 500"
    assert status_text("expired", 0, 10) == "Link expired — set a new link"
