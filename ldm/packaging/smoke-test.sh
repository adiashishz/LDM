#!/bin/sh
# Smoke test of an installed LDM package (.deb autopkgtest; CI runs it after installing the .deb and
# the Arch package): the commands start, the window opens, a current yt-dlp is in use
# (the bundled copy where the package has one), and a download through a D-Bus-activated service
# (the installed activation file) finishes.
set -eu
WORK=$(mktemp -d)
trap 'kill "${SERVER:-}" "${DAEMON:-}" 2>/dev/null || true; rm -rf "$WORK"' EXIT
export HOME="$WORK/home" XDG_DOWNLOAD_DIR="$WORK/Downloads"
mkdir -p "$HOME" "$XDG_DOWNLOAD_DIR" "$WORK/www"

ldm --help >/dev/null
QT_QPA_PLATFORM=offscreen python3 - <<'PY'
# The window opens (offscreen, with no service), with its font and its SVG icons.
from PySide6 import __version__ as pyside
from PySide6.QtGui import QImageReader
from PySide6.QtWidgets import QApplication
app = QApplication([])
from ldm.qt.client import DaemonClient
from ldm.qt.theme import ui_font
from ldm.qt.window import MainWindow
font = ui_font()
assert font.family() == "Inter", font.family()
assert b"svg" in QImageReader.supportedImageFormats(), "Qt's SVG image plugin is missing"
window = MainWindow(DaemonClient("unix:path=/nonexistent"))
window.show()
app.processEvents()
assert not window.grab().isNull()
window.close()
print(f"window: opens (PySide6 {pyside}, Inter, SVG icons)")
PY

python3 - <<'PY'
from ldm import media, ytdlp
copy = ytdlp.bundled_copy()
bundled = ytdlp.zip_version(copy) if copy else None
used = media.YTDLP_VERSION
assert ytdlp.version_key(used) >= ytdlp.version_key(max(bundled or "", ytdlp.installed_version() or "",
                                                        key=ytdlp.version_key)), used
assert ytdlp.version_key(used) >= (2026, 1), f"yt-dlp {used} is older than LDM needs"
print(f"yt-dlp: {used} in use (bundled {bundled}, system {ytdlp.installed_version()})")
PY

head -c 3000000 /dev/urandom > "$WORK/www/file.bin"
python3 -m http.server --bind 127.0.0.1 --directory "$WORK/www" 8765 >/dev/null 2>&1 &
SERVER=$!
sleep 1
if command -v dbus-run-session >/dev/null 2>&1; then
    # A private bus: the service starts by D-Bus activation (the installed activation file).
    dbus-run-session -- sh -c 'ldm add http://127.0.0.1:8765/file.bin --wait >/dev/null && ldm list'
else
    # Inside the Flatpak (no bus daemon in the runtime): the caller's bus, and the service started here.
    : "${DBUS_SESSION_BUS_ADDRESS:?no dbus-run-session and no session bus}"
    ldm daemon >"$WORK/daemon.log" 2>&1 &
    DAEMON=$!
    # Wait until it has its bus name, asking the bus only (no D-Bus activation of another copy).
    python3 - "$WORK/daemon.log" <<'PY'
import asyncio, sys
from dbus_fast import Message
from dbus_fast.aio import MessageBus

async def wait():
    bus = await MessageBus().connect()
    for _ in range(100):
        reply = await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                       interface="org.freedesktop.DBus", member="NameHasOwner",
                                       signature="s", body=["io.github.adiashishz.LDM.Service"]))
        if reply.body[0]:
            return
        await asyncio.sleep(0.1)
    sys.exit("the service did not start:\n" + open(sys.argv[1]).read())

asyncio.run(wait())
PY
    ldm add http://127.0.0.1:8765/file.bin --wait >/dev/null && ldm list
    kill "$DAEMON"
fi
python3 -c 'import sys; a, b = (open(p, "rb").read() for p in sys.argv[1:]); sys.exit(a != b)' \
    "$WORK/www/file.bin" "$XDG_DOWNLOAD_DIR/file.bin"
echo "download: OK"
