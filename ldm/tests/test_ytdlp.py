"""The per-user yt-dlp: fetching a release, checking it, and importing it instead of the system's."""

import asyncio
import hashlib
import io
import os
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ldm import media, ytdlp
from ldm.manager import Manager
from ldm.engine import DownloadError
from ldm.store import Store


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    """The user copy lives under XDG_DATA_HOME: keep it in the test's folder."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path / "data"


def bundle(tmp_path, version: str) -> Path:
    """A distribution package's copy, in <data dir>/ldm/yt-dlp/."""
    target = tmp_path / "system" / "ldm" / "yt-dlp" / "yt-dlp"
    target.parent.mkdir(parents=True)
    target.write_bytes(zipapp(version, marker="FROM_BUNDLED_COPY"))
    return target


def zipapp(version: str, marker: str = "FROM_USER_COPY") -> bytes:
    """A stand-in for yt-dlp's zipimport build: shebang line, then a zip with the yt_dlp package."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("yt_dlp/__init__.py", f"{marker} = True\n")
        z.writestr("yt_dlp/version.py", f"__version__ = '{version}'\n")
        z.writestr("__main__.py", "")
    return b"#!/usr/bin/env python3\n" + buffer.getvalue()


@pytest.fixture
def releases():
    """A local stand-in for github.com/yt-dlp/yt-dlp/releases; `state` changes what it serves."""
    state = {"latest": "2099.1.2", "body": zipapp("2099.1.2"), "sums": None, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["requests"].append(self.path)
            latest = state["latest"]
            if self.path == "/releases/latest":
                self.send_response(302)
                self.send_header("Location", f"https://github.com/yt-dlp/yt-dlp/releases/tag/{latest}")
                self.end_headers()
                return
            body = {f"/releases/download/{latest}/yt-dlp": state["body"],
                    f"/releases/download/{latest}/SHA2-256SUMS": (state["sums"] or (
                        f"{hashlib.sha256(state['body']).hexdigest()}  yt-dlp\n"
                        "0000  yt-dlp.exe\n").encode())}.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_port}/releases"
    yield state
    server.shutdown()
    server.server_close()


def update(releases, current):
    import httpx

    async def go():
        async with httpx.AsyncClient() as client:
            return await ytdlp.update(client, current, releases["url"])
    return asyncio.run(go())


def test_version_order():
    assert ytdlp.version_key("2026.10.26") > ytdlp.version_key("2026.9.30") > ytdlp.version_key(None)
    assert ytdlp.version_key("2026.10.26.1") > ytdlp.version_key("2026.10.26")


def test_update_downloads_checks_and_installs(releases):
    assert update(releases, "2026.10.26") == "2099.1.2"
    assert ytdlp.user_copy().read_bytes() == releases["body"]
    assert ytdlp.user_version() == "2099.1.2"
    assert ytdlp.user_copy().is_relative_to(Path(os.environ["XDG_DATA_HOME"]))
    releases["requests"].clear()
    assert update(releases, "2099.1.2") is None  # up to date: only the redirect is fetched
    assert releases["requests"] == ["/releases/latest"]


def test_bad_download_keeps_the_old_copy(releases):
    update(releases, "2026.10.26")
    releases.update(latest="2099.2.1", body=zipapp("2099.2.1"), sums=b"deadbeef  yt-dlp\n")
    with pytest.raises(ValueError, match="checksum"):
        update(releases, "2099.1.2")
    releases.update(body=b"<html>not a zip</html>", sums=None)
    with pytest.raises(ValueError, match="not a zipimport build"):
        update(releases, "2099.1.2")
    assert ytdlp.user_version() == "2099.1.2" and not ytdlp.user_copy().with_name("yt-dlp.part").exists()


def run_python(code: str, data_dirs: Path | str = "/nonexistent") -> str:
    """Run code in a fresh interpreter. XDG_DATA_DIRS only there: a bundled copy, or none (GTK in this
    process needs the real one)."""
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                          env={**os.environ, "XDG_DATA_DIRS": str(data_dirs)}).stdout.strip()


ACTIVATE = ("from ldm import ytdlp; v = ytdlp.activate(); import yt_dlp; "
            "print(v, getattr(yt_dlp, 'FROM_USER_COPY', False))")
WHICH = ("from ldm import ytdlp; v = ytdlp.activate(); import yt_dlp; "
         "print(v, 'user' if hasattr(yt_dlp, 'FROM_USER_COPY') else 'bundled' if hasattr(yt_dlp, 'FROM_BUNDLED_COPY') "
         "else 'installed')")


def test_newer_user_copy_is_imported_first():
    target = ytdlp.user_copy()
    target.parent.mkdir(parents=True)
    target.write_bytes(zipapp("2099.1.2"))
    assert run_python(ACTIVATE) == "2099.1.2 True"


def test_older_user_copy_is_ignored():
    if ytdlp.installed_version() is None:
        pytest.skip("needs an installed yt-dlp to compare with")
    target = ytdlp.user_copy()
    target.parent.mkdir(parents=True)
    target.write_bytes(zipapp("2001.1.1"))  # the distribution caught up
    assert run_python(ACTIVATE) == "None False"


def test_bundled_copy_is_found_in_xdg_data_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "system"))
    assert ytdlp.bundled_copy() is None
    path = bundle(tmp_path, "2099.1.2")
    monkeypatch.setenv("XDG_DATA_DIRS", f"{tmp_path / 'elsewhere'}::{tmp_path / 'system'}")
    assert ytdlp.bundled_copy() == path


def test_newer_bundled_copy_is_imported_first(tmp_path):
    bundle(tmp_path, "2099.1.2")
    assert run_python(WHICH, tmp_path / "system") == "2099.1.2 bundled"


def test_newest_of_bundled_and_user_copy_wins(tmp_path):
    bundle(tmp_path, "2099.1.2")
    ytdlp.user_copy().parent.mkdir(parents=True)
    ytdlp.user_copy().write_bytes(zipapp("2099.3.4"))  # updated since the package was built
    assert run_python(WHICH, tmp_path / "system") == "2099.3.4 user"
    ytdlp.user_copy().write_bytes(zipapp("2099.1.1"))  # a newer package came after the last update
    assert run_python(WHICH, tmp_path / "system") == "2099.1.2 bundled"


def test_older_bundled_copy_is_ignored(tmp_path):
    if ytdlp.installed_version() is None:
        pytest.skip("needs an installed yt-dlp to compare with")
    bundle(tmp_path, "2001.1.1")
    assert run_python(WHICH, tmp_path / "system") == "None installed"


def test_manager_updates_and_restarts_when_idle(tmp_path, releases):
    manager = Manager(Store(tmp_path / "state.db"), ytdlp_releases=releases["url"])
    restarts = []
    manager.on_restart = lambda: restarts.append(True)

    async def go():
        await manager.start()
        try:
            version = await manager.update_ytdlp()
            await asyncio.sleep(1.2)  # the restart waits a second for the D-Bus reply
            return version, manager.settings()
        finally:
            await manager.shutdown()

    version, settings = asyncio.run(go())
    assert version == "2099.1.2" and restarts == [True]
    assert settings["ytdlp_version"] == media.YTDLP_VERSION  # until the restart
    assert settings["ytdlp_pending"] == "2099.1.2"


def test_manager_reports_unreachable_releases(tmp_path):
    manager = Manager(Store(tmp_path / "state.db"), ytdlp_releases="http://127.0.0.1:9/releases")

    async def go():
        try:
            await manager.update_ytdlp()
        finally:
            await manager.shutdown()

    with pytest.raises(DownloadError, match="yt-dlp"):
        asyncio.run(go())


def test_daily_check_respects_the_setting(tmp_path, monkeypatch):
    calls = []
    manager = Manager(Store(tmp_path / "state.db"))

    async def fake_update():
        calls.append(True)
        return ""

    monkeypatch.setattr(manager, "update_ytdlp", fake_update)
    manager._check_ytdlp()  # just started: not yet
    manager._ytdlp_not_before = 0

    async def go():
        manager._check_ytdlp()
        manager._check_ytdlp()  # within a day: nothing
        manager.store.set_setting("ytdlp_checked", 0.0)
        manager.store.set_setting("ytdlp_auto_update", False)
        manager._check_ytdlp()
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert calls == [True]
