"""C-4.2, C-5.6, C-5.9, C-5.12: what the daemon decides when the process table is still moving.

A census the kernel is still draining is re-read, not quarantined; a guardian that
cannot be inspected decides nothing; a receipt on disk always beats a stale lost verdict."""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from types import SimpleNamespace

import pytest

from subfleet import daemon as daemon_module
from subfleet.contracts import (
    Attestation, AttestationResult, Credential, Lane, LaneOwner, Launch, Outcome, OutcomeClass,
    attempt_dir,
)
from subfleet.daemon import Daemon
from subfleet.guardian import atomic_publish
from subfleet.procs import Containment, ProcessIdentity, ProcessTable
from subfleet.store import Store

JOB = "20260905-100000-settle"
ATTEMPT = JOB + "/a1"
STARTED = "Sat Sep  5 10:00:00 2026"
BUSY = Containment(frozenset({4243}), frozenset({4243}), frozenset(), False,
                   {4243: ProcessIdentity(4243, "boot", STARTED)}, (),
                   {4243: {"ppid": 4242, "pgid": 4242, "stat": "R"}})
GUARDIAN_ONLY = Containment(frozenset({4242}), frozenset({4242}), frozenset(), False,
                            {4242: ProcessIdentity(4242, "boot", STARTED)}, (),
                            {4242: {"ppid": 1, "pgid": 4242, "stat": "S"}})
UNVERIFIABLE = Containment(unverifiable=True, errors=("group enumeration unavailable",
                                                      "descendant enumeration unavailable"))
EMPTY = Containment()


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    """A daemon core with a running attempt and no processes: the census is injected."""
    root = tmp_path / "state"
    attempt_dir(root, JOB, 1).mkdir(parents=True)
    core = object.__new__(Daemon)
    core.root, core.store = root, Store(root / "state.sqlite3")
    core.stopping = threading.Event()
    core.term_grace_s, core.kill_settle_s, core.exit_settle_s = .05, .3, .3
    core._exit_settle = {}
    core._children, core._pending_launches, core._starting_deadlines = {}, set(), {}
    # C-5.12: a shared process table that shows no process, so every verdict is the injected `liveness`.
    core._inspect_next, core.inspect_interval_s, core._inspect_retry = {}, .5, set()
    core._process_table = shared(ProcessTable({}, "boot"))
    core._launches, core._export_locks = {}, {}
    core.log = logging.getLogger("subfleet.test")
    core._salvage = lambda job, a: ([], None)
    core._record_identity = lambda *args: None
    core._export = lambda job_id: None
    core.timers = SimpleNamespace(record_auth_dead=lambda *args: None, metadata={})   # C-11.2: the pin roster reads it
    core._notify = lambda: None
    core._boundary = lambda *args: None
    core._publish = lambda role, path, contents: atomic_publish(path, contents)
    home = root / "home"
    core.store.put_lane(Lane("codex-1", "codex", "codex:test", Credential("codex", str(home), "home"),
                             str(home), LaneOwner.V2, False))
    core.store.add_job(job_id=JOB, request_id="settle-1", payload_digest="digest", kind="run",
                       state="running", workdir=str(root), prompt_path=str(root / "prompt.md"),
                       sandbox="read-only")
    core.store.add_attempt(attempt_id=ATTEMPT, job_id=JOB, seq=1, lane_id="codex-1",
                           model_requested="astra", state="running", guardian_pid=4242,
                           child_pid=4243, pgid=4242, boot_id="boot", proc_start=STARTED,
                           started_at="2026-09-05T14:00:00Z", evidence_json="{}")
    # No real process is signalled or inspected: the leader is gone, signals succeed.
    monkeypatch.setattr(daemon_module.procs, "same_process", lambda *args, **kwargs: False)
    monkeypatch.setattr(daemon_module.procs, "signal_group", lambda *args, **kwargs: True)
    monkeypatch.setattr(daemon_module.procs, "signal_process", lambda *args, **kwargs: True)
    yield core
    core.store.close()


def attempt(core) -> dict:
    return dict(core.store.get_attempt(ATTEMPT))


def draining(core, busy_for_s: float, busy=BUSY) -> list[float]:
    """Inject a census that stays busy for `busy_for_s` after its first read, then empties."""
    calls: list[float] = []
    first: list[float] = []

    def contain(a):
        now = time.monotonic()
        first.append(now) if not first else None
        calls.append(now - first[0])
        return busy if now - first[0] < busy_for_s else EMPTY
    core._contain = contain
    return calls


def test_c5_6_kill_re_reads_a_draining_census_within_the_settle_window(daemon, monkeypatch):
    """C-5.6 pids still being torn down after SIGKILL are re-read for kill_settle_s, not quarantined."""
    now = 0.0
    killed_at = None
    signals = []
    calls = []

    def wait(timeout):
        nonlocal now
        now += timeout
        return False

    def signal_group(pgid, sig, **identity):
        nonlocal killed_at
        signals.append(sig)
        if sig == signal.SIGKILL:
            killed_at = now
        return True

    def contain(a):
        # The process ignores TERM and starts draining only after KILL. An
        # overloaded host must not make it disappear before escalation occurs.
        elapsed = None if killed_at is None else now - killed_at
        census = EMPTY if elapsed is not None and elapsed >= .15 else BUSY
        calls.append((elapsed, census.verified_empty))
        return census

    # Replace only this daemon module's clock, not the shared time module or
    # real threading primitives used by pytest and filesystem publication.
    monkeypatch.setattr(daemon_module, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(daemon_module.procs, "signal_group", signal_group)
    daemon.stopping = SimpleNamespace(wait=wait)
    daemon._contain = contain
    daemon._kill_attempt(attempt(daemon))
    a = attempt(daemon)
    assert a["state"] == "finalizing", a
    assert a["quarantine_reason"] is None
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    after_kill = [(elapsed, empty) for elapsed, empty in calls if elapsed is not None]
    assert len(after_kill) >= 3
    assert all(not empty for _, empty in after_kill[:-1])
    assert after_kill[-1][1] is True
    assert .15 <= after_kill[-1][0] <= daemon.kill_settle_s
    receipt = json.loads((attempt_dir(daemon.root, JOB, 1) / "exit.json").read_text())
    assert receipt["signal"] == 9 and receipt["killed_by"] == "operator"


def test_c5_6_kill_quarantines_only_after_the_settle_window(daemon):
    """C-5.6 a census that never empties quarantines once kill_settle_s has elapsed, with its evidence."""
    draining(daemon, 60)
    started = time.monotonic()
    daemon._kill_attempt(attempt(daemon))
    assert time.monotonic() - started >= .3
    a = attempt(daemon)
    assert a["state"] == "quarantined"
    detail = json.loads(a["quarantine_reason"])
    assert detail["reason"] == "termination could not verify containment"
    assert detail["live_pids"] == [4243]
    assert detail["shapes"]["4243"] == {"ppid": 4242, "pgid": 4242, "stat": "R"}
    assert daemon.store.get_job(JOB)["state"] == "lost"
    kinds = [row["kind"] for row in daemon.store.list_events(JOB)]
    assert "attempt.quarantined" in kinds


@pytest.mark.parametrize("census", [BUSY, GUARDIAN_ONLY, UNVERIFIABLE],
                         ids=["survivor", "guardian-only", "unverifiable"])
def test_c5_9_exit_receipt_census_waits_out_exit_settle_before_quarantining(daemon, census):
    """C-5.9 after exit.json the census may drain for exit_settle_s; past it, writers remain."""
    daemon.store.update_attempt(ATTEMPT, state="finalizing", rc=0)
    atomic_publish(attempt_dir(daemon.root, JOB, 1) / "exit.json",
                   json.dumps({"rc": 0, "finished_at": "2026-09-05T14:01:00Z", "wall_s": 60,
                               "child_pid": 4243}).encode())
    daemon._contain = lambda a: census
    daemon._finalize(attempt(daemon))
    assert attempt(daemon)["state"] == "finalizing"
    assert ATTEMPT in daemon._exit_settle
    daemon._finalize(attempt(daemon))
    assert attempt(daemon)["state"] == "finalizing"
    daemon._exit_settle[ATTEMPT] -= 1.0
    daemon._finalize(attempt(daemon))
    a = attempt(daemon)
    assert a["state"] == "quarantined"
    assert json.loads(a["quarantine_reason"])["reason"] == "writers remain after exit receipt"
    assert ATTEMPT not in daemon._exit_settle


class StubAdapter:
    """Classifies every exit as ok; the daemon's own logic is what is under test."""

    def classify(self, adir, launch, exit_info):
        return Outcome(OutcomeClass.OK, "ok")

    def attest(self, adir, launch, outcome, model):
        return AttestationResult(Attestation.UNATTESTED, None, "stub")

    def deliverable(self, adir, launch, outcome):
        return b"done\n"


def receipt_path(core):
    return attempt_dir(core.root, JOB, 1) / "exit.json"


def publish_receipt(core, rc=0):
    atomic_publish(receipt_path(core), json.dumps({"rc": rc, "signal": None, "finished_at": "2026-09-05T14:01:00Z",
                                                   "wall_s": .026, "child_pid": 4243}).encode())


def with_launch(core, monkeypatch):
    adir = attempt_dir(core.root, JOB, 1)
    (adir / "stdout").write_text("hello\n")
    core._launches[ATTEMPT] = Launch(argv=("fake",), env_add={}, env_remove=(), cwd=str(core.root),
                                     stdin_path=None, stdout_path=str(adir / "stdout"),
                                     stderr_path=str(adir / "stderr"), raw_stream_path=None,
                                     native_session_id=None, lane_id="codex-1")
    monkeypatch.setattr(daemon_module, "get_adapter", lambda provider: StubAdapter())


def never_census(a):
    raise AssertionError("the census must not run on this path")


def test_c4_2_receipt_on_disk_wins_over_a_stale_lost_verdict(daemon, monkeypatch):
    """C-4.2 a guardian found dead right after publishing exit.json completed its attempt, not lost it."""
    with_launch(daemon, monkeypatch)
    publish_receipt(daemon, rc=0)
    daemon._contain = lambda a: EMPTY
    daemon._lost(attempt(daemon))      # what the tick calls for a dead guardian and an empty census
    a = attempt(daemon)
    assert a["state"] == "succeeded", a
    assert a["rc"] == 0 and a["outcome_class"] == "ok"
    assert a["outcome_detail"] != "guardian lost without exit receipt"
    job = daemon.store.get_job(JOB)
    assert job["state"] == "succeeded" and job["accepted_attempt_id"] == ATTEMPT
    assert (attempt_dir(daemon.root, JOB, 1) / "deliverable.md").read_bytes() == b"done\n"


def test_c4_2_missing_receipt_is_still_a_loss(daemon, monkeypatch):
    """C-4.2 without a receipt the dead-guardian path records the loss as before."""
    with_launch(daemon, monkeypatch)
    daemon._contain = lambda a: EMPTY
    daemon._lost(attempt(daemon))
    a = attempt(daemon)
    assert a["state"] == "lost" and a["outcome_detail"] == "guardian lost without exit receipt"
    # A lost read-only attempt is retried (C-4.2), so the job waits for its next attempt.
    assert daemon.store.get_job(JOB)["state"] == "waiting"


def test_c4_2_dead_guardian_with_a_late_receipt_finalizes_instead_of_lost(daemon, monkeypatch):
    """C-4.2 the tick re-reads exit.json after finding the guardian dead: the receipt landed in between."""
    def dead_after_publishing(pid, boot_id, proc_start):
        publish_receipt(daemon, rc=0)     # the guardian wrote and exited during the liveness check
        return "dead"
    monkeypatch.setattr(daemon_module.procs, "liveness", dead_after_publishing)
    daemon._contain = never_census
    daemon._process_attempt(ATTEMPT)
    a = attempt(daemon)
    assert a["state"] == "finalizing" and a["rc"] == 0
    assert "attempt.finalizing" in [row["kind"] for row in daemon.store.list_events(JOB)]


def test_c4_2_uninspectable_guardian_decides_nothing_this_tick(daemon, monkeypatch):
    """C-4.2, C-5.5 a failed liveness inspection is not death: no census, no loss, no kill."""
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: "unknown")
    daemon._contain = never_census
    before = len(daemon.store.list_events(JOB))
    daemon._process_attempt(ATTEMPT)
    assert attempt(daemon)["state"] == "running"
    assert len(daemon.store.list_events(JOB)) == before


def test_c4_2_dead_guardian_without_receipt_runs_containment(daemon, monkeypatch):
    """C-4.2 only a dead guardian with no receipt reaches containment; an empty census is a loss."""
    with_launch(daemon, monkeypatch)
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: "dead")
    calls = []
    daemon._contain = lambda a: calls.append(1) or EMPTY
    daemon._process_attempt(ATTEMPT)
    assert calls and attempt(daemon)["state"] == "lost"

def table_showing(*rows) -> ProcessTable:
    """A process table of (pid, ppid, pgid, stat) rows, all started at STARTED in boot "boot"."""
    return ProcessTable({pid: (ppid, pgid, stat, STARTED) for pid, ppid, pgid, stat in rows}, "boot")


def shared(table: ProcessTable | None, reads: list | None = None):
    """A stand-in for `Daemon._process_table` that gives every inspection `table`, due again .5 s after it fell due."""
    def process_table(due):
        if reads is not None:
            reads.append(1)
        return table, due + .5
    return process_table


def test_c5_12_a_healthy_attempt_is_inspected_once_per_interval_from_the_shared_table(daemon, monkeypatch):
    """C-5.12, C-5.6 the shared table answers "alive" and lists the group; nothing else is asked until the interval ends."""
    reads = []
    daemon._process_table = shared(table_showing((4242, 1, 4242, "Ss"), (4243, 4242, 4242, "S"), (4300, 1, 4300, "S")),
                                   reads)
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: pytest.fail("the table already answered"))
    monkeypatch.setattr(daemon_module.procs, "snapshot", lambda: pytest.fail("no second table"))
    daemon._contain = never_census
    for _ in range(5):
        daemon._process_attempt(ATTEMPT)
    assert reads == [1]
    owned = json.loads(attempt(daemon)["evidence_json"])["owned_identities"]
    assert owned == {"4242": {"pid": 4242, "boot_id": "boot", "proc_start": STARTED},
                     "4243": {"pid": 4243, "boot_id": "boot", "proc_start": STARTED}}
    assert [row["kind"] for row in daemon.store.list_events(JOB)].count("attempt.processes_recorded") == 1
    daemon._inspect_next[ATTEMPT] = 0                     # the interval ends
    daemon._process_attempt(ATTEMPT)
    assert reads == [1, 1]
    # The same members again: nothing to record, so no second event.
    assert [row["kind"] for row in daemon.store.list_events(JOB)].count("attempt.processes_recorded") == 1


def test_c5_12_the_receipt_is_read_every_tick_whatever_the_inspection_interval(daemon, monkeypatch):
    """C-4.2, C-5.12 rate-limiting inspection never delays a normal end: exit.json is files, not `ps`."""
    daemon._process_table = shared(table_showing((4242, 1, 4242, "Ss")))
    daemon._process_attempt(ATTEMPT)
    assert daemon._inspect_next[ATTEMPT] > time.monotonic()   # inside the interval now
    publish_receipt(daemon, rc=0)
    daemon._process_attempt(ATTEMPT)
    assert attempt(daemon)["state"] == "finalizing"


def test_c5_12_a_shared_table_never_pronounces_death(daemon, monkeypatch):
    """C-5.12, C-4.2 a guardian the table does not show is asked about afresh; alive there, it stays running."""
    with_launch(daemon, monkeypatch)
    daemon._process_table = shared(table_showing((9999, 1, 9999, "S")))
    asked = []
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: asked.append(args) or "alive")
    monkeypatch.setattr(daemon_module.procs, "snapshot", lambda: table_showing((4242, 1, 4242, "Ss")))
    daemon._contain = never_census
    daemon._process_attempt(ATTEMPT)
    assert asked == [(4242, "boot", STARTED)] and attempt(daemon)["state"] == "running"
    assert "4242" in json.loads(attempt(daemon)["evidence_json"])["owned_identities"]
    # And when the fresh read agrees that it is gone, containment runs as before.
    daemon._inspect_next.clear()
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: "dead")
    calls = []
    daemon._contain = lambda a: calls.append(1) or EMPTY
    daemon._process_attempt(ATTEMPT)
    assert calls and attempt(daemon)["state"] == "lost"


def test_c5_12_members_are_recorded_only_while_the_recorded_guardian_leads_the_group(daemon):
    """C-5.6, C-5.4 a table in which the leader is a reused pid records nothing."""
    reused = ProcessTable({4242: (1, 4242, "Ss", "Sun Sep  6 11:00:00 2026"),
                           4243: (4242, 4242, "S", STARTED)}, "boot")
    daemon._record_owned(attempt(daemon), reused)
    assert "owned_identities" not in json.loads(attempt(daemon)["evidence_json"])
    # The recorded guardian, alive, but no longer leading the recorded group: not its members either.
    elsewhere = ProcessTable({4242: (1, 1, "S", STARTED), 4243: (4242, 4242, "S", STARTED)}, "boot")
    daemon._record_owned(attempt(daemon), elsewhere)
    assert "owned_identities" not in json.loads(attempt(daemon)["evidence_json"])


def test_c5_12_a_failed_process_table_read_is_rationed_like_a_good_one(daemon, monkeypatch):
    """C-5.12 `ps` failing costs one read per interval, not one per attempt that asks."""
    from subfleet import procs
    reads = []

    def failing():
        reads.append(1)
        raise procs.InspectionError("ps timed out")
    monkeypatch.setattr(daemon_module.procs, "snapshot", failing)
    daemon._table, daemon._table_lock = (None, 0.0), threading.Lock()
    assert [Daemon._process_table(daemon, time.monotonic())[0] for _ in range(6)] == [None] * 6
    assert reads == [1]
    daemon._table = (None, 0.0)                                   # the interval ends
    monkeypatch.setattr(daemon_module.procs, "snapshot", lambda: table_showing((4242, 1, 4242, "Ss")))
    assert Daemon._process_table(daemon, time.monotonic())[0].is_process(4242, "boot", STARTED)


def add_running(core, job_id: str, guardian: int) -> str:
    """Another running attempt beside the fixture's, led by `guardian`."""
    core.store.add_job(job_id=job_id, request_id=job_id, payload_digest="digest", kind="run", state="running",
                       workdir=str(core.root), prompt_path=str(core.root / "prompt.md"), sandbox="read-only")
    core.store.add_attempt(attempt_id=job_id + "/a1", job_id=job_id, seq=1, lane_id="codex-1",
                           model_requested="astra", state="running", guardian_pid=guardian,
                           child_pid=guardian + 1, pgid=guardian, boot_id="boot", proc_start=STARTED,
                           started_at="2026-09-05T14:00:00Z", evidence_json="{}")
    attempt_dir(core.root, job_id, 1).mkdir(parents=True)
    return job_id + "/a1"


@pytest.mark.parametrize("died_at", [103.37, 104.99])
@pytest.mark.parametrize("read_s", [.02, .3, .72, 1.5])
def test_c5_12_each_inspection_is_given_a_table_read_since_its_last(daemon, monkeypatch, read_s, died_at):
    """C-5.12 on a fake clock: each inspection is given a table read after the one it last had, and less than an
    interval before it fell due; reads begin at least an interval apart and, while attempts run, at most an interval
    (or one `ps`, if slower) and a tick apart; a guardian that dies is seen within that and one more read.
    0.72 s is what one table read took in the 2026-09-25 review at load 40 to 60; 1.5 s is slower than the interval."""
    with_launch(daemon, monkeypatch)
    clock, tick_s, interval = [100.0], .05, 1.0
    monkeypatch.setattr(daemon_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    del daemon._process_table                                  # the daemon's own shared table
    daemon._table, daemon._table_lock = (None, 0.0), threading.Lock()
    daemon._table_next = 0.0                                   # (845d663's name for the expiry)
    daemon.inspect_interval_s = interval
    attempts = [add_running(daemon, JOB + "-b", 5252), ATTEMPT, add_running(daemon, JOB + "-c", 6262)]
    reads: list[tuple[ProcessTable, float]] = []               # each table read, and when its read began

    def snapshot():
        started = clock[0]
        clock[0] += read_s                                     # the kernel is read as `ps` starts
        table = table_showing(*[(pid, 1, pid, "Ss") for pid in (4242, 5252, 6262) if pid != 4242 or started < died_at])
        reads.append((table, started))
        return table
    monkeypatch.setattr(daemon_module.procs, "snapshot", snapshot)
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: "dead")   # asked once the table stops showing it
    uses, asking = [], {}
    daemon._record_owned = lambda a, table: uses.append((a["attempt_id"], *asking[a["attempt_id"]],
                                                         next(t for read, t in reads if read is table)))
    daemon._contain = lambda a: EMPTY
    seen_at = None
    while clock[0] < died_at + 3 + read_s:
        for aid in attempts:
            asking[aid] = (clock[0], daemon._inspect_next.get(aid, clock[0]))   # when it asks, and when it fell due
            daemon._process_attempt(aid)
            if seen_at is None and attempt(daemon)["state"] != "running":
                seen_at = clock[0]                             # before a later attempt's read moves the clock
        clock[0] += tick_s
    gaps = [later - earlier for (_, earlier), (_, later) in zip(reads, reads[1:])]
    slowest = max(interval, read_s)
    assert len(uses) > 3 * len(reads) / 2                      # the table was shared
    for aid in attempts:
        mine = [began for who, _, _, began in uses if who == aid]
        assert mine == sorted(set(mine))                       # a newer table each time
    assert all(due - began < interval for _, _, due, began in uses)
    if read_s < interval:
        assert max(asked - began for _, asked, _, began in uses) < interval
    assert min(gaps) >= interval - 1e-9
    assert max(gaps) <= slowest + tick_s + 1e-9
    assert attempt(daemon)["state"] == "lost"
    assert seen_at - died_at <= slowest + tick_s + read_s + 1e-9


def test_c5_12_only_the_inspection_that_reads_waits_for_ps(daemon, monkeypatch):
    """C-5.12 an inspection that finds another's read running takes the last table if it is newer than the one it
    last had, and otherwise returns and asks again next tick: a slow or hung `ps` holds one worker, not one per
    running attempt, and the others' receipts are still read every tick."""
    del daemon._process_table                                  # the daemon's own shared table
    daemon._table, daemon._table_lock = (table_showing((4242, 1, 4242, "Ss")), 50.0), threading.Lock()
    daemon._table_next = 0.0                                   # (845d663's name for the expiry)
    monkeypatch.setattr(daemon_module.procs, "snapshot", lambda: pytest.fail("a read is already running"))
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: pytest.fail("nothing is asked singly"))
    daemon._contain = never_census

    def tick():
        worker = threading.Thread(target=daemon._process_attempt, args=(ATTEMPT,), daemon=True)
        worker.start()
        worker.join(5)
        return not worker.is_alive()
    daemon._table_lock.acquire()                               # another attempt's `ps` is running
    try:
        daemon._inspect_next[ATTEMPT] = 50.0                  # its last table expired at 50: nothing newer yet
        assert tick(), "the inspection waited for another attempt's read"
        assert daemon._inspect_next[ATTEMPT] == 50.0
        assert "owned_identities" not in json.loads(attempt(daemon)["evidence_json"])
        daemon._inspect_next[ATTEMPT] = 49.5                  # it last had an older table: the last one is newer
        assert tick() and daemon._inspect_next[ATTEMPT] == 50.0
        assert "4242" in json.loads(attempt(daemon)["evidence_json"])["owned_identities"]
        other = add_running(daemon, JOB + "-b", 5252)          # a new attempt, never inspected
        daemon._table = (table_showing((5252, 1, 5252, "Ss")), 50.0)
        worker = threading.Thread(target=daemon._process_attempt, args=(other,), daemon=True)
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
        due = daemon._inspect_next[other]                      # due from when it first asked, not from each retry
        daemon._table = (daemon._table[0], due + .5)           # so the read that was running serves it
        daemon._process_attempt(other)
        assert daemon._inspect_next[other] == due + .5
        assert "5252" in json.loads(daemon.store.get_attempt(other)["evidence_json"])["owned_identities"]
        daemon._inspect_next[ATTEMPT] = 50.0
        publish_receipt(daemon, rc=0)                          # its receipt is read on the next tick, read or no read
        assert tick() and attempt(daemon)["state"] == "finalizing"
    finally:
        daemon._table_lock.release()



def test_c5_12_a_read_that_ended_while_an_inspection_asked_is_taken_not_repeated(daemon, monkeypatch):
    """C-5.12 an inspection that found the table expired and then waited its turn at the lock while another's read
    ended takes that read's table: reads begin at least an interval apart, never back to back.

    Final review of PR #37, 2026-09-26: dropping the re-check inside the lock survived every test (mutant M16)."""
    del daemon._process_table                                  # the daemon's own shared table
    daemon.inspect_interval_s = 1.0
    real = threading.Lock()
    at_lock, other_done = threading.Event(), threading.Event()
    reads = []

    class Lock:
        """The table's lock, reached just as another inspection's read ends."""
        def acquire(self, blocking=True):
            at_lock.set()
            assert other_done.wait(5), "the other read never ended"
            return real.acquire(blocking=blocking)

        def release(self):
            real.release()
    daemon._table, daemon._table_lock = (None, 0.0), Lock()
    monkeypatch.setattr(daemon_module.procs, "snapshot",
                        lambda: reads.append(1) or table_showing((4242, 1, 4242, "Ss")))
    result = []
    worker = threading.Thread(target=lambda: result.append(Daemon._process_table(daemon, 100.0)), daemon=True)
    worker.start()
    assert at_lock.wait(5)
    other = (table_showing((4242, 1, 4242, "Ss")), 100.0 + 1.0)   # its read began at 100 and has just ended
    daemon._table = other
    other_done.set()
    worker.join(5)
    assert not worker.is_alive()
    assert reads == [], "a second read began as the other ended"
    assert result == [other]

def test_c5_12_only_the_inspection_that_reads_waits_for_the_boot_identity(daemon, monkeypatch):
    """C-5.12 the reader reads the table's boot identity before any other attempt is given the table, so a slow
    `sysctl` holds the reader's worker only: the others return at once and ask again next tick, and then share
    that one read.

    Final review of PR #37, 2026-09-26: each attempt given the table read its boot identity itself, behind the
    table's lock, so one 2 s `sysctl` held all four inspecting workers for 1.8 to 2.2 s."""
    del daemon._process_table                                  # the daemon's own shared table
    daemon._table, daemon._table_lock = (None, 0.0), threading.Lock()
    daemon.inspect_interval_s = 30                             # one table serves the whole test
    others = [add_running(daemon, JOB + "-b", 5252), add_running(daemon, JOB + "-c", 6262)]
    reading, release, boot_reads = threading.Event(), threading.Event(), []

    def slow_boot_id():
        boot_reads.append(1)
        reading.set()
        assert release.wait(30), "the boot identity read was never released"
        return "boot"
    monkeypatch.setattr(daemon_module.procs, "boot_id", slow_boot_id)
    monkeypatch.setattr(daemon_module.procs, "snapshot",
                        lambda: ProcessTable({pid: (1, pid, "Ss", STARTED) for pid in (4242, 5252, 6262)}))
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: pytest.fail("nothing is asked singly"))
    daemon._contain = never_census
    reader = threading.Thread(target=daemon._process_attempt, args=(ATTEMPT,), daemon=True)
    reader.start()
    try:
        assert reading.wait(10), "the reader never read the boot identity"
        for aid in others:                                     # they ask while its `sysctl` runs
            worker = threading.Thread(target=daemon._process_attempt, args=(aid,), daemon=True)
            worker.start()
            worker.join(20)
            assert not worker.is_alive(), f"{aid} waited for the reader's sysctl"
    finally:
        release.set()
        reader.join(10)
    assert not reader.is_alive()
    for aid in others:                                         # their next tick: the table, its one boot read
        daemon._process_attempt(aid)
    assert boot_reads == [1]
    for aid, pid in zip([ATTEMPT, *others], (4242, 5252, 6262)):
        assert str(pid) in json.loads(daemon.store.get_attempt(aid)["evidence_json"])["owned_identities"]



def test_c5_12_a_table_is_published_however_its_boot_identity_read_ends(daemon, monkeypatch):
    """C-5.12, C-5.10 a boot-identity read that raises something other than an inspection failure costs the reader's
    pass (C-5.10 retries it), not the interval's ration: the table is still published, so the next attempt to ask
    is given it rather than reading `ps` again."""
    del daemon._process_table                                  # the daemon's own shared table
    daemon._table, daemon._table_lock = (None, 0.0), threading.Lock()
    daemon.inspect_interval_s = 30
    other = add_running(daemon, JOB + "-b", 5252)
    reads = []

    def broken():
        raise RuntimeError("not an inspection failure")
    monkeypatch.setattr(daemon_module.procs, "boot_id", broken)
    monkeypatch.setattr(daemon_module.procs, "snapshot",
                        lambda: reads.append(1) or ProcessTable({pid: (1, pid, "Ss", STARTED) for pid in (4242, 5252)}))
    daemon._contain = never_census
    with pytest.raises(RuntimeError):
        daemon._process_attempt(ATTEMPT)
    assert reads == [1] and daemon._table[0] is not None
    with pytest.raises(RuntimeError):                          # the other asks the same table's boot identity
        daemon._process_attempt(other)
    assert reads == [1]

def test_c5_12_a_failed_shared_read_is_all_that_an_outage_costs_an_interval(daemon, monkeypatch):
    """C-5.12, C-4.2 when this interval's table could not be read, no attempt asks about its guardian singly,
    and nothing is decided until a read works."""
    del daemon._process_table                                  # the daemon's own shared table
    daemon._table, daemon._table_lock = (None, 0.0), threading.Lock()
    attempts = [ATTEMPT, add_running(daemon, JOB + "-b", 5252), add_running(daemon, JOB + "-c", 6262)]
    reads, asked = [], []

    def failing():
        reads.append(1)
        raise daemon_module.procs.InspectionError("ps timed out")
    monkeypatch.setattr(daemon_module.procs, "snapshot", failing)
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: asked.append(args) or "unknown")
    daemon._contain = never_census
    for aid in attempts:
        daemon._process_attempt(aid)
    assert reads == [1] and asked == []
    assert [daemon.store.get_attempt(aid)["state"] for aid in attempts] == ["running"] * 3
    assert all(daemon._inspect_next[aid] == daemon._table[1] for aid in attempts)


def test_c5_12_a_boot_identity_the_table_cannot_read_is_read_once_and_decides_nothing(daemon, monkeypatch):
    """C-5.12, C-4.2 every attempt that asks shares the table's one boot-identity read; when it fails, no
    attempt asks singly and nothing is decided."""
    del daemon._process_table                                  # the daemon's own shared table
    daemon._table, daemon._table_lock = (None, 0.0), threading.Lock()
    attempts = [ATTEMPT, add_running(daemon, JOB + "-b", 5252), add_running(daemon, JOB + "-c", 6262)]
    boot_reads, asked = [], []

    def unavailable():
        boot_reads.append(1)
        raise daemon_module.procs.InspectionError("macOS boot identity is unavailable")
    monkeypatch.setattr(daemon_module.procs, "boot_id", unavailable)
    monkeypatch.setattr(daemon_module.procs, "snapshot",
                        lambda: ProcessTable({pid: (1, pid, "Ss", STARTED) for pid in (4242, 5252, 6262)}))
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: asked.append(args) or "unknown")
    daemon._contain = never_census
    for aid in attempts:
        daemon._process_attempt(aid)
    assert boot_reads == [1] and asked == []
    assert [daemon.store.get_attempt(aid)["state"] for aid in attempts] == ["running"] * 3


def test_c5_12_a_table_whose_uuid_read_fell_back_decides_nothing_for_uuid_records(daemon, monkeypatch):
    """C-5.3, C-5.12 one failed UUID read inside the shared table's boot read is a failed read: no attempt
    recorded with the UUID asks singly or reads a table of its own, and nothing is decided."""
    session = "11111111-1111-4111-8111-111111111111"
    del daemon._process_table                                  # the daemon's own shared table
    daemon._table, daemon._table_lock = (None, 0.0), threading.Lock()
    daemon._table_next = 0.0                                   # (845d663's name for the expiry)
    attempts = [ATTEMPT, add_running(daemon, JOB + "-b", 5252), add_running(daemon, JOB + "-c", 6262)]
    for aid in attempts:
        daemon.store.update_attempt(aid, boot_id=session)
    reads, asked = [], []

    def snapshot():
        reads.append(1)
        return ProcessTable({pid: (1, pid, "Ss", STARTED) for pid in (4242, 5252, 6262)}, "1726000000")
    monkeypatch.setattr(daemon_module.procs, "snapshot", snapshot)
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: asked.append(args) or "alive")
    daemon._contain = never_census
    for aid in attempts:
        daemon._process_attempt(aid)
    assert reads == [1] and asked == []
    assert [daemon.store.get_attempt(aid)["state"] for aid in attempts] == ["running"] * 3
    assert all("owned_identities" not in json.loads(daemon.store.get_attempt(aid)["evidence_json"]) for aid in attempts)


def test_c5_12_a_guardian_recorded_with_a_legacy_boot_timestamp_still_has_its_group_owned(daemon, monkeypatch):
    """C-5.3, C-5.6, C-5.12 the shared table says "alive" for a `kern.boottime` record that C-5.3 matches, with no
    fresh read, and its group's members are recorded as owned from that table, as `same_process` would allow."""
    session = "11111111-1111-4111-8111-111111111111"
    daemon.store.update_attempt(ATTEMPT, boot_id="1726000000")
    table = ProcessTable({4242: (1, 4242, "Ss", STARTED), 4243: (4242, 4242, "S", STARTED)}, session)

    def read(argv, *, empty_ok=False):
        assert argv[-1] == "kern.boottime", argv
        return "{ sec = 1726000000, usec = 0 } Sat Sep 10 10:00:00 2024\n"
    monkeypatch.setattr(daemon_module.procs, "_read", read)
    daemon._process_table = shared(table)
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: pytest.fail("the table matched it itself"))
    monkeypatch.setattr(daemon_module.procs, "snapshot", lambda: pytest.fail("no table of its own"))
    daemon._contain = never_census
    daemon._process_attempt(ATTEMPT)
    owned = json.loads(attempt(daemon)["evidence_json"])["owned_identities"]
    assert owned == {"4242": {"pid": 4242, "boot_id": session, "proc_start": STARTED},
                     "4243": {"pid": 4243, "boot_id": session, "proc_start": STARTED}}


def publish_start(core):
    atomic_publish(attempt_dir(core.root, JOB, 1) / "start.json",
                   json.dumps({"guardian_pid": 4242, "pgid": 4242, "boot_id": "boot", "proc_start": STARTED,
                               "started_at": "2026-09-05T14:00:00Z"}).encode())


@pytest.mark.parametrize("census", [EMPTY, GUARDIAN_ONLY], ids=["guardian-gone", "guardian-exiting"])
def test_c4_2_start_grace_reads_the_receipts_again_after_its_census(daemon, monkeypatch, census):
    """C-4.2, C-5.5 a guardian that wrote both receipts while start grace ran its census finished its attempt:
    it is neither released to run again (`starting-no-receipt`) nor quarantined."""
    with_launch(daemon, monkeypatch)
    daemon.store.update_attempt(ATTEMPT, state="starting")
    daemon.start_grace_s, daemon._starting_deadlines[ATTEMPT] = 10, 0.0   # start grace is over

    def contain(a):
        publish_start(daemon)
        publish_receipt(daemon, rc=0)                          # the provider ran and exited 0 meanwhile
        return census
    daemon._contain = contain
    daemon._process_attempt(ATTEMPT)
    assert attempt(daemon)["state"] == "starting"               # nothing decided from the census
    daemon._contain = never_census
    daemon._process_attempt(ATTEMPT)                            # the next tick reads the receipts
    a = attempt(daemon)
    assert a["state"] == "finalizing" and a["rc"] == 0 and a["outcome_detail"] is None
    assert daemon.store.get_job(JOB)["state"] == "running"
    kinds = [row["kind"] for row in daemon.store.list_events(JOB)]
    assert "attempt.no_launch" not in kinds and "attempt.quarantined" not in kinds


@pytest.mark.parametrize(("census", "state", "detail"),
                         [(EMPTY, "failed", "starting-no-receipt"), (GUARDIAN_ONLY, "quarantined", None)],
                         ids=["empty", "guardian-alive"])
def test_c4_2_start_grace_without_a_receipt_still_decides_from_its_census(daemon, monkeypatch, census, state, detail):
    """C-4.2 no receipt after the census: an empty census releases and retries, anything else quarantines."""
    with_launch(daemon, monkeypatch)
    daemon.store.update_attempt(ATTEMPT, state="starting")
    daemon.start_grace_s, daemon._starting_deadlines[ATTEMPT] = 10, 0.0
    daemon._contain = lambda a: census
    daemon._process_attempt(ATTEMPT)
    a = attempt(daemon)
    assert a["state"] == state
    if detail:
        assert a["outcome_detail"] == detail
    else:
        assert json.loads(a["quarantine_reason"])["reason"] == "start grace expired without a receipt"



def test_c4_2_start_grace_with_start_json_alone_is_a_running_attempt(daemon, monkeypatch):
    """C-4.2 the guardian wrote `start.json` (only) while start grace ran its census, which shows it alive: its
    provider is running, so the next tick reads `start.json` and the attempt runs; it is not quarantined.

    Final review of PR #37, 2026-09-26: re-reading only `exit.json` after the census survived every test, and
    quarantined the attempt that, under load, is the likely one: `start.json` late, the guardian alive."""
    with_launch(daemon, monkeypatch)
    daemon.store.update_attempt(ATTEMPT, state="starting")
    daemon.start_grace_s, daemon._starting_deadlines[ATTEMPT] = 10, 0.0   # start grace is over

    def contain(a):
        publish_start(daemon)                                  # the provider is running; no exit.json yet
        return GUARDIAN_ONLY
    daemon._contain = contain
    daemon._process_attempt(ATTEMPT)
    assert attempt(daemon)["state"] == "starting"               # nothing decided from the census
    daemon._contain = never_census
    daemon._process_table = shared(table_showing((4242, 1, 4242, "Ss")))
    daemon._process_attempt(ATTEMPT)                            # the next tick reads start.json
    assert attempt(daemon)["state"] == "running"
    kinds = [row["kind"] for row in daemon.store.list_events(JOB)]
    assert "attempt.quarantined" not in kinds and "attempt.no_launch" not in kinds


@pytest.mark.parametrize("census", [EMPTY, GUARDIAN_ONLY], ids=["empty", "guardian-alive"])
@pytest.mark.parametrize("empty", ["{}", "[]", "null"])
def test_c4_2_start_grace_takes_only_a_receipt_the_tick_would_take(daemon, monkeypatch, census, empty):
    """C-4.2 after its census, start grace counts a receipt as the tick does, only when it reads as a value:
    `start.json` and `exit.json` holding `{}`, `[]` or `null` are no receipt, so the census decides at once.

    Final review of PR #37, 2026-09-26: the re-read tested `.exists()`, and the tick then ignored such a file, so
    the attempt stayed `starting` and took a full census, the environment scan included, on every tick."""
    with_launch(daemon, monkeypatch)
    daemon.store.update_attempt(ATTEMPT, state="starting")
    daemon.start_grace_s, daemon._starting_deadlines[ATTEMPT] = 10, 0.0
    adir, censuses = attempt_dir(daemon.root, JOB, 1), []

    def contain(a):
        censuses.append(1)
        atomic_publish(adir / "start.json", empty.encode())
        atomic_publish(adir / "exit.json", empty.encode())
        return census
    daemon._contain = contain
    daemon._process_attempt(ATTEMPT)
    a = attempt(daemon)
    assert censuses == [1]
    if census is EMPTY:
        assert a["state"] == "failed" and a["outcome_detail"] == "starting-no-receipt"
    else:
        assert a["state"] == "quarantined"
        assert json.loads(a["quarantine_reason"])["reason"] == "start grace expired without a receipt"

def test_c5_12_the_control_loop_forgets_the_inspection_clock_of_an_attempt_that_is_not_live(daemon):
    """C-5.12 `_inspect_next` keeps an entry only for a live attempt: each control tick drops the rest."""
    ended = add_running(daemon, JOB + "-b", 5252)
    daemon.store.update_attempt(ended, state="failed")
    daemon._inspect_next = {ATTEMPT: 5.0, ended: 5.0, "20260905-090000-gone/a1": 5.0}
    scheduled, ticks = [], []
    daemon._schedule = lambda key, *args, **kwargs: scheduled.append(key)
    daemon._recovery_complete, daemon._last_maintenance, daemon.tick_s = threading.Event(), time.monotonic(), .05
    daemon.stopping = SimpleNamespace(is_set=lambda: ticks.append(1) or len(ticks) > 1, wait=lambda timeout: False)
    daemon._control()                                          # one tick
    assert daemon._inspect_next == {ATTEMPT: 5.0}
    assert ATTEMPT in scheduled and ended not in scheduled
