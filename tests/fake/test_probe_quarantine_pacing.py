"""C-5.7a: a quarantined probe is looked at again on its own backing-off clock,
and a look that finds what the record already says writes nothing.

Before this clause every admission pass (up to twenty a second) re-ran the full
C-5.5 census on a quarantined probe, checked its guardian's identity, appended
another probe.state record, and committed a probe.quarantined transaction, and
found that record by parsing every probe.state event ever written. Two reviews
of PR #40 reproduced it: 20 unchanged passes, 20 censuses, 20 guardian checks,
20 records, 20 transactions. These tests drive `_recover_probes` (and `_admit`)
under a fake monotonic clock, with the census and the identity check replaced by
counters, so every count below is exact and every run the same.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from subfleet import daemon as daemon_module, procs
from subfleet.adapters import registry
from subfleet.contracts import Launch
from subfleet.daemon import (PROBE_RECHECK_CEILING_S, PROBE_RECORD, Daemon, after, probe_evidence,
                             probe_recheck_delay, utcnow)
from tests.fake.conftest import Harness
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401 - fixture
from tests.fake_adapter import FakeAdapter

HOLDER = "probe:quarantine-fixture"
GUARDIAN, CHILD, SURVIVOR, OTHER = 900001, 900002, 900003, 900004


def census(*pids: int, stat: str = "S", errors: tuple[str, ...] = ()) -> procs.Containment:
    """A census that finds `pids` alive (escaped: marker source only), or none."""
    return procs.Containment(
        marker_pids=frozenset(pids),
        identities={pid: procs.ProcessIdentity(pid, "fixture-boot", f"start-{pid}") for pid in pids},
        shapes={pid: {"ppid": 1, "pgid": pid, "stat": stat} for pid in pids},
        unverifiable=bool(errors), errors=errors)


UNVERIFIABLE = ("group enumeration unavailable", "descendant enumeration unavailable",
                "marker enumeration unavailable")


class World:
    """The fake clock and process table a probe is looked at through."""

    def __init__(self, service, monkeypatch, table: procs.Containment):
        self.now = 0.0
        self.table = table
        self.censuses: list[float] = []       # when a full C-5.5 census ran
        self.identity_checks: list[float] = []  # when the guardian's identity was asked
        self.looks: list[float] = []          # when `_contain_probe` ran
        monkeypatch.setattr(daemon_module, "time", SimpleNamespace(monotonic=lambda: self.now,
                                                                   sleep=lambda seconds: None))

        def take_census(record):
            self.censuses.append(self.now)
            return self.table

        def same_process(*args):
            self.identity_checks.append(self.now)
            return False                      # the guardian has exited

        original = service._contain_probe

        def contain(record):
            self.looks.append(self.now)
            return original(record)

        monkeypatch.setattr(service, "_probe_census", take_census)
        monkeypatch.setattr(procs, "same_process", same_process)
        monkeypatch.setattr(service, "_contain_probe", contain)

        def forbidden(*args, **kwargs):
            raise AssertionError("no recorded identity is alive, so nothing may be signalled")
        monkeypatch.setattr(procs, "signal_group", forbidden)
        monkeypatch.setattr(procs, "signal_process", forbidden)


def submitted(service, harness) -> str:
    return service.dispatch("submit", harness.submit_args(tier="hard"))["job_id"]


def started_probe(service, job_id: str | None) -> dict:
    """A probe whose guardian started and exited, receipt on disk, lease held: what
    a restart finds. The first `_recover_probes` contains it; survivors quarantine it."""
    directory = service.root / "lanes" / "codex-1" / "probes" / HOLDER.rsplit(":", 1)[-1]
    directory.mkdir(parents=True)
    record = {"holder": HOLDER, "job_id": job_id, "lane_id": "codex-1", "model_id": "gpt-6-astra",
              "directory": str(directory), "state": "starting", "created_at": utcnow(),
              "deadline_at": after(60), "guardian_pid": GUARDIAN, "pgid": GUARDIAN,
              "boot_id": "fixture-boot", "proc_start": "fixture-start", "owned_identities": {}}
    service.store.acquire_lease("lane:codex-1:slot:0", HOLDER)
    service._save_probe(record)
    launch = Launch(("fake",), {}, (), str(directory), None, str(directory / "stdout"),
                    str(directory / "stderr"), None, None)
    value = dataclasses.asdict(launch)
    value.pop("env_add")
    (directory / "launch.json").write_text(json.dumps(value))
    (directory / "exit.json").write_text(json.dumps({"rc": 0, "signal": None, "wall_s": .1,
                                                     "child_pid": CHILD}))
    return record


def quarantine(service, world: World) -> None:
    """The look that quarantines: census not empty after the kill protocol."""
    service.term_grace_s = 0
    service._recover_probes()
    assert service._probe_record(HOLDER)["state"] == "quarantined"
    assert service.store.list_leases(HOLDER), "C-5.7: a quarantined probe keeps its lease"


def holder_records(service) -> int:
    return sum(1 for row in service.store.query(
        "SELECT data_json FROM events WHERE kind='probe.state' ORDER BY event_id")
        if json.loads(row["data_json"]).get("holder") == HOLDER)


def events(service) -> int:
    return service.store.one("SELECT count(*) AS n FROM events")["n"]


def expected_looks(start: float, horizon: float) -> list[float]:
    """C-5.7a's schedule: the next look `probe_recheck_delay(n)` after the n-th."""
    times, at, n = [], start, 1
    while True:
        at += probe_recheck_delay(n)
        if at > horizon:
            return times
        times.append(at)
        n += 1


# --- the finding ----------------------------------------------------------------

def test_c5_7a_twenty_unchanged_passes_cost_nothing(routing_state, monkeypatch):
    """The reviewers' reproduction: 20 passes over an unchanged quarantined probe.

    Before: 20 censuses, 20 guardian checks, 20 probe.state records, 20
    probe.quarantined transactions. Now, at one instant: none of them."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    started_probe(service, job_id)
    world = World(service, monkeypatch, census(SURVIVOR))
    quarantine(service, world)
    looks, censuses, checks = len(world.looks), len(world.censuses), len(world.identity_checks)
    rows, changes = events(service), service.store.connection.total_changes
    for _ in range(20):
        service._recover_probes()
    assert (len(world.looks), len(world.censuses), len(world.identity_checks)) == (looks, censuses, checks)
    assert events(service) == rows
    assert service.store.connection.total_changes == changes, "not one row changed: nothing to wake on"


def test_c5_7a_unchanged_passes_cost_one_census_per_backoff_interval_and_write_no_rows(routing_state, monkeypatch):
    """Five minutes of 50 ms passes: looks at 1, 3, 7, 15, 31, 63 s, then every 60 s.

    Each look is one census and one guardian identity check, and writes nothing,
    because nothing it finds differs from the record."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    started_probe(service, job_id)
    world = World(service, monkeypatch, census(SURVIVOR))
    quarantine(service, world)
    first_censuses, first_checks = len(world.censuses), len(world.identity_checks)
    rows, changes = events(service), service.store.connection.total_changes
    record = service._probe_record(HOLDER)
    for tick in range(1, 20 * 300 + 1):
        world.now = tick / 20
        service._recover_probes()
    schedule = expected_looks(0.0, 300.0)
    assert schedule == [1, 3, 7, 15, 31, 63, 123, 183, 243]
    assert world.looks[1:] == schedule
    assert world.censuses[first_censuses:] == schedule, "one census per look, and one look per interval"
    assert world.identity_checks[first_checks:] == schedule
    gaps = [later - earlier for earlier, later in zip([0.0, *schedule], schedule)]
    assert gaps == [probe_recheck_delay(n) for n in range(1, len(schedule) + 1)]
    assert max(gaps) == PROBE_RECHECK_CEILING_S
    assert events(service) == rows and service.store.connection.total_changes == changes
    assert service._probe_record(HOLDER) == record
    assert service.store.list_leases(HOLDER)
    assert service.store.get_job(job_id)["wait_reason"] == "uncertain"


def test_c5_7a_the_lookup_reads_one_row_whatever_the_history(routing_state, monkeypatch):
    """Each look finds the record in one index step: 2,000 other holders' records
    are never fetched, where the old walk parsed every one of them per pass."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    with service.store.transaction("test.seed") as tx:
        tx.executemany("INSERT INTO events(ts,kind,data_json) VALUES (?,?,?)",
                       [(utcnow(), "probe.state", json.dumps({"holder": f"probe:old-{n}", "state": "completed"}))
                        for n in range(2000)])
    started_probe(service, job_id)
    world = World(service, monkeypatch, census(SURVIVOR))
    quarantine(service, world)
    fetched = []
    query = service.store.query

    def counting(sql, params=()):
        rows = query(sql, params)
        if sql == PROBE_RECORD:
            fetched.append(len(rows))
        return rows
    monkeypatch.setattr(service.store, "query", counting)
    for tick in range(1, 20 * 60 + 1):
        world.now = tick / 20
        service._recover_probes()
    assert fetched and set(fetched) == {1}, fetched
    # Two lookups a look (recovery's, and the comparison before writing), none between looks.
    assert len(fetched) == 2 * len(expected_looks(0.0, 60.0))


# --- what a look writes -----------------------------------------------------------

def test_c5_7a_a_run_state_flicker_is_not_a_change(routing_state, monkeypatch):
    """A busy survivor reads R on one look and S on the next: no row for that."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    started_probe(service, job_id)
    world = World(service, monkeypatch, census(SURVIVOR, stat="S"))
    quarantine(service, world)
    rows = events(service)
    for tick in range(1, 20 * 120 + 1):
        world.now = tick / 20
        world.table = census(SURVIVOR, stat="R+" if tick % 2 else "S")
        service._recover_probes()
    assert len(world.looks) > 5
    assert events(service) == rows


def test_c5_7a_changed_evidence_is_recorded_once_and_does_not_restart_the_clock(routing_state, monkeypatch):
    """A second survivor appears between looks: the next look records it (one
    probe.state record, one probe.quarantined event); the looks after it keep
    backing off, so a process tree that keeps changing cannot buy back a census
    a second."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    started_probe(service, job_id)
    world = World(service, monkeypatch, census(SURVIVOR))
    quarantine(service, world)
    records, rows = holder_records(service), events(service)
    quarantined = service.store.one("SELECT count(*) AS n FROM events WHERE kind='probe.quarantined'")["n"]
    for tick in range(1, 20 * 300 + 1):
        world.now = tick / 20
        if world.now == 10:
            world.table = census(SURVIVOR, OTHER)
        service._recover_probes()
    assert world.looks[1:] == expected_looks(0.0, 300.0), "the change did not reset the schedule"
    assert holder_records(service) == records + 1
    assert service.store.one("SELECT count(*) AS n FROM events WHERE kind='probe.quarantined'")["n"] == quarantined + 1
    assert events(service) > rows
    assert service._probe_record(HOLDER)["containment"]["live_pids"] == [SURVIVOR, OTHER]
    assert service.store.get_job(job_id)["wait_reason"] == "uncertain"


def test_c5_7a_unverifiable_errors_that_change_are_evidence(routing_state, monkeypatch):
    """2026-09-27 09:58-11:10Z on the release line: a probe held its lease for 72
    minutes on an unverifiable census whose errors alternated between one failed
    source and all three. Which inspection failed is evidence; the same failure
    found again is not news."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    started_probe(service, job_id)
    world = World(service, monkeypatch, census(errors=UNVERIFIABLE))
    quarantine(service, world)
    records = holder_records(service)
    for tick in range(1, 20 * 60 + 1):
        world.now = tick / 20
        service._recover_probes()
    assert holder_records(service) == records
    world.table = census(errors=UNVERIFIABLE[2:])
    world.now = 1000.0
    service._recover_probes()
    assert holder_records(service) == records + 1
    assert service._probe_record(HOLDER)["containment"]["errors"] == list(UNVERIFIABLE[2:])
    assert service.store.list_leases(HOLDER)


# --- release, and never before ----------------------------------------------------

def test_c5_7a_a_verified_empty_look_releases_when_it_is_due(routing_state, monkeypatch):
    """Survivors gone at 5 s are seen by the look due at 7 s, which releases the
    lease, completes the record, and drops the clock."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    started_probe(service, job_id)
    world = World(service, monkeypatch, census(SURVIVOR))
    quarantine(service, world)
    released_at = None
    for tick in range(1, 20 * 20 + 1):
        world.now = tick / 20
        if world.now == 5:
            world.table = procs.Containment()
        service._recover_probes()
        if released_at is None and not service.store.list_leases(HOLDER):
            released_at = world.now
    assert released_at == 7
    assert service._probe_record(HOLDER)["state"] == "completed"
    assert HOLDER not in service._probe_rechecks
    assert world.looks[1:] == [1, 3, 7]
    assert service.store.get_job(job_id)["wait_reason"] == "capacity"


def test_c5_7a_an_unverifiable_census_never_releases(routing_state, monkeypatch):
    """C-5.7 holds through an hour of passes: uncertain containment keeps the lease."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    started_probe(service, job_id)
    world = World(service, monkeypatch, census(errors=UNVERIFIABLE))
    quarantine(service, world)
    rows = events(service)
    for tick in range(1, 4 * 3600 + 1):       # quarter-second passes: an hour in 14,400
        world.now = tick / 4
        service._recover_probes()
        assert service.store.list_leases(HOLDER)
    assert world.looks[1:] == expected_looks(0.0, 3600.0)
    assert len(world.looks) - 1 == 6 + 58     # 1..63 s doubling, then every 60 s to 3,543 s
    assert events(service) == rows


# --- where the clock starts and ends ------------------------------------------------

def test_c5_7a_the_quarantining_look_starts_the_clock(routing_state, monkeypatch):
    """A probe quarantined by admission's own probe (`_probe_candidate`) is not
    looked at again by the pass that follows it, only a second later."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    record = started_probe(service, job_id)
    world = World(service, monkeypatch, census(SURVIVOR))
    service.term_grace_s = 0

    def failed(*args):
        raise OSError("receipt unavailable")
    monkeypatch.setattr(service, "_execute_probe", failed)
    outcome = service._probe_candidate(service.store.get_job(job_id),
                                       SimpleNamespace(chosen_lane="codex-1", chosen_model="astra"), record["holder"])
    assert outcome.evidence["probe_quarantined"]
    looks = len(world.looks)
    service._recover_probes()
    world.now = .95
    service._recover_probes()
    assert len(world.looks) == looks
    world.now = 1
    service._recover_probes()
    assert len(world.looks) == looks + 1


def test_c5_7a_a_restart_looks_once_and_writes_nothing(routing_state, monkeypatch):
    """The clock is in memory: a new daemon looks at the quarantined probe on its
    first pass, finds what the record says, and writes nothing; then it paces."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    started_probe(service, job_id)
    world = World(service, monkeypatch, census(SURVIVOR))
    quarantine(service, world)
    rows = events(service)
    service.close()
    fresh = Daemon(service.root)
    try:
        again = World(fresh, monkeypatch, census(SURVIVOR))
        for _ in range(10):
            fresh._recover_probes()
        assert again.looks == [0.0]
        assert fresh.store.one("SELECT count(*) AS n FROM events")["n"] == rows
        again.now = 1
        fresh._recover_probes()
        assert again.looks == [0.0, 1]
        assert fresh.store.list_leases(HOLDER)
    finally:
        fresh.close()


def test_c5_7a_a_released_lease_drops_its_clock(routing_state, monkeypatch):
    service, harness = routing_state
    job_id = submitted(service, harness)
    started_probe(service, job_id)
    world = World(service, monkeypatch, census(SURVIVOR))
    quarantine(service, world)
    assert HOLDER in service._probe_rechecks
    service.store.release_leases(HOLDER)
    service._recover_probes()
    assert HOLDER not in service._probe_rechecks


def test_c5_7a_admission_passes_over_a_quarantined_probe_change_no_row(routing_state, monkeypatch):
    """End to end through `_admit`: a pass that finds a quarantined probe not due
    and its job `uncertain` commits nothing (on the release line, PR #40's store
    generation would not move, so no waiter wakes)."""
    service, harness = routing_state
    job_id = submitted(service, harness)
    started_probe(service, job_id)
    world = World(service, monkeypatch, census(SURVIVOR))
    quarantine(service, world)
    service._admit()                          # settles anything the first pass does once
    changes = service.store.connection.total_changes
    looks = len(world.looks)
    for tick in range(1, 20 * 30 + 1):
        world.now = tick / 20
        service._admit()
    assert service.store.connection.total_changes == changes
    assert len(world.looks) - looks == len(expected_looks(0.0, 30.0))
    assert service._holds[job_id] == {"reason": "uncertain"}


# --- the property -------------------------------------------------------------------

#: Passes a tick to a minute and a half apart; the process table mostly as it
#: was, sometimes changed (a run state, a pid, which inspection failed), rarely
#: emptied, so most examples run long enough to reach the 60 s ceiling.
STEPS = st.lists(st.tuples(st.sampled_from([.05, .3, 1, 2.5, 7, 30, 61, 90]),
                           st.sampled_from(["same"] * 5 + ["stat", "pid", "errors", "empty"])),
                 min_size=10, max_size=60)


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(steps=STEPS)
def test_c5_7a_pacing_properties(tmp_path_factory, steps):
    """For any sequence of passes and process-table changes:

    - a look happens exactly when a pass finds the clock due (never early, never skipped);
    - consecutive looks are at least `probe_recheck_delay(n)` apart;
    - a look appends a probe.state record exactly when `probe_evidence` changed,
      and a probe.quarantined event exactly when it did and the probe stays quarantined;
    - the lease is released exactly by the first look that saw a verified-empty census.
    """
    root = tmp_path_factory.mktemp("pacing") / "state"
    root.mkdir()
    harness = Harness(root)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
        monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
        monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
        monkeypatch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
        service = Daemon(root)
        try:
            job_id = submitted(service, harness)
            started_probe(service, job_id)
            world = World(service, monkeypatch, census(SURVIVOR))
            quarantine(service, world)
            due, looks_so_far, released = probe_recheck_delay(1), 1, False
            pids, stat, errors = [SURVIVOR], "S", ()
            for dt, change in steps:
                if change == "stat":
                    stat = "R" if stat == "S" else "S"
                elif change == "pid":
                    pids = [SURVIVOR] if len(pids) == 2 else [SURVIVOR, OTHER]
                elif change == "errors":
                    errors = () if errors else UNVERIFIABLE[2:]
                if change == "empty":
                    world.table = procs.Containment()
                elif change != "same":
                    world.table = census(*pids, stat=stat, errors=errors)
                world.now += dt
                before_looks = len(world.looks)
                records = holder_records(service)
                quarantines = service.store.one(
                    "SELECT count(*) AS n FROM events WHERE kind='probe.quarantined'")["n"]
                previous = service._probe_record(HOLDER)
                service._recover_probes()
                looked = len(world.looks) > before_looks
                if released:
                    assert not looked, "a released probe is never looked at again"
                    continue
                assert looked == (world.now >= due), (world.now, due)
                if not looked:
                    assert holder_records(service) == records
                    continue
                if len(world.looks) >= 2:
                    gap = world.looks[-1] - world.looks[-2]
                    assert gap >= probe_recheck_delay(looks_so_far) - 1e-9
                seen = world.table
                if seen.verified_empty:
                    assert not service.store.list_leases(HOLDER)
                    assert service._probe_record(HOLDER)["state"] == "completed"
                    released = True
                    event("released by a verified-empty look")
                    continue
                assert service.store.list_leases(HOLDER), "never released on a census not verified empty"
                found = {**previous, "state": "quarantined", "containment": seen.to_dict()}
                changed = probe_evidence(found) != probe_evidence(previous)
                assert holder_records(service) == records + (1 if changed else 0)
                assert service.store.one("SELECT count(*) AS n FROM events WHERE kind='probe.quarantined'")["n"] \
                    == quarantines + (1 if changed else 0)
                # What is kept is always what the latest look found, but for run states.
                assert probe_evidence(service._probe_record(HOLDER)) == probe_evidence(found)
                event("a look recorded changed evidence" if changed else "a look wrote nothing")
                looks_so_far += 1
                due = world.now + probe_recheck_delay(looks_so_far)
            event(f"rechecks: {min(len(world.looks) - 1, 8)}{'+' if len(world.looks) > 8 else ''}")
            event("reached the 60 s ceiling" if looks_so_far > 7 else "still doubling")
        finally:
            service.close()
