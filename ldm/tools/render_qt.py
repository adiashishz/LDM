"""Dev tool: render the Qt window with sample downloads to PNG files, without a display or a service.

    QT_QPA_PLATFORM=offscreen python tools/render_qt.py OUT_DIR

Writes main-light.png, main-dark.png and a few states (details hidden, a finished and a failed download).
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QSettings, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ldm.qt.theme import ui_font  # noqa: E402
from ldm.qt.window import MainWindow  # noqa: E402
from ldm.ui.remote import ServiceCalls  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
NOW = time.time()
MB = 1024 * 1024


def row(i, name, url, state, size, done=0, speed=0.0, kind="http", hours_ago=1.0, **extra):
    return {"id": i, "url": url, "name": name, "dir": str(Path.home() / "Downloads"), "state": state,
            "size": size, "done": done, "speed": speed, "kind": kind, "position": i, "added_at": NOW - hours_ago * 3600,
            "finished_at": NOW - hours_ago * 3000 if state == "done" else 0.0, "resumable": True, "connections": 16,
            "error": extra.pop("error", ""), "speed_limit": 0, **extra}


SAMPLE = [
    row(1, "blender-4.5.2-linux-x64.tar.xz", "https://download.blender.org/release/Blender4.5/blender-4.5.2-linux-x64.tar.xz",
        "running", 286 * MB, 35 * MB, 3.0 * MB, hours_ago=0.5),
    row(2, "Distributed Systems, Lecture 7.mp4", "https://www.youtube.com/watch?v=abc", "running", 412 * MB,
        263 * MB, 5.2 * MB, kind="media", hours_ago=0.2, quality="1080p, MP4"),
    row(3, "ubuntu-26.04-desktop-amd64.iso", "magnet:?xt=urn:btih:abc", "running", 6100 * MB, 2300 * MB,
        9.8 * MB, kind="torrent", hours_ago=1.6, peers=142, ratio=0.12),
    row(4, "podcast-archive-2026.zip", "https://podcasts.example.org/archive/2026.zip", "queued", 114 * MB,
        3 * MB, hours_ago=20, scheduled=True, schedule_start="01:00", schedule_stop="06:00", schedule_days=""),
    row(5, "dataset-v3.parquet", "https://data.example.org/v3/dataset-v3.parquet", "expired", 1200 * MB,
        564 * MB, hours_ago=2.5, error="Link expired (403)"),
    row(6, "kdenlive-25.08.AppImage", "https://download.kde.org/stable/kdenlive/kdenlive-25.08.AppImage", "queued",
        118 * MB, hours_ago=2.6),
    row(7, "Fedora-Workstation-Live-x86_64-44.iso", "https://download.fedoraproject.org/pub/fedora/44/Fedora.iso",
        "done", 2300 * MB, hours_ago=1.1),
    row(8, "Lecture 07 - Distributed Systems.pdf", "https://courses.example.edu/ds/lecture07.pdf", "done",
        8.6 * MB, hours_ago=1.2),
    row(9, "wallpapers-pack.zip", "https://files.example.com/wallpapers-pack.zip", "paused", 412 * MB, 120 * MB,
        hours_ago=26),
    row(10, "song-of-the-week.flac", "https://music.example.com/song.flac", "done", 38 * MB, hours_ago=30),
]
PARTS = [{"first": i * 18 * MB, "last": (i + 1) * 18 * MB - 1, "done": int(18 * MB * f), "active": f < 1}
         for i, f in enumerate([.18, .12, .15, .09, .14, .2, .11, .13, .08, .16, .1, .12, .07, .14, .09, .11])]


class FakeClient(QObject, ServiceCalls):
    """Answers the window's calls from SAMPLE, at once."""

    added = Signal("qulonglong")
    removed = Signal("qulonglong")
    state_changed = Signal("qulonglong", str, str)
    progress = Signal(object)
    connected_changed = Signal(bool)
    connected = True

    def _call(self, method, signature, args, callback, timeout=None):
        answers = {"List": SAMPLE, "GetSettings": {"speed_limit": 0, "download_dir": str(Path.home()),
                                                   "clipboard_watch": False, "finish_action": "nothing"},
                   "GetParts": PARTS, "Get": next((r for r in SAMPLE if args and r["id"] == args[0]), None)}
        if callback:
            callback(answers.get(method), None)

    def close(self):
        pass

    def start_daemon(self):
        pass


def settle(app, seconds=0.3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setFont(ui_font())
    (OUT / "render.ini").unlink(missing_ok=True)  # start from the defaults every time
    settings = QSettings(str(OUT / "render.ini"), QSettings.Format.IniFormat)
    window = MainWindow(FakeClient(), settings)
    window.resize(1280, 840)
    window.show()
    for _ in range(20):
        window.sidebar.add_speed(sum(r["speed"] for r in SAMPLE) * (0.6 + 0.4 * (_ % 5) / 5))
    window.table.selectRow(0)
    window._tick()
    for theme in ("light", "dark"):
        window.set_theme(theme)
        settle(app)
        window.grab().save(str(OUT / f"main-{theme}.png"))
        print("saved", OUT / f"main-{theme}.png")
    window.set_theme("light")
    for number, name in ((4, "failed"), (6, "finished")):
        window.table.selectRow(number)
        settle(app)
        window.grab().save(str(OUT / f"main-{name}.png"))
    window.details_action.trigger()
    window.sidebar.choose("view:finished")
    settle(app)
    window.grab().save(str(OUT / "main-finished-view.png"))
    print("saved the rest to", OUT)


if __name__ == "__main__":
    main()
