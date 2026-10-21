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


def test_login_and_log_commands(daemon, http_server, monkeypatch, capsys):
    import io
    import random
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", daemon)
    data = random.Random(3).randbytes(100_000)
    cfg, url = http_server(data=data, auth=("digest", "ann", "s3cret"))
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret\n"))
    assert main(["login", "127.0.0.1", "ann", "--exact", "--password-stdin"]) == 0
    assert main(["settings", "proxy_mode", "none"]) == 0
    assert main(["add", url, "--wait"]) == 0
    capsys.readouterr()
    assert main(["logins"]) == 0
    assert capsys.readouterr().out == "127.0.0.1 (exact): ann\n"
    assert main(["log", "1"]) == 0
    log = capsys.readouterr().out
    assert "← HTTP 401" in log and "← HTTP 206" in log and "s3cret" not in log
    monkeypatch.setattr("sys.stdin", io.StringIO("proxypw\n"))
    assert main(["proxy-password", "--password-stdin"]) == 0
    assert main(["login", "127.0.0.1", "--remove"]) == 0
    capsys.readouterr()
    assert main(["logins"]) == 0 and capsys.readouterr().out == ""


def test_batch_import_and_export_commands(daemon, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", daemon)
    assert main(["batch", "http://127.0.0.1:9/img*.jpg", "--from", "8", "--to", "10", "--pad", "2", "--paused"]) == 0
    links = tmp_path / "links.txt"
    links.write_text("# list\nhttp://127.0.0.1:9/a.zip\nhttp://127.0.0.1:9/b.zip\n")
    assert main(["import", str(links), "--paused"]) == 0
    out = tmp_path / "out.txt"
    assert main(["export-links", str(out)]) == 0
    assert out.read_text().splitlines() == ["http://127.0.0.1:9/img08.jpg", "http://127.0.0.1:9/img09.jpg",
                                            "http://127.0.0.1:9/img10.jpg", "http://127.0.0.1:9/a.zip",
                                            "http://127.0.0.1:9/b.zip"]
    saved = tmp_path / "unfinished.json"
    assert main(["export-unfinished", str(saved)]) == 0
    assert main(["import-unfinished", str(saved)]) == 0
    assert "5 downloads imported" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="exactly one"):
        main(["batch", "http://127.0.0.1:9/img.jpg", "--to", "3"])
    with pytest.raises(SystemExit, match="--to"):
        main(["batch", "http://127.0.0.1:9/img*.jpg"])


def test_backup_restore_and_session_commands(daemon, monkeypatch, capsys, tmp_path):
    import io
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", daemon)
    assert main(["add", "http://127.0.0.1:9/keep.iso", "--paused"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("pw\n"))
    assert main(["backup", str(tmp_path / "b"), "--encrypt", "--password-stdin"]) == 0
    saved = capsys.readouterr().out.strip().splitlines()[-1]
    assert saved.startswith(str(tmp_path / "b" / "ldm-backup-")) and saved.endswith(".ldmbackup")
    assert main(["rm", "1"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("pw\n"))
    assert main(["restore", saved, "--password-stdin"]) == 0
    assert "1 downloads restored" in capsys.readouterr().out
    assert main(["data-usage"]) == 0
    assert capsys.readouterr().out == "used this session: 0 B (no limit)\n"
    assert main(["resume-all"]) == 0 and main(["pause-all"]) == 0
    assert "1 downloads paused" in capsys.readouterr().out
