"""Command-line interface.

Most commands talk to the daemon (`ldm daemon`) over D-Bus. `ldm get` downloads in the
foreground without a daemon (Ctrl-C pauses; `ldm get --resume ID` continues).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

from .engine import DownloadError, Downloader, LinkExpired, RemoteChanged
from .store import Store, default_db_path, default_download_dir

FINAL_STATES = {"done", "error", "expired", "changed", "paused"}


class DaemonNotRunning(Exception):
    pass


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def parse_rate(text: str) -> int:
    """'500K', '2M', '1G' (binary units) or plain bytes per second; 0 = unlimited."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMG]?)(?:i?B)?(?:/s)?", text.strip(), re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid rate: {text}")
    factor = 1024 ** " KMG".index(match.group(2).upper() or " ")
    return int(float(match.group(1)) * factor)


def progress_line(done: int, size: int | None, speed: float) -> str:
    if size and size > 0:
        eta = f"{(size - done) / speed:.0f}s" if speed > 0 else "--"
        return f"{done * 100 / size:5.1f}%  {human(done)} / {human(size)}  {human(speed)}/s  ETA {eta}"
    return f"{human(done)}  {human(speed)}/s"


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
                       "start": Variant("b", not args.paused)}
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
                print(line + (f"  ({d['error']})" if d.get("error") else ""))
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
        elif args.command == "settings":
            if args.key is None:
                for key, value in sorted(values(await iface.call_get_settings()).items()):
                    print(f"{key} = {value}")
            else:
                value = parse_setting(args.key, args.value)
                variant = Variant("s", value) if isinstance(value, str) else Variant("x", value)
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
        elif args.command == "watch":
            await watch(iface)
    finally:
        bus.disconnect()
    return 0


def parse_setting(key: str, text: str | None):
    if text is None:
        raise SystemExit(f"ldm settings {key}: missing value")
    if key == "speed_limit":
        return parse_rate(text)
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
    sub.add_parser("watch", help="show live progress")
    sub.add_parser("daemon", help="run the download service")

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
