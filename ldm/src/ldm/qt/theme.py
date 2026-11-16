"""Colours, fonts and icons of the "A + G + Classic" design, light and dark.

The window follows the system's light or dark setting unless the user picks one. Icons are the
design's 24×24 line drawings, drawn in any colour through QtSvg.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer


@dataclass(frozen=True)
class Colors:
    dark: bool
    win: str
    chrome: str
    side: str
    panel: str
    card: str
    line: str
    fg: str
    dim: str
    field: str
    stripe: str
    track: str
    press: str
    accent: str
    red: str
    green: str
    bar_bad: str = "#DC2626"
    bar_done: str = "#16A34A"
    bar_muted: str = "#8E8E93"

    def bar(self, tone: str) -> str:
        return {"bad": self.bar_bad, "done": self.bar_done, "muted": self.bar_muted}.get(tone, self.accent)

    def text(self, tone: str) -> str:
        """Status text: red for problems, green when done, accent while active, dim otherwise."""
        return {"bad": self.red, "done": self.green, "muted": self.dim}.get(tone, self.accent)


LIGHT = Colors(False, win="#FFFFFF", chrome="#F7F7F8", side="#F2F3F5", panel="#FAFAFB", card="#FFFFFF",
               line="#E3E4E8", fg="#1C1D21", dim="#5F6168", field="#ECEDF0", stripe="#F7F7F9", track="#E5E6EA",
               press="#E7E8EC", accent="#2563EB", red="#B91C1C", green="#15803D")
DARK = Colors(True, win="#1C1C1F", chrome="#202024", side="#232327", panel="#202024", card="#2A2A2F",
              line="#34343A", fg="#F2F2F4", dim="#A1A1A8", field="#2E2E33", stripe="#212125", track="#38383E",
              press="#303036", accent="#3B82F6", red="#F87171", green="#4ADE80")

# File tiles and category icons
GROUP_COLORS = {"archive": "#7C3AED", "document": "#D97706", "audio": "#DB2777", "program": "#4F46E5",
                "video": "#E11D48", "image": "#059669", "torrent": "#0891B2", "other": "#71717A"}

# The design's icons: SVG path data on a 24×24 grid, drawn as 1.8px lines.
ICONS = {
    "add": "M12 5v14M5 12h14",
    "resume": "M8 5v14l11-7z",
    "pause": "M8 5v14M16 5v14",
    "pause-all": "M6 5v14M11 5v14M16 9l5 3-5 3",
    "delete": "M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13",
    "schedule": "M12 7v5l3 2M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z",
    "queue-start": "M4 6h10M4 12h10M4 18h6M16 14v6l5-3z",
    "queue-stop": "M4 6h10M4 12h10M4 18h6M16 15h5v5h-5z",
    "grab": "M4 5h16v11H4zM9 20h6M10 9v4l3.5-2z",
    "settings": "M4 7h10M18 7h2M4 17h2M10 17h10M16 5v4M8 15v4",
    "details": "M3 7a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3zM15 4v16",
    "all": "M12 4v11M7 10l5 5 5-5M5 20h14",
    "downloading": "M4 12h4l3-7 4 14 3-7h2",
    "scheduled": "M12 7v5l3 2M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z",
    "finished": "M5 12.5l4.5 4.5L19 7.5",
    "problems": "M12 9v4M12 16.5v.5M10.3 4.3L2.6 18a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 4.3a2 2 0 0 0-3.4 0z",
    "queue": "M4 6h16M4 12h16M4 18h10",
    "archive": "M4 4h16v4H4zM5 8v12h14V8M10 12h4",
    "document": "M6 3h8l4 4v14H6zM9 12h6M9 16h6",
    "audio": "M9 18V5l11-2v13M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0zM20 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z",
    "program": "M4 5h16v14H4zM4 9h16",
    "video": "M4 6h12v12H4zM16 10l5-3v10l-5-3",
    "image": "M4 5h16v14H4zM4 16l5-5 4 4 3-3 4 4M15 9.5a1 1 0 1 0 0-.01",
    "torrent": "M6 3v7a6 6 0 0 0 12 0V3M6 3h3v7a3 3 0 0 0 6 0V3h3",
    "other": "M6 3h8l4 4v14H6z",
    "search": "M10.5 17a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13zM15.5 15.5L20 20",
    "chevron-down": "M7 10l5 5 5-5",
    "chevron-right": "M10 7l5 5-5 5",
    "more": "M6 12h.01M12 12h.01M18 12h.01",
    "folder": "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
    "close": "M6 6l12 12M18 6L6 18",
}
LOGO = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 28"><rect width="28" height="28" rx="7" '
        'fill="{accent}"/><path d="M14 7v10M9.5 13l4.5 4.5 4.5-4.5M9 21h10" stroke="#fff" stroke-width="2.5" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def svg_pixmap(svg: str, size: int) -> QPixmap:
    ratio = QGuiApplication.instance().devicePixelRatio() if QGuiApplication.instance() else 1.0
    pixmap = QPixmap(round(size * ratio), round(size * ratio))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, pixmap.width(), pixmap.height()))
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def icon(name: str, color: str, size: int = 20, width: float = 1.8) -> QIcon:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="{ICONS[name]}" '
           f'stroke="{color}" stroke-width="{width}" fill="none" stroke-linecap="round" '
           f'stroke-linejoin="round"/></svg>')
    return QIcon(svg_pixmap(svg, size))


def tool_icon(name: str, color: str, disabled: str, size: int = 20) -> QIcon:
    """A toolbar icon with its greyed-out look for when the action can't be used."""
    result = icon(name, color, size)
    result.addPixmap(icon(name, disabled, size).pixmap(size, size), QIcon.Mode.Disabled)
    return result


def logo(accent: str, size: int = 20) -> QPixmap:
    return svg_pixmap(LOGO.format(accent=accent), size)


_current = LIGHT


def current() -> Colors:
    """The colours in use (for widgets that paint themselves)."""
    return _current


def system_is_dark() -> bool:
    hints = QGuiApplication.styleHints()
    return hints.colorScheme() == Qt.ColorScheme.Dark


def colors_for(choice: str) -> Colors:
    """`choice`: "system", "light" or "dark"."""
    global _current
    dark = system_is_dark() if choice == "system" else choice == "dark"
    _current = DARK if dark else LIGHT
    return _current


FONTS = Path(__file__).with_name("fonts")  # Inter 4.1 (SIL Open Font License), the design's font


def ui_font() -> QFont:
    """Inter — bundled with LDM, so every system shows the design's font — at the design's 13px."""
    for path in sorted(FONTS.glob("Inter-*.ttf")):
        QFontDatabase.addApplicationFont(str(path))
    font = QFont("Inter") if "Inter" in QFontDatabase.families() else QGuiApplication.font()
    font.setPixelSize(13)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)  # Inter's shapes, as in the design
    return font


def palette(c: Colors) -> QPalette:
    p = QPalette()
    for role, value in ((QPalette.ColorRole.Window, c.win), (QPalette.ColorRole.WindowText, c.fg),
                        (QPalette.ColorRole.Base, c.win), (QPalette.ColorRole.AlternateBase, c.stripe),
                        (QPalette.ColorRole.Text, c.fg), (QPalette.ColorRole.Button, c.card),
                        (QPalette.ColorRole.ButtonText, c.fg), (QPalette.ColorRole.Highlight, c.accent),
                        (QPalette.ColorRole.HighlightedText, "#FFFFFF"), (QPalette.ColorRole.ToolTipBase, c.card),
                        (QPalette.ColorRole.ToolTipText, c.fg), (QPalette.ColorRole.PlaceholderText, c.dim),
                        (QPalette.ColorRole.Mid, c.line), (QPalette.ColorRole.Midlight, c.line),
                        (QPalette.ColorRole.Link, c.accent)):
        p.setColor(role, QColor(value))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(c.dim))
    return p


def arrow_files(color: str) -> dict[str, str]:
    """Small up/down arrow images for spin and combo boxes (Qt style sheets take images only from files)."""
    from PySide6.QtCore import QStandardPaths

    folder = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation) or ".") / "icons"
    folder.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, points in (("up", "2,7 6,3 10,7"), ("down", "2,4 6,8 10,4")):
        path = folder / f"arrow-{name}-{color.lstrip('#')}.svg"
        if not path.exists():
            path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">'
                            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.6" '
                            f'stroke-linecap="round" stroke-linejoin="round"/></svg>')
        files[name] = path.as_posix()
    return files


def stylesheet(c: Colors) -> str:
    arrows = arrow_files(c.dim)
    return f"""
QMainWindow, #central {{ background: {c.win}; }}
#topbar {{ background: {c.chrome}; }}
#topbar QMenuBar {{ background: transparent; color: {c.fg}; }}
#topbar QMenuBar::item {{ padding: 6px 10px; font-size: 13px; border-radius: 6px; background: transparent; }}
#topbar QMenuBar::item:selected, #topbar QMenuBar::item:pressed {{ background: {c.press}; }}
#brand {{ font-weight: 700; color: {c.fg}; }}
#search {{ background: {c.field}; border: 0; border-radius: 8px; padding: 0 8px; color: {c.fg};
          min-height: 30px; selection-background-color: {c.accent}; }}
#toolbar {{ background: {c.chrome}; border-bottom: 1px solid {c.line}; }}
#toolbar QToolButton {{ border: 0; border-radius: 8px; color: {c.fg}; font-size: 11.5px; padding: 4px 2px; }}
#toolbar QToolButton:hover {{ background: {c.press}; }}
#toolbar QToolButton:checked {{ background: {c.press}; }}
#toolbar QToolButton:disabled {{ color: {c.dim}; }}
#toolbar QToolButton#primary {{ background: {c.accent}; color: #FFFFFF; font-weight: 600; }}
#toolbar QToolButton#primary:hover {{ background: {QColor(c.accent).darker(110).name()}; }}
#toolbar QToolButton#primary:disabled {{ background: {c.track}; color: {c.dim}; }}
#toolsep {{ background: {c.line}; }}
QSplitter::handle {{ background: transparent; }}
#sidebar {{ background: {c.side}; border-right: 1px solid {c.line}; }}
#sidebar QLabel#section {{ color: {c.dim}; font-size: 11px; font-weight: 600; }}
#speedcard {{ background: {c.card}; border: 1px solid {c.line}; border-radius: 12px; }}
#speedcard QLabel {{ color: {c.dim}; font-size: 12px; }}
#speedcard QLabel#speed {{ color: {c.fg}; font-size: 13px; font-weight: 700; }}
QTableView {{ background: {c.win}; border: 0; color: {c.fg}; outline: 0; }}
QHeaderView {{ background: {c.win}; border: 0; }}
QHeaderView::section {{ background: {c.win}; color: {c.dim}; border: 0; border-bottom: 1px solid {c.line};
                        font-size: 11.5px; font-weight: 600; padding: 0 10px; height: 32px; }}
QHeaderView::section:hover {{ color: {c.fg}; }}
#details {{ background: {c.panel}; border-left: 1px solid {c.line}; }}
#details #detailshead {{ border-bottom: 1px solid {c.line}; }}
#details QLabel {{ color: {c.fg}; }}
#details QLabel#dim, #details QLabel#factkey {{ color: {c.dim}; }}
#details QLabel#name {{ font-size: 15px; font-weight: 700; }}
#details QLabel#percent {{ font-size: 32px; font-weight: 700; }}
#details QLabel#partslabel {{ font-size: 12px; font-weight: 600; }}
#details QPushButton {{ height: 36px; border-radius: 8px; border: 1px solid {c.line}; background: {c.card};
                       color: {c.fg}; padding: 0 12px; }}
#details QPushButton:hover {{ background: {c.press}; }}
#details QPushButton:disabled {{ color: {c.dim}; background: transparent; }}
#details QPushButton#primary {{ border: 0; background: {c.accent}; color: #FFFFFF; font-weight: 600; }}
#details QPushButton#primary[danger="true"] {{ background: {c.bar_bad}; }}
#details QToolButton {{ border: 0; border-radius: 8px; }}
#details QToolButton:hover {{ background: {c.press}; }}
#statusbar {{ background: {c.chrome}; border-top: 1px solid {c.line}; }}
#statusbar QLabel {{ color: {c.dim}; font-size: 12px; }}
#statusbar QLabel#speed {{ color: {c.fg}; font-weight: 600; }}
#banner {{ background: {c.field}; border-bottom: 1px solid {c.line}; }}
#banner QLabel {{ color: {c.fg}; font-weight: 600; }}
#banner QPushButton {{ border: 0; border-radius: 8px; background: {c.accent}; color: #FFFFFF; padding: 6px 14px;
                      font-weight: 600; }}
#banner QToolButton {{ border: 0; border-radius: 8px; }}
#banner QToolButton:hover {{ background: {c.press}; }}
#toast {{ background: {"#F2F2F4" if c.dark else "#1C1D21"}; border-radius: 10px; }}
#toast QLabel {{ color: {"#1C1D21" if c.dark else "#F2F2F4"}; }}
#toast QPushButton {{ border: 0; background: transparent; color: {"#2563EB" if c.dark else "#93C5FD"};
                     font-weight: 600; padding: 4px 8px; }}
#empty QLabel {{ color: {c.dim}; }}
#empty QLabel#title {{ color: {c.fg}; font-size: 15px; font-weight: 700; }}
QMenu {{ background: {c.card}; color: {c.fg}; border: 1px solid {c.line}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {c.press}; }}
QMenu::item:disabled {{ color: {c.dim}; }}
QMenu::separator {{ height: 1px; background: {c.line}; margin: 4px 6px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {c.track}; border-radius: 4px; min-height: 30px; margin: 2px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QDialog#dialog, #page, #pagebody {{ background: {c.side}; }}
#footer {{ background: {c.chrome}; border-top: 1px solid {c.line}; }}
#card {{ background: {c.card}; border: 1px solid {c.line}; border-radius: 10px; }}
#rowline {{ background: {c.line}; }}
#rowtitle {{ color: {c.fg}; }}
#rowsub {{ color: {c.dim}; font-size: 12px; }}
#cardtitle {{ color: {c.fg}; font-weight: 600; }}
#foldbody {{ background: transparent; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{ background: {c.field}; color: {c.fg};
    border: 1px solid transparent; border-radius: 7px; padding: 5px 8px; selection-background-color: {c.accent}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus {{
    border: 1px solid {c.accent}; }}
QLineEdit[error="true"] {{ border: 1px solid {c.red}; }}
QComboBox {{ padding-right: 26px; }}
QComboBox::drop-down {{ border: 0; width: 24px; subcontrol-origin: padding; subcontrol-position: center right; }}
QComboBox::down-arrow {{ image: url({arrows["down"]}); width: 12px; height: 12px; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 22px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: padding; subcontrol-position: top right;
    width: 20px; border: 0; background: transparent; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: padding; subcontrol-position: bottom right;
    width: 20px; border: 0; background: transparent; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({arrows["up"]}); width: 10px; height: 10px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({arrows["down"]}); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{ background: {c.card}; color: {c.fg}; border: 1px solid {c.line};
    selection-background-color: {c.press}; selection-color: {c.fg}; outline: 0; }}
QPushButton {{ background: {c.card}; color: {c.fg}; border: 1px solid {c.line}; border-radius: 8px;
    padding: 6px 14px; }}
QPushButton:hover {{ background: {c.press}; }}
QPushButton:disabled {{ color: {c.dim}; }}
QPushButton#primary {{ background: {c.accent}; color: #FFFFFF; border: 0; font-weight: 600; }}
QPushButton#primary:disabled {{ background: {c.track}; color: {c.dim}; }}
QPushButton#day {{ padding: 5px 0; }}
QPushButton#day:checked {{ background: {c.accent}; color: #FFFFFF; border: 0; }}
#badge {{ font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 9px; color: {c.dim}; }}
#badge[tone="accent"] {{ color: {c.accent}; background: {c.field}; }}
#badge[tone="good"] {{ color: {c.green}; background: {c.field}; }}
#badge[tone="warn"] {{ color: {c.red}; background: {c.field}; }}
QCheckBox {{ color: {c.fg}; spacing: 8px; }}
QToolTip {{ background: {c.card}; color: {c.fg}; border: 1px solid {c.line}; padding: 4px 6px; }}
"""
