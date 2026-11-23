"""`ldm install-browser-integration` on Windows: the browser bridge for Firefox, Chrome, Edge, Brave and
Chromium, LDM as a program for magnet links and .torrent files, and starting the service at login.

Everything goes into the user's part of the registry (HKEY_CURRENT_USER\\Software, or `base` under it in
tests), so no administrator rights are needed:
- `<browser>\\NativeMessagingHosts\\io.github.adiashishz.ldm` → a manifest file in LDM's data folder that
  names `ldm-nmhost.exe`;
- `Classes\\LDM.torrent` and `Classes\\LDM.magnet` (how LDM opens them), `.torrent\\OpenWithProgids`, and
  `LDM\\Capabilities` + `RegisteredApplications` so LDM is listed in Settings → Apps → Default apps. Windows
  won't let a program make itself the default; where no other program handles magnet links or .torrent
  files, LDM takes them right away, otherwise the user chooses LDM in Settings.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from . import paths
from .integration import HOST_NAME, bridge_manifest

log = logging.getLogger(__name__)

BROWSERS = {  # name → (manifest kind, the browser's key under Software)
    "firefox": ("firefox", r"Mozilla"),
    "chrome": ("chromium", r"Google\Chrome"),
    "edge": ("chromium", r"Microsoft\Edge"),
    "brave": ("chromium", r"BraveSoftware\Brave-Browser"),
    "chromium": ("chromium", r"Chromium"),
}
TORRENT_PROGID, MAGNET_PROGID = "LDM.torrent", "LDM.magnet"
APP_KEY = "LDM"  # Software\LDM\Capabilities
DEFAULT_APPS = "Settings → Apps → Default apps → LDM"

Entries = dict[str, dict[str, str]]  # key (under HKEY_CURRENT_USER) → {value name ("" = default): text}


def program(name: str) -> Path:
    """`ldm-nmhost.exe`: beside the running Python (winsys.beside_python), else on PATH."""
    from .winsys import beside_python

    beside = beside_python(name)
    if beside:
        return beside
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(f"cannot find {name}.exe (install LDM with pip, or use the installer)")
    return Path(found)


def open_command(gui: list[str]) -> str:
    return f'{subprocess.list2cmdline(gui)} "%1"'


def manifest_files(bridge: Path, folder: Path) -> dict[Path, str]:
    """One manifest for Firefox, one for the Chromium family (they name the extension differently)."""
    return {folder / f"{kind}.json": json.dumps(bridge_manifest(kind, bridge), indent=2) + "\n"
            for kind in ("firefox", "chromium")}


def entries(selected: list[str], folder: Path, gui: list[str], icon: Path | None,
            base: str = "Software") -> Entries:
    """What `install` writes to the registry."""
    found: Entries = {}
    for name in selected:
        kind, key = BROWSERS[name]
        found[rf"{base}\{key}\NativeMessagingHosts\{HOST_NAME}"] = {"": str(folder / f"{kind}.json")}
    classes, command = rf"{base}\Classes", open_command(gui)
    for progid, description, extra in ((TORRENT_PROGID, "Torrent file", {}),
                                       (MAGNET_PROGID, "Magnet link", {"URL Protocol": ""})):
        found[rf"{classes}\{progid}"] = {"": description, **extra}
        found[rf"{classes}\{progid}\shell\open\command"] = {"": command}
        if icon:
            found[rf"{classes}\{progid}\DefaultIcon"] = {"": str(icon)}
    found[rf"{classes}\.torrent\OpenWithProgids"] = {TORRENT_PROGID: ""}
    capabilities = rf"{base}\{APP_KEY}\Capabilities"
    found[capabilities] = {"ApplicationName": "LDM", "ApplicationDescription": "Download files, videos and torrents"}
    found[rf"{capabilities}\FileAssociations"] = {".torrent": TORRENT_PROGID}
    found[rf"{capabilities}\URLAssociations"] = {"magnet": MAGNET_PROGID}
    found[rf"{base}\RegisteredApplications"] = {"LDM": capabilities}
    return found


def unclaimed(base: str = "Software") -> Entries:
    """Where no other program opens magnet links or .torrent files, LDM's registration makes them work
    without a trip to Settings."""
    found: Entries = {}
    classes = rf"{base}\Classes"
    torrent = user_choice(base, r"FileExts\.torrent") or class_value(base, ".torrent")
    if not torrent or torrent == TORRENT_PROGID:
        found[rf"{classes}\.torrent"] = {"": TORRENT_PROGID}
    magnet = user_choice(base, "magnet", url=True)
    command = class_value(base, r"magnet\shell\open\command")
    if (magnet == MAGNET_PROGID) or (not magnet and (not command or command.startswith(open_command_prefix()))):
        found[rf"{classes}\magnet"] = {"": "URL:Magnet link", "URL Protocol": ""}
        found[rf"{classes}\magnet\shell\open\command"] = {"": open_command(_gui())}
    return found


def opens_with_ldm(name: str, base: str = "Software") -> bool:
    """Whether Windows opens magnet links ("magnet") or .torrent files (".torrent") with LDM now."""
    url = name == "magnet"
    chosen = user_choice(base, name if url else rf"FileExts\{name}", url)
    if chosen:
        return chosen in (TORRENT_PROGID, MAGNET_PROGID)
    if url:
        return (class_value(base, r"magnet\shell\open\command") or "").startswith(open_command_prefix())
    return class_value(base, ".torrent") == TORRENT_PROGID


def open_command_prefix() -> str:
    return subprocess.list2cmdline(_gui())


def _gui() -> list[str]:
    from .winsys import gui_command

    return gui_command()


def user_choice(base: str, name: str, url: bool = False) -> str | None:
    """The program the user chose in Windows' "Open with" or Default apps (its ProgId), if any."""
    where = (rf"{base}\Microsoft\Windows\Shell\Associations\UrlAssociations\{name}\UserChoice" if url
             else rf"{base}\Microsoft\Windows\CurrentVersion\Explorer\{name}\UserChoice")
    return read_value(where, "ProgId")


def class_value(base: str, key: str) -> str | None:
    """A class's default value as Windows sees it: the user's and the computer's registrations together
    (programs installed for everyone register for the whole computer). Tests read only their own `base`."""
    import winreg

    if base != "Software":
        return read_value(rf"{base}\Classes\{key}", "")
    return read_value(key, "", winreg.HKEY_CLASSES_ROOT)


def read_value(key: str, name: str, root: int | None = None) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER if root is None else root, key) as opened:
            value, _ = winreg.QueryValueEx(opened, name)
            return str(value)
    except OSError:
        return None


def write(found: Entries) -> None:
    import winreg

    for key, values in found.items():
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as opened:
            for name, value in values.items():
                winreg.SetValueEx(opened, name, 0, winreg.REG_SZ, value)


def delete_tree(key: str) -> bool:
    """Remove a key with everything under it; False when it wasn't there."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as opened:
            children = []
            while True:
                try:
                    children.append(winreg.EnumKey(opened, len(children)))
                except OSError:
                    break
        for child in children:
            delete_tree(rf"{key}\{child}")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        return True
    except FileNotFoundError:
        return False


def delete_value(key: str, name: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as opened:
            winreg.DeleteValue(opened, name)
    except FileNotFoundError:
        pass


def associations_changed() -> None:
    """Tell Explorer to reread the file and link associations."""
    import ctypes

    SHCNE_ASSOCCHANGED, SHCNF_IDLIST = 0x08000000, 0
    ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)


def write_icon(folder: Path) -> Path | None:
    """LDM's icon as an .ico for Explorer (from the tray's PNG, with Pillow)."""
    try:
        from PIL import Image

        from .wintray import ICON

        folder.mkdir(parents=True, exist_ok=True)
        icon = folder / "ldm.ico"
        Image.open(ICON).save(icon, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
        return icon
    except Exception as e:  # no Pillow: Explorer shows a generic icon
        log.info("no icon for magnet links and .torrent files: %s", e)
        return None


def install(selected: list[str] | None = None, dry_run: bool = False, log: Callable[[str], None] = print,
            base: str = "Software", folder: Path | None = None, autostart: bool = True) -> list[str]:
    """Register LDM (all supported browsers unless `selected`); returns the browsers set up."""
    from .integration import _autostart_wanted, set_autostart

    chosen = selected or list(BROWSERS)
    folder = folder or paths.data_dir() / "native-host"
    bridge, gui = program("ldm-nmhost"), _gui()
    files = manifest_files(bridge, folder)
    icon = None if dry_run else write_icon(folder)
    found = entries(chosen, folder, gui, icon, base)
    for path, content in files.items():
        log(f"{'would write' if dry_run else 'wrote'} {path}")
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if dry_run:
        for key in found:
            log(rf"would write HKEY_CURRENT_USER\{key}")
        return chosen
    write(found)
    take = unclaimed(base)
    write(take)
    associations_changed()
    log(f"the browser bridge is set up for: {', '.join(chosen)}")
    for kind, keys in (("magnet links", "magnet"), (".torrent files", ".torrent")):
        if any(key.endswith(rf"Classes\{keys}") for key in take) or opens_with_ldm(keys, base):
            log(f"LDM now opens {kind}")
        else:
            log(f"to open {kind} with LDM, choose it in {DEFAULT_APPS}")
    if autostart and _autostart_wanted() and set_autostart(True):
        log("the download service now starts at login")
    return chosen


def uninstall(selected: list[str] | None = None, log: Callable[[str], None] = print, base: str = "Software",
              folder: Path | None = None, autostart: bool = True) -> None:
    from .integration import set_autostart

    folder = folder or paths.data_dir() / "native-host"
    if autostart:
        set_autostart(False)
    for name in selected or list(BROWSERS):
        if delete_tree(rf"{base}\{BROWSERS[name][1]}\NativeMessagingHosts\{HOST_NAME}"):
            log(f"removed the browser bridge for {name}")
    classes = rf"{base}\Classes"
    if not selected:  # everything: also magnet links, .torrent files and the Default apps entry
        for progid in (TORRENT_PROGID, MAGNET_PROGID):
            delete_tree(rf"{classes}\{progid}")
        delete_value(rf"{classes}\.torrent\OpenWithProgids", TORRENT_PROGID)
        if read_value(rf"{classes}\.torrent", "") == TORRENT_PROGID:
            delete_value(rf"{classes}\.torrent", "")
        if (read_value(rf"{classes}\magnet\shell\open\command", "") or "").startswith(open_command_prefix()):
            delete_tree(rf"{classes}\magnet")
        delete_tree(rf"{base}\{APP_KEY}")
        delete_value(rf"{base}\RegisteredApplications", "LDM")
        associations_changed()
        for path in [*manifest_files(Path("ldm-nmhost.exe"), folder), folder / "ldm.ico"]:
            if path.exists():
                path.unlink()
                log(f"removed {path}")
        log("LDM no longer opens magnet links or .torrent files")
