"""The tray icon on Windows: progress in its tooltip, a menu (Show LDM, Pause All, Resume All, Quit LDM, and
during the "all downloads finished" countdown a Cancel), and the service's notifications.

What ldm.tray and ldm.notify do over D-Bus on Linux. The icon is pystray's, running its own thread with
the Win32 message loop; its menu actions are handed to the service's event loop. On Windows the icon stays
while the service runs (the "tray_icon" setting hides it): a service without a window would otherwise be
invisible and couldn't be quit. Notifications come from the icon, so they need it shown, and have no
buttons (pystray can't add them); clicking the icon opens LDM.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .format import NOTICE_STATES, notice_text, summary_text

log = logging.getLogger(__name__)

ICON = Path(__file__).parent / "resources" / "io.github.adiashishz.LDM.png"
UPDATE_INTERVAL = 2.0


@dataclass
class Item:
    """A menu entry (a separator without an action)."""

    label: str = ""
    action: Callable[[], None] | None = None
    default: bool = False                           # what clicking the icon does
    visible: Callable[[], bool] = lambda: True


def pystray_icon(title: str, items: list[Item]) -> Any:
    """The real icon (Windows): a pystray.Icon, not yet running."""
    import pystray
    from PIL import Image

    def entry(item: Item):
        if item.action is None:
            return pystray.Menu.SEPARATOR
        return pystray.MenuItem(item.label, lambda icon, menu_item: item.action(), default=item.default,
                                visible=lambda menu_item: item.visible())

    with Image.open(ICON) as file:
        image = file.copy()  # loaded now: Pillow's lazy loading isn't safe across the icon's threads
    return pystray.Icon("LDM", image, title, pystray.Menu(*(entry(i) for i in items)))


class WindowsTray:
    def __init__(self, summary: Callable[[], tuple[int, int, int, float]], show: Callable[[], object],
                 pause_all: Callable[[], object], resume_all: Callable[[], object], quit: Callable[[], None],
                 enabled: Callable[[], bool] = lambda: True,
                 make_icon: Callable[[str, list[Item]], Any] = pystray_icon):
        self.summary, self.enabled, self.make_icon = summary, enabled, make_icon
        self.on_cancel: Callable[[], None] | None = None  # cancels the countdown (set by the service)
        self.counting_down = ""                           # "Powering off in 60 seconds" while it runs
        self.items = [
            Item("Show LDM", lambda: self._in_loop(show), default=True),
            Item(),
            Item("Pause All", lambda: self._in_loop(pause_all)),
            Item("Resume All", lambda: self._in_loop(resume_all)),
            Item("Cancel", lambda: self._in_loop(self._cancel), visible=lambda: bool(self.counting_down)),
            Item(),
            Item("Quit LDM", lambda: self._in_loop(quit)),
        ]
        self.icon: Any = None
        self.text = summary_text(0, 0, 0, 0.0)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()  # the icon's thread has shown it: from then on update() may change it

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            self.icon = self.make_icon(f"LDM — {self.text}", self.items)
        except Exception as e:  # no pystray or Pillow, no taskbar (a service session): run without the icon
            log.warning("no tray icon: %s", e)
            return
        shown = self.enabled()  # read here: the settings live in the service's thread (SQLite)
        self._thread = threading.Thread(target=self._run, args=(shown,), name="ldm-tray", daemon=True)
        self._thread.start()
        self._task = self._loop.create_task(self._keep_updated())

    def _run(self, shown: bool) -> None:
        """The icon's own thread: only the icon's work here, nothing of the service's."""
        def setup(icon) -> None:
            icon.visible = shown
            self._ready.set()

        try:
            self.icon.run(setup=setup)
        except Exception as e:
            log.warning("the tray icon stopped: %s", e)

    def close(self) -> None:
        if self._task:
            self._task.cancel()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception as e:
                log.info("could not remove the tray icon: %s", e)
        if self._thread is not None:
            self._thread.join(5)

    def _in_loop(self, action: Callable[[], object]) -> None:
        """Menu actions come from the icon's thread: run them in the service's loop."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(action)

    async def _keep_updated(self) -> None:
        while True:
            self.update()
            await asyncio.sleep(UPDATE_INTERVAL)

    def update(self) -> None:
        if self.icon is None or not self._ready.is_set():  # (showing it from two threads at once fails)
            return
        enabled = self.enabled()
        if bool(self.icon.visible) != enabled:
            self.icon.visible = enabled
        text = summary_text(*self.summary())
        if text != self.text:
            self.text = text
            self.icon.title = f"LDM — {text}"[:127]  # Windows' limit for a tooltip

    # --- notifications and the countdown ---------------------------------------------------------------

    def notify(self, title: str, text: str) -> None:
        if self.icon is None or not self.icon.visible:
            return
        try:
            self.icon.notify(text, title)
        except Exception as e:
            log.info("could not show a notification: %s", e)

    def countdown(self, text: str | None) -> None:
        """finish.LocalFinisher's announcement: "Powering off in 60 seconds" (Cancel appears in the menu), or
        None when the countdown ended."""
        self.counting_down = text or ""
        if self.icon is None:
            return
        try:
            self.icon.update_menu()
            if text:
                self.icon.notify(f"{text}. To cancel, choose Cancel in LDM's tray menu.", "All downloads finished")
            else:
                self.icon.remove_notification()
        except Exception as e:
            log.info("could not show the countdown: %s", e)

    def _cancel(self) -> None:
        if self.on_cancel is not None:
            self.on_cancel()


def notify_on_state(manager, tray: WindowsTray) -> None:
    """Finished, failed and expired downloads as notifications, while the window is open too (Windows: the
    service is LDM's only way to show one). The "notify_when_closed" setting turns them off."""
    notified: set[int] = set()  # torrents announced when seeding started
    previous = manager.on_state

    def on_state(download_id: int, state: str, error: str) -> None:
        if previous:
            previous(download_id, state, error)
        if state not in NOTICE_STATES or (state == "done" and download_id in notified):
            return
        if state == "seeding":
            notified.add(download_id)
        if not manager.store.get_settings()["notify_when_closed"]:
            return
        try:
            d = manager.store.get(download_id)
        except KeyError:  # removed meanwhile
            return
        tray.notify(*notice_text(state, d.name or d.url, d.size, error))

    manager.on_state = on_state
