"""Torrent details (1DM's TorrentDetails screen): overview, files, peers, trackers — refreshed live."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gi.repository import Adw, GLib, Gtk

from ..format import human
from .client import DaemonClient
from .model import DownloadItem

REFRESH_MS = 1000
MAX_PEERS_SHOWN = 40


class TorrentDetailsDialog(Adw.Dialog):
    def __init__(self, client: DaemonClient, item: DownloadItem):
        super().__init__(title=item.title, content_width=620, content_height=560)
        self.client = client
        self.item = item
        self.page = Adw.PreferencesPage()
        self.groups: list[Adw.PreferencesGroup] = []
        view = Adw.ToolbarView(content=self.page)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)
        self._timer = GLib.timeout_add(REFRESH_MS, self._refresh)
        self.connect("closed", lambda *_: GLib.source_remove(self._timer))
        self._refresh()

    def _refresh(self) -> bool:
        self.client.torrent_details(self.item.id, self._show)
        return GLib.SOURCE_CONTINUE

    def _show(self, info: dict[str, Any] | None, error) -> None:
        for group in self.groups:
            self.page.remove(group)
        self.groups = []
        if error or info is None:
            self._group("Details", [("Not available", error.message if error else "")])
            return
        state = "Seeding" if info.get("seeding") else self.item.state.capitalize()
        self._group("Overview", [
            ("State", state),
            ("Peers", f"{info.get('peers', 0)} connected · {info.get('seeds', 0)} seed"
                      + ("s" if info.get("seeds", 0) != 1 else "")),
            ("Speed", f"↓ {human(info.get('download_speed', 0))}/s · ↑ {human(info.get('upload_speed', 0))}/s"),
            ("Uploaded", f"{human(info.get('uploaded', 0))} · ratio {info.get('ratio', 0):.2f}"),
        ])
        files = info.get("files") or []
        if files:
            group = self._group(f"Files ({len(files)})", [])
            for f in files:
                row = Adw.ActionRow(title=GLib.markup_escape_text(Path(f["path"]).name),
                                    subtitle=f"{human(f['done'])} of {human(f['size'])}"
                                    + ("" if f.get("wanted", True) else " · skipped"))
                bar = Gtk.ProgressBar(fraction=f["done"] / f["size"] if f["size"] else 1.0,
                                      valign=Gtk.Align.CENTER, width_request=140)
                row.add_suffix(bar)
                group.add(row)
        peers = info.get("peer_list") or []
        if peers:
            self._group(f"Peers ({len(peers)})", [
                (p["ip"], f"{p['client'] or 'unknown client'} · {p['progress'] * 100:.0f}% · "
                          f"↓ {human(p['down'])}/s ↑ {human(p['up'])}/s")
                for p in sorted(peers, key=lambda p: -p["down"])[:MAX_PEERS_SHOWN]])
        trackers = info.get("trackers") or []
        if trackers:
            self._group(f"Trackers ({len(trackers)})", [(t["url"], t.get("message", "")) for t in trackers])

    def _group(self, title: str, rows: list[tuple[str, str]]) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=title)
        for name, value in rows:
            row = Adw.ActionRow(title=GLib.markup_escape_text(name), subtitle=GLib.markup_escape_text(value),
                                subtitle_selectable=True)
            row.add_css_class("property")
            group.add(row)
        self.page.add(group)
        self.groups.append(group)
        return group
