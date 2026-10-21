"""Byte-range parts of a download and how they are split."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Part:
    """Byte range [first, last] (inclusive) of the target file; `done` bytes from `first` are written."""

    first: int
    last: int
    done: int = 0
    active: bool = False  # runtime only: a connection is working on it

    @property
    def pos(self) -> int:
        return self.first + self.done

    @property
    def remaining(self) -> int:
        return self.last - self.pos + 1

    @property
    def complete(self) -> bool:
        return self.remaining <= 0


def split_even(size: int, count: int, min_part: int) -> list[Part]:
    """Split `size` bytes into up to `count` equal parts of at least `min_part` bytes.

    The last part takes the remainder (same scheme as 1DM's upstream library).
    """
    count = max(1, min(count, size // min_part or 1))
    step = size // count
    parts = []
    for i in range(count):
        first = i * step
        last = size - 1 if i == count - 1 else first + step - 1
        parts.append(Part(first, last))
    return parts


def steal(parts: list[Part], min_part: int) -> Part | None:
    """Dynamic segmentation: split the part with the most bytes left and return the new tail part.

    The victim keeps the first half of its remaining bytes (its connection keeps streaming and
    stops at the new `last`); the returned part covers the second half and is not yet active.
    Returns None when no part has at least `2 * min_part` bytes left.
    """
    candidates = [p for p in parts if p.remaining >= 2 * min_part]
    if not candidates:
        return None
    victim = max(candidates, key=lambda p: p.remaining)
    keep = victim.remaining // 2
    tail = Part(victim.pos + keep, victim.last)
    victim.last = victim.pos + keep - 1
    parts.append(tail)
    parts.sort(key=lambda p: p.first)
    return tail
