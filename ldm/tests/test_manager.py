import asyncio
import random
import time

import pytest

from ldm.manager import Manager, SizeMismatch
from ldm.store import Store

FINAL = {"done", "error", "expired", "changed", "paused"}


def payload(size: int, seed: int = 1) -> bytes:
    return random.Random(seed).randbytes(size)


def new_manager(tmp_path, **settings) -> Manager:
    store = Store(tmp_path / "state.db")
    manager = Manager(store, progress_interval=0.05,
                      downloader_options={"min_part": 64 * 1024, "retry_base_delay": 0.01,
                                          "persist_interval": 0.05})
    store.set_setting("download_dir", str(tmp_path / "dl"))
    store.set_setting("retry_interval", 0)
    for key, value in settings.items():
        store.set_setting(key, value)
    return manager


async def wait_state(manager: Manager, download_id: int, states, timeout: float = 15.0,
                     watch: list | None = None) -> str:
    """Poll until the download reaches one of `states`; optionally record len(running)."""
    states = {states} if isinstance(states, str) else set(states)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if watch is not None:
            watch.append(len(manager.running))
        state = manager.store.get(download_id).state
        if state in states:
            return state
        await asyncio.sleep(0.01)
    raise AssertionError(f"download {download_id} stuck in {manager.store.get(download_id).state}")


def run(manager: Manager, scenario):
    async def go():
        await manager.start()
        try:
            return await scenario()
        finally:
            await manager.shutdown()
    return asyncio.run(go())


def test_parallel_limit_is_respected(tmp_path, http_server):
    data = payload(300_000)
    cfg, url = http_server(data=data, delay=0.005)
    manager = new_manager(tmp_path, max_parallel=2, default_connections=2)
    observed = []

    async def scenario():
        ids = [manager.add(url) for _ in range(5)]
        for i in ids:
            assert await wait_state(manager, i, "done", watch=observed) == "done"
        return ids

    ids = run(manager, scenario)
    assert max(observed) == 2
    for i in ids:
        assert manager.store.get(i).dest.read_bytes() == data


def test_pause_and_resume(tmp_path, http_server):
    data = payload(1_000_000)
    cfg, url = http_server(data=data, delay=0.01)
    manager = new_manager(tmp_path)

    async def scenario():
        i = manager.add(url, connections=2)
        await asyncio.sleep(0.2)
        manager.pause(i)
        await wait_state(manager, i, "paused")
        assert manager.describe(i)["done"] > 0
        cfg.delay = 0
        manager.resume(i)
        await wait_state(manager, i, "done")
        return i

    i = run(manager, scenario)
    assert manager.store.get(i).dest.read_bytes() == data


def test_remove_running_download_deletes_partial_file(tmp_path, http_server):
    cfg, url = http_server(data=payload(1_000_000), delay=0.01)
    manager = new_manager(tmp_path)
    removed = []
    manager.on_removed = removed.append

    async def scenario():
        i = manager.add(url, connections=2)
        await asyncio.sleep(0.2)
        temp = manager.store.get(i).temp_path
        assert temp.exists()
        await manager.remove(i, delete_files=True)
        return i, temp

    i, temp = run(manager, scenario)
    assert not temp.exists()
    assert removed == [i]
    with pytest.raises(KeyError):
        manager.store.get(i)


def test_host_rule_limits_downloads_and_connections(tmp_path, http_server):
    data = payload(400_000)
    cfg, url = http_server(data=data, delay=0.005)
    manager = new_manager(tmp_path, max_parallel=5)
    manager.set_host_rule("127.0.0.1", max_connections=2, max_downloads=1)
    observed = []

    async def scenario():
        ids = [manager.add(url, connections=8) for _ in range(3)]
        for i in ids:
            await wait_state(manager, i, "done", watch=observed)

    run(manager, scenario)
    assert max(observed) == 1
    assert cfg.max_active_ranges <= 2


def test_errors_are_retried_automatically(tmp_path, http_server):
    data = payload(200_000)
    cfg, url = http_server(data=data, status=500)
    manager = new_manager(tmp_path, retry_count=2)
    manager._options["max_retries"] = 0
    states = []

    def on_state(i, state, error):
        states.append(state)
        if state == "error":
            cfg.status = None  # server recovers before the automatic retry

    manager.on_state = on_state

    async def scenario():
        i = manager.add(url)
        await wait_state(manager, i, "done")
        for _ in range(200):  # the store says done slightly before the manager emits it
            if states[-1] == "done":
                break
            await asyncio.sleep(0.01)
        return i

    i = run(manager, scenario)
    assert "error" in states and states[-1] == "done"
    assert manager.store.get(i).dest.read_bytes() == data


def test_new_url_keeps_downloaded_data(tmp_path, http_server):
    data = payload(1_000_000)
    cfg_a, url_a = http_server(data=data, delay=0.01)
    # small delay: the probe in set_url closes early, so the server must not race ahead and
    # count the whole file as sent
    cfg_b, url_b = http_server(data=data, delay=0.002)
    cfg_c, url_c = http_server(data=payload(999_999, seed=5))
    manager = new_manager(tmp_path)

    async def scenario():
        i = manager.add(url_a, connections=2)
        await asyncio.sleep(0.2)
        manager.pause(i)
        await wait_state(manager, i, "paused")
        with pytest.raises(SizeMismatch):
            await manager.set_url(i, url_c)
        cfg_b.bytes_sent = 0
        await manager.set_url(i, url_b)
        await wait_state(manager, i, "done")
        return i

    i = run(manager, scenario)
    assert manager.store.get(i).dest.read_bytes() == data
    assert cfg_b.bytes_sent < len(data)
    assert manager.store.get(i).url == url_b


def test_expired_link_can_be_replaced(tmp_path, http_server):
    data = payload(100_000)
    cfg_a, url_a = http_server(data=data, status=403)
    cfg_b, url_b = http_server(data=data)
    manager = new_manager(tmp_path)

    async def scenario():
        i = manager.add(url_a)
        assert await wait_state(manager, i, FINAL) == "expired"
        await manager.set_url(i, url_b)
        await wait_state(manager, i, "done")
        return i

    i = run(manager, scenario)
    assert manager.store.get(i).dest.read_bytes() == data


def test_restart_recovery(tmp_path, http_server):
    data = payload(1_000_000)
    cfg, url = http_server(data=data, delay=0.01)
    manager = new_manager(tmp_path)

    async def first_run():
        i = manager.add(url, connections=2)
        await asyncio.sleep(0.2)
        return i

    i = run(manager, first_run)  # shutdown mid-download
    assert manager.store.get(i).state == "queued"
    assert sum(p.done for p in manager.store.load_parts(i)) > 0

    cfg.delay, cfg.bytes_sent = 0, 0
    second = new_manager(tmp_path)
    run(second, lambda: wait_state(second, i, "done"))
    assert second.store.get(i).dest.read_bytes() == data
    assert cfg.bytes_sent < len(data)


def test_crashed_running_state_is_requeued(tmp_path, http_server):
    data = payload(50_000)
    cfg, url = http_server(data=data)
    manager = new_manager(tmp_path)
    i = manager.store.add(url, tmp_path / "dl")
    manager.store.set_state(i, "running")  # as if the daemon died mid-download
    run(manager, lambda: wait_state(manager, i, "done"))
    assert manager.store.get(i).dest.read_bytes() == data


def test_probe_reports_size_resume_and_name(tmp_path, http_server):
    cfg, url = http_server(data=payload(12_345), disposition='attachment; filename="clip.mkv"')
    manager = new_manager(tmp_path)
    info = asyncio.run(manager.probe(url, {"Referer": "https://site/"}))
    assert {k: info[k] for k in ("size", "resumable", "filename")} == {"size": 12_345, "resumable": True,
                                                                         "filename": "clip.mkv"}
    assert cfg.requests[-1]["Referer"] == "https://site/"
