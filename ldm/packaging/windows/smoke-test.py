"""Smoke test of LDM's Windows installer (CI runs it after build.py): install it silently for this user, then
- the bundle brings yt-dlp, libtorrent, ffmpeg and Node.js (`ldm version`);
- the browser bridge, magnet links, .torrent files and starting at login are registered, and LDM's folder is
  on the user's PATH;
- the service starts (ldm-service.exe) and a download through it finishes, from the command line and from
  the browser bridge;
- the window opens and a second start hands its link to it;
- `ldm quit` ends the service and the window;
- uninstalling removes the programs, the registration and the PATH entry (the user's downloads and settings
  stay).

    python smoke-test.py build\\windows\\LDM-Setup-<version>.exe

Standard library only: it runs with any Python, outside the bundle.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import winreg
from pathlib import Path

APP = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "LDM"
HOST = "io.github.adiashishz.ldm"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def step(text: str) -> None:
    print(f"--- {text}", flush=True)


def ldm(*args: str, env: dict | None = None, timeout: float = 120) -> str:
    result = subprocess.run([str(APP / "ldm.exe"), *args], capture_output=True, text=True, env=env,
                            timeout=timeout)
    print(result.stdout + result.stderr, end="", flush=True)
    if result.returncode:
        sys.exit(f"ldm {' '.join(args)} failed ({result.returncode})")
    return result.stdout


def value(key: str, name: str = "") -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as opened:
            return str(winreg.QueryValueEx(opened, name)[0])
    except OSError:
        return None


def on_path() -> bool:
    """LDM's folder is in the user's PATH (the installer adds it, so `ldm` works in a terminal)."""
    folders = (value("Environment", "Path") or "").split(";")
    return any(folder and Path(os.path.expandvars(folder)) == APP for folder in folders)


def until(condition, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            sys.exit(f"timed out: {what}")
        time.sleep(0.25)


def bridge(*messages: dict) -> list[dict]:
    """Talk to ldm-nmhost.exe as a browser does: 32-bit length, then JSON."""
    data = b"".join(struct.pack("<I", len(body)) + body for body in (json.dumps(m).encode() for m in messages))
    result = subprocess.run([str(APP / "ldm-nmhost.exe")], input=data, capture_output=True, timeout=60)
    out, replies = result.stdout, []
    while out:
        (length,) = struct.unpack("<I", out[:4])
        replies.append(json.loads(out[4:4 + length]))
        out = out[4 + length:]
    return replies


def install(setup: Path) -> None:
    step(f"install {setup.name}")
    subprocess.run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
                    f"/LOG={Path(tempfile.gettempdir()) / 'ldm-install.log'}"], check=True, timeout=600)
    for name in ("ldm-gui.exe", "ldm-service.exe", "ldm.exe", "ldm-nmhost.exe", "unins000.exe"):
        assert (APP / name).is_file(), f"{name} is missing"


def bundled_tools() -> None:
    step("what the bundle brings")
    text = ldm("version")
    tools = str(APP / "_internal" / "tools").lower()
    lines = {line.split(" ", 1)[0].rstrip(":"): line for line in text.lower().splitlines()}
    assert lines["yt-dlp"].startswith("yt-dlp 20"), lines["yt-dlp"]
    assert str(APP / "_internal" / "yt-dlp").lower() in lines["yt-dlp"], "yt-dlp isn't the bundled copy"
    assert lines["libtorrent"].startswith("libtorrent 2"), lines["libtorrent"]
    assert tools in lines["ffmpeg"], lines["ffmpeg"]
    assert "node" in lines["javascript"] and tools in lines["javascript"], lines["javascript"]


def registration() -> None:
    step("registration")
    for browser in (r"Mozilla", r"Google\Chrome", r"Microsoft\Edge"):
        manifest = value(rf"Software\{browser}\NativeMessagingHosts\{HOST}")
        assert manifest, f"no native messaging host for {browser}"
        program = json.loads(Path(manifest).read_text(encoding="utf-8"))["path"]
        assert Path(program) == APP / "ldm-nmhost.exe", program
    assert str(APP / "ldm-gui.exe") in (value(r"Software\Classes\LDM.magnet\shell\open\command") or "")
    assert str(APP / "ldm-service.exe") in (value(RUN_KEY, "LDM") or ""), value(RUN_KEY, "LDM")
    assert value(r"Software\RegisteredApplications", "LDM")
    assert on_path(), f"LDM's folder isn't on the user's PATH: {value('Environment', 'Path')}"


def downloads(env: dict, work: Path) -> subprocess.Popen:
    step("downloads through the service")
    (work / "www").mkdir()
    data = os.urandom(3_000_000)
    (work / "www" / "file.bin").write_bytes(data)
    (work / "www" / "other.bin").write_bytes(data[::-1])
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(work / "www"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    service = subprocess.Popen([str(APP / "ldm-service.exe")], env=env)
    until(lambda: subprocess.run([str(APP / "ldm.exe"), "list"], env=env, capture_output=True).returncode == 0,
          60, "the service answers")
    ldm("add", f"{base}/file.bin", "--wait", env=env)
    assert (work / "Downloads" / "file.bin").read_bytes() == data, "the download differs"

    replies = bridge({"id": 1, "cmd": "ping"}, {"id": 2, "cmd": "add", "url": f"{base}/other.bin", "mode": "direct"})
    print(replies, flush=True)
    assert replies[0]["ok"] and replies[0]["daemon"], replies[0]
    assert replies[1]["ok"] and replies[1]["download_id"], replies[1]
    until(lambda: (work / "Downloads" / "other.bin").exists()
          and (work / "Downloads" / "other.bin").read_bytes() == data[::-1], 60, "the bridge's download")
    return service


def window(env: dict) -> subprocess.Popen:
    step("the window")
    gui = subprocess.Popen([str(APP / "ldm-gui.exe")], env={**env, "QT_QPA_PLATFORM": "offscreen"})
    until(lambda: bridge({"id": 1, "cmd": "ping"})[0].get("gui"), 60, "the window connects to the service")
    assert gui.poll() is None, "the window ended"
    second = subprocess.run([str(APP / "ldm-gui.exe"), "magnet:?xt=urn:btih:" + "a" * 40],
                            env={**env, "QT_QPA_PLATFORM": "offscreen"}, timeout=60)
    assert second.returncode == 0, "a second start didn't hand its link to the window"
    assert gui.poll() is None, "the window ended"
    return gui


def uninstall() -> None:
    step("uninstall")
    subprocess.run([str(APP / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], check=True,
                   timeout=300)
    # The uninstaller runs on from a copy of itself: wait for the files to go.
    until(lambda: not (APP / "ldm.exe").exists(), 120, "the programs are removed")
    assert value(rf"Software\Mozilla\NativeMessagingHosts\{HOST}") is None, "the bridge is still registered"
    assert value(r"Software\Classes\LDM.magnet") is None, "magnet links are still registered"
    assert value(RUN_KEY, "LDM") is None, "LDM still starts at login"
    assert value(r"Software\RegisteredApplications", "LDM") is None
    assert not on_path(), f"LDM's folder is still on the user's PATH: {value('Environment', 'Path')}"


def main() -> int:
    setup = Path(sys.argv[1]).resolve()
    work = Path(tempfile.mkdtemp(prefix="ldm-smoke-"))
    (work / "Downloads").mkdir()
    env = {**os.environ, "XDG_DOWNLOAD_DIR": str(work / "Downloads")}  # LDM honours it everywhere
    install(setup)
    bundled_tools()
    registration()
    service = downloads(env, work)
    gui = window(env)
    step("ldm quit")
    ldm("quit", env=env)
    until(lambda: service.poll() is not None, 30, "the service ends")
    until(lambda: gui.poll() is not None, 30, "the window closes")
    uninstall()
    step("all good")
    return 0


if __name__ == "__main__":
    sys.exit(main())
