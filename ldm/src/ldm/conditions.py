"""What the scheduler asks of the computer: a metered network, running on battery and its charge.

`Conditions` holds the current state; each system's monitor keeps it up to date (Linux: ldm.system). Without
one, the network counts as unmetered and the computer as on mains power, so downloads aren't held back.
"""

from __future__ import annotations


class Conditions:
    """Current state; also the fallback when the system's services are unavailable."""

    metered = False
    on_battery = False
    battery = 100.0

    async def close(self) -> None:
        """Stop watching (the system's monitors)."""
