"""Readable navigation labels without changing source text or locator identity."""
from __future__ import annotations

import re


# Common Wingdings/Symbol list bullets embedded in PDF bookmarks. These private
# glyphs have no portable font mapping; do not strip arbitrary private-use text
# or meaningful mathematical symbols elsewhere in a heading.
_BULLET_PREFIX = re.compile(r"^(?:[\uf06e\uf0a7\uf0b7]\s*)+")


def clean_outline_title(title: str) -> str:
    return _BULLET_PREFIX.sub("", " ".join(title.split())).strip()
