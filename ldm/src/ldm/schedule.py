"""Scheduler time windows (1DM's download scheduler): start/stop time of day on chosen weekdays."""

from __future__ import annotations

from datetime import datetime, timedelta

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_time(text: str) -> int:
    """'HH:MM' -> minutes after midnight."""
    hours, _, minutes = text.strip().partition(":")
    value = int(hours) * 60 + int(minutes or 0)
    if not 0 <= value < 24 * 60 or not 0 <= int(minutes or 0) < 60:
        raise ValueError(f"invalid time: {text!r}")
    return value


def parse_days(text: str) -> set[int]:
    """'mon,wed' -> {0, 2}; empty means every day."""
    names = [d.strip().lower()[:3] for d in text.split(",") if d.strip()]
    unknown = [d for d in names if d not in DAYS]
    if unknown:
        raise ValueError(f"unknown day: {unknown[0]}")
    return {DAYS.index(d) for d in names} or set(range(7))


def in_window(now: datetime, start: str, stop: str, days: str) -> bool:
    """Is `now` inside the window? A stop earlier than start runs overnight (e.g. 23:00-06:00);
    the day that counts is the day the window started. start == stop means the whole day."""
    s, e, allowed = parse_time(start), parse_time(stop), parse_days(days)
    minute, day = now.hour * 60 + now.minute, now.weekday()
    if s < e:
        return day in allowed and s <= minute < e
    if s > e:
        return (minute >= s and day in allowed) or (minute < e and (day - 1) % 7 in allowed)
    return day in allowed


def next_start(now: datetime, start: str, stop: str, days: str) -> datetime | None:
    """When the window opens next (None if it is open now)."""
    if in_window(now, start, stop, days):
        return None
    s, allowed = parse_time(start), parse_days(days)
    base = now.replace(hour=s // 60, minute=s % 60, second=0, microsecond=0)
    for offset in range(8):
        candidate = base + timedelta(days=offset)
        if candidate > now and candidate.weekday() in allowed:
            return candidate
    return None


def describe_start(when: datetime, now: datetime) -> str:
    if when.date() == now.date():
        return when.strftime("%H:%M")
    if when.date() == (now + timedelta(days=1)).date():
        return when.strftime("tomorrow %H:%M")
    return when.strftime("%a %H:%M")
