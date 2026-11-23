# PyInstaller spec of LDM's Windows bundle: one folder (dist/LDM) with four programs sharing one Python:
#   ldm-gui.exe      the window (no console)
#   ldm-service.exe  the download service (no console; at login, and started by the window and the bridge)
#   ldm.exe          the command line
#   ldm-nmhost.exe   the browser bridge (the browsers talk to it over stdin/stdout)
# and in _internal: yt-dlp (its official zipimport build, so LDM's "update yt-dlp" can replace it) and the
# tools folder (ffmpeg, ffprobe and node). build.py prepares those and runs this with LDM_BUILD set.
#
# yt-dlp is analysed from the pip copy, so the bundle gets what it needs (standard library modules, its
# extras), and then its own modules are left out: a frozen copy would always be imported before the zip.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = Path(SPECPATH)  # noqa: F821 (PyInstaller defines it)
WORK = Path(os.environ["LDM_BUILD"])
ICON = str(WORK / "ldm.ico")

# The Linux desktop's D-Bus modules: never imported on Windows.
LINUX_ONLY = {"ldm.system", "ldm.notify", "ldm.tray", "ldm.dbus_service", "ldm.ui.desktop", "ldm.qt.desktop"}
# LDM imports some modules only when needed, yt-dlp (not frozen: a zip on sys.path) needs its "default"
# extras, and pystray picks its Windows backend by name.
HIDDEN = (
    [m for m in collect_submodules("ldm", on_error="ignore") if m not in LINUX_ONLY]
    + ["pystray._win32", "PIL.PngImagePlugin", "PIL.IcoImagePlugin", "PIL.BmpImagePlugin"]
    + [m for package in ("brotli", "certifi", "mutagen", "Cryptodome", "requests", "urllib3", "websockets",
                         "yt_dlp_ejs")
       for m in collect_submodules(package, filter=lambda name: ".tests" not in name and ".test." not in name)]
)
DATAS = (
    collect_data_files("ldm")            # Inter, icons
    + collect_data_files("certifi")
    + collect_data_files("yt_dlp_ejs")   # the JavaScript yt-dlp runs with node for YouTube
    + [(str(WORK / "yt-dlp" / "yt-dlp"), "yt-dlp")]
    + [(str(path), "tools") for path in sorted((WORK / "tools").iterdir())]
)
EXCLUDES = ["tkinter", "dbus_fast", "secretstorage", "gi"]


def analysis(script: str) -> Analysis:  # noqa: F821
    a = Analysis([str(HERE / script)], hiddenimports=HIDDEN, datas=DATAS, excludes=EXCLUDES,  # noqa: F821
                 runtime_hooks=[str(HERE / "tools_path.py")], noarchive=False)
    a.pure = [entry for entry in a.pure if entry[0] != "yt_dlp" and not entry[0].startswith("yt_dlp.")]
    return a


def program(a, name: str, console: bool):
    return EXE(PYZ(a.pure), a.scripts, [], exclude_binaries=True, name=name, console=console,  # noqa: F821
               icon=ICON, upx=False)


gui, service, cli, nmhost = (analysis(s) for s in ("ldm_gui.py", "ldm_service.py", "ldm_cli.py",
                                                   "ldm_nmhost.py"))
COLLECT(  # noqa: F821
    program(gui, "ldm-gui", console=False), gui.binaries, gui.datas,
    program(service, "ldm-service", console=False), service.binaries, service.datas,
    program(cli, "ldm", console=True), cli.binaries, cli.datas,
    program(nmhost, "ldm-nmhost", console=True), nmhost.binaries, nmhost.datas,
    name="LDM", upx=False,
)
