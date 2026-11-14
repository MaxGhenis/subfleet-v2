"""C-23.9 (amended): properties of the strict parser and the re-ask diagnosis.

Invariants, for every peer output these strategies can build:
- strict: text around a block the parser accepts is never accepted, and what
  the parser accepts is the object as written;
- bounded: a verdict is accepted at any nesting up to MAX_DEPTH and refused
  beyond it, on every interpreter;
- fail closed: the diagnosis leaves a re-ask free to approve (no refusal, no
  outcome lock) only when the one object it read is bound to the reviewed
  revision and has the shape of an approval. The oracle below judges the object
  as a plain dict, before it is serialized, wrapped and damaged; the diagnosis
  sees only the damaged text;
- total: the diagnosis returns for any text and never raises.
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st
import pytest

from subfleet.gate.errors import GateError
from subfleet.gate.verdict import (MAX_DEPTH, TEMPLATE_VERDICT, VERDICT_BEGIN, VERDICT_END,
                                   VerdictFormatError, parse_verdict, rejected_output_evidence)

REVISION = {"kind": "plan", "sha256": "a" * 64, "bytes": 8}
OTHER = {"kind": "plan", "sha256": "b" * 64, "bytes": 8}
FINDING = {"severity": "high", "location": "plan:1", "description": "Add rollback."}
TEMPLATE_KEYS = {"schema_version", "artifact_revision", "verdict", "summary", "findings", "notes"}

# Text with no sentinel and no verdict-schema key written as `key:`.
prose = st.text(alphabet="abcdefghijklmnopqrstuvwxyz ,.\n", max_size=60)


def envelope(value) -> str:
    return f"{VERDICT_BEGIN}\n{json.dumps(value)}\n{VERDICT_END}\n"


def valid(verdict: str) -> dict:
    return {"schema_version": 1, "artifact_revision": REVISION, "verdict": verdict,
            "summary": "Reviewed the exact plan.",
            "findings": [FINDING] if verdict == "changes_requested" else [], "notes": []}


@given(st.sampled_from(["approve", "changes_requested", "blocked"]), prose, prose)
def test_c23_9_text_outside_an_accepted_block_is_never_accepted(verdict, before, after):
    """C-23.9: the parser accepts the block alone and nothing wrapped around it."""
    value = valid(verdict)
    text = envelope(value)
    assert parse_verdict(text, REVISION) == value
    if (before + after).strip():
        with pytest.raises(VerdictFormatError, match="outside the verdict sentinel"):
            parse_verdict(before + text + after, REVISION)


def nested(levels: int) -> list:
    value: list = []
    for _ in range(levels - 1):
        value = [value]
    return value


@given(st.integers(min_value=1, max_value=3 * MAX_DEPTH), st.sampled_from(["extra", "findings", "notes"]))
def test_c23_9_nesting_is_accepted_up_to_the_bound_and_refused_beyond_it(levels, member):
    """C-23.9 (amended): the nesting bound is exact; a member sits one level below the object."""
    value = {**valid("blocked"), member: nested(levels)}
    if levels + 1 > MAX_DEPTH:
        with pytest.raises(VerdictFormatError, match="nested more than"):
            parse_verdict(envelope(value), REVISION)
    elif member == "extra" or levels == 1:  # an unknown member, or an empty findings or notes list
        assert parse_verdict(envelope(value), REVISION) == value
    else:  # within the bound the shape rules still apply: a list of lists is neither findings nor notes
        with pytest.raises(VerdictFormatError):
            parse_verdict(envelope(value), REVISION)


# One verdict-like object: any mix of right and wrong members, spellings and nesting.
member_values = {
    "schema_version": st.sampled_from([1, 2, None, [1], {"verdict": "blocked"}]),
    "artifact_revision": st.sampled_from([REVISION, OTHER, None, {**REVISION, "bytes": 9}, [REVISION]]),
    "verdict": st.sampled_from(["approve", "changes_requested", "blocked", "", None, TEMPLATE_VERDICT,
                                "Approve", "approve ", ["approve"], {"is": "approve"}]),
    "summary": st.sampled_from(["Reviewed.", "", None, 5, ["x"], {"verdict": "blocked"}]),
    "findings": st.sampled_from([[], None, [FINDING], "none", {}, [[]], ""]),
    "notes": st.sampled_from([[], None, ["Consider a rollback."], "n", {}, ""]),
}
extra_members = st.dictionaries(
    st.sampled_from(["Verdict", "VERDICT", "Findings", "review", "final_verdict", "confidence", "Notes"]),
    st.sampled_from(["changes_requested", "approve", [FINDING], {"verdict": "blocked"}, 1]), max_size=2)


@st.composite
def verdict_objects(draw) -> dict:
    """An approval-shaped object with a few members changed, dropped or added.

    Starting from the shape and changing little makes an object that breaks
    exactly one rule common, which is where a missing rule would show.
    """
    value = {"schema_version": 1, "artifact_revision": REVISION,
             "verdict": draw(st.sampled_from(["approve", "", None, TEMPLATE_VERDICT])),
             "summary": "Reviewed.", "findings": [], "notes": []}
    for key in draw(st.sets(st.sampled_from(sorted(TEMPLATE_KEYS)), max_size=2)):
        value[key] = draw(member_values[key])
    for key in draw(st.sets(st.sampled_from(sorted(TEMPLATE_KEYS)), max_size=1)):
        value.pop(key)
    if draw(st.integers(min_value=0, max_value=3)) == 0:
        value.update(draw(extra_members))
    return value


def approval_shaped(value: dict) -> bool:
    """The oracle: bound to the reviewed revision and consistent with approving."""
    return (value.get("artifact_revision") == REVISION and "artifact_revision" in value
            and set(value) <= TEMPLATE_KEYS
            and value.get("verdict") in ("approve", "", None, TEMPLATE_VERDICT)
            and value.get("findings") in ([], None, {}, "")
            and value.get("notes") in ([], None, {}, "")
            and (value.get("summary") is None or isinstance(value["summary"], str))
            and not isinstance(value.get("schema_version"), (list, dict)))


def damage(payload: str, how: str) -> str:
    """Ways a peer's block fails on form while its object stays readable."""
    if how == "fence":
        return f"```json\n{payload}\n```"
    if how == "trailing-comma":
        return payload[:-1] + ",}" if payload != "{}" else payload
    if how == "json-string":
        return json.dumps(payload)
    return payload


@settings(max_examples=400)
@given(verdict_objects(), st.sampled_from(["none", "fence", "trailing-comma", "json-string"]),
       prose, prose, st.booleans())
def test_c23_9_a_reask_is_free_to_approve_only_after_an_approval_shaped_object(value, how, before, after, sentinels):
    """C-23.9 (amended): no refusal and no lock implies the object read was bound and approval-shaped."""
    payload = damage(json.dumps(value), how)
    text = (before + f"{VERDICT_BEGIN}\n{payload}\n{VERDICT_END}\n" + after) if sentinels else payload
    evidence = rejected_output_evidence(text, REVISION)
    if evidence["refusal"] is None and not evidence["outcome_lock"]:
        assert approval_shaped(value), (value, how, evidence)
    if approval_shaped(value) and sentinels:
        # The motivating shapes stay re-askable: prose around a readable approval-shaped block.
        assert evidence["refusal"] is None and not evidence["outcome_lock"], (value, how, evidence)


@settings(max_examples=300)
@given(st.lists(st.sampled_from(list('{}[]":,\\`\'*- \n') + ["verdict", "findings", "notes",
                                                                "artifact_revision", "approve", "null",
                                                                VERDICT_BEGIN, VERDICT_END, "x"]),
                max_size=40).map("".join))
def test_c23_9_the_diagnosis_is_total(text):
    """C-23.9 (amended), C-17.1: any output is diagnosed; nothing raises out of the gate op."""
    evidence = rejected_output_evidence(text, REVISION)
    assert set(evidence) == {"refusal", "verdicts", "outcome_lock"}
    assert evidence["refusal"] is None or isinstance(evidence["refusal"], str)
    try:
        parse_verdict(text, REVISION)
    except GateError:
        pass
