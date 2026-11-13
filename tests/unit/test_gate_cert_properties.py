"""D-GATE-CERT1 (C-23.9): properties of what the gate accepts.

Invariants, for every peer output these strategies build:
1. accepted implies certifiable: whenever `parse_verdict` accepts a text, the
   verdict it returns is the payload as JSON decodes it, the certificate's re-wrap
   parses back to it, and for an approval `certificate()` succeeds with a
   `peer_verdict` equal to it;
2. no new acceptances: the head refuses every text that release/217's
   `parse_verdict` at 266c3ab8b refuses (bundled verbatim, `tests/reference_verdict_266c3ab8b.py`),
   and where both accept they return the same verdict;
4. determinism: the same text gives the same verdict and the same certificate bytes.

The texts mix verdict objects (nested values, finite, large and non-finite numbers,
strings holding either sentinel or its escapes, non-ASCII text) with the ways a peer
breaks the envelope, so both parsers accept some and refuse others
(`test_the_strategy_reaches_every_class` keeps that true).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import HealthCheck, find, given, settings
from hypothesis import strategies as st

from subfleet.gate.certificate import certificate
from subfleet.gate.errors import GateError
from subfleet.gate.verdict import VERDICT_BEGIN, VERDICT_END, escape_sentinels, parse_verdict, wrap_verdict
from tests import reference_verdict_266c3ab8b as base

REVISION = {"kind": "plan", "sha256": "a" * 64, "bytes": 8}
FINDING_LOCATION = "plan:1"
# Nesting is drawn this deep at most: past the gate's fixed bound (32) once #178's re-ask is ported.
DEEPEST = 40
SETTINGS = settings(max_examples=500, deadline=None, database=None,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


@dataclass(frozen=True)
class Raw:
    """JSON text written as is: a literal json.dumps cannot write (1e1000) or will not (NaN)."""
    text: str


@dataclass(frozen=True)
class Str:
    """A JSON string and how the peer wrote it."""
    value: str
    style: str


def render_str(item: Str) -> str:
    if item.style == "plain":                     # UTF-8 as is: a sentinel in it stays literal
        return json.dumps(item.value, ensure_ascii=False)
    text = json.dumps(item.value)                 # non-ASCII as \uXXXX
    if item.style == "escaped":                   # each sentinel's first hyphen as -
        return escape_sentinels(text)
    if item.style == "hyphens":                   # every hyphen as -
        return text.replace("-", "\\u002d")
    return text


def render(value: Any, separators: tuple[str, str]) -> str:
    items, keys = separators
    if isinstance(value, Raw):
        return value.text
    if isinstance(value, Str):
        return render_str(value)
    if isinstance(value, dict):
        return "{" + items.join(render(key, separators) + keys + render(item, separators)
                                for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + items.join(render(item, separators) for item in value) + "]"
    return json.dumps(value)


FRAGMENTS = [VERDICT_BEGIN, VERDICT_END, VERDICT_BEGIN + VERDICT_BEGIN[3:], VERDICT_END[:-1], "-", "---",
             "--SUBFLEET-VERDICT-", "\\u002d", "\\", '"', "é", " ", "\ud800", "\U0001f600", "中"]
STYLES = ["plain", "ascii", "escaped", "escaped", "hyphens"]
LITERALS = ["1e1000", "-1e400", "2e308", "1.7976931348623157e308", "5e-324", "1e-1000", "-0.0", "1E2",
            "NaN", "Infinity", "-Infinity", "1" + "0" * 400, "1" + "0" * 400 + ".0"]

texts_in_strings = st.lists(st.one_of(st.sampled_from(FRAGMENTS), st.text(max_size=6)), max_size=4).map("".join)
strs = st.builds(Str, texts_in_strings, st.sampled_from(STYLES))
numbers = st.one_of(st.integers(min_value=-10**30, max_value=10**30),
                    st.floats(allow_nan=False, allow_infinity=False),
                    st.sampled_from(LITERALS).map(Raw))
leaves = st.one_of(st.none(), st.booleans(), numbers, strs)


def nested(depth: int, leaf: Any) -> Any:
    for _ in range(depth):
        leaf = [leaf]
    return leaf


values = st.recursive(leaves, lambda children: st.one_of(
    st.lists(children, max_size=3),
    st.dictionaries(st.one_of(strs, texts_in_strings), children, max_size=3)), max_leaves=10)
members = st.one_of(values, values, st.builds(nested, st.integers(min_value=1, max_value=DEEPEST), leaves))


def prefixed(prefix: str):
    return st.builds(lambda item: Str(prefix + item.value, item.style), strs)


@st.composite
def verdict_objects(draw) -> dict:
    """Mostly a valid approval; sometimes another verdict or a member the rules refuse."""
    verdict = draw(st.sampled_from(["approve"] * 5 + ["changes_requested", "blocked"]))
    finding = {"severity": draw(prefixed("high")), "location": FINDING_LOCATION,
               "description": draw(prefixed("Add rollback "))}
    value: dict = {"schema_version": 1, "artifact_revision": dict(REVISION), "verdict": verdict,
                   "summary": draw(st.one_of(st.just("Reviewed."), prefixed("Reviewed "))),
                   "findings": [finding] if verdict == "changes_requested" else [], "notes": []}
    for key, item in draw(st.dictionaries(st.one_of(st.sampled_from(["extra", "weight", "Notes"]), strs),
                                          members, max_size=2)).items():
        value[key] = item
    change = draw(st.integers(min_value=0, max_value=11))
    if change == 0:
        value["artifact_revision"]["bytes"] = Raw(draw(st.sampled_from(["9", "8.0", "8e0", "8e400", "NaN"])))
    elif change == 1:
        value["schema_version"] = Raw(draw(st.sampled_from(["1.0", "1e1000", "2", "true"])))
    elif change == 2:
        value["findings" if verdict == "approve" else "notes"] = [finding] if verdict == "approve" else [draw(strs)]
    elif change == 3:
        value["summary"] = draw(st.sampled_from([Str(" ", "ascii"), Raw("1e1000"), Raw("null")]))
    elif change == 4:
        value["artifact_revision"][draw(strs)] = draw(members)
    return value


@st.composite
def peer_outputs(draw) -> str:
    payload = render(draw(verdict_objects()), draw(st.sampled_from([(",", ":"), (", ", ": "), (",\n  ", ": ")])))
    block = f"{VERDICT_BEGIN}\n{payload}\n{VERDICT_END}"
    shape = draw(st.sampled_from(["block"] * 6 + ["spaces", "prefix", "suffix", "double", "bare", "no-end"]))
    return {"block": block + "\n", "spaces": f"  \n{VERDICT_BEGIN}{payload}{VERDICT_END}\t\n",
            "prefix": "Summary first.\n" + block, "suffix": block + "\nDone.",
            "double": block + "\n" + block, "bare": payload, "no-end": f"{VERDICT_BEGIN}\n{payload}\n"}[shape]


def outcome(parse, text: str) -> tuple[bool, Any]:
    try:
        return True, parse(text, REVISION)
    except Exception as exc:  # noqa: BLE001 - any exception refuses; the base lets RecursionError escape
        return False, exc


def certify(verdict: dict) -> dict:
    state = {"id": "gate-cert", "peer": "astra", "on_agreement": "proceed"}
    round_state = {"status": "approve", "revision": REVISION, "verdict": verdict,
                   "main_approval": {"approved": True, "expected_revision": REVISION}}
    return certificate(state, round_state, issued_at="2026-11-13T00:00:00Z")


def certificate_bytes(issued: dict) -> bytes:
    """certificate.json's encoding (`write_json`)."""
    return (json.dumps(issued, indent=2, sort_keys=True) + "\n").encode("utf-8")


def decoded(text: str) -> Any:
    return json.loads(text.split(VERDICT_BEGIN, 1)[1].split(VERDICT_END, 1)[0])


@SETTINGS
@given(peer_outputs())
def test_accepted_implies_certifiable(text):
    """Invariants 1 and 4: an accepted verdict is the peer's value, certifies unchanged, and does so every time."""
    accepted, verdict = outcome(parse_verdict, text)
    if not accepted:
        assert isinstance(verdict, GateError) and verdict.code == 4, repr(verdict)
        return
    assert verdict == decoded(text)                       # never rewritten while parsed
    assert parse_verdict(wrap_verdict(verdict), REVISION) == verdict
    assert parse_verdict(text, REVISION) == verdict
    if verdict["verdict"] == "approve":
        issued = certify(verdict)
        assert issued["peer_verdict"] == verdict
        assert certificate_bytes(certify(parse_verdict(text, REVISION))) == certificate_bytes(issued)


@SETTINGS
@given(peer_outputs())
def test_head_refuses_every_text_the_base_refuses(text):
    """Invariant 2: no text release/217 refused at 266c3ab8b is accepted now, and accepted ones agree."""
    base_accepted, base_verdict = outcome(base.parse_verdict, text)
    head_accepted, head_verdict = outcome(parse_verdict, text)
    if head_accepted:
        assert base_accepted, (text, base_verdict)
        assert head_verdict == base_verdict


def depth(value: Any) -> int:
    pending, deepest = [(value, 0)], 0
    while pending:
        item, level = pending.pop()
        if isinstance(item, (dict, list)):
            deepest = max(deepest, level + 1)
            pending.extend((child, level + 1) for child in (item.values() if isinstance(item, dict) else item))
    return deepest


def holds_sentinel(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False)
    return VERDICT_BEGIN in text or VERDICT_END in text


def accepted(parse, text: str) -> Any:
    """The verdict `parse` returns for `text`, or None when it refuses."""
    ok, verdict = outcome(parse, text)
    return verdict if ok else None


def refusal(parse, text: str) -> str:
    ok, error = outcome(parse, text)
    return "" if ok else str(error)


CLASSES = {
    "both accept an approval quoting a sentinel": lambda text: (
        (verdict := accepted(parse_verdict, text)) is not None and verdict["verdict"] == "approve"
        and holds_sentinel(verdict)),
    "both accept non-ASCII text": lambda text: (
        (verdict := accepted(parse_verdict, text)) is not None
        and not json.dumps(verdict, ensure_ascii=False).isascii()),
    "both accept nesting 20 deep or more": lambda text: (
        (verdict := accepted(parse_verdict, text)) is not None and depth(verdict) >= 20),
    "the base accepts a number past float range that the head refuses": lambda text: (
        accepted(base.parse_verdict, text) is not None and "float range" in refusal(parse_verdict, text)),
    "both refuse": lambda text: accepted(base.parse_verdict, text) is None,
}


@pytest.mark.parametrize("name", CLASSES)
def test_the_strategy_reaches_every_class(name):
    """The properties above are not vacuous: the strategy builds each kind of text they judge."""
    find(peer_outputs(), CLASSES[name], settings=settings(max_examples=5000, deadline=None, database=None))


def test_the_base_parser_is_266c3ab8b_verbatim_and_had_both_defects():
    body = base.SOURCE.read_bytes()
    assert hashlib.sha256(body).hexdigest() == base.SHA256
    assert base.git_blob_id(body) == base.GIT_BLOB
    assert base.parse_verdict is not parse_verdict
    overflow = f'{VERDICT_BEGIN}\n{json.dumps({"schema_version": 1, "artifact_revision": REVISION, "verdict": "approve", "summary": "s", "findings": [], "notes": []})[:-1]}, "x": 1e1000}}\n{VERDICT_END}'
    assert base.parse_verdict(overflow, REVISION)["x"] == float("inf")
    with pytest.raises(GateError, match="float range"):
        parse_verdict(overflow, REVISION)
