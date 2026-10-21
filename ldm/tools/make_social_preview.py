"""Render .github/social-preview.png (1280x640), the image GitHub shows when the repo link is shared.

Upload it under the repository's Settings -> General -> Social preview (git can't set it).
"""

import io
from pathlib import Path

import cairo
import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import GdkPixbuf, Pango, PangoCairo  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ICON = ROOT / "ldm" / "src" / "ldm" / "resources" / "io.github.adiashishz.LDM.svg"
OUT = ROOT / ".github" / "social-preview.png"
W, H = 1280, 640


def text(cr, markup: str, font: str, x: float, y: float, rgb: tuple[float, float, float]) -> float:
    layout = PangoCairo.create_layout(cr)
    layout.set_font_description(Pango.FontDescription.from_string(font))
    layout.set_markup(markup, -1)
    cr.set_source_rgb(*rgb)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)
    return layout.get_pixel_size()[1]


def main() -> None:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surface)
    background = cairo.LinearGradient(0, 0, W, H)
    background.add_color_stop_rgb(0, 0.98, 0.98, 0.98)
    background.add_color_stop_rgb(1, 0.91, 0.94, 0.99)
    cr.set_source(background)
    cr.paint()
    _, png = GdkPixbuf.Pixbuf.new_from_file_at_size(str(ICON), 320, 320).save_to_bufferv("png", [], [])
    cr.set_source_surface(cairo.ImageSurface.create_from_png(io.BytesIO(png)), 110, (H - 320) / 2)
    cr.paint()
    x, y = 500, 170
    y += text(cr, "<b>LDM</b>", "Sans 88", x, y, (0.12, 0.12, 0.14))
    y += text(cr, "Download manager for Linux", "Sans 34", x, y + 4, (0.24, 0.24, 0.27)) + 26
    y += text(cr, "Files · Videos · Torrents · Browser extension", "Sans 24", x, y, (0.11, 0.44, 0.85)) + 30
    text(cr, "github.com/adiashishz/LDM", "Monospace 20", x, y, (0.40, 0.40, 0.44))
    OUT.parent.mkdir(exist_ok=True)
    surface.write_to_png(str(OUT))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
