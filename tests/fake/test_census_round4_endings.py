"""Round 4 (D-KILL3 review r4 of #240): every ending keeps the worktree as a reader row.

P3-1: start grace that ends an attempt unlaunched, finding only a cwd observer, with
attempts left, queues the job again holding its `worktree:` key (C-5.9). Ended from
the queue, by `kill`, `_fail_queued`, or a cancel before its next launch, the job
dropped every lease, took no cwd scan, wrote no reader row and left the `worktree
retained` line out of its notice. Each such ending now scans the worktree before its
transaction, as the export does, and converts the key as the other endings do.

I6, over every ending (Hypothesis, bounded): a job that ends while it holds a
`worktree:` key and has a live observer leaves exactly one `worktree-read:<folder>:<job
id>` row, held by `cwd:<job id>`, and no writer key; with no observer it leaves none.

The source jobs run the real submit, admission, finalization, export and kill; only
the provider and the kernel's reads are fixtures (`observed`).
"""

import contextlib
import json
import os
from pathlib import Path

from hypothesis import HealthCheck, example, given, settings, strategies as st
import pytest

from subfleet import daemon as daemon_module, folders, procs, protocol, retention
from subfleet.adapters.registry import register
from subfleet.daemon import Daemon, LAUNCH_CHECK_UNFINISHED
from subfleet.procs import Containment
from tests.fake.conftest import Harness
from tests.fake.test_census_round3_leases import kept_key, leases, observed  # noqa: F401
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401
from tests.fake.test_workspace_contract import repository
from tests.fake_adapter import FakeAdapter

OBSERVER = 4500
RETAINED = f"worktree retained: pid {OBSERVER}: cwd in worktree"


def notices(daemon, job_id: str) -> str:
    return "\n".join(row["text"] for row in daemon.store.query("SELECT text FROM notices WHERE job_id=?", (job_id,)))


def queued_holding_its_key(daemon, harness):
    """r4's input: a1 ends unlaunched at start grace with only the observer and an attempt
    left (`max_attempts=2`), so the job is queued again holding `worktree:<wt>`."""
    repository(daemon, harness)
    job_id, attempt, _ = reserve(daemon, harness, sandbox="workspace-write", max_attempts=2)
    daemon.store.update_attempt(attempt["attempt_id"], state="starting")
    daemon._unlaunched(daemon.store.get_attempt(attempt["attempt_id"]), "starting-no-receipt",
                       census=Containment(cwd_pids=frozenset({OBSERVER})))
    job = daemon.store.get_job(job_id)
    assert job["state"] == "queued"
    worktree = job["worktree"]
    assert leases(daemon) == {folders.exclusive_key(folders.canonical(worktree)): job_id}
    return job_id, worktree


def end_by_kill(daemon, job_id):
    daemon.kill(protocol.KillArgs(job_id))


def end_by_fail_queued(daemon, job_id):
    daemon._fail_queued(daemon.store.get_job(job_id), "dummy failure while preparing the retry")


def end_cancelled_before_launch(daemon, job_id):
    """Re-admitted as a2, then killed: `_launch` finds the cancel and ends a2 unlaunched."""
    daemon._admit()
    a2 = daemon.store.list_attempts(job_id)[-1]
    assert a2["seq"] == 2 and a2["state"] == "reserved", a2
    daemon.kill(protocol.KillArgs(job_id))
    assert daemon.store.get_job(job_id)["state"] not in daemon_module.TERMINAL, "a2 is still active"
    daemon._unlaunched(daemon.store.get_attempt(a2["attempt_id"]), "cancelled-before-launch")


QUEUE_ENDINGS = {"kill": (end_by_kill, "cancelled"), "fail-queued": (end_by_fail_queued, "failed"),
                 "cancelled-before-launch": (end_cancelled_before_launch, "cancelled")}


@pytest.mark.parametrize("ending", list(QUEUE_ENDINGS))
@pytest.mark.parametrize("scan", ["observer", "unreadable", "gone"])
def test_r4_a_job_ending_from_the_queue_keeps_its_worktree_as_one_reader_row(observed, monkeypatch, ending, scan):
    """r4's executed input, each ending: no lease was left, and the notice read only
    "cancelled while waiting to retry / attempt a1: unknown, rc=-: starting-no-receipt".
    An unreadable scan keeps the row too, as the export's does; it names no pid."""
    daemon, harness, cwd = observed
    job_id, worktree = queued_holding_its_key(daemon, harness)
    if scan == "unreadable":
        def unreadable(folder):
            raise procs.InspectionError("dummy cwd read unavailable")
        monkeypatch.setattr(procs, "worktree_cwd_pids", unreadable)
    elif scan == "gone":
        cwd[0] = frozenset()
    end, state = QUEUE_ENDINGS[ending]
    end(daemon, job_id)
    assert daemon.store.get_job(job_id)["state"] == state
    if scan == "gone":
        assert leases(daemon) == {}
        assert "worktree retained" not in notices(daemon, job_id)
        return
    assert leases(daemon) == {kept_key(worktree, job_id): folders.cwd_holder(job_id)}
    assert daemon._pending_worktree_holds() == [job_id]
    assert (RETAINED in notices(daemon, job_id)) == (scan == "observer"), notices(daemon, job_id)


def test_r4_a_kill_of_a_queued_job_that_holds_no_key_takes_no_scan(observed, monkeypatch):
    """Only a job holding a `worktree:` key is scanned: a plain queued job's cancel reads no cwd."""
    daemon, harness, cwd = observed
    repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    scans = []
    monkeypatch.setattr(procs, "worktree_cwd_pids", lambda folder: scans.append(folder) or cwd[0])
    daemon.kill(protocol.KillArgs(job_id))
    assert daemon.store.get_job(job_id)["state"] == "cancelled"
    assert scans == [] and leases(daemon) == {}


#: `_unlaunched`'s callers that pass no census (`_launch`, admission's launch checks).
UNCENSUSED = ["cancelled-before-launch", f"{LAUNCH_CHECK_UNFINISHED} dummy", "needs a shell: dummy",
              "guardian-identity-unavailable: dummy", "reserved-no-launch"]


@pytest.mark.parametrize("detail", UNCENSUSED, ids=lambda detail: detail.split(":")[0].split()[0])
@pytest.mark.parametrize("left", [False, True], ids=["ends", "queued-again"])
def test_r4_an_unlaunched_attempt_without_a_census_scans_the_worktree(observed, detail, left):
    """Ending, it converts the key with the notice line; queued again, the job keeps its
    own key, as after start grace (C-5.9)."""
    daemon, harness, cwd = observed
    repository(daemon, harness)
    job_id, attempt, _ = reserve(daemon, harness, sandbox="workspace-write", max_attempts=2 if left else 1)
    worktree = daemon.store.get_job(job_id)["worktree"]
    daemon._unlaunched(daemon.store.get_attempt(attempt["attempt_id"]), detail)
    if left:
        assert daemon.store.get_job(job_id)["state"] == "queued"
        assert leases(daemon) == {folders.exclusive_key(folders.canonical(worktree)): job_id}
        return
    assert daemon.store.get_job(job_id)["state"] == "failed"
    assert leases(daemon) == {kept_key(worktree, job_id): folders.cwd_holder(job_id)}
    assert RETAINED in notices(daemon, job_id)


# --- P3-2: the observer is in the worktree, not the scratch -----------------------------------------


def test_r4_an_observer_never_holds_the_attempt_tmpdir(observed, monkeypatch):
    """r4's executed input, with the real `_contain`: a writable job with an allocated
    TMPDIR finishes `succeeded` with observer {4500}. At 4cddd7925 the TMPDIR was kept
    at the verdict, retention pinned the job for it (`attempt-tmpdir`), and each sweep
    took a census until the observer left. It is removed at the verdict now; the
    worktree stays held as a reader row, which alone pins the job (`turn-folder`)."""
    daemon, harness, cwd = observed
    repository(daemon, harness)
    job_id, attempt, adir = reserve(daemon, harness, sandbox="workspace-write")
    folder = Path(daemon._prepare_attempt_tmpdir(daemon.store.get_attempt(attempt["attempt_id"])))
    (folder / "left-behind").write_text("dummy scratch")
    censuses = []
    real = procs.containment
    monkeypatch.setattr(procs, "containment", lambda *args, **kwargs: censuses.append(1) or real(*args, **kwargs))
    daemon._finalize(receipt_fixture(daemon, daemon.store.get_attempt(attempt["attempt_id"]), adir))
    job = daemon.store.get_job(job_id)
    assert job["state"] == "succeeded" and RETAINED in notices(daemon, job_id)
    assert not folder.exists()
    record = json.loads(daemon.store.get_attempt(attempt["attempt_id"])["evidence_json"])["tmpdir"]
    assert record["outcome"] == "removed" and record["removed_at"] and record["cleared_at"]
    assert leases(daemon) == {kept_key(job["worktree"], job_id): folders.cwd_holder(job_id)}
    taken = len(censuses)
    for _ in range(3):
        daemon._sweep_attempt_tmpdirs()
    assert len(censuses) == taken, "no census for a released TMPDIR"
    daemon.store.acknowledge_notices("fake-session", [row["notice_id"] for row in daemon.store.list_notices()])
    assert retention._pin_reasons(daemon.store, set(), None, only=job_id,
                                  root=daemon.root).get(job_id) == "turn-folder"


# --- I6 over every ending ---------------------------------------------------------------------------

ENDINGS = ["export", "verdict", "unlaunched", "quarantine-resolution", "cancel-queued", "fail-queued",
           "cancelled-before-launch"]
#: Bounded (the ci profile's spirit): each example builds a repository, a worktree and a daemon.
BOUNDED = settings(max_examples=8, deadline=None, derandomize=True, database=None,
                   suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


class Ending:
    """A daemon with one writable job in its worktree, ended one way."""

    def __init__(self, root: Path, patch: pytest.MonkeyPatch):
        self.harness = Harness(root)
        self.observers: frozenset[int] = frozenset({OBSERVER})
        patch.setattr(procs, "worktree_cwd_pids", lambda worktree: self.observers)
        self.daemon = Daemon(self.harness.root)

        def refuse_real_launch(*args):
            raise AssertionError("state-only fixtures must never launch a guardian")
        patch.setattr(self.daemon, "_launch", refuse_real_launch)
        self.daemon._salvage = lambda job, a, **kwargs: ([], None, {})
        repository(self.daemon, self.harness)

    def run(self, ending: str, pids: frozenset[int]) -> str:
        daemon = self.daemon
        queued = ending in ("cancel-queued", "fail-queued", "cancelled-before-launch")
        job_id, attempt, adir = reserve(daemon, self.harness, sandbox="workspace-write",
                                        max_attempts=2 if queued else 1)
        self.worktree = daemon.store.get_job(job_id)["worktree"]
        if queued:
            daemon.store.update_attempt(attempt["attempt_id"], state="starting")
            daemon._unlaunched(daemon.store.get_attempt(attempt["attempt_id"]), "starting-no-receipt",
                               census=daemon._contain(daemon.store.get_attempt(attempt["attempt_id"])))
            assert daemon.store.get_job(job_id)["state"] == "queued"
        self.observers = pids                       # who is in the worktree as it ends
        current = daemon.store.get_attempt(attempt["attempt_id"])
        if ending in ("export", "verdict"):
            daemon._finalize(receipt_fixture(daemon, current, adir, rc=0 if ending == "export" else 1))
        elif ending == "unlaunched":
            daemon.store.update_attempt(attempt["attempt_id"], state="starting")
            current = daemon.store.get_attempt(attempt["attempt_id"])
            daemon._unlaunched(current, "starting-no-receipt", census=daemon._contain(current))
        elif ending == "quarantine-resolution":
            daemon._quarantine(current, Containment(marker_pids=frozenset({9999})), "dummy writers remain")
            daemon._resolve_quarantine(daemon.store.get_attempt(attempt["attempt_id"]),
                                       protocol.KillArgs(job_id, confirm_dead=True, operator_note="dummy"))
        elif ending == "cancel-queued":
            end_by_kill(daemon, job_id)
        elif ending == "fail-queued":
            end_by_fail_queued(daemon, job_id)
        else:
            end_cancelled_before_launch(daemon, job_id)
        assert daemon.store.get_job(job_id)["state"] in daemon_module.TERMINAL, (ending, daemon.store.get_job(job_id))
        return job_id

    def check(self, job_id: str, pids: frozenset[int]) -> None:
        attempts = {a["attempt_id"] for a in self.daemon.store.list_attempts(job_id)}
        held = {row["lease_key"]: row["holder"] for row in self.daemon.store.list_leases()}
        mine = {key: holder for key, holder in held.items()
                if holder in attempts | {job_id, folders.cwd_holder(job_id)}}
        assert not [key for key in mine if key.startswith(folders.EXCLUSIVE)], ("I6: a writer key", mine)
        if pids:
            assert mine == {kept_key(self.worktree, job_id): folders.cwd_holder(job_id)}, ("I6", mine)
            text = notices(self.daemon, job_id)
            for pid in pids:
                assert f"pid {pid}: cwd in worktree" in text, text
        else:
            assert mine == {}, ("no observer, no row", mine)


@contextlib.contextmanager
def ending_world(root: Path):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon_module.procs, "boot_id", lambda: "unit-test-boot")
        patch.setattr(daemon_module.procs, "proc_start", lambda pid: "unit-test-start")
        patch.setattr(daemon_module.procs, "same_process", lambda *args: False)
        patch.setattr(daemon_module.procs, "snapshot", lambda: daemon_module.procs.ProcessTable({
            os.getpid(): (1, os.getpgrp(), "S", "unit-test-start")}, "unit-test-boot"))
        patch.setattr(daemon_module.procs, "identity", lambda pid: daemon_module.procs.snapshot().identity(pid))
        patch.setattr(daemon_module.procs, "group_members", lambda *args: {})
        patch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment())
        patch.setattr(procs, "signal_group", lambda *a, **kw: pytest.fail("cwd cannot authorize a signal"))
        patch.setattr(daemon_module, "CWD_SETTLE_S", 0)
        register("codex", FakeAdapter)
        state = Ending(root, patch)
        try:
            yield state
        finally:
            state.daemon.close()
        state.harness.check_notices()


@BOUNDED
@given(ending=st.sampled_from(ENDINGS),
       pids=st.frozensets(st.integers(min_value=4500, max_value=4520), max_size=3))
# Every ending once with r4's observer, whatever is drawn:
@example(ending="export", pids=frozenset({OBSERVER}))
@example(ending="verdict", pids=frozenset({OBSERVER}))
@example(ending="unlaunched", pids=frozenset({OBSERVER}))
@example(ending="quarantine-resolution", pids=frozenset({OBSERVER}))
@example(ending="cancel-queued", pids=frozenset({OBSERVER}))
@example(ending="fail-queued", pids=frozenset({OBSERVER}))
@example(ending="cancelled-before-launch", pids=frozenset({OBSERVER}))
def test_r4_i6_every_ending_with_an_observer_leaves_one_reader_row_and_no_writer_key(tmp_path_factory, ending, pids):
    root = tmp_path_factory.mktemp("r4-ending") / "state"
    root.mkdir()
    with ending_world(root) as state:
        job_id = state.run(ending, pids)
        state.check(job_id, pids)
