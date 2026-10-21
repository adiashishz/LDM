"""Backup and restore (1DM): settings, host rules, site logins and the download list in one file.

A plain backup is readable JSON (`.json`) without passwords. With a password it is encrypted
(`.ldmbackup`: scrypt + Fernet from python3-cryptography) and also carries the login and proxy passwords.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

FORMAT = "ldm-backup"
VERSION = 1
MAGIC = b"LDMBACKUP1\n"
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
PREFIX = "ldm-backup-"
NOT_RESTORED = {"backup_last"}  # bookkeeping, not preferences


class BackupError(Exception):
    pass


def default_folder() -> Path:
    documents = os.environ.get("XDG_DOCUMENTS_DIR") or str(Path.home() / "Documents")
    return Path(documents) / "LDM Backups"


def file_name(encrypted: bool, when: float | None = None) -> str:
    stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(when))
    return f"{PREFIX}{stamp}{'.ldmbackup' if encrypted else '.json'}"


def encode(data: dict[str, Any], password: str = "") -> bytes:
    text = json.dumps(data, indent=1).encode()
    if not password:
        return text
    salt = os.urandom(16)
    header = {"kdf": "scrypt", **SCRYPT, "salt": base64.b64encode(salt).decode()}
    return MAGIC + json.dumps(header).encode() + b"\n" + _fernet(password, salt, SCRYPT).encrypt(text)


def decode(blob: bytes, password: str = "") -> dict[str, Any]:
    if blob.startswith(MAGIC):
        if not password:
            raise BackupError("this backup is protected by a password")
        header_line, _, token = blob[len(MAGIC):].partition(b"\n")
        header = json.loads(header_line)
        from cryptography.fernet import InvalidToken
        try:
            text = _fernet(password, base64.b64decode(header["salt"]),
                           {k: header[k] for k in ("n", "r", "p")}).decrypt(token)
        except InvalidToken:
            raise BackupError("wrong password") from None
    else:
        text = blob
    try:
        data = json.loads(text)
    except ValueError:
        raise BackupError("not an LDM backup") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise BackupError("not an LDM backup")
    if data.get("version", 0) > VERSION:
        raise BackupError("this backup was made by a newer LDM")
    return data


def is_encrypted(blob: bytes) -> bool:
    return blob.startswith(MAGIC)


def _fernet(password: str, salt: bytes, params: dict[str, int]):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    key = Scrypt(salt=salt, length=32, **params).derive(password.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def prune(folder: Path, keep: int) -> list[Path]:
    """Delete the oldest automatic backups beyond `keep`; returns what was deleted."""
    backups = sorted((p for p in folder.glob(f"{PREFIX}*") if p.suffix in (".json", ".ldmbackup")),
                     key=lambda p: p.name, reverse=True)
    removed = backups[keep:] if keep > 0 else []
    for path in removed:
        path.unlink(missing_ok=True)
    return removed
