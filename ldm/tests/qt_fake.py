"""A stand-in for the Qt service client: answers at once from canned data and records every call."""

from PySide6.QtCore import QObject, Signal

from ldm.store import DEFAULT_SETTINGS
from ldm.ui.remote import ServiceCalls


class FakeClient(QObject, ServiceCalls):
    added = Signal("qulonglong")
    removed = Signal("qulonglong")
    state_changed = Signal("qulonglong", str, str)
    progress = Signal(object)
    connected_changed = Signal(bool)
    connected = True

    def __init__(self, rows, **answers):
        super().__init__()
        self.rows, self.calls = rows, []
        self.settings = {**DEFAULT_SETTINGS, "download_dir": "/tmp", "clipboard_watch": False}
        self.answers = answers

    def _call(self, method, signature, args, callback, timeout=None):
        self.calls.append((method, args))
        answers = {"List": self.rows, "GetSettings": self.settings, "FindDuplicates": [], "Add": 99, "GetParts": [],
                   "ListHostRules": [], "ListLogins": [],
                   "Get": next((r for r in self.rows if args and r["id"] == args[0]), None), **self.answers}
        if method == "SetSettings":
            self.settings.update({k: v.value for k, v in args[0].items()})
        if callback:
            callback(answers.get(method), None)

    def called(self, method):
        return [args for m, args in self.calls if m == method]

    def close(self):
        pass
