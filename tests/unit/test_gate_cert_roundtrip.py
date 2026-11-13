"""D-GATE-CERT1 (C-23.9): every verdict the gate accepts can certify.

`certificate()` serializes the accepted verdict and parses it again, so whatever
`parse_verdict` accepts must survive that round trip unchanged. Two inputs did not
(review r5 of #178, reproduced on release/217 at 266c3ab8b): a number past float
range, which parses as infinity and is written back as `Infinity`; and an escaped
sentinel, which decodes to the sentinel and is written back literally.
"""

from __future__ import annotations

import json

import pytest

from subfleet.gate.certificate import certificate, replay, write_json
from subfleet.gate.errors import GateError
from subfleet.gate.round import peer_prompt
from subfleet.gate.service import dispatch
from subfleet.gate.verdict import (VERDICT_BEGIN, VERDICT_END, escape_sentinels, parse_verdict,
                                   wrap_verdict)
from tests.fake.test_gate_end_to_end import arguments, finish, wire
from tests.unit.test_gate_admission import core  # noqa: F401  Real submit/admit; fake process boundary.

REVISION = {"kind": "plan", "sha256": "a" * 64, "bytes": 8}
FINDING = {"severity": "high", "location": "plan:1", "description": "Add rollback."}


def payload(**changes):
    return {"schema_version": 1, "artifact_revision": REVISION, "verdict": "approve",
            "summary": "Reviewed the exact plan.", "findings": [], "notes": [], **changes}


def wrap(text: str) -> str:
    return f"{VERDICT_BEGIN}\n{text}\n{VERDICT_END}\n"


def with_members(value: dict, members: str) -> str:
    """The JSON text of `value` with raw members (literals json.dumps cannot write) appended."""
    return json.dumps(value)[:-1] + members + "}"


def certify(verdict: dict, revision: dict = REVISION) -> dict:
    state = {"id": "gate-cert", "peer": "astra", "on_agreement": "proceed"}
    round_state = {"status": "approve", "revision": revision, "verdict": verdict,
                   "main_approval": {"approved": True, "expected_revision": revision}}
    return certificate(state, round_state, issued_at="2026-11-13T00:00:00Z")


# --- numbers -------------------------------------------------------------------------

@pytest.mark.parametrize("members,where", [
    (', "extra": 1e1000', "$.extra"),
    (', "extra": -1e400', "$.extra"),
    (', "extra": 2e308', "$.extra"),
    (', "extra": {"deep": [0, 1.5, 1' + "0" * 400 + '.0]}', "$.extra.deep[2]"),
    (', "a key": 1e999', '$["a key"]'),
    (', "extra": [1e999, -1e999]', "$.extra[0]"),
])
def test_number_past_float_range_is_refused_at_parse_time_naming_where(members, where):
    """D-GATE-CERT1: a literal past float range parses as infinity; it is refused with exit 4 and its path."""
    with pytest.raises(GateError) as error:
        parse_verdict(wrap(with_members(payload(), members)), REVISION)
    assert error.value.code == 4
    assert str(error.value) == f"peer verdict holds a number outside the float range at {where}"


def test_number_past_float_range_inside_a_finding_names_the_finding():
    value = payload(verdict="changes_requested", findings=[FINDING])
    text = json.dumps(value).replace('"description": "Add rollback."', '"description": "Add rollback.", "weight": -1e400')
    with pytest.raises(GateError, match=r"at \$\.findings\[0\]\.weight$"):
        parse_verdict(wrap(text), REVISION)


def test_number_path_is_bounded_and_ascii():
    """The path is peer-controlled text: it is clipped and written in ASCII before it reaches the journal."""
    key = "\udc80" + "k" * 1000
    text = with_members(payload(), f', {json.dumps(key)}: 1e1000')
    with pytest.raises(GateError) as error:
        parse_verdict(wrap(text), REVISION)
    message = str(error.value)
    assert message.isascii() and message.endswith("...")
    assert len(message) < 300


def test_revision_binding_outranks_a_number_past_float_range():
    """The binding stays its own refusal: a wrongly bound verdict is reported as such."""
    text = with_members(payload(artifact_revision={**REVISION, "bytes": 9}), ', "extra": 1e1000')
    with pytest.raises(GateError, match="different artifact revision"):
        parse_verdict(wrap(text), REVISION)


@pytest.mark.parametrize("literal", ["1.7976931348623157e308", "-1.7976931348623157e308", "5e-324",
                                     "1e-1000", "-0.0", "1" + "0" * 300, "0.1", "1E2"])
def test_finite_extremes_are_accepted_and_certify_unchanged(literal):
    """Only infinity is new; every finite number round-trips through the certificate exactly."""
    parsed = parse_verdict(wrap(with_members(payload(), f', "extra": {literal}')), REVISION)
    assert parsed["extra"] == json.loads(literal)
    assert type(parsed["extra"]) is type(json.loads(literal))
    assert certify(parsed)["peer_verdict"] == parsed


def test_a_round_with_a_number_past_float_range_blocks_instead_of_agreeing(core, tmp_path):
    """D-GATE-CERT1: the round blocks with exit 4; it never journals `agreed` and then fails every poll."""
    plan = tmp_path / "plan.md"
    plan.write_text("A concrete plan.\n")
    started = dispatch(core, "gate.start", wire(arguments(plan)))
    finish(core, started, transform=lambda text: text.replace('"notes":[]}', '"notes":[],"extra":1e1000}'))
    result = dispatch(core, "gate.poll", {"gate_id": started["gate_id"]})
    assert result["code"] == 4 and result["status"] == "blocked"
    assert "outside the float range at $.extra" in result["message"]
    state = core._gate_service._load(started["gate_id"])
    assert state["status"] == "blocked" and state["rounds"][-1]["verdict"] is None
    assert dispatch(core, "gate.poll", {"gate_id": started["gate_id"]}) == result
    assert not (core.root / "gates" / started["gate_id"] / "certificate.json").exists()


# --- sentinels -----------------------------------------------------------------------

# Escaped in the peer's JSON, so its output holds one sentinel pair; each decodes to a sentinel.
ESCAPED_BEGIN = "\\u002d--SUBFLEET-VERDICT-BEGIN---"
ESCAPED_END = "\\u002d--SUBFLEET-VERDICT-END---"
# Decodes to three sentinels in a row that share their hyphens, written so that the
# peer's text holds none. `str.replace` resumes after a match, so it misses the second
# begin sentinel, which starts inside the first.
OVERLAPPING = ("\\u002d--SUBFLEET-VERDICT-BEGIN-\\u002d-SUBFLEET-VERDICT-BEGIN-\\u002d-"
               "SUBFLEET-VERDICT-END---")


def quoting(escaped: str) -> str:
    """A valid approval whose summary quotes `escaped` (JSON string content) between words."""
    return json.dumps(payload(summary="QUOTE")).replace('"QUOTE"', f'"see {escaped} here"')


@pytest.mark.parametrize("escaped,decoded", [
    (ESCAPED_BEGIN, VERDICT_BEGIN),
    (ESCAPED_END, VERDICT_END),
    (OVERLAPPING, "---SUBFLEET-VERDICT-BEGIN---SUBFLEET-VERDICT-BEGIN---SUBFLEET-VERDICT-END---"),
    ("\\u002d\\u002d\\u002dSUBFLEET-VERDICT-END\\u002d\\u002d\\u002d", VERDICT_END),
    ("-\\u002d--SUBFLEET-VERDICT-BEGIN---", "-" + VERDICT_BEGIN),
    ("\\\\\\u002d--SUBFLEET-VERDICT-END---", "\\" + VERDICT_END),
])
def test_a_quoted_sentinel_is_accepted_as_written_and_certifies(escaped, decoded):
    """D-GATE-CERT1: the verdict keeps the decoded text, and the certificate re-parse finds one pair."""
    text = wrap(quoting(escaped))
    assert text.count(VERDICT_BEGIN) == text.count(VERDICT_END) == 1
    parsed = parse_verdict(text, REVISION)
    assert parsed["summary"] == f"see {decoded} here"  # the stored value is the peer's, unescaped
    assert certify(parsed)["peer_verdict"] == parsed


def test_a_sentinel_quoted_in_a_member_name_certifies():
    text = wrap(with_members(payload(), f', "{ESCAPED_END}": [{{"{ESCAPED_BEGIN}": 1}}]'))
    parsed = parse_verdict(text, REVISION)
    assert parsed[VERDICT_END] == [{VERDICT_BEGIN: 1}]
    assert certify(parsed)["peer_verdict"] == parsed


@pytest.mark.parametrize("value", [
    VERDICT_BEGIN, VERDICT_END, VERDICT_BEGIN[1:] + VERDICT_END[:-1], "-" * 3 + VERDICT_BEGIN * 3,
    (VERDICT_BEGIN + VERDICT_END[3:]) * 4, "\\" + VERDICT_END, "x\\u002d" + VERDICT_BEGIN,
    {VERDICT_BEGIN: [VERDICT_END, -1, -1.5e-3]}, "--SUBFLEET-VERDICT-BEGIN---", "plain-text - with -- hyphens",
    VERDICT_BEGIN + VERDICT_BEGIN[3:] + VERDICT_END[3:], VERDICT_END + VERDICT_END[3:] * 3,
])
def test_escaped_json_holds_no_sentinel_and_decodes_to_the_same_value(value):
    for dumps in ({}, {"indent": 2, "sort_keys": True}, {"ensure_ascii": False}):
        escaped = escape_sentinels(json.dumps(value, **dumps))
        assert VERDICT_BEGIN not in escaped and VERDICT_END not in escaped
        assert json.loads(escaped) == value
    assert wrap_verdict(value).count(VERDICT_BEGIN) == wrap_verdict(value).count(VERDICT_END) == 1


def test_a_one_pass_replace_would_miss_a_sentinel_that_starts_inside_another():
    """Why the escape uses a lookahead: the overlapping case defeats `str.replace`."""
    text = json.dumps(VERDICT_BEGIN + VERDICT_BEGIN[3:])
    naive = text.replace(VERDICT_BEGIN, "\\u002d" + VERDICT_BEGIN[1:])
    assert VERDICT_BEGIN in naive and VERDICT_BEGIN not in escape_sentinels(text)


def test_escaping_leaves_sentinel_free_json_byte_for_byte():
    """No sentinel, no change: certificates and prompts keep v1's bytes."""
    value = payload(summary="a - b -- c --- d", extra=[-1, "---", "SUBFLEET-VERDICT-BEGIN"])
    assert escape_sentinels(json.dumps(value)) == json.dumps(value)
    assert wrap_verdict(value) == f"{VERDICT_BEGIN}\n{json.dumps(value)}\n{VERDICT_END}"


def test_replay_rebuilds_a_certificate_whose_verdict_quotes_a_sentinel(tmp_path):
    """D-GATE-CERT1: replay() goes through certificate(), so archived evidence replays too."""
    verdict = parse_verdict(wrap(quoting(ESCAPED_BEGIN)), REVISION)
    round_state = {"status": "approve", "revision": REVISION, "verdict": verdict,
                   "main_approval": {"approved": True, "expected_revision": REVISION}}
    state = {"schema_version": 1, "id": "gate-cert", "peer": "astra", "on_agreement": "proceed",
             "rounds": [round_state]}
    write_json(tmp_path / "gate.json", state)
    write_json(tmp_path / "certificate.json", {"issued_at": "2026-11-13T00:00:00Z"})
    assert replay(tmp_path) == certify(verdict)
    assert replay(tmp_path)["peer_verdict"]["summary"] == f"see {VERDICT_BEGIN} here"


def test_a_round_whose_peer_quotes_a_sentinel_agrees_and_issues_its_certificate(core, tmp_path):
    """D-GATE-CERT1: review r5's input completes the gate (exit 0) instead of wedging it at `agreed`."""
    plan = tmp_path / "plan.md"
    plan.write_text("A concrete plan.\n")
    started = dispatch(core, "gate.start", wire(arguments(plan)))
    finish(core, started, transform=lambda text: text.replace(
        '"The copied plan is consistent."', f'"Quoted {ESCAPED_BEGIN} and {ESCAPED_END}."'))
    result = dispatch(core, "gate.poll", {"gate_id": started["gate_id"]})
    assert result["code"] == 0 and result["status"] == "completed", result
    directory = core.root / "gates" / started["gate_id"]
    issued = json.loads((directory / "certificate.json").read_text())
    assert issued["peer_verdict"]["summary"] == f"Quoted {VERDICT_BEGIN} and {VERDICT_END}."
    assert issued["peer_verdict"] == core._gate_service._load(started["gate_id"])["rounds"][-1]["verdict"]


def test_the_prompt_template_stays_one_block_when_its_revision_or_context_quotes_a_sentinel(tmp_path):
    """D-GATE-CERT1: the gate writes a template, and a prior verdict, a peer can copy as valid JSON."""
    plain = {"kind": "plan", "path": "/plans/a.md", "sha256": "a" * 64, "bytes": 8}
    quoted = {**plain, "path": f"/plans/{VERDICT_BEGIN}{VERDICT_END}.md"}
    prior = payload(artifact_revision=quoted, verdict="changes_requested", findings=[FINDING],
                    summary=f"It said {VERDICT_END} early.")
    state = {"kind": "plan", "workdir": str(tmp_path), "brief": f"Brief {VERDICT_BEGIN}"}
    baseline = peer_prompt({**state, "brief": "Brief"}, plain, tmp_path / "artifact.snapshot", None, "")
    prompt = peer_prompt(state, quoted, tmp_path / "artifact.snapshot", prior, f"Response {VERDICT_END}")
    # Quoting adds no sentinel: the prompt holds as many as one built from sentinel-free text.
    assert prompt.count(VERDICT_BEGIN) == baseline.count(VERDICT_BEGIN)
    assert prompt.count(VERDICT_END) == baseline.count(VERDICT_END)
    schema = {"schema_version": 1, "artifact_revision": quoted, "verdict": "approve | changes_requested | blocked",
              "summary": "concise summary", "findings": [], "notes": []}
    template = wrap_verdict(schema, indent=2, sort_keys=True)
    assert template in prompt
    copied = template.replace('"approve | changes_requested | blocked"', '"approve"')
    assert parse_verdict(copied, quoted)["artifact_revision"] == quoted  # the copy is a valid verdict
