"""Where LDM keeps its files on each system, and file names and writes that also work on Windows."""

import os
from pathlib import Path

import pytest

from ldm import engine, paths
from ldm.engine import sanitize_filename, seek_write, windows_name
from ldm.files import check_name


def test_linux_folders_follow_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "WINDOWS", False)
    monkeypatch.setattr(paths, "MACOS", False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert paths.data_dir() == tmp_path / "data" / "ldm"
    assert paths.log_dir() == tmp_path / "state" / "ldm"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert paths.data_dir() == Path.home() / ".local/share" / "ldm"


def test_windows_and_macos_folders(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert paths.data_dir() == tmp_path / "Local" / "LDM"
    assert paths.log_dir() == tmp_path / "Local" / "LDM" / "Logs"
    monkeypatch.setattr(paths, "WINDOWS", False)
    monkeypatch.setattr(paths, "MACOS", True)
    assert paths.data_dir() == Path.home() / "Library" / "Application Support" / "LDM"
    assert paths.log_dir() == Path.home() / "Library" / "Logs" / "LDM"


def test_saves_and_backups_folders(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DOWNLOAD_DIR", str(tmp_path / "dl"))
    assert paths.download_dir() == tmp_path / "dl"  # honoured on every system (the tests rely on it)
    monkeypatch.delenv("XDG_DOWNLOAD_DIR")
    monkeypatch.delenv("XDG_DOCUMENTS_DIR")
    if paths.WINDOWS:  # the known folders, wherever the user moved them
        assert paths.download_dir().is_absolute() and paths.documents_dir().is_absolute()
    else:
        assert paths.download_dir() == Path.home() / "Downloads"
        assert paths.documents_dir() == Path.home() / "Documents"


def test_windows_names():
    assert windows_name('a<b>c:d"e\\f|g?h*i.txt') == "a_b_c_d_e_f_g_h_i.txt"
    assert windows_name("report. ") == "report"
    assert windows_name("tab\there") == "tab_here"
    assert windows_name("CON") == "_CON" and windows_name("nul.txt") == "_nul.txt"
    assert windows_name("console.log") == "console.log"


def test_server_names_are_made_safe_for_windows_there(monkeypatch):
    monkeypatch.setattr(engine, "WINDOWS", True)
    assert sanitize_filename('Q3: "plan"?.pdf') == "Q3_ _plan__.pdf"
    monkeypatch.setattr(engine, "WINDOWS", False)
    assert sanitize_filename('Q3: "plan"?.pdf') == 'Q3: "plan"?.pdf'  # Linux allows these


def test_rename_refuses_names_windows_cannot_hold(monkeypatch):
    import ldm.files

    monkeypatch.setattr(ldm.files, "WINDOWS", True)
    with pytest.raises(ValueError):
        check_name("a:b.txt")
    assert check_name("a b.txt") == "a b.txt"


def test_seek_write_puts_parts_in_place(tmp_path):
    """Windows' replacement for pwrite: parts written out of order land at their offsets, in binary."""
    path = tmp_path / "file.bin"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | engine.BINARY)
    try:
        engine._preallocate(fd, 8)
        seek_write(fd, b"\r\n\r\n", 4)
        seek_write(fd, b"ab\nc", 0)
    finally:
        os.close(fd)
    assert path.read_bytes() == b"ab\nc\r\n\r\n"


def test_the_installers_bundle(monkeypatch, tmp_path):
    """In the Windows installer's bundle, LDM's programs are .exe files side by side: the service is started
    as ldm-service.exe, and yt-dlp is the bundle's zip."""
    import subprocess
    import sys

    from ldm import winsys, ytdlp
    from ldm.ui import remote

    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "ldm-gui.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    assert paths.program("ldm-service") == tmp_path / "ldm-service.exe"
    assert winsys.service_command() == [str(tmp_path / "ldm-service.exe")]

    started = []

    class Process:
        def __init__(self, command, **options):
            started.append(command)

        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", Process)
    remote.start_daemon()  # the window starts the service
    assert started == [[str(tmp_path / "ldm-service.exe")]]

    assert ytdlp.bundled_copy() is None
    (tmp_path / "_internal" / "yt-dlp").mkdir(parents=True)
    (tmp_path / "_internal" / "yt-dlp" / "yt-dlp").write_bytes(b"zip")
    assert ytdlp.bundled_copy() == tmp_path / "_internal" / "yt-dlp" / "yt-dlp"
