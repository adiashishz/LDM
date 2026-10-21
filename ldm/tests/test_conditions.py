"""Scheduler window, metered network and battery conditions in the manager (fake monitor and clock)."""

import asyncio
from datetime import datetime

import pytest

from ldm.manager import InvalidArgument, Manager
from ldm.store import Store
from ldm.system import Conditions

from test_manager import payload, run, wait_state


class FakeInhibitor:
    def __init__(self):
        self.calls, self.active = [], False

    def set_active(self, active):  # like SleepInhibitor: only changes matter
        if active != self.active:
            self.active = active
            self.calls.append(active)

    def close(self):
        self.calls.append("closed")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def new_manager(tmp_path, clock=None, **settings):
    store = Store(tmp_path / "state.db")
    conditions, inhibitor = Conditions(), FakeInhibitor()
    manager = Manager(store, progress_interval=0.05, conditions=conditions, inhibitor=inhibitor,
                      clock=clock or Clock(datetime(2026, 10, 19, 12, 0)),
                      downloader_options={"min_part": 64 * 1024, "retry_base_delay": 0.01,
                                          "persist_interval": 0.05})
    store.set_setting("download_dir", str(tmp_path / "dl"))
    for key, value in settings.items():
        store.set_setting(key, value)
    return manager, conditions, inhibitor


def test_scheduled_download_waits_for_the_window(tmp_path, http_server):
    data = payload(200_000)
    cfg, url = http_server(data=data)
    clock = Clock(datetime(2026, 10, 19, 12, 0))  # Monday noon
    manager, _, inhibitor = new_manager(tmp_path, clock, schedule_start="23:00", schedule_stop="06:00")

    async def scenario():
        scheduled = manager.add(url, scheduled=True)
        normal = manager.add(url, name="now.bin")
        assert await wait_state(manager, normal, "done") == "done"
        await asyncio.sleep(0.1)
        info = manager.describe(scheduled)
        assert (info["state"], info["scheduled"], info["waiting"]) == ("queued", True, "scheduled — starts 23:00")
        clock.now = datetime(2026, 10, 19, 23, 1)
        manager.conditions_changed()  # what the 30 s tick does
        assert await wait_state(manager, scheduled, "done") == "done"
        assert "waiting" not in manager.describe(scheduled)

    run(manager, scenario)
    assert inhibitor.calls[:2] == [True, False] and inhibitor.calls[-1] == "closed"


def test_schedule_closing_holds_a_running_download(tmp_path, http_server):
    data = payload(2_000_000)
    cfg, url = http_server(data=data, delay=0.01)
    clock = Clock(datetime(2026, 10, 19, 23, 30))
    manager, _, _ = new_manager(tmp_path, clock, schedule_start="23:00", schedule_stop="06:00")

    async def scenario():
        i = manager.add(url, scheduled=True, connections=2)
        await wait_state(manager, i, "running")
        await asyncio.sleep(0.2)
        clock.now = datetime(2026, 10, 20, 6, 0)
        manager.conditions_changed()
        await asyncio.sleep(0.3)
        d = manager.store.get(i)
        done = sum(p.done for p in manager.store.load_parts(i))
        assert d.state == "queued" and 0 < done < len(data)  # held with progress, not paused
        assert manager.describe(i)["waiting"] == "scheduled — starts 23:00"
        manager.set_schedule(i, False)  # taking it off the schedule releases it
        assert await wait_state(manager, i, "done") == "done"

    run(manager, scenario)
    assert manager.store.get(1).dest.read_bytes() == data


def test_metered_and_battery_holds(tmp_path, http_server):
    data = payload(1_500_000)
    cfg, url = http_server(data=data, delay=0.01)
    manager, conditions, _ = new_manager(tmp_path, battery_limit=30)

    async def scenario():
        conditions.metered = True
        wifi_only = manager.add(url, unmetered_only=True)
        await asyncio.sleep(0.1)
        assert manager.describe(wifi_only)["waiting"] == "metered connection"
        conditions.metered = False
        manager.conditions_changed()
        await wait_state(manager, wifi_only, "running")
        conditions.on_battery, conditions.battery = True, 20.0
        manager.conditions_changed()
        await asyncio.sleep(0.3)
        assert manager.describe(wifi_only)["waiting"] == "battery below 30%"
        manager.pause(wifi_only)  # a user pause still works while held
        assert manager.store.get(wifi_only).state == "paused"
        assert "waiting" not in manager.describe(wifi_only)
        conditions.on_battery = False
        manager.resume(wifi_only)
        assert await wait_state(manager, wifi_only, "done") == "done"

    run(manager, scenario)


def test_pause_on_metered_and_new_links_join_schedule(tmp_path):
    manager, conditions, _ = new_manager(tmp_path, pause_on_metered=True, schedule_new_links=True)

    async def scenario():
        conditions.metered = True
        i = manager.add("http://127.0.0.1:9/never", start=False)
        assert manager.describe(i)["scheduled"] is True
        manager.set_schedule(i, False)
        manager.resume(i)
        assert manager.describe(i)["waiting"] == "metered connection"

    run(manager, scenario)


def test_each_download_can_have_its_own_window(tmp_path):
    clock = Clock(datetime(2026, 10, 19, 12, 0))  # Monday noon
    manager, _, _ = new_manager(tmp_path, clock, schedule_start="02:00", schedule_stop="06:00")

    async def scenario():
        default = manager.add("http://127.0.0.1:9/a", scheduled=True, start=False)
        own = manager.add("http://127.0.0.1:9/b", scheduled=True, schedule_start="20:00",
                          schedule_stop="23:00", schedule_days="sat,sun", start=False)
        same = manager.add("http://127.0.0.1:9/c", scheduled=True, schedule_start="02:00", start=False)
        a, b, c = (manager.describe(i) for i in (default, own, same))
        assert (a["schedule_start"], a["schedule_stop"], a["schedule_custom"]) == ("02:00", "06:00", False)
        assert (b["schedule_start"], b["schedule_stop"], b["schedule_days"], b["schedule_custom"]) == \
            ("20:00", "23:00", "sat,sun", True)
        assert c["schedule_custom"] is False  # same as the defaults: keeps following them

        manager.update_settings({"schedule_start": "03:00"})  # defaults move, own times don't
        assert manager.describe(default)["schedule_start"] == "03:00"
        assert manager.describe(own)["schedule_start"] == "20:00"

        for i in (default, own):
            manager.resume(i)
        assert manager.describe(default)["waiting"] == "scheduled — starts tomorrow 03:00"
        assert manager.describe(own)["waiting"] == "scheduled — starts Sat 20:00"

        manager.set_schedule(own, True, {"schedule_days": ""})  # every day, its own times otherwise default
        b = manager.describe(own)
        assert (b["schedule_start"], b["schedule_days"], b["waiting"]) == ("03:00", "", "scheduled — starts tomorrow 03:00")
        assert b["schedule_custom"] is False  # equal to the defaults again
        with pytest.raises(InvalidArgument):
            manager.set_schedule(own, True, {"schedule_stop": "24:61"})
        with pytest.raises(InvalidArgument):
            manager.add("http://127.0.0.1:9/d", scheduled=True, schedule_days="funday")

    run(manager, scenario)


@pytest.mark.parametrize("key, value", [("schedule_start", "25:00"), ("schedule_days", "funday"),
                                        ("battery_limit", 120)])
def test_schedule_settings_are_validated(tmp_path, key, value):
    manager, _, _ = new_manager(tmp_path)
    with pytest.raises(InvalidArgument):
        manager.update_settings({key: value})
