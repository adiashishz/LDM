"""`ldm install-browser-integration`: native messaging host + D-Bus activation + desktop file."""

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

HOST_NAME = "io.github.ldm"  # native messaging host name used by the extension
FIREFOX_EXTENSION_ID = "ldm@ldm.local"
CHROME_EXTENSION_ID = "nfmeebgoiidgaakcafhniggapnipjhgp"  # derived from "key" in extension/manifest.base.json
GUI_ID = "io.github.ldm.Gui"
BRAVE_FLATPAK = "com.brave.Browser"
FLATPAK_TALK = [f"--talk-name={BUS_NAME}", f"--talk-name={GUI_ID}"]


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


def activation_files(data: Path) -> dict[Path, str]:
    ldm, gui = executable("ldm"), executable("ldm-gui")
    services = data / "dbus-1" / "services"
    return {
        services / f"{BUS_NAME}.service": f"[D-BUS Service]\nName={BUS_NAME}\nExec={ldm} daemon\n",
        services / f"{GUI_ID}.service": f"[D-BUS Service]\nName={GUI_ID}\nExec={gui} --gapplication-service\n",
        data / "applications" / f"{GUI_ID}.desktop": (
            "[Desktop Entry]\nType=Application\nName=Downloads (ldm)\nGenericName=Download Manager\n"
            f"Comment=Multi-part download manager\nExec={gui} %U\nIcon=folder-download\nTerminal=false\n"
            "Categories=Network;FileTransfer;GTK;\nStartupNotify=true\nDBusActivatable=true\n"),
    }


def plan(home: Path, selected: list[str] | None = None) -> tuple[dict[Path, str | bytes], list[Browser]]:
    """Files to write (path -> content) for the selected or detected browsers."""
    known = browsers(home)
    chosen = [known[name] for name in selected] if selected else [b for b in known.values() if b.installed()]
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    files: dict[Path, str | bytes] = dict(activation_files(data))
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
    for path in files:
        if path.exists():
            path.unlink()
            log(f"removed {path}")
    _reload_bus_config()
    for browser in chosen:
        if browser.flatpak:
            no_talk = [arg.replace("--talk-name", "--no-talk-name") for arg in FLATPAK_TALK]
            log(f"to revoke {browser.name}'s D-Bus access: flatpak override --user {' '.join(no_talk)} {browser.flatpak}")


def _reload_bus_config() -> None:
    """dbus-broker does not watch service directories; ask it to rescan (best effort)."""
    if shutil.which("gdbus"):
        subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path",
                        "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.ReloadConfig"],
                       capture_output=True, timeout=10)
