"""SQLite persistence for downloads and their parts.

The schema is meant to stay stable across the Python v1 and the planned Rust v2.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .segments import Part

SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id            INTEGER PRIMARY KEY,
    url           TEXT    NOT NULL,
    final_url     TEXT,
    kind          TEXT    NOT NULL DEFAULT 'http',
    state         TEXT    NOT NULL,
    dir           TEXT    NOT NULL,
    name          TEXT,
    size          INTEGER,
    resumable     INTEGER NOT NULL DEFAULT 0,
    connections   INTEGER NOT NULL DEFAULT 8,
    headers_json  TEXT    NOT NULL DEFAULT '{}',
    etag          TEXT,
    last_modified TEXT,
    added_at      REAL    NOT NULL,
    finished_at   REAL,
    error         TEXT
);
CREATE TABLE IF NOT EXISTS parts (
    download_id INTEGER NOT NULL REFERENCES downloads(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    first_byte  INTEGER NOT NULL,
    last_byte   INTEGER NOT NULL,
    done        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (download_id, idx)
);
PRAGMA user_version = 1;
"""


@dataclass
class DownloadRow:
    id: int
    url: str
    final_url: str | None
    kind: str
    state: str
    dir: Path
    name: str | None
    size: int | None
    resumable: bool
    connections: int
    headers: dict[str, str]
    etag: str | None
    last_modified: str | None
    added_at: float
    finished_at: float | None
    error: str | None

    @property
    def dest(self) -> Path:
        return self.dir / self.name

    @property
    def temp_path(self) -> Path:
        return self.dir / f"{self.name}.{self.id}.part"


class Store:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def add(self, url: str, dir: Path | str, *, name: str | None = None,
            headers: dict[str, str] | None = None, connections: int = 8) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO downloads (url, state, dir, name, connections, headers_json, added_at)"
                " VALUES (?, 'queued', ?, ?, ?, ?, ?)",
                (url, str(Path(dir).expanduser().resolve()), name, connections,
                 json.dumps(headers or {}), time.time()),
            )
        return cur.lastrowid

    def get(self, download_id: int) -> DownloadRow:
        row = self.db.execute("SELECT * FROM downloads WHERE id = ?", (download_id,)).fetchone()
        if row is None:
            raise KeyError(download_id)
        return _to_row(row)

    def list(self) -> list[DownloadRow]:
        return [_to_row(r) for r in self.db.execute("SELECT * FROM downloads ORDER BY id")]

    def update(self, download_id: int, **fields) -> DownloadRow:
        if "resumable" in fields:
            fields["resumable"] = int(fields["resumable"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.db:
            self.db.execute(f"UPDATE downloads SET {cols} WHERE id = ?", (*fields.values(), download_id))
        return self.get(download_id)

    def set_state(self, download_id: int, state: str, error: str | None = None) -> None:
        self.update(download_id, state=state, error=error)

    def finish(self, download_id: int, name: str, size: int) -> None:
        self.update(download_id, state="done", name=name, size=size, finished_at=time.time(), error=None)

    def save_parts(self, download_id: int, parts: list[tuple[int, int, int]]) -> None:
        """Replace the stored parts with (first, last, done) tuples."""
        with self.db:
            self.db.execute("DELETE FROM parts WHERE download_id = ?", (download_id,))
            self.db.executemany(
                "INSERT INTO parts (download_id, idx, first_byte, last_byte, done) VALUES (?, ?, ?, ?, ?)",
                [(download_id, i, *p) for i, p in enumerate(parts)],
            )

    def load_parts(self, download_id: int) -> list[Part]:
        rows = self.db.execute(
            "SELECT first_byte, last_byte, done FROM parts WHERE download_id = ? ORDER BY first_byte",
            (download_id,),
        )
        return [Part(r["first_byte"], r["last_byte"], r["done"]) for r in rows]


def _to_row(r: sqlite3.Row) -> DownloadRow:
    return DownloadRow(
        id=r["id"], url=r["url"], final_url=r["final_url"], kind=r["kind"], state=r["state"],
        dir=Path(r["dir"]), name=r["name"], size=r["size"], resumable=bool(r["resumable"]),
        connections=r["connections"], headers=json.loads(r["headers_json"]), etag=r["etag"],
        last_modified=r["last_modified"], added_at=r["added_at"], finished_at=r["finished_at"],
        error=r["error"],
    )
