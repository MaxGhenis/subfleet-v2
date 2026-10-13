"""Busy writes cannot freeze completion or lose a detached checkout."""
from contextlib import contextmanager
import errno
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import host_push
from subfleet import daemon as daemon_module
from subfleet.daemon import Daemon, PUSH_WORKSPACE_STORE_MAX_FAILURES
from subfleet.retention_archive import owned_worktree
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Launch, attempt_dir
from subfleet.procs import Containment
from subfleet.protocol import KillArgs
from test_host_push import submit, worlds  # noqa: F401
from test_host_push_admission import notices, ordinary, pin_routing, placed
from test_host_push_store_completion_pr183 import ManualExecutor, Rows


class BusyRows(Rows):
    """Roll back a one-shot busy error at any write in a push transaction."""
    def __init__(self, count):
        super().__init__(count)
        self.connection.execute("CREATE TABLE events (kind TEXT, job_id TEXT)")
        self.connection.commit()
        self.fail_at = None
        self.writes = 0
        self.faults = 0

    def write(self):
        self.writes += 1
        if self.fail_at == self.writes:
            self.fail_at = None
            self.faults += 1
            error = sqlite3.OperationalError("database is locked")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error

    @contextmanager
    def transaction(self, kind="state.changed", *, job_id=None):
        owner = self

        class Connection:
            def execute(self, sql, parameters=()):
                if sql.split()[0] in {"UPDATE", "INSERT", "DELETE"}:
                    owner.write()
                return owner.connection.execute(sql, parameters)

        # Check BEGIN, each mutation, the audit and COMMIT independently; an
        # error after UPDATE rolls back ownership and its audit together.
        with self.connection:
            self.write()
            before = self.connection.total_changes
            yield Connection()
            if self.connection.total_changes != before:
                self.write()
                self.connection.execute("INSERT INTO events VALUES (?, ?)", (kind, job_id))
            self.write()


def busy_interleavings(root, count, steps, patch):
    # The count is the bound under test here; the retry window is tested in test_host_push_storage_retry_budget.
    patch.setattr(daemon_module, "PUSH_WORKSPACE_STORE_RETRY_WINDOW_S", 0)
    core = object.__new__(Daemon)
    core.root, core.store, core.pushes = root, BusyRows(count), ManualExecutor()
    core._push_workspaces, core._push_unused_workspaces = {}, set()
    core._push_workspace_store_errors = {}
    core._push_workspace_store_failures = {}
    core._push_workspace_store_first_failure = {}
    core._push_preparation_offered = False
    core._capacity_waits = {}
    core._notify = lambda: None
    built = []
    cancel_in_build = [False]
    pinned = set()
    plain_placements = 0
    passes = 0
    settled_failures = set()

    def fail_queued(job, detail, *, kind):
        # The small table has no production notice schema. Exercise the same
        # atomic state/audit boundary here; real-Store tests check the notice.
        job_id = job["job_id"]
        assert core._push_workspace_store_failures[job_id] == PUSH_WORKSPACE_STORE_MAX_FAILURES
        assert "after 8 completion write failures" in detail
        with core.store.transaction(kind, job_id=job_id) as tx:
            tx.execute("UPDATE jobs SET state='failed' WHERE job_id=?", (job_id,))
        settled_failures.add(job_id)

    core._fail_queued = fail_queued

    def cancel(job_id):
        if core.store.list_attempts(job_id) or core.store.job(job_id)["state"] in {"failed", "cancelled"}:
            return
        try:
            with core.store.transaction("job.cancelled", job_id=job_id) as tx:
                tx.execute("UPDATE jobs SET state='cancelled',cancel_requested_at='now' WHERE job_id=?", (job_id,))
        except sqlite3.OperationalError as exc:
            # A cancellation caller may retry a busy transaction. Its rollback
            # must leave the build result and later admission passes intact.
            assert core._push_workspace_store_busy(exc)

    def prepare(state_root, source, baseline, destination):
        destination.mkdir(parents=True)
        (destination / "complete").write_text("whole checkout")
        built.append(destination.name)
        if cancel_in_build[0]:
            cancel(destination.name)

    patch.setattr(host_push, "prepare_workspace", prepare)

    def detached_pass():
        nonlocal passes, plain_placements
        passes += 1
        core._push_preparation_offered = False
        skipped = core._reap_push_workspaces({})
        for index in range(count):
            job_id = str(index)
            job = core.store.job(job_id)
            if (job["state"] not in {"queued", "waiting"} or job["cancel_requested_at"]
                    or job_id in pinned or job_id in skipped):
                continue
            if core._push_workspace_ready(job):
                checkout = root / "worktrees" / job_id
                assert (checkout / "complete").is_file()
                assert core.store.job(job_id)["worktree"] == str(checkout)
                assert not core.store.list_attempts(job_id), "job placed twice"
                core.store.connection.execute("INSERT INTO attempts VALUES (?)", (job_id,))
                core.store.connection.execute("UPDATE jobs SET state='running' WHERE job_id=?", (job_id,))
                core.store.connection.commit()
        # A distinct ordinary job queues behind push completion on every pass.
        # Its store is healthy: the fault belongs to a push's write. Every pass
        # must reach it even when recording a checkout rolls back.
        assert core._push_workspace_ready({"push_branch": None, "sandbox": "read-only", "in_place": False})
        plain_placements += 1
        assert plain_placements == passes

    try:
        # Guarantee each example includes a real rolled-back completion write.
        detached_pass()
        core.pushes.complete()
        core.store.fail_at = core.store.writes + 2
        detached_pass()
        assert core.store.faults == 1
        for action, choice in steps:
            job_id = str(choice % count)
            if action == "pass":
                detached_pass()
            elif action == "busy":
                core.store.fail_at = core.store.writes + 1 + choice % 4
            elif action == "recover":
                core.store.fail_at = None
            elif action in {"complete", "complete_cancel"}:
                cancel_in_build[0] = action == "complete_cancel"
                try:
                    core.pushes.complete()
                finally:
                    cancel_in_build[0] = False
            elif action == "cancel":
                cancel(job_id)
            elif action == "pin":
                pinned.add(job_id)
            elif action == "unpin":
                pinned.discard(job_id)
            elif action in {"restart", "crash"}:
                if action == "restart":
                    core.pushes.drain()
                else:
                    core.pushes.pending.clear()
                core._push_workspaces, core._push_unused_workspaces = {}, set()
                core._push_workspace_store_errors = {}
                core._push_workspace_store_failures = {}
                core._push_workspace_store_first_failure = {}
                core._push_preparation_offered = False
            for index in range(count):
                job_id = str(index)
                destination = root / "worktrees" / job_id
                if destination.exists():
                    assert (destination / "complete").is_file()
                    # Durable job identity at the conventional path is also
                    # retention ownership across loss of an unrecorded future.
                    assert (job_id in core._push_workspaces
                            or owned_worktree(core.store.job(job_id), root) == destination)
                if job_id in core._push_workspaces and core._push_workspaces[job_id].done():
                    assert core._push_workspaces[job_id].exception() is None

        core.store.fail_at = None
        pinned.clear()
        for _ in range(count + 4):
            detached_pass()
            core.pushes.drain()
        detached_pass()
        for index in range(count):
            job_id = str(index)
            job = core.store.job(job_id)
            if job["state"] == "failed":
                assert job_id in settled_failures, "job failed before its completion budget was exhausted"
                assert core.store.list_attempts(job_id) == []
            elif job["state"] != "cancelled":
                assert core.store.list_attempts(job_id), "recovery never placed a live job"
            destination = root / "worktrees" / job_id
            if destination.exists():
                assert owned_worktree(job, root) == destination, "checkout lost ownership"
            ownership_events = core.store.connection.execute(
                "SELECT COUNT(*) FROM events WHERE kind='job.workspace_built' AND job_id=?", (job_id,)).fetchone()[0]
            assert ownership_events <= 1, "ownership recorded twice"
        assert all(built.count(str(index)) <= 1 for index in range(count)), "checkout built twice"
        assert core._push_workspaces == {}
        assert core._push_workspace_store_errors == {}
        assert core._push_workspace_store_failures == {}
    finally:
        core.pushes.drain()
        core.store.connection.close()


@settings(max_examples=120, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(count=st.integers(min_value=1, max_value=4),
       steps=st.lists(st.tuples(st.sampled_from([
           "pass", "busy", "recover", "complete", "complete_cancel", "cancel",
           "restart", "crash", "pin", "unpin"]), st.integers(min_value=0, max_value=8)),
           min_size=1, max_size=65))
def test_busy_store_completion_cancel_pass_restart_interleavings(tmp_path, monkeypatch, count, steps):
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory, monkeypatch.context() as patch:
        busy_interleavings(Path(directory), count, steps, patch)


def fault_transaction(world, kind, phase, error):
    """Inject once into the real Store, including its audit/commit writes."""
    core = world.core
    transaction = core.store.transaction
    connection = core.store.connection
    armed = [True]
    active = [False]

    class Connection:
        def __getattr__(self, name):
            return getattr(connection, name)

        def execute(self, sql, parameters=()):
            verb = sql.split()[0]
            hit = (phase == "begin" and verb == "BEGIN"
                   or phase == "update" and verb == "UPDATE"
                   or phase == "event" and sql.startswith("INSERT INTO events")
                   or phase == "notice" and sql.startswith("INSERT INTO notices")
                   or phase == "commit" and verb == "COMMIT")
            if armed[0] and active[0] and hit:
                armed[0] = False
                raise error
            return connection.execute(sql, parameters)

    @contextmanager
    def wrapped(event="state.changed", **kwargs):
        previous = active[0]
        active[0] = event == kind
        try:
            with transaction(event, **kwargs) as tx:
                yield tx
        finally:
            active[0] = previous

    world.patch.setattr(core.store, "connection", Connection())
    world.patch.setattr(core.store, "transaction", wrapped)
    return armed


def finished(world, *, failure=None):
    core = world.core
    pin_routing(world, world.patch)
    job = submit(world)

    def prepare(root, source, baseline, destination):
        if failure is not None:
            raise failure
        destination.mkdir(parents=True)

    world.patch.setattr(host_push, "prepare_workspace", prepare)
    core._admit_kind("detached")
    future = core._push_workspaces[job]
    if failure is None:
        assert future.result(timeout=180)
        assert core._job(job)["worktree"] is None
    else:
        with pytest.raises(type(failure)):
            future.result(timeout=180)
    return job, future


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY_SNAPSHOT])
@pytest.mark.parametrize("phase", ["begin", "update", "event", "commit"])
def test_busy_completion_write_rolls_back_and_retries_without_rebuilding(worlds, phase, code):
    with worlds() as world:
        core = world.core
        job, future = finished(world)
        plain = ordinary(world)
        error = sqlite3.OperationalError("busy store report")
        error.sqlite_errorcode = code
        armed = fault_transaction(world, "job.workspace_built", phase, error)
        core._admit_kind("detached")
        assert not armed[0]
        assert future.result() is True
        assert core._push_workspaces[job] is future
        assert core._job(job)["worktree"] is None
        assert core.store.list_attempts(job) == []
        assert placed(core, plain)
        core._admit_kind("detached")
        assert placed(core, job)
        assert job not in core._push_workspaces
        assert len(core.store.query("SELECT 1 FROM events WHERE kind='job.workspace_built' AND job_id=?", (job,))) == 1


@pytest.mark.parametrize("error", [sqlite3.OperationalError("attempt to write a readonly database"),
                                  sqlite3.DatabaseError("database disk image is malformed"),
                                  sqlite3.IntegrityError("ownership constraint failed")],
                         ids=["readonly", "corrupt", "constraint"])
def test_permanent_ownership_failure_fails_only_its_job_with_one_notice(worlds, error):
    with worlds() as world:
        core = world.core
        job, future = finished(world)
        plain = ordinary(world)
        fault_transaction(world, "job.workspace_built", "update", error)
        core._admit_kind("detached")
        core.pushes.submit(lambda: None).result(timeout=180)
        assert future.result() is True
        assert (core._job(job)["state"], core._job(job)["rc"]) == ("failed", 1)
        assert placed(core, plain)
        assert not (core.root / "worktrees" / job).exists()
        assert str(error) in " ".join(notices(core, job))
        core._admit_kind("detached")
        assert len(notices(core, job)) == 1
        assert core._push_workspace_store_errors == {}


def test_cancellation_while_ownership_is_busy_discards_completed_checkout(worlds):
    with worlds() as world:
        core = world.core
        job, future = finished(world)
        plain = ordinary(world)
        fault_transaction(world, "job.workspace_built", "commit", sqlite3.OperationalError("database is locked"))
        core._admit_kind("detached")
        assert core._push_workspaces[job] is future
        core.kill(KillArgs(job))
        core._admit_kind("detached")
        core.pushes.submit(lambda: None).result(timeout=180)
        assert placed(core, plain)
        assert not (core.root / "worktrees" / job).exists()
        assert core._push_workspaces == {}
        assert core._push_workspace_store_errors == {}
        assert core.store.list_attempts(job) == []


@pytest.mark.parametrize("cancelled", [False, True])
def test_restart_finds_unrecorded_completion_after_busy_ownership(worlds, cancelled):
    from test_host_push_restart import restart

    with worlds() as world:
        core = world.core
        job, _ = finished(world)
        fault_transaction(world, "job.workspace_built", "begin", sqlite3.OperationalError("database is locked"))
        core._admit_kind("detached")
        if cancelled:
            core.kill(KillArgs(job))
        destination = core.root / "worktrees" / job
        assert destination.is_dir()
        assert core._job(job)["worktree"] is None
        core = restart(world)
        pin_routing(world, world.patch)

        def rebuild(*args):
            raise AssertionError("rebuilt unrecorded whole checkout after restart")

        world.patch.setattr(host_push, "prepare_workspace", rebuild)
        plain = ordinary(world)
        core._admit_kind("detached")
        assert placed(core, plain)
        if cancelled:
            assert not core.store.list_attempts(job)
            assert owned_worktree(core._job(job), core.root) == destination
        else:
            assert placed(core, job)
            assert core._job(job)["worktree"] == str(destination)


def test_busy_refusal_notice_retains_failed_build_until_notice_is_durable(worlds):
    with worlds() as world:
        core = world.core
        job, future = finished(world, failure=AdapterError("checkout refused", code=7))
        plain = ordinary(world)
        fault_transaction(world, "job.failed", "notice", sqlite3.OperationalError("database is locked"))
        core._admit_kind("detached")
        assert core._push_workspaces[job] is future
        assert core._job(job)["state"] == "waiting"
        assert notices(core, job) == []
        assert placed(core, plain)
        core._admit_kind("detached")
        assert (core._job(job)["state"], core._job(job)["rc"]) == ("failed", 7)
        assert len(notices(core, job)) == 1


def test_permanent_ownership_error_survives_a_busy_failure_notice(worlds):
    with worlds() as world:
        core = world.core
        job, future = finished(world)
        plain = ordinary(world)
        permanent = sqlite3.IntegrityError("ownership constraint failed")
        fault_transaction(world, "job.workspace_built", "update", permanent)
        fault_transaction(world, "job.workspace_failed", "notice", sqlite3.OperationalError("database is locked"))
        core._admit_kind("detached")
        assert placed(core, plain)
        assert core._push_workspaces[job] is future
        assert core._push_workspace_store_errors[job] is permanent
        assert core._job(job)["state"] == "waiting"
        assert notices(core, job) == []
        core._admit_kind("detached")
        assert core._job(job)["state"] == "failed"
        assert str(permanent) in " ".join(notices(core, job))
        assert len(notices(core, job)) == 1
        core.pushes.submit(lambda: None).result(timeout=180)
        assert not (core.root / "worktrees" / job).exists()


def test_busy_deferral_write_does_not_spend_the_build_retry_budget(worlds):
    with worlds() as world:
        core = world.core
        job, future = finished(world, failure=OSError(errno.ENOSPC, "test disk full"))
        plain = ordinary(world)
        fault_transaction(world, "job.workspace_deferred", "commit", sqlite3.OperationalError("database is locked"))
        core._admit_kind("detached")
        assert core._push_workspaces[job] is future
        assert job not in core._workspace_deferrals
        assert placed(core, plain)
        core._admit_kind("detached")
        assert core._workspace_deferrals[job] == 1
        assert core._job(job)["next_check_at"]
        assert notices(core, job) == []


def test_busy_preparing_write_does_not_prevent_an_ordinary_job_placing(worlds):
    with worlds() as world:
        core = world.core
        pin_routing(world, world.patch)
        job = submit(world)
        plain = ordinary(world)
        world.patch.setattr(host_push, "prepare_workspace", lambda root, source, base, dest: dest.mkdir(parents=True))
        armed = fault_transaction(world, "job.workspace_preparing", "update", sqlite3.OperationalError("database is locked"))
        core._admit_kind("detached")
        assert not armed[0]
        assert placed(core, plain)
        core._push_workspaces[job].result(timeout=180)
        core._admit_kind("detached")
        assert placed(core, job)


@pytest.fixture
def queued_admission_template(worlds):
    """Validate the Git input once; each example gets isolated rows and files."""
    with worlds() as world:
        yield world, submit(world)


@contextmanager
def admission_example(template, root, patch):
    root.mkdir()
    shutil.copyfile(template.core.root / "policy.json", root / "policy.json")
    connection = sqlite3.connect(root / "state.sqlite3")
    try:
        template.core.store.connection.backup(connection)
    finally:
        connection.close()
    shutil.copytree(template.core.root / "jobs", root / "jobs")
    core = Daemon(root, desktop_prober=lambda: None)
    core._contain = lambda attempt, foreign_ownership=None: Containment()
    core._saved_launch = lambda attempt: Launch((), {}, (), str(template.repo), str(root / "prompt"),
        str(attempt_dir(root, attempt["job_id"], attempt["seq"]) / "stdout"),
        str(attempt_dir(root, attempt["job_id"], attempt["seq"]) / "stderr"), None, None)
    try:
        yield SimpleNamespace(core=core, repo=template.repo, prompt=template.prompt, patch=patch)
    finally:
        core.close()


@settings(max_examples=20, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(steps=st.lists(st.tuples(st.sampled_from(["pass", "complete", "cancel", "busy"]),
                               st.sampled_from(["begin", "update", "event", "commit"])),
                     min_size=1, max_size=20))
def test_generated_busy_completions_leave_real_detached_admission_live(queued_admission_template, tmp_path,
                                                                    monkeypatch, steps):
    """The generated pass/cancel model also runs the complete admission loop."""
    template, job = queued_admission_template
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory, monkeypatch.context() as patch, \
            admission_example(template, Path(directory) / "state", patch) as world:
        core = world.core
        pin_routing(world, world.patch)
        # Manual scheduling makes completion deterministic; keep the actual
        # executor for Daemon.close(), with all its own queued work drained.
        executor = core.pushes
        manual = ManualExecutor()
        core.pushes = manual
        world.patch.setattr(host_push, "prepare_workspace", lambda root, source, base, dest: dest.mkdir(parents=True))
        try:
            core._admit_kind("detached")
            manual.complete()
            fault_transaction(world, "job.workspace_built", "commit", sqlite3.OperationalError("database is locked"))
            plain = ordinary(world)
            core._admit_kind("detached")
            assert placed(core, plain), "busy completion stopped real detached admission"
            for action, phase in steps:
                if action == "pass":
                    core._admit_kind("detached")
                    assert placed(core, plain)
                elif action == "complete":
                    manual.complete()
                elif action == "cancel":
                    if not core.store.list_attempts(job):
                        core.kill(KillArgs(job))
                else:
                    fault_transaction(world, "job.workspace_built", phase,
                                      sqlite3.OperationalError("database is locked"))
                future = core._push_workspaces.get(job)
                if future is not None and future.done():
                    assert future.exception() is None
                destination = core.root / "worktrees" / job
                if destination.exists():
                    assert owned_worktree(core._job(job), core.root) == destination
            # Give the finite fault schedule enough passes to recover.
            for _ in range(len(steps) + 3):
                core._admit_kind("detached")
                manual.drain()
            assert placed(core, plain)
            row = core._job(job)
            destination = core.root / "worktrees" / job
            if row["state"] == "cancelled":
                assert not destination.exists()
                assert core.store.list_attempts(job) == []
            elif row["state"] == "failed":
                assert not destination.exists()
                assert core.store.list_attempts(job) == []
                assert len(notices(core, job)) == 1
                assert "after 8 completion write failures" in notices(core, job)[0]
            else:
                assert placed(core, job)
                assert row["worktree"] == str(destination)
            assert core._push_workspaces == {}
        finally:
            manual.drain()
            core.pushes = executor


@pytest.mark.parametrize("cancelled", [False, True])
def test_permanent_preparing_error_keeps_running_build_slot_until_cleanup(worlds, cancelled):
    with worlds() as world:
        core = world.core
        pin_routing(world, world.patch)
        job = submit(world)
        entered, release = threading.Event(), threading.Event()
        built = []

        def prepare(root, source, baseline, destination):
            built.append(destination.name)
            if destination.name == job:
                entered.set()
                assert release.wait(60)
            destination.mkdir(parents=True)

        world.patch.setattr(host_push, "prepare_workspace", prepare)
        fault_transaction(world, "job.workspace_preparing", "update", sqlite3.IntegrityError("preparing constraint"))
        fault_transaction(world, "job.workspace_failed", "notice", sqlite3.OperationalError("database is locked"))
        actual = core.store.transaction

        @contextmanager
        def after_worker_starts(kind="state.changed", **kwargs):
            if kind == "job.workspace_preparing":
                assert entered.wait(30)
            with actual(kind, **kwargs) as tx:
                yield tx

        world.patch.setattr(core.store, "transaction", after_worker_starts)
        try:
            core._admit_kind("detached")
            future = core._push_workspaces[job]
            assert entered.is_set() and not future.done()
            healthy = submit(world, "jobs/after-permanent-preparing-error")
            plain = ordinary(world)
            if cancelled:
                core.kill(KillArgs(job))
            core._admit_kind("detached")
            assert core._job(job)["state"] == ("cancelled" if cancelled else "failed")
            assert len(notices(core, job)) == 1
            assert placed(core, plain)
            assert core._push_workspaces[job] is future
            assert healthy not in core._push_workspaces, "unfinished construction released its slot"
            assert built == [job]
        finally:
            release.set()
            core.pushes.submit(lambda: None).result(timeout=180)
        assert not (core.root / "worktrees" / job).exists()
        core._admit_kind("detached")
        core._push_workspaces[healthy].result(timeout=180)
        core._admit_kind("detached")
        assert placed(core, healthy)
        assert core._push_workspace_store_errors == {}
