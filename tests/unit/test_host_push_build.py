"""Host checkout preparation never holds the detached admission pass."""
import os
from pathlib import Path
import threading
import time

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import host_push
from test_host_push import worlds, submit  # noqa: F401
from test_host_push_admission import notices, ordinary, pin_routing, placed


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
        assert release.wait(60), "test did not release checkout build"
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
        assert entered.wait(30)
        # Assert event ordering with room for unrelated host/database work.
        # The build stays held well beyond this deadline; measure real pass
        # latency separately rather than making the property a one-second SLA.
        assert returned.wait(10), "checkout build blocked the admission pass"
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
        caller.join(120)
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
        def synchronous(job):
            if job.get("push_branch") is not None:
                world.core._prepare_push_workspace(job, world.core.root / "worktrees" / job["job_id"])
            return True
        world.patch.setattr(world.core, "_push_workspace_ready", synchronous)
        with pytest.raises(AssertionError, match="checkout build blocked"):
            build_does_not_block(world, world.patch)


def cancellation_after_snapshot(world, patch, *, skip):
    """Cancel after the pass reads its row, before it consumes a false build."""
    from subfleet.protocol import KillArgs

    pin_routing(world, patch)
    core = world.core
    job = submit(world)
    entered, release = threading.Event(), threading.Event()
    snapshot, consume = threading.Event(), threading.Event()
    errors, calls, gated = [], [], []
    prepare = host_push.prepare_workspace
    worker = core._prepare_push_workspace

    def watched(*args, **kwargs):
        calls.append(threading.current_thread().name)
        if not skip:
            entered.set()
            assert release.wait(30), "test did not release checkout build"
        return prepare(*args, **kwargs)

    def held_worker(*args, **kwargs):
        entered.set()
        assert release.wait(30), "test did not release cancelled worker"
        return worker(*args, **kwargs)

    patch.setattr(host_push, "prepare_workspace", watched)
    if skip:
        patch.setattr(core, "_prepare_push_workspace", held_worker)
    core._admit_kind("detached")
    future = core._push_workspaces[job]
    behind = ordinary(world)
    ready = core._push_workspace_ready

    def paused(job_row):
        if job_row["job_id"] == job:
            snapshot.set()  # _admit_pass already captured this uncancelled row
            assert consume.wait(30), "test did not release admission snapshot"
            result = ready(job_row)
            gated.append(result)
            return result
        return ready(job_row)

    def admission():
        try:
            core._admit_kind("detached")
        except BaseException as exc:
            errors.append(exc)

    patch.setattr(core, "_push_workspace_ready", paused)
    caller = threading.Thread(target=admission, name="test-admission-snapshot")
    try:
        assert entered.wait(30)
        caller.start()
        assert snapshot.wait(30)
        core.kill(KillArgs(job))
        release.set()
        future.result(timeout=180)
        assert not (core.root / "worktrees" / job).exists()
    finally:
        release.set()
        consume.set()
        if caller.ident is not None:
            caller.join(30)
    assert not caller.is_alive()
    assert errors == []
    assert gated == [False], "discarded build was marked ready"
    assert calls == ([] if skip else ["subfleet-push_0"]), "synchronous rebuild after cancellation"
    assert core._job(job)["state"] == "cancelled"
    assert core.store.list_attempts(job) == []
    assert core.store.list_leases(job) == []
    assert placed(core, behind)


@pytest.mark.parametrize("skip", [False, True], ids=["discarded", "skipped"])
def test_cancelled_build_after_admission_snapshot_never_rebuilds(worlds, skip):
    with worlds() as world:
        cancellation_after_snapshot(world, world.patch, skip=skip)


def test_mutation_ignoring_discarded_build_result_is_caught(worlds):
    with worlds() as world:
        ready = world.core._push_workspace_ready

        def ignored_false(job):
            future = world.core._push_workspaces.get(job["job_id"])
            result = ready(job)
            if future is not None and future.done() and future.exception() is None:
                return True
            return result
        world.patch.setattr(world.core, "_push_workspace_ready", ignored_false)
        with pytest.raises(AssertionError, match="discarded build was marked ready"):
            cancellation_after_snapshot(world, world.patch, skip=False)


def test_missing_ready_checkout_never_rebuilds_inside_admission(worlds):
    import shutil

    with worlds() as world:
        pin_routing(world, world.patch)
        core = world.core
        job = submit(world)
        core._admit_kind("detached")
        core._push_workspaces[job].result(timeout=180)
        behind = ordinary(world)
        ready = core._push_workspace_ready

        def disappeared(job_row):
            result = ready(job_row)
            if job_row["job_id"] == job and result:
                shutil.rmtree(core.root / "worktrees" / job)
            return result

        def rebuild(*args, **kwargs):
            raise AssertionError("synchronous rebuild of missing ready checkout")

        world.patch.setattr(core, "_push_workspace_ready", disappeared)
        world.patch.setattr(host_push, "prepare_workspace", rebuild)
        core._admit_kind("detached")
        row = core._job(job)
        assert (row["state"], row["rc"]) == ("failed", 7)
        assert "disappeared after preparation" in " ".join(notices(core, job))
        assert core.store.list_attempts(job) == []
        assert core.store.list_leases(job) == []
        assert placed(core, behind)


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
    source = Path(os.environ.get("SF_HOST_PUSH_BENCHMARK_SOURCE", Path(__file__).resolve().parents[2]))
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
            try:
                return actual(repo, *args, **kwargs)
            finally:
                timings.append((args[0], time.perf_counter() - started))

        world.patch.setattr(host_push, "git", timed)
        started = time.perf_counter()
        world.core._admit_kind("detached")
        pass_s = time.perf_counter() - started
        assert placed(world.core, behind)
        admit_ready(world.core)
        row = world.core._job(job)
        outcome = {key: row[key] for key in ("state", "rc", "wait_reason", "worktree")}
        messages = notices(world.core, job)
        print(f"REAL_REPO admission_pass_s={pass_s:.6f} git_calls={timings} outcome={outcome} notices={messages}")
        if not placed(world.core, job):
            # This measurement preserves the existing Git cap. Heavy host
            # load may refuse the real repository; that result must still
            # settle only this job and leave the next one already placed.
            assert (row["state"], row["rc"]) == ("failed", 7), (outcome, messages)
            assert "host git fsck failed (TimeoutExpired)" in " ".join(messages)
            assert any(command == "fsck" and elapsed >= host_push.TIMEOUT_S
                       for command, elapsed in timings)
            assert not (world.core.root / "worktrees" / job).exists()
