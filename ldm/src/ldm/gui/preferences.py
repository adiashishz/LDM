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

        self.add(general)
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
        self.quality.connect("notify::selected",
                             lambda r, _p: self._save({"media_quality": QUALITIES[r.get_selected()][0]}))
        self.container.connect("notify::selected",
                               lambda r, _p: self._save({"media_container": CONTAINERS[r.get_selected()][0]}))

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
