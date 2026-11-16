"""The smaller dialogs: batch download, import links, checksum, diagnostics log, parts, torrent details,
site login, host rule and one download's schedule."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFontDatabase, QGuiApplication, QPainter
from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

from ..batch import expand
from ..files import algorithm_for
from ..format import human
from ..ui.text import WINDOW_KEYS
from .forms import Card, ComboRow, Dialog, EntryRow, Row, ScheduleEditor, SpinRow, SwitchRow
from .theme import current

MAX_PEERS_SHOWN = 40
MAX_IMPORT = 2000


class BatchDialog(Dialog):
    """1DM's batch download: one * in the link, replaced by a range of numbers or letters."""

    def __init__(self, parent: QWidget, client, on_done: Callable[[str], None]):
        super().__init__(parent, "Batch download", 560, 640, action="Add")
        self.client, self.on_done = client, on_done
        self.url = EntryRow("Link with * where the number goes", "", "https://example.com/img*.jpg")
        self.kind = ComboRow("Replace * with", [("numbers", "Numbers"), ("letters", "Letters")])
        self.first = SpinRow("From", "", 0, 999_999)
        self.first.set_value(1)
        self.last = SpinRow("To", "", 0, 999_999)
        self.last.set_value(10)
        self.pad = SpinRow("Zero padding", "Digits per number, e.g. 3 gives 001", 0, 10)
        self.first_letter = EntryRow("From letter", "a")
        self.last_letter = EntryRow("To letter", "z")
        self.form = Card("", "", self.url, self.kind, self.first, self.last, self.pad, self.first_letter,
                         self.last_letter)
        self.preview = Row("Preview", "Type a link with one *")
        self.schedule = ScheduleEditor()
        self.start = SwitchRow("Start now", "", True)
        self.page.add(self.form)
        self.page.add(Card("Links", "", self.preview))
        self.page.add(Card("Options", "", self.schedule, self.start))
        for row in (self.url, self.first_letter, self.last_letter):
            row.changed.connect(lambda _t: self._update())
        for row in (self.first, self.last, self.pad):
            row.changed.connect(lambda _v: self._update())
        self.kind.chosen.connect(lambda _v: self._update())
        self.links: list[str] = []
        client.get_settings(lambda settings, error: settings and self.schedule.set_defaults(settings))
        self._update()

    def _update(self) -> None:
        letters = self.kind.value() == "letters"
        for row in (self.first, self.last, self.pad):
            row.setVisible(not letters)
        for row in (self.first_letter, self.last_letter):
            row.setVisible(letters)
        self.form.sync_lines()
        try:
            if letters:
                self.links = expand(self.url.text(), letters=(self.first_letter.text().strip(),
                                                              self.last_letter.text().strip()))
            else:
                self.links = expand(self.url.text(), numbers=(int(self.first.value()), int(self.last.value()),
                                                              int(self.pad.value())))
            first, last = self.links[0], self.links[-1]
            self.preview.set_title(f"{len(self.links)} links")
            self.preview.set_subtitle(first if len(self.links) == 1 else f"{first}\n…\n{last}")
        except ValueError as e:
            self.links = []
            self.preview.set_title("Preview")
            self.preview.set_subtitle(str(e)[0].upper() + str(e)[1:] if self.url.text() else "Type a link with one *")
        self.action_button.setEnabled(bool(self.links))

    def options(self) -> dict[str, Any]:
        options: dict[str, Any] = {"start": self.start.is_on(), "scheduled": self.schedule.is_on()}
        if self.schedule.is_on():
            options.update(self.schedule.window())
        return options

    def accept(self) -> None:
        links, options = list(self.links), self.options()
        for link in links:
            self.client.add(link, options)
        self.on_done(f"{len(links)} downloads added")
        super().accept()


class ImportLinksDialog(Dialog):
    """Links from a text file, with a box each; links already in the list start unticked."""

    def __init__(self, parent: QWidget, client, links: list[str], known: list[str], on_done: Callable[[str], None]):
        super().__init__(parent, "Import links", 620, 640, action="Add")
        self.client, self.on_done = client, on_done
        self.start = SwitchRow("Start now")
        self.page.add(Card("", "", self.start))
        known_set = set(known)
        self.checks: list[tuple[str, QCheckBox]] = []
        rows = Card(f"{len(links)} links", "Links already in your list are unticked.")
        for link in links[:MAX_IMPORT]:
            check = QCheckBox(link)
            check.setChecked(link not in known_set)
            row = QFrame()
            row.setObjectName("row")
            line = QHBoxLayout(row)
            line.setContentsMargins(14, 8, 14, 8)
            line.addWidget(check)
            rows.add(row)
            self.checks.append((link, check))
        self.page.add(rows)

    def accept(self) -> None:
        chosen = [link for link, check in self.checks if check.isChecked()]
        for link in chosen:
            self.client.add(link, {"start": self.start.is_on(),
                                   **({"kind": "torrent"} if link.startswith("magnet:") else {})})
        self.on_done(f"{len(chosen)} downloads added")
        super().accept()


class ChecksumDialog(Dialog):
    """MD5 and SHA-256 of a finished file, and "Compare with clipboard" (1DM)."""

    def __init__(self, parent: QWidget, client, download_id: int, title: str):
        super().__init__(parent, "Checksum", 640, 380)
        self.client, self.download_id = client, download_id
        self.values: dict[str, str] = {}
        self.rows: dict[str, Row] = {}
        card = Card(title)
        for algorithm, label in (("md5", "MD5"), ("sha256", "SHA-256")):
            copy = QPushButton("Copy")
            copy.clicked.connect(lambda _c=False, a=algorithm: self._copy(a))
            row = Row(label, "Calculating…", copy)
            card.add(row)
            self.rows[algorithm] = row
            client.checksum(download_id, algorithm, lambda value, error, a=algorithm: self._got(a, value, error))
        compare = QPushButton("Compare with clipboard")
        compare.clicked.connect(lambda: self.show_comparison(QGuiApplication.clipboard().text()))
        self.result = Row("Compare with a checksum you copied", "", compare)
        self.page.add(card)
        self.page.add(Card("", "", self.result))

    def _got(self, algorithm: str, value: str | None, error) -> None:
        self.values[algorithm] = value or ""
        self.rows[algorithm].set_subtitle(value if value else f"Failed: {error.message}")

    def _copy(self, algorithm: str) -> None:
        if self.values.get(algorithm):
            QGuiApplication.clipboard().setText(self.values[algorithm])
            self.toast("Copied")

    def show_comparison(self, text: str | None) -> None:
        expected = (text or "").strip().lower()
        algorithm = algorithm_for(expected)
        if algorithm is None:
            self.result.set_subtitle("The clipboard doesn't hold an MD5, SHA-1 or SHA-256 checksum")
            return
        if algorithm not in self.values:  # SHA-1 isn't shown: calculate it now, then compare
            self.result.set_subtitle(f"Calculating {algorithm.upper()}…")
            self.client.checksum(self.download_id, algorithm,
                                 lambda value, error: self._got_extra(algorithm, value, error, text))
            return
        same = self.values[algorithm] == expected
        self.result.set_subtitle(f"✓ Matches ({algorithm.upper()})" if same
                                 else f"✗ Does not match ({algorithm.upper()})")

    def _got_extra(self, algorithm: str, value: str | None, error, text: str) -> None:
        if error:
            self.result.set_subtitle(f"Failed: {error.message}")
            return
        self.values[algorithm] = value
        self.show_comparison(text)


class LogDialog(Dialog):
    """Download diagnostics: requests, responses, retries."""

    def __init__(self, parent: QWidget, client, download_id: int, title: str):
        super().__init__(parent, f"Diagnostics — {title}", 780, 540)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.view.setMinimumHeight(380)
        self.page.add(self.view)
        copy = QPushButton("Copy all")
        copy.clicked.connect(lambda: (QGuiApplication.clipboard().setText(self.view.toPlainText()),
                                      self.toast("Copied")))
        self.footer.insertWidget(0, copy)
        client.get_log(download_id, self._show)

    def _show(self, lines: list[str] | None, error) -> None:
        if error:
            text = f"Could not read the log: {error.message}"
        else:
            text = "\n".join(lines) if lines else ("Nothing recorded yet — the log covers downloads started since "
                                                   "the download service last started.")
        self.view.setPlainText(text)


class PartsBar(QWidget):
    """Where each connection is in the file."""

    def __init__(self):
        super().__init__()
        self.parts: list[dict[str, Any]] = []
        self.setFixedHeight(28)

    def paintEvent(self, _event) -> None:
        c = current()
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(c.track))
        if not self.parts or self.parts[-1]["last"] >= 1 << 61:
            return
        total = self.parts[-1]["last"] + 1
        width = self.width()
        for p in self.parts:
            x = p["first"] / total * width
            color = QColor(c.accent)
            color.setAlphaF(1.0 if p["active"] else 0.7)
            painter.fillRect(QRectF(x, 0, p["done"] / total * width, self.height()), color)
            painter.fillRect(QRectF(x, 0, 1, self.height()), QColor(c.dim))  # where the part starts


class PartsDialog(Dialog):
    """1DM's multi-part view, live."""

    def __init__(self, parent: QWidget, client, download_id: int, title: str):
        super().__init__(parent, "Parts", 640, 600)
        self.client, self.download_id = client, download_id
        self.bar = PartsBar()
        self.summary = QLabel()
        self.summary.setObjectName("rowsub")
        head = Card(title)
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(14, 12, 14, 12)
        column.addWidget(self.bar)
        column.addWidget(self.summary)
        head.add(box)
        self.rows = Card("Parts")
        self.row_widgets: list[Row] = []
        self.page.add(head)
        self.page.add(self.rows)
        self.timer = QTimer(self, interval=500)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()
        self._refresh()

    def _refresh(self) -> None:
        self.client.get_parts(self.download_id, self._show)

    def _show(self, parts: list[dict[str, Any]] | None, error) -> None:
        if error:
            self.summary.setText(error.message)
            return
        parts = parts or []
        self.bar.parts = parts
        self.bar.update()
        if not parts:
            self.summary.setText("No parts: the download hasn't started, or it is a video or torrent")
        else:
            total = parts[-1]["last"] + 1 if parts[-1]["last"] < 1 << 61 else 0
            done = sum(p["done"] for p in parts)
            active = sum(1 for p in parts if p["active"])
            self.summary.setText(f"{len(parts)} parts · {active} downloading · "
                                 + (f"{human(done)} of {human(total)}" if total else human(done)))
        while len(self.row_widgets) < len(parts):
            self.row_widgets.append(self.rows.add(Row("")))
        for index, row in enumerate(self.row_widgets):
            row.setVisible(index < len(parts))
            if index < len(parts):
                p = parts[index]
                size = p["last"] - p["first"] + 1
                state = "downloading" if p["active"] else ("finished" if p["done"] >= size else "waiting")
                row.set_title(f"Part {index + 1} · {state}")
                row.set_subtitle(f"{human(p['done'])} of {human(size)} · from byte {p['first']:,}")
        self.rows.sync_lines()


class TorrentDetailsDialog(Dialog):
    """Overview, files, peers and trackers of a torrent, refreshed every second."""

    def __init__(self, parent: QWidget, client, download_id: int, title: str, state: Callable[[], str]):
        super().__init__(parent, title, 640, 600)
        self.client, self.download_id, self.state = client, download_id, state
        self.cards: list[Card] = []
        self.timer = QTimer(self, interval=1000)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()
        self._refresh()

    def _refresh(self) -> None:
        self.client.torrent_details(self.download_id, self._show)

    def _show(self, info: dict[str, Any] | None, error) -> None:
        scroll = self.page.verticalScrollBar().value()
        for card in self.cards:
            card.hide()
            card.deleteLater()
        self.cards = []
        if error or info is None:
            self._card("Details", [("Not available", error.message if error else "")])
            return
        seeds = info.get("seeds", 0)
        self._card("Overview", [
            ("State", "Seeding" if info.get("seeding") else self.state().capitalize()),
            ("Peers", f"{info.get('peers', 0)} connected · {seeds} seed{'s' if seeds != 1 else ''}"),
            ("Speed", f"↓ {human(info.get('download_speed', 0))}/s · ↑ {human(info.get('upload_speed', 0))}/s"),
            ("Uploaded", f"{human(info.get('uploaded', 0))} · ratio {info.get('ratio', 0):.2f}"),
        ])
        files = info.get("files") or []
        if files:
            card = self._card(f"Files ({len(files)})", [])
            for f in files:
                bar = QProgressBar()
                bar.setRange(0, 1000)
                bar.setValue(int(1000 * (f["done"] / f["size"] if f["size"] else 1.0)))
                bar.setTextVisible(False)
                bar.setFixedWidth(140)
                card.add(Row(Path(f["path"]).name, f"{human(f['done'])} of {human(f['size'])}"
                             + ("" if f.get("wanted", True) else " · skipped"), bar))
        peers = info.get("peer_list") or []
        if peers:
            self._card(f"Peers ({len(peers)})", [
                (p["ip"], f"{p['client'] or 'unknown client'} · {p['progress'] * 100:.0f}% · "
                          f"↓ {human(p['down'])}/s ↑ {human(p['up'])}/s")
                for p in sorted(peers, key=lambda p: -p["down"])[:MAX_PEERS_SHOWN]])
        trackers = info.get("trackers") or []
        if trackers:
            self._card(f"Trackers ({len(trackers)})", [(t["url"], t.get("message", "")) for t in trackers])
        QTimer.singleShot(0, lambda: self.page.verticalScrollBar().setValue(scroll))

    def _card(self, title: str, rows: list[tuple[str, str]]) -> Card:
        card = Card(title, "", *(Row(name, value) for name, value in rows))
        self.page.add(card)
        self.cards.append(card)
        return card


class LoginDialog(Dialog):
    """A site login (HTTP Basic/Digest) for a host; the password goes to the keyring."""

    def __init__(self, parent: QWidget, host: str, username: str, on_save: Callable[[str, str, str, bool], None],
                 subdomains: bool = True):
        super().__init__(parent, "Site login", 480, 420, action="Save")
        self.on_save = on_save
        self.host = EntryRow("Site", host, "example.com")
        self.user = EntryRow("User name", username)
        self.password = EntryRow("Password", "", password=True)
        self.subdomains = SwitchRow("Also for subdomains", "", subdomains)
        self.page.add(Card("", "Used when the site asks for a user name and password.", self.host, self.user,
                           self.password, self.subdomains))

    def accept(self) -> None:
        if not self.host.text().strip():
            self.host.set_error(True)
            return
        self.on_save(self.host.text().strip(), self.user.text().strip(), self.password.text(), self.subdomains.is_on())
        super().accept()


class HostRuleDialog(Dialog):
    def __init__(self, parent: QWidget, rule: dict[str, Any] | None, on_save: Callable[[str, int, int, bool], None]):
        super().__init__(parent, "Edit host rule" if rule else "Add host rule", 480, 420, action="Save")
        self.on_save = on_save
        self.host = EntryRow("Host", rule["host"] if rule else "", "example.com")
        self.connections = SpinRow("Max connections per download", "0 means no limit", 0, 32)
        self.downloads = SpinRow("Max parallel downloads", "0 means no limit", 0, 20)
        self.subdomains = SwitchRow("Include subdomains", "", rule["include_subdomains"] if rule else True)
        if rule:
            self.connections.set_value(rule["max_connections"])
            self.downloads.set_value(rule["max_downloads"])
        self.page.add(Card("", "", self.host, self.connections, self.downloads, self.subdomains))

    def accept(self) -> None:
        host = self.host.text().strip().lower()
        if not host:
            self.host.set_error(True)
            return
        self.on_save(host, int(self.connections.value()), int(self.downloads.value()), self.subdomains.is_on())
        super().accept()


class ScheduleDialog(Dialog):
    """One download's schedule; `values` is its row from the service (scheduled + effective times)."""

    def __init__(self, parent: QWidget, title: str, values: dict[str, Any], defaults: dict[str, Any],
                 on_save: Callable[[bool, dict[str, str]], None]):
        super().__init__(parent, "Schedule", 520, 460, action="Save")
        self.on_save = on_save
        self.editor = ScheduleEditor()
        self.editor.set_defaults(defaults)
        self.editor.times.load(*(values.get(k) or d for k, d in zip(WINDOW_KEYS, self.editor.defaults)))
        self.editor.set_on(bool(values.get("scheduled")))
        self.editor.set_open(True)
        self.page.add(Card(title, "", self.editor))

    def accept(self) -> None:
        self.on_save(self.editor.is_on(), self.editor.window())
        super().accept()
