"""The service on Windows: tray icon, notifications, battery, keeping the computer awake, the "all finished"
countdown, Credential Manager and starting at login.

The logic runs everywhere with fakes; the tests marked windows_only make the real Win32 calls (Windows CI)."""

import asyncio
import subprocess
import sys
import threading
import uuid

import pytest

from ldm import winsys
from ldm.finish import COUNTDOWN_TEXT, LocalFinisher
from ldm.format import notice_text, summary_text
from ldm.manager import Manager
from ldm.passwords import MemoryPasswords
from ldm.store import Store
from ldm.winsys import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, KeepAwake, PowerMonitor
from ldm.wintray import WindowsTray, notify_on_state

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


class FakeIcon:
    """What WindowsTray uses of a pystray.Icon."""

    def __init__(self, title, items):
        self.title, self.items = title, items
        self.visible = False
        self.notes, self.menu_updates, self.removed = [], 0, 0
        self.running = False

    def run(self, setup):
        self.running = True
        setup(self)

    def stop(self):
        self.running = False

    def notify(self, text, title):
        self.notes.append((title, text))

    def update_menu(self):
        self.menu_updates += 1

    def remove_notification(self):
        self.removed += 1

    def item(self, label):
        return next(i for i in self.items if i.label == label)


async def until(condition, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


def test_texts():
    assert summary_text(0, 0, 0, 0.0) == "No downloads running"
    assert summary_text(3, 450, 1000, 2 * 1024 * 1024) == "3 downloading · 45% · 2.0 MiB/s"
    assert notice_text("done", "a.iso", 1024) == ("Download finished", "a.iso — 1.0 KiB")
    assert notice_text("error", "a.iso", None, "404") == ("Download failed", "a.iso: 404")
    assert notice_text("expired", "a.iso", None) == ("Link expired", "a.iso: set a new link in LDM")


def test_keep_awake_asks_only_on_changes_and_keeps_state_when_windows_refuses():
    calls, answer = [], [True]

    def call(flags):
        calls.append(flags)
        return answer[0]

    awake = KeepAwake(call)
    awake.set_active(True)
    awake.set_active(True)
    awake.close()
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS] and not awake.active
    answer[0] = False
    awake.set_active(True)
    assert not awake.active  # refused: asked again next time


def test_power_monitor_reports_battery_changes():
    readings = [(False, 100.0)]
    changes = []

    def read():
        if readings[0] is None:
            raise OSError("no power status")
        return readings[0]

    async def scenario():
        monitor = PowerMonitor(lambda: changes.append((monitor.on_battery, monitor.battery)), read, interval=0.02)
        await monitor.start()
        readings[0] = (True, 80.0)
        await until(lambda: changes)
        readings[0] = None  # unreadable: the last state stays
        await asyncio.sleep(0.05)
        await monitor.close()
        return monitor

    monitor = asyncio.run(scenario())
    assert changes == [(True, 80.0)] and monitor.on_battery and not monitor.metered


def test_tray_tooltip_menu_and_countdown():
    progress = [(0, 0, 0, 0.0)]
    enabled = [True]
    actions = []
    threads = set()

    def tray_enabled():  # reads the settings database: only in the service's thread
        threads.add(threading.get_ident())
        return enabled[0]

    async def scenario():
        tray = WindowsTray(lambda: progress[0], lambda: actions.append("show"), lambda: actions.append("pause"),
                           lambda: actions.append("resume"), lambda: actions.append("quit"),
                           enabled=tray_enabled, make_icon=FakeIcon)
        tray.on_cancel = lambda: actions.append("cancel")
        await tray.start()
        icon = tray.icon
        await until(lambda: icon.running)
        assert icon.visible and icon.title == "LDM — No downloads running"
        assert icon.item("Show LDM").default and not icon.item("Cancel").visible()

        progress[0] = (2, 500, 1000, 1024.0)
        tray.update()
        assert icon.title == "LDM — 2 downloading · 50% · 1.0 KiB/s"
        enabled[0] = False  # the "tray_icon" setting
        tray.update()
        assert not icon.visible
        tray.notify("Download finished", "a.iso")  # hidden icon: no notification
        enabled[0] = True
        tray.update()

        # Menu actions come from the icon's own thread and run in the service's loop.
        for label in ("Show LDM", "Pause All", "Resume All", "Quit LDM"):
            await asyncio.to_thread(icon.item(label).action)
        await until(lambda: len(actions) == 4)

        tray.countdown("Powering off in 60 seconds")
        assert icon.item("Cancel").visible() and icon.menu_updates == 1
        await asyncio.to_thread(icon.item("Cancel").action)
        await until(lambda: "cancel" in actions)
        tray.countdown(None)
        assert not icon.item("Cancel").visible() and icon.removed == 1
        tray.notify("Download finished", "a.iso")
        tray.close()
        return icon

    icon = asyncio.run(scenario())
    assert actions == ["show", "pause", "resume", "quit", "cancel"]
    assert threads == {threading.get_ident()}
    assert icon.notes == [("All downloads finished",
                           "Powering off in 60 seconds. To cancel, choose Cancel in LDM's tray menu."),
                          ("Download finished", "a.iso")]
    assert not icon.running


def test_tray_leaves_the_icon_alone_until_its_thread_has_shown_it():
    """Showing the icon from the service's update and from the icon's own start at once breaks it."""
    release = threading.Event()

    class SlowIcon(FakeIcon):
        def run(self, setup):
            release.wait(5)
            super().run(setup)

    async def scenario():
        tray = WindowsTray(lambda: (1, 0, 0, 1.0), print, print, print, print, make_icon=SlowIcon)
        await tray.start()
        tray.update()
        before = (tray.icon.visible, tray.icon.title)
        release.set()
        await until(lambda: tray.icon.running)
        tray.update()
        after = (tray.icon.visible, tray.icon.title)
        tray.close()
        return before, after

    before, after = asyncio.run(scenario())
    assert before == (False, "LDM — No downloads running")
    assert after == (True, "LDM — 1 downloading · 1 B/s")


def test_tray_without_an_icon_still_runs():
    def no_icon(title, items):
        raise OSError("no taskbar")

    async def scenario():
        tray = WindowsTray(lambda: (0, 0, 0, 0.0), print, print, print, print, make_icon=no_icon)
        await tray.start()
        tray.update()
        tray.notify("a", "b")
        tray.countdown("Suspending in 60 seconds")
        tray.close()

    asyncio.run(scenario())


def test_notifications_for_finished_failed_and_expired(tmp_path):
    class Tray:
        notes = []

        def notify(self, title, text):
            self.notes.append((title, text))

    manager = Manager(Store(tmp_path / "state.db"), passwords=MemoryPasswords())
    first = manager.store.add("https://example.com/a.iso", tmp_path, name="a.iso")
    torrent = manager.store.add("magnet:?xt=urn:btih:abc", tmp_path, name="linux.iso", kind="torrent")
    seen = []
    manager.on_state = lambda *args: seen.append(args)  # another listener stays
    tray = Tray()
    notify_on_state(manager, tray)

    manager.on_state(first, "running", "")
    manager.on_state(first, "error", "404 Not Found")
    manager.on_state(torrent, "seeding", "")
    manager.on_state(torrent, "done", "")  # announced already when seeding started
    manager.on_state(99, "done", "")       # removed meanwhile
    manager.store.set_setting("notify_when_closed", False)
    manager.on_state(first, "expired", "")
    assert tray.notes == [("Download failed", "a.iso: 404 Not Found"), ("Download finished", "linux.iso — 0 B")]
    assert len(seen) == 6


def test_countdown_is_announced_and_cleared():
    async def scenario():
        said, done = [], []

        async def power(action):
            done.append(action)

        finisher = LocalFinisher(lambda: None, lambda: None, power, countdown=0.05, announce=said.append)
        finisher.run("suspend")
        await asyncio.sleep(0.01)
        finisher.cancel()
        await asyncio.sleep(0.01)
        finisher.run("poweroff")
        await asyncio.sleep(0.1)
        return said, done

    said, done = asyncio.run(scenario())
    sleeping = COUNTDOWN_TEXT["suspend"]  # "Going to sleep" on Windows
    assert said == [f"{sleeping} in 0 seconds", None, "Powering off in 0 seconds", None]  # (0.05 s here)
    assert done == ["poweroff"]


def test_service_command_has_no_console_window():
    command = winsys.service_command()
    assert command[1:] == ["-m", "ldm", "daemon"]
    if sys.platform == "win32":
        assert command[0].lower().endswith("pythonw.exe")


# --- the real calls ----------------------------------------------------------------------------------

@windows_only
def test_real_power_status_and_keep_awake():
    on_battery, battery = winsys.power_status()
    assert isinstance(on_battery, bool) and 0 <= battery <= 100
    awake = KeepAwake()
    awake.set_active(True)
    assert awake.active
    awake.close()
    assert not awake.active


@windows_only
def test_credential_manager_round_trip():
    store = winsys.CredentialPasswords(prefix=f"LDM-test-{uuid.uuid4().hex}")
    assert store.get("login", "example.com") is None
    store.set("login", "example.com", "pä55wörd")
    store.set("login", "example.com", "second")  # replaces
    assert store.get("login", "example.com") == "second"
    store.delete("login", "example.com")
    store.delete("login", "example.com")  # gone already: fine
    assert store.get("login", "example.com") is None


@windows_only
def test_passwords_go_to_credential_manager(tmp_path):
    from ldm.passwords import open_store

    assert isinstance(open_store(tmp_path / "passwords.json"), winsys.CredentialPasswords)


@windows_only
def test_autostart_run_key():
    import winreg

    test_key = rf"Software\LDM-tests-{uuid.uuid4().hex}"
    key = rf"{test_key}\Run"
    try:
        assert winsys.set_autostart(True, key)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as run:
            value, kind = winreg.QueryValueEx(run, winsys.RUN_VALUE)
        assert kind == winreg.REG_SZ and value == subprocess.list2cmdline(winsys.service_command())
        assert winsys.set_autostart(False, key) and winsys.set_autostart(False, key)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as run, pytest.raises(FileNotFoundError):
            winreg.QueryValueEx(run, winsys.RUN_VALUE)
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, test_key)


@windows_only
def test_the_real_icon_and_menu_build():
    from ldm.wintray import pystray_icon

    tray = WindowsTray(lambda: (0, 0, 0, 0.0), print, print, print, print)
    icon = pystray_icon("LDM", tray.items)
    labels = [str(item) for item in icon.menu.items]
    assert "Show LDM" in labels and "Quit LDM" in labels


@windows_only
def test_service_starts_and_stops_with_its_tray(tmp_path):
    """The real wiring: battery, keep-awake, tray icon and notifications start with the service and end
    with it (on a runner without a taskbar the icon may not show; the service runs anyway)."""
    from ldm.daemon import serve

    async def scenario():
        stop, ready = asyncio.Event(), asyncio.Event()
        service = asyncio.create_task(serve(db_path=tmp_path / "state.db", stop=stop, ready=ready,
                                            manager_options={"passwords": MemoryPasswords()},
                                            manage_autostart=False, dbus=False))
        await asyncio.wait_for(ready.wait(), 20)
        stop.set()
        return await asyncio.wait_for(service, 20)

    assert asyncio.run(scenario()) is False
