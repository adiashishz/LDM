"""Export/import of downloads (1DM: export/import unfinished downloads) and duplicate links."""

import asyncio
import json

import pytest

from ldm.manager import InvalidArgument, Manager
from ldm.store import Store

from test_manager import payload, run, wait_state


def new_manager(path, download_dir) -> Manager:
    store = Store(path)
    store.set_setting("download_dir", str(download_dir))
    return Manager(store, progress_interval=0.05,
                   downloader_options={"min_part": 64 * 1024, "persist_interval": 0.05})


def test_unfinished_download_moves_to_a_new_database_and_resumes(tmp_path, http_server):
    data = payload(3_000_000)
    cfg, url = http_server(data=data, delay=0.01)
    first = new_manager(tmp_path / "one.db", tmp_path / "dl")

    async def partly():
        i = first.add(url, connections=2)
        await wait_state(first, i, "running")
        await asyncio.sleep(0.5)
        first.pause(i)
        await wait_state(first, i, "paused")
        done = first.add("http://127.0.0.1:9/finished.bin", start=False)
        first.store.finish(done, "finished.bin", 10)
        return i

    run(first, partly)
    exported = first.export_downloads()
    assert [d["url"] for d in json.loads(exported)["downloads"]] == [url]  # finished ones left out
    assert len(json.loads(first.export_downloads(unfinished_only=False))["downloads"]) == 2
    sent_before = cfg.bytes_sent

    second = new_manager(tmp_path / "two.db", tmp_path / "other")
    second.store.add("http://127.0.0.1:9/existing", tmp_path)  # ids differ from the old database

    async def resume():
        (i,) = second.import_downloads(exported)
        assert second.store.get(i).state == "paused"
        second.resume(i)
        assert await wait_state(second, i, "done") == "done"
        return i

    i = run(second, resume)
    assert second.store.get(i).dest.read_bytes() == data
    assert cfg.bytes_sent - sent_before < len(data) * 0.9  # continued, didn't start over


def test_import_without_partial_data_starts_over(tmp_path, http_server):
    data = payload(200_000)
    cfg, url = http_server(data=data)
    exported = json.dumps({"format": "ldm-downloads", "version": 1, "downloads": [{
        "id": 7, "url": url, "final_url": url, "kind": "http", "state": "paused", "dir": str(tmp_path / "dl"),
        "name": "test.bin", "size": len(data), "resumable": True, "connections": 2, "headers": {},
        "parts": [[0, 99_999, 50_000], [100_000, 199_999, 0]]}]})
    manager = new_manager(tmp_path / "s.db", tmp_path / "dl")

    async def scenario():
        (i,) = manager.import_downloads(exported)
        manager.resume(i)
        return i, await wait_state(manager, i, {"done", "error"})

    i, state = run(manager, scenario)
    assert state == "done" and manager.store.get(i).dest.read_bytes() == data


@pytest.mark.parametrize("text", ["not json", '{"format": "other"}', '{"format": "ldm-downloads", "version": 99, "downloads": []}'])
def test_bad_import_files(tmp_path, text):
    with pytest.raises(InvalidArgument):
        new_manager(tmp_path / "s.db", tmp_path).import_downloads(text)


def test_find_duplicates(tmp_path):
    manager = new_manager(tmp_path / "s.db", tmp_path)
    i = manager.add("https://example.com/file.iso", start=False)
    manager.store.update(i, final_url="https://cdn.example.com/x/file.iso")
    manager.add("https://example.com/other.iso", start=False)
    assert [d["id"] for d in manager.find_duplicates("https://example.com/file.iso#top")] == [i]
    assert [d["id"] for d in manager.find_duplicates("https://cdn.example.com/x/file.iso")] == [i]
    assert manager.find_duplicates("https://example.com/new.iso") == []
