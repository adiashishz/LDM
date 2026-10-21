"""File actions on downloads (1DM: md5 / compare with clipboard, move, rename, auto-move and cleanup)."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .engine import unique_path

ALGORITHMS = ("md5", "sha1", "sha256")
HEX_LENGTHS = {32: "md5", 40: "sha1", 64: "sha256"}
CHUNK = 1 << 20


def checksum(path: Path, algorithm: str) -> str:
    """Hex digest of a file (blocking: run in a thread)."""
    if algorithm not in ALGORITHMS:
        raise ValueError(f"algorithm must be one of {', '.join(ALGORITHMS)}")
    if not path.is_file():
        raise ValueError("checksums are for single files")
    digest = hashlib.new(algorithm)
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def algorithm_for(expected: str) -> str | None:
    """Which algorithm a pasted hex checksum belongs to (by its length)."""
    text = expected.strip().lower()
    if not text or any(c not in "0123456789abcdef" for c in text):
        return None
    return HEX_LENGTHS.get(len(text))


def check_name(name: str) -> str:
    name = name.strip()
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise ValueError("not a valid file name")
    return name


def move(source: Path, target: Path) -> Path:
    """Move a file or folder without overwriting anything: a taken name gets " (1)" etc. Blocking."""
    target = unique_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))  # rename on the same disk, copy + delete across disks
    return target
