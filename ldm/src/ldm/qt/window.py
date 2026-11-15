"""LDM's main window ("A + G + Classic"): menus and search, a labelled toolbar, the sidebar, the download
table, a details panel that folds away, and a status bar.

The window only talks to the background service; closing it never stops downloads.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMenuBar, QMessageBox, QStackedWidget, QToolButton,
                               QVBoxLayout, QWidget)

from .. import __version__
from ..batch import parse_link_list
from ..clipboard import Offer
from ..format import human, parse_rate
from ..schedule import describe_start, next_start
from ..ui.item import CATEGORIES, LIBRARY, Download
from ..ui.states import FINISH_ACTIONS, PAUSABLE, RESUMABLE
from ..ui.text import is_url, limit_text, short_path
from .details import DetailsPanel
from .model import DownloadsFilter, DownloadsModel, queue_label
from .sidebar import Sidebar
from .table import DownloadTable
from .theme import Colors, colors_for, icon, logo, palette, stylesheet
from .widgets import EmptyState, MessageBar, Toast

if TYPE_CHECKING:
    from .client import DaemonClient

LATER = "Comes with the next update of the new window"  # dialogs arrive in the next step of the Qt work
EMPTY = {
    "all": ("No downloads", "Add a link, paste one into the search field, or drop it here."),
    "downloading": ("Nothing downloading", "Active, queued and paused downloads appear here."),
    "scheduled": ("Nothing scheduled", "Downloads on the schedule appear here."),
    "finished": ("No finished downloads", "Completed files appear here."),
    "problems": ("Nothing needs attention", "Failed downloads and expired links appear here."),
}


class MainWindow(QMainWindow):
    def __init__(self, client: DaemonClient, settings: QSettings | None = None):
        super().__init__()
        self.client = client
        self.settings = settings or QSettings("LDM", "LDM")
        self.theme_choice = str(self.settings.value("theme", "system"))
        self.c = colors_for(self.theme_choice)
        self.service: dict = {}  # the service's settings, as last fetched
        self.downloads = DownloadsModel(self)
        self.proxy = DownloadsFilter(self.downloads, self)
        self.offer, self.offered = Offer(), ""
        self.setWindowTitle("LDM")
        self.resize(1280, 840)
        self.setMinimumSize(900, 560)
        self.setAcceptDrops(True)

        self._build_actions()
        self.setMenuWidget(self._build_topbar())
        central = QWidget()
        central.setObjectName("central")
        column = QVBoxLayout(central)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self._build_toolbar())
        self.service_bar = MessageBar("Start", self.colors, closable=False)
        self.service_bar.button_clicked.connect(self.start_daemon)
        self.offer_bar = MessageBar("Download", self.colors, closable=True)
        self.offer_bar.button_clicked.connect(self._download_offered)
        column.addWidget(self.service_bar)
        column.addWidget(self.offer_bar)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.sidebar = Sidebar(self.colors)
        self.sidebar.chosen.connect(self._show_view)
        body.addWidget(self.sidebar)
        self.table = DownloadTable(self.proxy, self.colors)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.table.context_menu.connect(self._row_menu)
        self.table.activated_item.connect(self._activated)
        self.empty = EmptyState()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.empty)
        list_box = QWidget()
        list_layout = QVBoxLayout(list_box)
        list_layout.setContentsMargins(8, 4, 8, 4)
        list_layout.addWidget(self.stack)
        body.addWidget(list_box, 1)
        self.details = DetailsPanel(self.colors)
        self.details.action.connect(self._details_action)
        self.details.hide_requested.connect(lambda: self.details_action.setChecked(False))
        self.details.more_requested.connect(self._row_menu)
        body.addWidget(self.details)
        column.addLayout(body, 1)
        column.addWidget(self._build_statusbar())
        self.setCentralWidget(central)
        self.toast_widget = Toast(list_box)

        self.details.setVisible(self.details_action.isChecked())
        self.downloads.changed.connect(self._downloads_changed)
        self.downloads.dataChanged.connect(self._row_data_changed)
        self.proxy.rowsInserted.connect(self._sync_empty)
        self.proxy.rowsRemoved.connect(self._sync_empty)
        self.proxy.modelReset.connect(self._sync_empty)
        self.proxy.layoutChanged.connect(self._sync_empty)

        client.connected_changed.connect(self._connection_changed)
        client.added.connect(self.refresh)
        client.removed.connect(self.downloads.remove)
        client.state_changed.connect(self._state_changed)
        client.progress.connect(self.downloads.apply_progress)
        self._timers = []
        for seconds, callback in ((1, self._tick), (2, self._refresh_torrents), (30, self._load_service_settings)):
            timer = QTimer(self, interval=seconds * 1000)
            timer.timeout.connect(callback)
            timer.start()
            self._timers.append(timer)
        QGuiApplication.styleHints().colorSchemeChanged.connect(lambda _scheme: self.apply_theme())
        self.apply_theme()
        self._connection_changed(client.connected)
        self._update_actions()

    def colors(self) -> Colors:
        return self.c

    # --- building ----------------------------------------------------------------------------

    def _action(self, text: str, handler: Callable[[], None], shortcut: str | QKeySequence.StandardKey | None = None,
                tip: str = "", enabled: bool = True) -> QAction:
        action = QAction(text, self)
        action.triggered.connect(lambda _checked=False: handler())
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
        if tip:
            action.setToolTip(tip)
            action.setStatusTip(tip)
        action.setEnabled(enabled)
        self.addAction(action)
        return action

    def _later(self, text: str, shortcut: str | None = None) -> QAction:
        action = self._action(text, lambda: None, shortcut, LATER, enabled=False)
        action.setData("later")
        return action

    def _build_actions(self) -> None:
        a = self._action
        self.add_action = a("Add link…", self.ask_add, QKeySequence.StandardKey.New, "Add a download (Ctrl+N)")
        self.resume_action = a("Resume", self.resume_selected, "Ctrl+R", "Resume the selected downloads")
        self.pause_action = a("Pause", self.pause_selected, "Ctrl+P", "Pause the selected downloads")
        self.pause_all_action = a("Pause all", lambda: self.client.pause_all(self._report),
                                  tip="Pause every download")
        self.resume_all_action = a("Resume all", lambda: self.client.resume_all(self._report),
                                   tip="Resume every paused download")
        self.delete_action = a("Delete…", self.delete_selected, QKeySequence.StandardKey.Delete,
                               "Remove the selected downloads, and their files if you choose")
        self.start_queue_action = a("Start queue", self.start_queue, tip="Resume the paused downloads in the queue")
        self.stop_queue_action = a("Stop queue", self.stop_queue,
                                   tip="Pause the queued downloads; running ones finish")
        self.schedule_action = self._later("Schedule…")
        self.grab_action = self._later("Grab videos…")
        self.settings_action = self._later("Settings…", "Ctrl+,")
        self.batch_action = self._later("Batch download…")
        self.import_links_action = self._later("Import links…")
        self.details_action = a("Details", self._toggle_details, "F9", "Show or hide the details panel")
        self.details_action.setCheckable(True)
        self.details_action.setChecked(self.settings.value("details", True, type=bool))
        self.search_action = a("Search", self._focus_search, QKeySequence.StandardKey.Find)
        self.quit_action = a("Quit", QApplication.quit, QKeySequence.StandardKey.Quit,
                             "Close LDM; downloads continue in the background")
        self.open_action = a("Open", lambda: self._on_current(self.open_file))
        self.open_folder_action = a("Open folder", lambda: self._on_current(self.open_folder))
        self.copy_link_action = a("Copy link", lambda: self._on_current(self.copy_link), QKeySequence.StandardKey.Copy)
        self.set_url_action = a("Set a new link…", lambda: self._on_current(self.ask_new_url))
        self.limit_action = a("Speed limit…", lambda: self._on_current(self.ask_speed_limit))
        self.move_top_action = a("Move to top", lambda: self._on_current(self.move_to_top))
        self.rename_action = a("Rename…", lambda: self._on_current(self.ask_rename), "F2")
        self.move_to_action = a("Move to folder…", lambda: self._on_current(self.ask_move))
        self.export_links_action = a("Export links…", self.export_links)
        self.export_unfinished_action = a("Export unfinished…", self.export_unfinished)
        self.import_unfinished_action = a("Import unfinished…", self.import_unfinished)
        self.about_action = a("About LDM", self.about)

    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(44)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 12, 0)
        layout.setSpacing(4)
        self.logo = QLabel()
        layout.addWidget(self.logo)
        brand = QLabel("LDM")
        brand.setObjectName("brand")
        brand.setContentsMargins(8, 0, 14, 0)
        layout.addWidget(brand)
        menubar = QMenuBar()
        menubar.setNativeMenuBar(False)  # the menus sit in the window's own bar, as in the design
        tasks = menubar.addMenu("&Tasks")
        tasks.addActions([self.add_action, self.batch_action, self.grab_action])
        tasks.addSeparator()
        tasks.addActions([self.import_links_action, self.export_links_action])
        tasks.addSeparator()
        tasks.addActions([self.export_unfinished_action, self.import_unfinished_action])
        tasks.addSeparator()
        tasks.addAction(self.quit_action)
        file = menubar.addMenu("&File")
        file.addActions([self.open_action, self.open_folder_action, self.copy_link_action])
        file.addSeparator()
        file.addActions([self.rename_action, self.move_to_action])
        file.addSeparator()
        file.addAction(self.delete_action)
        downloads = menubar.addMenu("&Downloads")
        downloads.addActions([self.resume_action, self.pause_action])
        downloads.addSeparator()
        downloads.addActions([self.resume_all_action, self.pause_all_action, self.start_queue_action,
                              self.stop_queue_action])
        downloads.addSeparator()
        downloads.addActions([self.move_top_action, self.limit_action, self.schedule_action, self.set_url_action])
        downloads.addSeparator()
        finish = downloads.addMenu("When all finish")
        self.finish_group = QActionGroup(self)
        for label, value in FINISH_ACTIONS:
            action = finish.addAction(label)
            action.setCheckable(True)
            action.setData(value)
            action.setChecked(value == "nothing")
            self.finish_group.addAction(action)
        self.finish_group.triggered.connect(self._finish_chosen)
        view = menubar.addMenu("&View")
        view.addAction(self.details_action)
        view.addAction(self.search_action)
        theme = view.addMenu("Appearance")
        self.theme_group = QActionGroup(self)
        for label, value in (("Follow the system", "system"), ("Light", "light"), ("Dark", "dark")):
            action = theme.addAction(label)
            action.setCheckable(True)
            action.setData(value)
            action.setChecked(value == self.theme_choice)
            self.theme_group.addAction(action)
        self.theme_group.triggered.connect(lambda action: self.set_theme(action.data()))
        sort = view.addMenu("Sort by")
        for label, column in (("Queue order", -1), ("Name", 0), ("Size", 1), ("Progress", 2), ("Date added", 5)):
            action = sort.addAction(label)
            action.triggered.connect(lambda _c=False, col=column: self.table.set_sort(
                col, Qt.SortOrder.AscendingOrder if col in (-1, 0) else Qt.SortOrder.DescendingOrder))
        view.addSeparator()
        view.addAction(self.settings_action)
        help_menu = menubar.addMenu("&Help")
        help_menu.addAction(self._action("Keyboard shortcuts", self.show_shortcuts, "Ctrl+?"))
        help_menu.addAction(self._action("Report a problem", lambda: QDesktopServices.openUrl(
            QUrl("https://github.com/adiashishz/LDM/issues"))))
        help_menu.addAction(self.about_action)
        self.menubar = menubar
        layout.addWidget(menubar)
        layout.addStretch(1)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Search downloads or paste a link")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(280)
        self.search.setAccessibleName("Search downloads or paste a link")
        self.search.textChanged.connect(self._search_changed)
        self.search.returnPressed.connect(self._search_entered)
        self.search_icon = self.search.addAction(icon("search", "#888", 14), QLineEdit.ActionPosition.LeadingPosition)
        layout.addWidget(self.search)
        return bar

    def _build_toolbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("toolbar")
        bar.setFixedHeight(64)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(2)
        self.tool_buttons: list[tuple[QToolButton, str]] = []
        groups = [[(self.add_action, "add", "Add link"), (self.resume_action, "resume", "Resume"),
                   (self.pause_action, "pause", "Pause"), (self.pause_all_action, "pause-all", "Pause all"),
                   (self.delete_action, "delete", "Delete")],
                  [(self.schedule_action, "schedule", "Schedule"), (self.start_queue_action, "queue-start", "Start queue"),
                   (self.stop_queue_action, "queue-stop", "Stop queue")],
                  [(self.grab_action, "grab", "Grab videos"), (self.settings_action, "settings", "Settings")]]
        for number, group in enumerate(groups):
            if number:
                separator = QFrame()
                separator.setObjectName("toolsep")
                separator.setFixedSize(1, 36)
                layout.addSpacing(6)
                layout.addWidget(separator)
                layout.addSpacing(6)
            for action, icon_name, label in group:
                layout.addWidget(self._tool_button(action, icon_name, label, primary=action is self.add_action))
        layout.addStretch(1)
        layout.addWidget(self._tool_button(self.details_action, "details", "Details"))
        return bar

    def _tool_button(self, action: QAction, icon_name: str, label: str, primary: bool = False) -> QToolButton:
        button = QToolButton()
        button.setDefaultAction(action)
        button.setText(label)  # shorter than the menu text ("Add link…")
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setFixedHeight(52)
        button.setMinimumWidth(70)
        button.setIconSize(QSize(20, 20))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAccessibleName(label)
        if primary:
            button.setObjectName("primary")
        self.tool_buttons.append((button, icon_name))
        return button

    def _build_statusbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("statusbar")
        bar.setFixedHeight(28)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setSpacing(18)
        self.status_count, self.status_running, self.status_speed, self.status_limit, self.status_right = (
            QLabel(), QLabel(), QLabel(), QLabel(), QLabel())
        self.status_speed.setObjectName("speed")
        for label in (self.status_count, self.status_running, self.status_speed, self.status_limit):
            layout.addWidget(label)
        layout.addStretch(1)
        layout.addWidget(self.status_right)
        return bar

    # --- theme --------------------------------------------------------------------------------

    def set_theme(self, choice: str) -> None:
        self.theme_choice = choice
        self.settings.setValue("theme", choice)
        self.apply_theme()

    def apply_theme(self) -> None:
        self.c = colors_for(self.theme_choice)
        app = QApplication.instance()
        app.setPalette(palette(self.c))
        app.setStyleSheet(stylesheet(self.c))
        self.logo.setPixmap(logo(self.c.accent))
        for button, icon_name in self.tool_buttons:
            color = "#FFFFFF" if button.objectName() == "primary" else self.c.fg
            button.setIcon(icon(icon_name, color if button.isEnabled() else self.c.dim))
        self.search_icon.setIcon(icon("search", self.c.dim, 14, 2))
        for widget in (self.sidebar, self.details, self.service_bar, self.offer_bar):
            widget.restyle()
        self.table.viewport().update()

    # --- the service -------------------------------------------------------------------------

    def _connection_changed(self, connected: bool) -> None:
        self.service_bar.setVisible(not connected)
        if not connected:
            self.service_bar.show_text("The download service is not running")
        self.add_action.setEnabled(connected)
        if connected:
            self.client.list(lambda rows, error: rows is not None and self.downloads.load(rows))
            self._load_service_settings()
        self._update_actions()

    def start_daemon(self) -> None:
        try:
            self.client.start_daemon()
        except OSError as e:
            self.toast(f"Could not start the download service: {e}")
            return
        QTimer.singleShot(5000, lambda: self.client.connected or self.toast(
            "The download service did not start — see ~/.local/state/ldm/daemon.log"))

    def _load_service_settings(self) -> None:
        if self.client.connected:
            self.client.get_settings(self._got_service_settings)

    def _got_service_settings(self, settings, error) -> None:
        if error or not settings:
            return
        self.service = settings
        for action in self.finish_group.actions():
            action.setChecked(action.data() == settings.get("finish_action", "nothing"))
        self._update_status()

    def refresh(self, download_id: int) -> None:
        self.client.get(download_id, lambda row, error: row is not None and self.downloads.upsert(row))

    def _state_changed(self, download_id: int, state: str, error: str) -> None:
        self.downloads.apply_state(download_id, state, error)
        self.refresh(download_id)  # the name and size become known after the first probe

    def _refresh_torrents(self) -> None:
        """Peers, upload speed and ratio of active torrents aren't part of the Progress signal."""
        if self.client.connected:
            for d in self.downloads:
                if d.kind == "torrent" and d.state in ("running", "seeding"):
                    self.refresh(d.id)

    def _tick(self) -> None:
        """Once a second: the speed graph, and the shown download's parts while it downloads."""
        self.sidebar.add_speed(sum(d.speed for d in self.downloads if d.state == "running"))
        d = self.details.download
        if (d is not None and self.details.isVisible() and d.kind == "http" and d.state == "running"
                and self.client.connected):
            download_id = d.id
            self.client.get_parts(download_id, lambda parts, error: parts is not None
                                  and self.details.download is not None and self.details.download.id == download_id
                                  and self.details.show_download(self.details.download, parts))

    # --- list ---------------------------------------------------------------------------------

    def _show_view(self, view: str, group: str, queue: str) -> None:
        self.proxy.show(view, group, queue)
        self._sync_empty()

    def _search_changed(self, text: str) -> None:
        self.proxy.set_text("" if is_url(text) or text.strip().startswith("magnet:") else text)
        self._sync_empty()

    def _search_entered(self) -> None:
        text = self.search.text().strip()
        if is_url(text) or text.startswith("magnet:"):
            self.search.clear()
            self.request_download(text)

    def _focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def _sync_empty(self, *_) -> None:
        empty = self.proxy.rowCount() == 0
        if empty:
            if self.proxy.text:
                self.empty.set_text("No matching downloads", "Try other words.")
            elif self.proxy.group:
                self.empty.set_text(f"No {CATEGORIES[self.proxy.group].lower()}",
                                    "Downloads of this type appear here.")
            elif self.proxy.queue:
                self.empty.set_text(f"{queue_label(self.proxy.queue)} is empty", "")
            else:
                self.empty.set_text(*EMPTY[self.proxy.view])
        self.stack.setCurrentWidget(self.empty if empty else self.table)

    def _downloads_changed(self) -> None:
        self.sidebar.update_counts(self.downloads)
        self._update_status()
        self._update_actions()

    def _row_data_changed(self, top, bottom, _roles=()) -> None:
        d = self.details.download
        if d is not None and top.row() <= self.downloads.index_of(d.id).row() <= bottom.row():
            self.details.show_download(d)

    def _selection_changed(self, *_) -> None:
        items = self.table.selected_items()
        self.details.show_download(items[0] if len(items) == 1 else self.table.current_item() if items else None)
        self._update_actions()

    def _update_actions(self) -> None:
        items = self.table.selected_items() if hasattr(self, "table") else []
        states = {d.state for d in items}
        connected = self.client.connected
        current = items[0] if len(items) == 1 else None
        for action, enabled in (
                (self.resume_action, connected and bool(states & RESUMABLE)),
                (self.pause_action, connected and bool(states & PAUSABLE)),
                (self.delete_action, connected and bool(items)),
                (self.pause_all_action, connected), (self.resume_all_action, connected),
                (self.start_queue_action, connected), (self.stop_queue_action, connected),
                (self.open_action, current is not None and current.state in {"done", "seeding"}),
                (self.open_folder_action, current is not None and bool(current.path)),
                (self.copy_link_action, bool(items)),
                (self.set_url_action, connected and current is not None
                 and current.state in {"paused", "error", "expired", "changed", "queued"}),
                (self.limit_action, connected and current is not None
                 and current.state in {"queued", "running", "paused", "error"}),
                (self.move_top_action, connected and current is not None and current.state in {"queued", "paused"}),
                (self.rename_action, connected and current is not None
                 and current.state in {"queued", "paused", "error", "expired", "changed", "done"}),
                (self.move_to_action, connected and current is not None
                 and current.state in {"queued", "paused", "error", "expired", "changed", "done"}),
                (self.export_unfinished_action, connected), (self.import_unfinished_action, connected),
                (self.export_links_action, len(self.downloads) > 0)):
            action.setEnabled(enabled)
        if hasattr(self, "tool_buttons"):
            for button, icon_name in self.tool_buttons:
                color = "#FFFFFF" if button.objectName() == "primary" else self.c.fg
                button.setIcon(icon(icon_name, color if button.isEnabled() else self.c.dim))

    def _update_status(self) -> None:
        downloads = list(self.downloads)
        running = [d for d in downloads if d.state == "running"]
        self.status_count.setText(f"{len(downloads)} download{'s' if len(downloads) != 1 else ''}")
        self.status_running.setText(f"{len(running)} running")
        self.status_speed.setText(f"↓ {human(sum(d.speed for d in running))}/s")
        limit = self.service.get("speed_limit", 0)
        self.status_limit.setText(f"Speed limit: {human(limit) + '/s' if limit else 'off'}")
        right = []
        now = datetime.now()
        starts = [s for d in downloads if d.on_schedule and d.state == "queued" and d.schedule_start
                  and (s := next_start(now, d.schedule_start, d.schedule_stop, d.schedule_days))]
        if starts:
            right.append(f"Next scheduled start: {describe_start(min(starts), now)}")
        folder = self.service.get("download_dir")
        if folder:
            try:
                right.append(f"{human(shutil.disk_usage(folder).free)} free")
            except OSError:
                pass
        self.status_right.setText(" · ".join(right))

    def _toggle_details(self) -> None:
        self.details.setVisible(self.details_action.isChecked())
        self.settings.setValue("details", self.details_action.isChecked())

    def _activated(self, d: Download) -> None:
        if d.state in {"done", "seeding"}:
            self.open_file(d)
        else:
            self.details_action.setChecked(True)
            self._toggle_details()

    def _row_menu(self, position) -> None:
        if not self.table.selected_items():
            return
        menu = QMenu(self)
        menu.addActions([self.open_action, self.open_folder_action, self.copy_link_action])
        menu.addSeparator()
        menu.addActions([self.resume_action, self.pause_action, self.move_top_action, self.limit_action,
                         self.schedule_action, self.set_url_action])
        menu.addSeparator()
        menu.addActions([self.rename_action, self.move_to_action])
        menu.addSeparator()
        menu.addAction(self.delete_action)
        menu.exec(position)

    def _details_action(self, name: str) -> None:
        d = self.details.download
        if d is not None:
            getattr(self, name)(d)

    def _on_current(self, handler: Callable[[Download], None]) -> None:
        items = self.table.selected_items()
        if items:
            handler(items[0])

    # --- acting on downloads ----------------------------------------------------------------

    def toast(self, text: str, button: str = "", action: Callable[[], None] | None = None) -> None:
        self.toast_widget.show_message(text, button, action)

    def _report(self, _result, error) -> None:
        if error:
            self.toast(error.message)

    def _for_each(self, items: list[Download], call: Callable, nothing: str = "") -> None:
        """Call `call(id, callback)` for each download; one message for any failures."""
        if not items:
            if nothing:
                self.toast(nothing)
            return
        pending, errors = len(items), []

        def done(_result, error) -> None:
            nonlocal pending
            pending -= 1
            if error:
                errors.append(error.message)
            if not pending and errors:
                self.toast(errors[0] if len(errors) == 1 else f"{len(errors)} downloads failed: {errors[0]}")

        for d in items:
            call(d.id, done)

    def pause(self, d: Download) -> None:
        self.client.pause(d.id, self._report)

    def resume(self, d: Download) -> None:
        self.client.resume(d.id, self._report)

    def pause_selected(self) -> None:
        self._for_each([d for d in self.table.selected_items() if d.state in PAUSABLE], self.client.pause,
                       "None of the selected downloads is running or queued")

    def resume_selected(self) -> None:
        self._for_each([d for d in self.table.selected_items() if d.state in RESUMABLE], self.client.resume,
                       "None of the selected downloads is paused or failed")

    def start_queue(self) -> None:
        self._for_each([d for d in self.downloads if d.state == "paused" and not d.on_schedule],
                       self.client.resume, "No paused downloads in the queue")

    def stop_queue(self) -> None:
        self._for_each([d for d in self.downloads if d.state == "queued"], self.client.pause,
                       "No downloads are waiting in the queue")

    def delete_selected(self) -> None:
        items = self.table.selected_items()
        if not items:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Delete")
        box.setText(f"Remove {len(items)} downloads?" if len(items) > 1 else f"Remove “{items[0].title}”?")
        box.setInformativeText("Remove from the list and keep the files, or delete the files too "
                               "(including unfinished data).")
        keep = box.addButton("Remove from list", QMessageBox.ButtonRole.AcceptRole)
        delete = box.addButton("Delete files", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(keep)
        box.exec()
        if box.clickedButton() in (keep, delete):
            files = box.clickedButton() is delete
            self._for_each(items, lambda download_id, callback: self.client.remove(download_id, files, callback))

    def open_file(self, d: Download) -> None:
        if d.path and not QDesktopServices.openUrl(QUrl.fromLocalFile(d.path)):
            self.toast("Could not open the file")

    def open_folder(self, d: Download) -> None:
        if d.dir and not QDesktopServices.openUrl(QUrl.fromLocalFile(d.dir)):
            self.toast("Could not open the folder")

    def copy_link(self, d: Download) -> None:
        links = [item.url for item in self.table.selected_items()] or [d.url]
        QGuiApplication.clipboard().setText("\n".join(links))
        self.toast("Link copied" if len(links) == 1 else f"{len(links)} links copied")

    def move_to_top(self, d: Download) -> None:
        # The service sends no signal for position changes, so fetch the row to re-sort it.
        self.client.move(d.id, 0, lambda r, e: self._report(r, e) if e else self.refresh(d.id))

    def ask_new_url(self, d: Download) -> None:
        clipboard = QGuiApplication.clipboard().text().strip()
        url, ok = QInputDialog.getText(self, "Set a new link", "Paste a fresh link to the same file. "
                                       "Downloaded data is kept when the size matches.",
                                       text=clipboard if is_url(clipboard) else "")
        if not ok or not url.strip():
            return
        if not is_url(url):
            self.toast("That is not an http(s) link")
            return
        self.client.set_url(d.id, url.strip(), self._report)

    def ask_speed_limit(self, d: Download) -> None:
        text, ok = QInputDialog.getText(self, "Speed limit", "Maximum speed for this download, e.g. 500K or 2M. "
                                        "0 means unlimited.", text=limit_text(d.speed_limit))
        if not ok:
            return
        try:
            rate = parse_rate(text or "0")
        except ValueError:
            self.toast(f"Invalid speed limit: {text}")
            return
        self.client.set_speed_limit(d.id, rate, lambda r, e: self._report(r, e) if e else self.refresh(d.id))

    def ask_rename(self, d: Download) -> None:
        name, ok = QInputDialog.getText(self, "Rename", "A new name for the file. An existing file is never "
                                        "replaced.", text=d.name or d.title)
        if ok and name.strip() and name.strip() != d.name:
            self.client.rename(d.id, name.strip(), self._moved)

    def ask_move(self, d: Download) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Move to folder", d.dir or str(Path.home()))
        if folder:
            self.client.move_to(d.id, folder, self._moved)

    def _moved(self, path, error) -> None:
        if error:
            self._report(None, error)
        else:
            self.toast(f"Now at {short_path(path)}")

    def _finish_chosen(self, action: QAction) -> None:
        value = action.data()
        self.client.set_settings({"finish_action": value}, self._report)
        if value != "nothing":
            self.toast(f"When all downloads finish: {action.text()}")

    # --- adding ---------------------------------------------------------------------------------

    def ask_add(self) -> None:
        """A plain prompt until the new add dialog arrives (next step of the Qt work)."""
        clipboard = QGuiApplication.clipboard().text().strip()
        url, ok = QInputDialog.getText(self, "Add link", "Link to download (http, https or magnet):",
                                       text=clipboard if is_url(clipboard) or clipboard.startswith("magnet:") else "")
        if ok and url.strip():
            self.request_download(url.strip())

    def request_download(self, url: str, name: str = "", headers: dict[str, str] | None = None,
                         kind: str = "") -> None:
        """A link from the search field, the clipboard, a drop or the desktop: checked for duplicates, added."""
        if not self.client.connected:
            self.toast("Start the download service first")
            return
        kind = kind or ("torrent" if url.startswith("magnet:") or url.lower().endswith(".torrent") else "")
        options = {"name": name or None, "headers": headers or None, "kind": kind or None, "start": True}

        def add() -> None:
            self.client.add(url, options, added)

        def added(download_id, error) -> None:
            if error:
                self._report(None, error)
                return
            self.toast(f"Added {name or url}", "Undo", lambda: self.client.remove(download_id, True, self._report))

        def checked(found, error) -> None:
            if not found:
                add()
                return
            self.table.select_id(found[0]["id"])
            self.toast("Already in your list", "Add again", add)

        self.client.find_duplicates(url, checked)

    def _download_offered(self) -> None:
        self.offer_bar.hide()
        self.request_download(self.offered)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == event.Type.ActivationChange and self.isActiveWindow() and self.client.connected:
            self._check_clipboard()

    def _check_clipboard(self) -> None:
        """1DM's clipboard monitor: offer a copied link when the window gets focus."""
        if not self.service.get("clipboard_watch", True):
            return
        known = self.downloads.urls()
        if self.offered in known:
            self.offer_bar.hide()
        link = self.offer.check(QGuiApplication.clipboard().text(), known, self.service.get("clipboard_exclude", ""))
        if link:
            self.offered = link
            self.offer_bar.show_text(f"Download copied link? {link if len(link) <= 70 else link[:67] + '…'}")

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        data = event.mimeData()
        candidates = [u.toString() for u in data.urls()] if data.hasUrls() else data.text().splitlines()
        url = next((line.strip() for line in candidates
                    if is_url(line) or line.strip().startswith(("magnet:", "file:"))), "")
        if url:
            event.acceptProposedAction()
            self.request_download(url)

    # --- import and export ----------------------------------------------------------------------

    def export_links(self) -> None:
        links = [self.proxy.item(row).url for row in range(self.proxy.rowCount())]
        if not links:
            self.toast("No downloads to export")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export links", "ldm-links.txt", "Text files (*.txt)")
        if path:
            self._write(path, "".join(f"{u}\n" for u in links), f"{len(links)} links exported")

    def export_unfinished(self) -> None:
        def got(text, error) -> None:
            if error:
                self._report(None, error)
                return
            path, _ = QFileDialog.getSaveFileName(self, "Export unfinished downloads", "ldm-unfinished.json",
                                                  "JSON files (*.json)")
            if path:
                self._write(path, text, f"{len(json.loads(text)['downloads'])} downloads exported")

        self.client.export_downloads(True, got)

    def import_unfinished(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import unfinished downloads", "", "JSON files (*.json)")
        if not path:
            return
        try:
            text = Path(path).read_text()
        except (OSError, UnicodeDecodeError) as e:
            self.toast(f"Could not read the file: {e}")
            return
        self.client.import_downloads(text, lambda ids, error: self._report(None, error) if error else
                                     self.toast(f"{len(ids)} downloads imported — they are paused"))

    def import_link_file(self, path: str) -> list[str]:
        """Links in a text file (for the import dialog of the next step)."""
        return parse_link_list(Path(path).read_text(errors="replace"))

    def _write(self, path: str, text: str, message: str) -> None:
        try:
            Path(path).write_text(text)
            self.toast(message)
        except OSError as e:
            self.toast(f"Could not save: {e}")

    # --- help -----------------------------------------------------------------------------------

    def show_shortcuts(self) -> None:
        QMessageBox.information(self, "Keyboard shortcuts", "\n".join((
            "Ctrl+N\tAdd a link", "Ctrl+F\tSearch", "Ctrl+R\tResume", "Ctrl+P\tPause", "Delete\tDelete…",
            "F2\tRename", "F9\tShow or hide details", "Ctrl+C\tCopy link",
            "Ctrl+Q\tQuit (downloads continue in the background)", "",
            "Ctrl+click or Shift+click selects several downloads.")))

    def about(self) -> None:
        QMessageBox.about(self, "About LDM", f"<b>LDM {__version__}</b><br>Download manager: files, videos and "
                          "torrents.<br><br><a href='https://github.com/adiashishz/LDM'>github.com/adiashishz/LDM</a>"
                          "<br>MIT License · Aditya Ashish")

    def closeEvent(self, event) -> None:
        self.client.close()
        super().closeEvent(event)


__all__ = ["MainWindow", "LIBRARY"]
