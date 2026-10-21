# LDM

A download manager for Linux: fast multi-part downloads that survive restarts, videos and streams,
torrents, a scheduler, and a browser extension that hands your downloads to it. Built with
GTK 4 / libadwaita and Python, inspired by 1DM for Android.

Downloads run in a small background service, so closing the window never stops them.

## Features

- **Files:** up to 32 connections per download, pause/resume across restarts, automatic retries,
  replace an expired link without losing progress, per-site connection limits, speed limits,
  checksums, rename/move, batch downloads (`https://site/img*.jpg`), import/export of link lists.
- **Videos and streams:** YouTube and the sites [yt-dlp](https://github.com/yt-dlp/yt-dlp) supports,
  HLS/DASH playlists (including encrypted HLS), quality choice with size estimates, subtitles.
- **Torrents:** magnet links and `.torrent` files via libtorrent, file selection, sequential
  download, optional seeding, extra/public tracker lists.
- **Scheduler and conditions:** a default time window plus per-download times, "only on unmetered
  connections", battery limit, data-usage limit, keep the computer awake, and suspend or power off
  when everything is done.
- **Browser extension** for Firefox, Chrome and Brave: takes over browser downloads with their
  cookies, finds the videos and files on a page (including a same-site crawler), and opens LDM's
  confirmation dialog.
- **Desktop integration:** notifications, start at login, tray icon (where the desktop has a tray),
  copied-link prompt, proxy (system, HTTP, SOCKS5) and DNS-over-HTTPS, site logins stored in the
  keyring, encrypted backups.

## Install from source (Fedora)

LDM uses the distribution's GTK, so the virtual environment is built on the system Python:

```sh
sudo dnf install python3-gobject gtk4 libadwaita ffmpeg-free python3-dbus-fast python3-dns \
                 python3-socksio python3-secretstorage python3-cryptography
sudo dnf install rb_libtorrent-python3   # for torrents (optional)

git clone https://github.com/adiashishz/LDM.git && cd LDM/ldm
uv venv --python /usr/bin/python3 --system-site-packages .venv
uv pip install -e '.[dev]'

.venv/bin/ldm install-browser-integration   # service, desktop entry, browser bridge, start at login
.venv/bin/ldm-gui
```

YouTube also needs a JavaScript runtime for yt-dlp (Node.js, Deno or Bun).

### Browser extension

```sh
cd extension && python3 build.py            # dist/firefox and dist/chromium
```

- **Firefox:** `about:debugging` → This Firefox → Load Temporary Add-on → `dist/firefox/manifest.json`
- **Chrome / Brave:** the extensions page → Developer mode → Load unpacked → `dist/chromium`
- Brave as a Flatpak also needs D-Bus access (the installer prints the `flatpak override` command).

## Use

Open **LDM** from the app menu, or use the command line, which talks to the same service:

```sh
ldm add https://example.com/file.iso --wait
ldm add 'https://www.youtube.com/watch?v=…' --media --quality 1080
ldm add 'magnet:?xt=urn:btih:…' --sequential
ldm list
ldm --help
```

More commands are in [`ldm/README.md`](ldm/README.md).

## Packaging

- `ldm/packaging/ldm.spec` and `build-rpm.sh`: RPM for Fedora (COPR steps in `COPR.md`).
- `ldm/packaging/io.github.adiashishz.LDM.yml`: Flatpak manifest (not built yet).
- `extension/sign-firefox.sh`: signs the Firefox add-on for permanent installs.

## Development

```sh
cd ldm && .venv/bin/python -m pytest -q     # local servers and private D-Bus buses only, no internet
cd extension && node --test tests/*.test.mjs
```

## License

[MIT](LICENSE)
