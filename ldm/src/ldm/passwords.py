"""Passwords for site logins and the proxy: the desktop keyring (Secret Service), else a private file.

Items are looked up by (kind, key): ("login", host) or ("proxy", "proxy"). The calls block (the
keyring may ask to be unlocked), so the manager runs them in a thread and keeps the values in memory.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

APP = "ldm"


class MemoryPasswords:
    """In memory only (tests)."""

    where = "memory"

    def __init__(self):
        self.items: dict[tuple[str, str], str] = {}

    def get(self, kind: str, key: str) -> str | None:
        return self.items.get((kind, key))

    def set(self, kind: str, key: str, password: str) -> None:
        self.items[(kind, key)] = password

    def delete(self, kind: str, key: str) -> None:
        self.items.pop((kind, key), None)


class FilePasswords(MemoryPasswords):
    """A JSON file only the user can read: the fallback when no keyring service is running."""

    where = "file"

    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        if path.exists():
            data = json.loads(path.read_text())
            self.items = {(kind, key): value for kind, entries in data.items() for key, value in entries.items()}

    def set(self, kind: str, key: str, password: str) -> None:
        super().set(kind, key, password)
        self._save()

    def delete(self, kind: str, key: str) -> None:
        super().delete(kind, key)
        self._save()

    def _save(self) -> None:
        data: dict[str, dict[str, str]] = {}
        for (kind, key), value in self.items.items():
            data.setdefault(kind, {})[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)


class KeyringPasswords:
    """GNOME Keyring / KWallet through the Secret Service D-Bus API (python3-secretstorage)."""

    where = "keyring"

    def __init__(self):
        import secretstorage

        self.connection = secretstorage.dbus_init()
        self.collection = secretstorage.get_default_collection(self.connection)

    def _items(self, kind: str, key: str):
        return self.collection.search_items({"application": APP, "kind": kind, "key": key})

    def _unlocked(self) -> None:
        if self.collection.is_locked():
            self.collection.unlock()

    def get(self, kind: str, key: str) -> str | None:
        self._unlocked()
        for item in self._items(kind, key):
            return item.get_secret().decode()
        return None

    def set(self, kind: str, key: str, password: str) -> None:
        self._unlocked()
        self.collection.create_item(f"LDM {kind}: {key}", {"application": APP, "kind": kind, "key": key},
                                    password.encode(), replace=True)

    def delete(self, kind: str, key: str) -> None:
        self._unlocked()
        for item in self._items(kind, key):
            item.delete()


def open_store(fallback: Path):
    """The keyring if a Secret Service is running, else the private file next to the database."""
    try:
        return KeyringPasswords()
    except Exception as e:  # not installed, no session bus, no keyring daemon
        log.info("no keyring available, saving passwords in %s: %s", fallback, e)
        return FilePasswords(fallback)
