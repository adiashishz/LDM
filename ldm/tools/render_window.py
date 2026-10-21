"""Dev tool: render the main window and dialogs to PNG files without a visible desktop window.

Run with a headless GDK backend and a daemon on the session bus, e.g.
    gtk4-broadwayd :5 &
    GDK_BACKEND=broadway BROADWAY_DISPLAY=:5 python tools/render_window.py OUT_DIR
"""

import faulthandler
import sys
from pathlib import Path

from ldm.gui.app import Application  # pins Gtk 4 / Adw 1 before gi.repository imports
from ldm.gui.preferences import PreferencesDialog

from gi.repository import Gio, GLib, Gtk  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")


def save(widget: Gtk.Widget, name: str) -> None:
    width, height = widget.get_width(), widget.get_height()
    snapshot = Gtk.Snapshot.new()
    Gtk.WidgetPaintable.new(widget).snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None:
        print(f"nothing to render for {name}: mapped={widget.get_mapped()} size={width}x{height}", flush=True)
        return
    texture = widget.get_native().get_renderer().render_texture(node, None)
    texture.save_to_png(str(OUT / f"{name}.png"))
    print("saved", OUT / f"{name}.png", f"{width}x{height}", flush=True)


def close_dialog(window) -> None:
    dialog = window.get_visible_dialog()
    if dialog:
        dialog.close()


def steps(app: Application):
    window = app.get_active_window()
    for _ in range(100):  # wait until the backend has mapped and sized the window
        if window.get_mapped() and window.get_width() > 0:
            break
        yield 0.1
    yield 2.0
    save(window, "1-list")
    window.open_add_dialog("https://example.com/files/ubuntu.iso")
    yield 1.0
    save(window, "2-add-dialog")
    close_dialog(window)
    PreferencesDialog(app.client).present(window)
    yield 1.0
    save(window, "3-preferences")
    close_dialog(window)
    window.filters.set_active_name("problems")
    yield 0.5
    save(window, "4-problems")
    window.filters.set_active_name("finished")
    yield 0.5
    save(window, "5-finished")
    app.quit()


def main() -> None:
    faulthandler.dump_traceback_later(60, exit=True)  # never hang a headless run
    OUT.mkdir(parents=True, exist_ok=True)
    app = Application(flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(app):
        # Headless backends may not tick the frame clock: animated dialogs would stay invisible.
        Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
        it = steps(app)

        def next_step():
            delay = next(it, None)
            if delay is not None:
                GLib.timeout_add(int(delay * 1000), next_step)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(next_step)

    app.connect("activate", activate)
    app.run([])


if __name__ == "__main__":
    main()
