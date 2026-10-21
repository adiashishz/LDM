"""Token-bucket speed limiter shared by any number of connections."""

from __future__ import annotations

import asyncio
import time
from typing import Callable

MIN_BURST = 64 * 1024


class TokenBucket:
    """Limits throughput to `rate` bytes/s (0 = unlimited). The rate can change at any time.

    Consumers take bytes after receiving them and sleep off any debt, so several connections
    sharing one bucket add up to the configured rate.
    """

    def __init__(self, rate: float = 0, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._rate = rate
        self._tokens = 0.0
        self._last = clock()

    @property
    def rate(self) -> float:
        return self._rate

    def set_rate(self, rate: float) -> None:
        self._refill()
        self._rate = rate
        self._tokens = min(self._tokens, self._burst)

    @property
    def _burst(self) -> float:
        return max(self._rate * 0.25, MIN_BURST)

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
        self._last = now

    async def consume(self, n: int) -> None:
        if self._rate <= 0:
            return
        self._refill()
        self._tokens -= n
        if self._tokens < 0:
            await asyncio.sleep(-self._tokens / self._rate)
