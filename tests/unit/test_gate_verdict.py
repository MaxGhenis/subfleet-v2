"""Malformed peer output never counts as either agreement or requested changes."""

import json

import pytest

from subfleet.contracts import Attestation, AttestationResult
from subfleet.gate.errors import GateError
from subfleet.gate.verdict import (MAX_DEPTH, TEMPLATE_VERDICT, VERDICT_BEGIN, VERDICT_END, VerdictFormatError,
                                   decode_output, parse_verdict, rejected_output_evidence,
                                   validate_attestation)

REVISION = {"kind": "plan", "sha256": "a" * 64, "bytes": 8}
FINDING = {"severity": "high", "location": "plan:1", "description": "Add rollback."}


def payload(**changes):
    return {"schema_version": 1, "artifact_revision": REVISION, "verdict": "approve",
            "summary": "Reviewed the exact plan.", "findings": [], "notes": [], **changes}


def envelope(value):
    return f"{VERDICT_BEGIN}\n{json.dumps(value)}\n{VERDICT_END}\n"


def nested(levels):
    """A list nested `levels` deep; as a member of the payload it sits one level lower."""
    value = []
    for _ in range(levels - 1):
        value = [value]
    return value


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
    pytest.param(VERDICT_BEGIN + "[" * 100_000 + "]" * 100_000 + VERDICT_END, id="nesting-too-deep-to-parse"),
    envelope(payload(summary="\ud800")),
    # Built as text: parsing nesting this deep at import overflows a test worker's stack.
    pytest.param(envelope(payload()).replace('"Reviewed the exact plan."', "[" * 30_000 + "]" * 30_000),
                 id="summary-nested-too-deep"),
    envelope(payload(extra=nested(MAX_DEPTH))),
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


def test_c23_9_nesting_is_bounded_by_a_fixed_depth_not_by_the_interpreter():
    """C-3.2, C-23.9 (amended): what parses, journals and pretty-prints differs by Python version, so the bound is fixed."""
    at_bound = payload(extra=nested(MAX_DEPTH - 1))
    assert parse_verdict(envelope(at_bound), REVISION) == at_bound
    json.dumps(at_bound, indent=2, sort_keys=True)  # gate.json's encoding, recursive on Python 3.12
    with pytest.raises(VerdictFormatError, match=f"nested more than {MAX_DEPTH} levels"):
        parse_verdict(envelope(payload(extra=nested(MAX_DEPTH))), REVISION)
    with pytest.raises(VerdictFormatError, match=f"nested more than {MAX_DEPTH} levels"):
        parse_verdict(envelope(payload(findings=nested(MAX_DEPTH))), REVISION)


def test_c23_9_non_utf8_output_is_a_format_failure():
    """C-23.9 (amended): undecodable bytes are a malformed envelope, not an operational error."""
    assert decode_output(envelope(payload()).encode()) == envelope(payload())
    with pytest.raises(VerdictFormatError, match="not UTF-8"):
        decode_output(b"\xff" + envelope(payload()).encode())


OTHER = {**REVISION, "sha256": "b" * 64}


@pytest.mark.parametrize("text,refusal,verdicts,lock", [
    ("preface\n" + envelope(payload()), None, ["approve"], []),
    (envelope({k: v for k, v in payload().items() if k != "verdict"}), None, [], []),
    (envelope(payload(verdict="")) + "x", None, [], []),
    ("preface\n" + envelope(payload(verdict="changes_requested", findings=[FINDING])), None,
     ["changes_requested"], ["named verdict 'changes_requested'", "listed findings or notes"]),
    (envelope(payload(verdict="blocked")) + envelope(payload()), None, ["blocked", "approve"],
     ["named verdict 'blocked'"]),
    (envelope(payload()).replace('"approve"', '"blocked", "verdict": "approve"'), None,
     ["blocked", "approve"], ["named verdict 'blocked'"]),
    (envelope(payload(verdict=TEMPLATE_VERDICT)) + envelope(payload()), None, ["approve"], []),
    (envelope(payload(verdict=None, findings=[FINDING])), None, [], ["listed findings or notes"]),
    (envelope(payload(verdict=["approve"])) + "x", None, ['["approve"]'], ["named verdict '[\"approve\"]'"]),
    (envelope(payload(verdict="\ud800")) + "x", None, ["\\ud800"], ["named verdict '\\\\ud800'"]),
    # Only the template's own shape leaves the re-ask free to approve.
    (envelope({**{k: v for k, v in payload().items() if k not in ("verdict", "findings")},
               "Verdict": "changes_requested", "Findings": [FINDING]}), None, [],
     ["carried members outside the verdict template (Findings, Verdict)"]),
    (envelope({**{k: v for k, v in payload().items() if k != "verdict"},
               "review": {"verdict": "changes_requested", "findings": [FINDING]}}), None, [],
     ["carried members outside the verdict template (review)"]),
    (envelope(payload(confidence="high")) + "x", None, ["approve"],
     ["carried members outside the verdict template (confidence)"]),
    (envelope(payload(summary={"real_verdict": "changes_requested"})), None, ["approve"],
     ["gave a summary that is not text"]),
    (envelope(payload(schema_version=[{"verdict": "blocked"}])), None, ["approve"],
     ["gave a schema_version that is not a number"]),
    # Blocks that read only after repair (fence, escape, trailing comma, JSON string) are held to the same rules.
    (envelope(payload()).replace('"notes": []', '"notes": [],'), None, ["approve"], []),
    (envelope(payload(verdict="changes_requested", findings=[FINDING])).replace('"notes": []', '"notes": [],'),
     None, ["changes_requested"], ["named verdict 'changes_requested'", "listed findings or notes"]),
    (envelope(payload(artifact_revision=OTHER)).replace('"notes": []', '"notes": [],'),
     "different artifact revision", ["approve"], []),
    (envelope(payload(artifact_revision=OTHER, summary=f"Plan {REVISION['sha256']}.")).replace('"notes": []', '"notes": [],'),
     "different artifact revision", ["approve"], []),
    (f"{VERDICT_BEGIN}\n```json\n{json.dumps(payload())}\n```\n{VERDICT_END}".replace("Reviewed", "Match \\d+;"),
     None, ["approve"], []),
    (envelope(json.dumps(payload(verdict="blocked"))), None, ["blocked"], ["named verdict 'blocked'"]),
    (VERDICT_BEGIN + json.dumps(payload(verdict="blocked")), None, ["blocked"], ["named verdict 'blocked'"]),
    # What cannot be read, even after repair, refuses the re-ask.
    ("prose only, no block", "no verdict object to re-emit", [], []),
    (f"{VERDICT_BEGIN}{{not json{VERDICT_END} and more", "cannot be read as a JSON object", [], []),
    (f"{VERDICT_BEGIN}{{verdict: 'changes_requested'}}{VERDICT_END}", "cannot be read as a JSON object", [], []),
    (envelope([payload()]), "cannot be read as a JSON object", [], []),
    # Too deep to parse on one interpreter, a non-object on another: unreadable on both.
    pytest.param(VERDICT_BEGIN + "[" * 100_000 + "]" * 100_000 + VERDICT_END, "cannot be read as a JSON object", [], [],
                 id="nesting-too-deep-to-diagnose"),
    (envelope(payload(extra=nested(MAX_DEPTH))), "cannot be read as a JSON object", [], []),
    (envelope(payload(artifact_revision={**REVISION, "x": nested(MAX_DEPTH)})), "cannot be read as a JSON object", [], []),
    # Verdict fields outside the blocks must read as one object, bound to the reviewed revision.
    ("Verdict: blocked, see below.\n" + envelope(payload()), "outside the verdict blocks", ["approve"], []),
    ("**Findings:** none\n" + envelope(payload()), "outside the verdict blocks", ["approve"], []),
    ('{"verdict"    : "changes_requested", "findings"\n\n\t  : [1]}\n' + envelope(payload()),
     "different artifact revision", ["approve", "changes_requested"],
     ["named verdict 'changes_requested'", "listed findings or notes"]),
    ('"previous_verdict": {"verdict": "approve"}', "outside the verdict blocks", [], []),
    (json.dumps(payload(artifact_revision=OTHER)), "different artifact revision", ["approve"], []),
    (json.dumps({k: v for k, v in payload().items() if k != "artifact_revision"}),
     "different artifact revision", ["approve"], []),
    (json.dumps(payload()).replace('"artifact_revision"', '"\\u0061rtifact_revision"').replace(
        REVISION["sha256"], "b" * 64), "different artifact revision", ["approve"], []),
    ("```json\n" + json.dumps(payload()) + "\n```", None, ["approve"], []),
    ("I checked " + json.dumps(payload(artifact_revision=REVISION)), "outside the verdict blocks", [], []),
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
    ("x" + envelope(payload(notes=None, findings=None)), None, ["approve"], []),
    ("x" + envelope(payload(verdict="changes_requested")), "requests changes without a finding",
     ["changes_requested"], ["named verdict 'changes_requested'"]),
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


# Short ids: a failure must not print a megabyte parameter into the log.
LINEAR_INPUTS = {
    "begin-sentinels": VERDICT_BEGIN * 40_000 + VERDICT_END,
    **{f"run-of-{name}": character * 200_000 for name, character in (
        ("double-quotes", '"'), ("single-quotes", "'"), ("backticks", "`"), ("backslashes", "\\"),
        ("spaces", " "), ("commas", ","))},
    "key-then-spaces": "verdict:" + " " * 200_000,
    "key-then-nonword-run": "verdict" + "!" * 200_000,
    "keys-with-space-runs": ("findings" + " " * 1000) * 200,
    "commas-with-space-runs": VERDICT_BEGIN + ("," + " " * 1000) * 200,
    "newlines-with-space-runs": VERDICT_BEGIN + ("\n" + " " * 1000) * 200,
}


@pytest.mark.parametrize("text", LINEAR_INPUTS.values(), ids=LINEAR_INPUTS.keys())
def test_c23_9_rejected_output_diagnosis_is_linear(text):
    """C-23.9 (amended): no peer output can make the diagnosis stall the daemon.

    Each input is linear at a few hundred milliseconds or less. The quadratic scans
    these replaced took from ten seconds to minutes on the same inputs, so the
    bound has room for a slow, loaded test runner and still tells the two apart.
    """
    import time
    started = time.perf_counter()
    rejected_output_evidence(text, REVISION)
    assert time.perf_counter() - started < 5


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
