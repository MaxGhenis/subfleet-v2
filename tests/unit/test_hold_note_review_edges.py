"""C-9.6: review counterexamples for mixed closures and legacy acknowledgements."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from subfleet import cli, daemon, protocol, render, scheduler
from subfleet.capacity import from_store
from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner
from subfleet.conversations import waits
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from subfleet.store import Store
from tests.caps import capped

NOW = datetime(2026, 10, 26, 16, 55, tzinfo=timezone.utc)
HELD_UNTIL = "2026-10-26T22:55:00Z"
LIMIT_UNTIL = "2026-10-26T23:55:00Z"


@pytest.fixture
def operator(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "utcnow", lambda: "2026-10-26T16:55:00Z")
    with Store(tmp_path / "state.db") as store:
        store.put_lane(Lane("codex-1", "codex", "codex:one",
                            Credential("codex", "/homes/codex-1", "home"),
                            "/homes/codex-1", LaneOwner.V2, False, True))
        yield SimpleNamespace(store=store, _notify=lambda: None, _desktop_identity=lambda: None,
                              _capacity_view=lambda _: from_store(store, now=NOW))


@pytest.mark.parametrize("hold_first", [True, False])
def test_conversation_retains_both_closures_when_their_ends_differ(operator, hold_first):
    """The stored closure order cannot hide either the limit or the operator hold."""
    rows = [Closure("codex-1", "account", HELD_UNTIL, ClosureReason.OPERATOR_HOLD,
                    ClockSource.REPORTED, "operator: ramp safety bound"),
            Closure("codex-1", "account", LIMIT_UNTIL, ClosureReason.PROVIDER_LIMIT,
                    ClockSource.REPORTED, "provider-event")]
    for row in rows if hold_first else reversed(rows):
        operator.store.add_closure(row)
    policy = capped(load_policy(DEFAULT_POLICY_PATH))
    policy["reserve"] = {**policy.get("reserve", {}), "models": []}
    job = {"job_id": "job", "task": None, "tier": "hard", "pinned_model": "astra",
           "sandbox": "read-only", "exclusions": []}
    decision = scheduler.evaluate(policy, from_store(operator.store, now=NOW), job)
    summary = waits.lane_summary(decision)
    text = waits.lanes_reason(summary, scheduler.dominant_rejection(decision))
    assert summary["lanes"] == {"closed:account": ["codex-1"]}, text
    assert summary["reset"] == LIMIT_UNTIL, text
    assert f"held by the operator: ramp safety bound (until {HELD_UNTIL} at the latest)" in text


@pytest.mark.parametrize("until", ["2026-10-26T22:55:00.500Z", "2026-10-26T22:55:00.000001+00:00"])
def test_legacy_263_acknowledgement_normalizes_the_requested_precision(operator, monkeypatch, capsys, until):
    """#263 stores whole seconds: applying a new hold is never a kept refresh."""
    def legacy_reply(_op, args):
        answer = daemon.Daemon._hold_lane(operator, protocol.LanesArgs(**args))
        answer.pop("kept")
        answer["note"] = args.get("note")
        return answer

    monkeypatch.setattr(cli, "_client", lambda _: SimpleNamespace(call=legacy_reply))
    assert cli.main(["lanes", "hold", "codex-1", "--until", until, "--note", "applied"]) == 0
    text = capsys.readouterr().out
    assert "held by the operator: applied" in text
    assert "kept" not in text, text


def test_a_pin_record_before_268_does_not_invent_a_credential_note():
    """#263 omitted source_event from pin evidence; absence is not an unsafe note."""
    old_record = {"lane_id": "codex-1", "reasons": [f"closed:account:{HELD_UNTIL}"],
                  "closures": [{"scope": "account", "until_at": HELD_UNTIL, "reason": "operator-hold"}]}
    text = render.pin_failure(old_record, "2026-10-26T16:55:00Z")
    assert "held by the operator" in text and "at the latest)" in text
    assert "would carry a credential" not in text, text
