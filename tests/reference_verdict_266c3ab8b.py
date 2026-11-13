"""`subfleet/gate/verdict.py` as of 266c3ab8b (release/217), for the D-GATE-CERT1 differential.

The source is bundled byte for byte as `tests/fixtures/gates/verdict-266c3ab8b.py.txt`
and loaded from there, so no test needs git history. Its SHA-256, and the git blob id
`git rev-parse 266c3ab8b:subfleet/gate/verdict.py` printed when it was bundled, pin it
to that commit; importing this module refuses any other bytes. Its relative import
(`from .errors import GateError`) resolves to the head's `subfleet.gate.errors`, which
is unchanged since 266c3ab8b, so both parsers raise the same `GateError`.
"""
from __future__ import annotations

import hashlib
import types
from pathlib import Path

SOURCE = Path(__file__).parent / "fixtures" / "gates" / "verdict-266c3ab8b.py.txt"
SHA256 = "a466a5d72ff56d7d3d040f99d6ccec809fa8e270ce94eb094696d1984f160121"
GIT_BLOB = "7c88bd13a30be3266eb88d1fc49cc4914cc8effa"


def git_blob_id(body: bytes) -> str:
    """The id git gives these bytes as a blob (SHA-1 of the blob header and the bytes)."""
    return hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest()


def _load() -> types.ModuleType:
    body = SOURCE.read_bytes()
    if hashlib.sha256(body).hexdigest() != SHA256 or git_blob_id(body) != GIT_BLOB:
        raise AssertionError(f"{SOURCE} is not subfleet/gate/verdict.py at 266c3ab8b")
    module = types.ModuleType(__name__ + ".base")
    module.__package__ = "subfleet.gate"
    exec(compile(body.decode("utf-8"), str(SOURCE), "exec"), module.__dict__)
    return module


base = _load()
parse_verdict = base.parse_verdict
