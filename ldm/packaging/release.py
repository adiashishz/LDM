#!/usr/bin/env python3
"""Prepare an LDM release: one version number, written everywhere it appears.

    release.py VERSION NOTE [NOTE...]   set VERSION in every file, add changelog/release-note entries
    release.py --check [TAG]            check that every file has the same version (and TAG = v<version>)
    release.py --update-ytdlp           pin the newest yt-dlp release for the .deb (debian/yt-dlp.pin)

Each NOTE becomes one line of the release notes (RPM %changelog, debian/changelog, AppStream metainfo,
and from there the GitHub release). See RELEASING.md for the whole procedure.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "ldm/pyproject.toml"
INIT = ROOT / "ldm/src/ldm/__init__.py"
NMHOST = ROOT / "ldm/src/ldm/nmhost.py"
EXTENSION = ROOT / "extension/manifest.base.json"
SPEC = ROOT / "ldm/packaging/ldm-downloader.spec"
DEBIAN_CHANGELOG = ROOT / "debian/changelog"
PKGBUILD = ROOT / "ldm/packaging/arch/ldm-downloader/PKGBUILD"
PKGBUILD_GIT = ROOT / "ldm/packaging/arch/ldm-downloader-git/PKGBUILD"
METAINFO = ROOT / "ldm/data/io.github.adiashishz.LDM.metainfo.xml"
YTDLP_PIN = ROOT / "debian/yt-dlp.pin"

VERSION_RE = r"\d+\.\d+\.\d+"


def read_versions() -> dict[str, str | None]:
    """The version each file states (None where it can't be found)."""
    def find(path: Path, pattern: str) -> str | None:
        match = re.search(pattern, path.read_text(), re.M)
        return match.group(1) if match else None

    return {
        "ldm/pyproject.toml": find(PYPROJECT, rf'^version = "({VERSION_RE})"'),
        "ldm/src/ldm/__init__.py": find(INIT, rf'^__version__ = "({VERSION_RE})"'),
        "ldm/src/ldm/nmhost.py": find(NMHOST, rf'^VERSION = "({VERSION_RE})"'),
        "extension/manifest.base.json": json.loads(EXTENSION.read_text()).get("version"),
        "ldm/packaging/ldm-downloader.spec": find(SPEC, rf"^Version:\s+({VERSION_RE})$"),
        "debian/changelog": find(DEBIAN_CHANGELOG, rf"\A\S+ \(({VERSION_RE})-"),
        "ldm/packaging/arch/ldm-downloader/PKGBUILD": find(PKGBUILD, rf"^pkgver=({VERSION_RE})$"),
        "ldm/packaging/arch/ldm-downloader-git/PKGBUILD": find(PKGBUILD_GIT, rf"^pkgver=({VERSION_RE})\.r"),
        "ldm/data/io.github.adiashishz.LDM.metainfo.xml": find(METAINFO, rf'<release version="({VERSION_RE})"'),
    }


def check(tag: str | None = None) -> int:
    versions = read_versions()
    found = set(versions.values())
    for path, version in versions.items():
        print(f"  {version or 'NOT FOUND':10} {path}")
    if len(found) != 1 or None in found:
        print("the versions don't agree: run release.py VERSION to set them all", file=sys.stderr)
        return 1
    version = found.pop()
    if tag is not None and tag != f"v{version}":
        print(f"tag {tag} doesn't match the version {version} in the files", file=sys.stderr)
        return 1
    print(f"version {version}" + (f", matching tag {tag}" if tag else ""))
    return 0


def maintainer() -> tuple[str, str]:
    """Name and email from the latest debian/changelog entry, so every changelog names the same person."""
    match = re.search(r"^ -- (.+?) <([^>]+)>", DEBIAN_CHANGELOG.read_text(), re.M)
    if not match:
        sys.exit("can't find the maintainer in debian/changelog")
    return match.group(1), match.group(2)


def substitute(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text()
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.M)
    if count != 1:
        sys.exit(f"{path.relative_to(ROOT)}: no match for {pattern!r}")
    path.write_text(new)


def set_version(version: str, notes: list[str], now: datetime) -> None:
    if not re.fullmatch(VERSION_RE, version):
        sys.exit(f"not a version (X.Y.Z): {version}")
    if version in set(read_versions().values()):
        sys.exit(f"{version} is already the current version")
    name, mail = maintainer()

    substitute(PYPROJECT, rf'^version = "{VERSION_RE}"', f'version = "{version}"')
    substitute(INIT, rf'^__version__ = "{VERSION_RE}"', f'__version__ = "{version}"')
    substitute(NMHOST, rf'^VERSION = "{VERSION_RE}"', f'VERSION = "{version}"')
    substitute(EXTENSION, rf'^(\s*)"version": "{VERSION_RE}"', rf'\g<1>"version": "{version}"')
    substitute(PKGBUILD, rf"^pkgver={VERSION_RE}$", f"pkgver={version}")
    substitute(PKGBUILD, r"^pkgrel=\d+$", "pkgrel=1")
    substitute(PKGBUILD_GIT, rf"^pkgver={VERSION_RE}\.r", f"pkgver={version}.r")

    # RPM: new version, release 1, and a %changelog entry on top.
    substitute(SPEC, rf"^Version:(\s+){VERSION_RE}$", rf"Version:\g<1>{version}")
    substitute(SPEC, r"^Release:(\s+)\d+%\{\?dist\}$", r"Release:\g<1>1%{?dist}")
    entry = f"* {now.strftime('%a %b %d %Y')} {name} <{mail}> - {version}-1\n" + "".join(f"- {n}\n" for n in notes)
    substitute(SPEC, r"^%changelog\n", f"%changelog\n{entry}\n")

    # Debian: a new entry on top (the release workflow adds the per-distribution suffix).
    debian = (f"ldm-downloader ({version}-1) unstable; urgency=medium\n\n"
              + "".join(f"  * {n}\n" for n in notes)
              + f"\n -- {name} <{mail}>  {email.utils.format_datetime(now)}\n\n")
    DEBIAN_CHANGELOG.write_text(debian + DEBIAN_CHANGELOG.read_text())

    # AppStream: shown by GNOME Software, KDE Discover and Flathub, and used for the GitHub release notes.
    items = "".join(f"          <li>{escape(n, quote=False)}</li>\n" for n in notes)
    release = (f'    <release version="{version}" date="{now.date().isoformat()}">\n'
               f"      <description>\n        <ul>\n{items}        </ul>\n      </description>\n    </release>\n")
    substitute(METAINFO, r"^  <releases>\n", f"  <releases>\n{release}")
    print(f"set version {version} in {len(read_versions())} files")


def release_notes(version: str) -> str:
    """The metainfo's notes for `version`, as Markdown (for the GitHub release)."""
    text = METAINFO.read_text()
    match = re.search(rf'<release version="{re.escape(version)}"[^>]*>(.*?)</release>', text, re.S)
    if not match:
        sys.exit(f"no release notes for {version} in the metainfo")
    items = re.findall(r"<(?:li|p)>(.*?)</(?:li|p)>", match.group(1), re.S)
    from html import unescape
    return "\n".join(f"- {' '.join(unescape(i).split())}" for i in items) + "\n"


def update_ytdlp() -> None:
    """Pin the latest yt-dlp release (version and official SHA-256 of its zipimport build)."""
    with urllib.request.urlopen("https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest", timeout=30) as r:
        version = json.load(r)["tag_name"]
    url = f"https://github.com/yt-dlp/yt-dlp/releases/download/{version}/SHA2-256SUMS"
    with urllib.request.urlopen(url, timeout=30) as r:
        sums = r.read().decode()
    sha = next((line.split()[0] for line in sums.splitlines() if line.split()[1:] == ["yt-dlp"]), None)
    if not sha or not re.fullmatch(r"[0-9a-f]{64}", sha):
        sys.exit(f"yt-dlp {version}: no checksum for the zipimport build in SHA2-256SUMS")
    old = re.search(r"^version=(.+)$", YTDLP_PIN.read_text(), re.M).group(1)
    substitute(YTDLP_PIN, r"^version=.+$", f"version={version}")
    substitute(YTDLP_PIN, r"^sha256=.+$", f"sha256={sha}")
    print(f"yt-dlp {old} -> {version}" if old != version else f"yt-dlp {version} is already pinned")


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    if argv[0] == "--check":
        return check(argv[1] if len(argv) > 1 else None)
    if argv[0] == "--notes":
        print(release_notes(argv[1]), end="")
        return 0
    if argv[0] == "--update-ytdlp":
        update_ytdlp()
        return 0
    if len(argv) < 2:
        sys.exit("give the release notes: release.py VERSION NOTE [NOTE...]")
    set_version(argv[0], argv[1:], datetime.now(timezone.utc).astimezone())
    return check()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
