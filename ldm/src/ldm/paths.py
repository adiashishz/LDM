"""Where LDM keeps its files on each system.

             Linux (unchanged)                 Windows                     macOS
    data     $XDG_DATA_HOME/ldm                %LOCALAPPDATA%\\LDM          ~/Library/Application Support/LDM
    logs     $XDG_STATE_HOME/ldm               %LOCALAPPDATA%\\LDM\\Logs     ~/Library/Logs/LDM
    saves    $XDG_DOWNLOAD_DIR or ~/Downloads  the Downloads known folder  ~/Downloads
    backups  $XDG_DOCUMENTS_DIR or ~/Documents the Documents known folder  ~/Documents

XDG_DOWNLOAD_DIR and XDG_DOCUMENTS_DIR are honoured everywhere (the tests set them).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"

# Windows' known folders (KNOWNFOLDERID); Downloads and Documents can be moved, e.g. to another drive.
DOWNLOADS_ID = "374DE290-123F-4565-9164-39C4925E467B"
DOCUMENTS_ID = "FDD39AD0-238F-46AF-ADB4-6C85480369C7"


def data_dir() -> Path:
    """The database, saved passwords and the user's own yt-dlp."""
    if WINDOWS:
        return _local_app_data() / "LDM"
    if MACOS:
        return Path.home() / "Library" / "Application Support" / "LDM"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "ldm"


def log_dir() -> Path:
    """The service's log when the window starts it."""
    if WINDOWS:
        return _local_app_data() / "LDM" / "Logs"
    if MACOS:
        return Path.home() / "Library" / "Logs" / "LDM"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "ldm"


def download_dir() -> Path:
    if os.environ.get("XDG_DOWNLOAD_DIR"):
        return Path(os.environ["XDG_DOWNLOAD_DIR"])
    return (_known_folder(DOWNLOADS_ID) if WINDOWS else None) or Path.home() / "Downloads"


def documents_dir() -> Path:
    if os.environ.get("XDG_DOCUMENTS_DIR"):
        return Path(os.environ["XDG_DOCUMENTS_DIR"])
    return (_known_folder(DOCUMENTS_ID) if WINDOWS else None) or Path.home() / "Documents"


def _local_app_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def _known_folder(folder_id: str) -> Path | None:
    """A Windows known folder's current place (SHGetKnownFolderPath), or None."""
    import ctypes
    import uuid
    from ctypes import wintypes

    try:
        shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
    except (AttributeError, OSError):
        return None
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.c_char_p, wintypes.DWORD, wintypes.HANDLE,
                                             ctypes.POINTER(ctypes.c_wchar_p)]
    path = ctypes.c_wchar_p()
    if shell32.SHGetKnownFolderPath(uuid.UUID(folder_id).bytes_le, 0, None, ctypes.byref(path)) != 0:
        return None
    try:
        return Path(path.value) if path.value else None
    finally:
        ole32.CoTaskMemFree(path)
