"""Command-line interface.

Most commands talk to the daemon (`ldm daemon`) over D-Bus. `ldm get` downloads in the
foreground without a daemon (Ctrl-C pauses; `ldm get --resume ID` continues).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .engine import DownloadError, Downloader, LinkExpired, RemoteChanged
from .format import human, parse_rate, progress_line
from .store import Store, default_db_path, default_download_dir

FINAL_STATES = {"done", "error", "expired", "changed", "paused"}


class DaemonNotRunning(Exception):
    pass


def print_progress(done: int, size: int | None, speed: float) -> None:
    print(f"\r{progress_line(done, size, speed):<70}", end="", flush=True)


def parse_headers(values: list[str]) -> dict[str, str]:
    headers = {}
    for value in values:
        name, sep, content = value.partition(":")
        if not sep:
            raise SystemExit(f"invalid header (expected 'Name: value'): {value}")
        headers[name.strip()] = content.strip()
    return headers


# --- standalone mode -----------------------------------------------------------------------

def run_download(store: Store, download_id: int) -> int:
    downloader = Downloader(store, download_id, on_progress=print_progress)
    try:
        path = asyncio.run(downloader.run())
    except KeyboardInterrupt:
        print(f"\npaused — continue with: ldm get --resume {download_id}")
        return 130
    except LinkExpired as e:
        print(f"\nlink expired or refused ({e}) — a fresh link is needed")
        return 1
    except RemoteChanged as e:
        print(f"\nremote file changed ({e}) — the download must restart")
        return 1
    except DownloadError as e:
        print(f"\nfailed: {e} — retry with: ldm get --resume {download_id}")
        return 1
    print(f"\nsaved {path}")
    return 0


def cmd_get(args) -> int:
    store = Store(args.db or default_db_path("standalone.db"))
    if args.resume is not None:
        try:
            d = store.get(args.resume)
        except KeyError:
            print(f"no download with id {args.resume}")
            return 1
        if d.state == "done":
            print(f"download {args.resume} is already finished: {d.dest}")
            return 0
        return run_download(store, args.resume)
    if not args.url:
        raise SystemExit("ldm get: a URL or --resume ID is required")
    download_id = store.add(args.url, args.dir or default_download_dir(), name=args.name,
                            headers=parse_headers(args.header), connections=args.connections)
    print(f"download {download_id}: {args.url}")
    return run_download(store, download_id)


def cmd_daemon(args) -> int:
    import logging

    from .daemon import AlreadyRunning, serve
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request otherwise
    try:
        asyncio.run(serve(db_path=args.db))
    except AlreadyRunning:
        print("the ldm daemon is already running")
        return 1
    return 0


# --- daemon client -------------------------------------------------------------------------

async def connect():
    from dbus_fast import DBusError
    from dbus_fast.aio import MessageBus

    from .dbus_service import BUS_NAME, INTERFACE, OBJECT_PATH

    bus = await MessageBus().connect()
    try:
        introspection = await bus.introspect(BUS_NAME, OBJECT_PATH)
    except DBusError as e:
        bus.disconnect()
        if e.type in ("org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NameHasNoOwner"):
            raise DaemonNotRunning from e
        raise
    return bus, bus.get_proxy_object(BUS_NAME, OBJECT_PATH, introspection).get_interface(INTERFACE)


def values(vardict) -> dict:
    return {k: v.value for k, v in vardict.items()}


async def wait_for(iface, download_id: int, show_progress: bool) -> str:
    finished = asyncio.get_running_loop().create_future()

    def on_state(i, state, error):
        if i == download_id and state in FINAL_STATES and not finished.done():
            finished.set_result((state, error))

    def on_progress(batch):
        for i, done, size, speed in batch:
            if i == download_id and show_progress:
                print_progress(done, size if size >= 0 else None, speed)

    iface.on_state_changed(on_state)
    iface.on_progress(on_progress)
    current = values(await iface.call_get(download_id))
    if current["state"] in FINAL_STATES:
        return current["state"]
    state, error = await finished
    if show_progress:
        print()
    if error:
        print(f"{state}: {error}")
    return state


async def client(args) -> int:
    from dbus_fast import Variant

    bus, iface = await connect()
    try:
        if args.command == "add":
            options = {"connections": Variant("x", args.connections) if args.connections else None,
                       "dir": Variant("s", str(args.dir.expanduser().resolve())) if args.dir else None,
                       "name": Variant("s", args.name) if args.name else None,
                       "headers": Variant("a{ss}", parse_headers(args.header)) if args.header else None,
                       "speed_limit": Variant("x", args.limit) if args.limit else None,
                       "start": Variant("b", not args.paused),
                       "kind": Variant("s", "media") if args.media or args.quality else None,
                       "format": Variant("s", media_selector(args.quality)) if args.quality else None,
                       "format_label": Variant("s", args.quality) if args.quality else None,
                       "sequential": Variant("b", True) if args.sequential else None,
                       "scheduled": Variant("b", True) if args.scheduled or schedule_window(args) else None,
                       **{k: Variant("s", v) for k, v in schedule_window(args).items()},
                       "unmetered_only": Variant("b", True) if args.unmetered_only else None,
                       "priorities": Variant("ax", file_priorities(args.files)) if args.files else None}
            download_id = await iface.call_add(args.url, {k: v for k, v in options.items() if v is not None})
            print(f"download {download_id} added")
            if args.wait:
                state = await wait_for(iface, download_id, show_progress=True)
                d = values(await iface.call_get(download_id))
                print(f"{state}: {Path(d['dir']) / d.get('name', '')}")
                return 0 if state == "done" else 1
        elif args.command == "list":
            for d in map(values, await iface.call_list()):
                size = human(d["size"]) if "size" in d else "?"
                done = f"{d['done'] * 100 / d['size']:5.1f}%" if d.get("size") else "    -"
                line = f"{d['id']:>4}  {d['state']:<8} {done} {size:>10}  {d.get('name') or d['url']}"
                note = d.get("error") or d.get("waiting")
                print(line + (f"  ({note})" if note else ""))
        elif args.command == "pause":
            await iface.call_pause(args.id)
        elif args.command == "resume":
            await iface.call_resume(args.id)
        elif args.command == "rm":
            await iface.call_remove(args.id, args.delete)
        elif args.command == "move":
            await iface.call_move(args.id, args.position)
        elif args.command == "set-url":
            await iface.call_set_url(args.id, args.url)
        elif args.command == "limit":
            await iface.call_set_speed_limit(args.id, args.rate)
        elif args.command == "schedule":
            await iface.call_set_schedule(args.id, args.state == "on", schedule_window(args))
        elif args.command == "settings":
            if args.key is None:
                for key, value in sorted(values(await iface.call_get_settings()).items()):
                    print(f"{key} = {value}")
            else:
                value = parse_setting(args.key, args.value)
                variant = (Variant("s", value) if isinstance(value, str) else
                           Variant("b", value) if isinstance(value, bool) else Variant("x", value))
                await iface.call_set_settings({args.key: variant})
        elif args.command == "host-rule":
            if args.remove:
                await iface.call_remove_host_rule(args.host)
            else:
                await iface.call_set_host_rule(args.host, args.max_connections, args.max_downloads, not args.exact)
        elif args.command == "host-rules":
            for r in map(values, await iface.call_list_host_rules()):
                scope = "" if r["include_subdomains"] else " (exact)"
                print(f"{r['host']}{scope}: max_connections={r['max_connections']} max_downloads={r['max_downloads']}")
        elif args.command == "login":
            if args.remove:
                await iface.call_remove_login(args.host)
            else:
                if not args.user:
                    raise SystemExit("ldm login HOST USER (or --remove)")
                password = read_password(args.password_stdin, f"password for {args.user}@{args.host}: ")
                await iface.call_set_login(args.host, args.user, password, not args.exact)
        elif args.command == "logins":
            for r in map(values, await iface.call_list_logins()):
                print(f"{r['host']}{'' if r['include_subdomains'] else ' (exact)'}: {r['username']}")
        elif args.command == "proxy-password":
            password = read_password(args.password_stdin, "proxy password (empty to remove): ")
            await iface.call_set_settings({"proxy_password": Variant("s", password)})
        elif args.command == "log":
            for line in await iface.call_get_log(args.id):
                print(line)
        elif args.command in ("batch", "import"):
            from .batch import expand, parse_link_list
            if args.command == "batch":
                if not args.letters and args.last is None:
                    raise SystemExit("ldm batch: give the last number with --to (or use --letters)")
                try:
                    links = expand(args.url, letters=tuple(args.letters) if args.letters else None,
                                   numbers=None if args.letters else (args.first, args.last, args.pad))
                except ValueError as e:
                    raise SystemExit(f"ldm batch: {e}")
            else:
                links = parse_link_list(Path(args.file).expanduser().read_text())
            options = {"start": Variant("b", not args.paused)}
            if args.dir:
                options["dir"] = Variant("s", str(args.dir.expanduser().resolve()))
            for link in links:
                kind = {"kind": Variant("s", "torrent")} if link.startswith("magnet:") else {}
                await iface.call_add(link, {**options, **kind})
            print(f"{len(links)} downloads added")
        elif args.command == "checksum":
            print(await iface.call_checksum(args.id, args.algo))
        elif args.command == "rename":
            print(await iface.call_rename(args.id, args.name))
        elif args.command == "mv":
            print(await iface.call_move_to(args.id, str(args.folder.expanduser().resolve())))
        elif args.command == "export-links":
            rows = [values(r) for r in await iface.call_list()]
            links = [r["url"] for r in rows if not args.state or r["state"] in args.state.split(",")]
            Path(args.file).expanduser().write_text("".join(f"{link}\n" for link in links))
            print(f"{len(links)} links written to {args.file}")
        elif args.command == "export-unfinished":
            Path(args.file).expanduser().write_text(await iface.call_export_downloads(not args.all))
            print(f"saved to {args.file}")
        elif args.command == "import-unfinished":
            ids = await iface.call_import_downloads(Path(args.file).expanduser().read_text())
            print(f"{len(ids)} downloads imported (paused — resume them with `ldm resume ID`)")
        elif args.command == "torrent-files":
            source = args.source if "://" in args.source or args.source.startswith("magnet:") \
                else Path(args.source).resolve().as_uri()
            info = values(await iface.call_probe_torrent(source, {}))
            print(f"{info.get('name', '')}  ({human(info['size']) if 'size' in info else 'size unknown'})")
            for f in info.get("files", []):
                f = values(f)
                print(f"{f['index']:>5}  {human(f['size']):>10}  {f['path']}")
        elif args.command == "watch":
            await watch(iface)
    finally:
        bus.disconnect()
    return 0


def file_priorities(text: str) -> list[int]:
    """'0,2' -> priorities for files 0..max: chosen ones 1, others 0 (the daemon pads the rest)."""
    chosen = {int(i) for i in text.split(",") if i.strip()}
    return [1 if i in chosen else 0 for i in range(max(chosen) + 1)]


def media_selector(quality: str) -> str:
    from .media import default_selector
    return default_selector(quality)


def read_password(from_stdin: bool, prompt: str) -> str:
    """Never from the command line (shell history, `ps`): a prompt, or stdin for scripts."""
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    import getpass
    return getpass.getpass(prompt)


def schedule_window(args) -> dict[str, str]:
    """--start/--stop/--days → a download's own schedule times (missing ones come from the defaults)."""
    given = {"schedule_start": args.start, "schedule_stop": args.stop, "schedule_days": args.days}
    return {k: v for k, v in given.items() if v is not None}


def schedule_options(p) -> None:
    p.add_argument("--start", metavar="HH:MM", help="own schedule: start time (default: settings)")
    p.add_argument("--stop", metavar="HH:MM", help="own schedule: stop time; earlier than start = overnight")
    p.add_argument("--days", metavar="DAYS", help='own schedule: e.g. "sat,sun"; "" = every day')


def parse_setting(key: str, text: str | None):
    if text is None:
        raise SystemExit(f"ldm settings {key}: missing value")
    if key in ("speed_limit", "schedule_speed_limit"):
        return parse_rate(text)
    if key in ("schedule_start", "schedule_stop", "schedule_days", "clipboard_exclude"):
        return text  # "02:00" is not JSON
    if key == "download_dir":
        return str(Path(text).expanduser().resolve())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


async def watch(iface) -> None:
    def on_progress(batch):
        line = " | ".join(f"#{i} {progress_line(done, size if size >= 0 else None, speed)}"
                          for i, done, size, speed in batch)
        print(f"\r{line[:200]:<100}", end="", flush=True)

    def on_state(i, state, error):
        print(f"\n#{i} {state}" + (f": {error}" if error else ""))

    iface.on_progress(on_progress)
    iface.on_state_changed(on_state)
    await asyncio.Event().wait()


# --- argument parsing ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ldm", description="Multi-part download manager")
    parser.add_argument("--db", type=Path, help="state database (daemon and get)")
    sub = parser.add_subparsers(dest="command", required=True)

    def download_options(p):
        p.add_argument("-o", "--dir", type=Path, help="target directory")
        p.add_argument("-n", "--name", help="file name (default: from server)")
        p.add_argument("-H", "--header", action="append", default=[], help="extra request header 'Name: value'")

    p = sub.add_parser("add", help="queue a download in the daemon")
    p.add_argument("url")
    download_options(p)
    p.add_argument("-c", "--connections", type=int, choices=range(1, 33), metavar="1-32")
    p.add_argument("--limit", type=parse_rate, help="speed limit, e.g. 2M")
    p.add_argument("--paused", action="store_true", help="add without starting")
    p.add_argument("--media", action="store_true",
                   help="download a video/stream page with yt-dlp (YouTube, HLS/DASH playlists, …)")
    p.add_argument("--quality", help="media quality: best, audio, or a maximum height such as 720")
    p.add_argument("--files", help="torrent: only these file indexes, e.g. 0,2 (see `ldm torrent-files`)")
    p.add_argument("--sequential", action="store_true", help="torrent: download pieces in order")
    p.add_argument("--scheduled", action="store_true", help="only download inside the schedule window")
    schedule_options(p)
    p.add_argument("--unmetered-only", action="store_true", help="wait while the connection is metered")
    p.add_argument("--wait", action="store_true", help="show progress until it finishes")

    sub.add_parser("list", help="list downloads")
    for name, text in (("pause", "pause a download"), ("resume", "resume or retry a download")):
        sub.add_parser(name, help=text).add_argument("id", type=int)
    p = sub.add_parser("rm", help="remove a download")
    p.add_argument("id", type=int)
    p.add_argument("--delete", action="store_true", help="also delete its file")
    p = sub.add_parser("move", help="change queue position")
    p.add_argument("id", type=int)
    p.add_argument("position", type=int)
    p = sub.add_parser("set-url", help="give a download a fresh link")
    p.add_argument("id", type=int)
    p.add_argument("url")
    p = sub.add_parser("limit", help="per-download speed limit (0 = unlimited)")
    p.add_argument("id", type=int)
    p.add_argument("rate", type=parse_rate)
    p = sub.add_parser("schedule", help="put a download on the schedule (on) or take it off (off); "
                                        "without --start/--stop/--days it follows the default times")
    p.add_argument("id", type=int)
    p.add_argument("state", choices=("on", "off"))
    schedule_options(p)
    p = sub.add_parser("settings", help="show settings, or set one: ldm settings KEY VALUE")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p = sub.add_parser("host-rule", help="limit connections/downloads for a host and its subdomains")
    p.add_argument("host")
    p.add_argument("--max-connections", type=int, default=0)
    p.add_argument("--max-downloads", type=int, default=0)
    p.add_argument("--exact", action="store_true", help="do not apply to subdomains")
    p.add_argument("--remove", action="store_true")
    sub.add_parser("host-rules", help="list host rules")
    p = sub.add_parser("login", help="save a site login (HTTP Basic/Digest); asks for the password")
    p.add_argument("host")
    p.add_argument("user", nargs="?")
    p.add_argument("--exact", action="store_true", help="only this host, not its subdomains")
    p.add_argument("--remove", action="store_true", help="forget the login for HOST")
    p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    sub.add_parser("logins", help="list saved site logins (without passwords)")
    p = sub.add_parser("proxy-password", help="set the manual proxy's password (kept in the keyring)")
    p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    p = sub.add_parser("log", help="diagnostics of a download: requests, responses, retries")
    p.add_argument("id", type=int)
    p = sub.add_parser("batch", help="download a numbered series: one * in the link, e.g. https://site/img*.jpg")
    p.add_argument("url")
    p.add_argument("--from", dest="first", type=int, default=1, help="first number (default 1)")
    p.add_argument("--to", dest="last", type=int, help="last number")
    p.add_argument("--pad", type=int, default=0, help="zero-pad numbers to this width, e.g. 3 → 001")
    p.add_argument("--letters", nargs=2, metavar=("FIRST", "LAST"), help="letters instead, e.g. a z")
    p.add_argument("--paused", action="store_true", help="add without starting")
    p.add_argument("-o", "--dir", type=Path, help="folder")
    p = sub.add_parser("import", help="add the links in a text file (one per line, # for comments)")
    p.add_argument("file")
    p.add_argument("--paused", action="store_true", help="add without starting")
    p.add_argument("-o", "--dir", type=Path, help="folder")
    p = sub.add_parser("checksum", help="checksum of a finished file")
    p.add_argument("id", type=int)
    p.add_argument("--algo", choices=("md5", "sha1", "sha256"), default="sha256")
    p = sub.add_parser("rename", help="rename a download (the file, or the name it will get)")
    p.add_argument("id", type=int)
    p.add_argument("name")
    p = sub.add_parser("mv", help="move a download to another folder")
    p.add_argument("id", type=int)
    p.add_argument("folder", type=Path)
    p = sub.add_parser("export-links", help="write the links of your downloads to a text file")
    p.add_argument("file")
    p.add_argument("--state", help="only these states, e.g. done or queued,paused,error")
    p = sub.add_parser("export-unfinished", help="save unfinished downloads to a file (to move them elsewhere)")
    p.add_argument("file")
    p.add_argument("--all", action="store_true", help="finished downloads too")
    p = sub.add_parser("import-unfinished", help="add downloads from an export-unfinished file")
    p.add_argument("file")
    p = sub.add_parser("torrent-files", help="list the files in a magnet link or .torrent (index, size, path)")
    p.add_argument("source")
    sub.add_parser("watch", help="show live progress")
    sub.add_parser("daemon", help="run the download service")
    sub.add_parser("clipboard-watch", help="offer copied links in a notification (where the desktop allows it)")
    p = sub.add_parser("install-browser-integration",
                       help="install the browser bridge, D-Bus activation and desktop file")
    p.add_argument("--browser", action="append", choices=["firefox", "chrome", "brave"],
                   help="limit to these browsers (default: the ones found)")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="only show what would be written")
    p.add_argument("--flatpak-override", action="store_true",
                   help="also grant Flatpak browsers D-Bus access to ldm (otherwise printed)")

    p = sub.add_parser("get", help="download in the foreground without the daemon")
    p.add_argument("url", nargs="?")
    download_options(p)
    p.add_argument("-c", "--connections", type=int, default=8, choices=range(1, 33), metavar="1-32")
    p.add_argument("--resume", type=int, metavar="ID", help="continue a paused standalone download")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "get":
        return cmd_get(args)
    if args.command == "daemon":
        return cmd_daemon(args)
    if args.command == "clipboard-watch":
        import logging

        from .clipboard import run_watcher
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        try:
            return asyncio.run(run_watcher())
        except KeyboardInterrupt:
            return 130
    if args.command == "install-browser-integration":
        from . import integration
        if args.uninstall:
            integration.uninstall(Path.home(), args.browser)
        else:
            integration.install(Path.home(), args.browser, dry_run=args.dry_run,
                                flatpak_override=args.flatpak_override)
        return 0
    from dbus_fast import DBusError
    try:
        return asyncio.run(client(args))
    except DaemonNotRunning:
        print("the ldm daemon is not running — start it with: ldm daemon")
    except DBusError as e:
        print(f"error: {e.text}")
    except KeyboardInterrupt:
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())
