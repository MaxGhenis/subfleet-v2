"""Independent second-round ownership completion probes for PR #183."""
from contextlib import contextmanager
import sqlite3
import threading

from subfleet import host_push
from subfleet.protocol import KillArgs
from test_host_push import submit, worlds  # noqa: F401
from test_host_push_admission import ordinary, pin_routing, placed
from test_host_push_store_interleavings import finished


def test_recovered_real_sqlite_full_retains_the_completed_checkout(worlds):
    """An actual one-shot storage error is gone before failure processing begins."""
    with worlds() as world:
        core = world.core
        job, future = finished(world)
        plain = ordinary(world)
        connection = core.store.connection
        connection.execute("CREATE TABLE review_storage_probe (payload BLOB)")
        connection.execute("""
            CREATE TRIGGER review_ownership_storage_fault
            AFTER UPDATE OF worktree ON jobs
            WHEN OLD.worktree IS NULL AND NEW.worktree IS NOT NULL
            BEGIN
                INSERT INTO review_storage_probe VALUES (randomblob(1048576));
            END
        """)
        previous_quota = connection.execute("PRAGMA max_page_count").fetchone()[0]
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        connection.execute(f"PRAGMA max_page_count={page_count}")
        actual_transaction = core.store.transaction
        errors = []

        @contextmanager
        def recover_quota_after_rollback(kind="state.changed", **kwargs):
            try:
                with actual_transaction(kind, **kwargs) as tx:
                    yield tx
            except sqlite3.Error as exc:
                if kind == "job.workspace_built" and not errors:
                    errors.append(exc)
                    # Restore capacity after Store has rolled back and before
                    # completion processes the result. Later writes are healthy.
                    connection.execute(f"PRAGMA max_page_count={previous_quota}")
                    connection.execute("DROP TRIGGER review_ownership_storage_fault")
                raise

        world.patch.setattr(core.store, "transaction", recover_quota_after_rollback)
        core._admit_kind("detached")
        core.pushes.submit(lambda: None).result(timeout=180)
        assert len(errors) == 1
        assert (errors[0].sqlite_errorcode & 0xff) == sqlite3.SQLITE_FULL
        assert future.result() is True
        assert placed(core, plain)
        assert core._job(job)["state"] in {"queued", "waiting"}, (
            "recovered transient SQLITE_FULL failed a completed checkout: "
            f"state={core._job(job)['state']}; "
            f"checkout_exists={(core.root / 'worktrees' / job).exists()}"
        )
        assert (core.root / "worktrees" / job).is_dir()
        core._admit_kind("detached")
        assert placed(core, job)


def test_cancelled_unreturned_worker_keeps_other_pushes_waiting(worlds):
    """Retaining a running slot is safe, but its release needs the worker to return."""
    with worlds() as world:
        core = world.core
        pin_routing(world, world.patch)
        first = submit(world, "jobs/review-blocked-worker")
        entered, release = threading.Event(), threading.Event()
        built = []

        def prepare(root, source, baseline, destination):
            built.append(destination.name)
            if destination.name == first:
                entered.set()
                assert release.wait(60), "review did not release its own worker"
            destination.mkdir(parents=True)

        world.patch.setattr(host_push, "prepare_workspace", prepare)
        try:
            core._admit_kind("detached")
            assert entered.wait(30)
            future = core._push_workspaces[first]
            core.kill(KillArgs(first))
            healthy = submit(world, "jobs/review-after-blocked-worker")
            plain = ordinary(world)
            for _ in range(3):
                core._admit_kind("detached")
            assert core._job(first)["state"] == "cancelled"
            assert not future.done()
            assert core._push_workspaces[first] is future
            assert healthy not in core._push_workspaces
            assert not placed(core, healthy)
            assert built == [first]
            assert placed(core, plain)
        finally:
            release.set()
            core.pushes.submit(lambda: None).result(timeout=180)
        assert not (core.root / "worktrees" / first).exists()
        core._admit_kind("detached")
        core._push_workspaces[healthy].result(timeout=180)
        core._admit_kind("detached")
        assert placed(core, healthy)
