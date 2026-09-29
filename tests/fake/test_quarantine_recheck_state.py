"""C-5.7, C-5.7b: the daemon's recheck of quarantined attempts, in process, over a scripted
process table.

The daemon is the real one (store, admission, `_quarantine`, the sweep, the release);
only `procs._read`, the one place `ps` is started, answers from a `World` the test
writes. So every census here is `procs.containment` itself, over rows the test chose.
"""

from __future__ import annotations

import json
import signal
import threading
import uuid

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import daemon as daemon_module
from subfleet import procs, protocol
from subfleet.adapters.registry import register
from subfleet.daemon import Daemon
from subfleet.procs import Containment, ProcessIdentity
from subfleet.salvage import SalvageError
from tests.fake.conftest import Harness
from tests.fake.test_state_contract import reserve
from tests.fake_adapter import FakeAdapter

BOOT = "unit-test-boot"
T0 = "Tue Sep 29 03:14:45 2026"
T1 = "Tue Sep 29 03:20:00 2026"
T2 = "Tue Sep 29 09:00:00 2026"
REAL_PROC_START = procs.proc_start


class World:
    """What `ps` reports: rows (pid -> ppid, pgid, stat, lstart) and marked pids
    (pid -> attempt id), answered to exactly the argv `procs` sends."""

    def __init__(self, root: str):
        self.root = root
        self.rows: dict[int, tuple[int, int, str, str]] = {}
        self.marks: dict[int, str] = {}
        self.fail_table = self.fail_scan = False
        self.reads: list[list[str]] = []

    def read(self, argv, *, empty_ok=False):
        self.reads.append(list(argv))
        if argv == procs.TABLE_ARGV:
            if self.fail_table:
                raise procs.InspectionError("ps inspection failed (1)")
            return "".join(f"{pid} {ppid} {pgid} {stat} {start}\n"
                           for pid, (ppid, pgid, stat, start) in self.rows.items())
        if argv == procs.MARKER_ARGV:
            if self.fail_scan:
                raise procs.InspectionError("ps inspection failed (1)")
            return "".join(f"{pid} /usr/bin/python3 x SUBFLEET_ATTEMPT={aid} SUBFLEET_ROOT={self.root} HOME=/x\n"
                           for pid, aid in self.marks.items())
        if argv[:2] == ["/bin/ps", "-p"]:
            row = self.rows.get(int(argv[2]))
            return (row[{"lstart=": 3, "stat=": 2}[argv[4]]] + "\n") if row else ""
        raise procs.InspectionError("unexpected read: " + " ".join(argv))

    def process_reads(self) -> int:
        return sum(argv in (procs.TABLE_ARGV, procs.MARKER_ARGV) for argv in self.reads)


@pytest.fixture
def world_daemon(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    harness = Harness(root)
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: BOOT)
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "unit-test-start")
    register("codex", FakeAdapter)
    daemon = Daemon(harness.root)

    def refuse_real_launch(*args):
        raise AssertionError("state-only fixtures must never launch a guardian")
    monkeypatch.setattr(daemon, "_launch", refuse_real_launch)
    daemon.quarantine_recheck_s = 0          # a failed recheck is offered again on the next sweep
    world = World(str(daemon.root))
    monkeypatch.setattr(daemon_module.procs, "proc_start", REAL_PROC_START)
    monkeypatch.setattr(daemon_module.procs, "_read", world.read)
    try:
        yield daemon, harness, world
    finally:
        daemon.close()
    harness.check_notices()


def identities(pairs) -> dict:
    return {str(pid): {"pid": pid, "boot_id": BOOT, "proc_start": start} for pid, start in pairs}


def quarantine(daemon, harness, *, guardian=(4100, T0), owned=(), listed=(),
               reason="writers remain after exit receipt"):
    """A quarantined attempt holding an `out:` lease, as `_quarantine` leaves one."""
    job_id, attempt, _ = reserve(daemon, harness, out_path=str(harness.root / f"out-{uuid.uuid4().hex}.md"))
    aid = attempt["attempt_id"]
    daemon.store.update_attempt(aid, state="running", guardian_pid=guardian[0], pgid=guardian[0], boot_id=BOOT,
                                proc_start=guardian[1], started_at=daemon_module.utcnow(),
                                evidence_json=json.dumps({"owned_identities": identities(owned)}))
    census = Containment(marker_pids=frozenset(pid for pid, _ in listed),
                         identities={pid: ProcessIdentity(pid, BOOT, start) for pid, start in listed})
    daemon._quarantine(daemon.store.get_attempt(aid), census, reason)
    assert daemon.store.get_attempt(aid)["state"] == "quarantined"
    return job_id, aid


def leases(daemon, job_id, aid) -> list[str]:
    return [row["lease_key"] for row in daemon.store.query(
        "SELECT lease_key FROM leases WHERE holder IN (?,?)", (job_id, aid))]


def events(daemon, aid, kind) -> list[dict]:
    return [json.loads(row["data_json"]) for row in daemon.store.query(
        "SELECT data_json FROM events WHERE attempt_id=? AND kind=? ORDER BY event_id", (aid, kind))]


def test_c5_7b_nothing_left_releases_the_leases_and_leaves_the_job_as_quarantined(world_daemon):
    """C-5.7b: group empty and every recorded identity gone: `quarantine.auto_resolved`,
    the leases released, the attempt `lost`; the job's state, rc and notice are the quarantine's."""
    daemon, harness, world = world_daemon
    job_id, aid = quarantine(daemon, harness, owned=[(4200, T1)], listed=[(4201, T1)])
    job_before = daemon.store.get_job(job_id)
    assert any(key.startswith("out:") for key in leases(daemon, job_id, aid))
    daemon._recheck_quarantines()
    assert daemon.store.get_attempt(aid)["state"] == "lost"
    assert leases(daemon, job_id, aid) == []
    resolved = events(daemon, aid, "quarantine.auto_resolved")
    assert len(resolved) == 1 and resolved[0]["by"] == "recheck"
    assert resolved[0]["containment"]["live_pids"] == [] and not resolved[0]["containment"]["unverifiable"]
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == (job_before["state"], job_before["rc"]) == ("lost", 125)
    assert len(daemon.store.query("SELECT * FROM notices WHERE job_id=?", (job_id,))) == 1
    daemon._recheck_quarantines()                          # resolved once, never again
    assert len(events(daemon, aid, "quarantine.auto_resolved")) == 1


def test_c5_7b_a_cancelled_attempt_resolves_interrupted(world_daemon):
    """C-5.7b, C-7.2: the attempt of a cancelled job ends `interrupted`, as `--confirm-dead` ends it."""
    daemon, harness, world = world_daemon
    job_id, attempt, _ = reserve(daemon, harness)
    daemon.dispatch("kill", {"job_id": job_id})
    daemon.store.update_attempt(attempt["attempt_id"], state="running", guardian_pid=4100, pgid=4100,
                                boot_id=BOOT, proc_start=T0, started_at=daemon_module.utcnow())
    daemon._quarantine(daemon.store.get_attempt(attempt["attempt_id"]), Containment(), "termination could not verify containment")
    daemon._recheck_quarantines()
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "interrupted"
    assert daemon.store.get_job(job_id)["state"] == "cancelled"


def test_c5_7b_a_member_of_the_recorded_group_holds_the_quarantine_until_it_is_gone(world_daemon):
    """C-5.7b: the guardian is gone but its group still has a member (reparented to launchd,
    no marker visible): the attempt stays quarantined with its leases; once the member is
    gone, the next sweep releases it."""
    daemon, harness, world = world_daemon
    job_id, aid = quarantine(daemon, harness)
    world.rows = {4101: (1, 4100, "S", T0)}
    for _ in range(3):
        daemon._recheck_quarantines()
        assert daemon.store.get_attempt(aid)["state"] == "quarantined"
        assert leases(daemon, job_id, aid)
    evidence = json.loads(daemon.store.get_attempt(aid)["quarantine_reason"])
    assert evidence["reason"] == "writers remain after exit receipt" and evidence["live_pids"] == [4101]
    world.rows = {}
    daemon._recheck_quarantines()
    assert daemon.store.get_attempt(aid)["state"] == "lost" and leases(daemon, job_id, aid) == []


def test_c5_7b_a_live_recorded_identity_holds_even_where_no_source_would_find_it(world_daemon):
    """C-5.7b: a recorded tool shell (`/bin/zsh`: its own group, parent launchd, no marker)
    holds the quarantine by identity alone; when its pid is held by a process with another
    start (reused), it is gone and the attempt is released."""
    daemon, harness, world = world_daemon
    job_id, aid = quarantine(daemon, harness, owned=[(4200, T1)])
    world.rows = {4200: (1, 4200, "Ss", T1), 4201: (4200, 4200, "S", T2), 7000: (1, 7000, "Ss", T0)}
    daemon._recheck_quarantines()
    assert daemon.store.get_attempt(aid)["state"] == "quarantined"
    live = json.loads(daemon.store.get_attempt(aid)["quarantine_reason"])
    assert live["live_pids"] == [4200, 4201]               # its child is recorded too, for later sweeps
    world.rows = {4200: (1, 4200, "Ss", T2), 7000: (1, 7000, "Ss", T0)}   # 4200 reused; 4201 gone
    daemon._recheck_quarantines()
    assert daemon.store.get_attempt(aid)["state"] == "lost" and leases(daemon, job_id, aid) == []


def test_c5_7b_an_orphan_recorded_only_by_an_earlier_recheck_still_holds(world_daemon):
    """C-5.7b: a process a recheck found through a live recorded shell stays recorded by
    identity after the shell exits and it is reparented: its evidence was kept."""
    daemon, harness, world = world_daemon
    job_id, aid = quarantine(daemon, harness, owned=[(4200, T1)])
    world.rows = {4200: (1, 4200, "Ss", T1), 4201: (4200, 4300, "S", T2)}   # 4201 left the shell's group
    daemon._recheck_quarantines()
    world.rows = {4201: (1, 4300, "S", T2)}                 # the shell exits; 4201 now has no source but identity
    daemon._recheck_quarantines()
    assert daemon.store.get_attempt(aid)["state"] == "quarantined"
    world.rows = {}
    daemon._recheck_quarantines()
    assert daemon.store.get_attempt(aid)["state"] == "lost"


def test_c5_7b_a_reused_guardian_pid_and_its_group_are_a_strangers(world_daemon):
    """C-5.7b: days later the guardian's pid leads another process's group; neither it nor
    that group holds the quarantine (POSIX reuses the pid only once the attempt's group ended)."""
    daemon, harness, world = world_daemon
    job_id, aid = quarantine(daemon, harness)
    world.rows = {4100: (1, 4100, "Ss", T2), 4102: (4100, 4100, "S", T2)}
    daemon._recheck_quarantines()
    assert daemon.store.get_attempt(aid)["state"] == "lost"


def test_c5_7b_a_marked_process_holds_and_a_failed_read_decides_nothing(world_daemon):
    """C-5.5, C-5.7b: a live marked process holds; a `ps` that fails holds and writes nothing."""
    daemon, harness, world = world_daemon
    job_id, aid = quarantine(daemon, harness)
    world.rows, world.marks = {4300: (1, 4300, "S", T1)}, {4300: aid}
    daemon._recheck_quarantines()
    assert daemon.store.get_attempt(aid)["state"] == "quarantined"
    written = len(events(daemon, aid, "quarantine.still_live"))
    world.rows, world.marks = {}, {}
    for fault in ("fail_table", "fail_scan"):
        setattr(world, fault, True)
        daemon._recheck_quarantines()
        setattr(world, fault, False)
        assert daemon.store.get_attempt(aid)["state"] == "quarantined"
    assert len(events(daemon, aid, "quarantine.still_live")) == written
    daemon._recheck_quarantines()
    assert daemon.store.get_attempt(aid)["state"] == "lost"


def test_c5_7b_live_evidence_is_written_only_when_its_live_set_changes(world_daemon):
    """C-5.7b, C-5.11: a quarantine that stays live costs no store write per sweep."""
    daemon, harness, world = world_daemon
    job_id, aid = quarantine(daemon, harness, listed=[(4300, T1)])
    world.rows, world.marks = {4300: (1, 4300, "S", T1)}, {4300: aid}
    daemon._recheck_quarantines()
    first = events(daemon, aid, "quarantine.still_live")     # the one thing learnt: the recorded group ended
    assert len(first) == 1 and first[0]["ended_groups"] == [4100]
    for _ in range(4):
        daemon._recheck_quarantines()
    assert len(events(daemon, aid, "quarantine.still_live")) == 1       # the quarantine already listed 4300
    world.rows[4301] = (4300, 4300, "S", T2)
    for _ in range(3):
        daemon._recheck_quarantines()
    assert len(events(daemon, aid, "quarantine.still_live")) == 2
    evidence = json.loads(daemon.store.get_attempt(aid)["quarantine_reason"])
    assert evidence["groups_ended"] == [4100] and evidence["reason"] == "writers remain after exit receipt"
    assert {(r["pid"], r["proc_start"]) for r in evidence["recorded"]} == {(4300, T1), (4301, T2)}


def test_c5_7b_a_sweep_starts_at_most_four_reads_however_many_it_releases(world_daemon):
    """C-5.7b: nothing quarantined starts no process; otherwise one table and one scan
    serve every attempt, and one fresh pair serves every release."""
    daemon, harness, world = world_daemon
    daemon._recheck_quarantines()
    assert world.process_reads() == 0
    held = [quarantine(daemon, harness, guardian=(4100 + n, T0)) for n in range(5)]
    world.rows = {4100: (1, 4100, "S", T0), 4101: (1, 4101, "S", T0)}  # the first two are still live
    world.reads.clear()
    daemon._recheck_quarantines()
    assert world.process_reads() == 4
    states = [daemon.store.get_attempt(aid)["state"] for _, aid in held]
    assert states == ["quarantined", "quarantined", "lost", "lost", "lost"]
    world.reads.clear()
    daemon._recheck_quarantines()                            # nothing to release: no fresh pair
    assert world.process_reads() == 2


def test_c5_7b_a_failed_salvage_keeps_the_quarantine_until_a_sweep_can_salvage(world_daemon, monkeypatch, caplog):
    """C-5.7b, C-13.1: a release never goes ahead without its salvage; the failure is
    logged by type on the 1st, 2nd, 4th ... in a row, and the next good sweep releases."""
    daemon, harness, world = world_daemon
    job_id, aid = quarantine(daemon, harness)
    salvaged = []

    def failing(job, a):
        raise SalvageError("git add timed out after 60 s", transient=True)
    monkeypatch.setattr(daemon, "_salvage", failing)
    caplog.set_level("ERROR", logger=daemon.log.name)
    for _ in range(5):
        daemon._recheck_quarantines()
    assert daemon.store.get_attempt(aid)["state"] == "quarantined" and leases(daemon, job_id, aid)
    logged = [r.getMessage() for r in caplog.records if "quarantine recheck" in r.getMessage()]
    assert len(logged) == 3 and all("SalvageError" in line and "timed out" not in line for line in logged)
    # The third failure in a row says so, once: in the evidence and to the operator.
    evidence = json.loads(daemon.store.get_attempt(aid)["quarantine_reason"])
    assert evidence["recheck_error"]["error_type"] == "SalvageError" and evidence["recheck_error"]["failures"] == 3
    assert evidence["reason"] == "writers remain after exit receipt"
    told = daemon.store.query("SELECT text FROM service_notices WHERE text LIKE ?", (f"%{job_id}%",))
    assert len(told) == 1 and "--force-release" in told[0]["text"] and "timed out" not in told[0]["text"]
    monkeypatch.setattr(daemon, "_salvage", lambda job, a: salvaged.append(a["attempt_id"]) or (
        [{"role": "salvage", "path": "refs/subfleet-salvage/x", "sha256": "0" * 64, "bytes": 0}], None))
    daemon._recheck_quarantines()
    assert salvaged == [aid] and daemon.store.get_attempt(aid)["state"] == "lost"
    assert daemon.store.query("SELECT * FROM artifacts WHERE attempt_id=? AND role='salvage'", (aid,))
    assert aid not in daemon._quarantine_failures


def test_c5_7b_a_failing_recheck_backs_off_doubling_to_the_ceiling(world_daemon, monkeypatch):
    """C-5.7b, C-5.10: a recheck that raises is offered again after the period,
    doubling per failure in a row to QUARANTINE_RETRY_CEILING_S; other attempts go on."""
    from subfleet.contracts import QUARANTINE_RETRY_CEILING_S
    daemon, harness, world = world_daemon
    daemon.quarantine_recheck_s = 60
    job_id, aid = quarantine(daemon, harness)
    clock = [1000.0]
    monkeypatch.setattr(daemon_module.time, "monotonic", lambda: clock[0])

    def failing(job, a):
        raise SalvageError("git add failed")
    monkeypatch.setattr(daemon, "_salvage", failing)
    delays = []
    for _ in range(9):
        daemon._recheck_quarantines()
        count, due = daemon._quarantine_failures[aid]
        delays.append(due - clock[0])
        clock[0] = due - 1
        daemon._recheck_quarantines()                         # not yet due: not tried
        assert daemon._quarantine_failures[aid][0] == count
        clock[0] = due
    assert delays == [60, 120, 240, 480, 960, 1920, QUARANTINE_RETRY_CEILING_S, QUARANTINE_RETRY_CEILING_S,
                      QUARANTINE_RETRY_CEILING_S]


def test_c5_7_an_operator_and_the_sweep_resolve_an_attempt_once(world_daemon):
    """C-5.7, C-5.7b: `--confirm-dead` racing the sweep: one resolution, one release, and
    the loser changes nothing."""
    daemon, harness, world = world_daemon
    for _ in range(6):
        job_id, aid = quarantine(daemon, harness)
        barrier = threading.Barrier(2)

        def operator():
            barrier.wait()
            daemon._resolve_quarantine(daemon.store.get_attempt(aid), protocol.KillArgs(job_id, confirm_dead=True))

        def sweep():
            barrier.wait()
            daemon._recheck_quarantines()

        threads = [threading.Thread(target=operator), threading.Thread(target=sweep)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        kinds = [row["kind"] for row in daemon.store.query(
            "SELECT kind FROM events WHERE attempt_id=? AND kind IN "
            "('quarantine.auto_resolved','quarantine.confirmed_dead')", (aid,))]
        assert len(kinds) == 1, kinds
        assert daemon.store.get_attempt(aid)["state"] == "lost" and leases(daemon, job_id, aid) == []
    assert daemon.dispatch("kill", {"job_id": job_id, "confirm_dead": True})["status"] == "already finished"


def test_c5_7_a_request_that_finds_the_attempt_released_is_recorded_with_its_note(world_daemon):
    """C-5.7: a `--force-release` accepted while quarantined but run after the recheck
    released it is recorded, note and all, not dropped."""
    daemon, harness, world = world_daemon
    job_id, aid = quarantine(daemon, harness)
    attempt = daemon.store.get_attempt(aid)
    daemon._recheck_quarantines()
    daemon._resolve_quarantine(attempt, protocol.KillArgs(job_id, force_release=True, operator_note="checked by hand"))
    late = [data for data in events(daemon, aid, "quarantine.request_after_release") if data]
    assert late == [{"operator_note": "checked by hand", "override": True, "state": "lost"}]


def test_c5_7_confirm_dead_runs_the_quarantine_census_and_keeps_the_reason(world_daemon):
    """C-5.7: `--confirm-dead` holds on a live recorded identity no source would find, and
    the evidence it rewrites keeps the quarantine's reason."""
    daemon, harness, world = world_daemon
    job_id, aid = quarantine(daemon, harness, owned=[(4200, T1)], reason="termination could not verify containment")
    world.rows = {4200: (1, 4200, "Ss", T1)}
    daemon._resolve_quarantine(daemon.store.get_attempt(aid), protocol.KillArgs(job_id, confirm_dead=True))
    evidence = json.loads(daemon.store.get_attempt(aid)["quarantine_reason"])
    assert evidence["reason"] == "termination could not verify containment" and evidence["live_pids"] == [4200]
    assert daemon.store.get_attempt(aid)["state"] == "quarantined"


# --- the property ---------------------------------------------------------------------

PIDS = st.integers(4100, 4112)
STARTS = st.sampled_from([T0, T1, T2])


@settings(max_examples=120, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(rows=st.dictionaries(PIDS, st.tuples(st.sampled_from([1, 4100, 4101, 4105]), PIDS,
                                            st.sampled_from(["S", "Ss", "Z", "R"]), STARTS), max_size=8),
       guardian=st.tuples(PIDS, STARTS),
       owned=st.lists(st.tuples(PIDS, STARTS), max_size=3),
       listed=st.lists(st.tuples(PIDS, STARTS), max_size=3),
       marks=st.lists(PIDS, max_size=2),
       fail=st.sampled_from([None, None, None, "fail_table", "fail_scan"]))
def test_c5_7b_a_lease_is_never_released_while_a_recorded_identity_is_alive(
        world_daemon, rows, guardian, owned, listed, marks, fail):
    """C-5.7b's invariant, over random process tables: a sweep releases a quarantined
    attempt's leases only if none of its recorded identities (the guardian, the owned
    processes, the quarantine's listed identities) is live with its recorded start, no
    process carries its marker, the recorded group has no live member (unless another
    process leads it at the guardian's pid), and every read worked. And it does release
    one exactly then (an independent oracle). Every attempt the sweep looks at, including
    those earlier examples left quarantined, is checked against the table it saw."""
    daemon, harness, world = world_daemon
    world.rows, world.marks, world.fail_table, world.fail_scan = {}, {}, False, False
    job_id, aid = quarantine(daemon, harness, guardian=guardian, owned=owned, listed=listed)
    world.rows = dict(rows)
    world.marks = {pid: aid for pid in marks}
    if fail:
        setattr(world, fail, True)
    before = {row["attempt_id"]: dict(row) for row in daemon.store.query(
        "SELECT * FROM attempts WHERE state='quarantined'")}
    daemon._recheck_quarantines()

    def live(pid, start=None):
        row = world.rows.get(pid)
        return row is not None and not row[2].startswith("Z") and (start is None or row[3] == start)

    for other, a in before.items():
        listed = daemon_module.quarantine_evidence(a)
        recorded = (daemon_module.recorded_identities(listed.get("recorded"))
                    + daemon_module.recorded_identities(listed.get("identities"))
                    + daemon_module.recorded_identities(json.loads(a["evidence_json"] or "{}").get("owned_identities"))
                    + [ProcessIdentity(a["guardian_pid"], BOOT, a["proc_start"])])
        g, g_start = a["guardian_pid"], a["proc_start"]
        stranger = live(g) and world.rows[g][1] == g and world.rows[g][3] != g_start
        ended = g in set(listed.get("groups_ended") or ())
        members = [] if stranger or ended else [pid for pid, row in world.rows.items() if row[1] == g and live(pid)]
        expected = not (fail or any(live(r.pid, r.proc_start) for r in recorded)
                        or any(live(pid) for pid, owner in world.marks.items() if owner == other) or members)
        released = daemon.store.get_attempt(other)["state"] != "quarantined"
        if released:
            held = [r for r in recorded if live(r.pid, r.proc_start)]
            assert not held, (other, held, world.rows)
            assert leases(daemon, other.rsplit("/", 1)[0], other) == []
        else:
            assert leases(daemon, other.rsplit("/", 1)[0], other)
        assert released == expected, (other, recorded, world.rows, world.marks, fail, listed.get("groups_ended"))


# --- the owned record (C-5.6, C-5.12) --------------------------------------------------

def running(daemon, harness, guardian=(4100, T0)):
    """A running attempt whose guardian leads its group, as `attempt.running` records it."""
    job_id, attempt, _ = reserve(daemon, harness)
    daemon.store.update_attempt(attempt["attempt_id"], state="running", guardian_pid=guardian[0], pgid=guardian[0],
                                boot_id=BOOT, proc_start=guardian[1], started_at=daemon_module.utcnow(),
                                evidence_json="{}")
    return job_id, daemon.store.get_attempt(attempt["attempt_id"])


def shown(rows: dict, *, began: float, ended: float) -> procs.ProcessTable:
    return procs.ProcessTable(dict(rows), BOOT, taken_at=ended, began_at=began)


TREE = {4100: (1, 4100, "Ss", T0), 4101: (4100, 4100, "S", T0),       # guardian, provider
        4200: (4101, 4200, "Ss", T1), 4201: (4200, 4200, "S", T1)}    # a tool session: zsh and uv


def persisted(daemon, aid) -> set[int]:
    return {int(pid) for pid in json.loads(daemon.store.get_attempt(aid)["evidence_json"]).get("owned_identities", {})}


def test_c5_12_the_owned_record_is_written_at_most_every_persist_interval(world_daemon, monkeypatch):
    """C-5.12: the first record is written at once, later gains at most every
    OWNED_PERSIST_S, and a record that did not change is never written again."""
    from subfleet.contracts import OWNED_PERSIST_S
    daemon, harness, world = world_daemon
    job_id, a = running(daemon, harness)
    clock = [100.0]
    monkeypatch.setattr(daemon_module.time, "monotonic", lambda: clock[0])
    base = {pid: TREE[pid] for pid in (4100, 4101)}
    daemon._record_owned(a, shown(base, began=99, ended=100))
    assert persisted(daemon, a["attempt_id"]) == {4100, 4101}
    clock[0] = 101
    daemon._record_owned(a, shown(TREE, began=100.5, ended=101))
    assert set(daemon._owned[a["attempt_id"]].processes) == {4100, 4101, 4200, 4201}
    assert persisted(daemon, a["attempt_id"]) == {4100, 4101}            # in memory only, for now
    clock[0] = 100 + OWNED_PERSIST_S
    daemon._record_owned(a, shown(TREE, began=clock[0] - .5, ended=clock[0]))
    assert persisted(daemon, a["attempt_id"]) == {4100, 4101, 4200, 4201}
    evidence = json.loads(daemon.store.get_attempt(a["attempt_id"])["evidence_json"])
    assert set(evidence["owned_groups"]) == {"4100", "4200"}
    writes = len(events(daemon, a["attempt_id"], "attempt.processes_recorded"))
    clock[0] += 5 * OWNED_PERSIST_S
    daemon._record_owned(a, shown(TREE, began=clock[0] - .5, ended=clock[0]))
    assert len(events(daemon, a["attempt_id"], "attempt.processes_recorded")) == writes


def test_c5_12_a_table_read_before_the_last_that_added_adds_but_never_drops(world_daemon):
    """C-5.12: a slow read that began before a newer table ended cannot show what was
    born in between, so it drops nothing; a table that began after it drops the dead."""
    daemon, harness, world = world_daemon
    job_id, a = running(daemon, harness)
    daemon._record_owned(a, shown(TREE, began=10, ended=11))
    old = {pid: TREE[pid] for pid in (4100, 4101)}
    daemon._record_owned(a, shown(old, began=5, ended=12))               # began before 11 ended
    assert set(daemon._owned[a["attempt_id"]].processes) == {4100, 4101, 4200, 4201}
    assert set(daemon._owned[a["attempt_id"]].groups) == {4100, 4200}
    daemon._record_owned(a, shown(old, began=13, ended=14))
    assert set(daemon._owned[a["attempt_id"]].processes) == {4100, 4101}
    assert set(daemon._owned[a["attempt_id"]].groups) == {4100}


def test_c5_6_a_recorded_group_outlives_its_leader_until_no_process_is_in_it(world_daemon):
    """C-5.6, C-5.12: the tool session's zsh exits and its command is orphaned in its
    group; the group stays recorded (the census counts its members) until the table
    shows no process in it at all, zombies included."""
    daemon, harness, world = world_daemon
    job_id, a = running(daemon, harness)
    daemon._record_owned(a, shown(TREE, began=10, ended=11))
    orphaned = {**{pid: TREE[pid] for pid in (4100, 4101)}, 4201: (1, 4200, "S", T1)}
    daemon._record_owned(a, shown(orphaned, began=12, ended=13))
    assert 4200 in daemon._owned[a["attempt_id"]].groups
    world.rows = {4201: (1, 4200, "S", T1), 4202: (1, 4200, "S", T2)}      # 4202 was never recorded
    assert daemon._contain(a).live_pids >= {4201, 4202}
    zombie = {**{pid: TREE[pid] for pid in (4100, 4101)}, 4201: (1, 4200, "Z", T1)}
    daemon._record_owned(a, shown(zombie, began=14, ended=15))
    assert 4200 in daemon._owned[a["attempt_id"]].groups                  # a zombie keeps its group
    daemon._record_owned(a, shown({pid: TREE[pid] for pid in (4100, 4101)}, began=16, ended=17))
    assert 4200 not in daemon._owned[a["attempt_id"]].groups


def test_c5_7_a_quarantine_writes_the_owned_record_whole(world_daemon, monkeypatch):
    """C-5.7, C-5.7b: the recheck reads the evidence, not memory, so the quarantine
    writes what memory held, even inside OWNED_PERSIST_S."""
    daemon, harness, world = world_daemon
    job_id, a = running(daemon, harness)
    daemon._record_owned(a, shown({pid: TREE[pid] for pid in (4100, 4101)}, began=1, ended=2))
    daemon._record_owned(a, shown(TREE, began=3, ended=4))
    assert persisted(daemon, a["attempt_id"]) == {4100, 4101}
    daemon._quarantine(daemon.store.get_attempt(a["attempt_id"]), Containment(), "writers remain after exit receipt")
    evidence = json.loads(daemon.store.get_attempt(a["attempt_id"])["evidence_json"])
    assert set(evidence["owned_identities"]) == {"4100", "4101", "4200", "4201"}
    assert set(evidence["owned_groups"]) == {"4100", "4200"}


def test_c5_6_the_kill_signals_owned_groups_and_loose_owned_processes_only(world_daemon, monkeypatch):
    """C-5.4, C-5.6: SIGTERM reaches the recorded group, the tool session's group (its
    leader owned) and, one by one, an owned orphan whose group has no owned leader;
    never a stranger's process or group, even one listed under an owned parent while
    traced. Escalation SIGKILLs the groups, then each owned survivor."""
    daemon, harness, world = world_daemon
    job_id, a = running(daemon, harness)
    daemon.term_grace_s = daemon.kill_settle_s = 0
    world.rows = {**TREE,
                  4300: (4101, 4250, "S", T1),     # the provider's child, in a group whose leader is gone
                  4400: (4101, 4400, "SX", T2),    # a stranger the provider's debugger attached to
                  4401: (4400, 4400, "S", T2),
                  7000: (1, 7000, "Ss", T0)}       # an unrelated process
    signals = []
    monkeypatch.setattr(daemon_module.procs, "signal_group",
                        lambda pgid, sig, **identity: signals.append(("group", pgid, sig)) or True)
    monkeypatch.setattr(daemon_module.procs, "signal_process",
                        lambda ident, sig: signals.append(("process", ident.pid, sig)) or True)
    daemon._kill_attempt(a)
    term = [s for s in signals if s[2] == signal.SIGTERM]
    assert sorted(term) == [("group", 4100, signal.SIGTERM), ("group", 4200, signal.SIGTERM),
                            ("process", 4300, signal.SIGTERM)]
    kill = [s for s in signals if s[2] == signal.SIGKILL]
    assert ("group", 4100, signal.SIGKILL) in kill and ("group", 4200, signal.SIGKILL) in kill
    assert {pid for kind, pid, _ in kill if kind == "process"} == {4100, 4101, 4200, 4201, 4300}
    assert not [s for s in signals if s[1] in (4400, 4401, 7000)]
    evidence = json.loads(daemon.store.get_attempt(a["attempt_id"])["evidence_json"])
    assert set(evidence["owned_identities"]) == {"4100", "4101", "4200", "4201", "4300"}
    assert daemon.store.get_attempt(a["attempt_id"])["state"] == "quarantined"     # the world never emptied
