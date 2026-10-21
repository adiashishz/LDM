"""Preferences: daemon settings and per-host rules."""

from __future__ import annotations

from typing import Any

from gi.repository import Adw, Gio, GLib, Gtk

from ..format import human, parse_rate
from .client import DaemonClient
from .dialogs import limit_text, short_path

QUALITIES = [("best", "Best available"), (2160, "2160p (4K)"), (1440, "1440p"), (1080, "1080p"), (720, "720p"),
             (480, "480p"), (360, "360p"), ("audio", "Audio only")]
CONTAINERS = [("mp4", "MP4 (plays everywhere)"), ("mkv", "MKV (keeps every codec)")]

ENCRYPTION = [("prefer", "Prefer encrypted connections"), ("require", "Encrypted connections only"),
              ("off", "No encryption")]
TORRENT_SWITCHES = {
    "torrent_dht": ("DHT", "Find peers without trackers"),
    "torrent_pex": ("Peer exchange", "Learn about peers from other peers"),
    "torrent_lsd": ("Local peer discovery", "Find peers on your local network"),
    "torrent_upnp": ("Port forwarding", "Open the port on your router with UPnP / NAT-PMP"),
    "torrent_utp": ("µTP", "Low-latency transport that yields to other traffic"),
    "torrent_fetch_metadata": ("Get file list before adding", "For magnet links"),
    "torrent_sequential": ("Download in order by default", "Lets you play videos while they download"),
    "torrent_seed": ("Seed after finishing", "Keep uploading to other peers"),
}
TORRENT_SPINS = {  # key -> (title, subtitle, min, max, step, digits)
    "torrent_listen_port": ("Listening port", "0 picks a random port", 0, 65535, 1, 0),
    "torrent_max_peers": ("Peers per torrent", "", 1, 500, 1, 0),
    "torrent_max_connections": ("Connections in total", "", 1, 2000, 1, 0),
    "torrent_seed_ratio": ("Stop seeding at ratio", "0 means no limit", 0, 100, 0.1, 1),
    "torrent_seed_minutes": ("Stop seeding after (minutes)", "0 means no limit", 0, 100000, 10, 0),
}

# setting key -> (title, subtitle, minimum, maximum)
SPIN_SETTINGS = {
    "max_parallel": ("Parallel downloads", "How many files download at the same time", 1, 20),
    "default_connections": ("Connections per download", "Default for new downloads", 1, 32),
    "retry_count": ("Automatic retries", "After a failed download", 0, 20),
    "retry_interval": ("Retry delay", "Seconds before retrying", 0, 3600),
}


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, client: DaemonClient):
        super().__init__(title="Preferences")
        self.client = client
        self.folder: str | None = None
        self.spins: dict[str, Adw.SpinRow] = {}

        general = Adw.PreferencesPage(title="General", icon_name="emblem-system-symbolic")
        downloads = Adw.PreferencesGroup(title="Downloads")
        self.folder_row = Adw.ActionRow(title="Download folder")
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", self._choose_folder)
        self.folder_row.add_suffix(choose)
        downloads.add(self.folder_row)
        network = Adw.PreferencesGroup(title="Network")
        self.limit = Adw.EntryRow(title="Speed limit for all downloads (e.g. 2M, 0 = unlimited)",
                                  show_apply_button=True)
        self.limit.connect("apply", self._apply_limit)
        network.add(self.limit)
        for key, (title, subtitle, low, high) in SPIN_SETTINGS.items():
            row = Adw.SpinRow.new_with_range(low, high, 1)
            row.set_title(title)
            row.set_subtitle(subtitle)
            row.set_sensitive(False)  # until settings are loaded
            (downloads if key in ("max_parallel", "default_connections") else network).add(row)
            self.spins[key] = row
        video = Adw.PreferencesGroup(title="Video", description="Streams and sites such as YouTube, via yt-dlp")
        self.quality = Adw.ComboRow(title="Default quality",
                                    model=Gtk.StringList.new([label for _, label in QUALITIES]))
        self.container = Adw.ComboRow(title="Merge audio and video into",
                                      model=Gtk.StringList.new([label for _, label in CONTAINERS]))
        self.subtitles = Adw.EntryRow(title="Subtitle languages (e.g. en,hi — empty: none)", show_apply_button=True)
        self.subtitles.connect("apply", lambda row: self._save({"subtitles": row.get_text().replace(" ", "")}))
        self.runtime = Adw.EntryRow(title="JavaScript runtime for YouTube (empty: find automatically)",
                                    show_apply_button=True)
        self.runtime.connect("apply", lambda row: self._save({"js_runtime": row.get_text().strip()}))
        self.runtime_found = Adw.ActionRow(title="Runtime in use", subtitle="…")
        for row in (self.quality, self.container, self.subtitles, self.runtime, self.runtime_found):
            row.set_sensitive(False)
            video.add(row)
        general.add(downloads)
        general.add(network)
        general.add(video)

        hosts = Adw.PreferencesPage(title="Host Rules", icon_name="network-server-symbolic")
        self.rules = Adw.PreferencesGroup(
            title="Host Rules",
            description="Limit connections or parallel downloads for a site. Rules apply to subdomains "
                        "unless marked exact.")
        add_rule = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add Rule", valign=Gtk.Align.CENTER)
        add_rule.add_css_class("flat")
        add_rule.connect("clicked", lambda *_: self._edit_rule(None))
        self.rules.set_header_suffix(add_rule)
        hosts.add(self.rules)
        self.rule_rows: list[Gtk.Widget] = []

        torrents = Adw.PreferencesPage(title="Torrents", icon_name="network-workgroup-symbolic")
        network_group = Adw.PreferencesGroup(title="Network")
        seeding_group = Adw.PreferencesGroup(title="Seeding")
        behaviour = Adw.PreferencesGroup(title="Adding torrents")
        self.torrent_rows: dict[str, Gtk.Widget] = {}
        for key, (title, subtitle) in TORRENT_SWITCHES.items():
            row = Adw.SwitchRow(title=title, subtitle=subtitle, sensitive=False)
            group = (seeding_group if key == "torrent_seed" else
                     behaviour if key in ("torrent_fetch_metadata", "torrent_sequential") else network_group)
            group.add(row)
            self.torrent_rows[key] = row
        for key, (title, subtitle, low, high, step, digits) in TORRENT_SPINS.items():
            row = Adw.SpinRow.new_with_range(low, high, step)
            row.set_digits(digits)
            row.set_title(title)
            row.set_subtitle(subtitle)
            row.set_sensitive(False)
            (seeding_group if "seed" in key else network_group).add(row)
            self.torrent_rows[key] = row
        self.encryption = Adw.ComboRow(title="Encryption", sensitive=False,
                                       model=Gtk.StringList.new([label for _, label in ENCRYPTION]))
        network_group.add(self.encryption)
        self.upload = Adw.EntryRow(title="Upload speed limit (e.g. 500K, 0 = unlimited)", show_apply_button=True)
        self.upload.connect("apply", self._apply_upload)
        seeding_group.add(self.upload)
        self.trackers = Adw.EntryRow(title="Extra trackers for every torrent (separate with spaces)",
                                     show_apply_button=True)
        self.trackers.connect("apply", lambda row: self._save(
            {"torrent_trackers": "\n".join(row.get_text().split())}))
        self.ip_filter = Adw.EntryRow(title="IP block list file (P2P or DAT, empty = none)", show_apply_button=True)
        self.ip_filter.connect("apply", lambda row: self._save({"torrent_ip_filter": row.get_text().strip()}))
        behaviour.add(self.trackers)
        network_group.add(self.ip_filter)
        for group in (behaviour, network_group, seeding_group):
            torrents.add(group)

        self.add(general)
        self.add(torrents)
        self.add(hosts)
        client.get_settings(self._loaded)
        self._reload_rules()

    # --- settings --------------------------------------------------------------------------

    def _loaded(self, settings: dict[str, Any] | None, error) -> None:
        if error:
            self.add_toast(Adw.Toast(title=GLib.markup_escape_text(error.message)))
            return
        self.folder = settings["download_dir"]
        self.folder_row.set_subtitle(short_path(self.folder))
        self.limit.set_text(limit_text(settings["speed_limit"]))
        for key, row in self.spins.items():
            row.set_value(settings[key])
            row.set_sensitive(True)
            row.connect("notify::value", lambda r, _p, k=key: self._save({k: int(r.get_value())}))
        quality = settings["media_quality"]
        quality = int(quality) if str(quality).isdigit() else quality
        self.quality.set_selected(next((i for i, (v, _) in enumerate(QUALITIES) if v == quality), 0))
        self.container.set_selected(next((i for i, (v, _) in enumerate(CONTAINERS)
                                          if v == settings["media_container"]), 0))
        self.subtitles.set_text(settings["subtitles"])
        self.runtime.set_text(settings["js_runtime"])
        self.runtime_found.set_subtitle(settings.get("js_runtime_detected")
                                        or "None found — YouTube needs deno, node or bun")
        for row in (self.quality, self.container, self.subtitles, self.runtime, self.runtime_found):
            row.set_sensitive(True)
        self._load_torrent_settings(settings)
        self.quality.connect("notify::selected",
                             lambda r, _p: self._save({"media_quality": QUALITIES[r.get_selected()][0]}))
        self.container.connect("notify::selected",
                               lambda r, _p: self._save({"media_container": CONTAINERS[r.get_selected()][0]}))

    def _load_torrent_settings(self, settings: dict[str, Any]) -> None:
        for key, row in self.torrent_rows.items():
            if isinstance(row, Adw.SwitchRow):
                row.set_active(settings[key])
                row.connect("notify::active", lambda r, _p, k=key: self._save({k: r.get_active()}))
            else:
                row.set_value(settings[key])
                is_float = isinstance(settings[key], float)
                row.connect("notify::value", lambda r, _p, k=key, f=is_float:
                            self._save({k: float(r.get_value()) if f else int(r.get_value())}))
            row.set_sensitive(True)
        self.encryption.set_selected(next((i for i, (v, _) in enumerate(ENCRYPTION)
                                           if v == settings["torrent_encryption"]), 0))
        self.encryption.set_sensitive(True)
        self.encryption.connect("notify::selected", lambda r, _p: self._save(
            {"torrent_encryption": ENCRYPTION[r.get_selected()][0]}))
        self.upload.set_text(limit_text(settings["torrent_upload_limit"]))
        self.trackers.set_text(" ".join(settings["torrent_trackers"].split()))
        self.ip_filter.set_text(settings["torrent_ip_filter"])

    def _apply_upload(self, row: Adw.EntryRow) -> None:
        try:
            self._save({"torrent_upload_limit": parse_rate(row.get_text() or "0")})
            row.remove_css_class("error")
        except ValueError:
            row.add_css_class("error")

    def _save(self, values: dict[str, Any]) -> None:
        self.client.set_settings(values, self._report)

    def _report(self, _result, error) -> None:
        if error:
            self.add_toast(Adw.Toast(title=GLib.markup_escape_text(error.message)))

    def _apply_limit(self, row: Adw.EntryRow) -> None:
        try:
            rate = parse_rate(row.get_text() or "0")
        except ValueError:
            row.add_css_class("error")
            return
        row.remove_css_class("error")
        self._save({"speed_limit": rate})
        self.add_toast(Adw.Toast(title=f"Speed limit: {human(rate) + '/s' if rate else 'unlimited'}"))

    def _choose_folder(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Download Folder")
        if self.folder:
            dialog.set_initial_folder(Gio.File.new_for_path(self.folder))

        def done(d, result):
            try:
                folder = d.select_folder_finish(result)
            except GLib.Error:
                return
            self.folder = folder.get_path()
            self.folder_row.set_subtitle(short_path(self.folder))
            self._save({"download_dir": self.folder})

        dialog.select_folder(self.get_root(), None, done)

    # --- host rules ------------------------------------------------------------------------

    def _reload_rules(self) -> None:
        self.client.list_host_rules(self._show_rules)

    def _show_rules(self, rules: list[dict[str, Any]] | None, error) -> None:
        for row in self.rule_rows:
            self.rules.remove(row)
        self.rule_rows = []
        for rule in rules or []:
            limits = []
            if rule["max_connections"]:
                limits.append(f"{rule['max_connections']} connections")
            if rule["max_downloads"]:
                limits.append(f"{rule['max_downloads']} at a time")
            scope = "includes subdomains" if rule["include_subdomains"] else "exact host"
            row = Adw.ActionRow(title=rule["host"], subtitle=" · ".join(limits + [scope]), activatable=True)
            row.connect("activated", lambda _r, r=rule: self._edit_rule(r))
            delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove Rule", valign=Gtk.Align.CENTER)
            delete.add_css_class("flat")
            delete.connect("clicked", lambda _b, host=rule["host"]: self.client.remove_host_rule(
                host, lambda *_: self._reload_rules()))
            row.add_suffix(delete)
            self.rules.add(row)
            self.rule_rows.append(row)

    def _edit_rule(self, rule: dict[str, Any] | None) -> None:
        dialog = Adw.AlertDialog(heading="Edit Host Rule" if rule else "Add Host Rule")
        form = Adw.PreferencesGroup()
        host = Adw.EntryRow(title="Host (e.g. example.com)", text=rule["host"] if rule else "")
        connections = Adw.SpinRow.new_with_range(0, 32, 1)
        connections.set_title("Max connections per download")
        connections.set_subtitle("0 = no limit")
        downloads = Adw.SpinRow.new_with_range(0, 20, 1)
        downloads.set_title("Max parallel downloads")
        downloads.set_subtitle("0 = no limit")
        subdomains = Adw.SwitchRow(title="Include subdomains", active=rule["include_subdomains"] if rule else True)
        if rule:
            connections.set_value(rule["max_connections"])
            downloads.set_value(rule["max_downloads"])
        for row in (host, connections, downloads, subdomains):
            form.add(row)
        dialog.set_extra_child(form)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")

        def respond(_d, response):
            if response != "save":
                return
            name = host.get_text().strip().lower()
            if rule and name != rule["host"]:
                self.client.remove_host_rule(rule["host"])
            self.client.set_host_rule(name, int(connections.get_value()), int(downloads.get_value()),
                                      subdomains.get_active(),
                                      lambda _r, error: (self._report(_r, error), self._reload_rules()))

        dialog.connect("response", respond)
        dialog.present(self)
