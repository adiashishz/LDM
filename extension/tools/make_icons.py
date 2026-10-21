"""Generate src/icons/icon-{16,32,48,128}.png: white download arrow on a blue rounded square.

Standard library only (supersampled for anti-aliasing).
"""

import struct
import zlib
from pathlib import Path

BLUE = (0x35, 0x84, 0xE4)
OUT = Path(__file__).resolve().parent.parent / "src" / "icons"
SS = 4  # supersampling factor


def inside_rounded(x, y, n, r):
    cx = min(max(x, r), n - r)
    cy = min(max(y, r), n - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def inside_arrow(x, y, n):
    u, v = x / n, y / n
    shaft = abs(u - 0.5) <= 0.075 and 0.20 <= v <= 0.52
    head = 0.42 <= v <= 0.68 and abs(u - 0.5) <= 0.25 * (0.68 - v) / 0.26
    base = 0.75 <= v <= 0.83 and abs(u - 0.5) <= 0.28
    return shaft or head or base


def pixel(px, py, size):
    big = size * SS
    covered = arrow = 0
    for sy in range(SS):
        for sx in range(SS):
            x, y = px * SS + sx + 0.5, py * SS + sy + 0.5
            if inside_rounded(x, y, big, big * 0.22):
                covered += 1
                arrow += inside_arrow(x, y, big)
    total = SS * SS
    if not covered:
        return (0, 0, 0, 0)
    mix = arrow / covered
    rgb = tuple(round(c * (1 - mix) + 255 * mix) for c in BLUE)
    return (*rgb, round(255 * covered / total))


def png(size):
    rows = b"".join(b"\x00" + bytes(c for px in range(size) for c in pixel(px, py, size)) for py in range(size))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 48, 128):
        (OUT / f"icon-{size}.png").write_bytes(png(size))
        print("wrote", OUT / f"icon-{size}.png")
