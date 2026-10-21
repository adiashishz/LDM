"""AES-128 keys for encrypted HLS streams (1DM's "m3u8 key base64"); no yt-dlp import, the GUI uses it."""

from __future__ import annotations

import base64
import binascii
import re


def aes_hex(text: str, name: str = "key") -> str:
    """A 16-byte AES-128 key or IV as hex, from base64 (as 1DM takes it) or hex ("0x…" allowed)."""
    value = text.strip()
    digits = value[2:] if value.lower().startswith("0x") else value
    if re.fullmatch(r"[0-9a-fA-F]{32}", digits):
        return digits.lower()
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != 16:
        raise ValueError(f"the {name} must be 16 bytes: 32 hex digits or 24 base64 characters")
    return raw.hex()
