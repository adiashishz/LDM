"""Checksums, rename/move, move-after-finish, remove-when-finished, cleanup of old entries."""

import asyncio
import hashlib
from datetime import datetime, timedelta

import pytest

from ldm import files
from ldm.manager import Busy, InvalidArgument, Manager
from ldm.store import Store

from test_manager import payload, run, wait_state


def new_manager(tmp_path, clock=None, **settings) -> Manager:
    store = Store(tmp_path / "state.db")
    extra = {"clock": clock} if clock else {}
    manager = Manager(store, progress_interval=0.05, **extra,
                      downloader_options={"min_part": 64 * 1024, "persist_interval": 0.05})
    store.set_setting("download_dir", str(tmp_path / "dl"))
    for key, value in settings.items():
        store.set_setting(key, value)
    return manager


def test_checksums_and_clipboard_detection(tmp_path, http_server):
    data = payload(300_000)
    cfg, url = http_server(data=data)
    manager = new_manager(tmp_path)

    async def scenario():
        i = manager.add(url)
        await wait_state(manager, i, "done")
        return i, {a: await manager.checksum(i, a) for a in files.ALGORITHMS}

    i, sums = run(manager, scenario)
    assert sums == {a: hashlib.new(a, data).hexdigest() for a in files.ALGORITHMS}
    assert [files.algorithm_for(sums[a].upper() + "\n") for a in files.ALGORITHMS] == list(files.ALGORITHMS)
    assert files.algorithm_for("not-hex") is None and files.algorithm_for("ab") is None


def test_rename_and_move_finished_and_paused(tmp_path, http_server):
    data = payload(2_000_000)
    cfg, url = http_server(data=data, delay=0.01)
    manager = new_manager(tmp_path)
    (tmp_path / "dl").mkdir()
    (tmp_path / "dl" / "taken.bin").write_bytes(b"x")

    async def scenario():
        done = manager.add(url, name="first.bin")
        await wait_state(manager, done, "done")
        renamed = await manager.rename(done, "taken.bin")       # taken: numbered, never overwritten
        moved = await manager.move_to(done, str(tmp_path / "elsewhere"))
        with pytest.raises(InvalidArgument):
            await manager.rename(done, "../escape")

        partial = manager.add(url, name="second.bin", connections=2)
        await wait_state(manager, partial, "running")
        with pytest.raises(Busy):
            await manager.rename(partial, "x.bin")
        await asyncio.sleep(0.3)
        manager.pause(partial)
        await wait_state(manager, partial, "paused")
        await manager.move_to(partial, str(tmp_path / "later"))
        await manager.rename(partial, "renamed.bin")
        manager.resume(partial)
        await wait_state(manager, partial, "done")
        return done, partial, renamed, moved

    done, partial, renamed, moved = run(manager, scenario)
    assert renamed.endswith("taken (1).bin") and (tmp_path / "dl" / "taken.bin").read_bytes() == b"x"
    assert moved == str(tmp_path / "elsewhere" / "taken (1).bin")
    assert (tmp_path / "elsewhere" / "taken (1).bin").read_bytes() == data
    assert (tmp_path / "later" / "renamed.bin").read_bytes() == data
    assert not list((tmp_path / "dl").glob("*.part"))


def test_move_after_finish_and_remove_from_list(tmp_path, http_server):
    data = payload(100_000)
    cfg, url = http_server(data=data)
    manager = new_manager(tmp_path, move_finished_dir=str(tmp_path / "done"), remove_finished=True)

    async def scenario():
        manager.add(url)
        for _ in range(500):  # finished, moved, then removed from the list
            if not manager.store.list():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("not removed")

    run(manager, scenario)
    assert (tmp_path / "done" / "test.bin").read_bytes() == data


def test_cleanup_old_entries(tmp_path):
    now = datetime(2026, 10, 21, 12, 0)
    manager = new_manager(tmp_path, clock=lambda: now, cleanup_days=7, cleanup_finished_only=False)
    store = manager.store
    old_done = store.add("http://127.0.0.1:9/old", tmp_path, state="paused")
    store.finish(old_done, "old.bin", 1)
    (tmp_path / "old.bin").write_bytes(b"keep me")
    new_done = store.add("http://127.0.0.1:9/new", tmp_path, state="paused")
    store.finish(new_done, "new.bin", 1)
    old_paused = store.add("http://127.0.0.1:9/stale", tmp_path, name="stale.bin", state="paused")
    long_ago = (now - timedelta(days=8)).timestamp()
    store.update(old_done, finished_at=long_ago)
    store.update(old_paused, added_at=long_ago)
    store.update(new_done, finished_at=(now - timedelta(days=1)).timestamp())

    async def scenario():
        await asyncio.sleep(0.2)

    run(manager, scenario)
    assert [d.id for d in store.list()] == [new_done]
    assert (tmp_path / "old.bin").read_bytes() == b"keep me"  # entries go, files stay
