"""A newer yt-dlp than the system's, kept per user.

Distributions update yt-dlp slower than YouTube changes (Debian and Ubuntu by months), and old versions
stop working. LDM can fetch yt-dlp's official zipimport build — pure Python, run by the system interpreter,
with the YouTube challenge solver (yt-dlp-ejs) inside — into ~/.local/share/ldm/yt-dlp/ and import it from
there whenever it is newer than the installed yt-dlp. Optional extras (brotli, websockets, mutagen,
pycryptodomex) still come from the system.

Packages for distributions with an old yt-dlp (Debian, Ubuntu) also ship the same build in
/usr/share/ldm/yt-dlp/, so videos work from the first start, offline, or with updates turned off.
The newest of the three copies (installed, bundled, user) is the one imported.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import re
import sys
import zipfile
from pathlib import Path

import httpx

# LDM_YTDLP_RELEASES: a mirror with the same layout (the tests point it at nothing).
RELEASES = os.environ.get("LDM_YTDLP_RELEASES") or "https://github.com/yt-dlp/yt-dlp/releases"
ASSET = "yt-dlp"            # the zipimport build
SUMS = "SHA2-256SUMS"
VERSION_FILE = "yt_dlp/version.py"


def folder() -> Path:
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return data / "ldm" / "yt-dlp"


def user_copy() -> Path:
    return folder() / ASSET


def bundled_copy() -> Path | None:
    """The copy a distribution package ships in <data dir>/ldm/yt-dlp/ (searched in XDG_DATA_DIRS), if any."""
    for data in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":"):
        path = Path(data) / "ldm" / "yt-dlp" / ASSET
        if data and path.is_file():
            return path
    return None


def version_key(version: str | None) -> tuple[int, ...]:
    """'2026.10.26' -> (2026, 10, 26); None or '' sorts first."""
    return tuple(int(p) for p in re.findall(r"\d+", version or ""))


def installed_version() -> str | None:
    """The yt-dlp installed with Python (distribution package or pip), without importing it."""
    try:
        return importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        return None


def zip_version(path: Path) -> str | None:
    """The version inside a zipimport build, or None if it isn't one."""
    try:
        with zipfile.ZipFile(path) as z:
            text = z.read(VERSION_FILE).decode()
    except (OSError, KeyError, zipfile.BadZipFile, UnicodeDecodeError):
        return None
    match = re.search(r"""^__version__\s*=\s*['"]([^'"]+)['"]""", text, re.M)
    return match.group(1) if match else None


def user_version() -> str | None:
    return zip_version(user_copy())


def activate() -> str | None:
    """Import yt-dlp from the user's or the bundled copy when one is newer than the installed yt-dlp;
    returns the version used, or None for the installed one.

    Must run before the first `import yt_dlp`."""
    if "yt_dlp" in sys.modules:
        return None
    bundled = bundled_copy()
    copies = [(user_version(), user_copy()), (zip_version(bundled) if bundled else None, bundled)]
    version, path = max(copies, key=lambda c: version_key(c[0]))  # the user's copy wins a tie
    if version and version_key(version) > version_key(installed_version()):
        sys.path.insert(0, str(path))
        return version
    return None


async def latest_version(client: httpx.AsyncClient, releases: str = RELEASES) -> str:
    """The newest release's tag, from the redirect of /releases/latest (no API rate limit)."""
    response = await client.get(f"{releases}/latest", follow_redirects=False)
    location = response.headers.get("location", "")
    match = re.search(r"/tag/([^/?#]+)$", location)
    if response.status_code not in (301, 302, 303, 307, 308) or not match:
        raise ValueError(f"could not find the latest yt-dlp release (HTTP {response.status_code})")
    return match.group(1)


async def update(client: httpx.AsyncClient, current: str | None, releases: str = RELEASES) -> str | None:
    """Download the latest release into user_copy() when it is newer than `current`.

    Returns the new version, or None when `current` is already the latest. The file is checked against
    the release's SHA2-256SUMS and replaced atomically, so a failed update leaves the old copy working."""
    latest = await latest_version(client, releases)
    if version_key(latest) <= version_key(current):
        return None
    base = f"{releases}/download/{latest}"
    sums = await client.get(f"{base}/{SUMS}", follow_redirects=True)
    sums.raise_for_status()
    expected = next((line.split()[0].lower() for line in sums.text.splitlines()
                     if len(line.split()) == 2 and line.split()[1].lstrip("*") == ASSET), None)
    if not expected:
        raise ValueError(f"yt-dlp {latest}: {ASSET} is not listed in {SUMS}")
    asset = await client.get(f"{base}/{ASSET}", follow_redirects=True)
    asset.raise_for_status()
    if hashlib.sha256(asset.content).hexdigest() != expected:
        raise ValueError(f"yt-dlp {latest}: the download does not match its checksum")
    target = user_copy()
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(ASSET + ".part")
    partial.write_bytes(asset.content)
    if zip_version(partial) is None:
        partial.unlink()
        raise ValueError(f"yt-dlp {latest}: the download is not a zipimport build")
    os.replace(partial, target)
    return zip_version(target)


def remove() -> bool:
    """Go back to the installed yt-dlp (takes effect when the service restarts)."""
    try:
        user_copy().unlink()
        return True
    except FileNotFoundError:
        return False
