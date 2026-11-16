"""Display text and input checks shared by the windows (no toolkit needed)."""

import sys
from pathlib import Path

from ldm.ui import text


def test_links():
    assert text.is_url(" https://example.com/a.zip ")
    assert not text.is_url("ftp://example.com/a.zip") and not text.is_url("https://")
    assert text.is_torrent_link("magnet:?xt=urn:btih:abc")
    assert not text.is_torrent_link("magnet:?dn=name")
    assert text.is_torrent_link("https://example.com/pack.TORRENT")
    assert text.looks_like_login("https://example.com/account/login?next=/a.zip")
    assert not text.looks_like_login("https://example.com/files/a.zip")


def test_speed_limits():
    assert text.parse_limit("") == 0
    assert text.parse_limit("nonsense") is None
    assert text.parse_limit("2M") == 2 * 1024 * 1024
    assert text.limit_text(0) == "0"
    assert text.limit_text(2 * 1024 * 1024) == "2.0M"


def test_short_path():
    home = str(Path.home())
    if sys.platform == "win32":  # Windows users don't know "~": paths stay as they are
        assert text.short_path(home + "\\Downloads") == home + "\\Downloads"
        return
    assert text.short_path(home + "/Downloads") == "~/Downloads"
    assert text.short_path(home) == "~"
    assert text.short_path("/srv/files") == "/srv/files"


def test_schedule_and_durations():
    assert text.describe_window("01:00", "06:00", "") == "01:00–06:00, every day"
    assert text.describe_window("23:00", "05:00", "mon,tue") == "23:00–05:00 (overnight), Mon Tue"
    assert text.duration_text(None) == ""
    assert text.duration_text(75) == "1:15"
    assert text.duration_text(3725) == "1:02:05"


def test_details():
    assert text.capture_details({"url": "https://cdn.example.com/v.m3u8", "kind": "hls"}) == "HLS stream · cdn.example.com"
    assert text.torrent_extra("running", 1, 0, 0) == " · 1 peer"
    assert text.torrent_extra("seeding", 3, 0, 1.5) == " · ratio 1.50"
    assert text.torrent_extra("paused", 3, 0, 1.5) == ""
    assert text.key_valid("") and not text.key_valid("not a key")
