"""Export and import downloads as JSON (1DM: "export/import unfinished downloads"; M9 backups reuse it).

Partial data stays where it is: an imported download picks up its `.part` file (or video temp folder)
when it is still there, and starts over otherwise.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from .media import media_temp_dir
from .store import DownloadRow, Store

FORMAT = "ldm-downloads"
VERSION = 1
UNFINISHED = {"queued", "running", "processing", "paused", "error", "expired", "changed"}


def export_downloads(store: Store, unfinished_only: bool = True) -> dict[str, Any]:
    downloads = []
    for d in store.list():
        if unfinished_only and d.state not in UNFINISHED:
            continue
        entry = {
            "id": d.id, "url": d.url, "final_url": d.final_url, "kind": d.kind, "state": d.state,
            "dir": str(d.dir), "name": d.name, "size": d.size, "resumable": d.resumable,
            "connections": d.connections, "headers": d.headers, "etag": d.etag,
            "last_modified": d.last_modified, "added_at": d.added_at, "finished_at": d.finished_at,
            "speed_limit": d.speed_limit, "options": d.options, "done_bytes": d.done_bytes,
            "parts": [[p.first, p.last, p.done] for p in store.load_parts(d.id)],
        }
        t = store.get_torrent(d.id) if d.kind == "torrent" else None
        if t is not None:
            entry["torrent"] = {"metadata": _b64(t.metadata), "resume_data": _b64(t.resume_data),
                                "priorities": t.priorities, "sequential": t.sequential}
        downloads.append(entry)
    return {"format": FORMAT, "version": VERSION, "downloads": downloads}


def import_downloads(store: Store, data: dict[str, Any] | str) -> list[int]:
    """Add the downloads (paused, or done if they were finished); returns the new ids."""
    if isinstance(data, str):
        data = json.loads(data)
    if data.get("format") != FORMAT or not isinstance(data.get("downloads"), list):
        raise ValueError("not an LDM downloads file")
    if data.get("version", 0) > VERSION:
        raise ValueError("this file was made by a newer LDM")
    ids = []
    for entry in data["downloads"]:
        state = "done" if entry.get("state") == "done" else "paused"
        new_id = store.add(entry["url"], entry["dir"], name=entry.get("name"), headers=entry.get("headers") or {},
                           connections=entry.get("connections", 8), state=state, kind=entry.get("kind", "http"),
                           options=entry.get("options") or {})
        store.update(new_id, final_url=entry.get("final_url"), size=entry.get("size"),
                     resumable=bool(entry.get("resumable")), etag=entry.get("etag"),
                     last_modified=entry.get("last_modified"), speed_limit=entry.get("speed_limit", 0),
                     done_bytes=entry.get("done_bytes", 0), finished_at=entry.get("finished_at"))
        d = store.get(new_id)
        if state != "done":
            _adopt_partial_data(store, d, entry)
        if "torrent" in entry:
            t = entry["torrent"]
            store.save_torrent(new_id, metadata=_unb64(t.get("metadata")), resume_data=_unb64(t.get("resume_data")),
                               priorities=t.get("priorities", []), sequential=bool(t.get("sequential")))
        ids.append(new_id)
    return ids


def _adopt_partial_data(store: Store, d: DownloadRow, entry: dict[str, Any]) -> None:
    """Partial files are named after the download id: move the old id's data to the new one."""
    old = DownloadRow(**{**vars(d), "id": entry.get("id", d.id)})
    if d.kind == "http" and d.name:
        if old.temp_path.exists():
            if old.temp_path != d.temp_path:
                os.replace(old.temp_path, d.temp_path)
            store.save_parts(d.id, [tuple(p) for p in entry.get("parts", [])])
        else:
            store.update(d.id, final_url=None)  # nothing downloaded here: probe again and start over
    elif d.kind == "media" and media_temp_dir(old).is_dir() and media_temp_dir(old) != media_temp_dir(d):
        os.replace(media_temp_dir(old), media_temp_dir(d))


def _b64(data: bytes | None) -> str | None:
    return base64.b64encode(data).decode() if data else None


def _unb64(text: str | None) -> bytes | None:
    return base64.b64decode(text) if text else None
