import random

import pytest

from ldm.cli import file_priorities, main, parse_rate


def test_get_then_resume_finished_download_is_a_no_op(tmp_path, http_server, capsys):
    data = random.Random(3).randbytes(200_000)
    cfg, url = http_server(data=data)
    db, out = tmp_path / "state.db", tmp_path / "out"

    assert main(["--db", str(db), "get", url, "-o", str(out), "-c", "2"]) == 0
    assert (out / "test.bin").read_bytes() == data

    requests_before = len(cfg.requests)
    assert main(["--db", str(db), "get", "--resume", "1"]) == 0
    assert "already finished" in capsys.readouterr().out
    assert len(cfg.requests) == requests_before
    assert sorted(p.name for p in out.iterdir()) == ["test.bin"]


@pytest.mark.parametrize("text,expected", [("0", 0), ("1500", 1500), ("500K", 512_000),
                                           ("2M", 2 * 1024 ** 2), ("1.5MiB/s", 1_572_864)])
def test_parse_rate(text, expected):
    assert parse_rate(text) == expected


def test_file_priorities():
    assert file_priorities("0,2") == [1, 0, 1]
    assert file_priorities("3") == [0, 0, 0, 1]


def test_schedule_commands_against_daemon(daemon, monkeypatch, capsys):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", daemon)  # the test daemon's private bus
    assert main(["settings", "schedule_start", "01:30"]) == 0
    assert main(["settings", "schedule_speed_limit", "2M"]) == 0
    assert main(["add", "http://127.0.0.1:9/night.iso", "--scheduled", "--unmetered-only", "--paused"]) == 0
    assert main(["add", "http://127.0.0.1:9/weekend.iso", "--start", "10:00", "--days", "sat,sun", "--paused"]) == 0
    assert main(["schedule", "1", "on", "--stop", "05:00"]) == 0
    assert main(["schedule", "2", "off"]) == 0
    assert main(["settings", "schedule_start", "25:00"]) == 1
    assert main(["schedule", "1", "on", "--days", "funday"]) == 1
    capsys.readouterr()
    assert main(["settings"]) == 0
    out = capsys.readouterr().out
    assert "schedule_start = 01:30" in out and "schedule_speed_limit = 2097152" in out
