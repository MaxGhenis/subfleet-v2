"""Spellings of one UUID, for the tests of C-26.3's "whichever case a UUID is
spelled in": a store written before native ids were canonical keeps an upper-case
UUID, Claude Code names a session in lower case, and a person may type either."""

from __future__ import annotations

import uuid

from hypothesis import strategies as st


def spell(value: str, mask: int) -> str:
    """`value` with each character whose bit is set in `mask` in upper case, the rest in lower."""
    return "".join(c.upper() if (mask >> i) & 1 else c.lower() for i, c in enumerate(value))


#: Any casing of any UUID's canonical (hyphenated) form.
masks = st.integers(min_value=0, max_value=(1 << 36) - 1)
uuids = st.uuids().map(str)

#: Named directions every spelling test covers besides the generated ones: the
#: stored id in upper case and the other in lower, the reverse, and two mixed.
DIRECTIONS = {"upper-stored": (lambda u: u.upper(), lambda u: u.lower()),
              "lower-stored": (lambda u: u.lower(), lambda u: u.upper()),
              "mixed": (lambda u: spell(u, 0x5A5A5A5A5), lambda u: spell(u, 0xA5A5A5A5A))}


def other_uuid(value: str) -> str:
    """A different UUID from `value`, in the same case."""
    flipped = str(uuid.UUID(int=uuid.UUID(value).int ^ 1))
    return flipped.upper() if value.isupper() else flipped
