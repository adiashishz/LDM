# ldm — Linux download manager (v1, Python)

Working name. Design: [../notes/10-linux-design.md](../notes/10-linux-design.md).

- **M1:** engine — multi-part HTTP(S) into one pre-allocated file, dynamic segmentation, fsync-safe
  resume state in SQLite, retries, changed-file and expired-link detection.
- **M2 (this):** background daemon on D-Bus (`io.github.ldm`, `/io/github/ldm/Manager1`) with a
  queue, parallel-download limit, per-host rules, global/per-download speed limits, auto-retry,
  link refresh, and restart recovery. The CLI is a client of the daemon.

## Use

```sh
uv venv .venv && uv pip install -e '.[dev]'
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
