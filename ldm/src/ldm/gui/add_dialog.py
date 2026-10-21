"""Add-download dialog, 1DM-style: checks the link first and shows what will be downloaded."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from gi.repository import Adw, Gio, GLib, Gtk

from ..format import human
from .client import DaemonClient
from .dialogs import is_url, parse_limit, read_clipboard_url, short_path

# "Save in" choices: (label, XDG directory or None for the download folder setting)
FOLDERS = [
    ("Downloads", None),
    ("Videos", GLib.UserDirectory.DIRECTORY_VIDEOS),
    ("Music", GLib.UserDirectory.DIRECTORY_MUSIC),
    ("Pictures", GLib.UserDirectory.DIRECTORY_PICTURES),
    ("Documents", GLib.UserDirectory.DIRECTORY_DOCUMENTS),
]
OTHER_FOLDER = "Other folder…"
MAX_PARTS = 32
PLAYLIST_EXTENSIONS = (".m3u8", ".mpd")


def is_torrent_link(text: str) -> bool:
    parts = urlsplit(text.strip())
    if parts.scheme == "magnet":
        return "xt=" in parts.query
    return parts.scheme in ("http", "https", "file") and parts.path.lower().endswith(".torrent")


def duration_text(seconds: float | None) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


class AddDialog(Adw.Dialog):
    def __init__(self, client: DaemonClient, url: str = "", on_added: Callable[[int], None] | None = None,
                 name: str = "", headers: dict[str, str] | None = None, kind: str = ""):
        super().__init__(title="Download File", content_width=560)
        self.client = client
        self.kind_hint = kind          # "media" when the browser sent a video page or stream
        self.media: dict[str, Any] | None = None  # ProbeMedia result once the link is a video
        self.torrent: dict[str, Any] | None = None  # ProbeTorrent result (empty dict: no file list)
        self.file_checks: list[tuple[int, Gtk.CheckButton, int]] = []  # (file index, box, size)
        self.settings: dict[str, Any] = {}
        self.on_added = on_added
        self.download_dir: str | None = None
        self.custom_folder: str | None = None
        self._folder_index = 0
        self._auto_name = not name     # file name may be replaced by the server's suggestion
        self._probe_generation = 0
        self._probe_timer = 0
        headers = headers or {}

        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.start_button = Gtk.Button(label="Start", sensitive=False, tooltip_text="Add and start downloading")
        self.start_button.add_css_class("suggested-action")
        self.start_button.connect("clicked", lambda *_: self._submit(start=True))
        self.later_button = Gtk.Button(label="Add to List", sensitive=False, tooltip_text="Add paused")
        self.later_button.connect("clicked", lambda *_: self._submit(start=False))
        header.pack_end(self.start_button)
        header.pack_end(self.later_button)

        # --- the file
        file_group = Adw.PreferencesGroup()
        self.url = Adw.EntryRow(title="Link", text=url)
        self.resume = Gtk.Label(valign=Gtk.Align.CENTER)
        self.resume.add_css_class("caption-heading")
        self.spinner = Adw.Spinner(visible=False, valign=Gtk.Align.CENTER)
        self.url.add_suffix(self.spinner)
        self.url.add_suffix(self.resume)
        self.name = Adw.EntryRow(title="Save as", text=name or "")
        self.name.connect("changed", self._on_name_edited)
        self.size = Adw.ActionRow(title="Size", subtitle="Enter a link")
        self.size.add_css_class("property")
        for row in (self.url, self.name, self.size):
            file_group.add(row)

        # --- video (shown when yt-dlp finds one: site pages, HLS/DASH playlists)
        self.media_group = Adw.PreferencesGroup(title="Video", visible=False)
        self.media_title = Adw.ActionRow(title="", subtitle_lines=1, title_lines=2)
        self.quality = Adw.ComboRow(title="Quality")
        self.quality.connect("notify::selected", self._on_quality)
        self.subtitles = Adw.SwitchRow(title="Subtitles", visible=False)
        for row in (self.media_title, self.quality, self.subtitles):
            self.media_group.add(row)

        # --- torrent (magnets and .torrent files)
        self.torrent_group = Adw.PreferencesGroup(title="Torrent", visible=False)
        self.files_row = Adw.ExpanderRow(title="Files")
        self.select_all = Gtk.CheckButton(active=True, valign=Gtk.Align.CENTER, tooltip_text="Select all")
        self.select_all.connect("toggled", self._on_select_all)
        self.files_row.add_suffix(self.select_all)
        self.sequential = Adw.SwitchRow(title="Download in order",
                                        subtitle="Lets you play a video while it downloads")
        self.torrent_group.add(self.files_row)
        self.torrent_group.add(self.sequential)
        self.without_list = Gtk.Button(label="Add without file list", visible=False, valign=Gtk.Align.CENTER)
        self.without_list.connect("clicked", self._on_without_list)
        self.size.add_suffix(self.without_list)

        # --- where
        where = Adw.PreferencesGroup(title="Save in")
        self.folder_row = Adw.ComboRow(title="Folder",
                                       model=Gtk.StringList.new([label for label, _ in FOLDERS] + [OTHER_FOLDER]))
        self.folder_row.connect("notify::selected", self._on_folder_selected)
        where.add(self.folder_row)

        # --- how
        options = Adw.PreferencesGroup(title="Options")
        self.parts_row = Adw.ActionRow(title="Number of parts")
        self.parts = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, MAX_PARTS, 1)
        self.parts.set_size_request(220, -1)
        self.parts.set_draw_value(True)
        self.parts.set_value_pos(Gtk.PositionType.LEFT)
        self.parts.set_digits(0)
        self.parts.set_valign(Gtk.Align.CENTER)
        for mark in (1, 8, 16, 32):
            self.parts.add_mark(mark, Gtk.PositionType.BOTTOM, None)
        self.parts_row.add_suffix(self.parts)
        self.limit = Adw.EntryRow(title="Speed limit (e.g. 2M, empty = unlimited)")
        self.limit.connect("changed", self._validate)
        options.add(self.parts_row)
        options.add(self.limit)

        # --- request headers (prefilled by the browser)
        header_group = Adw.PreferencesGroup()
        expander = Adw.ExpanderRow(title="Request headers",
                                   subtitle="Sent with every request; filled in by the browser extension")
        self.referer = Adw.EntryRow(title="Referer", text=headers.get("Referer", ""))
        self.cookie = Adw.PasswordEntryRow(title="Cookie", text=headers.get("Cookie", ""))
        self.user_agent = Adw.EntryRow(title="User-Agent", text=headers.get("User-Agent", ""))
        for row in (self.referer, self.cookie, self.user_agent):
            expander.add_row(row)
            row.connect("changed", self._schedule_probe)
        expander.set_expanded(bool(headers))
        header_group.add(expander)

        page = Adw.PreferencesPage()
        for group in (file_group, self.media_group, self.torrent_group, where, options, header_group):
            page.add(group)
        self.toasts = Adw.ToastOverlay(child=page)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        self.set_child(view)

        self.url.connect("changed", self._on_url_changed)
        self.connect("closed", lambda *_: self._cancel_timer())
        client.get_settings(self._on_settings)
        if url:
            self._probe()
        else:
            read_clipboard_url(self, self._prefill)
        self._validate()

    # --- settings and folders ------------------------------------------------------------------

    def _on_settings(self, settings: dict[str, Any] | None, error) -> None:
        if not settings:
            return
        self.settings = settings
        self.download_dir = settings["download_dir"]
        if self.torrent is None:
            self.sequential.set_active(bool(settings.get("torrent_sequential")))
        if self.parts.get_sensitive():
            self.parts.set_value(settings["default_connections"])
        self._show_folder()

    def folder(self) -> str | None:
        index = self.folder_row.get_selected()
        if index < len(FOLDERS):
            special = FOLDERS[index][1]
            path = GLib.get_user_special_dir(special) if special is not None else None
            return path or self.download_dir
        return self.custom_folder or self.download_dir

    def _show_folder(self) -> None:
        path = self.folder()
        self.folder_row.set_subtitle(short_path(path) if path else "")

    def _on_folder_selected(self, *_) -> None:
        if self.folder_row.get_selected() < len(FOLDERS):
            self._folder_index = self.folder_row.get_selected()
            self._show_folder()
            return
        dialog = Gtk.FileDialog(title="Save In")
        if self.custom_folder or self.download_dir:
            dialog.set_initial_folder(Gio.File.new_for_path(self.custom_folder or self.download_dir))

        def done(d, result):
            try:
                self.custom_folder = d.select_folder_finish(result).get_path()
                self._folder_index = len(FOLDERS)
            except GLib.Error:
                self.folder_row.set_selected(self._folder_index)  # cancelled: back to the old choice
            self._show_folder()

        dialog.select_folder(self.get_root(), None, done)

    # --- checking the link ---------------------------------------------------------------------

    def _prefill(self, url: str) -> None:
        if not self.url.get_text():
            self.url.set_text(url)

    def _on_url_changed(self, *_) -> None:
        self._validate()
        self._schedule_probe()

    def _on_name_edited(self, *_) -> None:
        if self.name.has_focus():
            self._auto_name = False

    def headers(self) -> dict[str, str]:
        return {k: row.get_text().strip() for k, row in
                (("Referer", self.referer), ("Cookie", self.cookie), ("User-Agent", self.user_agent))
                if row.get_text().strip()}

    def _schedule_probe(self, *_) -> None:
        self._cancel_timer()
        self._probe_timer = GLib.timeout_add(600, self._probe)

    def _cancel_timer(self) -> None:
        if self._probe_timer:
            GLib.source_remove(self._probe_timer)
            self._probe_timer = 0

    def _probe(self) -> bool:
        self._probe_timer = 0
        url = self.url.get_text().strip()
        self._probe_generation += 1
        if not (is_url(url) or is_torrent_link(url)):
            self._show_probe_state(None)
            return GLib.SOURCE_REMOVE
        self._set_media(None)
        self._set_torrent(None)
        if self.kind_hint == "torrent" or is_torrent_link(url):
            self._probe_torrent(url)
            return GLib.SOURCE_REMOVE
        if self.kind_hint == "media" or url.split("?")[0].lower().endswith(PLAYLIST_EXTENSIONS):
            self._probe_media(url)
            return GLib.SOURCE_REMOVE
        generation = self._probe_generation
        self.spinner.set_visible(True)
        self.resume.set_label("")
        self.size.set_subtitle("Checking the link…")
        self.client.probe(url, self.headers(),
                          lambda result, error: generation == self._probe_generation and self._on_probe(result, error))
        return GLib.SOURCE_REMOVE

    def _probe_torrent(self, url: str) -> None:
        generation = self._probe_generation
        self.spinner.set_visible(True)
        self.resume.set_label("")
        magnet = url.startswith("magnet:")
        self.size.set_subtitle("Getting the file list from peers…" if magnet else "Reading the torrent…")
        self.without_list.set_visible(magnet)
        self.client.probe_torrent(url, self.headers(),
                                  lambda result, error: generation == self._probe_generation
                                  and self._on_torrent(result, error))

    def _on_torrent(self, result: dict[str, Any] | None, error) -> None:
        self.spinner.set_visible(False)
        self.without_list.set_visible(False)
        if error:
            self._show_probe_state(None)
            self.size.set_subtitle(f"Could not read the torrent: {error.message}")
            return
        self._set_torrent(result)

    def _on_without_list(self, _button) -> None:
        self._probe_generation += 1  # ignore the file list if it still arrives
        self.spinner.set_visible(False)
        self.without_list.set_visible(False)
        self._set_torrent({})

    def _set_torrent(self, info: dict[str, Any] | None) -> None:
        self.torrent = info
        self.torrent_group.set_visible(info is not None)
        self.parts_row.set_visible(info is None)
        if info is None:
            return
        for css in ("success", "warning"):
            self.resume.remove_css_class(css)
        self.resume.add_css_class("accent")
        self.resume.set_label("Torrent")
        for row in getattr(self, "_file_rows", []):
            self.files_row.remove(row)
        self._file_rows, self.file_checks = [], []
        for f in info.get("files") or []:
            check = Gtk.CheckButton(active=True, valign=Gtk.Align.CENTER)
            check.connect("toggled", lambda *_: self._update_selected())
            row = Adw.ActionRow(title=GLib.markup_escape_text(Path(f["path"]).name),
                                subtitle=GLib.markup_escape_text(str(Path(f["path"]).parent)), activatable_widget=check)
            row.add_prefix(check)
            size = Gtk.Label(label=human(f["size"]), valign=Gtk.Align.CENTER)
            size.add_css_class("dim-label")
            row.add_suffix(size)
            self.files_row.add_row(row)
            self._file_rows.append(row)
            self.file_checks.append((f["index"], check, f["size"]))
        self.files_row.set_visible(bool(self.file_checks))
        self.files_row.set_expanded(1 < len(self.file_checks) <= 20)
        if self._auto_name and info.get("name"):
            self.name.set_text(info["name"])
            self._auto_name = True
        self._update_selected()

    def _on_select_all(self, check: Gtk.CheckButton) -> None:
        for _, box, _ in self.file_checks:
            box.set_active(check.get_active())

    def _update_selected(self) -> None:
        if not self.torrent:
            self.size.set_subtitle("Size known once peers send the file list")
            return
        chosen = [size for _, box, size in self.file_checks if box.get_active()]
        total = sum(size for _, _, size in self.file_checks)
        self.files_row.set_title(f"Files ({len(chosen)} of {len(self.file_checks)} selected)")
        self.size.set_subtitle(f"{human(sum(chosen))} of {human(total)}" if len(chosen) < len(self.file_checks)
                               else f"{human(total)} ({total:,} bytes)")
        self._validate()

    def _probe_media(self, url: str) -> None:
        generation = self._probe_generation
        self.spinner.set_visible(True)
        self.resume.set_label("")
        self.size.set_subtitle("Looking for a video…")
        self.client.probe_media(url, self.headers(),
                                lambda result, error: generation == self._probe_generation
                                and self._on_media(result, error))

    def _on_media(self, result: dict[str, Any] | None, error) -> None:
        self.spinner.set_visible(False)
        if error:
            self._show_probe_state(None)
            self.size.set_subtitle(f"No downloadable video found: {error.message}")
            return
        self._set_media(result)

    def _on_probe(self, result: dict[str, Any] | None, error) -> None:
        self.spinner.set_visible(False)
        if error:
            self._show_probe_state(None)
            self.size.set_subtitle(f"Could not check the link: {error.message}")
            return
        if (result.get("content_type") or "").startswith("text/html"):
            self._probe_media(self.url.get_text().strip())  # a web page: is there a video on it?
            return
        self._show_probe_state(result)

    # --- video mode ----------------------------------------------------------------------------

    def _set_media(self, info: dict[str, Any] | None) -> None:
        self.media = info
        self.media_group.set_visible(info is not None)
        self.parts_row.set_title("Parallel fragments" if info else "Number of parts")
        if info is None:
            return
        for css in ("success", "warning"):
            self.resume.remove_css_class(css)
        self.resume.add_css_class("accent")
        self.resume.set_label("Live" if info.get("is_live") else "Video")
        details = [info.get("site", ""), duration_text(info.get("duration")), "live stream" if info.get("is_live") else ""]
        self.media_title.set_title(GLib.markup_escape_text(info.get("title", "")))
        self.media_title.set_subtitle(" · ".join(d for d in details if d))
        choices = info.get("choices") or []
        self.quality.set_model(Gtk.StringList.new(
            [c["label"] + (f" · ~{human(c['size'])}" if c.get("size") else "") for c in choices]))
        self.quality.set_selected(self._default_choice(choices))
        languages = info.get("subtitles") or []
        self.subtitles.set_visible(bool(languages))
        self.subtitles.set_subtitle("Available: " + ", ".join(languages[:8]) + ("…" if len(languages) > 8 else ""))
        self.subtitles.set_active(bool(self.settings.get("subtitles")))
        if self._auto_name and info.get("title"):
            self.name.set_text(info["title"])
            self._auto_name = True
        self.parts.set_sensitive(True)
        self.parts_row.set_subtitle("Pieces of the stream downloaded at the same time")
        self._on_quality()

    def _default_choice(self, choices: list[dict[str, Any]]) -> int:
        wanted = str(self.settings.get("media_quality", "best"))
        ids = [c["id"] for c in choices]
        if wanted in ids:
            return ids.index(wanted)
        if wanted.isdigit():  # highest choice not above the preferred height
            heights = [(i, int(c["id"])) for i, c in enumerate(choices) if c["id"].isdigit()]
            below = [i for i, h in heights if h <= int(wanted)]
            if below:
                return below[0]
        return 0

    def _choice(self) -> dict[str, Any] | None:
        choices = (self.media or {}).get("choices") or []
        index = self.quality.get_selected()
        return choices[index] if 0 <= index < len(choices) else None

    def _on_quality(self, *_) -> None:
        choice = self._choice()
        if choice:
            self.size.set_subtitle(f"About {human(choice['size'])}" if choice.get("size") else "Size not known in advance")

    def _subtitle_languages(self) -> str:
        if not (self.media and self.subtitles.get_visible() and self.subtitles.get_active()):
            return ""
        available = self.media.get("subtitles") or []
        wanted = [lang for lang in (self.settings.get("subtitles") or "").split(",") if lang in available]
        return ",".join(wanted or available[:1])

    def _show_probe_state(self, result: dict[str, Any] | None) -> None:
        for css in ("success", "warning"):
            self.resume.remove_css_class(css)
        if result is None:
            self.resume.set_label("")
            self.size.set_subtitle("Enter a link" if not self.url.get_text() else "—")
            self._set_parts_allowed(True)
            return
        resumable = result.get("resumable", False)
        self.resume.set_label("Resume: Yes" if resumable else "Resume: No")
        self.resume.add_css_class("success" if resumable else "warning")
        size = result.get("size")
        details = [f"{human(size)} ({size:,} bytes)" if size is not None else "Unknown size"]
        if result.get("content_type"):
            details.append(result["content_type"])
        self.size.set_subtitle(" · ".join(details))
        if self._auto_name and result.get("filename"):
            self.name.set_text(result["filename"])
        self._set_parts_allowed(resumable)

    def _set_parts_allowed(self, allowed: bool) -> None:
        self.parts.set_sensitive(allowed)
        if allowed:
            self.parts_row.set_subtitle("More parts can be faster on slow servers")
        else:
            self.parts.set_value(1)
            self.parts_row.set_subtitle("This server does not support parts or resuming")

    # --- submitting ----------------------------------------------------------------------------

    def _validate(self, *_) -> bool:
        url_ok = is_url(self.url.get_text()) or is_torrent_link(self.url.get_text())
        if self.file_checks and not any(box.get_active() for _, box, _ in self.file_checks):
            url_ok = False  # nothing selected
        limit_ok = parse_limit(self.limit.get_text()) is not None
        for row, ok in ((self.url, url_ok or not self.url.get_text()), (self.limit, limit_ok)):
            (row.remove_css_class if ok else row.add_css_class)("error")
        for button in (self.start_button, self.later_button):
            button.set_sensitive(url_ok and limit_ok)
        return url_ok and limit_ok

    def options(self, start: bool) -> dict[str, Any]:
        options = {
            "name": self.name.get_text().strip() or None,
            "dir": self.folder(),
            "connections": int(self.parts.get_value()),
            "speed_limit": parse_limit(self.limit.get_text()) or None,
            "headers": self.headers() or None,
            "start": start,
        }
        if self.torrent is not None:
            options.update(kind="torrent", sequential=self.sequential.get_active(), name=None)
            if self.torrent:
                priorities = [0] * self.torrent.get("num_files", len(self.file_checks))
                for index, box, _ in self.file_checks:
                    priorities[index] = 1 if box.get_active() else 0
                options.update(priorities=priorities, metadata=self.torrent.get("metadata"))
            options.pop("connections", None)
            return options
        choice = self._choice()
        if self.media and choice:
            options.update(kind="media", format=choice["selector"], format_label=choice["label"],
                           title=self.media.get("title"), subtitles=self._subtitle_languages() or None)
            if self._auto_name:
                options["name"] = None  # let yt-dlp name the file after the title
        return options

    def _submit(self, start: bool) -> None:
        if not self._validate():
            return
        for button in (self.start_button, self.later_button):
            button.set_sensitive(False)
        self.client.add(self.url.get_text().strip(), self.options(start), self._on_result)

    def _on_result(self, download_id, error) -> None:
        if error:
            self._validate()
            self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(error.message)))
            return
        if self.on_added:
            self.on_added(download_id)
        self.close()
