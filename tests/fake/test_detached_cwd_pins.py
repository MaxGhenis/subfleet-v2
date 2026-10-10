"""A reservation pins the resolved detached cwd through its lease's lifetime."""
from pathlib import Path
import tempfile
from types import SimpleNamespace

from hypothesis import example, given, settings, strategies as st
import pytest

from subfleet import daemon as daemon_module, folders, retention
from subfleet import retention_archive as rarch
from tests.fake_adapter import FakeAdapter
from tests.fake.test_admission_latency import fleet_daemon, submit
from tests.fake.test_admission_liveness import _end, _live
from tests.fake.test_writer_retention_fence import checkout, measured
from tests.unit.retention_world import git
from tests.unit.test_retention_shared_folders import retiring_tree


def setup(daemon, harness, writable):
    measured(daemon, harness)
    tree, nested = retiring_tree(daemon, harness)
    outside = Path(checkout(harness.root / "outside"))
    job = submit(daemon, harness, workdir=str(outside), in_place=True,
                 sandbox="workspace-write" if writable else "read-only")
    return Path(tree), Path(nested), outside, job


def swap(outside, nested):
    outside.rename(outside.with_name("outside-original"))
    outside.symlink_to(nested, target_is_directory=True)


def sweep(daemon):
    return retention.maintenance(daemon.store, daemon.root, max_jobs=0, max_bytes=0,
                                 holders=lambda watches, **_: {})


def attempt(daemon, job):
    return dict(daemon.store.list_attempts(job)[-1])


def quarantine(daemon, job):
    # The real quarantine transition releases lane slots, retaining folder leases.
    daemon._quarantine(attempt(daemon, job), SimpleNamespace(to_dict=lambda: {"verified_empty": False}),
                       "fixture: process census is uncertain")


def launch(daemon, job):
    """Run real launch preparation through the adapter boundary, without a process."""
    seen = []

    class Prepared(BaseException):
        pass

    def prepared(adapter, spec, *args, **kwargs):
        seen.append(spec.workdir)
        raise Prepared()

    with pytest.MonkeyPatch.context() as launch_patch:
        launch_patch.setattr(FakeAdapter, "build_launch", prepared)
        launch_patch.setattr(daemon, "_validate_home", lambda lane: None)
        launch_patch.setattr(daemon_module, "resolve_credential", lambda credential: {})
        launch_patch.setattr(daemon, "_guard_override", lambda *args, **kwargs: None)
        with pytest.raises(Prepared):
            daemon._launch(attempt(daemon, job))
    daemon.store.update_attempt(attempt(daemon, job)["attempt_id"], state="running")
    return seen[0]


@pytest.mark.parametrize("writable", [False, True], ids=["reader", "writer"])
@pytest.mark.parametrize("state", ["reserved", "starting", "running", "finalizing", "quarantined"])
def test_resolved_detached_cwd_survives_retention_for_the_entire_lease(tmp_path, writable, state):
    """Review lines 11–30: swap before admission, then retire with no provider."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        tree, nested, outside, job = setup(daemon, harness, writable)
        swap(outside, nested)
        daemon._admit()
        assert _live(daemon, job), daemon._holds
        if state in ("running", "finalizing", "quarantined"):
            assert Path(launch(daemon, job)) == nested
        if state == "quarantined":
            quarantine(daemon, job)
            assert daemon._job(job)["state"] == "lost"
        else:
            daemon.store.update_attempt(attempt(daemon, job)["attempt_id"], state=state)
        result = sweep(daemon)
        assert "retired" not in result["pruned"] and nested.is_dir(), result
        assert folders.turn_holds(daemon.store.query, str(nested), (folders.READER,))
        if writable:
            # C-6.5 still contends only on the write target recorded at submission.
            assert daemon.store.one("SELECT holder FROM leases WHERE lease_key=?",
                                    (folders.exclusive_key(str(outside)),))["holder"] == job
            assert daemon.store.one("SELECT 1 FROM leases WHERE lease_key=?",
                                    (folders.exclusive_key(str(nested)),)) is None
        _end(daemon, job)
        assert not daemon.store.list_leases(job)
        # The cwd row is released with the protecting job, allowing reclamation.
        result = sweep(daemon)
        assert "retired" in result["pruned"] and not nested.exists(), result


@pytest.mark.parametrize("writable", [False, True], ids=["reader", "writer"])
def test_current_cwd_fence_and_pin_are_atomic_at_admission(tmp_path, writable):
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        tree, nested, outside, job = setup(daemon, harness, writable)
        swap(outside, nested)
        fence = folders.exclusive_key(str(tree))
        assert daemon.store.acquire_lease(fence, "retention:retired")
        daemon._admit()
        assert not _live(daemon, job), daemon._holds
        assert fence in daemon._holds[job]["leases"]
        assert not daemon.store.list_leases(job)
        daemon.store.release_leases("retention:retired")
        daemon._admit()
        assert _live(daemon, job), daemon._holds
        assert "retired" not in sweep(daemon)["pruned"] and nested.is_dir()


def test_writer_git_target_fence_is_kept_when_its_resolved_cwd_is_outside(tmp_path):
    """The new cwd guard cannot replace #140's independent Git-target fence."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        outside = checkout(harness.root / "outside")
        git(outside, "config", "core.worktree", nested)
        job = submit(daemon, harness, workdir=outside, in_place=True, sandbox="workspace-write")
        assert daemon._write_target(daemon._job(job), outside) == nested
        fence = folders.exclusive_key(tree)
        assert daemon.store.acquire_lease(fence, "retention:retired")
        daemon._admit()
        assert not _live(daemon, job), daemon._holds
        assert daemon._holds[job]["leases"] == [fence]
        daemon.store.release_leases("retention:retired")
        daemon._admit()
        assert _live(daemon, job), daemon._holds


@pytest.mark.parametrize("writable", [False, True], ids=["reader", "writer"])
def test_launch_uses_the_cwd_pinned_at_reservation_after_another_swap(tmp_path, writable):
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        tree, nested, outside, job = setup(daemon, harness, writable)
        swap(outside, nested)
        daemon._admit()
        assert _live(daemon, job), daemon._holds
        outside.unlink()
        outside.symlink_to(outside.with_name("outside-original"), target_is_directory=True)
        assert Path(launch(daemon, job)) == nested
        assert "retired" not in sweep(daemon)["pruned"] and nested.is_dir()


@pytest.mark.parametrize("writable", [False, True], ids=["reader", "writer"])
def test_detached_quarantined_attempt_keeps_a_cwd_in_retention_quarantine(tmp_path, writable):
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        measured(daemon, harness)
        tree, nested = retiring_tree(daemon, harness)
        relative = Path(nested).relative_to(tree)
        seen = {}
        move = rarch.Retirement.quarantine

        def after_move(retirement):
            result = move(retirement)
            if retirement.job_id == "retired" and not seen:
                cwd = retirement.q_worktree / relative
                job = submit(daemon, harness, workdir=str(cwd), in_place=True,
                             sandbox="workspace-write" if writable else "read-only")
                seen.update(cwd=cwd, job=job)
                daemon._admit()
                assert _live(daemon, job), daemon._holds
                quarantine(daemon, job)
            return result

        patch.setattr(rarch.Retirement, "quarantine", after_move)
        for _ in range(2):
            result = sweep(daemon)
            assert seen["cwd"].is_dir(), result
            assert rarch.load_journal(daemon.root, "retired") is not None
        _end(daemon, seen["job"])
        result = sweep(daemon)
        assert not seen["cwd"].exists(), result


@settings(max_examples=40, deadline=None, derandomize=True)
@example(actions=["swap", "admit", "retain", "launch", "retain", "finish"], writable=False, uncertain=False)
@example(actions=["swap", "admit", "retain", "launch", "retain", "finish"], writable=True, uncertain=True)
@example(actions=["admit", "swap", "launch", "retain", "finish"], writable=False, uncertain=True)
@given(actions=st.permutations(["swap", "admit", "retain", "launch", "finish", "retain", "admit"]),
       writable=st.booleans(), uncertain=st.booleans())
def test_any_detached_cwd_lifetime_interleaving_preserves_the_reservation(actions, writable, uncertain):
    """Independent filesystem oracle; invalid lifecycle actions are harmless no-ops.

    Retention can run before/at admission or between any subsequent actions.
    A finish with uncertain process containment uses real quarantine, so the
    invariant continues to apply after the job's state becomes terminal.
    """
    with tempfile.TemporaryDirectory(prefix="detached-cwd-property-") as temporary:
        with fleet_daemon(Path(temporary) / "state") as (daemon, harness, patch):
            tree, nested, outside, job = setup(daemon, harness, writable)
            # Start with a symlink to the outside checkout. Retargeting that
            # link after reservation leaves the pinned canonical cwd in place.
            original = outside.with_name("outside-original")
            outside.rename(original)
            outside.symlink_to(original, target_is_directory=True)
            resolved, swapped, launched, ended = None, False, False, False
            for action in actions:
                if action == "swap" and not swapped:
                    outside.unlink()
                    outside.symlink_to(nested, target_is_directory=True)
                    swapped = True
                elif action == "admit" and resolved is None and not ended:
                    expected = outside.resolve()
                    daemon.store.update_job(job, next_check_at=None)
                    daemon._admit()
                    if _live(daemon, job):
                        resolved = expected
                elif action == "launch" and _live(daemon, job) and not launched:
                    assert Path(launch(daemon, job)) == resolved
                    launched = True
                elif action == "finish" and _live(daemon, job) and not ended:
                    quarantine(daemon, job) if uncertain else _end(daemon, job)
                    ended = True
                elif action == "retain":
                    result = sweep(daemon)
                    protected = _live(daemon, job) or any(
                        a["state"] == "quarantined" for a in daemon.store.list_attempts(job))
                    if protected:
                        assert resolved.is_dir(), (actions, result, resolved)
                        if resolved == nested:
                            assert "retired" not in result["pruned"], (actions, result)
