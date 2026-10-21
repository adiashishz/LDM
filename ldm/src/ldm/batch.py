"""Batch download (1DM): one `*` in a link stands for a range of numbers or letters.

    expand("https://site/img*.jpg", numbers=(1, 3, 2)) -> [.../img01.jpg, .../img02.jpg, .../img03.jpg]
    expand("https://site/part-*.zip", letters=("a", "c")) -> [.../part-a.zip, .../part-b.zip, .../part-c.zip]
"""

from __future__ import annotations

import string

MAX_LINKS = 10_000
MAX_PAD = 10


def expand(url: str, numbers: tuple[int, int, int] | None = None, letters: tuple[str, str] | None = None) -> list[str]:
    """`numbers` = (first, last, zero-pad width; 0 = none); `letters` = (first, last), same case."""
    url = url.strip()
    if url.count("*") != 1:
        raise ValueError("the link needs exactly one * where the number or letter goes" if "*" not in url
                         else "use only one * in the link")
    if (numbers is None) == (letters is None):
        raise ValueError("choose numbers or letters")
    if numbers is not None:
        first, last, pad = numbers
        if not 0 <= pad <= MAX_PAD:
            raise ValueError(f"zero padding can't be longer than {MAX_PAD}")
        if first < 0 or last < 0:
            raise ValueError("numbers can't be negative")
        step = 1 if last >= first else -1
        if abs(last - first) + 1 > MAX_LINKS:
            raise ValueError(f"at most {MAX_LINKS} links at once")
        values = [str(n).zfill(pad) for n in range(first, last + step, step)]
    else:
        first, last = letters
        alphabet = next((a for a in (string.ascii_lowercase, string.ascii_uppercase) if first in a), None)
        if len(first) != 1 or len(last) != 1 or alphabet is None or last not in alphabet:
            raise ValueError("letters must be single letters of the same case, e.g. a to z")
        start, stop = alphabet.index(first), alphabet.index(last)
        step = 1 if stop >= start else -1
        values = [alphabet[i] for i in range(start, stop + step, step)]
    return [url.replace("*", value) for value in values]


def parse_link_list(text: str) -> list[str]:
    """Links from a text file: one per line; blank lines, `#` comments and repeats are skipped."""
    seen: dict[str, None] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line.split(":", 1)[0].lower() in ("http", "https", "ftp", "magnet"):
            seen.setdefault(line, None)
    return list(seen)
