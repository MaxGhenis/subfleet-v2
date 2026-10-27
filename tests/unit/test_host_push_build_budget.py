"""One checkout preparation awaits a fresh admission decision at a time."""
from concurrent.futures import Future
from contextlib import contextmanager
import errno
import inspect
from pathlib import Path
import subprocess
import tempfile
import textwrap
import threading
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import host_push
from subfleet.adapters.base import AdapterError
from subfleet.daemon import Daemon
from subfleet.disk import GB
from test_host_push import submit, worlds  # noqa: F401
from test_host_push_admission import notices, ordinary, pin_routing, placed


def finished_build_does_not_block_healthy_push(world):
    core = world.core
    pin_routing(world, world.patch)
    first = submit(world, "jobs/finished-held")
    calls = []

    def prepare(root, source, base, destination):
        calls.append(destination.name)
        destination.mkdir(parents=True)

    world.patch.setattr(host_push, "prepare_workspace", prepare)
    core._admit_kind("detached")
    core._push_workspaces[first].result(timeout=180)
    with core.store.transaction("test.pin_after_build") as tx:
        tx.execute("UPDATE jobs SET pinned_lane=? WHERE job_id=?", ("missing-lane", first))
    second = submit(world, "jobs/healthy-after-held")
    core._admit_kind("detached")
    assert core._holds[first]["reason"] == "pin-unadmittable"
    assert second in core._push_workspaces, "a held job's finished build kept the preparation slot"
    assert first not in core._push_workspaces
    core._push_workspaces[second].result(timeout=180)
    core._admit_kind("detached")
    assert placed(core, second)
    assert core.store.list_attempts(first) == []
    assert core.store.list_leases(first) == []
    assert calls == [first, second]
    return first, second


def test_finished_build_releases_slot_before_its_unavailable_pin_hold(worlds):
    with worlds() as world:
        finished_build_does_not_block_healthy_push(world)


def test_mutation_skipping_startup_completion_consumption_is_caught(worlds):
    with worlds() as world:
        world.patch.setattr(world.core, "_reap_push_workspaces", lambda holds: set())
        with pytest.raises(AssertionError, match="held job's finished build kept"):
            finished_build_does_not_block_healthy_push(world)


def test_completed_checkout_cancelled_after_pin_hold_is_cleaned_up(worlds):
    from subfleet.protocol import KillArgs

    with worlds() as world:
        first, _ = finished_build_does_not_block_healthy_push(world)
        core = world.core
        checkout = core.root / "worktrees" / first
        assert checkout.is_dir()
        assert first not in core._push_workspaces
        core.kill(KillArgs(first))
        core._admit_kind("detached")
        core.pushes.submit(lambda: None).result(timeout=180)
        assert not checkout.exists(), "cancellation after a completion was reaped left its unused checkout"
        assert core._job(first)["state"] == "cancelled"
        assert core.store.list_attempts(first) == []
        assert core.store.list_leases(first) == []


@pytest.mark.parametrize("temporary", [False, True], ids=["refused", "backoff"])
def test_finished_build_error_is_classified_before_its_unavailable_pin_hold(worlds, temporary):
    with worlds() as world:
        core = world.core
        pin_routing(world, world.patch)
        first = submit(world, "jobs/failed-held")

        def prepare(root, source, base, destination):
            if destination.name == first:
                if temporary:
                    raise OSError(errno.ENOSPC, "test state volume is full")
                raise host_push._workspace_refused(host_push.PushError("test host push refused", kind="checkout-missing"))
            destination.mkdir(parents=True)

        world.patch.setattr(host_push, "prepare_workspace", prepare)
        core._admit_kind("detached")
        with pytest.raises(OSError if temporary else AdapterError):
            core._push_workspaces[first].result(timeout=180)
        with core.store.transaction("test.pin_after_build_error") as tx:
            tx.execute("UPDATE jobs SET pinned_lane=? WHERE job_id=?", ("missing-lane", first))
        second = submit(world, "jobs/healthy-after-error")
        core._admit_kind("detached")
        assert first not in core._push_workspaces
        assert second in core._push_workspaces
        row = core._job(first)
        if temporary:
            assert (row["state"], row["wait_reason"]) == ("waiting", "workspace")
            assert row["next_check_at"]
            assert core._workspace_deferrals[first] == 1
            assert core._holds[first]["error_type"] == "OSError"
            assert notices(core, first) == []
        else:
            assert (row["state"], row["rc"]) == ("failed", 7)
            (notice,) = notices(core, first)
            assert "committed Git checkout" in notice and "restore the checkout" in notice
            assert "test host push refused" not in notice
        core._push_workspaces[second].result(timeout=180)
        core._admit_kind("detached")
        assert placed(core, second)
        assert core.store.list_attempts(first) == []
        assert core.store.list_leases(first) == []


def test_queued_preparations_do_not_keep_building_after_disk_falls(worlds):
    with worlds() as world:
        core = world.core
        pin_routing(world, world.patch)
        core.policy["admission"]["disk"]["enabled"] = True
        reading = {"gb": 46}
        world.patch.setattr("subfleet.disk.free_bytes", lambda path: int(reading["gb"] * GB))
        jobs = [submit(world, f"jobs/budget-{index}") for index in range(4)]
        behind = ordinary(world)
        entered, release = threading.Event(), threading.Event()
        calls = []

        def prepare(root, source, base, destination):
            calls.append(destination.name)
            if len(calls) == 1:
                entered.set()
                assert release.wait(30), "test did not release the first build"
            # No Git is needed: only the worker's ownership/readiness matters.
            destination.mkdir(parents=True)

        world.patch.setattr(host_push, "prepare_workspace", prepare)
        try:
            core._admit_kind("detached")
            assert entered.wait(30)
            assert placed(core, behind), "a preparing push held the ordinary job"
            reading["gb"] = 39
            core._admit_kind("detached")
            assert core._disk.snapshot["holding"]
            assert all(core._holds[job]["reason"] == "disk" for job in jobs)
        finally:
            release.set()
            # This sentinel waits for every preparation already offered.
            core.pushes.submit(lambda: None).result(timeout=180)
        assert calls == [jobs[0]], "a queued build started after disk admission held it"
        assert len(core._push_workspaces) == 1
        for job in jobs:
            assert core.store.list_attempts(job) == []
            assert core.store.list_leases(job) == []
        for job in jobs[1:]:
            assert core._job(job)["worktree"] is None
            assert not (core.root / "worktrees" / job).exists()


class Queue:
    def __init__(self):
        self.futures = []

    def submit(self, function, *args):
        future = Future()
        self.futures.append(future)
        return future


class Store:
    def __init__(self, jobs):
        self.jobs = jobs

    @contextmanager
    def transaction(self, *args, **kwargs):
        yield self

    def execute(self, sql, parameters):
        if sql.startswith("SELECT") and " FROM leases" in sql:
            return SimpleNamespace(fetchall=lambda: [])
        if sql.startswith("SELECT"):
            return SimpleNamespace(fetchone=lambda: self.jobs[parameters[0]])
        if sql.startswith("UPDATE jobs SET worktree="):
            self.jobs[parameters[1]]["worktree"] = parameters[0]
            return
        job = self.jobs[parameters[0]]
        job.update(state="waiting", wait_reason="workspace")


def gate_harness(root: Path, count: int):
    jobs = {str(index): {"job_id": str(index), "push_branch": f"jobs/{index}",
                        "sandbox": "workspace-write", "in_place": False,
                        "kind": "dispatch", "task": None,
                        "state": "queued", "wait_reason": None, "worktree": None,
                        "cancel_requested_at": None}
            for index in range(count)}
    core = object.__new__(Daemon)
    core.root, core.store, core.pushes = root, Store(jobs), Queue()
    core._push_preparation_offered = False
    core._push_workspaces, core._push_unused_workspaces = {}, set()
    core._push_workspace_store_errors = {}
    core._push_workspace_store_failures = {}
    core._push_workspace_store_first_failure = {}
    core._prepare_push_workspace = lambda *args: True
    return core


def assert_one_outstanding(gate, root, count, steps):
    core = gate_harness(root, count)
    consumed = set()

    def look(index, *, fresh=True):
        if fresh:
            core._push_preparation_offered = False
        previous = dict(core._push_workspaces)
        gate(core, dict(core.store.jobs[str(index)]))
        for job, future in previous.items():
            if job not in core._push_workspaces:
                assert future.done(), "an unfinished build was consumed"
                consumed.add(future)
        outstanding = set(core.pushes.futures) - consumed
        assert outstanding == set(core._push_workspaces.values())
        assert len(outstanding) <= 1, "more than one unconsumed preparation"

    # The same admission pass looks at every queued job while its first build
    # is pending. Subsequent generated looks also include completed results.
    for index in range(count):
        look(index, fresh=False)
    for index, complete in steps:
        if complete and core._push_workspaces:
            job, future = next(iter(core._push_workspaces.items()))
            if not future.done():
                core.store.jobs[job]["worktree"] = str(root / "worktrees" / job)
                future.set_result(True)
        look(index % count)


@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(count=st.integers(min_value=1, max_value=12),
       steps=st.lists(st.tuples(st.integers(min_value=0, max_value=30), st.booleans()), max_size=45))
def test_production_gate_never_queues_two_unconsumed_builds(tmp_path, count, steps):
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        assert_one_outstanding(Daemon._push_workspace_ready, Path(directory), count, steps)


def test_mutation_removing_global_preparation_gate_is_caught(tmp_path):
    gate = Daemon._push_workspace_ready
    assert_one_outstanding(gate, tmp_path, 3, [(1, True), (2, False), (0, True)])
    source = textwrap.dedent(inspect.getsource(gate))
    guard = "if not self._push_workspaces and not self._push_preparation_offered:"
    assert guard in source
    namespace = dict(gate.__globals__)
    exec(compile(source.replace(guard, "if not self._push_preparation_offered:"),
                 "<preparation gate mutant>", "exec"), namespace)
    with pytest.raises(AssertionError, match="more than one unconsumed preparation"):
        assert_one_outstanding(namespace[gate.__name__], tmp_path, 3, [(1, False)])


def assert_one_offer_per_pass(gate, root, count, *, prior=False):
    core = gate_harness(root, count)
    if prior:
        # A previous pass offered a build that was still running when this
        # pass sampled disk. It completes just before its readiness gate.
        gate(core, dict(core.store.jobs["0"]))
        core._push_preparation_offered = False
        core.store.jobs["0"]["worktree"] = str(root / "worktrees" / "0")
        core._push_workspaces["0"].set_result(True)
        assert gate(core, dict(core.store.jobs["0"]))
    for index in range(count):
        job = core.store.jobs[str(index)]
        gate(core, dict(job))
        assert len(core.pushes.futures) <= 1, "more than one preparation offered in a pass"
        future = core._push_workspaces.get(str(index))
        if future is not None:
            # Complete and consume before the next job in this same pass.
            job["worktree"] = str(root / "worktrees" / str(index))
            future.set_result(True)
            assert gate(core, dict(job))
    assert len(core.pushes.futures) == 1


@settings(max_examples=20, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(count=st.integers(min_value=1, max_value=30), prior=st.booleans())
def test_fast_completions_still_offer_only_one_build_per_pass(tmp_path, count, prior):
    assert_one_offer_per_pass(Daemon._push_workspace_ready, tmp_path, count, prior=prior)


def test_mutation_removing_per_pass_offer_limit_is_caught(tmp_path):
    gate = Daemon._push_workspace_ready
    assert_one_offer_per_pass(gate, tmp_path, 3)
    source = textwrap.dedent(inspect.getsource(gate))
    guard = "if not self._push_workspaces and not self._push_preparation_offered:"
    assert guard in source
    namespace = dict(gate.__globals__)
    exec(compile(source.replace(guard, "if not self._push_workspaces:"),
                 "<per-pass preparation mutant>", "exec"), namespace)
    with pytest.raises(AssertionError, match="more than one preparation offered in a pass"):
        assert_one_offer_per_pass(namespace[gate.__name__], tmp_path, 3)


def test_mutation_forgetting_prior_pass_completion_spends_offer_is_caught(tmp_path):
    gate = Daemon._push_workspace_ready
    assert_one_offer_per_pass(gate, tmp_path, 3, prior=True)
    source = textwrap.dedent(inspect.getsource(gate))
    start = source.index("if future is not None and future.done():")
    marker = "self._push_preparation_offered = True"
    position = source.index(marker, start)
    source = source[:position] + "pass" + source[position + len(marker):]
    namespace = dict(gate.__globals__)
    exec(compile(source, "<prior-pass completion mutant>", "exec"), namespace)
    with pytest.raises(AssertionError, match="more than one preparation offered in a pass"):
        assert_one_offer_per_pass(namespace[gate.__name__], tmp_path, 3, prior=True)


@pytest.mark.parametrize("failure", [AdapterError("refused", code=7),
                                    OSError(errno.ENOSPC, "state volume full"),
                                    subprocess.TimeoutExpired("git", 60), RuntimeError("unexpected")],
                         ids=["refusal", "temporary", "git-timeout", "unexpected"])
def test_midpass_failed_future_is_retained_until_startup_classifies_it(tmp_path, failure):
    core = gate_harness(tmp_path, 2)
    gate = Daemon._push_workspace_ready
    assert not gate(core, dict(core.store.jobs["0"]))
    future = core._push_workspaces["0"]
    future.set_exception(failure)
    if isinstance(failure, RuntimeError):
        with pytest.raises(RuntimeError, match="unexpected"):
            gate(core, dict(core.store.jobs["0"]))
    else:
        assert not gate(core, dict(core.store.jobs["0"]))
    assert core._push_workspaces["0"] is future, "mid-pass failure lost its pending classification"
    assert not gate(core, dict(core.store.jobs["1"]))
    assert core.pushes.futures == [future]
