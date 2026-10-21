from datetime import datetime

import pytest

from ldm.schedule import describe_start, in_window, next_start, parse_days, parse_time

MON = datetime(2026, 10, 19, 12, 0)  # a Monday


def at(day_offset, hour, minute=0):
    return MON.replace(day=MON.day + day_offset, hour=hour, minute=minute)


def test_parse():
    assert parse_time("02:30") == 150
    assert parse_days("") == set(range(7))
    assert parse_days("mon, Sat") == {0, 5}
    with pytest.raises(ValueError):
        parse_time("25:00")
    with pytest.raises(ValueError):
        parse_days("funday")


def test_daytime_window():
    assert in_window(at(0, 9), "09:00", "17:00", "")
    assert not in_window(at(0, 17), "09:00", "17:00", "")
    assert not in_window(at(0, 10), "09:00", "17:00", "tue")


def test_overnight_window_counts_the_starting_day():
    assert in_window(at(0, 23, 30), "23:00", "06:00", "mon")
    assert in_window(at(1, 5, 59), "23:00", "06:00", "mon")      # Tuesday morning, started Monday
    assert not in_window(at(1, 23, 30), "23:00", "06:00", "mon")  # Tuesday night: not scheduled
    assert not in_window(at(0, 5), "23:00", "06:00", "mon")       # Monday morning: Sunday's window


def test_whole_day_window():
    assert in_window(at(0, 3), "00:00", "00:00", "mon")
    assert not in_window(at(1, 3), "00:00", "00:00", "mon")


def test_next_start():
    assert next_start(at(0, 9), "09:00", "17:00", "") is None
    assert next_start(at(0, 18), "09:00", "17:00", "") == at(1, 9)
    assert next_start(at(0, 18), "09:00", "17:00", "fri") == at(4, 9)
    assert next_start(at(0, 8), "09:00", "17:00", "") == at(0, 9)


def test_describe_start():
    assert describe_start(at(0, 23), MON) == "23:00"
    assert describe_start(at(1, 2), MON) == "tomorrow 02:00"
    assert describe_start(at(4, 2), MON) == "Fri 02:00"
