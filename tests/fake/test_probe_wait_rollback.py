"""C-6.10: a rolled-back probe-wait verdict cannot consume its decision."""

from contextlib import contextmanager
import sqlite3

import pytest

from subfleet.contracts import ClockSource, Closure, ClosureReason
from subfleet.daemon import after
from tests.fake.test_probe_turn import CUT, Probes, codex_lane, make_due, stand_still, submit
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401


@contextmanager
def failed_probe_wait(service, monkeypatch, fault):
    """Fault only job.probe_waiting, leaving the probe's own transactions intact."""
    real_tx = service.store.transaction

    @contextmanager
    def transaction(kind="state.changed", **options):
        if kind != "job.probe_waiting":
            with real_tx(kind, **options) as tx:
                yield tx
            return
        with monkeypatch.context() as patch:
            if fault == "decision":
                add = service.store.add_decision

                def insert_then_fail(*args, **kwargs):
                    add(*args, **kwargs)
                    raise RuntimeError("synthetic probe decision failure")

                patch.setattr(service.store, "add_decision", insert_then_fail)
            elif fault == "audit":
                service.store.conn.execute(
                    "CREATE TEMP TRIGGER fail_probe_wait BEFORE INSERT ON events "
                    "WHEN NEW.kind='job.probe_waiting' "
                    "BEGIN SELECT RAISE(ABORT, 'synthetic probe audit failure'); END")
            else:
                def deny(action, first, second, database, trigger):
                    if action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT":
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                service.store.conn.set_authorizer(deny)
            try:
                with real_tx(kind, **options) as tx:
                    yield tx
            finally:
                service.store.conn.set_authorizer(None)
                if fault == "audit":
                    service.store.conn.execute("DROP TRIGGER fail_probe_wait")

    with monkeypatch.context() as patch:
        patch.setattr(service.store, "transaction", transaction)
        yield


@pytest.mark.parametrize("fault", [None, "decision", "audit", "commit"],
                         ids=["control", "decision", "audit", "commit"])
def test_c6_10_a_probe_wait_retries_its_decision_after_rollback(routing_state, monkeypatch, fault):  # noqa: F811
    """A committed A then failed B reaches two decisions, as the no-fault control does."""
    service, harness = routing_state
    stand_still(monkeypatch)
    job_id = submit(service, harness)
    probes = Probes(service, monkeypatch, [CUT] * 4)

    def probe_without_writer_lock(*args):
        assert service.store._lock._depth == 0
        return probes(*args)

    monkeypatch.setattr(service, "_execute_probe", probe_without_writer_lock)
    service._admit()
    assert len(service.store.list_decisions(job_id)) == 1
    previous = service._capacity_waits[job_id]
    assert previous["signature"].startswith("probe-wait:")
    service.store.put_lane(codex_lane(harness.root, "codex-2"))
    service.store.put_closure(Closure("codex-2", "account", after(3600), ClosureReason.PROVIDER_LIMIT,
                                      ClockSource.REPORTED, "fixture"))
    make_due(service, job_id)
    before = service.store.get_job(job_id)
    decisions = service.store.list_decisions(job_id)
    events = [row for row in service.store.list_events(job_id) if row["kind"] == "job.probe_waiting"]
    restored = previous
    if fault is not None:
        expected = RuntimeError if fault == "decision" else sqlite3.DatabaseError
        with failed_probe_wait(service, monkeypatch, fault), pytest.raises(expected):
            service._admit()
        restored = service._capacity_waits.get(job_id)
        assert service.store.get_job(job_id) == before
        assert service.store.list_decisions(job_id) == decisions
        assert [row for row in service.store.list_events(job_id)
                if row["kind"] == "job.probe_waiting"] == events
    for _ in range(2):
        make_due(service, job_id)
        service._admit()
    assert len(service.store.list_decisions(job_id)) == 2
    assert restored == previous
    if fault is not None:
        assert restored is previous
    assert service._capacity_waits[job_id]["signature"] != previous["signature"]
    assert service._capacity_waits[job_id]["rechecks"] == 1
