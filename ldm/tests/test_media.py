"""Media downloads through yt-dlp, against HLS/DASH streams generated locally with FFmpeg."""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ldm.manager import Manager
from ldm.media import MediaDownloader, build_choices, find_js_runtime
from ldm.store import Store

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")

SOURCES = ["-f", "lavfi", "-i", "testsrc=size={w}x{h}:rate=25", "-f", "lavfi", "-i", "sine=frequency=440",
           "-t", "4"]
VIDEO = ["-c:v", "libopenh264", "-b:v", "200k", "-c:a", "aac", "-b:a", "64k"]


def ffmpeg(*args, cwd):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], cwd=cwd, check=True)


def sources(w, h):
    return [a.format(w=w, h=h) for a in SOURCES]


@pytest.fixture(scope="session")
def streams(tmp_path_factory):
    """path -> bytes for: /hls/master.m3u8 (240p + 480p), /aes/index.m3u8, /dash/manifest.mpd."""
    root = tmp_path_factory.mktemp("streams")
    for name, (w, h) in {"low": (320, 240), "high": (640, 480)}.items():
        (root / "hls" / name).mkdir(parents=True)
        ffmpeg(*sources(w, h), *VIDEO, "-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod",
               "index.m3u8", cwd=root / "hls" / name)
    (root / "hls" / "master.m3u8").write_text(
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=320x240,CODECS="avc1.42c00d,mp4a.40.2"\nlow/index.m3u8\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=600000,RESOLUTION=640x480,CODECS="avc1.42c01e,mp4a.40.2"\nhigh/index.m3u8\n')

    aes = root / "aes"
    aes.mkdir()
    (aes / "key.bin").write_bytes(os.urandom(16))
    (aes / "keyinfo").write_text(f"key.bin\n{aes / 'key.bin'}\n")
    ffmpeg(*sources(320, 240), *VIDEO, "-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod",
           "-hls_key_info_file", "keyinfo", "index.m3u8", cwd=aes)
    (aes / "keyinfo").unlink()

    dash = root / "dash"
    dash.mkdir()
    ffmpeg(*sources(320, 240), "-map", "0:v", "-map", "1:a", *VIDEO, "-f", "dash", "-seg_duration", "1",
           "-adaptation_sets", "id=0,streams=v id=1,streams=a", "manifest.mpd", cwd=dash)
    return {"/" + str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,height:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    return {"duration": float(data["format"]["duration"]),
            "streams": sorted(s["codec_type"] for s in data["streams"]),
            "height": next((s["height"] for s in data["streams"] if s["codec_type"] == "video"), None)}


def new_store(tmp_path):
    store = Store(tmp_path / "state.db")
    store.set_setting("download_dir", str(tmp_path / "out"))
    return store


def run_download(store, download_id, **kwargs):
    return asyncio.run(MediaDownloader(store, download_id, **kwargs).run())


def test_hls_quality_choice_and_download(tmp_path, http_server, streams):
    cfg, _ = http_server(data=b"", files=streams)
    store = new_store(tmp_path)
    url = f"{cfg.base}/hls/master.m3u8"
    info = asyncio.run(Manager(store).probe_media(url, {}))
    assert [c["id"] for c in info["choices"]] == ["best", "480", "240"]

    low = next(c for c in info["choices"] if c["id"] == "240")
    did = store.add(url, tmp_path / "out", kind="media", name="clip",
                    headers={"Referer": "https://site/page"},
                    options={"format": low["selector"], "custom_name": True})
    path = run_download(store, did)
    result = probe(path)
    assert path.name == "clip.mp4"
    assert result["height"] == 240 and result["streams"] == ["audio", "video"]
    assert 3.5 < result["duration"] < 4.6
    assert store.get(did).state == "done"
    assert any(r.get("Referer") == "https://site/page" for r in cfg.requests if True)


def test_aes128_hls_is_decrypted(tmp_path, http_server, streams):
    cfg, _ = http_server(data=b"", files=streams)
    store = new_store(tmp_path)
    did = store.add(f"{cfg.base}/aes/index.m3u8", tmp_path / "out", kind="media")
    assert b"METHOD=AES-128" in streams["/aes/index.m3u8"]
    result = probe(run_download(store, did))
    assert result["streams"] == ["audio", "video"] and 3.5 < result["duration"] < 4.6


def test_dash_separate_audio_and_video_are_merged(tmp_path, http_server, streams):
    cfg, _ = http_server(data=b"", files=streams)
    store = new_store(tmp_path)
    states = []
    did = store.add(f"{cfg.base}/dash/manifest.mpd", tmp_path / "out", kind="media")
    path = run_download(store, did, on_state=states.append)
    assert probe(path)["streams"] == ["audio", "video"]
    assert "processing" in states  # FFmpeg merge reported


def test_pause_and_resume_media(tmp_path, http_server, streams):
    cfg, _ = http_server(data=b"", files=streams, delay=0.1)
    store = new_store(tmp_path)
    did = store.add(f"{cfg.base}/hls/high/index.m3u8", tmp_path / "out", kind="media", connections=1)

    async def pause_midway():
        task = asyncio.create_task(MediaDownloader(store, did).run())
        await asyncio.sleep(0.6)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(pause_midway())
    assert store.get(did).state == "paused"
    cfg.delay = 0
    assert probe(run_download(store, did))["streams"] == ["audio", "video"]


def test_manager_routes_playlists_to_media(tmp_path, http_server, streams):
    cfg, _ = http_server(data=b"", files=streams)
    manager = Manager(new_store(tmp_path), progress_interval=0.05)
    progress = []
    manager.on_progress_batch = progress.extend

    async def go():
        await manager.start()
        try:
            did = manager.add(f"{cfg.base}/hls/low/index.m3u8", title="Test clip")
            assert manager.store.get(did).kind == "media"
            for _ in range(300):
                if manager.store.get(did).state in ("done", "error"):
                    break
                await asyncio.sleep(0.05)
            return did
        finally:
            await manager.shutdown()

    did = asyncio.run(go())
    d = manager.store.get(did)
    assert d.state == "done", d.error
    assert d.name.startswith("Test clip") is False or d.name.endswith(".mp4")
    assert (d.dir / d.name).exists()


def test_build_choices_estimates_sizes():
    formats = [{"format_id": "v1", "height": 1080, "vcodec": "avc1", "acodec": "none", "tbr": 4000, "filesize": 100},
               {"format_id": "v2", "height": 720, "vcodec": "avc1", "acodec": "none", "tbr": 2000, "filesize": 60},
               {"format_id": "a", "vcodec": "none", "acodec": "opus", "abr": 128, "filesize": 10}]
    choices = build_choices(formats)
    assert [(c["id"], c["size"]) for c in choices] == [("best", 110), ("1080", 110), ("720", 70), ("audio", 10)]


def test_js_runtime_found_outside_path(tmp_path, monkeypatch):
    node = tmp_path / ".nvm/versions/node/v22.1.0/bin/node"
    node.parent.mkdir(parents=True)
    node.write_text("#!/bin/sh\n")
    node.chmod(0o755)
    bun = tmp_path / ".bun/bin/bun"  # yt-dlp lags behind new bun releases: node is preferred
    bun.parent.mkdir(parents=True)
    bun.write_text("#!/bin/sh\n")
    bun.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/nonexistent")
    assert find_js_runtime() == ("node", str(node))


def test_partial_files_stay_in_a_private_folder(tmp_path, http_server, streams):
    """Pausing leaves resume files only in .ldm-media-<id>/, never next to other downloads."""
    cfg, _ = http_server(data=b"", files=streams, delay=0.1)
    store = new_store(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "someone-else.part").write_bytes(b"keep me")
    did = store.add(f"{cfg.base}/hls/high/index.m3u8", out, kind="media", connections=1)

    async def pause_midway():
        task = asyncio.create_task(MediaDownloader(store, did).run())
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(pause_midway())
    assert sorted(p.name for p in out.iterdir()) == [f".ldm-media-{did}", "someone-else.part"]
    cfg.delay = 0
    path = run_download(store, did)
    assert sorted(p.name for p in out.iterdir()) == sorted([path.name, "someone-else.part"])
    assert (out / "someone-else.part").read_bytes() == b"keep me"


def test_choices_use_youtube_quality_names_and_bitrate_estimates():
    """Widescreen video: pixel height 1012 is still called 1080p; size from bitrate when missing."""
    formats = [{"height": 1012, "format_note": "1080p", "vcodec": "vp9", "acodec": "none", "tbr": 800},
               {"height": 676, "format_note": "720p60", "vcodec": "vp9", "acodec": "none", "tbr": 400, "fps": 60},
               {"vcodec": "none", "acodec": "opus", "abr": 128, "tbr": 128}]
    choices = build_choices(formats, duration=100)
    assert [(c["id"], c["label"], c["size"]) for c in choices] == [
        ("best", "Best quality", 11_600_000), ("1080", "1080p", 11_600_000), ("720", "720p 60fps", 6_600_000),
        ("audio", "Audio only", 1_600_000)]
    assert choices[1]["selector"] == "bv*[height<=1012]+ba/b[height<=1012]/b"


def test_quality_names_from_dimensions():
    from ldm.media import _quality_name
    assert _quality_name({"height": 2026, "width": 3840}) == "2160p"
    assert _quality_name({"height": 1012, "width": 1920}) == "1080p"
    assert _quality_name({"height": 1920, "width": 1080}) == "1080p"   # vertical video
    assert _quality_name({"height": 240, "width": 320}) == "240p"
    assert _quality_name({"height": 1000}) == "1080p"


def test_encrypted_hls_with_a_pasted_key(tmp_path, http_server, streams):
    """1DM's "m3u8 key base64": the playlist's key URL can't be fetched, the user supplies the key."""
    import base64

    from ldm.hlskey import aes_hex
    from ldm.manager import InvalidArgument

    key = streams["/aes/key.bin"]
    hidden = {path: data for path, data in streams.items() if path != "/aes/key.bin"}  # key URL: 404
    cfg, _ = http_server(data=b"", files=hidden)
    store = new_store(tmp_path)
    url = f"{cfg.base}/aes/index.m3u8"
    without = store.add(url, tmp_path / "a", kind="media")
    with pytest.raises(Exception):
        run_download(store, without)

    manager = Manager(store)
    with pytest.raises(InvalidArgument, match="16 bytes"):
        manager.add(url, kind="media", hls_key="too-short", start=False)
    did = manager.add(url, kind="media", hls_key=base64.b64encode(key).decode(), start=False)
    assert store.get(did).options["hls_key"] == key.hex()
    result = probe(run_download(store, did))
    assert result["streams"] == ["audio", "video"] and 3.5 < result["duration"] < 4.6
    assert aes_hex("0x" + key.hex().upper()) == key.hex()
