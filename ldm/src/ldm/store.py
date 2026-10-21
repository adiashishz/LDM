"""SQLite persistence for downloads and their parts.

The schema is meant to stay stable across the Python v1 and the planned Rust v2.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .segments import Part

# Applied in order; PRAGMA user_version records how many have run.
MIGRATIONS = [
    """
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
""",
    """
ALTER TABLE downloads ADD COLUMN position INTEGER;
ALTER TABLE downloads ADD COLUMN speed_limit INTEGER NOT NULL DEFAULT 0;
UPDATE downloads SET position = id;
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE host_rules (
    host               TEXT PRIMARY KEY,
    max_connections    INTEGER NOT NULL DEFAULT 0,
    max_downloads      INTEGER NOT NULL DEFAULT 0,
    include_subdomains INTEGER NOT NULL DEFAULT 1
);
""",
    """
ALTER TABLE downloads ADD COLUMN options_json TEXT NOT NULL DEFAULT '{}';
""",
    """
CREATE TABLE torrents (
    download_id     INTEGER PRIMARY KEY REFERENCES downloads(id) ON DELETE CASCADE,
    info_hash       TEXT,
    metadata        BLOB,
    resume_data     BLOB,
    priorities      TEXT NOT NULL DEFAULT '[]',
    sequential      INTEGER NOT NULL DEFAULT 0,
    uploaded        INTEGER NOT NULL DEFAULT 0,
    seeding_seconds INTEGER NOT NULL DEFAULT 0
);
""",
    # Schedules are per download now (defaults + own times); there is no global on/off switch.
    """
DELETE FROM settings WHERE key = 'schedule_enabled';
""",
    # Progress of downloads without byte-range parts (yt-dlp, torrents), saved when they stop.
    """
ALTER TABLE downloads ADD COLUMN done_bytes INTEGER NOT NULL DEFAULT 0;
""",
    # Site logins (1DM's credentials); passwords live in the keyring, not here.
    """
CREATE TABLE logins (
    host               TEXT PRIMARY KEY,
    username           TEXT NOT NULL,
    include_subdomains INTEGER NOT NULL DEFAULT 1
);
""",
]


def default_download_dir() -> str:
    return os.environ.get("XDG_DOWNLOAD_DIR") or str(Path.home() / "Downloads")


def default_db_path(name: str = "ldm.db") -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"
    return Path(data_home) / "ldm" / name


# Values stored as JSON in `settings`; missing keys fall back to these.
DEFAULT_SETTINGS: dict[str, Any] = {
    "max_parallel": 3,
    "default_connections": 8,
    "speed_limit": 0,        # bytes/s for all downloads together, 0 = unlimited
    "retry_count": 3,        # automatic re-queues after an error
    "retry_interval": 30,    # seconds before an automatic re-queue
    "download_dir": None,    # None = XDG download dir
    "media_quality": "best",  # "best", "audio" or a maximum height such as 1080
    "media_container": "mp4",  # container for merged audio+video: "mp4" or "mkv"
    "subtitles": "",          # subtitle languages to save next to videos, e.g. "en,hi"; "" = none
    "js_runtime": "",         # path to deno/node/bun for YouTube; "" = find automatically
    "torrent_listen_port": 0,          # 0 = random
    "torrent_dht": True,
    "torrent_lsd": True,               # local peer discovery
    "torrent_pex": True,               # peer exchange
    "torrent_upnp": True,              # UPnP and NAT-PMP port forwarding
    "torrent_utp": True,
    "torrent_encryption": "prefer",    # prefer / require / off
    "torrent_max_peers": 50,           # per torrent
    "torrent_max_connections": 200,    # all torrents together
    "torrent_upload_limit": 0,         # bytes/s, 0 = unlimited
    "torrent_seed": False,             # keep seeding after finishing
    "torrent_seed_ratio": 1.0,         # stop seeding at this upload/download ratio (0 = no limit)
    "torrent_seed_minutes": 0,         # stop seeding after this long (0 = no limit)
    "torrent_fetch_metadata": True,    # get a magnet's file list before adding it
    "torrent_trackers": "",            # extra trackers for every torrent, one per line
    "torrent_ip_filter": "",           # path to a P2P/DAT block list, "" = none
    "torrent_sequential": False,       # default for "download in order"
    # Default window for downloads on the schedule; each download can have its own times instead.
    "schedule_start": "02:00",
    "schedule_stop": "06:00",          # earlier than start = overnight; equal = whole day
    "schedule_days": "",               # "mon,tue,…"; "" = every day
    "schedule_speed_limit": 0,         # bytes/s for scheduled downloads, 0 = unlimited
    "schedule_new_links": False,       # new downloads go on the schedule
    "pause_on_metered": False,         # hold every download on metered connections
    "battery_limit": 0,                # hold downloads on battery below this %, 0 = off
    "prevent_sleep": True,             # keep the computer awake while downloading
    "autostart": True,                 # start the download service at login
    "notify_when_closed": True,        # desktop notifications from the service when LDM is closed
    "clipboard_watch": True,           # offer to download links you copy
    "clipboard_exclude": "",           # extensions never offered, e.g. "html,php"
    # Network (LDM's own HTTP requests; yt-dlp and torrents use the proxy and user agent too)
    "proxy_mode": "system",            # "system" (desktop settings / environment), "none", "manual"
    "proxy_url": "",                   # manual: http://host:port or socks5://host:port
    "proxy_user": "",                  # the password is kept in the keyring
    "dns_mode": "system",              # "system" or "doh" (DNS-over-HTTPS)
    "doh_url": "cloudflare",           # cloudflare / google / quad9 or a URL
    "connect_timeout": 15,             # seconds
    "read_timeout": 30,                # seconds without data before retrying
    "user_agent": "default",           # default / firefox / chrome / mobile or a custom string
    "always_retry": False,             # retry failed downloads without a limit
    "restart_non_resumable": True,     # resume = start over when the server can't continue
    # Adding and finished downloads
    "skip_add_dialog": False,          # links from the browser/clipboard/drops are added without asking
    "open_html_in_browser": True,      # a link that is a web page: offer to open it instead
    "move_finished_dir": "",           # move finished files here ("" = leave them)
    "move_finished_torrent_dir": "",   # same for torrents (after seeding)
    "remove_finished": False,          # remove finished downloads from the list (files stay)
    "cleanup_days": 0,                 # remove list entries older than this many days (0 = never)
    "cleanup_finished_only": True,     # … but only finished ones
    "data_limit": 0,                   # bytes per session (since the service started); 0 = no limit
    "finish_action": "nothing",        # when all downloads finished: nothing / quit / suspend / poweroff
    "tray_icon": True,                 # progress in the tray while downloading (where the desktop has one)
    "backup_schedule": "off",          # automatic backups: off / daily / weekly
    "backup_dir": "",                  # "" = ~/Documents/LDM Backups
    "backup_keep": 10,                 # automatic backups kept in that folder
    "backup_last": 0.0,                # unix time of the last automatic backup (bookkeeping)
}


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
    position: int
    speed_limit: int
    options: dict[str, Any]
    done_bytes: int = 0  # media/torrent progress when stopped (HTTP downloads keep it in `parts`)

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()

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
        self._migrate()

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            self.db.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")

    def add(self, url: str, dir: Path | str, *, name: str | None = None,
            headers: dict[str, str] | None = None, connections: int = 8,
            state: str = "queued", kind: str = "http", options: dict[str, Any] | None = None) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO downloads (url, state, dir, name, connections, headers_json, added_at, kind,"
                " options_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (url, state, str(Path(dir).expanduser().resolve()), name, connections,
                 json.dumps(headers or {}), time.time(), kind, json.dumps(options or {})),
            )
            self.db.execute("UPDATE downloads SET position = id WHERE id = ?", (cur.lastrowid,))
        return cur.lastrowid

    def delete(self, download_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM downloads WHERE id = ?", (download_id,))

    def move(self, download_id: int, position: int) -> None:
        self.update(download_id, position=position)

    def queued(self) -> list[DownloadRow]:
        rows = self.db.execute("SELECT * FROM downloads WHERE state = 'queued' ORDER BY position, id")
        return [_to_row(r) for r in rows]

    def requeue_interrupted(self) -> list[int]:
        """Downloads left `running` by a crash or kill go back to the queue."""
        ids = [r[0] for r in self.db.execute("SELECT id FROM downloads WHERE state IN ('running', 'processing')")]
        with self.db:
            self.db.execute("UPDATE downloads SET state = 'queued' WHERE state IN ('running', 'processing')")
        return ids

    def get_settings(self) -> dict[str, Any]:
        settings = dict(DEFAULT_SETTINGS)
        settings.update((k, json.loads(v)) for k, v in self.db.execute("SELECT key, value FROM settings"))
        if settings["download_dir"] is None:
            settings["download_dir"] = default_download_dir()
        return settings

    def set_setting(self, key: str, value: Any) -> None:
        if key not in DEFAULT_SETTINGS:
            raise KeyError(key)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                            (key, json.dumps(value)))

    def set_host_rule(self, host: str, *, max_connections: int = 0, max_downloads: int = 0,
                      include_subdomains: bool = True) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO host_rules (host, max_connections, max_downloads, include_subdomains)"
                " VALUES (?, ?, ?, ?)",
                (host.lower(), max_connections, max_downloads, int(include_subdomains)),
            )

    def remove_host_rule(self, host: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM host_rules WHERE host = ?", (host.lower(),))

    def list_host_rules(self) -> list[HostRule]:
        return [_to_rule(r) for r in self.db.execute("SELECT * FROM host_rules ORDER BY host")]

    def get_torrent(self, download_id: int) -> TorrentRow | None:
        r = self.db.execute("SELECT * FROM torrents WHERE download_id = ?", (download_id,)).fetchone()
        if r is None:
            return None
        return TorrentRow(r["download_id"], r["info_hash"], r["metadata"], r["resume_data"],
                          json.loads(r["priorities"]), bool(r["sequential"]), r["uploaded"], r["seeding_seconds"])

    def save_torrent(self, download_id: int, **fields) -> None:
        """Insert or update the torrent row; `priorities` is a list, the rest are column values."""
        if "priorities" in fields:
            fields["priorities"] = json.dumps(fields["priorities"])
        if "sequential" in fields:
            fields["sequential"] = int(fields["sequential"])
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO torrents (download_id) VALUES (?)", (download_id,))
            if fields:
                cols = ", ".join(f"{k} = ?" for k in fields)
                self.db.execute(f"UPDATE torrents SET {cols} WHERE download_id = ?", (*fields.values(), download_id))

    def host_rule_for(self, host: str) -> HostRule | None:
        row = self._for_host("host_rules", host)
        return _to_rule(row) if row else None

    def _for_host(self, table: str, host: str) -> sqlite3.Row | None:
        """Exact host first, then parent domains whose entry includes subdomains."""
        labels = host.lower().split(".")
        for i in range(len(labels)):
            candidate = ".".join(labels[i:])
            row = self.db.execute(f"SELECT * FROM {table} WHERE host = ?", (candidate,)).fetchone()
            if row and (i == 0 or row["include_subdomains"]):
                return row
        return None

    def set_login(self, host: str, username: str, include_subdomains: bool = True) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO logins (host, username, include_subdomains) VALUES (?, ?, ?)",
                            (host.lower(), username, int(include_subdomains)))

    def remove_login(self, host: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM logins WHERE host = ?", (host.lower(),))

    def list_logins(self) -> list[dict[str, Any]]:
        return [{"host": r["host"], "username": r["username"], "include_subdomains": bool(r["include_subdomains"])}
                for r in self.db.execute("SELECT * FROM logins ORDER BY host")]

    def login_for(self, host: str) -> tuple[str, str] | None:
        """(host the login is saved under, user name) for a request host."""
        row = self._for_host("logins", host)
        return (row["host"], row["username"]) if row else None

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


@dataclass
class TorrentRow:
    download_id: int
    info_hash: str | None
    metadata: bytes | None
    resume_data: bytes | None
    priorities: list[int]
    sequential: bool
    uploaded: int
    seeding_seconds: int


def _to_row(r: sqlite3.Row) -> DownloadRow:
    return DownloadRow(
        id=r["id"], url=r["url"], final_url=r["final_url"], kind=r["kind"], state=r["state"],
        dir=Path(r["dir"]), name=r["name"], size=r["size"], resumable=bool(r["resumable"]),
        connections=r["connections"], headers=json.loads(r["headers_json"]), etag=r["etag"],
        last_modified=r["last_modified"], added_at=r["added_at"], finished_at=r["finished_at"],
        error=r["error"], position=r["position"], speed_limit=r["speed_limit"],
        options=json.loads(r["options_json"]), done_bytes=r["done_bytes"],
    )


@dataclass
class HostRule:
    host: str
    max_connections: int  # 0 = no limit
    max_downloads: int    # 0 = no limit
    include_subdomains: bool


def _to_rule(r: sqlite3.Row) -> HostRule:
    return HostRule(r["host"], r["max_connections"], r["max_downloads"], bool(r["include_subdomains"]))
