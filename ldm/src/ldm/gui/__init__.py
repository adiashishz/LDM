"""GTK4/libadwaita front end: a D-Bus client of the ldm daemon."""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

# Oldest supported: Ubuntu 24.04 LTS / Linux Mint 22 (GTK 4.14, libadwaita 1.5). Newer widgets fall back
# to GTK ones in compat.py; older systems (e.g. Debian 12) can use the Flatpak.
MIN_GTK = (4, 12)
MIN_ADW = (1, 5)


def too_old() -> str | None:
    """Why the installed GTK or libadwaita can't run LDM, or None."""
    from gi.repository import Adw, Gtk

    gtk = (Gtk.get_major_version(), Gtk.get_minor_version())
    adw = (Adw.get_major_version(), Adw.get_minor_version())
    problems = [f"{name} {'.'.join(map(str, need))} or newer (this system has {'.'.join(map(str, have))})"
                for name, have, need in (("GTK", gtk, MIN_GTK), ("libadwaita", adw, MIN_ADW)) if have < need]
    return "LDM needs " + " and ".join(problems) + "." if problems else None


def _report_too_old(message: str) -> None:
    from gi.repository import GLib, Gtk

    print(message, file=sys.stderr)
    # Launched from the app menu nobody sees stderr: show a plain GTK dialog (works on any GTK 4).
    if not Gtk.init_check():
        return
    hint = "Install LDM from Flathub instead, or upgrade the system."
    dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                               text="LDM can't start", secondary_text=f"{message}\n{hint}")
    loop = GLib.MainLoop()
    dialog.connect("response", lambda *_: loop.quit())
    dialog.present()
    loop.run()


def main() -> int:
    """`ldm-gui` entry point: check the toolkit versions before loading the window code."""
    if message := too_old():
        _report_too_old(message)
        return 1
    from .app import main as app_main

    return app_main()
