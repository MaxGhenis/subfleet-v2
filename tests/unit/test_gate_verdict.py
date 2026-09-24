"""Malformed peer output never counts as either agreement or requested changes."""

import json

import pytest

from subfleet.contracts import Attestation, AttestationResult
from subfleet.gate.errors import GateError
from subfleet.gate.verdict import (TEMPLATE_VERDICT, VERDICT_BEGIN, VERDICT_END, VerdictFormatError,
                                   decode_output, parse_verdict, rejected_output_evidence,
                                   validate_attestation)

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


FORMAT_FAILURES = [
    json.dumps(payload()),
    envelope(payload()).replace(VERDICT_END, ""),
    envelope(payload()) + envelope(payload()),
    VERDICT_END + json.dumps(payload()) + VERDICT_BEGIN,
    "preface\n" + envelope(payload()),
    envelope(payload()) + "trailing",
    f"```json\n{envelope(payload())}```",
    envelope([]),
    envelope(payload(schema_version=2)),
    envelope({k: v for k, v in payload().items() if k != "verdict"}),
    envelope(payload(verdict="changes-requested")),
    envelope(payload(findings={})),
    envelope(payload(verdict="changes_requested", findings=[{"severity": "high"}])),
    envelope(payload(summary=" ")),
    envelope(payload()).replace('"approve"', '"approve", "verdict": "blocked"'),
    envelope(payload()).replace('"findings": []', '"findings": NaN'),
    VERDICT_BEGIN + "[" * 100_000 + "]" * 100_000 + VERDICT_END,
]
NEVER_FORMAT_FAILURES = [
    (envelope(payload(artifact_revision={**REVISION, "sha256": "b" * 64})), "different"),
    (envelope({k: v for k, v in payload().items() if k != "artifact_revision"}), "different"),
    ("preface\n" + envelope(payload(artifact_revision={**REVISION, "bytes": 9})), "different"),
    (envelope(payload(artifact_revision=None, verdict=[])) + "trailing", "different"),
    (envelope(payload(findings=[FINDING])), "actionable findings"),
    (envelope(payload(notes=["Fix this first."])), "nonempty notes"),
    (envelope(payload(verdict="changes_requested")), "without an actionable"),
]


@pytest.mark.parametrize("text", FORMAT_FAILURES)
def test_c23_9_envelope_and_shape_failures_are_typed_for_the_rounds_one_reask(text):
    """C-23.9 (amended): only a format failure is a VerdictFormatError; it still blocks with 4."""
    with pytest.raises(VerdictFormatError) as error:
        parse_verdict(text, REVISION)
    assert error.value.code == 4


@pytest.mark.parametrize("text,message", NEVER_FORMAT_FAILURES)
def test_c23_9_binding_and_contradiction_failures_are_never_format_failures(text, message):
    """C-23.9 (amended): the revision binding is judged before text outside; neither it nor a contradiction is format."""
    with pytest.raises(GateError, match=message) as error:
        parse_verdict(text, REVISION)
    assert not isinstance(error.value, VerdictFormatError) and error.value.code == 4


def test_c23_9_non_utf8_output_is_a_format_failure():
    """C-23.9 (amended): undecodable bytes are a malformed envelope, not an operational error."""
    assert decode_output(envelope(payload()).encode()) == envelope(payload())
    with pytest.raises(VerdictFormatError, match="not UTF-8"):
        decode_output(b"\xff" + envelope(payload()).encode())


OTHER = {**REVISION, "sha256": "b" * 64}


@pytest.mark.parametrize("text,refusal,verdicts,lock", [
    ("prose only, no block", None, [], []),
    ("preface\n" + envelope(payload()), None, ["approve"], []),
    ("preface\n" + envelope(payload(verdict="changes_requested", findings=[FINDING])), None,
     ["changes_requested"], ["named verdict 'changes_requested'", "listed findings or notes"]),
    (envelope(payload(verdict="blocked")) + envelope(payload()), None, ["blocked", "approve"],
     ["named verdict 'blocked'"]),
    (envelope(payload()).replace('"approve"', '"blocked", "verdict": "approve"'), None,
     ["blocked", "approve"], ["named verdict 'blocked'"]),
    (envelope(payload(verdict=TEMPLATE_VERDICT)) + envelope(payload()), None, ["approve"], []),
    (envelope(payload(verdict="")) + "x", None, [], []),
    (envelope(payload(verdict=None, findings=[FINDING])), None, [], ["listed findings or notes"]),
    (envelope(payload(verdict=["approve"])) + "x", None, ['["approve"]'], ["named verdict '[\"approve\"]'"]),
    (envelope(payload(verdict="\ud800")) + "x", None, ["\\ud800"], ["named verdict '\\\\ud800'"]),
    # Unreadable blocks: the digest must be present; JSON verdict members still lock.
    (envelope(payload()).replace('"notes": []', '"notes": [],'), None, ["approve"], []),
    (envelope(payload(verdict="changes_requested", findings=[FINDING])).replace('"notes": []', '"notes": [],'),
     None, ["changes_requested"], ["named verdict 'changes_requested'", "listed findings or notes"]),
    (f"{VERDICT_BEGIN}{{not json{VERDICT_END} and more", "cannot read lacks the reviewed revision", [], []),
    (envelope(payload(artifact_revision=OTHER)).replace('"notes": []', '"notes": [],'),
     "cannot read lacks the reviewed revision", ["approve"], []),
    (VERDICT_BEGIN + json.dumps(payload(verdict="blocked")), None, ["blocked"], ["named verdict 'blocked'"]),
    (json.dumps(payload(artifact_revision=OTHER)), "outside the verdict blocks", ["approve"], []),
    ("I checked " + json.dumps(payload(artifact_revision=REVISION)), None, ["approve"], []),
    # Readable blocks: exact binding, no duplicate keys, no contradiction.
    (envelope(payload()) + envelope(payload(artifact_revision={**REVISION, "bytes": 9})),
     "different artifact revision", ["approve"], []),
    (envelope(payload()).replace('"artifact_revision"', '"artifact_revision": null, "artifact_revision"'),
     "different artifact revision", ["approve"], []),
    (envelope(payload()).replace('"sha256": "a', '"sha256": "' + "b" * 64 + '", "sha256": "a'),
     "different artifact revision", ["approve"], []),
    ("x" + envelope({k: v for k, v in payload().items() if k != "artifact_revision"}),
     "different artifact revision", ["approve"], []),
    ("x" + envelope(payload(findings=[FINDING])), "approves with findings or notes", ["approve"],
     ["listed findings or notes"]),
    ("x" + envelope(payload(notes=["n"])), "approves with findings or notes", ["approve"],
     ["listed findings or notes"]),
    ("x" + envelope(payload(findings="none")), "approves with findings or notes", ["approve"],
     ["listed findings or notes"]),
    ("x" + envelope(payload(notes=None, findings=None)), None, ["approve"], []),
    ("x" + envelope(payload(verdict="changes_requested")), "requests changes without a finding",
     ["changes_requested"], ["named verdict 'changes_requested'"]),
    (VERDICT_BEGIN + "[" * 100_000 + "]" * 100_000 + VERDICT_END, "too deeply nested", [], []),
])
def test_c23_9_rejected_output_diagnosis_only_forbids_or_constrains_a_reask(text, refusal, verdicts, lock):
    """C-23.9 (amended): every block, readable or not, can refuse a re-ask or lock it; none becomes a verdict."""
    evidence = rejected_output_evidence(text, REVISION)
    assert evidence["verdicts"] == verdicts
    assert evidence["outcome_lock"] == lock
    if refusal is None:
        assert evidence["refusal"] is None
    else:
        assert refusal in evidence["refusal"]


def test_c23_9_rejected_output_diagnosis_is_linear_in_sentinel_count():
    """C-23.9 (amended): a flood of begin sentinels cannot stall the gate lock."""
    import time
    text = VERDICT_BEGIN * 40_000 + VERDICT_END
    started = time.perf_counter()
    rejected_output_evidence(text, REVISION)
    assert time.perf_counter() - started < 2


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
