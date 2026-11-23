"""`ldm install-browser-integration` on Windows: the browser bridge, magnet links, .torrent files and the
Default apps entry in the registry. The real registry is used on Windows only, under a throwaway key."""

import json
import sys
import uuid
from pathlib import Path

import pytest

from ldm import winintegration
from ldm.integration import CHROME_EXTENSION_ID, FIREFOX_EXTENSION_ID, HOST_NAME

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def test_manifests_name_the_bridge_and_the_extension(tmp_path):
    files = winintegration.manifest_files(Path("C:/LDM/ldm-nmhost.exe"), tmp_path)
    firefox = json.loads(files[tmp_path / "firefox.json"])
    chromium = json.loads(files[tmp_path / "chromium.json"])
    assert firefox["name"] == chromium["name"] == HOST_NAME
    assert firefox["path"] == chromium["path"] == str(Path("C:/LDM/ldm-nmhost.exe"))
    assert firefox["allowed_extensions"] == [FIREFOX_EXTENSION_ID]
    assert chromium["allowed_origins"] == [f"chrome-extension://{CHROME_EXTENSION_ID}/"]


def test_registry_entries(tmp_path):
    found = winintegration.entries(["firefox", "edge"], tmp_path, [r"C:\Program Files\LDM\ldm-gui.exe"],
                                   tmp_path / "ldm.ico")
    assert found[rf"Software\Mozilla\NativeMessagingHosts\{HOST_NAME}"] == {"": str(tmp_path / "firefox.json")}
    assert found[rf"Software\Microsoft\Edge\NativeMessagingHosts\{HOST_NAME}"] == {"": str(tmp_path / "chromium.json")}
    assert not any("Chrome" in key for key in found)  # only the chosen browsers
    command = r'"C:\Program Files\LDM\ldm-gui.exe" "%1"'
    assert found[r"Software\Classes\LDM.torrent\shell\open\command"] == {"": command}
    assert found[r"Software\Classes\LDM.magnet"] == {"": "Magnet link", "URL Protocol": ""}
    assert found[r"Software\Classes\.torrent\OpenWithProgids"] == {"LDM.torrent": ""}
    assert found[r"Software\LDM\Capabilities\URLAssociations"] == {"magnet": "LDM.magnet"}
    assert found[r"Software\RegisteredApplications"] == {"LDM": r"Software\LDM\Capabilities"}


@windows_only
def test_install_and_uninstall(tmp_path):
    import winreg

    base = rf"Software\LDM-tests-{uuid.uuid4().hex}"
    folder = tmp_path / "native-host"
    said = []
    try:
        chosen = winintegration.install(log=said.append, base=base, folder=folder, autostart=False)
        assert chosen == list(winintegration.BROWSERS)
        manifest = winintegration.read_value(rf"{base}\Google\Chrome\NativeMessagingHosts\{HOST_NAME}", "")
        assert manifest == str(folder / "chromium.json")
        assert json.loads(Path(manifest).read_text())["path"].lower().endswith("ldm-nmhost.exe")
        # Nobody else handles them under this key: LDM takes them right away.
        assert winintegration.opens_with_ldm("magnet", base) and winintegration.opens_with_ldm(".torrent", base)
        assert "LDM now opens magnet links" in said and "LDM now opens .torrent files" in said

        winintegration.uninstall(log=said.append, base=base, folder=folder, autostart=False)
        assert winintegration.read_value(rf"{base}\Google\Chrome\NativeMessagingHosts\{HOST_NAME}", "") is None
        assert not winintegration.opens_with_ldm("magnet", base)
        assert not winintegration.opens_with_ldm(".torrent", base)
        assert winintegration.read_value(rf"{base}\RegisteredApplications", "LDM") is None
        assert not (folder / "chromium.json").exists()
    finally:
        winintegration.delete_tree(base)
    with pytest.raises(FileNotFoundError):
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, base)


@windows_only
def test_another_programs_torrents_stay_its_own():
    base = rf"Software\LDM-tests-{uuid.uuid4().hex}"
    try:
        winintegration.write({rf"{base}\Classes\.torrent": {"": "qBittorrent.File.Torrent"}})
        found = winintegration.unclaimed(base)
        assert rf"{base}\Classes\.torrent" not in found  # left to the user, in Settings
        assert rf"{base}\Classes\magnet" in found         # nobody had magnet links
    finally:
        winintegration.delete_tree(base)
