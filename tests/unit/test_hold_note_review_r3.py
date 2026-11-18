"""C-9.6: independent r3 checks for exact closure orders and legacy precision.

Exercise ordinary and pinned entries with real scheduler evidence, compare the
complete explanations byte for byte, and include truncation across a second.
"""

from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from subfleet import cli, daemon, protocol, render, scheduler, status_json
from subfleet.capacity import from_store
from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, hold_note_of
from subfleet.conversations import waits
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from subfleet.store import Store
from tests.caps import capped

NOW = datetime(2026, 10, 26, 16, 55, tzinfo=timezone.utc)
END = "2026-10-26T22:55:00Z"
RESET = "2026-10-26T23:55:00Z"
NOTE = "ramp safety bound"


def put_lane(store):
    store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/homes/codex-1", "home"),
                        "/homes/codex-1", LaneOwner.V2, False, True))


def test_exact_review_input_is_byte_identical_at_both_conversation_entries(tmp_path):
    policy = capped(load_policy(DEFAULT_POLICY_PATH))
    policy["reserve"] = {**policy.get("reserve", {}), "models": []}
    # The default pin horizon exceeds these ends. Force the pinned-wait entry
    # while leaving the exact stored closures and ordinary decision unchanged.
    policy.setdefault("admission", {})["pin_hold_far_s"] = 3600
    job = {"job_id": "job", "task": None, "tier": "hard", "pinned_model": "astra",
           "sandbox": "read-only", "exclusions": []}
    answers = []
    for hold_first in (True, False):
        with Store(tmp_path / f"order-{hold_first}.db") as store:
            put_lane(store)
            rows = [Closure("codex-1", "account", END, ClosureReason.OPERATOR_HOLD,
                            ClockSource.REPORTED, f"operator: {NOTE}"),
                    Closure("codex-1", "account", RESET, ClosureReason.PROVIDER_LIMIT,
                            ClockSource.REPORTED, "provider-event")]
            for row in rows if hold_first else reversed(rows):
                store.add_closure(row)
            view = from_store(store, now=NOW)
            decision = scheduler.evaluate(policy, view, job)
            summary = waits.lane_summary(decision)
            ordinary = waits.hold_reason({"reason": scheduler.dominant_rejection(decision), "lanes": summary},
                                         describe=str, who=str)
            pin = scheduler.pin_unadmittable(policy, view, {**job, "pinned_lane": "codex-1"})
            assert pin is not None
            pinned = waits.hold_reason({"reason": "pin-unadmittable", **pin}, describe=str, who=str)
            assert ordinary == waits.lanes_reason(summary, scheduler.dominant_rejection(decision))
            for text in (ordinary, pinned):
                assert f"held by the operator: {NOTE} (until {END} at the latest)" in text
            assert "closed until 2026-10-26 23:55 UTC at the earliest" in ordinary
            assert f"provider-limit) until {RESET}" in pinned
            answers.append((ordinary.encode(), pinned.encode()))
            print(json.dumps({"hold_first": hold_first, "ordinary": ordinary, "pinned": pinned}))
    assert answers[0] == answers[1]


@pytest.mark.parametrize("asked", ["2026-10-26T22:55:00.500Z", "2026-10-26T22:55:00.000001+00:00",
                                  "2026-10-26T22:54:59.9Z"])
@pytest.mark.parametrize("before", [None, (END, "applied"), (END, "previous"),
                                    ("2026-10-26T21:00:00Z", "previous")])
@pytest.mark.parametrize("json_flag", [False, True])
def test_legacy_reply_truncates_without_rounding_or_inventing_a_kept_row(
        tmp_path, monkeypatch, capsys, asked, before, json_flag):
    monkeypatch.setattr(daemon, "utcnow", lambda: "2026-10-26T16:55:00Z")
    with Store(tmp_path / "legacy.db") as store:
        put_lane(store)
        operator = SimpleNamespace(store=store, _notify=lambda: None, _desktop_identity=lambda: None,
                                   _capacity_view=lambda _: from_store(store, now=NOW))
        if before:
            daemon.Daemon._hold_lane(operator, protocol.LanesArgs(
                action="hold", lane_id="codex-1", until=before[0], note=before[1]))

        def legacy_reply(_op, args):
            # Run storage with the exact pre-r3 conversion, independently of timestamp.
            def legacy_timestamp(value):
                return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            with monkeypatch.context() as old_storage:
                old_storage.setattr(daemon, "timestamp", legacy_timestamp)
                reply = daemon.Daemon._hold_lane(operator, protocol.LanesArgs(**args))
            reply.pop("kept")
            reply["note"] = args.get("note")
            return reply

        monkeypatch.setattr(cli, "_client", lambda _: SimpleNamespace(call=legacy_reply))
        assert cli.main(["lanes", "hold", "codex-1", "--until", asked, "--note", "applied"]
                        + (["--json"] if json_flag else [])) == 0
        text = capsys.readouterr().out
        [row] = store.list_closures()
        applied = (row["until_at"], hold_note_of(row["source_event"]))
        normalized = ("2026-10-26T22:54:59Z" if "22:54:59" in asked else END, "applied")
        expected = before if before and before[0] >= normalized[0] else normalized
        assert applied == expected
        kept = json.loads(text)["kept"] if json_flag else "were kept" in text
        assert kept == (applied != normalized)
        if kept:
            assert before == applied
        assert status_json.timestamp(asked) == normalized[0]


@pytest.mark.parametrize("source,withheld", [(None, True), ("", True), ("Operator: ramp", True),
                                           ("operator", False), ("operator: ramp", False)])
def test_present_pin_sources_have_no_gap_between_absent_and_invalid(source, withheld):
    row = {"scope": "account", "until_at": END, "reason": "operator-hold", "source_event": source}
    pin = {"lane_id": "codex-1", "reasons": [f"closed:account:{END}"], "closures": [row]}
    text = waits.hold_reason({"reason": "pin-unadmittable", **pin}, describe=str, who=str)
    assert ("[note withheld: it would carry a credential]" in text) == withheld
    row.pop("source_event")
    text = render.pin_refusals(pin)
    assert "withheld" not in text and "held by the operator (until" in text
