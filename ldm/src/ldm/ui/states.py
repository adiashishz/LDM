"""Download states as the windows group them: list views, and which actions apply."""

from __future__ import annotations

from .. import sandbox

# List views: name -> states shown (None = all)
VIEWS = {
    "all": None,
    "downloading": {"running", "processing", "queued", "paused"},
    "finished": {"done", "seeding"},
    "problems": {"error", "expired", "changed"},
}

PAUSABLE = {"running", "processing", "queued", "seeding"}
RESUMABLE = {"paused", "error", "changed"}

# "When downloads finish" choices: (label, setting value)
FINISH_ACTIONS = [("Do Nothing", "nothing"), ("Quit LDM", "quit"), ("Suspend", "suspend"), ("Power Off", "poweroff")]
if sandbox.flatpak():  # a Flatpak can't suspend or power off the computer (no portal for it)
    FINISH_ACTIONS = FINISH_ACTIONS[:2]
