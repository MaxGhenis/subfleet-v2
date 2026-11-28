"""C-3.8, C-6.10: only committed admission verdicts suppress events and decisions."""

import json
import sqlite3

import pytest

from subfleet.contracts import ClockSource, Closure, ClosureReason
from subfleet.daemon import ADMISSION_EVENT, after
from subfleet.policy import load_policy
from tests.fake.test_capacity_wait_backoff import fleet  # noqa: F401
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401


@pytest.fixture
def full_fleet(fleet):  # noqa: F811
    service, harness = fleet
    policy_path = harness.root / "policy.json"
    policy = json.loads(policy_path.read_text())
    policy["caps"]["max_active_attempts"] = 1
    policy_path.write_text(json.dumps(policy))
    service.policy = load_policy(policy_path)
    holder = service.dispatch("submit", harness.submit_args())["job_id"]
    service._admit()
    assert service.store.list_attempts(holder)[0]["state"] == "reserved"
    job_id = service.dispatch("submit", harness.submit_args(tier="hard"))["job_id"]
    service._admit()
    assert service._capacity_waits[job_id]["label"] == "fleet-full"
    assert len(service.store.list_decisions(job_id)) == 1
    assert wait_count(service, job_id) == 1
    return service, job_id


def wait_count(service, job_id):
    return sum(row["kind"] == ADMISSION_EVENT for row in service.store.list_events(job_id))


def fail_new_verdict(service, job_id, monkeypatch, fault):
    """Change A to B while still full, then roll B back at three transaction stages."""
    service.store.put_closure(Closure("codex-1", "account", after(3600), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))
    service.store.update_job(job_id, next_check_at=after(-1))
    before = service.store.get_job(job_id)
    decisions = service.store.list_decisions(job_id)
    with monkeypatch.context() as patch:
        if fault == "decision":
            original = service.store.add_decision

            def insert_then_fail(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError("synthetic decision failure")

            patch.setattr(service.store, "add_decision", insert_then_fail)
            expected = RuntimeError
        elif fault == "audit":
            service.store.conn.execute(
                "CREATE TEMP TRIGGER fail_capacity_wait BEFORE INSERT ON events "
                "WHEN NEW.kind='job.capacity_waiting' "
                "BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END")
            expected = sqlite3.DatabaseError
        else:
            # Fail the actual COMMIT, after the decision, job UPDATE and audit INSERT.
            def deny(action, first, second, database, trigger):
                if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            service.store.conn.set_authorizer(deny)
            expected = sqlite3.DatabaseError
        try:
            with pytest.raises(expected):
                service._admit()
        finally:
            service.store.conn.set_authorizer(None)
            if fault == "audit":
                service.store.conn.execute("DROP TRIGGER fail_capacity_wait")
    assert service.store.get_job(job_id) == before
    assert service.store.list_decisions(job_id) == decisions
    assert wait_count(service, job_id) == 1


@pytest.mark.parametrize("fault", ["decision", "audit", "commit"])
def test_c3_8_a_rolled_back_verdict_does_not_consume_its_wait_event(full_fleet, monkeypatch, fault):
    """C-3.8 a valid full fleet retries B as a new wait after A, even after a failed COMMIT."""
    service, job_id = full_fleet
    previous = service._capacity_waits[job_id]
    fail_new_verdict(service, job_id, monkeypatch, fault)
    restored = service._capacity_waits.get(job_id)
    service._admit()
    assert wait_count(service, job_id) == 2
    assert restored == previous
    assert service._capacity_waits[job_id]["rechecks"] == 0
    assert service._capacity_waits[job_id]["signature"] != previous["signature"]
    for _ in range(3):
        service.store.update_job(job_id, next_check_at=after(-1))
        service._admit()
    assert wait_count(service, job_id) == 2


@pytest.mark.parametrize("fault", ["decision", "audit", "commit"])
def test_c6_10_a_rolled_back_verdict_does_not_consume_its_decision(full_fleet, monkeypatch, fault):
    """C-6.10 B's inserted but rolled-back decision must be inserted again on retry."""
    service, job_id = full_fleet
    previous = service._capacity_waits[job_id]
    fail_new_verdict(service, job_id, monkeypatch, fault)
    restored = service._capacity_waits.get(job_id)
    service._admit()
    assert len(service.store.list_decisions(job_id)) == 2
    assert restored == previous
    assert service._capacity_waits[job_id]["rechecks"] == 0
    assert service._capacity_waits[job_id]["signature"] != previous["signature"]
    for _ in range(3):
        service.store.update_job(job_id, next_check_at=after(-1))
        service._admit()
    assert len(service.store.list_decisions(job_id)) == 2
