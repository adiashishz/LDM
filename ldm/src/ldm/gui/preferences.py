"""Preferences: daemon settings, schedule and per-host rules."""

from __future__ import annotations

from typing import Any

from gi.repository import Adw, Gio, GLib, Gtk

from ..format import human, parse_rate
from .schedule_rows import DaysRow, TimeRow
from ..backup import file_name as backup_file_name
from ..backup import is_encrypted
from .model import FINISH_ACTIONS

BACKUP_SCHEDULES = [("off", "Never"), ("daily", "Every day"), ("weekly", "Every week")]
from .tools import ask_login
from .client import DaemonClient
from .dialogs import ask_text, limit_text, short_path

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
    "torrent_announce_all": ("Contact all trackers", "At once, instead of one at a time"),
    "torrent_merge_duplicates": ("Add trackers to torrents already in the list", "When you add one again"),
    "torrent_tracker_list": ("Add a public tracker list", "Fetched daily from the address below; more peers"),
}
TORRENT_SPINS = {  # key -> (title, subtitle, min, max, step, digits)
    "torrent_listen_port": ("Listening port", "0 picks a random port", 0, 65535, 1, 0),
    "torrent_max_peers": ("Peers per torrent", "", 1, 500, 1, 0),
    "torrent_max_connections": ("Connections in total", "", 1, 2000, 1, 0),
    "torrent_seed_ratio": ("Stop seeding at ratio", "0 means no limit", 0, 100, 0.1, 1),
    "torrent_seed_minutes": ("Stop seeding after (minutes)", "0 means no limit", 0, 100000, 10, 0),
}

SYSTEM_SWITCHES = {
    "pause_on_metered": ("Pause on metered connections", "Mobile data, hotspots and Wi-Fi marked as metered"),
    "prevent_sleep": ("Keep the computer awake", "While files are downloading"),
    "autostart": ("Start the download service at login", "Scheduled downloads run even if LDM isn't open"),
    "notify_when_closed": ("Notifications when LDM is closed", "For finished and failed downloads"),
    "tray_icon": ("Show progress in the tray", "While downloading, on desktops with a tray (GNOME needs the "
                                                "AppIndicator extension)"),
    "clipboard_watch": ("Offer to download copied links", "When LDM gets focus, and in the background "
                                                          "on desktops that allow it"),
}
NETWORK_SWITCHES = {
    "always_retry": ("Always retry", "Keep retrying failed downloads instead of stopping after the retries above"),
    "restart_non_resumable": ("Start over when resuming isn't possible", "For servers that can't continue a "
                                                                          "download; off: ask first"),
}
ADDING_SWITCHES = {
    "skip_add_dialog": ("Add without asking", "Links from the browser, the clipboard and drag-and-drop start at "
                                               "once with the defaults"),
    "open_html_in_browser": ("Offer to open web pages in the browser", "When a link is a page, not a file"),
}
FINISHED_SWITCHES = {
    "remove_finished": ("Remove finished downloads from the list", "The files stay where they are"),
    "cleanup_finished_only": ("Clean up only finished downloads", "Otherwise unfinished ones are removed too, "
                                                                   "with their partial data"),
}
PROXY_MODES = [("system", "System settings"), ("none", "No proxy"), ("manual", "Manual")]
DNS_SERVERS = [("cloudflare", "Cloudflare (1.1.1.1)"), ("google", "Google (8.8.8.8)"), ("quad9", "Quad9 (9.9.9.9)"),
               ("custom", "Custom URL")]
USER_AGENTS = [("default", "LDM"), ("firefox", "Firefox"), ("chrome", "Chrome"), ("mobile", "Mobile (Android)"),
               ("custom", "Custom")]
SCHEDULE_SWITCHES = {
    "schedule_new_links": ("Put new downloads on the schedule", "You can still change it when adding"),
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
        system = Adw.PreferencesGroup(title="System")
        self.setting_rows: dict[str, Gtk.Widget] = {}  # simple switches and spins, bound in _bind_rows
        for key, (title, subtitle) in SYSTEM_SWITCHES.items():
            row = Adw.SwitchRow(title=title, subtitle=subtitle, sensitive=False)
            system.add(row)
            self.setting_rows[key] = row
        battery = Adw.SpinRow.new_with_range(0, 100, 5)
        battery.set_title("Pause on battery below (%)")
        battery.set_subtitle("0 means never")
        battery.set_sensitive(False)
        system.add(battery)
        self.setting_rows["battery_limit"] = battery
        self.clipboard_exclude = Adw.EntryRow(title="Never offer links ending in (e.g. html,php)",
                                              show_apply_button=True)
        self.clipboard_exclude.connect("apply", lambda row: self._save(
            {"clipboard_exclude": row.get_text().replace(" ", "")}))
        system.add(self.clipboard_exclude)
        adding = Adw.PreferencesGroup(title="Adding")
        finished = Adw.PreferencesGroup(title="Finished downloads")
        for group, switches in ((adding, ADDING_SWITCHES), (finished, FINISHED_SWITCHES)):
            for key, (title, subtitle) in switches.items():
                row = Adw.SwitchRow(title=title, subtitle=subtitle, sensitive=False)
                group.add(row)
                self.setting_rows[key] = row
        self.folder_rows = {}
        for key, title in (("move_finished_dir", "Move finished files to"),
                           ("move_finished_torrent_dir", "Move finished torrents to")):
            row = Adw.ActionRow(title=title, subtitle="Leave them where they are")
            choose = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Choose Folder", valign=Gtk.Align.CENTER)
            choose.add_css_class("flat")
            choose.connect("clicked", lambda _b, k=key: self._choose_move_folder(k))
            clear = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text="Don't Move", valign=Gtk.Align.CENTER)
            clear.add_css_class("flat")
            clear.connect("clicked", lambda _b, k=key: self._set_move_folder(k, ""))
            row.add_suffix(choose)
            row.add_suffix(clear)
            finished.add(row)
            self.folder_rows[key] = row
        self.finish = Adw.ComboRow(title="When all downloads finish", sensitive=False,
                                   subtitle="Suspend and power off wait 60 seconds and can be cancelled",
                                   model=Gtk.StringList.new([l for l, _ in FINISH_ACTIONS]))
        finished.add(self.finish)
        cleanup = Adw.SpinRow.new_with_range(0, 3650, 1)
        cleanup.set_title("Remove list entries after (days)")
        cleanup.set_subtitle("0 means never")
        cleanup.set_sensitive(False)
        finished.add(cleanup)
        self.setting_rows["cleanup_days"] = cleanup
        usage = Adw.PreferencesGroup(title="Data usage", description="Counted since the download service started.")
        self.data_limit = Adw.EntryRow(title="Limit per session (e.g. 2G, 0 = no limit)", show_apply_button=True)
        self.data_limit.connect("apply", self._apply_data_limit)
        self.data_used = Adw.ActionRow(title="Used this session", subtitle="…")
        reset = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER, tooltip_text="Start a new session")
        reset.connect("clicked", lambda *_: self.client.reset_data_usage(
            lambda r, e: self._report(r, e) if e else self.data_used.set_subtitle(human(0))))
        self.data_used.add_suffix(reset)
        usage.add(self.data_limit)
        usage.add(self.data_used)
        general.add(downloads)
        general.add(adding)
        general.add(usage)
        general.add(network)
        general.add(finished)
        general.add(video)
        general.add(system)

        self.network_page = network_page = Adw.PreferencesPage(title="Network", icon_name="network-wired-symbolic")
        proxy = Adw.PreferencesGroup(title="Proxy", description="Used for files, videos and torrents "
                                                                 "(torrents only with a manual proxy).")
        self.proxy_mode = Adw.ComboRow(title="Proxy", model=Gtk.StringList.new([l for _, l in PROXY_MODES]),
                                       sensitive=False)
        self.proxy_url = Adw.EntryRow(title="Proxy address (http://host:port or socks5://host:port)",
                                      show_apply_button=True)
        self.proxy_url.connect("apply", lambda row: self._save({"proxy_url": row.get_text().strip()}))
        self.proxy_user = Adw.EntryRow(title="Proxy user name (optional)", show_apply_button=True)
        self.proxy_user.connect("apply", lambda row: self._save({"proxy_user": row.get_text().strip()}))
        self.proxy_password = Adw.PasswordEntryRow(title="Proxy password", show_apply_button=True)
        self.proxy_password.connect("apply", lambda row: self._save({"proxy_password": row.get_text()}))
        for row in (self.proxy_mode, self.proxy_url, self.proxy_user, self.proxy_password):
            proxy.add(row)
        dns = Adw.PreferencesGroup(title="DNS", description="DNS-over-HTTPS looks up site names privately and "
                                                             "gets past DNS blocking. Used for LDM's own file "
                                                             "downloads (not videos or torrents), and not with a proxy.")
        self.doh = Adw.SwitchRow(title="Use DNS-over-HTTPS", sensitive=False)
        self.doh_server = Adw.ComboRow(title="Server", model=Gtk.StringList.new([l for _, l in DNS_SERVERS]))
        self.doh_url = Adw.EntryRow(title="DNS-over-HTTPS URL (https://…/dns-query)", show_apply_button=True)
        self.doh_url.connect("apply", lambda row: self._save({"doh_url": row.get_text().strip()}))
        for row in (self.doh, self.doh_server, self.doh_url):
            dns.add(row)
        connection = Adw.PreferencesGroup(title="Connection")
        for key, title in (("connect_timeout", "Connection timeout (seconds)"),
                           ("read_timeout", "Read timeout (seconds)")):
            row = Adw.SpinRow.new_with_range(1, 600, 1)
            row.set_title(title)
            row.set_sensitive(False)
            connection.add(row)
            self.setting_rows[key] = row
        self.user_agent = Adw.ComboRow(title="Identify as (user agent)",
                                       model=Gtk.StringList.new([l for _, l in USER_AGENTS]), sensitive=False)
        self.user_agent_custom = Adw.EntryRow(title="Custom user agent", show_apply_button=True)
        self.user_agent_custom.connect("apply", lambda row: row.get_text().strip()
                                       and self._save({"user_agent": row.get_text().strip()}))
        connection.add(self.user_agent)
        connection.add(self.user_agent_custom)
        for key, (title, subtitle) in NETWORK_SWITCHES.items():
            row = Adw.SwitchRow(title=title, subtitle=subtitle, sensitive=False)
            connection.add(row)
            self.setting_rows[key] = row
        for group in (proxy, dns, connection):
            network_page.add(group)

        self.schedule_page = schedule = Adw.PreferencesPage(title="Schedule", icon_name="alarm-symbolic")
        window = Adw.PreferencesGroup(
            title="Default schedule",
            description="Like 1DM's scheduler: downloads you put on the schedule (in the add dialog or "
                        "from a download's menu → Schedule…) start and stop at these times, unless you "
                        "give that download its own times.")
        self.times = {"schedule_start": TimeRow("Start at"),
                      "schedule_stop": TimeRow("Stop at", "Earlier than the start time runs overnight")}
        self.days = DaysRow()
        for row in (*self.times.values(), self.days):
            row.set_sensitive(False)
            window.add(row)
        for key, (title, subtitle) in SCHEDULE_SWITCHES.items():
            row = Adw.SwitchRow(title=title, subtitle=subtitle, sensitive=False)
            window.add(row)
            self.setting_rows[key] = row
        self.schedule_limit = Adw.EntryRow(title="Speed limit while scheduled (e.g. 2M, 0 = unlimited)",
                                           show_apply_button=True)
        self.schedule_limit.connect("apply", self._apply_schedule_limit)
        window.add(self.schedule_limit)
        schedule.add(window)

        self.sites_page = hosts = Adw.PreferencesPage(title="Sites", icon_name="network-server-symbolic")
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
        self.logins = Adw.PreferencesGroup(
            title="Logins", description="User names and passwords for sites that ask for them (HTTP login). "
                                        "Passwords are kept in your keyring.")
        add_login = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add Login", valign=Gtk.Align.CENTER)
        add_login.add_css_class("flat")
        add_login.connect("clicked", lambda *_: self._edit_login(None))
        self.logins.set_header_suffix(add_login)
        hosts.add(self.logins)
        self.login_rows: list[Gtk.Widget] = []

        torrents = Adw.PreferencesPage(title="Torrents", icon_name="network-workgroup-symbolic")
        network_group = Adw.PreferencesGroup(title="Network")
        seeding_group = Adw.PreferencesGroup(title="Seeding")
        behaviour = Adw.PreferencesGroup(title="Adding torrents")
        self.torrent_rows: dict[str, Gtk.Widget] = {}
        for key, (title, subtitle) in TORRENT_SWITCHES.items():
            row = Adw.SwitchRow(title=title, subtitle=subtitle, sensitive=False)
            group = (seeding_group if key == "torrent_seed" else
                     behaviour if key in ("torrent_fetch_metadata", "torrent_sequential", "torrent_merge_duplicates",
                                          "torrent_tracker_list") else network_group)
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
        self.tracker_list_url = Adw.EntryRow(title="Public tracker list address", show_apply_button=True)
        self.tracker_list_url.connect("apply", lambda row: self._save(
            {"torrent_tracker_list_url": row.get_text().strip()}))
        behaviour.add(self.tracker_list_url)
        network_group.add(self.ip_filter)
        for group in (behaviour, network_group, seeding_group):
            torrents.add(group)

        self.backup_page = backup_page = Adw.PreferencesPage(title="Backup", icon_name="document-save-symbolic")
        now = Adw.PreferencesGroup(title="Backup and restore",
                                   description="Settings, host rules, logins and the download list in one file. "
                                               "Saved passwords are only included in backups with a password.")
        backup_row = Adw.ActionRow(title="Back up now")
        backup_button = Gtk.Button(label="Back Up…", valign=Gtk.Align.CENTER)
        backup_button.connect("clicked", lambda *_: self._back_up_now())
        backup_row.add_suffix(backup_button)
        restore_row = Adw.ActionRow(title="Restore a backup", subtitle="Downloads already in the list are kept")
        restore_button = Gtk.Button(label="Restore…", valign=Gtk.Align.CENTER)
        restore_button.connect("clicked", lambda *_: self._restore())
        restore_row.add_suffix(restore_button)
        now.add(backup_row)
        now.add(restore_row)
        auto = Adw.PreferencesGroup(title="Automatic backups")
        self.backup_schedule = Adw.ComboRow(title="Back up", sensitive=False,
                                            model=Gtk.StringList.new([l for _, l in BACKUP_SCHEDULES]))
        self.backup_folder = Adw.ActionRow(title="Folder", subtitle="…")
        choose = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Choose Folder", valign=Gtk.Align.CENTER)
        choose.add_css_class("flat")
        choose.connect("clicked", lambda *_: self._choose_backup_folder())
        self.backup_folder.add_suffix(choose)
        keep = Adw.SpinRow.new_with_range(1, 100, 1)
        keep.set_title("Backups to keep")
        keep.set_sensitive(False)
        self.setting_rows["backup_keep"] = keep
        self.backup_password = Adw.PasswordEntryRow(title="Password for automatic backups (empty = none)",
                                                    show_apply_button=True)
        self.backup_password.connect("apply", lambda row: self._save({"backup_password": row.get_text()}))
        for row in (self.backup_schedule, self.backup_folder, keep, self.backup_password):
            auto.add(row)
        backup_page.add(now)
        backup_page.add(auto)

        self.add(general)
        self.add(network_page)
        self.add(schedule)
        self.add(torrents)
        self.add(hosts)
        self.add(backup_page)
        client.get_settings(self._loaded)
        self._reload_rules()
        self._reload_logins()

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
        self._bind_rows(self.setting_rows, settings)
        self.clipboard_exclude.set_text(settings["clipboard_exclude"])
        for key, row in self.times.items():
            row.set_time(settings[key])
            row.set_sensitive(True)
            row.connect("changed", lambda r, k=key: self._save({k: r.get_time()}))
        self.days.set_days(settings["schedule_days"])
        self.days.set_sensitive(True)
        self.days.connect("changed", lambda r: self._save({"schedule_days": r.get_days()}))
        self.schedule_limit.set_text(limit_text(settings["schedule_speed_limit"]))
        self._load_network(settings)
        self.data_limit.set_text(limit_text(settings["data_limit"]))
        schedules = [v for v, _ in BACKUP_SCHEDULES]
        self.backup_schedule.set_selected(schedules.index(settings["backup_schedule"]))
        self.backup_schedule.set_sensitive(True)
        self.backup_schedule.connect("notify::selected", lambda r, _p: self._save(
            {"backup_schedule": schedules[r.get_selected()]}))
        self.backup_folder.set_subtitle(short_path(settings.get("backup_folder", "")))
        if settings.get("backup_password_set"):
            self.backup_password.set_title("Password for automatic backups (saved — type to change)")
        actions = [v for _, v in FINISH_ACTIONS]
        self.finish.set_selected(actions.index(settings["finish_action"]))
        self.finish.set_sensitive(True)
        self.finish.connect("notify::selected", lambda r, _p: self._save({"finish_action": actions[r.get_selected()]}))
        self.data_used.set_subtitle(human(settings.get("data_used", 0)))
        for key, row in self.folder_rows.items():
            row.set_subtitle(short_path(settings[key]) if settings[key] else "Leave them where they are")
        self.quality.connect("notify::selected",
                             lambda r, _p: self._save({"media_quality": QUALITIES[r.get_selected()][0]}))
        self.container.connect("notify::selected",
                               lambda r, _p: self._save({"media_container": CONTAINERS[r.get_selected()][0]}))

    def _load_torrent_settings(self, settings: dict[str, Any]) -> None:
        self._bind_rows(self.torrent_rows, settings)
        self.encryption.set_selected(next((i for i, (v, _) in enumerate(ENCRYPTION)
                                           if v == settings["torrent_encryption"]), 0))
        self.encryption.set_sensitive(True)
        self.encryption.connect("notify::selected", lambda r, _p: self._save(
            {"torrent_encryption": ENCRYPTION[r.get_selected()][0]}))
        self.upload.set_text(limit_text(settings["torrent_upload_limit"]))
        self.trackers.set_text(" ".join(settings["torrent_trackers"].split()))
        self.ip_filter.set_text(settings["torrent_ip_filter"])
        self.tracker_list_url.set_text(settings["torrent_tracker_list_url"])

    def _bind_rows(self, rows: dict[str, Gtk.Widget], settings: dict[str, Any]) -> None:
        """Show the values of switch and spin rows, then save every change."""
        for key, row in rows.items():
            if isinstance(row, Adw.SwitchRow):
                row.set_active(settings[key])
                row.connect("notify::active", lambda r, _p, k=key: self._save({k: r.get_active()}))
            else:
                row.set_value(settings[key])
                is_float = isinstance(settings[key], float)
                row.connect("notify::value", lambda r, _p, k=key, f=is_float:
                            self._save({k: float(r.get_value()) if f else int(r.get_value())}))
            row.set_sensitive(True)

    def _apply_schedule_limit(self, row: Adw.EntryRow) -> None:
        try:
            self._save({"schedule_speed_limit": parse_rate(row.get_text() or "0")})
            row.remove_css_class("error")
        except ValueError:
            row.add_css_class("error")

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

    # --- network ---------------------------------------------------------------------------

    def _load_network(self, settings: dict[str, Any]) -> None:
        modes = [m for m, _ in PROXY_MODES]
        self.proxy_mode.set_selected(modes.index(settings["proxy_mode"]))
        self.proxy_url.set_text(settings["proxy_url"])
        self.proxy_user.set_text(settings["proxy_user"])
        if settings.get("proxy_password_set"):
            self.proxy_password.set_title("Proxy password (saved — type to change)")
        self.doh.set_active(settings["dns_mode"] == "doh")
        servers = [s for s, _ in DNS_SERVERS]
        custom = settings["doh_url"] not in servers
        self.doh_server.set_selected(servers.index("custom") if custom else servers.index(settings["doh_url"]))
        self.doh_url.set_text(settings["doh_url"] if custom else "")
        agents = [a for a, _ in USER_AGENTS]
        known = settings["user_agent"] in agents
        self.user_agent.set_selected(agents.index(settings["user_agent"]) if known else agents.index("custom"))
        self.user_agent_custom.set_text("" if known else settings["user_agent"])
        self._sync_network()
        for row in (self.proxy_mode, self.doh, self.doh_server, self.user_agent):
            row.set_sensitive(True)
        self.proxy_mode.connect("notify::selected", lambda r, _p: (
            self._save({"proxy_mode": modes[r.get_selected()]}), self._sync_network()))
        self.doh.connect("notify::active", lambda r, _p: (
            self._save({"dns_mode": "doh" if r.get_active() else "system"}), self._sync_network()))
        self.doh_server.connect("notify::selected", self._on_doh_server)
        self.user_agent.connect("notify::selected", self._on_user_agent)

    def _sync_network(self) -> None:
        manual = PROXY_MODES[self.proxy_mode.get_selected()][0] == "manual"
        for row in (self.proxy_url, self.proxy_user, self.proxy_password):
            row.set_visible(manual)
        self.doh_server.set_visible(self.doh.get_active())
        self.doh_url.set_visible(self.doh.get_active() and DNS_SERVERS[self.doh_server.get_selected()][0] == "custom")
        self.user_agent_custom.set_visible(USER_AGENTS[self.user_agent.get_selected()][0] == "custom")

    def _on_doh_server(self, row, _param) -> None:
        server = DNS_SERVERS[row.get_selected()][0]
        if server != "custom":
            self._save({"doh_url": server})
        self._sync_network()

    def _on_user_agent(self, row, _param) -> None:
        agent = USER_AGENTS[row.get_selected()][0]
        if agent != "custom":
            self._save({"user_agent": agent})
        self._sync_network()

    def _apply_data_limit(self, row: Adw.EntryRow) -> None:
        try:
            self._save({"data_limit": parse_rate(row.get_text() or "0")})
            row.remove_css_class("error")
        except ValueError:
            row.add_css_class("error")

    # --- backup ----------------------------------------------------------------------------

    def _back_up_now(self) -> None:
        dialog = Adw.AlertDialog(heading="Back Up", body="A password encrypts the backup and also saves your "
                                                         "site and proxy passwords in it. Leave it empty for a "
                                                         "plain backup.")
        password = Adw.PasswordEntryRow(title="Password (optional)")
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        rows.add_css_class("boxed-list")
        rows.append(password)
        dialog.set_extra_child(rows)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Choose File…")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda d, r: r == "save" and self._save_backup_to(password.get_text()))
        dialog.present(self)

    def _save_backup_to(self, password: str) -> None:
        chooser = Gtk.FileDialog(title="Save Backup", initial_name=backup_file_name(bool(password)))

        def done(d, result) -> None:
            try:
                path = d.save_finish(result).get_path()
            except GLib.Error:
                return
            self.client.create_backup(path, password, lambda saved, error: self._report(None, error) if error else
                                      self.add_toast(Adw.Toast(title=GLib.markup_escape_text(
                                          f"Backed up to {short_path(saved)}"))))

        chooser.save(self.get_root(), None, done)

    def _restore(self) -> None:
        chooser = Gtk.FileDialog(title="Restore Backup")

        def done(d, result) -> None:
            try:
                file = d.open_finish(result)
                encrypted = is_encrypted(file.load_contents(None)[1])
            except GLib.Error:
                return
            if encrypted:
                ask_text(self, "Backup Password", "This backup is protected by a password.", "Restore", "", "",
                         lambda password: self._restore_from(file.get_path(), password), password=True)
            else:
                self._restore_from(file.get_path(), "")

        chooser.open(self.get_root(), None, done)

    def _restore_from(self, path: str, password: str) -> None:
        def restored(counts, error) -> None:
            if error:
                self._report(None, error)
                return
            self.add_toast(Adw.Toast(title=f"Restored {counts['settings']} settings, {counts['host_rules']} host "
                                           f"rules, {counts['logins']} logins and {counts['downloads']} downloads"))
            self._reload_rules()
            self._reload_logins()

        self.client.restore_backup(path, password, restored)

    def _choose_backup_folder(self) -> None:
        chooser = Gtk.FileDialog(title="Backup Folder")

        def done(d, result) -> None:
            try:
                path = d.select_folder_finish(result).get_path()
            except GLib.Error:
                return
            self._save({"backup_dir": path})
            self.backup_folder.set_subtitle(short_path(path))

        chooser.select_folder(self.get_root(), None, done)

    def _choose_move_folder(self, key: str) -> None:
        dialog = Gtk.FileDialog(title="Move Finished Downloads To")

        def done(d, result) -> None:
            try:
                self._set_move_folder(key, d.select_folder_finish(result).get_path())
            except GLib.Error:
                pass

        dialog.select_folder(self.get_root(), None, done)

    def _set_move_folder(self, key: str, path: str) -> None:
        self._save({key: path})
        self.folder_rows[key].set_subtitle(short_path(path) if path else "Leave them where they are")

    # --- logins ----------------------------------------------------------------------------

    def _reload_logins(self) -> None:
        self.client.list_logins(self._show_logins)

    def _show_logins(self, logins: list[dict[str, Any]] | None, error) -> None:
        for row in self.login_rows:
            self.logins.remove(row)
        self.login_rows = []
        for login in logins or []:
            scope = "" if login["include_subdomains"] else " · exact host"
            row = Adw.ActionRow(title=login["host"], subtitle=login["username"] + scope, activatable=True)
            row.connect("activated", lambda _r, entry=login: self._edit_login(entry))
            delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove Login", valign=Gtk.Align.CENTER)
            delete.add_css_class("flat")
            delete.connect("clicked", lambda _b, host=login["host"]: self.client.remove_login(
                host, lambda *_: self._reload_logins()))
            row.add_suffix(delete)
            self.logins.add(row)
            self.login_rows.append(row)

    def _edit_login(self, login: dict[str, Any] | None) -> None:
        def save(host: str, user: str, password: str, subdomains: bool) -> None:
            self.client.set_login(host, user, password, subdomains,
                                  lambda r, e: self._report(r, e) if e else self._reload_logins())

        ask_login(self, login["host"] if login else "", login["username"] if login else "", save,
                  login["include_subdomains"] if login else True)

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
