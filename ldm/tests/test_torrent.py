"""Torrents against a local swarm: a libtorrent seeder on 127.0.0.1, no internet."""

import asyncio
import os
import time
from pathlib import Path

import pytest

lt = pytest.importorskip("libtorrent")

from ldm.manager import Manager  # noqa: E402
from ldm.store import Store  # noqa: E402

OFFLINE = {"torrent_dht": False, "torrent_lsd": False, "torrent_upnp": False}


class Swarm:
    """Two files in a folder, a .torrent for them and a seeding session."""

    def __init__(self, root: Path, upload_limit: int = 0):
        self.src = root / "seed" / "My Files"
        self.src.mkdir(parents=True)
        (self.src / "a.bin").write_bytes(os.urandom(3_000_000))
        (self.src / "b.bin").write_bytes(os.urandom(1_000_000))
        fs = lt.file_storage()
        lt.add_files(fs, str(self.src))
        ct = lt.create_torrent(fs, 64 * 1024)
        lt.set_piece_hashes(ct, str(self.src.parent))
        self.data = lt.bencode(ct.generate())
        self.info = lt.torrent_info(lt.bdecode(self.data))
        self.torrent_file = root / "files.torrent"
        self.torrent_file.write_bytes(self.data)
        self.session = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
                                   "enable_upnp": False, "enable_natpmp": False,
                                   "upload_rate_limit": upload_limit,
                                   "ignore_limits_on_local_network": False})  # else 127.0.0.1 is unlimited
        params = lt.add_torrent_params()
        params.ti = self.info
        params.save_path = str(self.src.parent)
        params.flags |= lt.torrent_flags.seed_mode
        self.session.add_torrent(params)
        self.peer = f"127.0.0.1:{self.session.listen_port()}"

    @property
    def magnet(self) -> str:
        return lt.make_magnet_uri(self.info) + f"&x.pe={self.peer}"

    def same(self, folder: Path, name: str) -> bool:
        return (folder / name).read_bytes() == (self.src / name).read_bytes()


@pytest.fixture
def swarm(tmp_path):
    return Swarm(tmp_path)


def new_manager(tmp_path, **settings) -> Manager:
    store = Store(tmp_path / "state.db")
    store.set_setting("download_dir", str(tmp_path / "out"))
    for key, value in {**OFFLINE, **settings}.items():
        store.set_setting(key, value)
    return Manager(store, progress_interval=0.05, torrent_listen_host="127.0.0.1")


async def wait_state(manager, download_id, states, timeout=30):
    states = {states} if isinstance(states, str) else set(states)
    deadline = time.monotonic() + timeout
    while manager.store.get(download_id).state not in states:
        assert time.monotonic() < deadline, f"stuck in {manager.store.get(download_id).state}"
        await asyncio.sleep(0.05)
    return manager.store.get(download_id).state


def run(manager, scenario):
    async def go():
        await manager.start()
        try:
            return await scenario()
        finally:
            await manager.shutdown()
    return asyncio.run(go())


def test_torrent_file_download(tmp_path, swarm):
    manager = new_manager(tmp_path)

    async def scenario():
        did = manager.add(swarm.torrent_file.as_uri(), peers=[swarm.peer])
        assert manager.store.get(did).kind == "torrent"
        assert await wait_state(manager, did, ("done", "error")) == "done", manager.store.get(did).error
        return did

    did = run(manager, scenario)
    d = manager.store.get(did)
    out = d.dir / d.name
    assert d.name == "My Files" and swarm.same(out, "a.bin") and swarm.same(out, "b.bin")
    assert d.size == 4_000_000


def test_file_selection(tmp_path, swarm):
    """Hybrid torrents contain hidden padding files: priorities follow the real file indices."""
    manager = new_manager(tmp_path)

    async def scenario():
        info = await manager.probe_torrent(swarm.torrent_file.as_uri())
        assert [Path(f["path"]).name for f in info["files"]] == ["a.bin", "b.bin"]
        assert info["num_files"] > 2 and info["size"] == 4_000_000  # padding exists but isn't listed
        priorities = [0] * info["num_files"]
        priorities[next(f["index"] for f in info["files"] if f["path"].endswith("a.bin"))] = 1
        did = manager.add(swarm.torrent_file.as_uri(), peers=[swarm.peer], priorities=priorities)
        await wait_state(manager, did, "done")
        return did

    d = manager.store.get(run(manager, scenario))
    out = d.dir / d.name
    assert swarm.same(out, "a.bin")
    assert d.size == 3_000_000
    assert not (out / "b.bin").exists() or not swarm.same(out, "b.bin")


def test_magnet_gets_metadata_from_peers(tmp_path, swarm):
    manager = new_manager(tmp_path)

    async def scenario():
        info = await manager.probe_torrent(swarm.magnet)
        assert (info["name"], info["size"], len(info["files"])) == ("My Files", 4_000_000, 2)
        did = manager.add(swarm.magnet)
        assert manager.store.get(did).kind == "torrent"
        await wait_state(manager, did, "done")
        return did

    d = manager.store.get(run(manager, scenario))
    assert swarm.same(d.dir / d.name, "a.bin")
    # ("processing", "Getting torrent info") shows while peers send the file list; a local seeder
    # answers too fast for it to be observed reliably, so it isn't asserted here.


def test_pause_and_resume(tmp_path):
    swarm = Swarm(tmp_path, upload_limit=1_000_000)
    manager = new_manager(tmp_path)

    async def scenario():
        did = manager.add(swarm.torrent_file.as_uri(), peers=[swarm.peer])
        await wait_state(manager, did, ("running", "done"))
        await asyncio.sleep(1.0)
        assert manager.store.get(did).state == "running", "download finished before it could be paused"
        manager.pause(did)
        await wait_state(manager, did, "paused")
        assert manager.store.get_torrent(did).resume_data
        swarm.session.apply_settings({"upload_rate_limit": 0})
        manager.resume(did)
        await wait_state(manager, did, "done")
        return did

    d = manager.store.get(run(manager, scenario))
    assert swarm.same(d.dir / d.name, "a.bin") and swarm.same(d.dir / d.name, "b.bin")


def test_seeding_then_stop(tmp_path, swarm):
    manager = new_manager(tmp_path, torrent_seed=True, torrent_seed_ratio=0.0)

    async def scenario():
        did = manager.add(swarm.torrent_file.as_uri(), peers=[swarm.peer])
        await wait_state(manager, did, "seeding")
        assert manager.torrent_details(did)["seeding"] is True
        assert manager.running == set()  # seeding doesn't take a download slot
        manager.pause(did)
        assert manager.store.get(did).state == "done"
        return did

    run(manager, scenario)


def test_seeding_survives_restart(tmp_path, swarm):
    manager = new_manager(tmp_path, torrent_seed=True, torrent_seed_ratio=0.0)

    async def first():
        did = manager.add(swarm.torrent_file.as_uri(), peers=[swarm.peer])
        await wait_state(manager, did, "seeding")
        return did

    did = run(manager, first)
    assert manager.store.get(did).state == "seeding"
    second = new_manager(tmp_path, torrent_seed=True, torrent_seed_ratio=0.0)

    async def again():
        await asyncio.sleep(0.2)
        return second.torrent_details(did)["seeding"]

    assert run(second, again) is True


def test_probe_torrent_url_and_details(tmp_path, swarm, http_server):
    cfg, _ = http_server(data=b"", files={"/files.torrent": swarm.data})
    manager = new_manager(tmp_path)

    async def scenario():
        info = await manager.probe_torrent(f"{cfg.base}/files.torrent", {"Referer": "https://site/"})
        did = manager.add(f"{cfg.base}/files.torrent", start=False, metadata=info["metadata"])
        return info, manager.torrent_details(did)

    info, details = run(manager, scenario)
    assert sorted(Path(f["path"]).name for f in info["files"]) == ["a.bin", "b.bin"]
    assert info["metadata"] == swarm.data and not info["is_magnet"]
    assert cfg.requests[-1]["Referer"] == "https://site/"
    assert sorted(Path(f["path"]).name for f in details["files"]) == ["a.bin", "b.bin"]


def test_invalid_torrent_file(tmp_path):
    bad = tmp_path / "bad.torrent"
    bad.write_bytes(b"not bencoded")
    manager = new_manager(tmp_path)
    with pytest.raises(Exception, match="not a valid .torrent"):
        asyncio.run(manager.probe_torrent(bad.as_uri()))


def test_failed_add_leaves_no_row(tmp_path):
    """Regression: a torrent whose options can't be stored must not stay queued."""
    manager = new_manager(tmp_path)
    with pytest.raises(Exception):
        manager.add("https://example.org/x.torrent", metadata={"not": "bytes"})
    assert manager.store.list() == []
