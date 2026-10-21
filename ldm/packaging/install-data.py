#!/usr/bin/env python3
"""Install LDM's non-Python files under DESTDIR (used by the RPM spec and the Flatpak manifest).

    install-data.py DESTDIR [--prefix /usr] [--sysconfdir /etc] [--no-browsers]

Writes the desktop file, AppStream metainfo, icon, D-Bus activation files, the systemd user unit,
the clipboard watcher's autostart entry, and system-wide native-messaging manifests for Firefox,
Chrome and Chromium (the per-user `ldm install-browser-integration` covers Flatpak browsers).
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ICON = ROOT / "src/ldm/resources/io.github.adiashishz.LDM.svg"  # in the package: the per-user installer needs it
sys.path.insert(0, str(ROOT / "src"))
from ldm.integration import CHROME_EXTENSION_ID, FIREFOX_EXTENSION_ID, HOST_NAME  # noqa: E402


def install(source: Path, target: Path, text: str | None = None, mode: int = 0o644) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if text is None:
        shutil.copyfile(source, target)
    else:
        target.write_text(text)
    target.chmod(mode)
    print(target)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("destdir", type=Path)
    parser.add_argument("--prefix", default="/usr")
    parser.add_argument("--sysconfdir", default="/etc")
    parser.add_argument("--no-browsers", action="store_true", help="skip native-messaging manifests (Flatpak)")
    args = parser.parse_args()
    root = args.destdir
    prefix = root / args.prefix.lstrip("/")
    etc = root / args.sysconfdir.lstrip("/")
    bindir = f"{args.prefix}/bin"

    def with_prefix(name: str) -> str:
        return (DATA / name).read_text().replace("/usr/bin/", f"{bindir}/")

    share = prefix / "share"
    install(DATA / "io.github.adiashishz.LDM.desktop", share / "applications/io.github.adiashishz.LDM.desktop")
    install(DATA / "io.github.adiashishz.LDM.metainfo.xml", share / "metainfo/io.github.adiashishz.LDM.metainfo.xml")
    install(ICON, share / "icons/hicolor/scalable/apps/io.github.adiashishz.LDM.svg")
    install(None, share / "dbus-1/services/io.github.adiashishz.LDM.Service.service", with_prefix("io.github.adiashishz.LDM.Service.service"))
    install(None, share / "dbus-1/services/io.github.adiashishz.LDM.service", with_prefix("io.github.adiashishz.LDM.service"))
    install(None, prefix / "lib/systemd/user/ldm.service", with_prefix("ldm.service"))
    install(DATA / "io.github.adiashishz.LDM.ClipboardWatch.desktop", etc / "xdg/autostart/io.github.adiashishz.LDM.ClipboardWatch.desktop")
    if args.no_browsers:
        return

    host = f"{args.prefix}/libexec/ldm/ldm_nmhost.py"
    install(ROOT / "src/ldm/nmhost.py", prefix / "libexec/ldm/ldm_nmhost.py", mode=0o755)
    base = {"name": HOST_NAME, "description": "LDM download manager bridge", "path": host, "type": "stdio"}
    firefox = json.dumps({**base, "allowed_extensions": [FIREFOX_EXTENSION_ID]}, indent=2) + "\n"
    chromium = json.dumps({**base, "allowed_origins": [f"chrome-extension://{CHROME_EXTENSION_ID}/"]}, indent=2) + "\n"
    install(None, prefix / f"lib/mozilla/native-messaging-hosts/{HOST_NAME}.json", firefox)
    install(None, etc / f"opt/chrome/native-messaging-hosts/{HOST_NAME}.json", chromium)
    install(None, etc / f"chromium/native-messaging-hosts/{HOST_NAME}.json", chromium)


if __name__ == "__main__":
    main()
