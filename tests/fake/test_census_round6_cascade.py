"""Round 6 (D-KILL3 review r5 of #240): no cwd scan inside an open store transaction.

P3-1: `_request_cancel` scans the worktree of a job it may end from the queue before its own
transaction (C-5.9). The wall's cascade (`_cascade_final_wall`) calls it from inside the
transaction that settles the wall-cancelled parent's attempt, at quarantine resolution and
at finalization, so the store's write lock and `BEGIN IMMEDIATE` were held across a scan of
every process's cwd (0.65 to 3.70 s on the review's host). A cancel requested inside a
transaction now takes no scan: each job keeps its row with no pid line, as a job not scanned
does, and the cwd worker settles it. Nor is a finished job scanned, one whose export is
pending included: the cancel's transaction leaves it alone.

P3-2: a mutant that scans the root job only survived every round-4 test. The family kill of
a parent whose child is queued holding its key is the regression; I6 draws it too.

The jobs run the real submit, admission, start grace, quarantine, finalization, export and
kill; only the provider and the kernel's reads are fixtures (`observed`).
"""

import threading

import pytest

from subfleet import folders, procs, protocol
from subfleet.daemon import Daemon
from tests.fake.test_census_round3_leases import kept_key, leases, observed, tick  # noqa: F401
from tests.fake.test_census_round4_endings import RETAINED, end_by_parent, notices, parent_attempt
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401
from tests.fake.test_workspace_contract import repository

def family(daemon, harness) -> tuple[str, str, str]:
    """r5's input: a read-only parent, and its writable child (`independent=0`) that start
    grace ended unlaunched with only the observer and an attempt left, so it is queued again
    holding `worktree:<wt>` (r4's input). Returns the parent, the child and its worktree."""
    repository(daemon, harness)
    parent = daemon.dispatch("submit", harness.submit_args())["job_id"]
    child, attempt, _ = reserve(daemon, harness, sandbox="workspace-write", max_attempts=2, parent_job_id=parent)
    parent_attempt(daemon, parent)
    daemon.store.update_attempt(attempt["attempt_id"], state="starting")
    current = daemon.store.get_attempt(attempt["attempt_id"])
    daemon._unlaunched(current, "starting-no-receipt", census=daemon._contain(current))
    job = daemon.store.get_job(child)
    assert job["state"] == "queued" and not job["independent"], job
    assert leases(daemon).get(folders.exclusive_key(folders.canonical(job["worktree"]))) == child
    return parent, child, job["worktree"]


def spied(monkeypatch, daemon, cwd) -> list[tuple[str, int, bool]]:
    """Each cwd scan from now on: its worktree, the store's transaction depth, and whether
    this thread held the store's write lock."""
    scans = []

    def scan(worktree):
        scans.append((folders.canonical(worktree), daemon.store._depth, daemon.store._holds_writer()))
        return cwd[0]
    monkeypatch.setattr(procs, "worktree_cwd_pids", scan)
    return scans


def held_by(daemon, job_id: str) -> dict[str, str]:
    """The job's rows: its own, its attempts' and its kept cwd rows (the parent's are not)."""
    holders = {a["attempt_id"] for a in daemon.store.list_attempts(job_id)} | {job_id, folders.cwd_holder(job_id)}
    return {key: holder for key, holder in leases(daemon).items() if holder in holders}


def the_unscanned_child_settles(daemon, child, worktree, cwd) -> None:
    """The child ends cancelled with its worktree kept as one reader row and no pid line, as a
    job not scanned does (C-5.9); a worker tick keeps the row while the observer stays and
    releases it once the observer leaves."""
    job = daemon.store.get_job(child)
    assert (job["state"], job["rc"]) == ("cancelled", 130)
    text = notices(daemon, child)
    assert "cancel requested by max_wall_s from parent" in text and "worktree retained" not in text, text
    row = {kept_key(worktree, child): folders.cwd_holder(child)}
    assert held_by(daemon, child) == row
    tick(daemon, child)
    assert held_by(daemon, child) == row, "the observer is still there"
    cwd[0] = frozenset()
    tick(daemon, child)
    assert held_by(daemon, child) == {}


def test_r6_a_wall_cascade_from_quarantine_resolution_scans_nothing_under_the_write_lock(observed, monkeypatch):
    """r5's executed input (hold 2): the read-only parent's attempt is wall-cancelled and
    quarantined, and `kill --confirm-dead` of the parent cascades to the child inside the
    resolution's transaction. At a2380e42f the child's scan ran there, at `store._depth == 1`."""
    daemon, harness, cwd = observed
    parent, child, worktree = family(daemon, harness)
    scans = spied(monkeypatch, daemon, cwd)
    # The operator's request, through `kill`; its worker runs here, on this thread.
    monkeypatch.setattr(daemon, "_schedule", lambda key, fn, *args, **kwargs: fn(*args))
    answers = []
    end_by_parent(daemon, parent, "cascade-quarantine", resolve=lambda: answers.append(daemon.kill(
        protocol.KillArgs(parent, confirm_dead=True, operator_note="dummy"))))
    assert answers == [{"job_id": parent, "status": "resolution requested"}]
    assert [scan for scan in scans if scan[1] or scan[2]] == [], ("a scan under the write lock", scans)
    assert scans == [], "nothing else of the family is scanned"
    the_unscanned_child_settles(daemon, child, worktree, cwd)


def test_r6_a_wall_cascade_from_finalization_scans_nothing_under_the_write_lock(observed, monkeypatch):
    """The other caller (`_finalize`, inside `attempt.accepted`): the wall-cancelled parent's
    attempt finalizes from the wall's receipt, interrupted, and cascades to the child."""
    daemon, harness, cwd = observed
    parent, child, worktree = family(daemon, harness)
    scans = spied(monkeypatch, daemon, cwd)
    end_by_parent(daemon, parent, "cascade-finalize")
    assert [scan for scan in scans if scan[1] or scan[2]] == [], ("a scan under the write lock", scans)
    assert scans == [], "the read-only parent owns no worktree, and the child is not scanned"
    the_unscanned_child_settles(daemon, child, worktree, cwd)


def test_r6_a_finished_family_member_with_its_export_pending_is_not_scanned(observed, monkeypatch):
    """r5, from code: the scan's query had no job-state filter. A finished child whose export
    is still pending holds `worktree:<wt>` with no active attempt, so its parent's kill scanned
    it, and the cancel's transaction then left it alone. It is not scanned now; its export
    takes its own scan, as before, and converts the key."""
    daemon, harness, cwd = observed
    repository(daemon, harness)
    parent = daemon.dispatch("submit", harness.submit_args())["job_id"]
    child, attempt, adir = reserve(daemon, harness, sandbox="workspace-write", parent_job_id=parent)
    parent_attempt(daemon, parent)
    monkeypatch.setattr(daemon, "_export", lambda job_id: None)        # the export is still to run
    daemon._finalize(receipt_fixture(daemon, daemon.store.get_attempt(attempt["attempt_id"]), adir))
    job = daemon.store.get_job(child)
    worktree = job["worktree"]
    assert job["state"] == "succeeded" and child in daemon._pending_exports()
    assert held_by(daemon, child).get(folders.exclusive_key(folders.canonical(worktree))) == child
    scans = spied(monkeypatch, daemon, cwd)
    assert daemon.kill(protocol.KillArgs(parent))["status"] == "cancel requested"
    assert daemon.store.get_job(parent)["cancel_requested_at"]
    assert scans == [], ("a finished job is not scanned", scans)
    assert daemon.store.get_job(child)["state"] == "succeeded" and not daemon.store.get_job(child)["cancel_requested_at"]
    Daemon._export(daemon, child)
    assert scans == [(folders.canonical(worktree), 0, False)], "the export's own scan"
    assert held_by(daemon, child) == {kept_key(worktree, child): folders.cwd_holder(child)}


@pytest.mark.parametrize("scan", ["observer", "gone"])
def test_r6_a_family_kill_scans_the_queued_child_that_holds_its_key(observed, monkeypatch, scan):
    """r5's family-kill probe, the only test that killed the root-only scan: a client kill of
    the parent ends its child from the queue. The child is scanned before the transaction
    (C-5.9): with the observer it keeps one reader row and the notice names the pid; with
    nobody there its key goes."""
    daemon, harness, cwd = observed
    parent, child, worktree = family(daemon, harness)
    if scan == "gone":
        cwd[0] = frozenset()
    scans = spied(monkeypatch, daemon, cwd)
    end_by_parent(daemon, parent, "family-cancel")
    assert daemon.store.get_job(child)["state"] == "cancelled"
    assert daemon.store.get_job(parent)["cancel_requested_at"]
    assert scans == [(folders.canonical(worktree), 0, False)], scans
    if scan == "gone":
        assert held_by(daemon, child) == {} and "worktree retained" not in notices(daemon, child)
        return
    assert held_by(daemon, child) == {kept_key(worktree, child): folders.cwd_holder(child)}
    assert RETAINED in notices(daemon, child), notices(daemon, child)


def test_r6_another_threads_open_transaction_does_not_skip_this_threads_scan(observed, monkeypatch):
    """The check is this thread's hold on the store's write lock (`Store._holds_writer`), not
    `store._depth`, which counts any thread's transaction: a client kill that meets a worker's
    open transaction still scans before its own, and names the observer."""
    daemon, harness, cwd = observed
    _, child, worktree = family(daemon, harness)
    opened, scanned = threading.Event(), threading.Event()
    scans = []

    def scan(folder):
        scans.append((daemon.store._depth, daemon.store._holds_writer()))
        scanned.set()
        return cwd[0]
    monkeypatch.setattr(procs, "worktree_cwd_pids", scan)

    def worker():
        with daemon.store.transaction("test.worker_holds_the_store"):
            opened.set()
            scanned.wait(10)
    thread = threading.Thread(target=worker, name="test-worker")
    thread.start()
    try:
        assert opened.wait(10)
        daemon.kill(protocol.KillArgs(child))
    finally:
        scanned.set()
        thread.join(10)
    assert scans == [(1, False)], "scanned while the worker's transaction was open, outside it"
    assert daemon.store.get_job(child)["state"] == "cancelled"
    assert held_by(daemon, child) == {kept_key(worktree, child): folders.cwd_holder(child)}
    assert RETAINED in notices(daemon, child)


def test_r6_the_spied_scan_is_the_one_the_daemon_takes():
    """The spy replaces `procs.worktree_cwd_pids`, which `_worktree_observers` reads at call
    time; a daemon that imported it by name would bypass every spy above."""
    import inspect
    assert "procs.worktree_cwd_pids(" in inspect.getsource(Daemon._worktree_observers)
