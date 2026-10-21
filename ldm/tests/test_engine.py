import asyncio
import random
import time

import pytest

from ldm.engine import Downloader, LinkExpired, RemoteChanged
from ldm.ratelimit import TokenBucket
from ldm.store import Store

KIB = 1024


def payload(size: int, seed: int = 1) -> bytes:
    return random.Random(seed).randbytes(size)


def new_download(tmp_path, url, connections=4, **kwargs):
    store = Store(tmp_path / "state.db")
    return store, store.add(url, tmp_path / "dl", connections=connections, **kwargs)


def downloader(store, download_id, **kwargs):
    kwargs.setdefault("min_part", 64 * KIB)
    kwargs.setdefault("retry_base_delay", 0.01)
    return Downloader(store, download_id, **kwargs)


def download(store, download_id, **kwargs):
    return asyncio.run(downloader(store, download_id, **kwargs).run())


def pause_after(store, download_id, seconds):
    async def go():
        task = asyncio.create_task(downloader(store, download_id, persist_interval=0.05).run())
        await asyncio.sleep(seconds)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())


def test_multipart_download(tmp_path, http_server):
    data = payload(3_000_000)
    cfg, url = http_server(data=data)
    store, did = new_download(tmp_path, url)
    path = download(store, did)
    assert path.read_bytes() == data
    assert path.name == "test.bin"
    d = store.get(did)
    assert (d.state, d.size, d.resumable) == ("done", len(data), True)
    assert sum("Range" in r for r in cfg.requests) >= 5  # probe + 4 parts


def test_server_without_ranges_uses_one_connection(tmp_path, http_server):
    data = payload(500_000)
    cfg, url = http_server(data=data, ranges=False)
    store, did = new_download(tmp_path, url)
    assert download(store, did).read_bytes() == data
    assert not store.get(did).resumable
    assert len(store.load_parts(did)) == 1


def test_unknown_size(tmp_path, http_server):
    data = payload(300_000)
    cfg, url = http_server(data=data, ranges=False, send_length=False)
    store, did = new_download(tmp_path, url)
    assert download(store, did).read_bytes() == data
    assert store.get(did).size == len(data)


def test_empty_file(tmp_path, http_server):
    cfg, url = http_server(data=b"")
    store, did = new_download(tmp_path, url)
    assert download(store, did).read_bytes() == b""


def test_pause_and_resume_continues_where_it_stopped(tmp_path, http_server):
    data = payload(2_000_000)
    cfg, url = http_server(data=data, delay=0.01)
    store, did = new_download(tmp_path, url)
    pause_after(store, did, 0.15)

    assert store.get(did).state == "paused"
    saved = sum(p.done for p in store.load_parts(did))
    assert 0 < saved < len(data)

    cfg.delay, cfg.bytes_sent = 0.0, 0
    assert download(store, did).read_bytes() == data
    assert cfg.bytes_sent < len(data)  # did not fetch everything again


def test_dropped_connections_are_retried(tmp_path, http_server):
    data = payload(1_000_000)
    cfg, url = http_server(data=data, drops=3, drop_after=100_000)
    store, did = new_download(tmp_path, url)
    assert download(store, did).read_bytes() == data
    assert cfg.drops == 0


def test_idle_connections_split_slow_parts(tmp_path, http_server):
    data = payload(2_000_000)
    # 4 parts of 500 kB; the last one is served slowly, so finished connections steal from it
    cfg, url = http_server(data=data, slow_from=1_500_000, slow_delay=0.02)
    store, did = new_download(tmp_path, url)
    assert download(store, did).read_bytes() == data
    assert len(store.load_parts(did)) > 4


def test_changed_remote_file_is_detected_on_resume(tmp_path, http_server):
    data = payload(2_000_000)
    cfg, url = http_server(data=data, delay=0.01)
    store, did = new_download(tmp_path, url)
    pause_after(store, did, 0.15)

    cfg.data, cfg.etag, cfg.delay = payload(2_000_000, seed=2), '"v2"', 0.0
    with pytest.raises(RemoteChanged):
        download(store, did)
    assert store.get(did).state == "changed"


def test_refused_link_is_reported_as_expired(tmp_path, http_server):
    cfg, url = http_server(data=payload(1000), status=403)
    store, did = new_download(tmp_path, url)
    with pytest.raises(LinkExpired):
        download(store, did)
    assert store.get(did).state == "expired"


def test_content_disposition_name_and_no_overwrite(tmp_path, http_server):
    data = payload(10_000)
    cfg, url = http_server(data=data, disposition='attachment; filename="report.pdf"')
    (tmp_path / "dl").mkdir()
    (tmp_path / "dl" / "report.pdf").write_bytes(b"existing")
    store, did = new_download(tmp_path, url)
    path = download(store, did)
    assert path.name == "report (1).pdf"
    assert (tmp_path / "dl" / "report.pdf").read_bytes() == b"existing"


def test_speed_limit(tmp_path, http_server):
    data = payload(1_000_000)
    cfg, url = http_server(data=data)
    store, did = new_download(tmp_path, url)
    start = time.monotonic()
    path = download(store, did, limiters=[TokenBucket(2_000_000)])
    assert time.monotonic() - start >= 0.4
    assert path.read_bytes() == data


def test_connections_cap(tmp_path, http_server):
    data = payload(1_000_000)
    cfg, url = http_server(data=data, delay=0.002)
    store, did = new_download(tmp_path, url, connections=8)
    assert download(store, did, connections=2).read_bytes() == data
    assert cfg.max_active_ranges <= 2


def test_expired_redirect_target_is_resolved_again(tmp_path, http_server):
    data = payload(2_000_000)
    cfg, _ = http_server(data=data, delay=0.01)
    cfg.redirects["/start"] = "/files/test.bin?token=1"
    store, did = new_download(tmp_path, f"{cfg.base}/start")
    pause_after(store, did, 0.15)
    assert store.get(did).final_url.endswith("token=1")

    cfg.delay = 0.0
    cfg.deny.add("/files/test.bin?token=1")  # signed link expired
    cfg.redirects["/start"] = "/files/test.bin?token=2"
    assert download(store, did).read_bytes() == data
    assert store.get(did).final_url.endswith("token=2")
