"""C-8.5, review P3-5 and P3-7: where a push stands across a restart, and
which thread waits for it.

`pushing_at` is committed with the ownership claim immediately before `git
push`. A daemon that stops before it sent nothing, and the next one verifies
and publishes; one that stops after it may have reached the remote, and the
next one says to inspect the remote rather than pushing again. Every push runs
on the one push thread, never holding a pool worker while another push runs.
"""
import threading

from subfleet import host_push
from subfleet.daemon import Daemon
from subfleet.procs import Containment
from test_host_push import accept, commit, drain, remote_heads, submit, worlds  # noqa: F401


class Stopped(RuntimeError):
    """The daemon stopping at this point: nothing after it runs."""


def restart(world):
    """A new daemon on the same state root, as after a stop or a crash."""
    root = world.core.root
    world.core.close()
    world.core = Daemon(root, desktop_prober=lambda: None)
    world.core._contain = lambda attempt: Containment()
    return world.core


def pushes(world):
    return [args for _, args in world.calls if args[0] == "push"]


def test_a_restart_before_the_marker_verifies_again_and_publishes_once(worlds):
    with worlds() as world:
        submit(world)
        sha = commit(world.repo)
        actual = host_push.verify_bundle

        def stops(*args, **kwargs):
            raise Stopped("daemon stopped during verification")
        world.patch.setattr(host_push, "verify_bundle", stops)
        row = accept(world)
        assert not row["push_sha"] and not row["push_error"]
        record = world.core.store.one("SELECT * FROM job_pushes WHERE job_id=?", (world.job_id,))
        assert record["result"] == "pending" and record["pushing_at"] is None and record["intakes"] == 1
        assert world.core.store.query("SELECT * FROM job_owned_branches") == []
        world.patch.setattr(host_push, "verify_bundle", actual)
        restart(world)
        assert world.core._pending_exports() == [world.job_id]
        row = drain(world)
        assert row["push_sha"] == sha and not row["push_error"]
        record = world.core.store.one("SELECT * FROM job_pushes WHERE job_id=?", (world.job_id,))
        assert record["result"] == "succeeded" and record["pushing_at"] and record["intakes"] == 2
        assert len(pushes(world)) == 1
        assert remote_heads(world)["refs/heads/jobs/finished"] == sha
        assert world.core._pending_exports() == []


def test_a_restart_after_the_marker_reports_the_remote_for_inspection(worlds):
    with worlds() as world:
        submit(world)
        sha = commit(world.repo)
        inner = host_push.git

        def stops_at_push(repo, *args, **kwargs):
            if args[0] == "push":
                raise Stopped("daemon stopped as git push began")
            return inner(repo, *args, **kwargs)
        world.patch.setattr(host_push, "git", stops_at_push)
        accept(world)
        record = world.core.store.one("SELECT * FROM job_pushes WHERE job_id=?", (world.job_id,))
        assert record["result"] == "pending" and record["pushing_at"] and record["sha"] == sha
        assert world.core.store.one("SELECT * FROM job_owned_branches")["family_job_id"] == world.job_id
        world.patch.setattr(host_push, "git", inner)
        restart(world)
        row = drain(world)
        assert "inspect the remote" in row["push_error"] and not row["push_sha"]
        assert pushes(world) == []
        assert remote_heads(world) == {"refs/heads/trunk": world.base}
        assert "inspect the remote" in world.core.store.list_notices()[0]["text"]
        assert world.core._pending_exports() == []


def test_verification_cut_short_three_times_fails_without_sending(worlds):
    with worlds() as world:
        submit(world)
        commit(world.repo)

        def stops(*args, **kwargs):
            raise Stopped("daemon stopped during verification")
        world.patch.setattr(host_push, "verify_bundle", stops)
        accept(world)
        for expected in (2, 3):
            restart(world)
            drain(world)
            assert world.core.store.one("SELECT intakes FROM job_pushes")["intakes"] == expected
        restart(world)
        row = drain(world)
        assert "interrupted 3 times; nothing was sent" in row["push_error"]
        assert pushes(world) == [] and world.core._pending_exports() == []


def test_pushes_queue_on_the_push_thread_and_never_hold_a_pool_worker(worlds):
    """Two jobs accepted while a push is held: acceptance and export return at
    once, the second push waits behind the first on the push thread, and both
    publish once it is free."""
    with worlds() as world:
        first_job = submit(world, "jobs/one")
        first = commit(world.repo, text="one")
        gate, running, threads, peak = threading.Event(), [], set(), []
        actual = world.core._publish_bundle

        def held(job):
            threads.add(threading.current_thread().name)
            running.append(job["job_id"])
            peak.append(len(running))
            try:
                assert gate.wait(timeout=600)
                return actual(job)
            finally:
                running.remove(job["job_id"])
        world.core._publish_bundle = held
        accept(world, first_job, settle=False)
        second_job = submit(world, "jobs/two")
        second = commit(world.repo, text="two")
        accept(world, second_job, settle=False)
        for job in (first_job, second_job):
            world.core._export(job)          # a pool worker's pass: returns, never waits
        assert not gate.is_set()
        gate.set()
        assert drain(world, first_job)["push_sha"] == first
        assert drain(world, second_job)["push_sha"] == second
        assert threads == {"subfleet-push_0"} and max(peak) == 1
        assert remote_heads(world)["refs/heads/jobs/one"] == first
        assert remote_heads(world)["refs/heads/jobs/two"] == second
        assert world.core._pending_exports() == []
