"""Command-line interface (milestone 1: downloads run in the foreground; Ctrl-C pauses)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .engine import DownloadError, Downloader, LinkExpired, RemoteChanged
from .store import Store


def default_db_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"
    return Path(data_home) / "ldm" / "ldm.db"


def default_download_dir() -> Path:
    return Path(os.environ.get("XDG_DOWNLOAD_DIR") or Path.home() / "Downloads")


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def print_progress(done: int, size: int | None, speed: float) -> None:
    if size:
        eta = f"{(size - done) / speed:.0f}s" if speed > 0 else "--"
        line = f"{done * 100 / size:5.1f}%  {human(done)} / {human(size)}  {human(speed)}/s  ETA {eta}"
    else:
        line = f"{human(done)}  {human(speed)}/s"
    print(f"\r{line:<70}", end="", flush=True)


def parse_headers(values: list[str]) -> dict[str, str]:
    headers = {}
    for value in values:
        name, sep, content = value.partition(":")
        if not sep:
            raise SystemExit(f"invalid header (expected 'Name: value'): {value}")
        headers[name.strip()] = content.strip()
    return headers


def run_download(store: Store, download_id: int) -> int:
    downloader = Downloader(store, download_id, on_progress=print_progress)
    try:
        path = asyncio.run(downloader.run())
    except KeyboardInterrupt:
        print(f"\npaused — continue with: ldm resume {download_id}")
        return 130
    except LinkExpired as e:
        print(f"\nlink expired or refused ({e}) — a fresh link is needed")
        return 1
    except RemoteChanged as e:
        print(f"\nremote file changed ({e}) — the download must restart")
        return 1
    except DownloadError as e:
        print(f"\nfailed: {e} — retry with: ldm resume {download_id}")
        return 1
    print(f"\nsaved {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ldm", description="Multi-part download manager")
    parser.add_argument("--db", type=Path, default=default_db_path(), help="state database path")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="add a URL and download it")
    add.add_argument("url")
    add.add_argument("-o", "--dir", type=Path, default=default_download_dir())
    add.add_argument("-n", "--name", help="file name (default: from server)")
    add.add_argument("-c", "--connections", type=int, default=8, choices=range(1, 33), metavar="1-32")
    add.add_argument("-H", "--header", action="append", default=[], help="extra request header 'Name: value'")

    resume = sub.add_parser("resume", help="continue a paused or failed download")
    resume.add_argument("id", type=int)

    sub.add_parser("list", help="list downloads")

    args = parser.parse_args(argv)
    store = Store(args.db)

    if args.command == "add":
        download_id = store.add(args.url, args.dir, name=args.name,
                                headers=parse_headers(args.header), connections=args.connections)
        print(f"download {download_id}: {args.url}")
        return run_download(store, download_id)
    if args.command == "resume":
        try:
            d = store.get(args.id)
        except KeyError:
            print(f"no download with id {args.id}")
            return 1
        if d.state == "done":
            print(f"download {args.id} is already finished: {d.dest}")
            return 0
        return run_download(store, args.id)
    for d in store.list():
        size = human(d.size) if d.size is not None else "?"
        print(f"{d.id:>4}  {d.state:<8} {size:>10}  {d.name or d.url}" + (f"  ({d.error})" if d.error else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
