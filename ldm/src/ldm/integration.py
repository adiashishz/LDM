"""`ldm install-browser-integration`: native messaging host, D-Bus activation, systemd user unit, desktop file."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import nmhost
from .busnames import BUS_NAME

HOST_NAME = "io.github.adiashishz.ldm"  # native messaging host name used by the extension
FIREFOX_EXTENSION_ID = "ldm@adiashishz.github.io"
CHROME_EXTENSION_ID = "nfmeebgoiidgaakcafhniggapnipjhgp"  # derived from "key" in extension/manifest.base.json
GUI_ID = "io.github.adiashishz.LDM"
BRAVE_FLATPAK = "com.brave.Browser"
TORRENT_TYPES = ["x-scheme-handler/magnet", "application/x-bittorrent"]
FLATPAK_TALK = [f"--talk-name={BUS_NAME}", f"--talk-name={GUI_ID}"]
SYSTEMD_UNIT = "ldm.service"


@dataclass
class Browser:
    name: str
    kind: str                 # "firefox" or "chromium"
    manifest_dir: Path
    host_dir: Path            # where the host script copy lives (must be visible to the browser)
    flatpak: str | None
    installed: Callable[[], bool]


def browsers(home: Path) -> dict[str, Browser]:
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    host_dir = data / "ldm" / "native-host"
    brave_app = home / ".var/app" / BRAVE_FLATPAK
    return {
        "firefox": Browser("firefox", "firefox", home / ".mozilla/native-messaging-hosts", host_dir, None,
                           lambda: bool(shutil.which("firefox")) or (home / ".mozilla").exists()),
        "chrome": Browser("chrome", "chromium", home / ".config/google-chrome/NativeMessagingHosts", host_dir,
                          None, lambda: bool(shutil.which("google-chrome")) or (home / ".config/google-chrome").exists()),
        "brave": Browser("brave", "chromium",
                         brave_app / "config/BraveSoftware/Brave-Browser/NativeMessagingHosts",
                         brave_app / "data/ldm", BRAVE_FLATPAK, brave_app.exists),
    }


def host_manifest(browser: Browser) -> dict:
    manifest = {"name": HOST_NAME, "description": "ldm download manager bridge",
                "path": str(browser.host_dir / "ldm_nmhost.py"), "type": "stdio"}
    if browser.kind == "firefox":
        manifest["allowed_extensions"] = [FIREFOX_EXTENSION_ID]
    else:
        manifest["allowed_origins"] = [f"chrome-extension://{CHROME_EXTENSION_ID}/"]
    return manifest


def executable(name: str) -> Path:
    """`ldm` / `ldm-gui` next to the running interpreter (venv), else on PATH."""
    candidate = Path(sys.executable).parent / name
    found = candidate if candidate.exists() else (Path(shutil.which(name)) if shutil.which(name) else None)
    if found is None:
        raise FileNotFoundError(f"cannot find the `{name}` command")
    if " " in str(found):
        raise ValueError(f"D-Bus service files cannot run a path containing spaces: {found}")
    return found


ICON = Path(__file__).parent / "resources" / f"{GUI_ID}.svg"


def activation_files(data: Path, config: Path) -> dict[Path, str]:
    ldm, gui = executable("ldm"), executable("ldm-gui")
    services = data / "dbus-1" / "services"
    return {
        data / "icons" / "hicolor" / "scalable" / "apps" / f"{GUI_ID}.svg": ICON.read_text(),
        # Activation goes through systemd, so a daemon started by a client and one started at login are the same.
        services / f"{BUS_NAME}.service": (f"[D-BUS Service]\nName={BUS_NAME}\nExec={ldm} daemon\n"
                                           f"SystemdService={SYSTEMD_UNIT}\n"),
        config / "systemd" / "user" / SYSTEMD_UNIT: (
            "[Unit]\nDescription=LDM download manager\n\n"
            f"[Service]\nType=dbus\nBusName={BUS_NAME}\nExecStart={ldm} daemon\nRestart=on-failure\n\n"
            "[Install]\nWantedBy=default.target\n"),
        # GNOME (Wayland) can't watch the clipboard from the background; the window asks on focus instead.
        config / "autostart" / "io.github.adiashishz.LDM.ClipboardWatch.desktop": (
            "[Desktop Entry]\nType=Application\nName=LDM clipboard watcher\n"
            f"Comment=Offer to download links you copy\nExec={ldm} clipboard-watch\nIcon={GUI_ID}\n"
            "NoDisplay=true\nNotShowIn=GNOME;\nX-GNOME-Autostart-enabled=false\n"),
        services / f"{GUI_ID}.service": f"[D-BUS Service]\nName={GUI_ID}\nExec={gui} --gapplication-service\n",
        data / "applications" / f"{GUI_ID}.desktop": (
            "[Desktop Entry]\nType=Application\nName=LDM\nGenericName=Download Manager\n"
            f"Comment=Download files, videos and torrents\nExec={gui} %U\nIcon={GUI_ID}\nTerminal=false\n"
            "Categories=Network;FileTransfer;GTK;\nStartupNotify=true\nDBusActivatable=true\n"
            f"MimeType={';'.join(TORRENT_TYPES)};\n"),
    }


def plan(home: Path, selected: list[str] | None = None) -> tuple[dict[Path, str | bytes], list[Browser]]:
    """Files to write (path -> content) for the selected or detected browsers."""
    known = browsers(home)
    chosen = [known[name] for name in selected] if selected else [b for b in known.values() if b.installed()]
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    files: dict[Path, str | bytes] = dict(activation_files(data, config))
    host_source = Path(nmhost.__file__).read_bytes()
    for browser in chosen:
        files[browser.host_dir / "ldm_nmhost.py"] = host_source
        files[browser.manifest_dir / f"{HOST_NAME}.json"] = json.dumps(host_manifest(browser), indent=2) + "\n"
    return files, chosen


def install(home: Path, selected: list[str] | None = None, dry_run: bool = False,
            flatpak_override: bool = False, log: Callable[[str], None] = print) -> list[Browser]:
    files, chosen = plan(home, selected)
    for path, content in files.items():
        log(f"{'would write' if dry_run else 'wrote'} {path}")
        if dry_run:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        else:
            path.write_text(content)
    if not dry_run:
        _reload_bus_config()
        _set_default_handler(home, log)
        _systemctl("daemon-reload")
        if _autostart_wanted() and set_autostart(True):
            log(f"the download service now starts at login ({SYSTEMD_UNIT})")
    for browser in chosen:
        if browser.flatpak:
            command = ["flatpak", "override", "--user", *FLATPAK_TALK, browser.flatpak]
            if flatpak_override and not dry_run:
                subprocess.run(command, check=True)
                log(f"ran: {' '.join(command)}")
            else:
                log(f"{browser.name} runs in Flatpak; allow it to reach ldm with:\n  {' '.join(command)}")
    if not chosen:
        log("no supported browser found (use --browser to choose)")
    return chosen


def uninstall(home: Path, selected: list[str] | None = None, log: Callable[[str], None] = print) -> None:
    files, chosen = plan(home, selected or list(browsers(home)))
    set_autostart(False)
    for path in files:
        if path.exists():
            path.unlink()
            log(f"removed {path}")
    _reload_bus_config()
    _systemctl("daemon-reload")
    _remove_default_handler(home, log)
    for browser in chosen:
        if browser.flatpak:
            no_talk = [arg.replace("--talk-name", "--no-talk-name") for arg in FLATPAK_TALK]
            log(f"to revoke {browser.name}'s D-Bus access: flatpak override --user {' '.join(no_talk)} {browser.flatpak}")


def _set_default_handler(home: Path, log: Callable[[str], None]) -> None:
    """Make ldm open magnet: links and .torrent files (user decision for M6)."""
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(data / "applications")], capture_output=True, timeout=30)
    if not shutil.which("xdg-mime"):
        log("xdg-mime not found: set ldm as the magnet/.torrent handler in your desktop settings")
        return
    # xdg-mime reports success even when it can't create mimeapps.list in a missing folder.
    (Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")).mkdir(parents=True, exist_ok=True)
    subprocess.run(["xdg-mime", "default", f"{GUI_ID}.desktop", *TORRENT_TYPES], capture_output=True, timeout=30)
    set_ok = [t for t in TORRENT_TYPES if subprocess.run(["xdg-mime", "query", "default", t], capture_output=True,
                                                          text=True, timeout=30).stdout.strip() == f"{GUI_ID}.desktop"]
    if set_ok:
        log(f"ldm now opens: {', '.join(set_ok)}")
    if len(set_ok) < len(TORRENT_TYPES):
        log(f"could not make ldm the default for: {', '.join(t for t in TORRENT_TYPES if t not in set_ok)}")


def _remove_default_handler(home: Path, log: Callable[[str], None]) -> None:
    """Drop ldm from mimeapps.list defaults (xdg-mime has no 'unset')."""
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "mimeapps.list"
    if not config.exists():
        return
    lines = config.read_text().splitlines()
    kept = [line for line in lines if not (line.split("=")[0] in TORRENT_TYPES and f"{GUI_ID}.desktop" in line)]
    if kept != lines:
        config.write_text("\n".join(kept) + "\n")
        log(f"removed ldm as default for: {', '.join(TORRENT_TYPES)}")


def set_autostart(enabled: bool) -> bool:
    """Start the daemon at login (systemd user unit), or stop doing so. Returns whether it worked."""
    return _systemctl("enable" if enabled else "disable", SYSTEMD_UNIT)


def _autostart_wanted() -> bool:
    """The "autostart" setting, without creating a database just to read it."""
    from .store import Store, default_db_path

    path = default_db_path()
    return Store(path).get_settings()["autostart"] if path.exists() else True


def _systemctl(*args: str) -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _reload_bus_config() -> None:
    """dbus-broker does not watch service directories; ask it to rescan (best effort)."""
    if shutil.which("gdbus"):
        subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path",
                        "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.ReloadConfig"],
                       capture_output=True, timeout=10)
