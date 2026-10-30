"""Independent #183 probes for generated lifecycle interleavings and store faults.

All repositories and daemon roots used here belong to pytest's temporary tree.
"""
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import threading

from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import host_push
from subfleet.daemon import Daemon
from subfleet.retention_archive import owned_worktree
from test_host_push import submit, worlds  # noqa: F401
from test_host_push_admission import ordinary, pin_routing, placed


def test_one_transient_ownership_write_error_does_not_freeze_all_detached_admission(worlds):
    """The database is healthy again before the first reaping pass begins."""
    with worlds() as world:
        core = world.core
        pin_routing(world, world.patch)
        push = submit(world, "jobs/transient-ownership-write")
        entered, release = threading.Event(), threading.Event()

        def prepared(root, source, baseline, destination):
            entered.set()
            assert release.wait(30), "review did not release checkout construction"
            destination.mkdir(parents=True)

        world.patch.setattr(host_push, "prepare_workspace", prepared)
        core._admit_kind("detached")
        future = core._push_workspaces[push]
        assert entered.wait(30)
        # This is an actual SQLite write lock, released after the store's default
        # five-second busy timeout. No transaction implementation is replaced.
        blocker = sqlite3.connect(core.root / "state.sqlite3", isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            release.set()
            try:
                future.result(timeout=180)
            except sqlite3.OperationalError as exc:
                assert str(exc) == "database is locked"
            else:
                # Ownership now belongs to admission completion. Keep the
                # same real lock through that write's timeout instead.
                assert future.result() is True
                assert push in core._reap_push_workspaces({})
                assert core._push_workspaces[push] is future
                assert future.exception() is None
        finally:
            release.set()
            blocker.rollback()
            blocker.close()
        assert (core.root / "worktrees" / push).is_dir()
        assert core._job(push)["worktree"] is None
        # The ordinary job arrives only after the worker hit its one-shot fault.
        plain = ordinary(world)
        repeated = []
        for _ in range(3):
            try:
                core._admit_kind("detached")
            except sqlite3.OperationalError as exc:
                repeated.append(str(exc))
        assert not repeated, f"a recovered store still rethrows the same frozen Future: {repeated}"
        assert placed(core, plain), "one failed ownership write starved an unrelated non-push job"


class Rows:
    """Small real SQLite table for the production preparation and reaper SQL."""
    def __init__(self, count):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE jobs (job_id TEXT PRIMARY KEY, state TEXT, cancel_requested_at TEXT,
                               worktree TEXT, wait_reason TEXT, next_check_at TEXT);
            CREATE TABLE attempts (job_id TEXT);
            CREATE TABLE leases (lease_key TEXT PRIMARY KEY, holder TEXT, acquired_at TEXT, expires_at TEXT);
        """)
        for index in range(count):
            self.connection.execute("INSERT INTO jobs VALUES (?, 'queued', NULL, NULL, NULL, NULL)",
                                    (str(index),))
        self.connection.commit()

    @contextmanager
    def transaction(self, *args, **kwargs):
        with self.connection:
            yield self.connection

    def one(self, sql, parameters):
        row = self.connection.execute(sql, parameters).fetchone()
        return dict(row) if row is not None else None

    def list_attempts(self, job_id):
        return self.connection.execute("SELECT * FROM attempts WHERE job_id=?", (job_id,)).fetchall()

    def job(self, job_id):
        row = self.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        return {**row, "push_branch": f"jobs/{job_id}", "sandbox": "workspace-write",
                "in_place": False, "workdir": "/unused/local-fixture", "workdir_head": "0" * 40,
                "kind": "dispatch", "task": None}


class ManualExecutor:
    """Same serialized submission ordering as the push executor, chosen by steps."""
    def __init__(self):
        self.pending = []

    def submit(self, fn, *args):
        future = Future()
        self.pending.append((future, fn, args))
        return future

    def complete(self):
        if not self.pending:
            return
        future, fn, args = self.pending.pop(0)
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(fn(*args))
            except BaseException as exc:
                future.set_exception(exc)

    def drain(self):
        while self.pending:
            self.complete()


def run_interleavings(root, count, steps, patch):
    core = object.__new__(Daemon)
    core.root, core.store, core.pushes = root, Rows(count), ManualExecutor()
    core._push_workspaces, core._push_unused_workspaces = {}, set()
    core._push_workspace_store_errors = {}
    core._push_workspace_store_failures = {}
    core._push_workspace_store_first_failure = {}
    core._push_preparation_offered = False
    core._capacity_waits = {}
    core._notify = lambda: None
    built = []
    cancel_during_build = [False]

    def prepare(state_root, source, baseline, destination):
        destination.mkdir(parents=True)
        (destination / "complete").write_text("whole checkout")
        built.append(destination.name)
        if cancel_during_build[0]:
            core.store.connection.execute(
                "UPDATE jobs SET state='cancelled',cancel_requested_at='now' WHERE job_id=?",
                (destination.name,))
            core.store.connection.commit()

    patch.setattr(host_push, "prepare_workspace", prepare)
    pinned = set()

    def detached_pass():
        core._push_preparation_offered = False
        core._reap_push_workspaces({})
        for index in range(count):
            job = core.store.job(str(index))
            if job["state"] not in {"queued", "waiting"} or job["cancel_requested_at"]:
                continue
            if str(index) in pinned:
                continue
            if core._push_workspace_ready(job):
                checkout = Path(core.store.job(str(index))["worktree"])
                assert (checkout / "complete").is_file(), "readiness exposed before complete checkout"
                assert not core.store.list_attempts(str(index)), "job placed twice"
                core.store.connection.execute("INSERT INTO attempts VALUES (?)", (str(index),))
                core.store.connection.execute("UPDATE jobs SET state='running' WHERE job_id=?", (str(index),))
                core.store.connection.commit()
        assert len(core._push_workspaces) <= 1

    for action, choice in steps:
        job_id = str(choice % count)
        if action == "pass":
            detached_pass()
        elif action == "complete":
            core.pushes.complete()
        elif action == "complete_cancel":
            cancel_during_build[0] = True
            try:
                core.pushes.complete()
            finally:
                cancel_during_build[0] = False
        elif action == "cancel":
            if not core.store.list_attempts(job_id):
                core.store.connection.execute(
                    "UPDATE jobs SET state='cancelled',cancel_requested_at='now' WHERE job_id=?", (job_id,))
                core.store.connection.commit()
        elif action == "pin":
            pinned.add(job_id)
        elif action == "unpin":
            pinned.discard(job_id)
        elif action == "restart":
            # A clean shutdown drains running preparation; a crash forgets queued
            # work. This step models the completed-checkout restart boundary.
            core.pushes.drain()
            core._push_workspaces, core._push_unused_workspaces = {}, set()
            core._push_workspace_store_errors = {}
            core._push_workspace_store_failures = {}
            core._push_workspace_store_first_failure = {}
            core._push_preparation_offered = False
        elif action == "crash":
            # An offered but not yet started build disappears on process loss;
            # complete checkouts, if any, remain recorded for reuse or retention.
            core.pushes.pending.clear()
            core._push_workspaces, core._push_unused_workspaces = {}, set()
            core._push_workspace_store_errors = {}
            core._push_workspace_store_failures = {}
            core._push_workspace_store_first_failure = {}
            core._push_preparation_offered = False
        elif action == "turn":
            before = dict(core._push_workspaces)
            turn = {"kind": "turn", "push_branch": None, "sandbox": "workspace-write", "in_place": True}
            assert not Daemon._in_pass(turn, "detached")
            assert core._push_workspace_ready(turn)
            assert core._push_workspaces == before
        for index in range(count):
            job = core.store.job(str(index))
            destination = root / "worktrees" / str(index)
            if core.store.list_attempts(str(index)):
                assert (destination / "complete").is_file()
            elif destination.exists():
                assert (str(index) in core._push_workspaces
                        or owned_worktree(job, root) == destination), (
                    "completed unused checkout is neither outstanding nor persistently tracked")

    # Release all unrelated holds and finish each finite preparation. Every live
    # job must then place, including after arbitrary cancellations and restarts.
    pinned.clear()
    for _ in range(count + 3):
        detached_pass()
        core.pushes.drain()
    detached_pass()
    for index in range(count):
        job = core.store.job(str(index))
        if job["state"] != "cancelled":
            assert core.store.list_attempts(str(index)), "completed construction lost readiness forever"
        if job["state"] == "cancelled" and (root / "worktrees" / str(index)).exists():
            # A restart loses the in-memory cleanup tracker; persistent ownership
            # remains the retention fallback explicitly promised by the contract.
            assert owned_worktree(job, root) == root / "worktrees" / str(index)
    assert all(built.count(str(index)) <= 1 for index in range(count))
    core.store.connection.close()


@settings(max_examples=80, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(count=st.integers(min_value=1, max_value=4),
       steps=st.lists(st.tuples(st.sampled_from(["pass", "turn", "complete", "complete_cancel", "cancel",
                                               "restart", "crash", "pin", "unpin"]),
                               st.integers(min_value=0, max_value=8)), min_size=1, max_size=55))
def test_generated_preparation_pass_completion_cancel_restart_interleavings(tmp_path, monkeypatch, count, steps):
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory, monkeypatch.context() as patch:
        run_interleavings(Path(directory), count, steps, patch)
