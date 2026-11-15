"""C-6.8: a fresh group/marker census gates every add generation and cleanup."""
from __future__ import annotations

import errno
import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import daemon as daemon_module, procs
from subfleet.daemon import Daemon
from subfleet.retention_git import registration_of
from subfleet.salvage import SalvageError
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.fake.test_worktree_add_c68 import held, alive, submit, due, assert_finished  # noqa: F401
from tests.fake.test_workspace_contract import repository
from tests.unit.test_salvage import git


def census(kind):
    pids = frozenset({4243})
    return {
        "group": procs.Containment(group_pids=pids),
        "marker": procs.Containment(marker_pids=pids),
        "both": procs.Containment(group_pids=pids, marker_pids=pids),
        "empty": procs.Containment(),
        "unverifiable": procs.Containment(unverifiable=True),
        "error": procs.Containment(errors=("inspection failed",)),
    }[kind]


KINDS = st.sampled_from(("group", "marker", "both", "empty", "unverifiable", "error"))


@given(st.lists(KINDS, min_size=1, max_size=25))
@settings(max_examples=60, deadline=None)
def test_no_add_over_a_holder_across_passes(sequence):
    """Invariant 1, F3/F4: every census sequence gates the actual Popen call.

    Reusing one directory (one inode) across every pass cannot cache an empty
    answer. Group-only survivors keep the recorded group until an empty read.
    """
    import tempfile
    with tempfile.TemporaryDirectory(prefix="sf-c68-b1-census-") as scratch:
        core = object.__new__(Daemon)
        core.root = Path(scratch)
        core.policy = {"caps": {"worktree_add_timeout_s": 1}}
        core._worktree_adds = {"job": (4242, None)}
        core._tree_git = lambda *args: ["git", "-C", scratch]
        (core.root / "jobs" / "job").mkdir(parents=True)
        tree = core.root / "worktrees" / "job"
        tree.mkdir(parents=True)
        inode = tree.stat().st_ino
        job = {"job_id": "job", "workdir_head": "commit"}
        tracked = True
        for kind in sequence:
            with pytest.MonkeyPatch.context() as patch:
                observed = []
                starts = []
                def inspect(group, *args, **kwargs):
                    observed.append(group)
                    return census(kind)
                def start(*args, **kwargs):
                    starts.append(args)
                    raise OSError(errno.EAGAIN, "start deliberately stopped")
                patch.setattr(procs, "containment", inspect)
                patch.setattr(daemon_module.subprocess, "Popen", start)
                with pytest.raises((OSError, SalvageError)):
                    core._add_worktree(job, str(tree), None, 1)
                assert bool(starts) == (kind == "empty")
                assert observed == [4242 if tracked else None]
                if kind == "empty":
                    tracked = False
                assert ("job" in core._worktree_adds) is tracked
                assert tree.stat().st_ino == inode


def test_group_only_survivor_is_kept_across_passes(state_daemon, monkeypatch):  # noqa: F811
    daemon, _ = state_daemon
    job = {"job_id": "held-job"}
    daemon._worktree_adds[job["job_id"]] = (4242, None)
    seen = []
    answers = iter([census("group"), census("group"), census("empty")])
    def inspect(group, *args, **kwargs):
        seen.append(group)
        return next(answers)
    monkeypatch.setattr(procs, "containment", inspect)
    for _ in range(2):
        with pytest.raises(SalvageError, match="still has processes"):
            daemon._require_add_empty(job)
        assert job["job_id"] in daemon._worktree_adds
    daemon._require_add_empty(job)
    assert seen == [4242, 4242, 4242]
    assert job["job_id"] not in daemon._worktree_adds


@pytest.mark.parametrize("holder", ["group", "marker", "both"])
def test_a_new_successful_add_in_the_same_directory_needs_a_fresh_census(state_daemon, monkeypatch, holder):  # noqa: F811
    """F4: git preserves an emptied directory's inode across successful adds.

    An empty census from its first checkout cannot bless a later checkout into
    that directory. A holder after the second git exits blocks reuse and a
    third add, even though the path still has the first checkout's inode.
    """
    daemon, harness = state_daemon
    repository(daemon, harness)
    job_id = submit(daemon, harness)
    job = daemon.store.get_job(job_id)
    common = daemon._recorded_common(job)
    tree = daemon.root / "worktrees" / job_id
    tree.mkdir(parents=True)
    identity = (tree.stat().st_dev, tree.stat().st_ino)
    real_popen = subprocess.Popen
    adds, observations = [], []
    daemon.kill_settle_s = 0

    def start(command, *args, **kwargs):
        child = real_popen(command, *args, **kwargs)
        if "worktree" in command and "add" in command:
            adds.append(child)
        return child

    def inspect(group, *args, **kwargs):
        observations.append(group)
        return census(holder if len(adds) >= 2 else "empty")

    def forbidden(*args, **kwargs):
        pytest.fail("a later checkout's holder must prevent removal")

    def checkout_finished():
        assert (tree / "tracked.txt").read_text() == "baseline\n"
        assert git(tree, "rev-parse", "HEAD") == job["workdir_head"]
        assert git(tree, "status", "--porcelain") == ""

    monkeypatch.setattr(daemon_module.subprocess, "Popen", start)
    monkeypatch.setattr(procs, "containment", inspect)
    try:
        first = daemon._add_worktree(job, str(tree), common, 5)
        assert first.returncode == 0 and len(adds) == 1
        checkout_finished()
        assert (tree.stat().st_dev, tree.stat().st_ino) == identity

        # Empty the old checkout but retain the directory, the git 2.55 shape
        # that made a directory-identity cache inherit an obsolete clean read.
        admin = registration_of(common, tree, strict=True)
        assert admin is not None and admin.parent == Path(common) / "worktrees"
        shutil.rmtree(admin)
        for entry in tree.iterdir():
            assert entry.is_file()
            entry.unlink()
        assert (tree.stat().st_dev, tree.stat().st_ino) == identity

        with pytest.raises(SalvageError, match="still has processes"):
            daemon._add_worktree(job, str(tree), common, 5)
        assert len(adds) == 2 and adds[1].returncode == 0
        checkout_finished()
        assert (tree.stat().st_dev, tree.stat().st_ino) == identity
        assert daemon._worktree_adds[job_id][0] == adds[1].pid
        assert observations[-1] == adds[1].pid

        monkeypatch.setattr(daemon, "_discard_worktree", forbidden)
        for operation in (lambda: daemon._workspace(job),
                          lambda: daemon._add_worktree(job, str(tree), common, 5)):
            with pytest.raises(SalvageError, match="still has processes"):
                operation()
        assert len(adds) == 2
        assert (tree.stat().st_dev, tree.stat().st_ino) == identity
    finally:
        # Every real child is bounded, killed while still unreaped if needed,
        # and reaped even when an assertion interrupts a checkout (F9).
        for child in adds:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=5)


def test_a_successful_add_keeps_its_group_if_a_hook_left_a_writer(state_daemon, monkeypatch, tmp_path):  # noqa: F811
    daemon, _ = state_daemon
    daemon.kill_settle_s = 0
    job = {"job_id": "hook-job", "workdir_head": "commit"}
    (daemon.root / "jobs" / job["job_id"]).mkdir(parents=True)
    monkeypatch.setattr(daemon, "_tree_git", lambda *args: ["git"])
    seen = []
    answers = iter([census("empty"), census("group"), census("group"), census("empty")])
    def inspect(group, *args, **kwargs):
        seen.append(group)
        return next(answers)
    class Child:
        pid, returncode = 4242, None
        def __init__(self, *args, **kwargs):
            assert kwargs["start_new_session"] is True
        def wait(self, **kwargs):
            self.returncode = 0
            return 0
    monkeypatch.setattr(procs, "containment", inspect)
    monkeypatch.setattr(daemon_module.subprocess, "Popen", Child)
    with pytest.raises(SalvageError):
        daemon._add_worktree(job, str(tmp_path / "tree"), None, 1)
    with pytest.raises(SalvageError):
        daemon._require_add_empty(job)
    daemon._require_add_empty(job)
    assert seen == [None, 4242, 4242, 4242]


def test_the_cap_settles_repeated_censuses_before_returning(state_daemon, monkeypatch, tmp_path):  # noqa: F811
    """Invariant 2: one surviving census after kill is followed until empty."""
    daemon, _ = state_daemon
    daemon.kill_settle_s = 1
    job = {"job_id": "cap-job", "workdir_head": "commit"}
    (daemon.root / "jobs" / job["job_id"]).mkdir(parents=True)
    monkeypatch.setattr(daemon, "_tree_git", lambda *args: ["git"])
    seen, waits, signals = [], [], []
    answers = iter([census("empty"), census("group"), census("marker"), census("empty")])
    def inspect(group, *args, **kwargs):
        seen.append(group)
        return next(answers)
    class Child:
        pid, returncode = 4242, None
        def __init__(self, *args, **kwargs):
            assert kwargs["start_new_session"] is True
            assert kwargs["env"]["SUBFLEET_ATTEMPT"] == "worktree-add:cap-job"
            assert kwargs["env"]["SUBFLEET_ROOT"] == str(daemon.root)
            assert kwargs["env"]["LC_ALL"] == "C"
            assert "LANGUAGE" not in kwargs["env"]
            assert hasattr(kwargs["stderr"], "fileno")
        def wait(self, **kwargs):
            waits.append(kwargs)
            if len(waits) == 1:
                raise subprocess.TimeoutExpired(["git", "worktree", "add"], 1)
            self.returncode = -signal.SIGKILL
            return self.returncode
    def kill(group, sig):
        assert len(waits) == 1             # leader is still our unreaped child
        signals.append((group, sig))
    monkeypatch.setattr(procs, "containment", inspect)
    monkeypatch.setattr(daemon_module.subprocess, "Popen", Child)
    monkeypatch.setattr(daemon_module.os, "killpg", kill)
    with pytest.raises(subprocess.TimeoutExpired):
        daemon._add_worktree(job, str(tmp_path / "tree"), None, 1)
    assert signals == [(4242, signal.SIGKILL)]
    assert len(waits) == 2 and seen == [None, 4242, 4242, 4242]
    assert not daemon._worktree_adds


def test_the_cap_stops_a_real_filter_with_a_fake_empty_census(state_daemon, held, monkeypatch):  # noqa: F811
    """Real git/group kill; process death read from owned kernel pids only.

    The separate full-census integration test reports a skip when ps is denied.
    """
    daemon, harness = state_daemon
    daemon.policy["caps"]["worktree_add_timeout_s"] = 1
    job_id = submit(daemon, harness)
    held.hold()
    try:
        daemon._admit()
        assert held.filter_pids(), "the add reached the held smudge filter"
        assert not any(alive(pid) for pid in held.filter_pids())
        job = daemon.store.get_job(job_id)
        assert (job["state"], job["wait_reason"]) == ("waiting", "workspace")
    finally:
        held.release()
    due(daemon, job_id)
    daemon._admit()
    assert_finished(daemon.root / "worktrees" / job_id, held.head)


def test_a_denied_group_stop_has_a_bounded_reap_and_retains_the_child(state_daemon, monkeypatch, tmp_path):  # noqa: F811
    """A failed signal never turns the cap into an unbounded wait or releases the path."""
    daemon, _ = state_daemon
    daemon.kill_settle_s = 0.01
    job = {"job_id": "denied-stop", "workdir_head": "commit"}
    (daemon.root / "jobs" / job["job_id"]).mkdir(parents=True)
    monkeypatch.setattr(daemon, "_tree_git", lambda *args: ["git"])
    class Child:
        pid, returncode, ended = 4242, None, False
        def __init__(self, *args, **kwargs):
            pass
        def wait(self, **kwargs):
            assert "timeout" in kwargs
            raise subprocess.TimeoutExpired(["git", "worktree", "add"], kwargs["timeout"])
        def poll(self):
            if self.ended:
                self.returncode = -signal.SIGKILL
            return self.returncode
    def denied(*args):
        raise PermissionError(errno.EPERM, "signal denied")
    monkeypatch.setattr(daemon_module.subprocess, "Popen", Child)
    monkeypatch.setattr(daemon_module.os, "killpg", denied)
    with pytest.raises(SalvageError, match="leader has not exited") as refused:
        daemon._add_worktree(job, str(tmp_path / "tree"), None, 1)
    assert refused.value.transient and isinstance(refused.value.__cause__, PermissionError)
    assert job["job_id"] in daemon._worktree_adds
    child = daemon._worktree_add_children[job["job_id"]]
    # Even a synthetic empty process table cannot contradict our live handle.
    with pytest.raises(SalvageError, match="still has processes"):
        daemon._require_add_empty(job)
    child.ended = True
    daemon._require_add_empty(job)
    assert not daemon._worktree_adds and not daemon._worktree_add_children
