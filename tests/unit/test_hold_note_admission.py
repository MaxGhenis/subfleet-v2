"""C-9.6: real hold/refresh/release sequences are blind to explanatory notes."""

import tempfile
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hypothesis import example, given, settings, strategies as st

from subfleet import actions, daemon, protocol, scheduler, status_json
from subfleet.capacity import from_store
from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, Reading, ReadingLabel
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from subfleet.store import Store
from tests.caps import capped

START = datetime(2026, 10, 26, 16, 55, tzinfo=timezone.utc)
POLICY = capped(load_policy(DEFAULT_POLICY_PATH))
POLICY["reserve"] = {**POLICY.get("reserve", {}), "models": []}
JOB = {"job_id": "job", "task": None, "tier": "hard", "pinned_model": "astra", "sandbox": "read-only",
       "exclusions": []}
STEPS = st.lists(st.tuples(st.sampled_from(["hold", "release", "limit", "tick"]),
                           st.sampled_from([1, 3, 6, 24 * 40]),
                           st.text(alphabet="abc def:;!é", max_size=24)), min_size=1, max_size=6)


def iso(at):
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def without_notes(value):
    """Only explanatory fields may differ in admission's returned evidence."""
    if isinstance(value, dict):
        return {key: without_notes(item) for key, item in value.items() if key not in ("source_event", "note")}
    if isinstance(value, (list, tuple)):
        return [without_notes(item) for item in value]
    return value


@settings(max_examples=120, deadline=None, database=None, derandomize=True)
@example(steps=[("hold", 6, "first"), ("hold", 3, "earlier"), ("hold", 6, "equal"),
                ("hold", 24 * 40, "later"), ("release", 1, ""), ("limit", 3, "stray")])
@example(steps=[("hold", 24 * 40, "admit"), ("limit", 24 * 40, "stray")])
@example(steps=[("limit", 6, "stray"), ("hold", 3, "wait"), ("release", 1, "")])
@example(steps=[("hold", 3, "wait"), ("tick", 6, ""), ("hold", 1, "again")])
@given(steps=STEPS)
def test_real_store_sequences_admission_is_blind_to_notes(steps):
    """Compare all eight consumers after every step, including unstripped verdict signatures."""
    now = START
    with tempfile.TemporaryDirectory(prefix="hold-note-admission-") as directory, ExitStack() as stack:
        stack.enter_context(patch("subfleet.store.utc_now", lambda: iso(now)))
        stores = [stack.enter_context(Store(Path(directory) / f"{index}.db")) for index in range(2)]
        for store in stores:
            store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/homes/codex-1", "home"),
                                "/homes/codex-1", LaneOwner.V2, False, True))
        operators = [SimpleNamespace(store=store, _notify=lambda: None, _desktop_identity=lambda: None,
                      _capacity_view=lambda _, store=store: from_store(store, now=now)) for store in stores]
        for index, (kind, hours, words) in enumerate(steps):
            if kind == "tick":
                now += timedelta(hours=hours)
            else:
                until = iso(now + timedelta(hours=hours))
                for annotated, operator in enumerate(operators):
                    note = f"step {index}: {words}" if annotated else None
                    if kind in ("hold", "release"):
                        with patch.object(daemon, "utcnow", lambda: iso(now)):
                            daemon.Daemon._hold_lane(operator, protocol.LanesArgs(
                                action=kind, lane_id="codex-1", until=until, note=note))
                    else:
                        # A stray note on a provider row must not change any consumer either.
                        operator.store.add_closure(Closure("codex-1", "account", until,
                            ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED,
                            f"operator: {note}" if annotated else "provider-event"))
            for store in stores:
                store.add_reading(Reading("codex-1", "account", "seven_day", .2,
                    iso(now + timedelta(days=2)), ReadingLabel.PROVIDER, "wham", iso(now)))
            views = [from_store(store, now=now) for store in stores]
            held = [actions.store_holds(store, now=now) for store in stores]
            assert held[0] == held[1]
            for pinned in (False, True):
                job = {**JOB, **({"pinned_lane": "codex-1"} if pinned else {})}
                decisions = [scheduler.evaluate(POLICY, view, job) for view in views]
                assert without_notes(asdict(decisions[0])) == without_notes(asdict(decisions[1]))
                assert scheduler.dominant_rejection(decisions[0]) == scheduler.dominant_rejection(decisions[1])
                assert scheduler.verdict_signature(decisions[0]) == scheduler.verdict_signature(decisions[1])
                pins = [scheduler.pin_unadmittable(POLICY, view, job) for view in views]
                assert without_notes(pins[0]) == without_notes(pins[1])
                if "codex-1" in held[0]:
                    assert all(decision.chosen_lane is None for decision in decisions)
            lanes = [view["lanes"][0] for view in views]
            assert actions.lane_hold(lanes[0], now=now, held=held[0]) == actions.lane_hold(
                lanes[1], now=now, held=held[1])
            assert status_json.dispatchable(lanes[0]) == status_json.dispatchable(lanes[1])
            assert status_json.lane_verdict(lanes[0]) == status_json.lane_verdict(lanes[1])
