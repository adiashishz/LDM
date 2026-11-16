"""The local channel between the service and its clients, where there is no D-Bus (Windows, macOS) and next
to D-Bus on Linux.

Linux and macOS: a Unix socket only the user can open, `$XDG_RUNTIME_DIR/ldm/service.sock` (macOS: in the
user's temporary folder). Windows: a TCP connection on 127.0.0.1; the port and a secret token are in a file
only the user can read (`%LOCALAPPDATA%\\ldm\\service.json`), and a client must send the token first.

Messages are JSON, one per line (UTF-8):
    → {"hello": TOKEN}                                    first, on TCP only
    → {"id": 1, "method": "Add", "params": [...]}         a call, by its D-Bus method name
    ← {"id": 1, "result": ...}  or  {"id": 1, "error": {"name": "NotFound", "message": "..."}}
    ← {"signal": "Progress", "args": [...]}               to every client
Byte strings (a torrent's metadata) travel as {"$bytes": BASE64}.

Standard library only: the window and other clients use it without loading the download manager.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any

MAX_LINE = 64 * 1024 * 1024  # a message with a large .torrent's metadata still fits
TCP = sys.platform == "win32"


def runtime_folder() -> Path:
    """The user's private folder for the socket (or, on Windows, the port and token file)."""
    if TCP:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ldm"
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / "ldm"


def socket_path() -> Path:
    """Where the service listens (Linux, macOS). LDM_SOCKET overrides it."""
    return Path(os.environ.get("LDM_SOCKET") or runtime_folder() / "service.sock")


def tcp_file() -> Path:
    """Windows: the port and token of the running service. LDM_SOCKET overrides it."""
    return Path(os.environ.get("LDM_SOCKET") or runtime_folder() / "service.json")


def private_folder(path: Path) -> None:
    """The user's own folder for the channel (on Windows, LocalAppData is private already; chmod is harmless)."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def write_tcp_file(path: Path, port: int) -> str:
    """Write the port and a new token, readable by the user only; returns the token."""
    token = secrets.token_hex(32)
    private_folder(path.parent)
    temporary = path.with_suffix(".tmp")
    temporary.touch(mode=0o600)
    os.chmod(temporary, 0o600)  # before the token is written into it
    temporary.write_text(json.dumps({"port": port, "token": token}))
    os.replace(temporary, path)
    return token


def read_tcp_file(path: Path) -> tuple[int, str]:
    data = json.loads(path.read_text())
    return int(data["port"]), str(data["token"])


def _default(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return {"$bytes": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (set, tuple)):
        return list(value)
    raise TypeError(f"cannot send {type(value).__name__}")


def _object(value: dict) -> Any:
    if len(value) == 1 and "$bytes" in value:
        return base64.b64decode(value["$bytes"])
    return value


def encode(message: dict) -> bytes:
    return json.dumps(message, default=_default, separators=(",", ":")).encode() + b"\n"


def decode(line: bytes) -> dict:
    message = json.loads(line, object_hook=_object)
    if not isinstance(message, dict):
        raise ValueError("a message must be a JSON object")
    return message
