"""C-5.11, the 2026-09-24 wedge: what running attempts, probes and waiters cost.

With two or three attempts running and their callers waiting, the daemon
stopped answering in time (a `daemon.status` took 38.8 s; every client gave up
at 3-15 s). Each running attempt was re-offered to a worker every 50 ms tick,
and each of those passes asked `ps` about its guardian with three subprocesses
(`ps -p lstart`, `ps -p stat`, `sysctl kern.bootsessionuuid`); every 0.5 s it
also ran the full C-5.5 census, whose marker source dumps every process's
environment (`ps -axEww`, 2.3 MB on that machine), only to keep the group
members. A running probe did the same. Every one of those passes then woke
every `wait` caller, which re-read every job it watched behind the one store
lock, and every tick read every job ever accepted to look for pending exports.
These tests pin what each of those may cost, so the load stays flat as
attempts, probes, waiters and history are added. Since PR #37 (C-5.12) a
running attempt is inspected from one process table every attempt shares, so
its cases pin that table's cost; a probe keeps C-5.11's own pacing.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from subfleet import daemon as daemon_module
from subfleet import procs, protocol
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.daemon import Daemon

JOB = "20260924-114650-load-probe"
ATTEMPT = JOB + "/a1"
BOOT = "0f1e2d3c-4b5a-4968-8776-655443322110"   # synthetic
STARTED = "Thu Sep 24 11:46:51 2026"
GUARDIAN, CHILD, PGID = 4242, 4243, 4242
GROUP_SNAPSHOT = ["/bin/ps", "-axo", "pid=,pgid=,stat=,lstart="]
TABLE = procs.TABLE_ARGV                      # C-5.12's shared table
UUID_READ = ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"]


class FakePs:
    """Answers `procs._read` like macOS would for one guardian and its child.

    `starts` is each live pid's `lstart`; changing a pid's entry models a new
    process that has taken that pid. `failing` names the reads that fail
    ("table", "sysctl", "ps -p"), and `hidden` the pids the table does not show.
    """

    def __init__(self):
        self.calls: list[list[str]] = []
        self.starts = {GUARDIAN: STARTED, CHILD: STARTED, 4300: STARTED}
        self.failing: set[str] = set()
        self.hidden: set[int] = set()

    def __call__(self, argv, *, empty_ok=False):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        if argv[0].endswith("sysctl"):
            if "sysctl" in self.failing:
                raise procs.InspectionError("sysctl inspection unavailable")
            return BOOT + "\n" if argv[-1] == "kern.bootsessionuuid" else "{ sec = 1790255587, usec = 0 }\n"
        if argv == TABLE:
            if "table" in self.failing:
                raise procs.InspectionError("ps inspection unavailable")
            rows = [(GUARDIAN, 1, PGID, "Ss"), (CHILD, GUARDIAN, PGID, "R"), (4300, 1, 4300, "S")]
            return "".join(f"{pid} {ppid} {pgid} {stat:<4} {self.starts[pid]}\n"
                           for pid, ppid, pgid, stat in rows if pid not in self.hidden) + \
                f"4301 4300 4300 Z    {STARTED}\n"
        if argv[1:2] == ["-p"] and "ps -p" in self.failing:
            raise procs.InspectionError("ps inspection unavailable")
        if argv == GROUP_SNAPSHOT:
            return (f"{GUARDIAN} {PGID} Ss   {self.starts[GUARDIAN]}\n"
                    f"{CHILD} {PGID} R    {self.starts[CHILD]}\n"
                    f"4300 4300 S    {self.starts[4300]}\n4301 4300 Z    {STARTED}\n")
        if "pid=,ppid=,pgid=,stat=" in argv:
            return (f"{GUARDIAN} 1 {PGID} Ss\n{CHILD} {GUARDIAN} {PGID} R\n"
                    "4300 1 4300 S\n4301 4300 4300 Z\n")
        if "pid=,command=" in argv:
            return ""
        if argv[1:2] == ["-p"] and argv[-1] == "lstart=":
            return self.starts.get(int(argv[2]), "") + "\n"
        if argv[1:2] == ["-p"] and argv[-1] == "stat=":
            return "S\n" if int(argv[2]) in self.starts else ""
        raise AssertionError(argv)

    def environment_dumps(self):
        return [argv for argv in self.calls if "-axEww" in argv]

    def tables(self):
        return [argv for argv in self.calls if argv == TABLE]


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    core = Daemon(tmp_path / "state", inspect_interval_s=30)
    # The constructor recorded this machine's real boot identity; the attempt
    # below belongs to the fake one, so each test starts with nothing cached.
    procs.forget_boot_id()      # C-5.12's cache; conftest also clears it per test
    home = tmp_path / "home"
    core.store.put_lane(Lane("codex-1", "codex", "codex:test", Credential("codex", str(home), "home"),
                             str(home), LaneOwner.V2, False))
    core.store.add_job(job_id=JOB, request_id="load-1", payload_digest="digest", kind="run",
                       state="running", workdir=str(tmp_path), prompt_path=str(tmp_path / "prompt.md"),
                       sandbox="read-only")
    core.store.add_attempt(attempt_id=ATTEMPT, job_id=JOB, seq=1, lane_id="codex-1",
                           model_requested="astra", state="running", guardian_pid=GUARDIAN,
                           child_pid=CHILD, pgid=PGID, boot_id=BOOT, proc_start=STARTED,
                           started_at="2026-09-24T11:46:51Z", evidence_json="{}")
    yield core
    core.close()


def owned(core) -> dict:
    return json.loads(core.store.get_attempt(ATTEMPT)["evidence_json"]).get("owned_identities", {})


def count_liveness(monkeypatch) -> list:
    asked = []
    liveness = procs.liveness
    monkeypatch.setattr(daemon_module.procs, "liveness",
                        lambda *args: asked.append(args) or liveness(*args))
    return asked


# --- procs --------------------------------------------------------------------

def test_boot_session_uuid_is_reused_and_boottime_never_is(monkeypatch):
    """C-5.12, C-5.3: one read of the boot-session UUID serves `BOOT_ID_TTL_S`; legacy
    boottime seconds can shift, and a UUID read that fell back to them is not kept."""
    ps = FakePs()
    monkeypatch.setattr(procs, "_read", ps)
    assert [procs.boot_id() for _ in range(5)] == [BOOT] * 5
    assert sum(argv[0].endswith("sysctl") for argv in ps.calls) == 1

    procs.forget_boot_id()      # C-5.12's cache; conftest also clears it per test
    legacy = []
    def seconds_only(argv, *, empty_ok=False):
        legacy.append(argv)
        return "" if argv[-1] == "kern.bootsessionuuid" else "{ sec = 100, usec = 1 }"
    monkeypatch.setattr(procs, "_read", seconds_only)
    assert [procs.boot_id() for _ in range(3)] == ["100"] * 3
    assert len(legacy) == 6              # UUID attempt and boottime, each time


def test_group_members_is_one_snapshot_without_environments(monkeypatch):
    """C-5.11, C-5.5: the group source alone: live, non-zombie members and their
    start times from one `ps -axo pid=,pgid=,stat=,lstart=`; no environment is read."""
    ps = FakePs()
    monkeypatch.setattr(procs, "_read", ps)
    assert procs.group_members(PGID) == {GUARDIAN: STARTED, CHILD: STARTED}
    assert procs.group_members(4300) == {4300: STARTED}   # the zombie is not a member
    assert procs.group_members(0) == {}
    assert ps.calls == [GROUP_SNAPSHOT] * 2
    assert not ps.environment_dumps()


def test_group_members_failure_is_an_inspection_error(monkeypatch):
    """C-5.5: a failed snapshot is an inspection failure, never an empty group."""
    def broken(argv, *, empty_ok=False):
        raise procs.InspectionError("ps unavailable")
    monkeypatch.setattr(procs, "_read", broken)
    with pytest.raises(procs.InspectionError):
        procs.group_members(PGID)
    monkeypatch.setattr(procs, "_read", lambda argv, *, empty_ok=False: "x 4242 S Thu\n")
    with pytest.raises(procs.InspectionError):
        procs.group_members(PGID)


# --- running attempts -----------------------------------------------------------

def expire(core):
    """The next interval: the attempt is due and the shared table has expired."""
    core._inspect_next[ATTEMPT] = 0
    core._table = (core._table[0], 0.0)


def test_running_attempt_inspection_shares_one_table_and_never_dumps_environments(daemon, monkeypatch):
    """C-5.11, C-5.12: a second's worth of control ticks (20 at 50 ms) reads one process
    table, asks nothing about the guardian singly, runs no `ps -axEww`, and still
    records the owned group.

    Before C-5.11 the same 20 ticks spent 60 subprocesses on liveness alone
    (three per tick) and a full census with an environment dump; with C-5.11's
    per-attempt pacing it was ten; with the shared table it is two.
    """
    ps = FakePs()
    monkeypatch.setattr(procs, "_read", ps)
    asked = count_liveness(monkeypatch)

    for _ in range(20):
        daemon._process_attempt(ATTEMPT)

    assert asked == []                           # the table shows the guardian
    assert not ps.environment_dumps()
    assert ps.calls == [TABLE, UUID_READ]        # one table, and its boot identity once
    assert set(owned(daemon)) == {str(GUARDIAN), str(CHILD)}
    assert owned(daemon)[str(CHILD)] == {"pid": CHILD, "boot_id": BOOT, "proc_start": STARTED}
    assert daemon.store.get_attempt(ATTEMPT)["state"] == "running"

    # The next interval: one table read (the UUID is remembered), nothing else.
    ps.calls.clear()
    expire(daemon)
    daemon._process_attempt(ATTEMPT)
    assert asked == [] and ps.calls == [TABLE]


def test_a_pid_taken_by_a_new_group_member_is_recorded_afresh(daemon, monkeypatch):
    """C-5.4: a recorded pid now held by a different process in the group gets that
    process's identity, as the full census recorded it, so the kill path can
    signal it individually after it leaves the group."""
    ps = FakePs()
    monkeypatch.setattr(procs, "_read", ps)
    daemon._process_attempt(ATTEMPT)
    assert owned(daemon)[str(CHILD)]["proc_start"] == STARTED

    later = "Thu Sep 24 11:52:07 2026"
    ps.starts[CHILD] = later                  # the child exited; a sibling now has its pid
    expire(daemon)
    daemon._process_attempt(ATTEMPT)
    assert owned(daemon)[str(CHILD)] == {"pid": CHILD, "boot_id": BOOT, "proc_start": later}
    assert owned(daemon)[str(GUARDIAN)]["proc_start"] == STARTED


def failing_record(core, monkeypatch) -> list:
    """Every inspection that finds the guardian alive raises as it records the group (a store write)."""
    failures = []

    def record(a, table):
        failures.append(a["attempt_id"])
        raise RuntimeError("database or disk is full")
    monkeypatch.setattr(core, "_record_owned", record)
    return failures


def test_a_failing_inspection_is_retried_not_skipped(daemon, monkeypatch):
    """C-5.10 with C-5.11: a paced pass that raised leaves no pacing deadline, so the
    retry C-5.10 schedules repeats the inspection (and fails again, keeping its
    backoff) instead of returning at the gate and reading as recovery. It repeats
    it on the table the failed pass had, which is less than an interval old, so a
    failure costs no `ps` of its own."""
    ps = FakePs()
    monkeypatch.setattr(procs, "_read", ps)
    failures = failing_record(daemon, monkeypatch)
    for n in range(1, 4):
        with pytest.raises(RuntimeError):
            daemon._process_attempt(ATTEMPT)
        assert len(failures) == n
        assert ATTEMPT not in daemon._inspect_next
    assert ps.tables() == [TABLE]


def settle_key(core, key: str) -> None:
    """Wait, bounded, until `_schedule`'s worker for `key` has finished and been counted."""
    deadline = time.monotonic() + 30
    while True:
        with core._busy_lock:
            if key not in core._busy:
                return
        assert time.monotonic() < deadline, f"{key} is still running"
        time.sleep(.01)


def scheduled(core) -> int:
    """One pass of the attempt through C-5.10's `_schedule`, its retry due now; its failure count after."""
    core._worker_retry_at[ATTEMPT] = 0
    core._schedule(ATTEMPT, core._process_attempt, ATTEMPT, paced=True)
    settle_key(core, ATTEMPT)
    return core._worker_failures.get(ATTEMPT, 0)


def test_a_retry_that_finds_another_read_running_keeps_its_backoff(daemon, monkeypatch):
    """C-5.10, C-5.11: a retry that finds another attempt's `ps` read running has not repeated the inspection
    that raised, so it neither clears C-5.10's count nor adds to it, and the next failure is the second.

    Review of the merge, 2026-09-26: it returned normally, which `_schedule` counts as recovery. Whenever a
    read took 0.5 s or more every retry landed inside the next read, so the count went 1, 0, 1, the backoff
    never grew, and every failure was logged as the first (118 raises and log lines in 120 s at 0.72 s a read,
    against 8 raises and 4 lines)."""
    ps = FakePs()
    monkeypatch.setattr(procs, "_read", ps)
    failures = failing_record(daemon, monkeypatch)
    logged = []
    monkeypatch.setattr(daemon.log, "error", lambda msg, *args: logged.append(msg % args))
    counts = [scheduled(daemon)]                           # the inspection raises
    daemon._table = (daemon._table[0], 0.0)                # the table has expired ...
    assert daemon._table_lock.acquire(blocking=False)      # ... and another attempt is reading `ps`
    try:
        counts.append(scheduled(daemon))                   # the retry cannot inspect
    finally:
        daemon._table_lock.release()
    counts.append(scheduled(daemon))                       # the next one inspects, and raises again
    assert counts == [1, 1, 2]
    assert len(failures) == 2 and len(ps.tables()) == 2
    assert [line for line in logged if ATTEMPT in line] == [
        f"worker {ATTEMPT} failed: RuntimeError (1 in a row, next try in 0.5 s)",
        f"worker {ATTEMPT} failed: RuntimeError (2 in a row, next try in 1 s)"]


@pytest.mark.parametrize("cannot", ["table", "boot identity", "guardian", "recording table"])
def test_a_retry_that_cannot_inspect_keeps_its_backoff(daemon, monkeypatch, cannot):
    """C-5.10, C-5.11, C-4.2: nor is it recovery when the retry is given a table whose read failed, or whose boot
    identity cannot be read, or cannot inspect the guardian (its liveness is unknown), or cannot read a table
    to record the group from. Each decides nothing, and until the interval ends the retry waits at the pacing
    gate, which is no recovery either. The first pass that inspects to the end raises again: the second failure."""
    ps = FakePs()
    monkeypatch.setattr(procs, "_read", ps)
    failures = failing_record(daemon, monkeypatch)
    snapshot, liveness = procs.snapshot, procs.liveness
    counts = [scheduled(daemon)]                           # the inspection raises
    daemon._table = (daemon._table[0], 0.0)                # the next interval's read ...
    if cannot == "table":
        ps.failing.add("table")                            # ... fails
    elif cannot == "boot identity":
        procs.forget_boot_id()
        ps.failing.add("sysctl")                           # ... cannot read its boot identity
    elif cannot == "guardian":
        ps.hidden.add(GUARDIAN)                            # ... does not show the guardian,
        ps.failing.add("ps -p")                            # which cannot be asked about singly
    else:
        ps.hidden.add(GUARDIAN)                            # ... does not show the guardian, which is alive,
        reads = []                                         # and no second table can be read to record from

        def shared_read_only():
            reads.append(1)
            if len(reads) > 1:
                raise procs.InspectionError("ps inspection unavailable")
            return snapshot()
        monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: "alive")
        monkeypatch.setattr(daemon_module.procs, "snapshot", shared_read_only)
    counts.append(scheduled(daemon))                       # the retry cannot inspect
    assert daemon.store.get_attempt(ATTEMPT)["state"] == "running" and len(failures) == 1
    counts.append(scheduled(daemon))                       # inside the interval: the pacing gate
    ps.failing.clear(), ps.hidden.clear()
    monkeypatch.setattr(daemon_module.procs, "snapshot", snapshot)
    monkeypatch.setattr(daemon_module.procs, "liveness", liveness)
    expire(daemon)
    counts.append(scheduled(daemon))                       # inspects, and raises again
    assert counts == [1, 1, 1, 2]
    assert len(failures) == 2


def test_paced_inspection_still_reads_the_receipt_every_tick(daemon, monkeypatch):
    """C-5.11, C-4.2: pacing covers the `ps` questions only. An exit receipt written
    between two inspections moves the attempt to `finalizing` on the next tick."""
    ps = FakePs()
    monkeypatch.setattr(procs, "_read", ps)
    daemon._process_attempt(ATTEMPT)                  # inspected; the next is 30 s away
    adir = daemon_module.attempt_dir(daemon.root, JOB, 1)
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "exit.json").write_text(json.dumps({"rc": 0, "signal": None, "child_pid": CHILD,
                                                "finished_at": "2026-09-24T11:50:00Z"}))
    daemon._process_attempt(ATTEMPT)
    assert daemon.store.get_attempt(ATTEMPT)["state"] == "finalizing"


def test_pacing_state_is_dropped_once_an_attempt_is_no_longer_live(daemon, monkeypatch):
    """C-5.11, C-5.12: an attempt normally becomes terminal inside its own worker pass and
    is never offered again, so the control loop drops its inspection entry against
    the live set it reads each tick."""
    monkeypatch.setattr(procs, "_read", FakePs())
    daemon._process_attempt(ATTEMPT)
    assert ATTEMPT in daemon._inspect_next
    with daemon.store.transaction("test.finished") as tx:
        tx.execute("UPDATE attempts SET state='succeeded' WHERE attempt_id=?", (ATTEMPT,))
    live = {a["attempt_id"] for a in daemon.store.query(daemon_module.LIVE_ATTEMPTS)}
    daemon._forget_paced(live)
    assert ATTEMPT not in daemon._inspect_next


# --- probes -------------------------------------------------------------------

def test_a_running_probe_is_inspected_on_the_same_budget(daemon, monkeypatch, tmp_path):
    """C-5.11: a probe's wait loop reads its receipt every 50 ms but asks `ps` about
    its guardian once per liveness interval and records its group without an
    environment dump. Before the fix it asked every pass and took the full
    census every 0.5 s."""
    ps = FakePs()
    monkeypatch.setattr(procs, "_read", ps)
    asked = count_liveness(monkeypatch)
    monkeypatch.setattr(daemon, "_contain_probe", lambda record: True)
    directory = tmp_path / "probe"
    directory.mkdir()
    record = {"holder": "probe:admission:" + JOB, "job_id": JOB, "lane_id": "codex-1",
              "directory": str(directory), "state": "running", "guardian_pid": GUARDIAN,
              "pgid": PGID, "boot_id": BOOT, "proc_start": STARTED,
              "deadline_at": "2999-01-01T00:00:00Z", "owned_identities": {}}
    result = {}
    waiter = threading.Thread(target=lambda: result.update(zip(("safe", "receipt"),
                                                               daemon._await_probe(record))))
    waiter.start()
    time.sleep(1.2)                                # about 24 passes of the loop
    (directory / "exit.json").write_text(json.dumps({"rc": 0, "child_pid": CHILD}))
    waiter.join(timeout=5)
    assert not waiter.is_alive()
    assert result["safe"] is True and result["receipt"]["rc"] == 0
    assert len(asked) == 2                         # the pass's question and the leader re-check
    assert not ps.environment_dumps()
    assert set(record["owned_identities"]) == {str(GUARDIAN), str(CHILD)}


# --- exports --------------------------------------------------------------------

def test_the_export_sweep_is_one_statement_however_much_history_is_kept(daemon, monkeypatch):
    """C-5.11: each tick looks for accepted jobs that still hold a lease with one
    statement. It used to read every job ever accepted and query its leases one
    by one: 266 statements a tick with 265 retained jobs."""
    for n in range(200):
        daemon.store.add_job(job_id=f"20260924-0000{n:03d}-done", request_id=f"done-{n}",
                             payload_digest="d", kind="run", state="succeeded", workdir="/w",
                             prompt_path="/p", sandbox="read-only", accepted_attempt_id=f"x/a{n}")
    pending = "20260924-0000150-done"
    with daemon.store.transaction("test.lease") as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES ('out:/x',?,'t')", (pending,))
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES ('out:/y',?,'t')", (pending,))
    statements = []
    for name in ("query", "one"):
        real = getattr(daemon.store, name)
        monkeypatch.setattr(daemon.store, name,
                            lambda *a, _real=real, **k: statements.append(a[0]) or _real(*a, **k))
    assert daemon._pending_exports() == [pending]
    assert len(statements) == 1


# --- waiters ------------------------------------------------------------------

def test_wait_rereads_the_store_only_after_a_commit(daemon, monkeypatch):
    """C-5.11, C-15.5: wake-ups without a committed change read nothing. Before the
    fix every wake-up re-read every watched job; 200 wake-ups were 200 reads. The
    waiter now reads when it starts and when its job has ended; the hub between
    them reads only after a commit."""
    daemon.wait_hub.recheck_s = 60                              # only commits may cause a read here
    reads = []
    get_job = daemon.store.get_job
    monkeypatch.setattr(daemon.store, "get_job", lambda job_id: reads.append(job_id) or get_job(job_id))
    result = {}
    waiter = threading.Thread(target=lambda: result.update(
        daemon.wait(protocol.WaitArgs(job_ids=[JOB], deadline_s=20))))
    waiter.start()
    while not reads or daemon.wait_hub.reads < 1:
        time.sleep(.001)
    hub_reads = daemon.wait_hub.reads
    for _ in range(200):
        daemon._notify()
        time.sleep(.001)
    assert len(reads) == 1
    assert daemon.wait_hub.reads == hub_reads

    with daemon.store.transaction("test.finished", job_id=JOB) as tx:
        tx.execute("UPDATE jobs SET state='succeeded',rc=0 WHERE job_id=?", (JOB,))
    daemon._notify()
    waiter.join(timeout=5)
    assert not waiter.is_alive()
    assert result["timeout"] is False and result["jobs"][0]["state"] == "succeeded"
    assert len(reads) == 2
    assert daemon.wait_hub.reads == hub_reads + 1


def test_wait_rechecks_on_its_own_clock_without_a_commit(daemon, monkeypatch):
    """C-5.11, C-15.5: the generation is a hint; the hub still looks again on its own clock."""
    daemon.wait_hub.recheck_s = .05
    assert daemon.wait(protocol.WaitArgs(job_ids=[JOB], deadline_s=.6)) == {"timeout": True}
    assert 2 <= daemon.wait_hub.reads <= 20


def test_a_worker_that_commits_nothing_wakes_no_waiter(daemon, monkeypatch):
    """C-5.11: only `wait` listens, and it reads only the store: a worker pass during
    which nothing was committed (a running attempt's tick) has nothing to tell it.
    The wake-up is decided before the key is released, so a released key means
    the decision has been made."""
    woken = []
    monkeypatch.setattr(daemon, "_notify", lambda: woken.append(True))

    def settle(key):
        deadline = time.monotonic() + 5
        while True:
            with daemon._busy_lock:
                if key not in daemon._busy:
                    return
            assert time.monotonic() < deadline
            time.sleep(.005)

    for n in range(20):
        daemon._schedule(f"idle-{n}", lambda: None)
        settle(f"idle-{n}")
    assert woken == []

    def commit():
        with daemon.store.transaction("test.change", job_id=JOB) as tx:
            tx.execute("UPDATE jobs SET wait_reason='x' WHERE job_id=?", (JOB,))
    daemon._schedule("changes", commit)
    settle("changes")
    assert woken == [True]


def test_store_generation_counts_committed_changes_only(tmp_path):
    """C-5.11: the generation moves only when a top-level transaction commits a change."""
    from subfleet.store import Store
    store = Store(tmp_path / "s.sqlite3")
    try:
        start = store.generation
        with store.transaction("test.noop"):
            pass
        assert store.generation == start
        with pytest.raises(RuntimeError):
            with store.transaction("test.rolled-back") as tx:
                tx.execute("INSERT INTO events(ts,kind,data_json) VALUES ('t','k','{}')")
                raise RuntimeError("rollback")
        assert store.generation == start
        with store.transaction("test.outer") as tx:
            tx.execute("INSERT INTO events(ts,kind,data_json) VALUES ('t','k','{}')")
            with store.transaction("test.inner") as inner:
                inner.execute("INSERT INTO events(ts,kind,data_json) VALUES ('t','k','{}')")
            assert store.generation == start       # nothing is visible until the outer commit
        assert store.generation == start + 1
    finally:
        store.close()
