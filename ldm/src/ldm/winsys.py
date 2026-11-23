"""Windows' side of the service: battery, keeping the computer awake, sleep and power off when all downloads
finished, passwords in Credential Manager, and starting at login.

What D-Bus gives LDM on Linux (UPower, logind, the Secret Service, systemd), through the Win32 API. Standard
library only (ctypes, winreg); every call is made when it's needed, so the module imports anywhere (tests).
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Callable

from .conditions import Conditions

log = logging.getLogger(__name__)

POLL_SECONDS = 30.0  # how often the battery is checked (the service has no window to get power events)


# --- battery -------------------------------------------------------------------------------------------

class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


NO_BATTERY, UNKNOWN = 128, 255


def power_status() -> tuple[bool, float]:
    """(on battery, charge in %). A computer without a battery, or one Windows can't read, is on mains power."""
    status = SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        raise ctypes.WinError()
    if status.BatteryFlag in (NO_BATTERY, UNKNOWN):
        return False, 100.0
    percent = status.BatteryLifePercent
    return status.ACLineStatus == 0, float(percent) if percent <= 100 else 100.0


class PowerMonitor(Conditions):
    """On battery and its charge, checked every POLL_SECONDS. Metered connections aren't detected on Windows
    yet: the network always counts as unmetered."""

    def __init__(self, on_change: Callable[[], None] | None = None,
                 read: Callable[[], tuple[bool, float]] = power_status, interval: float = POLL_SECONDS):
        self.on_change, self.read, self.interval = on_change, read, interval
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self.refresh()
        self._task = asyncio.get_running_loop().create_task(self._poll())

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def refresh(self) -> None:
        try:
            on_battery, battery = self.read()
        except OSError as e:
            log.info("can't read the battery: %s", e)
            return
        before = (self.on_battery, round(self.battery))
        self.on_battery, self.battery = on_battery, battery
        if self.on_change and before != (self.on_battery, round(self.battery)):
            self.on_change()

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            self.refresh()


# --- keeping the computer awake ------------------------------------------------------------------------

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def set_execution_state(flags: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
    kernel32.SetThreadExecutionState.restype = wintypes.DWORD
    return kernel32.SetThreadExecutionState(flags) != 0


class KeepAwake:
    """While downloads run, Windows doesn't go to sleep by itself (the screen may still turn off).

    The request belongs to the thread that makes it: the service's event loop thread, which calls this and
    lives as long as the service. Windows drops it when the service ends."""

    def __init__(self, call: Callable[[int], bool] = set_execution_state):
        self.call = call
        self.active = False

    def set_active(self, active: bool) -> None:
        if active == self.active:
            return
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if active else 0)
        try:
            ok = self.call(flags)
        except OSError as e:
            ok = False
            log.info("could not change sleep prevention: %s", e)
        if ok:
            self.active = active
            if active:
                log.info("preventing sleep while downloading")

    def close(self) -> None:
        self.set_active(False)


# --- sleep and power off -------------------------------------------------------------------------------

def suspend() -> None:
    """Sleep (not hibernate). On some "Modern Standby" computers Windows only turns the screen off."""
    powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
    powrprof.SetSuspendState.argtypes = [wintypes.BOOLEAN, wintypes.BOOLEAN, wintypes.BOOLEAN]
    powrprof.SetSuspendState.restype = wintypes.BOOLEAN
    if not powrprof.SetSuspendState(False, False, False):
        raise ctypes.WinError(ctypes.get_last_error())


def power_off() -> None:
    """Shut down now, as the Start menu does (programs get the usual chance to close)."""
    result = subprocess.run(["shutdown", "/s", "/t", "0"], capture_output=True, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise OSError(result.stderr.strip() or f"shutdown failed ({result.returncode})")


async def power(action: str) -> None:
    """finish.LocalFinisher's system call: "suspend" or "poweroff"."""
    await asyncio.to_thread(suspend if action == "suspend" else power_off)


# --- passwords -----------------------------------------------------------------------------------------

class CREDENTIAL(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2  # this user on this computer, kept across logins
ERROR_NOT_FOUND = 1168


def _advapi32():
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    for name in ("CredReadW", "CredWriteW", "CredDeleteW"):
        getattr(advapi32, name).restype = wintypes.BOOL
    return advapi32


class CredentialPasswords:
    """Windows Credential Manager ("Windows Credentials" → generic credentials named "LDM/login/<host>",
    "LDM/proxy/proxy"). Same interface as passwords.KeyringPasswords."""

    where = "credential manager"

    def __init__(self, prefix: str = "LDM"):
        self.prefix = prefix
        self.advapi32 = _advapi32()

    def target(self, kind: str, key: str) -> str:
        return f"{self.prefix}/{kind}/{key}"

    def get(self, kind: str, key: str) -> str | None:
        found = ctypes.POINTER(CREDENTIAL)()
        if not self.advapi32.CredReadW(self.target(kind, key), CRED_TYPE_GENERIC, 0, ctypes.byref(found)):
            error = ctypes.get_last_error()
            if error == ERROR_NOT_FOUND:
                return None
            raise ctypes.WinError(error)
        try:
            item = found.contents
            return ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize).decode()
        finally:
            self.advapi32.CredFree(found)

    def set(self, kind: str, key: str, password: str) -> None:
        secret = password.encode()
        blob = (ctypes.c_ubyte * len(secret)).from_buffer_copy(secret)
        item = CREDENTIAL(Type=CRED_TYPE_GENERIC, TargetName=self.target(kind, key), UserName=key,
                          CredentialBlobSize=len(secret), CredentialBlob=blob, Persist=CRED_PERSIST_LOCAL_MACHINE)
        if not self.advapi32.CredWriteW(ctypes.byref(item), 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def delete(self, kind: str, key: str) -> None:
        if not self.advapi32.CredDeleteW(self.target(kind, key), CRED_TYPE_GENERIC, 0):
            error = ctypes.get_last_error()
            if error != ERROR_NOT_FOUND:
                raise ctypes.WinError(error)


# --- starting at login, and the window -----------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "LDM"


def beside_python(name: str) -> Path | None:
    """`name`.exe of LDM's: next to the running Python (a venv's Scripts folder), or in the Scripts folder
    next to it (pip without a venv)."""
    folder = Path(sys.executable).parent
    for candidate in (folder / f"{name}.exe", folder / "Scripts" / f"{name}.exe"):
        if candidate.exists():
            return candidate
    return None


def _pythonw() -> str:
    """The Python without a console window, next to the running one (a venv's Scripts folder)."""
    folder = Path(sys.executable).parent
    windowless = folder / "pythonw.exe"
    return str(windowless if windowless.exists() else sys.executable)


def service_command() -> list[str]:
    """How Windows starts the service at login: without a console window (its log goes to LDM's log folder).
    The installer's bundle has `ldm-service.exe` for it."""
    from . import paths

    if paths.FROZEN:
        return [str(paths.program("ldm-service"))]
    return [_pythonw(), "-m", "ldm", "daemon"]


def gui_command() -> list[str]:
    """How the service starts LDM's window: LDM_GUI_COMMAND (installers), `ldm-gui.exe` beside Python, or
    the module itself."""
    from .nmhost import split_command

    if os.environ.get("LDM_GUI_COMMAND"):
        return split_command(os.environ["LDM_GUI_COMMAND"])
    gui = beside_python("ldm-gui")
    return [str(gui)] if gui else [_pythonw(), "-m", "ldm.qt"]


def open_window() -> None:
    from .ui.remote import detached

    try:
        subprocess.Popen(gui_command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **detached())
    except OSError as e:
        log.warning("could not start LDM's window: %s", e)


def set_autostart(enabled: bool, key: str = RUN_KEY) -> bool:
    """Start the service at login (the user's Run key), or stop doing so. Returns whether it worked."""
    import winreg

    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as run:
            if enabled:
                winreg.SetValueEx(run, RUN_VALUE, 0, winreg.REG_SZ, subprocess.list2cmdline(service_command()))
            else:
                try:
                    winreg.DeleteValue(run, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError as e:
        log.warning("could not change starting at login: %s", e)
        return False
