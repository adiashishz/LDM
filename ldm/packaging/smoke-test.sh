#!/bin/sh
# Smoke test of an installed LDM package (.deb autopkgtest; CI runs it after installing the .deb and
# the Arch package): the commands start, the GUI's toolkit is new enough, a current yt-dlp is in use
# (the bundled copy where the package has one), and a download through a D-Bus-activated service
# (the installed activation file) finishes.
set -eu
WORK=$(mktemp -d)
trap 'kill "$SERVER" 2>/dev/null || true; rm -rf "$WORK"' EXIT
export HOME="$WORK/home" XDG_DOWNLOAD_DIR="$WORK/Downloads"
mkdir -p "$HOME" "$XDG_DOWNLOAD_DIR" "$WORK/www"

ldm --help >/dev/null
python3 -c 'from ldm import gui; m = gui.too_old(); assert m is None, m; import ldm.gui.app'
echo "GUI: GTK and libadwaita are new enough"

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
dbus-run-session -- sh -c 'ldm add http://127.0.0.1:8765/file.bin --wait >/dev/null && ldm list'
python3 -c 'import sys; a, b = (open(p, "rb").read() for p in sys.argv[1:]); sys.exit(a != b)' \
    "$WORK/www/file.bin" "$XDG_DOWNLOAD_DIR/file.bin"
echo "download: OK"
