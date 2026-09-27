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
attempts, probes, waiters and history are added. Since C-5.12 a running
attempt uses one shared process table, so its cases assert that tighter cost;
a probe keeps C-5.11's own pacing.
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
TABLE = procs.TABLE_ARGV
UUID_READ = ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"]


class FakePs:
    """Answers `procs._read` like macOS would for one guardian and its child.

    `starts` is each live pid's `lstart`; changing a pid's entry models a new
    process that has taken that pid.
    """

    def __init__(self):
        self.calls: list[list[str]] = []
        self.starts = {GUARDIAN: STARTED, CHILD: STARTED, 4300: STARTED}

    def __call__(self, argv, *, empty_ok=False):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        if argv[0].endswith("sysctl"):
            return BOOT + "\n" if argv[-1] == "kern.bootsessionuuid" else "{ sec = 1790255587, usec = 0 }\n"
        if argv == GROUP_SNAPSHOT:
            return (f"{GUARDIAN} {PGID} Ss   {self.starts[GUARDIAN]}\n"
                    f"{CHILD} {PGID} R    {self.starts[CHILD]}\n"
                    f"4300 4300 S    {self.starts[4300]}\n4301 4300 Z    {STARTED}\n")
        if argv == TABLE:
            return (f"{GUARDIAN} 1 {PGID} Ss   {self.starts[GUARDIAN]}\n"
                    f"{CHILD} {GUARDIAN} {PGID} R    {self.starts[CHILD]}\n"
                    f"4300 1 4300 S    {self.starts[4300]}\n4301 4300 4300 Z    {STARTED}\n")
        if "pid=,command=" in argv:
            return ""
        if argv[1:2] == ["-p"] and argv[-1] == "lstart=":
            return self.starts.get(int(argv[2]), "") + "\n"
        if argv[1:2] == ["-p"] and argv[-1] == "stat=":
            return "S\n" if int(argv[2]) in self.starts else ""
        raise AssertionError(argv)

    def environment_dumps(self):
        return [argv for argv in self.calls if "-axEww" in argv]


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    core = Daemon(tmp_path / "state", inspect_interval_s=30)
    # The constructor recorded this machine's real boot identity; the attempt
    # below belongs to the fake one, so each test starts with nothing cached.
    procs.forget_boot_id()
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
    """C-5.11 with C-5.12: the boot-session UUID is reused for five seconds;
    legacy boottime seconds can shift and are read every time."""
    ps = FakePs()
    monkeypatch.setattr(procs, "_read", ps)
    clock = StepClock(0.0)
    monkeypatch.setattr(procs, "time", clock)
    assert [procs.boot_id() for _ in range(5)] == [BOOT] * 5
    assert sum(argv[0].endswith("sysctl") for argv in ps.calls) == 1
    clock.now = procs.BOOT_ID_TTL_S + 0.01
    assert procs.boot_id() == BOOT
    assert sum(argv[0].endswith("sysctl") for argv in ps.calls) == 2

    procs.forget_boot_id()
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
    """The attempt is due and the shared table has expired."""
    core._inspect_next[ATTEMPT] = 0
    core._table = (core._table[0], 0.0)


def test_running_attempt_inspection_is_paced_and_never_dumps_environments(daemon, monkeypatch):
    """C-5.11 with C-5.12: twenty control ticks read one process table, ask
    nothing about the guardian singly, run no `ps -axEww`, and record its group.

    Before C-5.11 this spent sixty subprocesses on liveness and a full census;
    with C-5.11 it spent ten; the shared table needs two.
    """
    ps = FakePs()
    monkeypatch.setattr(procs, "_read", ps)
    asked = count_liveness(monkeypatch)

    for _ in range(20):
        daemon._process_attempt(ATTEMPT)

    # The same table shows both the recorded leader and its owned members.
    assert asked == []
    assert not ps.environment_dumps()
    assert ps.calls == [TABLE, UUID_READ]
    assert set(owned(daemon)) == {str(GUARDIAN), str(CHILD)}
    assert owned(daemon)[str(CHILD)] == {"pid": CHILD, "boot_id": BOOT, "proc_start": STARTED}
    assert daemon.store.get_attempt(ATTEMPT)["state"] == "running"

    # The next interval reads one new table; the boot UUID is remembered.
    ps.calls.clear()
    expire(daemon)
    daemon._process_attempt(ATTEMPT)
    assert asked == []
    assert ps.calls == [TABLE]


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


def test_a_member_recorded_under_another_boot_identity_is_recorded_afresh(daemon, monkeypatch):
    """C-5.3, C-5.4: a member recorded with legacy boot seconds (from before the boot
    UUID could be read) is re-identified while it is still in the group, as the
    full census refreshed it; kept, a later clock correction would leave it an
    identity that no signal may trust once it escapes."""
    ps = FakePs()
    monkeypatch.setattr(procs, "_read", ps)
    legacy = {"pid": CHILD, "boot_id": "1790255587", "proc_start": STARTED}
    with daemon.store.transaction("test.legacy") as tx:
        tx.execute("UPDATE attempts SET evidence_json=? WHERE attempt_id=?",
                   (json.dumps({"owned_identities": {str(CHILD): legacy}}), ATTEMPT))
    daemon._process_attempt(ATTEMPT)
    assert owned(daemon)[str(CHILD)] == {"pid": CHILD, "boot_id": BOOT, "proc_start": STARTED}

    # The next table records that same identity without any per-pid reads.
    ps.calls.clear()
    expire(daemon)
    daemon._process_attempt(ATTEMPT)
    assert ps.calls == [TABLE]


def test_pacing_cleanup_survives_a_worker_adding_an_entry_mid_walk(daemon):
    """C-5.11: workers add pacing entries while the control loop prunes them; the
    prune walks a copy, so an insertion mid-walk cannot raise and cost a tick."""
    daemon._inspect_next.update({"gone/a1": 1.0, "also-gone/a1": 1.0})

    class Live(set):
        def __contains__(self, aid):                # a worker's first deadline lands mid-walk
            daemon._inspect_next.setdefault("new/a1", 2.0)
            return super().__contains__(aid)

    daemon._forget_paced(Live({"new/a1"}))
    assert daemon._inspect_next == {"new/a1": 2.0}


def test_a_failing_inspection_is_retried_not_skipped(daemon, monkeypatch):
    """C-5.10 with C-5.11: a paced pass that raised leaves no pacing deadline, so the
    retry C-5.10 schedules repeats the inspection (and fails again, keeping its
    backoff) instead of returning at the gate and reading as recovery."""
    monkeypatch.setattr(procs, "_read", FakePs())
    failures = []
    def failing_record(a, table):
        failures.append(a["attempt_id"])
        raise RuntimeError("database or disk is full")
    monkeypatch.setattr(daemon, "_record_owned", failing_record)
    for n in range(1, 4):
        with pytest.raises(RuntimeError):
            daemon._process_attempt(ATTEMPT)
        assert len(failures) == n
        assert ATTEMPT not in daemon._inspect_next


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
    """C-5.11: an attempt normally becomes terminal inside its own worker pass and is
    never offered again, so the control loop drops its pacing entries against
    the live set it reads each tick."""
    monkeypatch.setattr(procs, "_read", FakePs())
    daemon._process_attempt(ATTEMPT)
    assert ATTEMPT in daemon._inspect_next
    with daemon.store.transaction("test.finished") as tx:
        tx.execute("UPDATE attempts SET state='succeeded' WHERE attempt_id=?", (ATTEMPT,))
    live = {a["attempt_id"] for a in daemon.store.query(daemon_module.LIVE_ATTEMPTS)}
    daemon._forget_paced(live)
    assert ATTEMPT not in daemon._inspect_next


class StepClock:
    """`time` inside `subfleet.daemon` only: each `monotonic()` call moves it on by
    `step`, so a count of looks depends on the loop, not on how the OS schedules."""

    def __init__(self, step):
        self.now, self.step = 0.0, step

    def monotonic(self):
        self.now += self.step
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


# --- probes -------------------------------------------------------------------

def test_a_running_probe_is_inspected_on_the_same_budget(daemon, monkeypatch, tmp_path):
    """C-5.11: a probe's wait loop reads its receipt every 50 ms but asks `ps` about
    its guardian once per liveness interval and records its group without an
    environment dump. Before the fix it asked every pass and took the full
    census every 0.5 s."""
    from subfleet.guardian import atomic_publish
    ps = FakePs()
    monkeypatch.setattr(procs, "_read", ps)
    asked = count_liveness(monkeypatch)
    monkeypatch.setattr(daemon, "_contain_probe", lambda record: True)
    # `time` inside subfleet.daemon stands still, so the next question is never
    # due however the OS schedules this test; only real waits pass.
    monkeypatch.setattr(daemon_module, "time", StepClock(0.0))
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
    try:
        deadline = time.monotonic() + 10
        while not record["owned_identities"]:       # the first pass has asked and recorded
            assert time.monotonic() < deadline
            time.sleep(.01)
        time.sleep(.5)                                # about ten more passes, none of them due
        atomic_publish(directory / "exit.json", json.dumps({"rc": 0, "child_pid": CHILD}).encode())
        waiter.join(timeout=10)
        assert not waiter.is_alive()
    finally:
        if waiter.is_alive():
            daemon.stopping.set()
            waiter.join(timeout=10)
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
    """C-5.11: a waiter woken without a committed change reads nothing. Before the fix
    every wake-up re-read every watched job; 200 wake-ups were 200 reads."""
    monkeypatch.setattr(daemon_module, "WAIT_RECHECK_S", 3600)   # only commits may cause a read here
    reads, first_read = [], threading.Event()
    get_job = daemon.store.get_job

    def spy(job_id):
        row = get_job(job_id)          # signalled once the read has happened, not before
        reads.append(row["state"])
        first_read.set()
        return row

    monkeypatch.setattr(daemon.store, "get_job", spy)
    result = {}
    waiter = threading.Thread(target=lambda: result.update(
        daemon.wait(protocol.WaitArgs(job_ids=[JOB], deadline_s=60))))
    waiter.start()
    try:
        assert first_read.wait(10)
        for _ in range(200):
            daemon._notify()
            time.sleep(.001)
        assert reads == ["running"]

        with daemon.store.transaction("test.finished", job_id=JOB) as tx:
            tx.execute("UPDATE jobs SET state='succeeded',rc=0 WHERE job_id=?", (JOB,))
        daemon._notify()
        waiter.join(timeout=10)
        assert not waiter.is_alive()
    finally:
        if waiter.is_alive():
            daemon.stopping.set()
            daemon._notify()
            waiter.join(timeout=10)
    assert result["timeout"] is False and result["jobs"][0]["state"] == "succeeded"
    assert reads == ["running", "succeeded"]


def test_wait_rechecks_on_its_own_clock_without_a_commit(daemon, monkeypatch):
    """C-5.11: the generation is a hint; a waiter still looks again on its own clock."""
    monkeypatch.setattr(daemon_module, "time", StepClock(.02))
    monkeypatch.setattr(daemon_module, "WAIT_RECHECK_S", .05)
    reads = []
    get_job = daemon.store.get_job
    monkeypatch.setattr(daemon.store, "get_job", lambda job_id: reads.append(job_id) or get_job(job_id))
    assert daemon.wait(protocol.WaitArgs(job_ids=[JOB], deadline_s=.6)) == {"timeout": True}
    assert len(reads) >= 2              # no commit ever happened, so every read after the first is the clock's


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
