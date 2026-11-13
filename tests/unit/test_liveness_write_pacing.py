"""D-DAEMON-QUIET1: durable liveness stays bounded without a write per census."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from subfleet import daemon as daemon_module, procs
from subfleet.store import Store
from tests.unit.test_daemon_settle import (  # noqa: F401
    ATTEMPT, EMPTY, JOB, STARTED, attempt, daemon, publish_receipt, with_launch,
)


class Clock:
    def __init__(self, monkeypatch):
        self.seconds = 0.0
        self.wall_seconds = 0.0
        monkeypatch.setattr(daemon_module, "time", SimpleNamespace(monotonic=lambda: self.seconds))
        monkeypatch.setattr(daemon_module, "utcnow_ms", self.stamp)

    def stamp(self):
        at = datetime(2026, 10, 22, 11, tzinfo=timezone.utc) + timedelta(seconds=self.wall_seconds)
        return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def age(self, stamp):
        return (datetime.fromisoformat(self.stamp().replace("Z", "+00:00"))
                - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()

    def set(self, seconds, *, wall=None):
        self.seconds = seconds
        self.wall_seconds = seconds if wall is None else wall


def table(*, extra_owned=False, escaped=(4400,)):
    rows = {4242: (1, 4242, "S", STARTED), 4243: (4242, 4242, "S", STARTED)}
    if extra_owned:
        rows[4250] = (4242, 4242, "S", STARTED)
    rows.update({pid: (4243, pid, "S", STARTED) for pid in escaped})
    return procs.ProcessTable(rows, "boot")


def evidence(core):
    return json.loads(attempt(core)["evidence_json"])


def liveness_events(core):
    return [row for row in core.store.list_events(JOB) if row["kind"] == "attempt.observed_alive"]


def saved_liveness_age(core, clock):
    saved = datetime.fromisoformat(evidence(core)["last_observed_alive_at"].replace("Z", "+00:00"))
    current = datetime.fromisoformat(clock.stamp().replace("Z", "+00:00"))
    return (current - saved).total_seconds()


def test_liveness_writes_at_the_minute_boundary_and_keeps_writing(daemon, monkeypatch):
    clock = Clock(monkeypatch)
    daemon._record_owned(attempt(daemon), table())
    initial = clock.stamp()
    generation = daemon.store.generation
    for seconds in (1, 30, 59.999):
        clock.set(seconds)
        daemon._record_owned(attempt(daemon), table())
        assert evidence(daemon)["last_observed_alive_at"] == initial
        assert daemon.store.generation == generation
    for seconds in (60, 120, 180):
        clock.set(seconds)
        daemon._record_owned(attempt(daemon), table())
        assert evidence(daemon)["last_observed_alive_at"] == clock.stamp()
    assert len(liveness_events(daemon)) == 3



def test_ownership_write_resets_the_liveness_pace_with_newest_observation(daemon, monkeypatch):
    clock = Clock(monkeypatch)
    daemon._record_owned(attempt(daemon), table())
    clock.set(59)
    daemon._record_owned(attempt(daemon), table(extra_owned=True))
    assert evidence(daemon)["last_observed_alive_at"] == clock.stamp()
    clock.set(60)
    daemon._record_owned(attempt(daemon), table(extra_owned=True))
    assert not liveness_events(daemon)
    clock.set(119)
    daemon._record_owned(attempt(daemon), table(extra_owned=True))
    assert evidence(daemon)["last_observed_alive_at"] == clock.stamp()
    assert len(liveness_events(daemon)) == 1


def test_liveness_pacing_is_independent_for_each_attempt(daemon, monkeypatch):
    clock = Clock(monkeypatch)
    second_id = JOB + "/a2"
    row = attempt(daemon)
    second = {key: value for key, value in row.items() if key in daemon.store._columns["attempts"]}
    second.update(attempt_id=second_id, seq=2)
    daemon.store.add_attempt(second)
    daemon._record_owned(attempt(daemon), table())
    clock.set(30)
    daemon._record_owned(dict(daemon.store.get_attempt(second_id)), table())
    clock.set(60)
    daemon._record_owned(attempt(daemon), table())
    daemon._record_owned(dict(daemon.store.get_attempt(second_id)), table())
    assert evidence(daemon)["last_observed_alive_at"] == clock.stamp()
    second_saved = json.loads(daemon.store.get_attempt(second_id)["evidence_json"])["last_observed_alive_at"]
    assert second_saved != clock.stamp()
    clock.set(90)
    daemon._record_owned(dict(daemon.store.get_attempt(second_id)), table())
    assert json.loads(daemon.store.get_attempt(second_id)["evidence_json"])["last_observed_alive_at"] == clock.stamp()
    assert [row["attempt_id"] for row in liveness_events(daemon)] == [ATTEMPT, second_id]


def test_liveness_age_stays_within_one_minute_plus_inspection_interval(daemon, monkeypatch):
    clock = Clock(monkeypatch)
    daemon.inspect_interval_s = 1.3
    daemon._record_owned(attempt(daemon), table())
    for inspection in range(1, 301):
        clock.set(inspection * daemon.inspect_interval_s)
        daemon._record_owned(attempt(daemon), table())
        saved = datetime.fromisoformat(evidence(daemon)["last_observed_alive_at"].replace("Z", "+00:00"))
        current = datetime.fromisoformat(clock.stamp().replace("Z", "+00:00"))
        assert 0 <= (current - saved).total_seconds() <= 60 + daemon.inspect_interval_s
    assert len(liveness_events(daemon)) == 6


def test_liveness_wall_clock_rollback_never_moves_saved_observation_backwards(daemon, monkeypatch):
    clock = Clock(monkeypatch)
    clock.set(100)
    daemon._record_owned(attempt(daemon), table())
    saved = evidence(daemon)["last_observed_alive_at"]
    clock.set(160, wall=95)
    daemon._record_owned(attempt(daemon), table())
    assert evidence(daemon)["last_observed_alive_at"] == saved
    # Ownership changes still commit, and must respect the same durable floor.
    clock.set(161, wall=94)
    daemon._record_owned(attempt(daemon), table(extra_owned=True))
    assert evidence(daemon)["last_observed_alive_at"] == saved
    assert "4250" in evidence(daemon)["owned_identities"]
    clock.set(222, wall=101)
    daemon._record_owned(attempt(daemon), table(extra_owned=True))
    assert evidence(daemon)["last_observed_alive_at"] == clock.stamp()


def test_rollback_floor_can_increase_lost_charge_relative_to_old_implementation(
        daemon, monkeypatch, tmp_path):
    clock = Clock(monkeypatch)
    monkeypatch.setattr(daemon_module, "age", clock.age)
    daemon.store.update_attempt(ATTEMPT, started_at=clock.stamp())
    reference_store = Store(tmp_path / "reference.sqlite3")
    daemon.store.connection.backup(reference_store.connection)
    # _lost records the charge endpoint before finalization; isolate that
    # decision from stream capture, salvage and terminal-state publication.
    reference = SimpleNamespace(store=reference_store, _finalize=lambda *args, **kwargs: None)
    monkeypatch.setattr(daemon, "_finalize", lambda *args, **kwargs: None)
    try:
        for monotonic, wall in ((100, 100), (160, 95)):
            clock.set(monotonic, wall=wall)
            snapshot = table()
            _record_owned_before_pacing(reference, attempt(reference), snapshot)
            daemon._record_owned(attempt(daemon), snapshot)
        clock.set(170, wall=110)
        daemon_module.Daemon._lost(reference, attempt(reference), census=EMPTY)
        daemon._lost(attempt(daemon), census=EMPTY)
        assert daemon_module.Daemon._running_s(reference, JOB) == 95
        assert daemon._running_s(JOB) == 100
        assert attempt(reference)["finished_at"] == evidence(reference)["last_observed_alive_at"]
        assert attempt(daemon)["finished_at"] == evidence(daemon)["last_observed_alive_at"]
    finally:
        reference_store.close()


def test_rollback_future_saved_observation_caps_lost_charge_at_current_wall_time(daemon, monkeypatch):
    clock = Clock(monkeypatch)
    monkeypatch.setattr(daemon_module, "age", clock.age)
    daemon.store.update_attempt(ATTEMPT, started_at=clock.stamp())
    monkeypatch.setattr(daemon, "_finalize", lambda *args, **kwargs: None)
    clock.set(100)
    daemon._record_owned(attempt(daemon), table())
    saved = evidence(daemon)["last_observed_alive_at"]
    clock.set(101, wall=90)
    daemon._record_owned(attempt(daemon), table())
    assert evidence(daemon)["last_observed_alive_at"] == saved
    assert clock.age(saved) == -10
    daemon._lost(attempt(daemon), census=EMPTY)
    assert attempt(daemon)["finished_at"] == clock.stamp()
    assert daemon._running_s(JOB) == 90


@pytest.mark.parametrize("refresh_after_step", [False, True], ids=["before-refresh", "after-refresh"])
def test_forward_wall_clock_step_has_no_minute_freshness_or_charge_bound(
        daemon, monkeypatch, refresh_after_step):
    clock = Clock(monkeypatch)
    monkeypatch.setattr(daemon_module, "age", clock.age)
    daemon.store.update_attempt(ATTEMPT, started_at=clock.stamp())
    monkeypatch.setattr(daemon, "_finalize", lambda *args, **kwargs: None)
    daemon._record_owned(attempt(daemon), table())
    # Monotonic time advances normally, while the observation's wall clock
    # jumps forward. Pacing therefore cannot give a wall-clock freshness bound.
    clock.set(1, wall=3600)
    daemon._record_owned(attempt(daemon), table())
    assert saved_liveness_age(daemon, clock) == 3600
    assert not liveness_events(daemon)
    if refresh_after_step:
        clock.set(60, wall=3659)
        daemon._record_owned(attempt(daemon), table())
        assert saved_liveness_age(daemon, clock) == 0
        assert len(liveness_events(daemon)) == 1
    daemon._lost(attempt(daemon), census=EMPTY)
    # A loss before refresh retains the old endpoint. A loss after refresh
    # includes the wall-clock step in its charge, despite only 60 monotonic s.
    assert daemon._running_s(JOB) == (3659 if refresh_after_step else 0)
    assert attempt(daemon)["finished_at"] == evidence(daemon)["last_observed_alive_at"]


def test_liveness_first_fresh_observation_after_restart_writes(daemon, monkeypatch):
    clock = Clock(monkeypatch)
    daemon._record_owned(attempt(daemon), table())
    clock.set(1)
    daemon._record_owned(attempt(daemon), table())
    assert not liveness_events(daemon)
    # Restart forgets both table identities and pace, but retains durable evidence.
    daemon._counted_tables.clear()
    daemon._last_observed_alive_write.clear()
    clock.set(2)
    daemon._record_owned(attempt(daemon), table())
    assert evidence(daemon)["last_observed_alive_at"] == clock.stamp()
    assert len(liveness_events(daemon)) == 1


def test_failed_liveness_commit_retries_the_same_table_and_observation(daemon, monkeypatch):
    clock = Clock(monkeypatch)
    daemon._record_owned(attempt(daemon), table())
    original_pace = dict(daemon._last_observed_alive_write)
    saved = evidence(daemon)
    snapshot = table()
    clock.set(60)
    real_transaction = daemon.store.transaction

    @contextmanager
    def fail_at_commit(kind="state.changed", **kwargs):
        with real_transaction(kind, **kwargs) as tx:
            yield tx
            if kind == "attempt.observed_alive":
                raise sqlite3.OperationalError("injected commit refusal")

    monkeypatch.setattr(daemon.store, "transaction", fail_at_commit)
    a = attempt(daemon)
    with pytest.raises(sqlite3.OperationalError, match="injected commit refusal"):
        daemon._record_owned(a, snapshot)
    assert evidence(daemon) == saved
    assert json.loads(a["evidence_json"]) == saved
    assert daemon._last_observed_alive_write == original_pace
    assert daemon._counted_tables[ATTEMPT] is not snapshot
    monkeypatch.setattr(daemon.store, "transaction", real_transaction)
    daemon._record_owned(a, snapshot)
    assert evidence(daemon)["last_observed_alive_at"] == clock.stamp()
    assert len(liveness_events(daemon)) == 1
    assert daemon._counted_tables[ATTEMPT] is snapshot



@pytest.mark.parametrize(
    ("kind", "changed"),
    [("attempt.processes_recorded", {"extra_owned": True}),
     ("attempt.counted_recorded", {"escaped": (4400, 4401)})],
    ids=["owned", "counted"],
)
def test_failed_identity_commit_does_not_consume_pace(daemon, monkeypatch, kind, changed):
    clock = Clock(monkeypatch)
    daemon._record_owned(attempt(daemon), table())
    original_pace = dict(daemon._last_observed_alive_write)
    saved = evidence(daemon)
    previous_table = daemon._counted_tables[ATTEMPT]
    snapshot = table(**changed)
    clock.set(59)
    real_transaction = daemon.store.transaction

    @contextmanager
    def fail_at_commit(event_kind="state.changed", **kwargs):
        with real_transaction(event_kind, **kwargs) as tx:
            yield tx
            if event_kind == kind:
                raise sqlite3.OperationalError("injected identity commit refusal")

    monkeypatch.setattr(daemon.store, "transaction", fail_at_commit)
    a = attempt(daemon)
    with pytest.raises(sqlite3.OperationalError, match="injected identity commit refusal"):
        daemon._record_owned(a, snapshot)
    assert daemon._last_observed_alive_write == original_pace
    assert evidence(daemon) == saved
    assert json.loads(a["evidence_json"]) == saved
    assert daemon._counted_tables[ATTEMPT] is previous_table

    monkeypatch.setattr(daemon.store, "transaction", real_transaction)
    daemon._record_owned(a, snapshot)
    assert evidence(daemon)["last_observed_alive_at"] == clock.stamp()
    assert daemon._last_observed_alive_write[ATTEMPT] == 59
    assert daemon._counted_tables[ATTEMPT] is snapshot
    clock.set(60)
    daemon._record_owned(attempt(daemon), table(**changed))
    assert not liveness_events(daemon)
    clock.set(119)
    daemon._record_owned(attempt(daemon), table(**changed))
    assert evidence(daemon)["last_observed_alive_at"] == clock.stamp()
    assert len(liveness_events(daemon)) == 1


def test_liveness_pace_starts_at_observation_before_a_slow_successful_commit(daemon, monkeypatch):
    clock = Clock(monkeypatch)
    monkeypatch.setattr(daemon_module, "age", clock.age)
    daemon.inspect_interval_s = 1
    daemon.store.update_attempt(ATTEMPT, started_at=clock.stamp())
    daemon._record_owned(attempt(daemon), table())
    real_transaction = daemon.store.transaction
    delayed = False

    @contextmanager
    def slow_commit(kind="state.changed", **kwargs):
        nonlocal delayed
        with real_transaction(kind, **kwargs) as tx:
            yield tx
            if kind == "attempt.observed_alive" and not delayed:
                delayed = True
                clock.set(70)  # The observation at 60 does not commit until 70.

    monkeypatch.setattr(daemon.store, "transaction", slow_commit)
    clock.set(60)
    daemon._record_owned(attempt(daemon), table())
    assert len(liveness_events(daemon)) == 1
    assert saved_liveness_age(daemon, clock) == 10
    for seconds in range(71, 130):
        clock.set(seconds)
        daemon._record_owned(attempt(daemon), table())
        assert saved_liveness_age(daemon, clock) <= 60 + daemon.inspect_interval_s
    assert len(liveness_events(daemon)) == 2
    assert saved_liveness_age(daemon, clock) == 9
    monkeypatch.setattr(daemon, "_finalize", lambda *args, **kwargs: None)
    daemon._lost(attempt(daemon), census=EMPTY)
    assert daemon._running_s(JOB) == 120


def test_replacing_commit_latency_is_included_in_saved_observation_age(daemon, monkeypatch):
    clock = Clock(monkeypatch)
    daemon.inspect_interval_s = 1
    daemon._record_owned(attempt(daemon), table())
    real_transaction = daemon.store.transaction
    durable_ages = []
    reader = sqlite3.connect(daemon.store.path.resolve().as_uri() + "?mode=ro", uri=True)

    @contextmanager
    def slow_commit(kind="state.changed", **kwargs):
        with real_transaction(kind, **kwargs) as tx:
            yield tx
            if kind == "attempt.observed_alive":
                clock.set(70 if not durable_ages else 129)
                saved = json.loads(reader.execute(
                    "SELECT evidence_json FROM attempts WHERE attempt_id=?", (ATTEMPT,)
                ).fetchone()[0])["last_observed_alive_at"]
                age = (datetime.fromisoformat(clock.stamp().replace("Z", "+00:00"))
                       - datetime.fromisoformat(saved.replace("Z", "+00:00"))).total_seconds()
                durable_ages.append(age)
                # The old observation remains durable while its replacement
                # commits, so that write's latency belongs in the bound.
                assert age <= 60 + daemon.inspect_interval_s + 10
                if len(durable_ages) == 2:
                    clock.set(130)

    monkeypatch.setattr(daemon.store, "transaction", slow_commit)
    try:
        clock.set(60)
        daemon._record_owned(attempt(daemon), table())
        for seconds in range(71, 121):
            clock.set(seconds)
            daemon._record_owned(attempt(daemon), table())
        assert durable_ages == [70, 69]
        assert saved_liveness_age(daemon, clock) == 10
        assert len(liveness_events(daemon)) == 2
    finally:
        reader.close()


def test_repeated_table_cannot_advance_saved_liveness_even_after_minute(daemon, monkeypatch):
    clock = Clock(monkeypatch)
    first = table()
    daemon._record_owned(attempt(daemon), first)
    saved = evidence(daemon)
    pace = dict(daemon._last_observed_alive_write)
    generation = daemon.store.generation
    clock.set(180)
    daemon._record_owned(attempt(daemon), first)
    assert evidence(daemon) == saved
    assert daemon._last_observed_alive_write == pace
    assert daemon.store.generation == generation
    daemon._record_owned(attempt(daemon), table())
    assert evidence(daemon)["last_observed_alive_at"] == clock.stamp()
    assert len(liveness_events(daemon)) == 1


@pytest.mark.parametrize("ending", ["succeeded", "lost", "quarantined"])
def test_ended_attempts_leave_no_liveness_pacing_entry(daemon, monkeypatch, ending):
    clock = Clock(monkeypatch)
    daemon._record_owned(attempt(daemon), table())
    assert ATTEMPT in daemon._last_observed_alive_write
    if ending == "quarantined":
        daemon._quarantine(attempt(daemon), EMPTY, "test ending")
    else:
        with_launch(daemon, monkeypatch)
        daemon._contain = lambda a: EMPTY
        if ending == "succeeded":
            publish_receipt(daemon, rc=0)
            daemon._finalize(attempt(daemon))
        else:
            daemon._lost(attempt(daemon), census=EMPTY)
    assert attempt(daemon)["state"] == ending
    assert ATTEMPT not in daemon._last_observed_alive_write
    # The control loop also prunes stale entries, preserving a different live attempt.
    daemon._last_observed_alive_write.update({ATTEMPT: 0.0, "still-live/a1": 1.0})
    daemon._forget_paced({"still-live/a1"})
    assert daemon._last_observed_alive_write["still-live/a1"] == 1.0
    assert ATTEMPT not in daemon._last_observed_alive_write


def test_terminal_attempt_worker_also_drops_liveness_pacing_entry(daemon, monkeypatch):
    Clock(monkeypatch)
    daemon._record_owned(attempt(daemon), table())
    with daemon.store.transaction("test.finished") as tx:
        tx.execute("UPDATE attempts SET state='succeeded' WHERE attempt_id=?", (ATTEMPT,))
    daemon._process_attempt(ATTEMPT)
    assert ATTEMPT not in daemon._last_observed_alive_write


def test_ownership_events_match_the_original_flow_and_coalesce_fresh_observations(
        daemon, monkeypatch, tmp_path):
    clock = Clock(monkeypatch)
    reference_store = Store(tmp_path / "reference.sqlite3")
    daemon.store.connection.backup(reference_store.connection)
    reference = SimpleNamespace(store=reference_store)
    snapshots = [table(), table(extra_owned=True, escaped=(4400, 4401)),
                 table(extra_owned=True, escaped=(4401,)),
                 table(extra_owned=True, escaped=(4401,)),
                 table(extra_owned=True, escaped=(4401,))]
    try:
        for seconds, snapshot in enumerate(snapshots):
            clock.set(seconds)
            _record_owned_before_pacing(reference, dict(reference.store.get_attempt(ATTEMPT)), snapshot)
            daemon._record_owned(attempt(daemon), snapshot)
            if seconds < 4:
                assert evidence(daemon)["last_observed_alive_at"] == clock.stamp()
        kinds = {"attempt.processes_recorded", "attempt.counted_recorded"}
        owned_events = lambda core: [(row["kind"], row["data_json"])
                                    for row in core.store.list_events(JOB) if row["kind"] in kinds]
        expected = owned_events(reference)
        assert [kind for kind, _ in expected] == [
            "attempt.processes_recorded", "attempt.counted_recorded",
            "attempt.processes_recorded", "attempt.counted_recorded",
            "attempt.counted_recorded", "attempt.counted_recorded",
        ]
        assert owned_events(daemon) == expected
        # No standalone observation is needed while ownership transactions carry it.
        assert not liveness_events(daemon)
    finally:
        reference_store.close()


# Frozen pre-change implementation: the differential fixture checks event count,
# order and payload against the prior ownership/absence flow.
def _record_owned_before_pacing(self, a: dict, table: procs.ProcessTable) -> None:
    """C-5.6, C-5.12: remember liveness, group ownership and walk evidence.

    A matching guardian birth confirms liveness even after it leaves the
    recorded group. Only group members observed while it still leads that
    group gain ownership. Other live walk members are counted evidence, so
    self-exit or a turn interrupt cannot erase a hidden-marker runner before
    finalization's first census.
    """
    guardian = a["guardian_pid"]
    if not table.is_process(guardian, a["boot_id"], a["proc_start"], legacy=True):
        return
    evidence = json.loads(a["evidence_json"] or "{}")
    members = ({pid: table.identity(pid) for pid in table.group(a["pgid"])}
               if table.rows[guardian][1] == a["pgid"] else {})
    owned = dict(evidence.get("owned_identities", {}))
    fresh_owned = {str(pid): dataclasses.asdict(ident) for pid, ident in members.items()
                   if ident and owned.get(str(pid)) != dataclasses.asdict(ident)}
    owned.update(fresh_owned)
    roots = {guardian}
    for kept in (owned, evidence.get("responsible_identities", {}), evidence.get("counted_identities", {})):
        for pid_text, known in kept.items():
            pid = int(pid_text)
            row = table.rows.get(pid)
            if ((not table.live(pid) and (row is None or row[3] == known.get("proc_start")))
                    or table.is_process(pid, known.get("boot_id"), known.get("proc_start"), legacy=True)):
                roots.add(pid)
    counted = {}
    for pid in table.descendants(roots) - members.keys() - {guardian}:
        ident = table.identity(pid)
        if ident is None:
            continue
        value = dataclasses.asdict(ident)
        if all(kept.get(str(pid)) != value for kept in
               (owned, evidence.get("responsible_identities", {}), evidence.get("counted_identities", {}))):
            counted[str(pid)] = value
    # Two distinct shared tables must prove absence/reuse. An unreadable
    # birth is no such proof. Persist the first miss beside its birth so
    # a restart cannot turn repeated short inspections into growing memory.
    # Only table deduplication is in memory; a restart reads a new table.
    history = getattr(self, '_counted_tables', None)
    if history is None:
        history = self._counted_tables = {}
    previous = evidence.get('counted_absent', {})
    previous_table = history.get(a['attempt_id'])
    # D-WALL1 needs a durable liveness bound for every fresh observation.
    # Retrying the same shared table must neither move that bound nor age
    # a counted identity twice, even if the clock has advanced meanwhile.
    observed = daemon_module.utcnow_ms() if previous_table is not table else None
    wrote = False
    absent = {}
    expired = {}
    aging = False
    if previous_table is not table:
        for pid_text, known in evidence.get('counted_identities', {}).items():
            pid = int(pid_text)
            row = table.rows.get(pid)
            if (not table.live(pid) or (row[3] and not table.is_process(
                    pid, known.get('boot_id'), known.get('proc_start'), legacy=True))):
                absent[pid_text] = known
                if previous.get(pid_text) == known:
                    expired[pid_text] = known
        absent = {pid: value for pid, value in absent.items() if pid not in expired}
        aging = absent != previous
    for key, kind, found in (("owned_identities", "attempt.processes_recorded", fresh_owned),
                              ("counted_identities", "attempt.counted_recorded", counted)):
        dropping = expired if key == 'counted_identities' else {}
        age_changed = key == 'counted_identities' and aging
        if not found and not dropping and not age_changed:
            continue
        with self.store.transaction(kind, job_id=a["job_id"], attempt_id=a["attempt_id"],
                                    data={"pids": sorted(map(int, found)),
                                          **({'expired_pids': sorted(map(int, dropping))} if dropping else {})}) as tx:
            actual = self.store.get_attempt(a["attempt_id"]) or a
            current = json.loads(actual.get("evidence_json") or "{}")
            # The deciding census already copied its seeds before asking
            # the OS. Never mutate that snapshot, or drop a newer birth.
            current[key] = {pid: value for pid, value in current.get(key, {}).items()
                            if dropping.get(pid) != value}
            current[key].update(found)
            if observed is not None:
                current['last_observed_alive_at'] = observed
            if key == 'counted_identities' and previous_table is not table:
                current['counted_absent'] = {pid: value for pid, value in absent.items()
                                            if current[key].get(pid) == value and pid not in found}
                if not current['counted_absent']:
                    current.pop('counted_absent')
            a["evidence_json"] = json.dumps(current)
            tx.execute("UPDATE attempts SET evidence_json=? WHERE attempt_id=?",
                       (a["evidence_json"], a["attempt_id"]))
        wrote = True
    if not wrote and observed is not None and evidence.get('last_observed_alive_at') != observed:
        with self.store.transaction('attempt.observed_alive', job_id=a['job_id'],
                                    attempt_id=a['attempt_id']) as tx:
            actual = self.store.get_attempt(a['attempt_id']) or a
            current = json.loads(actual.get('evidence_json') or '{}')
            current['last_observed_alive_at'] = observed
            a['evidence_json'] = json.dumps(current)
            tx.execute('UPDATE attempts SET evidence_json=? WHERE attempt_id=?',
                       (a['evidence_json'], a['attempt_id']))
    # A failed write must repeat this observation, rather than publishing
    # a table whose first-miss state never reached the store.
    history[a['attempt_id']] = table
