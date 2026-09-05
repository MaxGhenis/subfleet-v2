"""Malformed peer output never counts as either agreement or requested changes."""

import json

import pytest

from subfleet.contracts import Attestation, AttestationResult
from subfleet.gate.errors import GateError
from subfleet.gate.verdict import VERDICT_BEGIN, VERDICT_END, parse_verdict, validate_attestation

REVISION = {"kind": "plan", "sha256": "a" * 64, "bytes": 8}
FINDING = {"severity": "high", "location": "plan:1", "description": "Add rollback."}


def payload(**changes):
    return {"schema_version": 1, "artifact_revision": REVISION, "verdict": "approve",
            "summary": "Reviewed the exact plan.", "findings": [], "notes": [], **changes}


def envelope(value):
    return f"{VERDICT_BEGIN}\n{json.dumps(value)}\n{VERDICT_END}\n"


@pytest.mark.parametrize("verdict,findings", [("approve", []), ("changes_requested", [FINDING]), ("blocked", [])])
def test_valid_revision_bound_verdicts(verdict, findings):
    """C-23.9: only explicit structured review states count as peer verdicts."""
    value = payload(verdict=verdict, findings=findings)
    assert parse_verdict(envelope(value), REVISION) == value


@pytest.mark.parametrize("text,message", [
    (json.dumps(payload()), "sentinel"),
    (envelope(payload()).replace(VERDICT_END, ""), "sentinel"),
    (envelope(payload()) + envelope(payload()), "sentinel"),
    (f"{VERDICT_BEGIN}{json.dumps(payload())}{json.dumps(payload())}{VERDICT_END}", "invalid JSON"),
    (VERDICT_END + json.dumps(payload()) + VERDICT_BEGIN, "order"),
    ("preface\n" + envelope(payload()), "outside"),
    (envelope(payload()) + "trailing", "outside"),
    (envelope([]), "JSON object"),
    (envelope(payload(schema_version=2)), "schema"),
    (envelope(payload(schema_version=True)), "schema"),
    (envelope(payload(artifact_revision={**REVISION, "sha256": "b" * 64})), "different"),
    (envelope(payload(verdict="changes-requested")), "invalid"),
    (envelope(payload(verdict=[])), "invalid"),
    (envelope(payload(findings=[FINDING])), "actionable findings"),
    (envelope(payload(notes=["Fix this first."])), "nonempty notes"),
    (envelope(payload(verdict="changes_requested")), "without an actionable"),
    (envelope(payload(findings={})), "array of objects"),
    (envelope(payload(findings=["bug"])), "array of objects"),
    (envelope(payload(findings=[{"severity": "high"}])), "every peer finding"),
    (envelope(payload(findings=[{**FINDING, "location": " "}])), "every peer finding"),
    (envelope(payload(notes="none")), "summary/notes"),
    (envelope(payload(notes=[1])), "summary/notes"),
    (envelope(payload(summary=" ")), "summary/notes"),
    (envelope(payload(summary=None)), "summary/notes"),
    (envelope(payload()).replace('"approve"', '"approve", "verdict": "blocked"'), "duplicate JSON field"),
    (envelope(payload()).replace('"findings": []', '"findings": NaN'), "invalid JSON"),
])
def test_malformed_verdicts_fail_closed(text, message):
    """C-23.9, C-17.1: every ambiguous or contradictory verdict is blocked with 4."""
    with pytest.raises(GateError, match=message) as error:
        parse_verdict(text, REVISION)
    assert error.value.code == 4


@pytest.mark.parametrize("status", ["mismatch", "unattested", None])
@pytest.mark.parametrize("verdict,findings", [("approve", []), ("changes_requested", [FINDING])])
def test_fable_unattested_round_is_not_a_verdict(status, verdict, findings):
    """C-23.43: unattested Fable output is neither approval nor changes requested."""
    value = envelope(payload(verdict=verdict, findings=findings))
    with pytest.raises(GateError, match="not a verdict") as error:
        validate_attestation(status, "claude-fable-5-1", served_model="claude-fable-5-1")
        parse_verdict(value, REVISION)
    assert error.value.code == 4


@pytest.mark.parametrize("downgrade", [{}, "", {"served_model": "claude-opus-4-8"}, True])
def test_any_downgrade_record_discards_round(downgrade):
    """C-23.43: even an attested label cannot override a downgrade record."""
    with pytest.raises(GateError, match="downgrade"):
        validate_attestation("attested", "claude-fable-5-1",
                             served_model="claude-fable-5-1", downgrade=downgrade)


@pytest.mark.parametrize("served", [None, "claude-fable-5", "claude-opus-4-8"])
def test_attestation_must_name_requested_model(served):
    """C-23.43: the old Fable alias and another served model cannot attest this peer."""
    with pytest.raises(GateError, match="served-model"):
        validate_attestation("attested", "claude-fable-5-1", served_model=served)


def test_attestation_accepts_adapter_result_for_pinned_model():
    """C-23.43, C-12.5: the adapter's positive served-model evidence admits parsing."""
    result = AttestationResult(Attestation.ATTESTED, "claude-fable-5-1", "transcript")
    validate_attestation(result, "claude-fable-5-1")
    validate_attestation("attested", "gpt-6", served_model="gpt-6")


def test_c23_43_codex_round_without_persisted_rollout_still_counts():
    """C-23.43 (amended): a Codex peer pins its model at launch; an unattested round counts and is recorded."""
    from subfleet.gate.verdict import validate_attestation
    validate_attestation({"status": "unattested", "served_model": None}, "gpt-6-astra")


def test_c23_43_codex_mismatch_and_claude_unattested_are_not_verdicts():
    """C-23.43: a mismatch is never a verdict, and a Claude round must be positively attested."""
    from subfleet.gate.verdict import validate_attestation
    with pytest.raises(GateError):
        validate_attestation({"status": "mismatch", "served_model": "gpt-5.6-terra"}, "gpt-6-astra")
    with pytest.raises(GateError):
        validate_attestation({"status": "unattested", "served_model": None}, "claude-fable-5-1")
