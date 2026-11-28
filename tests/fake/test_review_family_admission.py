"""D-REVIEW-FAMILY1: admission cannot replace an independent reviewer."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from subfleet import daemon as daemon_module, scheduler, store as store_module
from subfleet.adapters import registry
from subfleet.contracts import ClockSource, Closure, ClosureReason, Reading, ReadingLabel
from subfleet.daemon import Daemon
from tests.fake.conftest import Harness
from tests.fake.test_routing_end_to_end import claude_lane
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.fake_adapter import FakeAdapter
from tests.unit.test_review_family_routing import baseline_routing


STAMP = "2026-11-13T16:00:00Z"


def review_args(harness, **changes):
    return harness.submit_args(**{"task": "review", "tier": "hard", "pinned_model": None, **changes})


def measured(service):
    for lane_id in ("claude-1", "codex-1"):
        service.store.add_reading(Reading(lane_id, "account", "seven_day", .2,
            "2026-12-01T16:00:00Z", ReadingLabel.PROVIDER, "fixture", daemon_module.utcnow()))


@pytest.mark.parametrize("family,missing", [("claude", "codex"), ("codex", "claude")])
def test_i2_admission_waits_without_other_family_and_override_permits_same_family(state_daemon, monkeypatch, family, missing):
    service, harness = state_daemon
    service.store.put_lane(claude_lane("claude-1"))
    measured(service)
    service.store.update_lane(f"{missing}-1", enabled=0)
    monkeypatch.setattr(service, "_launch", lambda *args: None)
    monkeypatch.setattr(registry, "_factories", {"claude": FakeAdapter, "codex": FakeAdapter})
    waiting = service.dispatch("submit", review_args(harness, caller_family=family))["job_id"]
    service._admit()
    assert not service.store.list_attempts(waiting)
    assert service.store.get_job(waiting)["state"] == "waiting"
    why = service.dispatch("why", {"job_id": waiting})
    assert why["decision"]["chosen_lane"] is None
    assert f"missing {missing} capacity" in why["text"]
    allowed = service.dispatch("submit", review_args(harness, caller_family=family,
                               allow_same_family=True))["job_id"]
    service._admit()
    assert service.store.get_job(allowed)["allow_same_family"] == 1
    assert [row["lane_id"] for row in service.store.list_attempts(allowed)] == [f"{family}-1"]
    assert not service.store.list_attempts(waiting)


@pytest.mark.parametrize("family,model", [("claude", "opus"), ("codex", "astra")])
def test_existing_same_family_pin_never_reserves_an_attempt(state_daemon, monkeypatch, family, model):
    service, harness = state_daemon
    service.store.put_lane(claude_lane("claude-1"))
    monkeypatch.setattr(service, "_launch", lambda *args: None)
    job_id = service.dispatch("submit", review_args(harness, pinned_model=model))["job_id"]
    # Emulate an old record or a retry pin: submit's explicit-pin refusal is
    # bypassed, so the scheduler itself must still preserve independence.
    service.store.update_job(job_id, caller_family=family)
    service._admit()
    assert not service.store.list_attempts(job_id)
    assert "requires a" in service.dispatch("why", {"job_id": job_id})["text"]


@pytest.mark.parametrize("task,family", [("review", None), ("build", None),
                                        ("build", "claude"), ("build", "codex")])
@pytest.mark.parametrize("closed", [False, True])
def test_i3_i4_durable_decision_rows_and_events_match_a2(state_daemon, tmp_path, monkeypatch, task, family, closed):
    """Compare actual admission records, including wait and placement events."""
    service, harness = state_daemon
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    legacy_harness = Harness(legacy_root)
    legacy = Daemon(legacy_root)
    frozen = datetime(2026, 11, 13, 16, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz else frozen.replace(tzinfo=None)

    monkeypatch.setattr(daemon_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(scheduler, "datetime", FrozenDateTime)
    monkeypatch.setattr(daemon_module, "utcnow", lambda: STAMP)
    monkeypatch.setattr(store_module, "utc_now", lambda: STAMP)
    monkeypatch.setattr(daemon_module.ids, "job_id", lambda *args, **kwargs: "comparison-job")
    monkeypatch.setattr(registry, "_factories", {"claude": FakeAdapter, "codex": FakeAdapter})
    before = baseline_routing()
    before["datetime"] = FrozenDateTime
    try:
        for target in (service, legacy):
            monkeypatch.setattr(target, "_launch", lambda *args: None)
            target.store.put_lane(claude_lane("claude-1"))
            measured(target)
            if closed:
                for lane_id in ("claude-1", "codex-1"):
                    target.store.add_closure(Closure(lane_id, "account", "2026-11-14T16:00:00Z",
                        ClosureReason.OPERATOR_HOLD, ClockSource.REPORTED, "fixture"))
        # Use the same request and source files for byte-comparable digests.
        args = harness.submit_args(request_id="comparison", task=task, tier="standard",
                                   caller_family=family, pinned_model=None)
        assert service.dispatch("submit", args) == legacy.dispatch("submit", args)
        service._admit()
        with monkeypatch.context() as old:
            old.setattr(scheduler, "evaluate", before["evaluate"])
            legacy._admit()

        def canonical(value):
            if isinstance(value, dict):
                return {key: canonical(item) for key, item in value.items()}
            if isinstance(value, list):
                return [canonical(item) for item in value]
            if isinstance(value, str):
                if value.startswith(("{", "[")):
                    try:
                        return canonical(json.loads(value))
                    except ValueError:
                        pass
                return value.replace(str(service.root), "<state>").replace(str(legacy.root), "<state>")
            return value

        current_rows = service.store.list_decisions("comparison-job")
        legacy_rows = legacy.store.list_decisions("comparison-job")
        assert current_rows and legacy_rows
        assert canonical(current_rows) == canonical(legacy_rows)
        assert canonical(service.store.query("SELECT * FROM events WHERE job_id='comparison-job' ORDER BY event_id")) == canonical(
            legacy.store.query("SELECT * FROM events WHERE job_id='comparison-job' ORDER BY event_id"))
    finally:
        legacy.close()
    legacy_harness.check_notices()
