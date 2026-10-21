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
