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
    calls = []
    monkeypatch.setattr(integration, "_systemctl", lambda *args: calls.append(args) or True)  # nor systemd
    monkeypatch.setattr(integration, "SYSTEMCTL_CALLS", calls, raising=False)
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

    firefox = json.loads((home / ".mozilla/native-messaging-hosts/io.github.adiashishz.ldm.json").read_text())
    chrome = json.loads((home / ".config/google-chrome/NativeMessagingHosts/io.github.adiashishz.ldm.json").read_text())
    brave_dir = home / ".var/app/com.brave.Browser"
    brave = json.loads((brave_dir / "config/BraveSoftware/Brave-Browser/NativeMessagingHosts/io.github.adiashishz.ldm.json")
                       .read_text())
    assert firefox["allowed_extensions"] == ["ldm@adiashishz.github.io"]
    assert chrome["allowed_origins"] == [f"chrome-extension://{integration.CHROME_EXTENSION_ID}/"]
    assert brave["path"] == str(brave_dir / "data/ldm/ldm_nmhost.py")  # visible inside the sandbox
    for manifest in (firefox, chrome, brave):
        host = Path(manifest["path"])
        shebang = host.read_text().splitlines()[0]  # packages rewrite it to #!/usr/bin/python3
        assert shebang in ("#!/usr/bin/env python3", "#!/usr/bin/python3")
        assert os.access(host, os.X_OK)

    services = home / ".local/share/dbus-1/services"
    assert "SystemdService=ldm.service" in (services / "io.github.adiashishz.LDM.Service.service").read_text()
    unit = (home / ".config/systemd/user/ldm.service").read_text()
    assert "Type=dbus" in unit and "BusName=io.github.adiashishz.LDM.Service" in unit and " daemon" in unit
    assert integration.SYSTEMCTL_CALLS == [("daemon-reload",), ("enable", "ldm.service")]
    assert "--gapplication-service" in (services / "io.github.adiashishz.LDM.service").read_text()
    assert (home / ".local/share/icons/hicolor/scalable/apps/io.github.adiashishz.LDM.svg").read_text().startswith("<svg")
    assert "DBusActivatable=true" in (home / ".local/share/applications/io.github.adiashishz.LDM.desktop").read_text()
    assert any("flatpak override --user --talk-name=io.github.adiashishz.LDM.Service --talk-name=io.github.adiashishz.LDM com.brave.Browser"
               in line for line in lines)

    integration.uninstall(home, log=lines.append)
    assert not (home / ".mozilla/native-messaging-hosts/io.github.adiashishz.ldm.json").exists()
    assert not (home / ".config/systemd/user/ldm.service").exists()
    assert integration.SYSTEMCTL_CALLS[2:] == [("disable", "ldm.service"), ("daemon-reload",)]
    assert not (services / "io.github.adiashishz.LDM.Service.service").exists()
    assert not (brave_dir / "data/ldm/ldm_nmhost.py").exists()


def test_dry_run_writes_nothing(home):
    integration.install(home, ["firefox"], dry_run=True, log=lambda _: None)
    assert not (home / ".mozilla").exists()


def test_desktop_file_registers_torrent_types_and_default(home):
    integration.install(home, ["firefox"], log=lambda _: None)
    desktop = (home / ".local/share/applications/io.github.adiashishz.LDM.desktop").read_text()
    assert "MimeType=x-scheme-handler/magnet;application/x-bittorrent;" in desktop
    if shutil.which("xdg-mime"):
        mimeapps = home / ".config/mimeapps.list"
        assert "x-scheme-handler/magnet=io.github.adiashishz.LDM.desktop" in mimeapps.read_text()
        integration.uninstall(home, log=lambda _: None)
        assert "io.github.adiashishz.LDM.desktop" not in mimeapps.read_text()
