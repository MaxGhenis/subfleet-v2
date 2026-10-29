"""C-23.14 (#239 round 8): `subfleet/sessions/scrub_release.py` is release/217's scrubbing half
at `2c981cba5`, kept verbatim but for its imports, as `scrub_473fe2941` keeps 473fe2941's:
`handoff.py`'s lines 59-152 and 236-1568 there (blob `f78955e05`) and `redact.py`'s lines
121-160 (blob `bde628b71`). Every mode runs it first (`test_scrub_release_first`). Its digest
is pinned here, and where the repository holds those blobs (a full clone; CI's checkout is
one commit), the kept lines are read from them too. A one-rule edit of the copy fails both."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from subfleet.sessions import scrub_release

REPO = Path(__file__).resolve().parents[2]
#: The module as imported, so a run against another tree reads that tree's copy.
FROZEN = Path(scrub_release.__file__)

#: Release/217's two files at `2c981cba5`, and what the frozen copy keeps of each.
HANDOFF_BLOB = "f78955e05a90300fa3414394b3548020739cbaa4"
REDACT_BLOB = "bde628b712e849ffe8621f8837e8e9c70f43ab46"
HANDOFF_KEPT = ((59, 152), (236, 1568))
REDACT_KEPT = ((121, 160),)
#: The sha256 of the kept lines: handoff's two ranges (each joined by line breaks, a blank
#: line between them), then redact's, each range ending in a line break.
KEPT_SHA256 = "f97717c6676a6f822ed5f1584ed8aadd8e34fafd7a030082752c7b48b6e613fd"
#: The one import line of `mask_approval` the copy changes, as release has it.
RELEASE_MASK_IMPORT = ("    from ..sessions.handoff import _BEARER_RE, _JWT_RE, _PEM_RE, _PREFIXED_TOKEN_RE, "
                       "_URL_PASSWORD_RE, _linear_sub\n")
FROZEN_MASK_IMPORT = ("    from subfleet.sessions.scrub_release import (_BEARER_RE, _JWT_RE, _PEM_RE, _PREFIXED_TOKEN_RE,\n"
                      "                                                 _URL_PASSWORD_RE, _linear_sub)\n")
REDACT_SEPARATOR = "\n\n# --- subfleet/conversations/redact.py at 2c981cba5, lines 121-160 ---------------------------\n\n"


def _kept_of_frozen() -> str:
    """The frozen copy's kept lines, as release has them: its body after the imports, the
    separator before `redact.py`'s lines taken out and `mask_approval`'s import restored."""
    text = FROZEN.read_text()
    body = text[text.index('REDACTED = "[REDACTED]"\n'):]
    scrub, separator, mask = body.partition(REDACT_SEPARATOR)
    assert separator and FROZEN_MASK_IMPORT in mask
    return scrub + mask.replace(FROZEN_MASK_IMPORT, RELEASE_MASK_IMPORT, 1)


def _blob_lines(blob: str) -> list[str] | None:
    try:
        run = subprocess.run(["git", "cat-file", "blob", blob], cwd=REPO, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return run.stdout.split("\n") if run.returncode == 0 else None


def _ranges(lines: list[str], ranges) -> list[str]:
    return ["\n".join(lines[first - 1:last]) + "\n" for first, last in ranges]


def test_the_frozen_copy_is_release_s_code_verbatim():
    """Pinned by digest everywhere, and against the git objects where the clone holds them."""
    kept = _kept_of_frozen()
    assert hashlib.sha256(kept.encode()).hexdigest() == KEPT_SHA256
    handoff_lines, redact_lines = _blob_lines(HANDOFF_BLOB), _blob_lines(REDACT_BLOB)
    if handoff_lines is None or redact_lines is None:
        pytest.skip("release/217's blobs are not in this clone (CI checks out one commit); the digest holds")
    first, second = _ranges(handoff_lines, HANDOFF_KEPT)
    assert kept == first + "\n" + second + "".join(_ranges(redact_lines, REDACT_KEPT))


def test_the_frozen_copy_imports_nothing_of_the_head():
    """Only `re`, `bisect`, `typing`, `subfleet.contracts` and itself: no head rule reaches it."""
    imports = re.findall(r"(?m)^\s*(?:from (\S+) import|import (\S+))", FROZEN.read_text())
    assert {a or b for a, b in imports} == {"__future__", "bisect", "re", "typing", "subfleet.contracts",
                                            "hashlib", "subfleet.sessions.scrub_release"}
