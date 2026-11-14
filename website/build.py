#!/usr/bin/env python3
"""Build the website (https://ldm-downloader.github.io) into a folder.

    build.py OUT [--version X.Y.Z]

The version decides the download links: the release workflow passes the version it publishes, and by
default it is the one in ldm/pyproject.toml. The page itself is index.html in this folder, with
{{PLACEHOLDERS}}; the icon and screenshots come from the app's own files.
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = "https://github.com/adiashishz/LDM"
SITE = "https://ldm-downloader.github.io"
ASSETS = {
    "icon.svg": ROOT / "ldm/src/ldm/resources/io.github.adiashishz.LDM.svg",
    "screenshot.png": ROOT / "ldm/data/screenshots/main.png",
    "screenshot-dark.png": ROOT / "ldm/data/screenshots/main-dark.png",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--version", default=re.search(r'^version = "(.+)"', (ROOT / "ldm/pyproject.toml")
                                                       .read_text(), re.M).group(1))
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        sys.exit(f"not a version: {args.version}")
    values = {"VERSION": args.version, "REPO": REPO, "SITE": SITE,
              "DL": f"{REPO}/releases/download/v{args.version}"}
    page = (HERE / "index.html").read_text()
    for key, value in values.items():
        page = page.replace("{{" + key + "}}", value)
    if left := re.findall(r"\{\{\w+\}\}", page):
        sys.exit(f"placeholders without a value: {sorted(set(left))}")
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.html").write_text(page)
    for name, source in ASSETS.items():
        shutil.copyfile(source, args.out / name)
    (args.out / ".nojekyll").write_text("")  # plain files: no Jekyll processing on GitHub Pages
    print(f"built the site for {args.version} in {args.out}")


if __name__ == "__main__":
    main()
