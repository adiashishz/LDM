"""Settings: General, Network, Schedule, Torrents, Sites and Backup. Every change is saved at once."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLineEdit, QPushButton,
                               QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from ..backup import file_name as backup_file_name
from ..backup import is_encrypted
from ..format import human, parse_rate
from ..ui.prefs import (ADDING_SWITCHES, BACKUP_SCHEDULES, CONTAINERS, DNS_SERVERS, ENCRYPTION, FINISHED_SWITCHES,
                        NETWORK_SWITCHES, PROXY_MODES, QUALITIES, SCHEDULE_SWITCHES, SPIN_SETTINGS, SYSTEM_SWITCHES,
                        TORRENT_SPINS, TORRENT_SWITCHES, USER_AGENTS)
from ..ui.states import FINISH_ACTIONS
from ..ui.text import limit_text, short_path
from .forms import Card, ComboRow, Dialog, EntryRow, Page, Row, ScheduleRows, SpinRow, SwitchRow
from .sidebar import NavItem
from .theme import current, icon
from .tools import HostRuleDialog, LoginDialog

PAGES = [("general", "General", "settings"), ("network", "Network", "downloading"),
         ("schedule", "Schedule", "schedule"), ("torrents", "Torrents", "torrent"),
         ("sites", "Sites", "all"), ("backup", "Backup", "archive")]


class PreferencesDialog(Dialog):
    def __init__(self, parent: QWidget, client, page: str = "general"):
        super().__init__(parent, "Settings", 860, 680)
        self.client = client
        self.settings: dict[str, Any] = {}
        self.loaded: list[Callable[[dict[str, Any]], None]] = []  # fill a control once settings arrive

        # Pages on the left, like the main window's sidebar.
        self.page.hide()
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        nav = QFrame()
        nav.setObjectName("sidebar")
        nav.setFixedWidth(200)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(10, 12, 10, 12)
        nav_layout.setSpacing(1)
        self.stack = QStackedWidget()
        self.pages: dict[str, Page] = {}
        self.nav_items: dict[str, NavItem] = {}
        for key, label, icon_name in PAGES:
            item = NavItem(key, label, icon_name, None, current)
            item.count = ""
            item.clicked.connect(lambda _c=False, k=key: self.show_page(k))
            nav_layout.addWidget(item)
            self.nav_items[key] = item
            self.pages[key] = Page()
            self.stack.addWidget(self.pages[key])
        nav_layout.addStretch(1)
        body.addWidget(nav)
        body.addWidget(self.stack, 1)
        self.layout().insertLayout(1, body, 1)
        self.toast_widget.setParent(self.stack)

        self._general()
        self._network()
        self._schedule()
        self._torrents()
        self._sites()
        self._backup()
        for each in self.pages.values():
            each.setEnabled(False)  # until the settings arrive
        self.show_page(page)
        client.get_settings(self._loaded)
        self._reload_rules()
        self._reload_logins()

    def show_page(self, key: str) -> None:
        for name, item in self.nav_items.items():
            item.setChecked(name == key)
        self.stack.setCurrentWidget(self.pages[key])

    # --- saving -------------------------------------------------------------------------------

    def _save(self, values: dict[str, Any]) -> None:
        self.settings.update(values)
        self.client.set_settings(values, self._report)

    def _report(self, _result, error) -> None:
        if error:
            self.toast(error.message)

    def _loaded(self, settings: dict[str, Any] | None, error) -> None:
        if error or not settings:
            self.toast(error.message if error else "Could not read the settings")
            return
        self.settings = settings
        for fill in self.loaded:
            fill(settings)
        for each in self.pages.values():
            each.setEnabled(True)

    def switch(self, key: str, title: str, subtitle: str = "") -> SwitchRow:
        row = SwitchRow(title, subtitle)
        self.loaded.append(lambda s: (row.set_on(bool(s.get(key)), quiet=True),
                                      row.toggled.connect(lambda on: self._save({key: on}))))
        return row

    def spin(self, key: str, title: str, subtitle: str, low: float, high: float, step: float = 1,
             digits: int = 0) -> SpinRow:
        row = SpinRow(title, subtitle, low, high, step, digits)

        def fill(s: dict[str, Any]) -> None:
            row.set_value(s.get(key, low), quiet=True)
            row.changed.connect(lambda v: self._save({key: float(v) if digits else int(v)}))

        self.loaded.append(fill)
        return row

    def choice(self, key: str, title: str, choices: list[tuple[Any, str]], subtitle: str = "",
               after: Callable[[], None] | None = None) -> ComboRow:
        row = ComboRow(title, choices, subtitle)

        def fill(s: dict[str, Any]) -> None:
            value = s.get(key)
            value = int(value) if isinstance(value, str) and value.isdigit() else value
            row.set_value(value)
            row.chosen.connect(lambda v: (self._save({key: v}), after and after()))

        self.loaded.append(fill)
        return row

    def entry(self, key: str, title: str, placeholder: str = "", password: bool = False,
              save: Callable[[str], Any] = str.strip, show: Callable[[Any], str] = str) -> EntryRow:
        row = EntryRow(title, "", placeholder, password)

        def fill(s: dict[str, Any]) -> None:
            if not password:
                row.set_text(show(s.get(key, "")))
            row.applied.connect(lambda text: self._save({key: save(text)}))

        self.loaded.append(fill)
        return row

    def rate(self, key: str, title: str, placeholder: str = "e.g. 2M; 0 means unlimited") -> EntryRow:
        row = EntryRow(title, "", placeholder)

        def apply(text: str) -> None:
            try:
                value = parse_rate(text or "0")
            except ValueError:
                row.set_error(True)
                return
            row.set_error(False)
            self._save({key: value})

        self.loaded.append(lambda s: (row.set_text(limit_text(s.get(key, 0))), row.applied.connect(apply)))
        return row

    def folder(self, key: str, title: str, empty: str, clearable: bool = True) -> Row:
        choose = QPushButton("Choose…")
        row = Row(title, "", choose)

        def show(path: str) -> None:
            row.set_subtitle(short_path(path) if path else empty)

        def pick() -> None:
            path = QFileDialog.getExistingDirectory(self, title, self.settings.get(key) or str(Path.home()))
            if path:
                self._save({key: path})
                show(path)

        choose.clicked.connect(pick)
        if clearable:
            clear = QPushButton("Don't move")
            clear.clicked.connect(lambda: (self._save({key: ""}), show("")))
            row.add_control(clear)
        self.loaded.append(lambda s: show(s.get(key) or ""))
        return row

    # --- pages ----------------------------------------------------------------------------------

    def _general(self) -> None:
        page = self.pages["general"]
        downloads = Card("Downloads", "", self.folder("download_dir", "Download folder", "", clearable=False))
        for key in ("max_parallel", "default_connections"):
            title, subtitle, low, high = SPIN_SETTINGS[key]
            downloads.add(self.spin(key, title, subtitle, low, high))
        page.add(downloads)
        page.add(Card("Adding", "", *(self.switch(k, t, s) for k, (t, s) in ADDING_SWITCHES.items())))

        self.data_used = Row("Used this session", "…")
        reset = QPushButton("Reset")
        reset.setToolTip("Start a new session")
        reset.clicked.connect(lambda: self.client.reset_data_usage(
            lambda r, e: self._report(r, e) if e else self.data_used.set_subtitle(human(0))))
        self.data_used.add_control(reset)
        self.loaded.append(lambda s: self.data_used.set_subtitle(human(s.get("data_used", 0))))
        page.add(Card("Data usage", "Counted since the download service started.",
                      self.rate("data_limit", "Limit per session", "e.g. 2G; 0 means no limit"), self.data_used))

        network = Card("Speed and retries", "", self.rate("speed_limit", "Speed limit for all downloads"))
        for key in ("retry_count", "retry_interval"):
            title, subtitle, low, high = SPIN_SETTINGS[key]
            network.add(self.spin(key, title, subtitle, low, high))
        page.add(network)

        finished = Card("Finished downloads", "", *(self.switch(k, t, s) for k, (t, s) in FINISHED_SWITCHES.items()))
        finished.add(self.folder("move_finished_dir", "Move finished files to", "Leave them where they are"))
        finished.add(self.folder("move_finished_torrent_dir", "Move finished torrents to", "Leave them where they are"))
        finished.add(self.choice("finish_action", "When all downloads finish", [(v, l) for l, v in FINISH_ACTIONS],
                                 "Suspend and power off wait 60 seconds and can be cancelled"))
        finished.add(self.spin("cleanup_days", "Remove list entries after (days)", "0 means never", 0, 3650))
        page.add(finished)

        self.ytdlp_row = Row("yt-dlp", "…")
        update = QPushButton("Update")
        update.clicked.connect(lambda: self._update_ytdlp(update))
        self.ytdlp_row.add_control(update)
        runtime_found = Row("JavaScript runtime in use", "…")
        self.loaded.append(lambda s: (self._show_ytdlp(s), runtime_found.set_subtitle(
            s.get("js_runtime_detected") or "None found — YouTube needs deno, node or bun")))
        page.add(Card("Video", "Streams and sites such as YouTube, via yt-dlp",
                      self.choice("media_quality", "Default quality", QUALITIES),
                      self.choice("media_container", "Merge audio and video into", CONTAINERS),
                      self.entry("subtitles", "Subtitle languages", "e.g. en,hi; empty means none",
                                 save=lambda t: t.replace(" ", "")),
                      self.entry("js_runtime", "JavaScript runtime for YouTube", "empty: find automatically"),
                      runtime_found, self.ytdlp_row,
                      self.switch("ytdlp_auto_update", "Keep yt-dlp up to date",
                                  "Sites change often; checks GitHub for a new release once a day")))

        system = Card("System", "", *(self.switch(k, t, s) for k, (t, s) in SYSTEM_SWITCHES.items()))
        system.add(self.spin("battery_limit", "Pause on battery below (%)", "0 means never", 0, 100, 5))
        system.add(self.entry("clipboard_exclude", "Never offer links ending in", "e.g. html,php",
                              save=lambda t: t.replace(" ", "")))
        page.add(system)

    def _network(self) -> None:
        page = self.pages["network"]
        self.proxy_url = self.entry("proxy_url", "Proxy address", "http://host:port or socks5://host:port")
        self.proxy_user = self.entry("proxy_user", "Proxy user name (optional)")
        self.proxy_password = self.entry("proxy_password", "Proxy password", password=True, save=lambda t: t)
        self.proxy_mode = self.choice("proxy_mode", "Proxy", PROXY_MODES, after=self._sync_network)
        self.loaded.append(lambda s: s.get("proxy_password_set") and self.proxy_password.set_title(
            "Proxy password (saved — type to change)"))
        page.add(Card("Proxy", "Used for files, videos and torrents (torrents only with a manual proxy).",
                      self.proxy_mode, self.proxy_url, self.proxy_user, self.proxy_password))

        self.doh = SwitchRow("Use DNS-over-HTTPS")
        self.doh_server = ComboRow("Server", DNS_SERVERS)
        self.doh_url = EntryRow("DNS-over-HTTPS address", "", "https://…/dns-query")

        def fill_dns(s: dict[str, Any]) -> None:
            servers = [v for v, _ in DNS_SERVERS]
            custom = s.get("doh_url") not in servers
            self.doh.set_on(s.get("dns_mode") == "doh", quiet=True)
            self.doh_server.set_value("custom" if custom else s.get("doh_url"))
            self.doh_url.set_text(s.get("doh_url", "") if custom else "")
            self.doh.toggled.connect(lambda on: (self._save({"dns_mode": "doh" if on else "system"}),
                                                 self._sync_network()))
            self.doh_server.chosen.connect(lambda v: (v != "custom" and self._save({"doh_url": v}),
                                                      self._sync_network()))
            self.doh_url.applied.connect(lambda t: t.strip() and self._save({"doh_url": t.strip()}))

        self.loaded.append(fill_dns)
        page.add(Card("DNS", "DNS-over-HTTPS looks up site names privately and gets past DNS blocking. Used for "
                             "LDM's own file downloads (not videos or torrents), and not with a proxy.",
                      self.doh, self.doh_server, self.doh_url))

        self.user_agent = ComboRow("Identify as (user agent)", USER_AGENTS)
        self.user_agent_custom = EntryRow("Custom user agent")

        def fill_agent(s: dict[str, Any]) -> None:
            agents = [v for v, _ in USER_AGENTS]
            known = s.get("user_agent") in agents
            self.user_agent.set_value(s.get("user_agent") if known else "custom")
            self.user_agent_custom.set_text("" if known else s.get("user_agent", ""))
            self.user_agent.chosen.connect(lambda v: (v != "custom" and self._save({"user_agent": v}),
                                                      self._sync_network()))
            self.user_agent_custom.applied.connect(lambda t: t.strip() and self._save({"user_agent": t.strip()}))
            self._sync_network()

        self.loaded.append(fill_agent)
        connection = Card("Connection", "",
                          self.spin("connect_timeout", "Connection timeout (seconds)", "", 1, 600),
                          self.spin("read_timeout", "Read timeout (seconds)", "", 1, 600),
                          self.user_agent, self.user_agent_custom,
                          *(self.switch(k, t, s) for k, (t, s) in NETWORK_SWITCHES.items()))
        page.add(connection)
        self.network_cards = [c for c in page.findChildren(Card)]

    def _sync_network(self) -> None:
        manual = self.proxy_mode.value() == "manual"
        for row in (self.proxy_url, self.proxy_user, self.proxy_password):
            row.setVisible(manual)
        self.doh_server.setVisible(self.doh.is_on())
        self.doh_url.setVisible(self.doh.is_on() and self.doh_server.value() == "custom")
        self.user_agent_custom.setVisible(self.user_agent.value() == "custom")
        for card in self.network_cards:
            card.sync_lines()

    def _schedule(self) -> None:
        page = self.pages["schedule"]
        times = ScheduleRows()

        def fill(s: dict[str, Any]) -> None:
            times.load(s.get("schedule_start", "02:00"), s.get("schedule_stop", "06:00"), s.get("schedule_days", ""))
            times.on_change(lambda: self._save(dict(zip(("schedule_start", "schedule_stop", "schedule_days"),
                                                        times.values()))))

        self.loaded.append(fill)
        self.schedule_times = times
        page.add(Card("Default schedule",
                      "Downloads you put on the schedule (in the Add dialog or with Schedule…) start and stop at "
                      "these times, unless you give that download its own times.",
                      *times.rows(), *(self.switch(k, t, s) for k, (t, s) in SCHEDULE_SWITCHES.items()),
                      self.rate("schedule_speed_limit", "Speed limit while scheduled")))

    def _torrents(self) -> None:
        page = self.pages["torrents"]
        adding = ("torrent_fetch_metadata", "torrent_sequential", "torrent_merge_duplicates", "torrent_tracker_list")
        behaviour = Card("Adding torrents", "", *(self.switch(k, *TORRENT_SWITCHES[k]) for k in adding))
        behaviour.add(self.entry("torrent_trackers", "Extra trackers for every torrent", "separate with spaces",
                                 save=lambda t: "\n".join(t.split()), show=lambda v: " ".join(str(v).split())))
        behaviour.add(self.entry("torrent_tracker_list_url", "Public tracker list address"))
        network = Card("Network", "", *(self.switch(k, *v) for k, v in TORRENT_SWITCHES.items()
                                        if k not in adding and k != "torrent_seed"))
        for key, (title, subtitle, low, high, step, digits) in TORRENT_SPINS.items():
            if "seed" not in key:
                network.add(self.spin(key, title, subtitle, low, high, step, digits))
        network.add(self.choice("torrent_encryption", "Encryption", ENCRYPTION))
        network.add(self.entry("torrent_ip_filter", "IP block list file (P2P or DAT)", "empty means none"))
        seeding = Card("Seeding", "", self.switch("torrent_seed", *TORRENT_SWITCHES["torrent_seed"]))
        for key, (title, subtitle, low, high, step, digits) in TORRENT_SPINS.items():
            if "seed" in key:
                seeding.add(self.spin(key, title, subtitle, low, high, step, digits))
        seeding.add(self.rate("torrent_upload_limit", "Upload speed limit", "e.g. 500K; 0 means unlimited"))
        for card in (behaviour, network, seeding):
            page.add(card)

    def _sites(self) -> None:
        page = self.pages["sites"]
        self.rules = Card("Host rules", "Limit connections or parallel downloads for a site. Rules apply to "
                                        "subdomains unless marked exact.")
        self.rules.add_head_control(self._add_button("Add rule", lambda: self._edit_rule(None)))
        self.logins = Card("Logins", "User names and passwords for sites that ask for them (HTTP login). "
                                     "Passwords are kept in your keyring.")
        self.logins.add_head_control(self._add_button("Add login", lambda: self._edit_login(None)))
        page.add(self.rules)
        page.add(self.logins)

    def _add_button(self, tip: str, handler: Callable[[], None]) -> QToolButton:
        button = QToolButton()
        button.setIcon(icon("add", current().fg, 16, 2))
        button.setToolTip(tip)
        button.setAccessibleName(tip)
        button.setAutoRaise(True)
        button.clicked.connect(handler)
        return button

    def _backup(self) -> None:
        page = self.pages["backup"]
        back_up = QPushButton("Back up…")
        back_up.clicked.connect(self._back_up_now)
        restore = QPushButton("Restore…")
        restore.clicked.connect(self._restore)
        page.add(Card("Backup and restore", "Settings, host rules, logins and the download list in one file. "
                                            "Saved passwords are only included in backups with a password.",
                      Row("Back up now", "", back_up),
                      Row("Restore a backup", "Downloads already in the list are kept", restore)))
        password = self.entry("backup_password", "Password for automatic backups", "empty means none",
                              password=True, save=lambda t: t)
        self.loaded.append(lambda s: s.get("backup_password_set") and password.set_title(
            "Password for automatic backups (saved — type to change)"))
        folder = self.folder("backup_dir", "Folder", "~/Documents/LDM Backups", clearable=False)
        self.loaded.append(lambda s: folder.set_subtitle(short_path(s.get("backup_folder") or s.get("backup_dir") or "")))
        page.add(Card("Automatic backups", "", self.choice("backup_schedule", "Back up", BACKUP_SCHEDULES), folder,
                      self.spin("backup_keep", "Backups to keep", "", 1, 100), password))

    # --- yt-dlp ---------------------------------------------------------------------------------

    def _show_ytdlp(self, settings: dict[str, Any]) -> None:
        pending = settings.get("ytdlp_pending")
        self.ytdlp_row.set_subtitle(f"Version {settings.get('ytdlp_version', '?')}"
                                    + (f" — {pending} is used once current downloads finish" if pending else ""))

    def _update_ytdlp(self, button: QPushButton) -> None:
        button.setEnabled(False)
        self.ytdlp_row.set_subtitle("Checking for a new version…")

        def done(version, error) -> None:
            button.setEnabled(True)
            self.toast(error.message if error else f"yt-dlp {version} installed" if version
                       else "yt-dlp is up to date")
            self.client.get_settings(lambda s, e: s and self._show_ytdlp(s))

        self.client.update_ytdlp(done)

    # --- host rules and logins ------------------------------------------------------------------

    def _reload_rules(self) -> None:
        self.client.list_host_rules(self._show_rules)

    def _show_rules(self, rules: list[dict[str, Any]] | None, _error) -> None:
        self.rules.clear()
        for rule in rules or []:
            limits = []
            if rule["max_connections"]:
                limits.append(f"{rule['max_connections']} connections")
            if rule["max_downloads"]:
                limits.append(f"{rule['max_downloads']} at a time")
            limits.append("includes subdomains" if rule["include_subdomains"] else "exact host")
            edit, delete = QPushButton("Edit"), QPushButton("Remove")
            edit.clicked.connect(lambda _c=False, r=rule: self._edit_rule(r))
            delete.clicked.connect(lambda _c=False, host=rule["host"]: self.client.remove_host_rule(
                host, lambda *_: self._reload_rules()))
            self.rules.add(Row(rule["host"], " · ".join(limits), edit, delete))
        if not rules:
            self.rules.add(Row("No rules", "Every site gets the default connections"))

    def _edit_rule(self, rule: dict[str, Any] | None) -> None:
        def save(host: str, connections: int, downloads: int, subdomains: bool) -> None:
            if rule and host != rule["host"]:
                self.client.remove_host_rule(rule["host"])
            self.client.set_host_rule(host, connections, downloads, subdomains,
                                      lambda r, e: (self._report(r, e), self._reload_rules()))

        HostRuleDialog(self, rule, save).open()

    def _reload_logins(self) -> None:
        self.client.list_logins(self._show_logins)

    def _show_logins(self, logins: list[dict[str, Any]] | None, _error) -> None:
        self.logins.clear()
        for login in logins or []:
            scope = "" if login["include_subdomains"] else " · exact host"
            edit, delete = QPushButton("Edit"), QPushButton("Remove")
            edit.clicked.connect(lambda _c=False, entry=login: self._edit_login(entry))
            delete.clicked.connect(lambda _c=False, host=login["host"]: self.client.remove_login(
                host, lambda *_: self._reload_logins()))
            self.logins.add(Row(login["host"], login["username"] + scope, edit, delete))
        if not logins:
            self.logins.add(Row("No logins", "Add one for sites that ask for a user name and password"))

    def _edit_login(self, login: dict[str, Any] | None) -> None:
        def save(host: str, user: str, password: str, subdomains: bool) -> None:
            self.client.set_login(host, user, password, subdomains,
                                  lambda r, e: self._report(r, e) if e else self._reload_logins())

        LoginDialog(self, login["host"] if login else "", login["username"] if login else "", save,
                    login["include_subdomains"] if login else True).open()

    # --- backup ---------------------------------------------------------------------------------

    def _back_up_now(self) -> None:
        password, ok = QInputDialog.getText(
            self, "Back up", "A password encrypts the backup and also saves your site and proxy passwords in it. "
                             "Leave it empty for a plain backup.", QLineEdit.EchoMode.Password)
        if not ok:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save backup", str(Path.home() / backup_file_name(bool(password))))
        if path:
            self.client.create_backup(path, password, lambda saved, error: self._report(None, error) if error
                                      else self.toast(f"Backed up to {short_path(saved)}"))

    def _restore(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Restore backup", str(Path.home()))
        if not path:
            return
        try:
            encrypted = is_encrypted(Path(path).read_bytes())
        except OSError as e:
            self.toast(f"Could not read the file: {e}")
            return
        password = ""
        if encrypted:
            password, ok = QInputDialog.getText(self, "Backup password", "This backup is protected by a password.",
                                                QLineEdit.EchoMode.Password)
            if not ok:
                return

        def restored(counts, error) -> None:
            if error:
                self._report(None, error)
                return
            self.toast(f"Restored {counts['settings']} settings, {counts['host_rules']} host rules, "
                       f"{counts['logins']} logins and {counts['downloads']} downloads")
            self._reload_rules()
            self._reload_logins()

        self.client.restore_backup(path, password, restored)

