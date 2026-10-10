"""Host checkout preparation never holds the detached admission pass."""
import os
from pathlib import Path
import threading
import time

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import host_push
from test_host_push import worlds, submit  # noqa: F401
from test_host_push_admission import ordinary, pin_routing, placed


def admit_ready(core):
    """Run a look, let builds settle, then consume their results on a new look."""
    core._admit_kind("detached")
    for future in list(getattr(core, "_push_workspaces", {}).values()):
        try:
            future.result(timeout=180)
        except Exception:
            pass  # admission classifies a workspace error, just as before
    core._admit_kind("detached")


def build_does_not_block(world, patch, looks=3):
    pin_routing(world, patch)
    core = world.core
    job = submit(world)
    behind = ordinary(world)
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    actual = host_push.prepare_workspace
    threads, errors = [], []

    def slow(*args, **kwargs):
        threads.append(threading.current_thread().name)
        entered.set()
        assert release.wait(10), "test did not release checkout build"
        return actual(*args, **kwargs)

    def admission():
        try:
            core._admit_kind("detached")
        except BaseException as exc:
            errors.append(exc)
        finally:
            returned.set()

    patch.setattr(host_push, "prepare_workspace", slow)
    caller = threading.Thread(target=admission, name="test-admission")
    caller.start()
    try:
        assert entered.wait(10)
        assert returned.wait(1), "checkout build blocked the admission pass"
        assert errors == []
        assert placed(core, behind)
        assert core.store.list_attempts(job) == []
        assert core.store.list_leases(job) == []
        assert threads == ["subfleet-push_0"]
        for _ in range(looks):
            core._admit_kind("detached")
        assert len(threads) == 1, "a preparing job queued duplicate builds"
    finally:
        release.set()
        caller.join(15)
    admit_ready(core)
    assert placed(core, job)


def test_slow_checkout_build_does_not_block_jobs_behind_it(worlds):
    with worlds() as world:
        build_does_not_block(world, world.patch)


@settings(max_examples=8, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(looks=st.integers(min_value=0, max_value=12))
def test_preparing_job_queues_one_build_across_admission_looks(worlds, looks):
    with worlds() as world:
        build_does_not_block(world, world.patch, looks)


def test_mutation_build_restored_inside_pass_is_caught(worlds):
    with worlds() as world:
        world.patch.setattr(world.core, "_push_workspace_ready", lambda job: True)
        with pytest.raises(AssertionError, match="checkout build blocked"):
            build_does_not_block(world, world.patch)


def test_build_cancelled_while_preparing_is_never_placed(worlds):
    with worlds() as world:
        pin_routing(world, world.patch)
        core = world.core
        job = submit(world)
        entered, release = threading.Event(), threading.Event()
        actual = host_push.prepare_workspace

        def held(*args, **kwargs):
            entered.set()
            assert release.wait(10)
            return actual(*args, **kwargs)

        world.patch.setattr(host_push, "prepare_workspace", held)
        try:
            core._admit_kind("detached")
            assert entered.wait(10)
            from subfleet.protocol import KillArgs
            core.kill(KillArgs(job))
        finally:
            release.set()
        admit_ready(core)
        assert core._job(job)["state"] == "cancelled"
        assert core.store.list_attempts(job) == []
        assert not (core.root / "worktrees" / job).exists()


def test_cancellation_after_build_finishes_discards_unused_checkout(worlds):
    with worlds() as world:
        pin_routing(world, world.patch)
        core = world.core
        job = submit(world)
        core._admit_kind("detached")
        core._push_workspaces[job].result(timeout=180)
        assert (core.root / "worktrees" / job).exists()
        assert core._job(job)["worktree"] == str(core.root / "worktrees" / job)
        from subfleet.protocol import KillArgs
        core.kill(KillArgs(job))
        core._admit_kind("detached")
        # The same serialized executor drains deletion after preparation.
        core.pushes.submit(lambda: None).result(timeout=180)
        assert not (core.root / "worktrees" / job).exists()
        assert core.store.list_attempts(job) == []


def test_restart_reuses_the_whole_ready_checkout_without_another_build(worlds):
    from test_host_push_restart import restart
    with worlds() as world:
        pin_routing(world, world.patch)
        core = world.core
        job = submit(world)
        core._admit_kind("detached")
        core._push_workspaces[job].result(timeout=180)
        built = core._job(job)["worktree"]
        assert built and Path(built).is_dir()
        core = restart(world)
        pin_routing(world, world.patch)

        def rebuild(*args, **kwargs):
            raise AssertionError("rebuilt a ready checkout after restart")

        world.patch.setattr(host_push, "prepare_workspace", rebuild)
        core._admit_kind("detached")
        assert placed(core, job)


@pytest.mark.skipif(os.environ.get("SF_HOST_PUSH_BENCHMARK") != "1", reason="explicit local-repo benchmark")
def test_real_repo_admission_pass_measurement(worlds):
    """Uses the committed checkout's real objects, with no remote operation."""
    source = Path(__file__).resolve().parents[2]
    _, head, _, _ = host_push.checkout_metadata(source)
    with worlds() as world:
        pin_routing(world, world.patch)
        job = submit(world)
        behind = ordinary(world)
        with world.core.store.transaction("test.benchmark") as tx:
            tx.execute("UPDATE jobs SET workdir=?,workdir_head=? WHERE job_id=?", (str(source), head, job))
        timings = []
        actual = host_push.git

        def timed(repo, *args, **kwargs):
            started = time.perf_counter()
            result = actual(repo, *args, **kwargs)
            timings.append((args[0], time.perf_counter() - started))
            return result

        world.patch.setattr(host_push, "git", timed)
        started = time.perf_counter()
        world.core._admit_kind("detached")
        pass_s = time.perf_counter() - started
        assert placed(world.core, behind)
        admit_ready(world.core)
        assert placed(world.core, job), dict(world.core._job(job))
        print(f"REAL_REPO admission_pass_s={pass_s:.6f} git_calls={timings}")
