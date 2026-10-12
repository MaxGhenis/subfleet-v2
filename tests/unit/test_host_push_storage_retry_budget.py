"""Completion storage faults retain ownership, with a finite retry budget."""
from concurrent.futures import Future
from contextlib import contextmanager
import sqlite3

import pytest

from subfleet import daemon as daemon_module
from subfleet.daemon import Daemon, PUSH_WORKSPACE_STORE_MAX_FAILURES, PUSH_WORKSPACE_STORE_RETRY_WINDOW_S
from test_host_push import submit, worlds  # noqa: F401
from test_host_push_admission import notices, ordinary, placed
from test_host_push_store_completion_pr183 import ManualExecutor
from test_host_push_store_interleavings import BusyRows, finished


TRANSIENT_CODES = [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY_SNAPSHOT,
                   sqlite3.SQLITE_FULL, sqlite3.SQLITE_PROTOCOL, sqlite3.SQLITE_IOERR_READ, sqlite3.SQLITE_IOERR_WRITE,
                   sqlite3.SQLITE_IOERR_FSYNC, sqlite3.SQLITE_IOERR_DIR_FSYNC,
                   sqlite3.SQLITE_IOERR_TRUNCATE, None]


@pytest.fixture
def completion(tmp_path):
    core = object.__new__(Daemon)
    core.root, core.store, core.pushes = tmp_path, BusyRows(1), ManualExecutor()
    core._push_workspace_store_errors, core._push_workspace_store_failures = {}, {}
    core._push_workspace_store_first_failure = {}
    core._push_unused_workspaces = set()
    core._capacity_waits = {}
    core._notify = lambda: None
    future = Future()
    future.set_result(True)
    core._push_workspaces = {"0": future}
    destination = tmp_path / "worktrees" / "0"
    destination.mkdir(parents=True)
    (destination / "complete").write_text("whole checkout")

    def unexpected_failure(*args, **kwargs):
        pytest.fail("a recoverable completion write failed its job")

    core._fail_queued = unexpected_failure
    try:
        yield core, future, destination
    finally:
        core.pushes.drain()
        core.store.connection.close()


@pytest.mark.parametrize("code", TRANSIENT_CODES)
@pytest.mark.parametrize("phase", [1, 2, 3, 4], ids=["begin", "update", "audit", "commit"])
def test_completion_storage_fault_retries_atomic_ownership_once(completion, monkeypatch, code, phase):
    core, future, destination = completion
    write = core.store.write

    def fault():
        try:
            write()
        except sqlite3.Error:
            error = sqlite3.OperationalError("database or disk is full")
            if code is not None:
                error.sqlite_errorcode = code
            raise error

    monkeypatch.setattr(core.store, "write", fault)
    core.store.fail_at = core.store.writes + phase
    holds = {}
    assert core._reap_push_workspaces(holds) == {"0"}
    assert holds == {"0": {"reason": "workspace", "preparing": True}}
    assert core._push_workspaces["0"] is future
    assert future.result() is True and future.exception() is None
    assert core.store.job("0")["worktree"] is None
    assert core.store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert (destination / "complete").is_file()
    assert core._push_workspace_store_failures == {"0": 1}
    assert core._push_workspace_ready({"push_branch": None, "sandbox": "read-only", "in_place": False})

    assert core._reap_push_workspaces({}) == set()
    assert core.store.job("0")["worktree"] == str(destination)
    assert core._push_workspaces == {} and core._push_workspace_store_failures == {}
    assert core._finish_push_workspace(core.store.job("0"), future) is True
    assert core.store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


@pytest.mark.parametrize("code", [sqlite3.SQLITE_IOERR,
                                 sqlite3.SQLITE_IOERR_SHORT_READ, sqlite3.SQLITE_IOERR_DATA,
                                 sqlite3.SQLITE_IOERR_CORRUPTFS, sqlite3.SQLITE_IOERR_SHMLOCK,
                                 sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CORRUPT,
                                 sqlite3.SQLITE_CONSTRAINT, sqlite3.SQLITE_MISUSE,
                                 sqlite3.SQLITE_ERROR, sqlite3.SQLITE_IOERR | (100 << 8)])
def test_completion_does_not_retry_unknown_or_permanent_storage_codes(code):
    error = sqlite3.OperationalError("database or disk is full")
    error.sqlite_errorcode = code
    assert not Daemon._push_workspace_store_transient(error)


def test_cancellation_clears_pending_storage_budget(completion, monkeypatch):
    core, future, destination = completion
    transaction = core.store.transaction

    @contextmanager
    def full(*args, **kwargs):
        error = sqlite3.OperationalError("database or disk is full")
        error.sqlite_errorcode = sqlite3.SQLITE_FULL
        raise error
        yield  # Keep the fault at the context-manager boundary.

    monkeypatch.setattr(core.store, "transaction", full)
    assert core._reap_push_workspaces({}) == {"0"}
    monkeypatch.setattr(core.store, "transaction", transaction)
    core.store.connection.execute("UPDATE jobs SET state='cancelled' WHERE job_id='0'")
    core.store.connection.commit()
    core._reap_push_workspaces({})
    core.pushes.drain()
    assert not destination.exists()
    assert core._push_workspaces == {} and core._push_workspace_store_failures == {}
    assert future.result() is True


@pytest.mark.parametrize("failure_commit_faults", [0, 2])
def test_exhausted_completion_budget_fails_once_even_if_failure_write_needs_retry(worlds, failure_commit_faults):
    with worlds() as world:
        core = world.core
        # The count is the bound under test here; the time floor is tested below.
        world.patch.setattr(daemon_module, "PUSH_WORKSPACE_STORE_RETRY_WINDOW_S", 0)
        job, future = finished(world)
        healthy = submit(world, "jobs/after-storage-budget")
        plain = ordinary(world)
        transaction = core.store.transaction
        completion_writes = 0
        failure_writes = 0

        @contextmanager
        def faulty(kind="state.changed", **kwargs):
            nonlocal completion_writes, failure_writes
            if kind == "job.workspace_built" and kwargs.get("job_id") == job:
                completion_writes += 1
                assert completion_writes <= PUSH_WORKSPACE_STORE_MAX_FAILURES
                error = sqlite3.OperationalError("database or disk is full")
                error.sqlite_errorcode = sqlite3.SQLITE_FULL
                raise error
            if kind == "job.workspace_failed" and kwargs.get("job_id") == job:
                failure_writes += 1
                if failure_writes <= failure_commit_faults:
                    error = sqlite3.OperationalError("failure audit write unavailable")
                    error.sqlite_errorcode = sqlite3.SQLITE_IOERR_WRITE
                    raise error
            with transaction(kind, **kwargs) as tx:
                yield tx

        world.patch.setattr(core.store, "transaction", faulty)
        for count in range(1, PUSH_WORKSPACE_STORE_MAX_FAILURES):
            core._admit_kind("detached")
            assert completion_writes == count
            assert core._push_workspace_store_failures[job] == count
            assert core._push_workspaces[job] is future
            assert healthy not in core._push_workspaces
            assert (core.root / "worktrees" / job).is_dir()
            assert core._job(job)["state"] in {"queued", "waiting"}
            assert core.store.list_attempts(job) == []
            assert placed(core, plain)

        for _ in range(failure_commit_faults):
            core._admit_kind("detached")
            assert completion_writes == PUSH_WORKSPACE_STORE_MAX_FAILURES
            assert core._push_workspaces[job] is future
            assert healthy not in core._push_workspaces
            assert core._job(job)["state"] in {"queued", "waiting"}
            assert notices(core, job) == []

        core._admit_kind("detached")
        core.pushes.submit(lambda: None).result(timeout=180)
        assert completion_writes == PUSH_WORKSPACE_STORE_MAX_FAILURES
        assert failure_writes == failure_commit_faults + 1
        assert (core._job(job)["state"], core._job(job)["rc"]) == ("failed", 1)
        assert core._push_workspace_store_errors == {} and core._push_workspace_store_failures == {}
        assert future.result() is True
        assert not (core.root / "worktrees" / job).exists()
        assert core.store.list_attempts(job) == []
        assert len(notices(core, job)) == 1
        assert "after 8 completion write failures" in notices(core, job)[0]
        assert len(core.store.query("SELECT 1 FROM events WHERE kind='job.workspace_failed' AND job_id=?", (job,))) == 1
        assert core.store.query("SELECT 1 FROM events WHERE kind='job.workspace_built' AND job_id=?", (job,)) == []
        core._admit_kind("detached")
        assert len(notices(core, job)) == 1
        assert healthy in core._push_workspaces or placed(core, healthy)


def test_completion_retries_last_the_window_not_just_the_count(completion, monkeypatch):
    """Review r3 of #183: admission passes run every tick, so eight retries took about
    350 ms. A recoverable failure is retried for PUSH_WORKSPACE_STORE_RETRY_WINDOW_S after
    the first one, however many passes that is; the job fails only once both the count and
    the window are spent, and a write that succeeds inside the window records ownership."""
    core, future, destination = completion
    clock = [1000.0]
    monkeypatch.setattr(daemon_module.time, "monotonic", lambda: clock[0])
    transaction = core.store.transaction

    @contextmanager
    def full(*args, **kwargs):
        error = sqlite3.OperationalError("database or disk is full")
        error.sqlite_errorcode = sqlite3.SQLITE_FULL
        raise error
        yield

    monkeypatch.setattr(core.store, "transaction", full)
    for _ in range(PUSH_WORKSPACE_STORE_MAX_FAILURES * 5):
        assert core._reap_push_workspaces({}) == {"0"}
        clock[0] += 0.05
    assert core._push_workspace_store_failures["0"] == PUSH_WORKSPACE_STORE_MAX_FAILURES * 5
    assert destination.is_dir() and core._push_workspaces == {"0": future}
    clock[0] = 1000.0 + PUSH_WORKSPACE_STORE_RETRY_WINDOW_S - 1
    assert core._reap_push_workspaces({}) == {"0"}
    monkeypatch.setattr(core.store, "transaction", transaction)
    core._reap_push_workspaces({})
    assert core._push_workspaces == {} and core._push_workspace_store_failures == {}
    assert core._push_workspace_store_first_failure == {}
    assert destination.is_dir()


def test_completion_fails_once_window_and_count_are_both_spent(completion, monkeypatch):
    core, future, destination = completion
    clock = [1000.0]
    monkeypatch.setattr(daemon_module.time, "monotonic", lambda: clock[0])
    failed = []
    core._fail_queued = lambda job, detail, **kwargs: failed.append(detail)

    @contextmanager
    def full(*args, **kwargs):
        error = sqlite3.OperationalError("database or disk is full")
        error.sqlite_errorcode = sqlite3.SQLITE_FULL
        raise error
        yield

    monkeypatch.setattr(core.store, "transaction", full)
    for _ in range(PUSH_WORKSPACE_STORE_MAX_FAILURES - 1):
        core._reap_push_workspaces({})
    clock[0] += PUSH_WORKSPACE_STORE_RETRY_WINDOW_S
    assert failed == []
    core._reap_push_workspaces({})
    assert len(failed) == 1 and f"after {PUSH_WORKSPACE_STORE_MAX_FAILURES} completion write failures" in failed[0]
    assert core._push_workspace_store_failures == {} and core._push_workspace_store_first_failure == {}
