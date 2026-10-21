"""Render src/icons/icon-{16,32,48,128}.png from LDM's app icon (one design for the app and the extension).

Needs PyGObject (Fedora: python3-gobject), which LDM's GUI uses anyway.
"""

from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "ldm" / "src" / "ldm" / "resources" / "io.github.adiashishz.LDM.svg"
OUT = ROOT / "extension" / "src" / "icons"


def main() -> None:
    for size in (16, 32, 48, 128):
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(SOURCE), size, size)
        target = OUT / f"icon-{size}.png"
        pixbuf.savev(str(target), "png", [], [])
        print("wrote", target)


if __name__ == "__main__":
    main()
