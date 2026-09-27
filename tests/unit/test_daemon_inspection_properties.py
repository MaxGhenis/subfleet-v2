"""C-5.11 invariants, for every input rather than one example.

- Pacing: a running attempt's guardian is asked about exactly on the ticks a
  greedy clock allows (the first tick, then the first tick at least one
  interval after the last question), whatever the tick times.
- Recording: the members asked about again are exactly those not recorded
  under their current start and boot identity; what is recorded is always the
  identity `procs.identity` gives.
- Exports: the one-statement query and the per-job sweep it replaced find the
  same jobs, in the same order, for any jobs and leases (a differential test).
- Generation: it counts exactly the top-level transactions that committed a
  change, and never moves backwards.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet import daemon as daemon_module
from subfleet import procs
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.daemon import Daemon
from subfleet.store import Store

JOB = "20260924-114650-properties"
ATTEMPT = JOB + "/a1"
BOOT = "0f1e2d3c-4b5a-4968-8776-655443322110"      # synthetic
OTHER_BOOT = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
LEGACY_BOOT = "1790255587"
STARTS = ["Thu Sep 24 11:46:51 2026", "Thu Sep 24 11:52:07 2026", "Sat Sep  5 10:00:00 2026"]
FIXTURE_HEALTH = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow]


@pytest.fixture
def core(tmp_path, monkeypatch):
    daemon = Daemon(tmp_path / "state")
    monkeypatch.setattr(procs, "_BOOT_ID", [])
    home = tmp_path / "home"
    daemon.store.put_lane(Lane("codex-1", "codex", "codex:test", Credential("codex", str(home), "home"),
                               str(home), LaneOwner.V2, False))
    daemon.store.add_job(job_id=JOB, request_id="p-1", payload_digest="d", kind="run", state="running",
                         workdir=str(tmp_path), prompt_path=str(tmp_path / "p.md"), sandbox="read-only")
    daemon.store.add_attempt(attempt_id=ATTEMPT, job_id=JOB, seq=1, lane_id="codex-1",
                             model_requested="astra", state="running", guardian_pid=4242,
                             child_pid=4243, pgid=4242, boot_id=BOOT, proc_start=STARTS[0],
                             started_at="2026-09-24T11:46:51Z", evidence_json="{}")
    yield daemon
    daemon.close()


class Clock:
    """Stands in for the `time` module inside `subfleet.daemon` only."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        import time
        return getattr(time, name)


@settings(max_examples=60, deadline=None, suppress_health_check=FIXTURE_HEALTH)
@given(gaps=st.lists(st.floats(min_value=0.0, max_value=0.4, allow_nan=False), min_size=1, max_size=120),
       interval=st.sampled_from([0.25, 0.5, 1.0, 2.0]))
def test_pacing_asks_exactly_when_a_greedy_clock_allows(core, monkeypatch, gaps, interval):
    clock = Clock()
    monkeypatch.setattr(daemon_module, "time", clock)
    monkeypatch.setattr(core, "_record_owned", lambda a: None)
    asked = []
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: asked.append(clock.now) or "alive")
    core.liveness_interval_s = interval
    core._liveness_next.clear()
    core._census_next.clear()

    expected, last = [], None
    for gap in gaps:
        clock.now += gap
        core._process_attempt(ATTEMPT)
        if last is None or clock.now >= last + interval:
            expected.append(clock.now)
            last = clock.now
    assert asked == expected
    assert all(b >= a + interval for a, b in zip(asked, asked[1:]))   # the gate's own arithmetic


identities = st.fixed_dictionaries({"proc_start": st.sampled_from(STARTS),
                                    "boot_id": st.sampled_from([BOOT, OTHER_BOOT, LEGACY_BOOT])})
# What `procs.identity` answers for a pid, independent of the snapshot: a start
# (the snapshot's, or one a new process has since given the pid), no process, or
# an inspection failure.
answers = st.one_of(st.sampled_from(STARTS), st.none(), st.just("error"))


@settings(max_examples=300, deadline=None, suppress_health_check=FIXTURE_HEALTH)   # every patch is re-set per input
@given(members=st.dictionaries(st.integers(4242, 4250), st.sampled_from(STARTS), max_size=9),
       recorded=st.dictionaries(st.integers(4240, 4252), identities, max_size=13),
       answered=st.dictionaries(st.integers(4242, 4250), answers, max_size=9),
       booted=st.sampled_from([BOOT, LEGACY_BOOT]))
def test_recording_asks_exactly_about_members_not_recorded_as_they_are(monkeypatch, members, recorded,
                                                                         answered, booted):
    monkeypatch.setattr(procs, "group_members", lambda pgid: dict(members))
    monkeypatch.setattr(procs, "boot_id", lambda: booted)   # a UUID machine, or a legacy-only one
    asked = []

    def identity(pid):
        asked.append(pid)
        answer = answered.get(pid, members[pid])
        if answer == "error":
            raise procs.InspectionError("ps unavailable")
        return None if answer is None else procs.ProcessIdentity(pid, booted, answer)

    monkeypatch.setattr(procs, "identity", identity)
    as_recorded = {str(pid): {"pid": pid, **value} for pid, value in recorded.items()}

    fresh = Daemon._new_group_identities(4242, as_recorded)

    stale = {pid for pid, started in members.items()
             if not (pid in recorded and recorded[pid] == {"proc_start": started, "boot_id": booted})}
    assert set(asked) == stale and len(asked) == len(stale)
    # What is recorded is what `identity` said, never the snapshot's start; a pid
    # it could not identify (gone, or inspection failed) is not recorded at all.
    recordable = {pid: answered.get(pid, members[pid]) for pid in stale}
    assert fresh == {str(pid): {"pid": pid, "boot_id": booted, "proc_start": answer}
                     for pid, answer in recordable.items() if answer not in (None, "error")}


def old_export_sweep(store) -> list[str]:
    """The per-job sweep `_pending_exports` replaced, verbatim in effect."""
    found = []
    for job in store.query("SELECT * FROM jobs WHERE accepted_attempt_id IS NOT NULL"):
        if store.one("SELECT 1 FROM leases WHERE holder=?", (job["job_id"],)):
            found.append(job["job_id"])
    return found


@settings(max_examples=150, deadline=None)
@given(jobs=st.lists(st.booleans(), max_size=12),
       holders=st.lists(st.one_of(st.integers(0, 14).map(lambda n: f"job-{n:02d}"),
                                  st.integers(0, 14).map(lambda n: f"job-{n:02d}/a1"),
                                  st.integers(0, 14).map(lambda n: f"retention:job-{n:02d}"),
                                  st.just("probe:timer:codex-1")), max_size=16))
def test_the_export_query_matches_the_sweep_it_replaced(tmp_path_factory, jobs, holders):
    store = Store(tmp_path_factory.mktemp("exports") / "s.sqlite3")
    try:
        with store.transaction("test.setup") as tx:
            for n, accepted in enumerate(jobs):
                tx.execute("INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,"
                           "sandbox,created_at,accepted_attempt_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (f"job-{n:02d}", f"r-{n}", "d", "run", "succeeded", "/w", "/p", "read-only",
                            f"2026-09-24T00:00:{n:02d}Z", f"job-{n:02d}/a1" if accepted else None))
            for n, holder in enumerate(holders):
                tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES (?,?,?)",
                           (f"key-{n}", holder, "t"))
        new = [row["job_id"] for row in store.query(daemon_module.PENDING_EXPORTS)]
        assert new == old_export_sweep(store)
    finally:
        store.close()


operations = st.lists(st.sampled_from(["noop", "change", "rollback", "nested-change", "inner-rollback"]),
                      max_size=25)


@settings(max_examples=100, deadline=None)
@given(ops=operations)
def test_generation_counts_committed_top_level_changes(tmp_path_factory, ops):
    store = Store(tmp_path_factory.mktemp("generation") / "s.sqlite3")
    insert = "INSERT INTO events(ts,kind,data_json) VALUES ('t','k','{}')"
    try:
        expected, seen = store.generation, [store.generation]
        for op in ops:
            if op == "noop":
                with store.transaction("t"):
                    pass
            elif op == "change":
                with store.transaction("t") as tx:
                    tx.execute(insert)
                expected += 1
            elif op == "rollback":
                with pytest.raises(RuntimeError):
                    with store.transaction("t") as tx:
                        tx.execute(insert)
                        raise RuntimeError("rolled back")
            elif op == "nested-change":
                with store.transaction("t"):
                    with store.transaction("t") as inner:
                        inner.execute(insert)
                    assert store.generation == expected     # not visible before the outer commit
                expected += 1
            elif op == "inner-rollback":                   # the outer commits only its own change
                with store.transaction("t") as tx:
                    tx.execute(insert)
                    with pytest.raises(RuntimeError):
                        with store.transaction("t") as inner:
                            inner.execute(insert)
                            raise RuntimeError("inner rolled back")
                expected += 1
            assert store.generation == expected
            seen.append(store.generation)
        assert seen == sorted(seen)
    finally:
        store.close()
