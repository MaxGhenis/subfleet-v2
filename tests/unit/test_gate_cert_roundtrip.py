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

from subfleet.gate.certificate import certificate
from subfleet.gate.errors import GateError
from subfleet.gate.service import dispatch
from subfleet.gate.verdict import VERDICT_BEGIN, VERDICT_END, parse_verdict
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
