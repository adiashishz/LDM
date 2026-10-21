# ldm — Linux download manager (v1, Python)

Working name. Design: [../notes/10-linux-design.md](../notes/10-linux-design.md).

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
```

State: `$XDG_DATA_HOME/ldm/ldm.db` (daemon) and `standalone.db` (`ldm get`); `--db` overrides.
Downloads go to `~/Downloads` unless `-o` / `settings download_dir` say otherwise. While a download
runs, its data is in `<name>.<id>.part` next to the target.

## Run as a service

`data/ldm.service` (systemd user unit) and `data/io.github.ldm.service` (D-Bus activation): copy
to `~/.config/systemd/user/` and `~/.local/share/dbus-1/services/`, set `ExecStart`/`Exec` to your
`ldm` path, then `systemctl --user daemon-reload && systemctl --user enable --now ldm`.

## Tests

```sh
.venv/bin/python -m pytest -q    # local HTTP test server and a private D-Bus bus; no network
```

`tools/render_window.py` renders the GUI to PNGs for visual checks without opening a window on your
desktop (run it under a headless compositor, e.g. `mutter --headless --wayland --no-x11
--virtual-monitor 900x700 --wayland-display ldm-render-0` with `WAYLAND_DISPLAY=ldm-render-0`).
