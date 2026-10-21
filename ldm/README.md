# LDM — Linux download manager (v1, Python)

Command `ldm`, app ID `io.github.ldm.Gui`. Design: [../notes/10-linux-design.md](../notes/10-linux-design.md).

- **M1:** engine — multi-part HTTP(S) into one pre-allocated file, dynamic segmentation, fsync-safe
  resume state in SQLite, retries, changed-file and expired-link detection.
- **M2 (this):** background daemon on D-Bus (`io.github.ldm`, `/io/github/ldm/Manager1`) with a
  queue, parallel-download limit, per-host rules, global/per-download speed limits, auto-retry,
  link refresh, and restart recovery. The CLI is a client of the daemon.

- **M3:** desktop app `ldm-gui` (GTK4/libadwaita): download list with All / Downloading / Finished /
  Problems views, add dialog (clipboard prefill, drag-and-drop of links, folder, connections, speed
  limit, request headers), pause/resume/retry, set new link, per-download speed limit, open file or
  folder, remove (optionally deleting the file), preferences and host rules, notifications. It is a
  pure D-Bus client: closing it never stops downloads. If the service is not running, a banner
  offers to start it (it is never started automatically).

- **M4:** browser integration. Extension in `../extension` (Firefox, Chrome, Brave): catches browser
  downloads (size / extension / site rules) with cookies and referer, "Download with ldm" context
  menus, media sniffer (video, audio, HLS/DASH playlists, subtitles) with a badge, and a Links tab to
  pick files from the page. It talks to `ldm_nmhost.py` (standard library + `gdbus`, so it also runs
  inside Flatpak browsers), which starts the daemon/GUI on demand via D-Bus activation and either
  opens the add dialog prefilled or queues directly.

- **M5:** video streams and sites through yt-dlp (installed with ldm): HLS/DASH playlists (incl.
  AES-128), YouTube and other sites, quality choice with size estimates, separate audio+video merged
  by FFmpeg into MP4/MKV, subtitles. `ldm add URL --media --quality 720`; in the extension
  "Download video from this page". YouTube needs a JavaScript runtime (deno, node or bun; found in
  `~/.nvm`, `~/.bun`, `~/.deno` too). Keep yt-dlp current: `uv pip install -U yt-dlp`.

- **M6:** torrents with libtorrent (`sudo dnf install rb_libtorrent-python3`): magnet links and
  `.torrent` files/URLs, file selection, download in order, Torrent Details (files, peers,
  trackers), optional seeding with ratio/time limits, DHT/PEX/LSD/UPnP/µTP/encryption/IP filter
  settings. ldm becomes the default app for magnet links and .torrent files.
  `ldm torrent-files SOURCE`, `ldm add MAGNET --files 0,2 --sequential`.

## Browser integration

```sh
.venv/bin/ldm install-browser-integration          # host manifests, D-Bus activation, desktop file
cd ../extension && python3 build.py                 # dist/firefox, dist/chromium
```

- Firefox: `about:debugging` → This Firefox → Load Temporary Add-on → `dist/firefox/manifest.json`
  (release Firefox needs a signed add-on for a permanent install; that is a packaging step).
- Chrome / Brave: `chrome://extensions` / `brave://extensions` → Developer mode → Load unpacked →
  `dist/chromium` (fixed id `nfmeebgoiidgaakcafhniggapnipjhgp`).
- Brave from Flatpak also needs D-Bus access to ldm (the installer prints this):
  `flatpak override --user --talk-name=io.github.ldm --talk-name=io.github.ldm.Gui com.brave.Browser`
- Re-run `ldm install-browser-integration` after updating ldm (it copies the host script).

## Use

The GUI needs the distro's PyGObject, GTK 4 and libadwaita (Fedora: `python3-gobject gtk4
libadwaita`), so the venv is built on the system Python with its site packages:

```sh
uv venv --python /usr/bin/python3 --system-site-packages .venv && uv pip install -e '.[dev]'
.venv/bin/ldm-gui                            # desktop app
.venv/bin/ldm daemon &                       # or install the service files below
.venv/bin/ldm add https://example.com/file.iso --wait
.venv/bin/ldm list
.venv/bin/ldm pause 1 && .venv/bin/ldm resume 1
.venv/bin/ldm set-url 1 https://mirror.example.org/file.iso   # fresh link, keeps data
.venv/bin/ldm settings max_parallel 2
.venv/bin/ldm settings speed_limit 2M
.venv/bin/ldm host-rule example.com --max-connections 4 --max-downloads 1
.venv/bin/ldm watch                          # live progress
.venv/bin/ldm get https://example.com/x.zip  # foreground, no daemon (Ctrl-C pauses)

# M7: scheduler (default times + per-download times) and run conditions
.venv/bin/ldm settings schedule_start 23:00 && .venv/bin/ldm settings schedule_stop 06:00  # default; overnight ok
.venv/bin/ldm settings schedule_days sat,sun   # default days; empty = every day
.venv/bin/ldm add https://example.com/big.iso --scheduled --unmetered-only   # follows the default times
.venv/bin/ldm add https://example.com/other.iso --start 01:00 --stop 05:00   # its own times
.venv/bin/ldm schedule 1 on --days fri        # give download 1 its own days
.venv/bin/ldm schedule 1 off                  # take it off the schedule (starts now)
.venv/bin/ldm settings battery_limit 20       # hold downloads on battery below 20 %
.venv/bin/ldm clipboard-watch                 # offer copied links (KDE/wlroots Wayland, X11)

# M8: network, adding links, files
.venv/bin/ldm settings proxy_mode manual && .venv/bin/ldm settings proxy_url socks5://127.0.0.1:1080
.venv/bin/ldm proxy-password                  # asks; kept in the keyring
.venv/bin/ldm settings dns_mode doh           # DNS-over-HTTPS (doh_url: cloudflare / google / quad9 / URL)
.venv/bin/ldm settings user_agent firefox     # or default / chrome / mobile / any string
.venv/bin/ldm login example.com alice         # site login (HTTP Basic/Digest); asks for the password
.venv/bin/ldm log 3                           # diagnostics: requests, responses, retries
.venv/bin/ldm batch 'https://site/img*.jpg' --from 1 --to 50 --pad 3
.venv/bin/ldm import links.txt && .venv/bin/ldm export-links all.txt
.venv/bin/ldm export-unfinished moving.json   # and import-unfinished on the other computer
.venv/bin/ldm checksum 3 --algo md5
.venv/bin/ldm rename 3 new-name.iso && .venv/bin/ldm mv 3 ~/ISOs
.venv/bin/ldm settings move_finished_dir ~/Done && .venv/bin/ldm settings cleanup_days 30
```

DNS-over-HTTPS needs `python3-dns`, SOCKS proxies need `python3-socksio` (both packaged in Fedora).

State: `$XDG_DATA_HOME/ldm/ldm.db` (daemon) and `standalone.db` (`ldm get`); `--db` overrides.
Downloads go to `~/Downloads` unless `-o` / `settings download_dir` say otherwise. While a download
runs, its data is in `<name>.<id>.part` next to the target.

## Run as a service

`ldm install-browser-integration` writes the systemd user unit (`~/.config/systemd/user/ldm.service`),
D-Bus activation through it, the desktop file and icon, and enables the unit when the `autostart`
setting is on (Preferences → System → "Start the download service at login"). Packages install the
same files system-wide: see `packaging/` (RPM spec, `build-rpm.sh`, `COPR.md`, Flatpak manifest).

## Tests

```sh
.venv/bin/python -m pytest -q    # local HTTP test server and a private D-Bus bus; no network
```

`tools/render_window.py` renders the GUI to PNGs for visual checks without opening a window on your
desktop (run it under a headless compositor, e.g. `mutter --headless --wayland --no-x11
--virtual-monitor 900x700 --wayland-display ldm-render-0` with `WAYLAND_DISPLAY=ldm-render-0`).
