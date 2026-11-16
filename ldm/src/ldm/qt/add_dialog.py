"""The Add dialog (1DM-style): checks the link first and shows what will be downloaded — a file, a video
(yt-dlp) or a torrent — then where, how, and with which browser headers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QCheckBox, QFileDialog, QLabel, QPushButton, QSlider, QWidget

from ..format import STATE_LABELS, human
from ..nmhost import header_name
from ..sandbox import folder_problem
from ..ui.text import (capture_details, duration_text, is_torrent_link, is_url, key_valid, looks_like_login,
                       parse_limit, short_path)
from .forms import Card, ComboRow, Dialog, EntryRow, Folding, Row, ScheduleEditor, SwitchRow
from .theme import current
from .widgets import MessageBar

Location = QStandardPaths.StandardLocation
# "Save in" choices: (label, standard location or None for the download folder setting)
FOLDERS = [("Downloads", None), ("Videos", Location.MoviesLocation), ("Music", Location.MusicLocation),
           ("Pictures", Location.PicturesLocation), ("Documents", Location.DocumentsLocation)]
OTHER_FOLDER = "Other folder…"
MAX_PARTS = 32
EDITABLE_HEADERS = ("Referer", "Origin", "Cookie", "User-Agent")
PLAYLIST_EXTENSIONS = (".m3u8", ".mpd")
PROBE_DELAY_MS = 600


class AddDialog(Dialog):
    def __init__(self, parent: QWidget, client, url: str = "", name: str = "", headers: dict[str, str] | None = None,
                 kind: str = "", captures: list[dict[str, Any]] | None = None,
                 on_show: Callable[[int], None] | None = None, on_added: Callable[[int], None] | None = None,
                 known: list[str] | None = None):
        super().__init__(parent, "Grab videos" if kind == "media" and not url else "Add download", width=600,
                         height=720)
        self.client, self.on_show, self.on_added = client, on_show, on_added
        self.duplicate: dict[str, Any] | None = None
        self._html_page = self._login_page = False
        self.kind_hint = kind           # "media": a video page or stream; "torrent": a magnet or .torrent
        self.media: dict[str, Any] | None = None    # ProbeMedia result once the link is a video
        self.torrent: dict[str, Any] | None = None  # ProbeTorrent result ({}: no file list)
        self.file_checks: list[tuple[int, QCheckBox, int]] = []  # (file index, box, size)
        self.settings: dict[str, Any] = {}
        self.download_dir: str | None = None
        self.custom_folder: str | None = None
        self.granted_folders: dict[int, str] = {}  # Flatpak: quick choices the user gave access to
        self._folder_index = 0
        self._auto_name = not name      # the name may be replaced by the server's suggestion
        self._generation = 0
        headers = headers or {}

        self.cancel_button.setText("Cancel")
        self.later_button = self.add_button("Add to list", lambda: self._submit(False))
        self.later_button.setToolTip("Add paused")
        self.start_button = self.add_button("Start", lambda: self._submit(True), primary=True)
        self.start_button.setToolTip("Add and start downloading")

        # 1DM: "Download link already exists!" — show or resume it, or add it again.
        self.duplicate_bar = MessageBar("Show", current, closable=True)
        self.duplicate_bar.button_clicked.connect(self._on_duplicate)
        self.web_bar = MessageBar("Open in browser", current, closable=True)
        self.web_bar.button_clicked.connect(self._open_in_browser)
        for bar in (self.duplicate_bar, self.web_bar):
            self.bars.addWidget(bar)

        # --- the file
        self.url = EntryRow("Link", url, "https://…, magnet:… or a page with a video")
        self.badge = QLabel()
        self.badge.setObjectName("badge")
        self.url.suffix.addWidget(self.badge)
        self.name = EntryRow("Save as", name)
        self.name.edit.textEdited.connect(lambda _t: setattr(self, "_auto_name", False))
        self.without_list = QPushButton("Add without file list")
        self.without_list.hide()
        self.without_list.clicked.connect(self._on_without_list)
        self.size = Row("Size", "Enter a link", self.without_list)
        self.page.add(Card("", "", self.url, self.name, self.size))

        # 1DM's grabber: media the browser loaded on the page, for sites yt-dlp doesn't know.
        self.captures = captures or []
        self.found = Card("Found on this page", "Video files and streams your browser loaded while the page played.")
        for capture in self.captures:
            use = QPushButton("Use")
            use.clicked.connect(lambda _c=False, cap=capture: self._use_capture(cap))
            self.found.add(Row(capture.get("filename") or capture["url"].rsplit("/", 1)[-1] or capture["url"],
                               capture_details(capture), use))
        self.found.setVisible(bool(self.captures))
        self.page.add(self.found)

        # --- video
        self.media_title = Row("")
        self.quality = ComboRow("Quality", [])
        self.quality.chosen.connect(lambda _v: self._on_quality())
        self.subtitles = SwitchRow("Subtitles")
        self.hls_key = EntryRow("Decryption key (optional, base64 or hex)")  # 1DM's "m3u8 key"
        self.hls_key.changed.connect(lambda _t: self._validate())
        self.media_card = Card("Video", "", self.media_title, self.quality, self.subtitles, self.hls_key)
        self.media_card.hide()
        self.page.add(self.media_card)

        # --- torrent
        self.files = Folding("Files")
        self.select_all = QCheckBox("All")
        self.select_all.setChecked(True)
        self.select_all.toggled.connect(self._on_select_all)
        self.files.head.add_control(self.select_all)
        self.sequential = SwitchRow("Download in order", "Lets you play a video while it downloads")
        self.torrent_card = Card("Torrent", "", self.files, self.sequential)
        self.torrent_card.hide()
        self.page.add(self.torrent_card)

        # --- where
        self.folder_row = ComboRow("Folder", [(i, label) for i, (label, _) in enumerate(FOLDERS)]
                                   + [(len(FOLDERS), OTHER_FOLDER)])
        self.folder_row.chosen.connect(self._on_folder_chosen)
        self.page.add(Card("Save in", "", self.folder_row))

        # --- how
        self.parts = QSlider(Qt.Orientation.Horizontal)
        self.parts.setRange(1, MAX_PARTS)
        self.parts.setValue(8)
        self.parts.setFixedWidth(200)
        self.parts.setAccessibleName("Number of parts")
        self.parts_value = QLabel("8")
        self.parts_value.setMinimumWidth(22)
        self.parts.valueChanged.connect(lambda v: self.parts_value.setText(str(v)))
        self.parts_row = Row("Number of parts", "More parts can be faster on slow servers", self.parts_value,
                             self.parts)
        self.limit = EntryRow("Speed limit", "", "e.g. 2M; empty means unlimited")
        self.limit.changed.connect(lambda _t: self._validate())
        self.schedule = ScheduleEditor()  # 1DM's scheduler, per download
        self.unmetered_only = SwitchRow("Only on unmetered connections",
                                        "Waits while on mobile data or a metered network")
        self.page.add(Card("Options", "", self.parts_row, self.limit, self.schedule, self.unmetered_only))

        # --- request headers (prefilled by the browser)
        self.header_rows = Folding("Request headers", "Sent with every request; filled in by the browser extension")
        self.referer = EntryRow("Referer", headers.get("Referer", ""))
        self.origin = EntryRow("Origin", headers.get("Origin", ""))
        self.cookie = EntryRow("Cookie", headers.get("Cookie", ""), password=True)
        self.user_agent = EntryRow("User-Agent", headers.get("User-Agent", ""))
        for row in (self.referer, self.origin, self.cookie, self.user_agent):
            self.header_rows.add(row)
            row.changed.connect(lambda _t: self._schedule_probe())
        # The rest of what the browser sent (Accept-Language, Sec-Fetch-…): kept as is; signed links check them.
        self.other_headers_row = self.header_rows.add(Row("Other browser headers"))
        self._set_other_headers({k: v for k, v in headers.items() if k not in EDITABLE_HEADERS})
        self.header_rows.set_open(bool(headers))
        self.page.add(Card("", "", self.header_rows))

        self.timer = QTimer(self, singleShot=True, interval=PROBE_DELAY_MS)
        self.timer.timeout.connect(self._probe)
        self.url.changed.connect(lambda _t: (self._validate(), self._schedule_probe()))
        client.get_settings(self._on_settings)
        if url:
            self._probe()
        else:
            # A copied link, unless it is already in the list (e.g. the one just downloaded).
            clip = QGuiApplication.clipboard().text().strip()
            if (is_url(clip) or is_torrent_link(clip)) and clip not in set(known or ()):
                self.url.set_text(clip)  # starts the check
        self._validate()

    # --- settings and folders ------------------------------------------------------------------

    def _on_settings(self, settings: dict[str, Any] | None, _error) -> None:
        if not settings:
            return
        self.settings = settings
        self.download_dir = settings.get("download_dir")
        if self.torrent is None:
            self.sequential.set_on(bool(settings.get("torrent_sequential")), quiet=True)
        self.schedule.set_defaults(settings)
        self.schedule.set_on(bool(settings.get("schedule_new_links")))
        if self.parts.isEnabled():
            self.parts.setValue(int(settings.get("default_connections", 8)))
        self._show_folder()

    def _standard(self, index: int) -> str | None:
        location = FOLDERS[index][1]
        return QStandardPaths.writableLocation(location) if location is not None else None

    def folder(self) -> str | None:
        index = self.folder_row.index()
        if index < len(FOLDERS):
            return self.granted_folders.get(index) or self._standard(index) or self.download_dir
        return self.custom_folder or self.download_dir

    def _show_folder(self) -> None:
        path = self.folder()
        self.folder_row.set_subtitle(short_path(path) if path else "")

    def _on_folder_chosen(self, index: int) -> None:
        initial = self.custom_folder or self.download_dir
        if index < len(FOLDERS):
            path = self._standard(index)
            if not path or index in self.granted_folders or not folder_problem(path):
                self._folder_index = index
                self._show_folder()
                return
            initial = path  # a Flatpak without access to it: choosing it in the dialog grants access
        chosen = QFileDialog.getExistingDirectory(self, "Save in", initial or str(Path.home()))
        if chosen:
            if index < len(FOLDERS):
                self.granted_folders[index] = chosen
            else:
                self.custom_folder = chosen
            self._folder_index = index
        else:
            self.folder_row.set_index(self._folder_index)  # cancelled: back to the old choice
        self._show_folder()

    # --- checking the link ---------------------------------------------------------------------

    def headers(self) -> dict[str, str]:
        edited = {k: row.text().strip() for k, row in (("Referer", self.referer), ("Origin", self.origin),
                                                       ("Cookie", self.cookie), ("User-Agent", self.user_agent))
                  if row.text().strip()}
        return {**self.other_headers, **edited}

    def _set_other_headers(self, headers: dict[str, str]) -> None:
        self.other_headers = headers
        self.other_headers_row.set_subtitle(", ".join(headers))
        self.other_headers_row.setVisible(bool(headers))

    def _schedule_probe(self) -> None:
        self.timer.start()

    def _busy(self, text: str) -> None:
        self.badge.setText("Checking…")
        self.badge.setProperty("tone", "")
        self._polish(self.badge)
        self.size.set_subtitle(text)

    def _badge(self, text: str, tone: str = "") -> None:
        self.badge.setText(text)
        self.badge.setProperty("tone", tone)
        self._polish(self.badge)

    @staticmethod
    def _polish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _probe(self) -> None:
        self.timer.stop()
        url = self.url.text().strip()
        self._generation += 1
        if not (is_url(url) or is_torrent_link(url)):
            self._show_probe_state(None)
            return
        self._set_media(None)
        self._set_torrent(None)
        self._html_page = self._login_page = False
        self.web_bar.hide()
        self._check_duplicate(url)
        if self.kind_hint == "torrent" or is_torrent_link(url):
            self._probe_torrent(url)
        elif self.kind_hint == "media" or url.split("?")[0].lower().endswith(PLAYLIST_EXTENSIONS):
            self._probe_media(url)
        else:
            generation = self._generation
            self._busy("Checking the link…")
            self.client.probe(url, self.headers(), lambda result, error: generation == self._generation
                              and self._on_probe(result, error))

    def _probe_torrent(self, url: str) -> None:
        generation = self._generation
        magnet = url.startswith("magnet:")
        self._busy("Getting the file list from peers…" if magnet else "Reading the torrent…")
        self.without_list.setVisible(magnet)
        self.client.probe_torrent(url, self.headers(), lambda result, error: generation == self._generation
                                  and self._on_torrent(result, error))

    def _on_torrent(self, result: dict[str, Any] | None, error) -> None:
        self.without_list.hide()
        if error:
            self._show_probe_state(None)
            self.size.set_subtitle(f"Could not read the torrent: {error.message}")
            return
        self._set_torrent(result)

    def _on_without_list(self) -> None:
        self._generation += 1  # ignore the file list if it still arrives
        self.without_list.hide()
        self._set_torrent({})

    def _set_torrent(self, info: dict[str, Any] | None) -> None:
        self.torrent = info
        self.torrent_card.setVisible(info is not None)
        self.parts_row.setVisible(info is None)
        if info is None:
            return
        self._badge("Torrent", "accent")
        while self.files.rows.count():
            widget = self.files.rows.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.file_checks = []
        for f in info.get("files") or []:
            check = QCheckBox()
            check.setChecked(True)
            check.setAccessibleName(Path(f["path"]).name)
            check.toggled.connect(lambda _on: self._update_selected())
            row = Row(Path(f["path"]).name, str(Path(f["path"]).parent), QLabel(human(f["size"])))
            row.controls.insertWidget(0, check)
            self.files.add(row)
            self.file_checks.append((f["index"], check, f["size"]))
        self.files.setVisible(bool(self.file_checks))
        self.files.set_open(1 < len(self.file_checks) <= 20)
        if self._auto_name and info.get("name"):
            self.name.set_text(info["name"])
        self._update_selected()

    def _on_select_all(self, on: bool) -> None:
        for _, box, _ in self.file_checks:
            box.setChecked(on)

    def _update_selected(self) -> None:
        if not self.torrent:
            self.size.set_subtitle("Size known once peers send the file list")
            self._validate()
            return
        chosen = [size for _, box, size in self.file_checks if box.isChecked()]
        total = sum(size for _, _, size in self.file_checks)
        self.files.head.set_title(f"Files ({len(chosen)} of {len(self.file_checks)} selected)")
        self.size.set_subtitle(f"{human(sum(chosen))} of {human(total)}" if len(chosen) < len(self.file_checks)
                               else f"{human(total)} ({total:,} bytes)")
        self._validate()

    def _probe_media(self, url: str) -> None:
        generation = self._generation
        self._busy("Looking for a video…")
        self.client.probe_media(url, self.headers(), lambda result, error: generation == self._generation
                                and self._on_media(result, error))

    def _on_media(self, result: dict[str, Any] | None, error) -> None:
        if error:
            self._show_probe_state(None)
            if self._html_page and self._login_page:
                self.size.set_subtitle("The site sent its login page: log in on the site in your browser, "
                                       "then download again")
            elif self.captures:
                self.size.set_subtitle("yt-dlp doesn't support this site — pick one of the videos found on the page")
            else:
                self.size.set_subtitle(f"No downloadable video found: {error.message}")
                if self._html_page and self.settings.get("open_html_in_browser", True):
                    self.web_bar.show_text("This link is a web page, not a file")  # 1DM: open it in the browser
            return
        self._set_media(result)

    def _on_probe(self, result: dict[str, Any] | None, error) -> None:
        if error:
            self._show_probe_state(None)
            self.size.set_subtitle(f"Could not check the link: {error.message}")
            return
        if (result.get("content_type") or "").startswith("text/html"):
            self._html_page = True
            self._login_page = looks_like_login(result.get("final_url") or "")
            self._probe_media(self.url.text().strip())  # a web page: is there a video on it?
            return
        self._show_probe_state(result)

    # --- duplicates and web pages ---------------------------------------------------------------

    def _check_duplicate(self, url: str) -> None:
        generation = self._generation
        self.client.find_duplicates(url, lambda found, error: generation == self._generation
                                    and self._show_duplicate(found[0] if found else None))

    def _show_duplicate(self, existing: dict[str, Any] | None) -> None:
        self.duplicate = existing
        if existing is None:
            self.duplicate_bar.hide()
            return
        size = f" · {human(existing['size'])}" if existing.get("size") else ""
        state = STATE_LABELS.get(existing["state"], existing["state"])
        resumable = existing["state"] in ("paused", "error", "expired", "queued")
        self.duplicate_bar.button.setText("Resume" if resumable else "Show")
        self.duplicate_bar.show_text(f"Already in your list — {state}{size}")

    def _on_duplicate(self) -> None:
        existing = self.duplicate
        if existing is None:
            return
        if existing["state"] in ("paused", "error", "expired"):
            self.client.resume(existing["id"])
        if self.on_show:
            self.on_show(existing["id"])
        self.reject()

    def _use_capture(self, capture: dict[str, Any]) -> None:
        """Download a file or stream the browser loaded on the page instead of the page itself."""
        self.kind_hint = "media" if capture.get("kind") in ("hls", "dash") else ""
        sent = capture.get("headers") or {}  # what the browser sent for it: signed links check these
        referer = sent.get("referer") or capture.get("referer")
        if referer or not self.referer.text():
            self.referer.set_text(referer or self.url.text().strip())
        self.origin.set_text(sent.get("origin", ""))
        if "cookie" in sent:
            self.cookie.set_text(sent["cookie"])
        if "user-agent" in sent:
            self.user_agent.set_text(sent["user-agent"])
        self._set_other_headers({header_name(k): v for k, v in sent.items()
                                 if header_name(k) not in EDITABLE_HEADERS})
        if capture.get("filename") and self._auto_name:
            self.name.set_text(capture["filename"])
        self.url.set_text(capture["url"])  # checks the new link

    def _open_in_browser(self) -> None:
        QDesktopServices.openUrl(QUrl(self.url.text().strip()))
        self.reject()

    # --- video ---------------------------------------------------------------------------------

    def _set_media(self, info: dict[str, Any] | None) -> None:
        self.media = info
        self.media_card.setVisible(info is not None)
        self.parts_row.set_title("Parallel fragments" if info else "Number of parts")
        if info is None:
            return
        self._badge("Live" if info.get("is_live") else "Video", "accent")
        details = [info.get("site", ""), duration_text(info.get("duration")), "live stream" if info.get("is_live") else ""]
        self.media_title.set_title(info.get("title", ""))
        self.media_title.set_subtitle(" · ".join(d for d in details if d))
        choices = info.get("choices") or []
        self.quality.set_choices([(i, c["label"] + (f" · ~{human(c['size'])}" if c.get("size") else ""))
                                  for i, c in enumerate(choices)])
        self.quality.set_index(self._default_choice(choices))
        languages = info.get("subtitles") or []
        self.subtitles.setVisible(bool(languages))
        self.subtitles.set_subtitle("Available: " + ", ".join(languages[:8]) + ("…" if len(languages) > 8 else ""))
        self.subtitles.set_on(bool(self.settings.get("subtitles")), quiet=True)
        if self._auto_name and info.get("title"):
            self.name.set_text(info["title"])
        self.parts.setEnabled(True)
        self.parts_row.set_subtitle("Pieces of the stream downloaded at the same time")
        self.media_card.sync_lines()
        self._on_quality()

    def _default_choice(self, choices: list[dict[str, Any]]) -> int:
        wanted = str(self.settings.get("media_quality", "best"))
        ids = [c["id"] for c in choices]
        if wanted in ids:
            return ids.index(wanted)
        if wanted.isdigit():  # the highest choice not above the preferred height
            below = [i for i, c in enumerate(choices) if c["id"].isdigit() and int(c["id"]) <= int(wanted)]
            if below:
                return below[0]
        return 0

    def _choice(self) -> dict[str, Any] | None:
        choices = (self.media or {}).get("choices") or []
        index = self.quality.index()
        return choices[index] if 0 <= index < len(choices) else None

    def _on_quality(self) -> None:
        choice = self._choice()
        if choice:
            self.size.set_subtitle(f"About {human(choice['size'])}" if choice.get("size")
                                   else "Size not known in advance")

    def _subtitle_languages(self) -> str:
        if not (self.media and not self.subtitles.isHidden() and self.subtitles.is_on()):
            return ""
        available = self.media.get("subtitles") or []
        wanted = [lang for lang in (self.settings.get("subtitles") or "").split(",") if lang in available]
        return ",".join(wanted or available[:1])

    def _show_probe_state(self, result: dict[str, Any] | None) -> None:
        if result is None:
            self._badge("")
            self.size.set_subtitle("Enter a link" if not self.url.text() else "—")
            self._set_parts_allowed(True)
            return
        resumable = result.get("resumable", False)
        self._badge("Can resume" if resumable else "Can't resume", "good" if resumable else "warn")
        size = result.get("size")
        details = [f"{human(size)} ({size:,} bytes)" if size is not None else "Unknown size"]
        if result.get("content_type"):
            details.append(result["content_type"])
        self.size.set_subtitle(" · ".join(details))
        if self._auto_name and result.get("filename"):
            self.name.set_text(result["filename"])
        self._set_parts_allowed(resumable)

    def _set_parts_allowed(self, allowed: bool) -> None:
        self.parts.setEnabled(allowed)
        if allowed:
            self.parts_row.set_subtitle("More parts can be faster on slow servers")
        else:
            self.parts.setValue(1)
            self.parts_row.set_subtitle("This server does not support parts or resuming")

    # --- adding --------------------------------------------------------------------------------

    def _validate(self) -> bool:
        text = self.url.text()
        url_ok = is_url(text) or is_torrent_link(text)
        if self.file_checks and not any(box.isChecked() for _, box, _ in self.file_checks):
            url_ok = False  # no file chosen
        limit_ok = parse_limit(self.limit.text()) is not None
        key_ok = key_valid(self.hls_key.text())
        self.url.set_error(not url_ok and bool(text))
        self.limit.set_error(not limit_ok)
        self.hls_key.set_error(not key_ok)
        for button in (self.start_button, self.later_button):
            button.setEnabled(url_ok and limit_ok and key_ok)
        return url_ok and limit_ok and key_ok

    def options(self, start: bool) -> dict[str, Any]:
        options = {
            "name": self.name.text().strip() or None,
            "dir": self.folder(),
            "connections": self.parts.value(),
            "speed_limit": parse_limit(self.limit.text()) or None,
            "headers": self.headers() or None,
            "start": start,
            "scheduled": self.schedule.is_on(),
            "unmetered_only": self.unmetered_only.is_on() or None,
        }
        if self.schedule.is_on():
            options.update(self.schedule.window())  # equal to the defaults = follow the defaults
        if self.torrent is not None:
            options.update(kind="torrent", sequential=self.sequential.is_on(), name=None)
            if self.torrent:
                priorities = [0] * self.torrent.get("num_files", len(self.file_checks))
                for index, box, _ in self.file_checks:
                    priorities[index] = 1 if box.isChecked() else 0
                options.update(priorities=priorities, metadata=self.torrent.get("metadata"))
            options.pop("connections", None)
            return options
        choice = self._choice()
        if self.media and choice:
            options.update(kind="media", format=choice["selector"], format_label=choice["label"],
                           title=self.media.get("title"), subtitles=self._subtitle_languages() or None,
                           hls_key=self.hls_key.text().strip() or None)
            if self._auto_name:
                options["name"] = None  # yt-dlp names the file after the title
        return options

    def _submit(self, start: bool) -> None:
        if not self._validate():
            return
        for button in (self.start_button, self.later_button):
            button.setEnabled(False)
        self.client.add(self.url.text().strip(), self.options(start), self._on_result)

    def _on_result(self, download_id, error) -> None:
        if error:
            self._validate()
            self.toast(error.message)
            return
        if self.on_added:
            self.on_added(download_id)
        self.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.start_button.isEnabled():
                self._submit(True)
            return
        super().keyPressEvent(event)
