"""Running as a Flatpak: folder checks, real folder names, the installer's Flatpak mode, start at login."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ldm import integration, sandbox
from ldm.manager import InvalidArgument, Manager
from ldm.store import Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def flatpak(monkeypatch):
    monkeypatch.setenv("FLATPAK_ID", sandbox.APP_ID)


def test_detects_a_flatpak(monkeypatch):
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.setattr(sandbox.Path, "exists", lambda self: False if str(self) == "/.flatpak-info" else True)
    assert not sandbox.flatpak()
    monkeypatch.setenv("FLATPAK_ID", sandbox.APP_ID)
    assert sandbox.flatpak()


def test_folder_problem_outside_a_flatpak(tmp_path, monkeypatch):
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.setattr(sandbox, "flatpak", lambda: False)
    assert sandbox.folder_problem(tmp_path) is None
    assert sandbox.folder_problem(tmp_path / "new" / "deeper") is None  # created when the download starts
    (tmp_path / "file").write_text("x")
    assert "is a file" in sandbox.folder_problem(tmp_path / "file" / "sub")
    if os.geteuid() != 0:  # root can write anywhere
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o500)
        assert "permission denied" in sandbox.folder_problem(locked / "sub")


def test_folder_problem_in_a_flatpak(tmp_path, monkeypatch, flatpak):
    monkeypatch.setattr(sandbox, "_sandbox_only", lambda folder: False)
    assert sandbox.folder_problem(tmp_path) is None
    # A folder the app wasn't given exists only on the sandbox's own temporary root: refused.
    monkeypatch.setattr(sandbox, "_sandbox_only", lambda folder: True)
    problem = sandbox.folder_problem(tmp_path / "Videos")
    assert "LDM has no access to" in problem and "Flatseal" in problem


def test_sandbox_only_compares_devices(tmp_path):
    assert sandbox._sandbox_only(Path("/")) is True
    assert sandbox._sandbox_only(tmp_path / "missing") is False


def test_host_path(tmp_path, monkeypatch):
    assert sandbox.host_path("/home/me/Downloads") == "/home/me/Downloads"
    doc = "/run/user/1000/doc/1a2b3c4d/Videos"
    monkeypatch.setattr(sandbox.os, "getxattr", lambda path, name: b"/home/me/Videos\0", raising=False)
    assert sandbox.host_path(doc) == "/home/me/Videos"
    monkeypatch.setattr(sandbox.os, "getxattr", lambda path, name: (_ for _ in ()).throw(OSError()), raising=False)
    assert sandbox.host_path(doc) == doc  # an older portal: the path as it is


def test_manager_refuses_a_folder_it_cannot_use(tmp_path, monkeypatch):
    manager = Manager(Store(tmp_path / "state.db"))
    monkeypatch.setattr(sandbox, "folder_problem", lambda path: "LDM has no access to /home/me/Videos" if "Videos"
                        in str(path) else None)
    with pytest.raises(InvalidArgument, match="no access"):
        manager.add("http://127.0.0.1:9/file", dir="/home/me/Videos", start=False)
    with pytest.raises(InvalidArgument, match="no access"):
        manager.update_settings({"download_dir": "/home/me/Videos"})
    with pytest.raises(InvalidArgument, match="no access"):
        manager.update_settings({"move_finished_dir": "/home/me/Videos"})
    assert manager.add("http://127.0.0.1:9/file", dir=str(tmp_path / "ok"), start=False)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home/.var/app/io.github.adiashishz.LDM/data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home/.var/app/io.github.adiashishz.LDM/config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    calls = []
    monkeypatch.setattr(integration, "_systemctl", lambda *args: calls.append(args) or True)
    monkeypatch.setattr(integration, "_reload_bus_config", lambda: calls.append("reload"))
    monkeypatch.setattr(sandbox, "request_autostart", lambda enabled: calls.append(("portal", enabled)) or True)
    monkeypatch.setattr(integration, "CALLS", calls, raising=False)
    return tmp_path / "home"


def test_flatpak_install_sets_up_every_host_browser(home, flatpak):
    lines = []
    chosen = integration.install(home, log=lines.append)
    assert sorted(b.name for b in chosen) == ["brave", "chrome", "chromium", "firefox"]  # not brave-flatpak
    data = home / ".var/app/io.github.adiashishz.LDM/data/ldm/native-host"
    launcher = data / integration.LAUNCHER_NAME
    assert launcher.read_text() == integration.FLATPAK_LAUNCHER and os.access(launcher, os.X_OK)
    assert (data / "ldm_nmhost.py").exists()
    manifest = json.loads((home / ".mozilla/native-messaging-hosts/io.github.adiashishz.ldm.json").read_text())
    assert manifest["path"] == str(launcher)
    # Flatpak exports the desktop file and D-Bus activation; no systemd, no xdg-mime, but the portal.
    assert not (home / ".var/app/io.github.adiashishz.LDM/data/applications").exists()
    assert integration.CALLS == [("portal", True)]
    assert any("desktop's settings" in line for line in lines)


def test_flatpak_launcher_prefers_the_host_python(home, flatpak, tmp_path):
    integration.install(home, ["firefox"], log=lambda _: None)
    launcher = home / ".var/app/io.github.adiashishz.LDM/data/ldm/native-host" / integration.LAUNCHER_NAME
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("python3", 'echo "host python: $*"'), ("gdbus", "true"),
                       ("flatpak", 'echo "flatpak: $*"')):
        (bin_dir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / name).chmod(0o755)
    (bin_dir / "dirname").symlink_to(subprocess.run(["sh", "-c", "command -v dirname"], capture_output=True,
                                                    text=True).stdout.strip())
    run = lambda: subprocess.run([str(launcher), "chrome-extension://x/"], capture_output=True, text=True,
                                 env={"PATH": str(bin_dir)}).stdout  # only the fake commands
    assert run().startswith("host python: ")
    (bin_dir / "python3").unlink()
    assert run().strip() == \
        "flatpak: run --command=ldm-nmhost io.github.adiashishz.LDM chrome-extension://x/"


def test_autostart_goes_through_the_portal_in_a_flatpak(home, flatpak):
    assert integration.set_autostart(False)
    assert integration.CALLS == [("portal", False)]


def test_install_data_for_a_flatpak(tmp_path):
    if not (ROOT / "packaging/install-data.py").exists():
        pytest.skip("the packaging scripts are not next to the tests (distribution package build)")
    subprocess.run([sys.executable, str(ROOT / "packaging/install-data.py"), str(tmp_path), "--prefix", "/app",
                    "--flatpak"], check=True, capture_output=True)
    files = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert not any("systemd" in f or "autostart" in f or "native-messaging" in f for f in files)
    service = (tmp_path / "app/share/dbus-1/services/io.github.adiashishz.LDM.Service.service").read_text()
    assert "Exec=/app/bin/ldm daemon" in service and "SystemdService" not in service


def test_flatpak_offers_no_suspend_or_power_off(tmp_path, monkeypatch, flatpak):
    manager = Manager(Store(tmp_path / "state.db"))
    for action in ("suspend", "poweroff"):
        with pytest.raises(InvalidArgument, match="Flatpak"):
            manager.update_settings({"finish_action": action})
    manager.update_settings({"finish_action": "quit"})
    finished = []
    manager.on_all_finished = finished.append
    manager.store.set_setting("finish_action", "poweroff")  # e.g. restored from a backup
    manager._check_all_finished()
    assert finished == []


def test_outside_a_flatpak_suspend_stays(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "flatpak", lambda: False)
    Manager(Store(tmp_path / "state.db")).update_settings({"finish_action": "suspend"})
