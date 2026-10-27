"""Round 4 (D-KILL3 review r4 of #240): TMPDIR release, one census fewer, and the placements r4 found unpinned.

P3-2: a cwd observer is in the worktree, not the scratch. The attempt's TMPDIR is
released from `containment_empty`, so an observer-only census removes it at the
verdict and no sweep takes a census for it again; the attempt's own attributed
processes still hold it, and an unverifiable census is still asked again (C-12.10).

I7 (Hypothesis, bounded): a TMPDIR is held only while the attempt's own attributed
processes live or its census is unverifiable; a cwd observer never holds it.

r4's notes: start grace's census is the `_unlaunched` cleanup's too (one census fewer);
the cleanup runs after the ending transaction commits and outside the store's write
lock; the export's conversion survives a crash only as one transaction; and the loss
path's cwd clock paces its census even when the shared process table expires early.
"""

import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace

from hypothesis import example, given, settings, strategies as st
import pytest

from subfleet import daemon as daemon_module, folders, procs
from subfleet.attempt_tmp import AttemptTemp
from subfleet.daemon import Daemon
from subfleet.procs import ProcessTable
from tests.unit.test_census_round3 import CWD, KEPT, KEY, LIFETIME, leases, writer
from tests.unit.test_daemon_settle import (
    ATTEMPT, JOB, attempt, daemon, publish_receipt, with_launch,
)  # noqa: F401

OBSERVED_ONLY = procs.Containment(cwd_pids=frozenset({4500}))


def with_scratch(core, tmp_path) -> Path:
    """Give the fixture's core the TMPDIR machinery `__init__` builds, and its attempt
    one allocation with a file in it (C-12.10)."""
    temp = tmp_path / "temp"
    temp.mkdir()
    core._attempt_temp = AttemptTemp(core.root, root=temp)
    core._tmpdir_held, core._tmpdir_busy = {}, set()
    core._tmpdir_guard, core._tmpdir_sweeping = threading.Lock(), threading.Lock()
    record = core._attempt_temp.create(ATTEMPT)
    evidence = json.loads(attempt(core)["evidence_json"] or "{}")
    evidence["tmpdir"] = record
    core.store.update_attempt(ATTEMPT, evidence_json=json.dumps(evidence))
    folder = Path(record["path"])
    (folder / "left-behind").write_text("dummy scratch")
    return folder


def scratch_record(core) -> dict:
    return json.loads(attempt(core)["evidence_json"])["tmpdir"]


def counted(core, census) -> list[float]:
    calls: list[float] = []
    core._contain = lambda a: calls.append(1) or census
    return calls


def committed_state(core) -> str:
    """The attempt's state as another connection sees it: committed rows only."""
    with contextlib.closing(sqlite3.connect(f"file:{core.root / 'state.sqlite3'}?mode=ro", uri=True)) as db:
        return db.execute("SELECT state FROM attempts WHERE attempt_id=?", (ATTEMPT,)).fetchone()[0]


# --- P3-2: an observer never holds the TMPDIR --------------------------------------------------------


def test_r4_an_observer_only_census_removes_the_tmpdir_at_the_verdict(daemon, monkeypatch, tmp_path):
    """r4's executed input at the unit: kept at the verdict, pinning retention, and one
    census per sweep until the shell left. The worktree stays held, as a reader row."""
    writer(daemon)
    folder = with_scratch(daemon, tmp_path)
    with_launch(daemon, monkeypatch)
    publish_receipt(daemon, rc=1)
    daemon.store.update_attempt(ATTEMPT, state="finalizing", rc=1)
    censuses = counted(daemon, OBSERVED_ONLY)
    monkeypatch.setattr(daemon_module, "CWD_SETTLE_S", 0)
    daemon._finalize(attempt(daemon))
    assert daemon.store.get_job(JOB)["state"] == "failed"
    assert not folder.exists() and scratch_record(daemon)["outcome"] == "removed"
    assert ATTEMPT not in daemon._tmpdir_held
    assert leases(daemon) == {KEPT: folders.cwd_holder(JOB)}
    taken = len(censuses)
    for _ in range(3):
        daemon._sweep_attempt_tmpdirs()
    assert len(censuses) == taken, "a released TMPDIR is never asked about again"


def test_r4_the_attempts_own_live_process_still_holds_its_tmpdir(daemon, tmp_path):
    """Its census found an attributed member: kept, and no census again until the state changes."""
    folder = with_scratch(daemon, tmp_path)
    daemon.store.update_attempt(ATTEMPT, state="failed")
    censuses = counted(daemon, procs.Containment(marker_pids=frozenset({12345}), cwd_pids=frozenset({4500})))
    daemon._cleanup_attempt_tmpdir(attempt(daemon))
    assert folder.is_dir() and daemon._tmpdir_held == {ATTEMPT: "failed"}
    for _ in range(3):
        daemon._sweep_attempt_tmpdirs()
    assert censuses == [1] and folder.is_dir() and "cleared_at" not in scratch_record(daemon)


def test_r4_an_unverifiable_census_holds_the_tmpdir_and_is_asked_again(daemon, tmp_path):
    folder = with_scratch(daemon, tmp_path)
    daemon.store.update_attempt(ATTEMPT, state="failed")
    censuses = counted(daemon, procs.Containment(unverifiable=True, cwd_pids=frozenset({4500})))
    daemon._cleanup_attempt_tmpdir(attempt(daemon))
    for _ in range(3):
        daemon._sweep_attempt_tmpdirs()
    assert len(censuses) == 4 and folder.is_dir() and ATTEMPT not in daemon._tmpdir_held


PIDS = st.frozensets(st.integers(min_value=100, max_value=110), max_size=3)


@settings(max_examples=40, derandomize=True, database=None, deadline=None)
@given(group=PIDS, descendants=PIDS, markers=PIDS, responsible=PIDS, cwd=PIDS, unverifiable=st.booleans())
# r4's census, whatever is drawn (the derandomized draws at 4cddd7925 never made it):
@example(group=frozenset(), descendants=frozenset(), markers=frozenset(), responsible=frozenset(),
         cwd=frozenset({4500}), unverifiable=False)
def test_r4_i7_only_the_attempts_own_processes_or_doubt_hold_its_tmpdir(group, descendants, markers,
                                                                        responsible, cwd, unverifiable):
    census = procs.Containment(group_pids=group, descendant_pids=descendants, marker_pids=markers,
                               responsible_pids=responsible, cwd_pids=cwd, unverifiable=unverifiable)
    with tempfile.TemporaryDirectory(prefix="r4-i7-") as directory:
        temp = Path(directory) / "temp"
        temp.mkdir()
        core = object.__new__(Daemon)
        core._tmpdir_held = {}
        core._attempt_temp = AttemptTemp(Path(directory), root=temp)
        record = core._attempt_temp.create(ATTEMPT)
        subject = {"attempt_id": ATTEMPT, "state": "failed"}
        fields = core._release_tmpdir(ATTEMPT, subject, record, census,
                                      lambda: pytest.fail("a census was given"))
        held = bool(census.live_pids) or unverifiable
        assert (fields == {}) == held, (census.to_dict(), fields)
        assert Path(record["path"]).is_dir() == held
        assert (ATTEMPT in core._tmpdir_held) == bool(census.live_pids)
        if not held:
            assert fields["outcome"] == "removed" and fields["cleared_at"]


# --- r4's note: start grace's census is the cleanup's too ---------------------------------------------


def test_r4_start_grace_hands_its_census_to_the_tmpdir_cleanup(daemon, monkeypatch, tmp_path):
    """One census for the whole ending: it decides the attempt ended unlaunched, and the
    TMPDIR it frees (two at 4cddd7925: the cleanup took a fresh one)."""
    folder = with_scratch(daemon, tmp_path)
    monkeypatch.setattr(procs, "liveness", lambda *a: "dead")
    daemon.store.update_attempt(ATTEMPT, state="starting")
    daemon.start_grace_s = 0
    daemon._starting_deadlines[ATTEMPT] = 0.0
    censuses = counted(daemon, procs.Containment())
    daemon._process_attempt(ATTEMPT)
    assert attempt(daemon)["state"] == "failed" and attempt(daemon)["outcome_detail"] == "starting-no-receipt"
    assert censuses == [1]
    assert not folder.exists() and scratch_record(daemon)["outcome"] == "removed"


# --- r4's note: where the `_unlaunched` cleanup runs --------------------------------------------------


def test_r4_the_unlaunched_cleanup_runs_after_its_ending_commits_and_outside_the_lock(daemon, tmp_path):
    """r4's placement probe: a census taken inside the transaction would hold the store's
    write lock for a `ps`; one taken before it sees a live attempt and frees nothing."""
    folder = with_scratch(daemon, tmp_path)
    daemon.store.update_attempt(ATTEMPT, state="reserved")
    seen = []

    def census(a):
        seen.append((daemon.store._depth, committed_state(daemon)))
        return procs.Containment()
    daemon._contain = census
    removals = []
    remove = daemon._attempt_temp.remove
    daemon._attempt_temp.remove = lambda record, subject, *, verified_empty: (
        removals.append((daemon.store._depth, verified_empty)) or remove(record, subject, verified_empty=verified_empty))
    daemon._process_attempt(ATTEMPT)                         # reserved, not pending: `reserved-no-launch`
    assert attempt(daemon)["state"] == "failed"
    assert seen == [(0, "failed")], seen
    assert (0, True) in removals and all(depth == 0 for depth, _ in removals), removals
    assert not folder.exists()


# --- r4's crash probe: the export converts in its one transaction -------------------------------------


@pytest.mark.parametrize("where", ["after-conversion", "between-insert-and-delete"])
def test_r4_a_crash_in_the_export_rolls_it_back_whole_and_the_retry_converts(daemon, where):
    """Either crash leaves the exact earlier state, the export is offered again, and the
    retry converts: the folder is never without a row, and no lease is half released."""
    writer(daemon)
    daemon.store.update_job(JOB, state="succeeded", accepted_attempt_id=ATTEMPT)
    daemon.store.update_attempt(ATTEMPT, state="succeeded")
    daemon.store.release_leases(ATTEMPT)
    daemon._worktree_observers = lambda job: frozenset({4500})
    before = leases(daemon)
    assert before == {KEY: JOB, LIFETIME: JOB}

    def crashing(tx, job_id, attempt_id=None):
        if where == "after-conversion":
            Daemon._keep_for_cwd(daemon, tx, job_id, attempt_id)
        else:
            tx.execute("INSERT OR REPLACE INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                       (KEPT, folders.cwd_holder(job_id), daemon_module.utcnow()))
        raise RuntimeError("dummy crash in the export")
    daemon._keep_for_cwd = crashing
    with pytest.raises(RuntimeError):
        Daemon._export_locked(daemon, JOB)
    assert leases(daemon) == before
    assert daemon._pending_exports() == [JOB]
    del daemon._keep_for_cwd
    Daemon._export_locked(daemon, JOB)
    assert leases(daemon) == {KEPT: folders.cwd_holder(JOB)}


# --- r4's note: the pacing gate --------------------------------------------------------------------------


@pytest.mark.parametrize("scan_s", [0, .254], ids=["instant", "review-cost"])
def test_r4_the_cwd_clock_paces_the_loss_when_the_shared_table_expires_early(daemon, monkeypatch, scan_s):
    """Attempts share one process table (C-5.12): one given a table another attempt read
    just before it fell due is inspected again when that table expires, well inside
    `inspect_interval_s`. The loss's census still waits for the attempt's own cwd
    clock (C-5.9). Without that gate, r3's one-attempt test passed (r4's note)."""
    now, censuses = [0.0], []
    monkeypatch.setattr(daemon_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    daemon._process_table = lambda due: (ProcessTable({}, "boot"), due + .1)

    def contain(a):
        censuses.append(now[0])
        now[0] += scan_s
        return CWD
    daemon._contain = contain
    daemon.store.update_job(JOB, max_attempts=1)
    monkeypatch.setattr(procs, "liveness", lambda *a: "dead")
    with_launch(daemon, monkeypatch)
    while attempt(daemon)["state"] == "running" and now[0] < 40:
        daemon._process_attempt(ATTEMPT)
        now[0] += .05
    assert attempt(daemon)["state"] == "lost" and attempt(daemon)["killed_by"] is None
    window = [t for t in censuses if t < censuses[0] + 30]
    assert len(window) <= 30 / daemon.inspect_interval_s + 1, len(window)
    assert len(daemon.store.query("SELECT 1 FROM events WHERE kind='attempt.lost_end'")) == 1
