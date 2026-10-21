import base64
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from ldm import integration

EXTENSION = Path(__file__).resolve().parents[2] / "extension"


@pytest.fixture
def home(tmp_path, monkeypatch):
    # Never touch the real profile: data, config (mimeapps.list) and the bus are all redirected.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home/.local/share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home/.config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(integration, "_reload_bus_config", lambda: None)  # don't touch the real bus
    (tmp_path / "home/.var/app/com.brave.Browser").mkdir(parents=True)
    return tmp_path / "home"


def test_chrome_id_matches_extension_key():
    manifest = json.loads((EXTENSION / "manifest.base.json").read_text())
    der = base64.b64decode(manifest["chromium"]["key"])
    derived = "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(der).hexdigest()[:32])
    assert derived == integration.CHROME_EXTENSION_ID
    assert manifest["firefox"]["browser_specific_settings"]["gecko"]["id"] == integration.FIREFOX_EXTENSION_ID


def test_install_and_uninstall(home):
    lines = []
    chosen = integration.install(home, ["firefox", "chrome", "brave"], log=lines.append)
    assert [b.name for b in chosen] == ["firefox", "chrome", "brave"]

    firefox = json.loads((home / ".mozilla/native-messaging-hosts/io.github.ldm.json").read_text())
    chrome = json.loads((home / ".config/google-chrome/NativeMessagingHosts/io.github.ldm.json").read_text())
    brave_dir = home / ".var/app/com.brave.Browser"
    brave = json.loads((brave_dir / "config/BraveSoftware/Brave-Browser/NativeMessagingHosts/io.github.ldm.json")
                       .read_text())
    assert firefox["allowed_extensions"] == ["ldm@ldm.local"]
    assert chrome["allowed_origins"] == [f"chrome-extension://{integration.CHROME_EXTENSION_ID}/"]
    assert brave["path"] == str(brave_dir / "data/ldm/ldm_nmhost.py")  # visible inside the sandbox
    for manifest in (firefox, chrome, brave):
        host = Path(manifest["path"])
        assert host.read_text().startswith("#!/usr/bin/env python3")
        assert os.access(host, os.X_OK)

    services = home / ".local/share/dbus-1/services"
    assert "daemon" in (services / "io.github.ldm.service").read_text()
    assert "--gapplication-service" in (services / "io.github.ldm.Gui.service").read_text()
    assert "DBusActivatable=true" in (home / ".local/share/applications/io.github.ldm.Gui.desktop").read_text()
    assert any("flatpak override --user --talk-name=io.github.ldm --talk-name=io.github.ldm.Gui com.brave.Browser"
               in line for line in lines)

    integration.uninstall(home, log=lines.append)
    assert not (home / ".mozilla/native-messaging-hosts/io.github.ldm.json").exists()
    assert not (services / "io.github.ldm.service").exists()
    assert not (brave_dir / "data/ldm/ldm_nmhost.py").exists()


def test_dry_run_writes_nothing(home):
    integration.install(home, ["firefox"], dry_run=True, log=lambda _: None)
    assert not (home / ".mozilla").exists()


def test_desktop_file_registers_torrent_types_and_default(home):
    integration.install(home, ["firefox"], log=lambda _: None)
    desktop = (home / ".local/share/applications/io.github.ldm.Gui.desktop").read_text()
    assert "MimeType=x-scheme-handler/magnet;application/x-bittorrent;" in desktop
    if shutil.which("xdg-mime"):
        mimeapps = home / ".config/mimeapps.list"
        assert "x-scheme-handler/magnet=io.github.ldm.Gui.desktop" in mimeapps.read_text()
        integration.uninstall(home, log=lambda _: None)
        assert "io.github.ldm.Gui.desktop" not in mimeapps.read_text()
