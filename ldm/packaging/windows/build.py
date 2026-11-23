"""Build LDM's Windows installer: the PyInstaller bundle (ldm.spec) with yt-dlp, ffmpeg and Node.js, then the
Inno Setup installer (ldm.iss).

    python ldm/packaging/windows/build.py [--no-installer]

Runs on Windows with LDM installed in the running Python and, for the installer, Inno Setup 6:

    pip install ./ldm[gui] "yt-dlp[default]==<version in debian/yt-dlp.pin>" libtorrent pyinstaller

(yt-dlp's extras must be the ones the bundled yt-dlp release expects.) Writes build/windows/dist/LDM (the bundle) and
build/windows/LDM-Setup-<version>.exe. Downloads are pinned below and checked against their SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
WORK = ROOT / "build" / "windows"

# FFmpeg, LGPL build (no GPL parts), from BtbN's FFmpeg-Builds: a month's last build, which stays downloadable
# for two years. Hash from the release's checksums.sha256. Same FFmpeg version as the Flatpak.
FFMPEG_RELEASE = "autobuild-2026-10-31-12-59"
FFMPEG_NAME = "ffmpeg-n9.0.2-39-g7adfbe40dc-win64-lgpl-shared-9.0"
FFMPEG_SHA256 = "e907b01fa4783913151237e37df2a10fa97d99a1598a021bf59d4ae62ffed4c0"
FFMPEG_URL = f"https://github.com/BtbN/FFmpeg-Builds/releases/download/{FFMPEG_RELEASE}/{FFMPEG_NAME}.zip"
FFMPEG_SOURCE = "https://github.com/FFmpeg/FFmpeg/tree/7adfbe40dc"
# Node.js for YouTube's challenges (yt-dlp runs its JavaScript); LTS, hash from nodejs.org's SHASUMS256.txt.
NODE_VERSION = "v24.22.3"
NODE_SHA256 = "e612af99f626ab415ea225af6bce5c971feb448cb5cae250bb1c3fa50c5f7d9f"
NODE_URL = f"https://nodejs.org/dist/{NODE_VERSION}/node-{NODE_VERSION}-win-x64.zip"
# yt-dlp: the same pinned release as the .deb (ldm/packaging/release.py --update-ytdlp updates it).
YTDLP_PIN = ROOT / "debian" / "yt-dlp.pin"

INNO = [Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe"]


def version() -> str:
    return tomllib.loads((ROOT / "ldm" / "pyproject.toml").read_text())["project"]["version"]


def fetch(url: str, sha256: str, name: str) -> Path:
    """Download once into build/windows/downloads, and check it."""
    target = WORK / "downloads" / name
    if not (target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == sha256):
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {url}")
        with urllib.request.urlopen(url, timeout=120) as response, open(target, "wb") as out:
            shutil.copyfileobj(response, out)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != sha256:
        target.unlink()
        sys.exit(f"{name}: SHA-256 {digest}, expected {sha256}")
    return target


def tools() -> None:
    """build/windows/tools: ffmpeg.exe, ffprobe.exe and their DLLs, node.exe, and their licences."""
    folder = WORK / "tools"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    with zipfile.ZipFile(fetch(FFMPEG_URL, FFMPEG_SHA256, f"{FFMPEG_NAME}.zip")) as z:
        for member in z.namelist():
            name = member.rsplit("/", 1)[-1]
            if "/bin/" in member and (name in ("ffmpeg.exe", "ffprobe.exe") or name.endswith(".dll")):
                (folder / name).write_bytes(z.read(member))
            elif member == f"{FFMPEG_NAME}/LICENSE.txt":
                (folder / "ffmpeg-LICENSE.txt").write_bytes(z.read(member))
    (folder / "ffmpeg-SOURCE.txt").write_text(
        f"FFmpeg {FFMPEG_NAME}, built by https://github.com/BtbN/FFmpeg-Builds (release {FFMPEG_RELEASE}),\n"
        f"licensed under the LGPL (see ffmpeg-LICENSE.txt). Its source code: {FFMPEG_SOURCE}\n")
    with zipfile.ZipFile(fetch(NODE_URL, NODE_SHA256, f"node-{NODE_VERSION}-win-x64.zip")) as z:
        top = f"node-{NODE_VERSION}-win-x64"
        (folder / "node.exe").write_bytes(z.read(f"{top}/node.exe"))
        (folder / "node-LICENSE.txt").write_bytes(z.read(f"{top}/LICENSE"))
    missing = {"ffmpeg.exe", "ffprobe.exe", "node.exe"} - {p.name for p in folder.iterdir()}
    if missing:
        sys.exit(f"missing from the downloads: {', '.join(sorted(missing))}")


def ytdlp() -> None:
    pin = dict(line.split("=", 1) for line in YTDLP_PIN.read_text().splitlines() if "=" in line
               and not line.startswith("#"))
    url = f"https://github.com/yt-dlp/yt-dlp/releases/download/{pin['version']}/yt-dlp"
    source = fetch(url, pin["sha256"], f"yt-dlp-{pin['version']}")
    target = WORK / "yt-dlp" / "yt-dlp"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def icon() -> None:
    from PIL import Image

    with Image.open(ROOT / "ldm" / "src" / "ldm" / "resources" / "io.github.adiashishz.LDM.png") as image:
        image.save(WORK / "ldm.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])


def bundle() -> Path:
    env = {**os.environ, "LDM_BUILD": str(WORK)}
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
                    "--distpath", str(WORK / "dist"), "--workpath", str(WORK / "pyinstaller"),
                    str(HERE / "ldm.spec")], check=True, env=env)
    source = WORK / "dist" / "LDM"
    # The tools go in as they are, next to the bundle's own files (tools_path.py puts them on PATH); through
    # PyInstaller, ffmpeg.exe's libraries would be collected a second time.
    shutil.copytree(WORK / "tools", source / "_internal" / "tools")
    return source


def report(bundle: Path, top: int = 25) -> None:
    """Where the bundle's space goes: its size, the largest folders under _internal and the largest files."""
    files = [(path.stat().st_size, path) for path in bundle.rglob("*") if path.is_file()]
    total = sum(size for size, _ in files)
    folders: dict[str, int] = {}
    for size, path in files:
        rel = path.relative_to(bundle).parts
        depth = 3 if rel[:2] == ("_internal", "PySide6") else 2 if rel[0] == "_internal" else 1
        key = "/".join(rel[:depth])
        folders[key] = folders.get(key, 0) + size
    mb = lambda n: f"{n / 1e6:8.1f} MB"  # noqa: E731
    print(f"bundle: {mb(total)} in {len(files)} files")
    print("largest folders:")
    for key, size in sorted(folders.items(), key=lambda item: -item[1])[:top]:
        print(f"  {mb(size)}  {key}")
    print("largest files:")
    for size, path in sorted(files, reverse=True)[:top]:
        print(f"  {mb(size)}  {path.relative_to(bundle)}")


def installer(source: Path) -> Path:
    iscc = next((path for path in INNO if path.exists()), None) or shutil.which("iscc")
    if not iscc:
        sys.exit("Inno Setup 6 (ISCC.exe) not found: install it, or build with --no-installer")
    subprocess.run([str(iscc), "/Q", f"/DVersion={version()}", f"/DSource={source}",
                    f"/DIcon={WORK / 'ldm.ico'}", f"/O{WORK}", str(HERE / "ldm.iss")], check=True)
    return WORK / f"LDM-Setup-{version()}.exe"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-installer", action="store_true", help="only the bundle (build/windows/dist/LDM)")
    args = parser.parse_args()
    if sys.platform != "win32":
        sys.exit("build.py builds the Windows installer: run it on Windows")
    WORK.mkdir(parents=True, exist_ok=True)
    tools()
    ytdlp()
    icon()
    source = bundle()
    print(f"bundle: {source}")
    report(source)
    if not args.no_installer:
        setup = installer(source)
        print(f"installer: {setup} ({setup.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
