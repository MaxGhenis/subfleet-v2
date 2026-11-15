"""C-6.8: a fresh group/marker census gates every add generation and cleanup."""
from __future__ import annotations

import errno
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import daemon as daemon_module, procs, worktree_add
from subfleet.daemon import Daemon
from subfleet.retention_git import registration_of
from subfleet.salvage import SalvageError
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.fake.test_worktree_add_c68 import held, alive, submit, due, assert_finished, cap_from_the_filter  # noqa: F401
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

    The separate full-census integration test reports a skip when ps is denied. The 1 s cap counts
    from the filter, and the next pass has the policy's own cap (review r1 of #329, P2).
    """
    daemon, harness = state_daemon
    normal = daemon.policy["caps"]["worktree_add_timeout_s"]
    daemon.policy["caps"]["worktree_add_timeout_s"] = 1
    cap_from_the_filter(monkeypatch, held)
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
    daemon.policy["caps"]["worktree_add_timeout_s"] = normal
    due(daemon, job_id)
    daemon._admit()
    assert_finished(daemon.root / "worktrees" / job_id, held.head)


def wait_until(condition, bound: float, why: str) -> None:
    deadline = time.monotonic() + bound
    while not condition():
        assert time.monotonic() < deadline, why
        time.sleep(0.05)


@pytest.mark.parametrize("denied", ["once", "first-pass"])
def test_a_later_census_stops_a_capped_group_its_first_signal_missed(state_daemon, held, monkeypatch, denied):  # noqa: F811
    """Review r1 of #329 (P2): a `killpg` that failed at the cap was never sent again, so the
    group ran on and held the job until its retries ran out.

    The leader stays the daemon's unreaped child, so each later census KILLs its group again
    before reaping it, and the job waits while the leader lives. Denied once, the census in the
    same pass's failure handler stops the group; denied through the first pass, the next pass
    does. No attempt is reserved until a later pass's census reads empty.
    """
    daemon, harness = state_daemon
    daemon.kill_settle_s = 0.5
    normal = daemon.policy["caps"]["worktree_add_timeout_s"]
    daemon.policy["caps"]["worktree_add_timeout_s"] = 1
    adds = cap_from_the_filter(monkeypatch, held)
    real_killpg = os.killpg
    calls, refusing = [], {"first-pass": True}

    def killpg(group, sig):
        refused = not calls if denied == "once" else refusing["first-pass"]
        calls.append((group, sig, refused))
        if refused:
            raise PermissionError(errno.EPERM, "signal deliberately denied")
        return real_killpg(group, sig)

    monkeypatch.setattr(daemon_module.os, "killpg", killpg)
    job_id = submit(daemon, harness)
    held.hold()
    try:
        daemon._admit()
        assert held.filter_pids(), "the add reached its filter"
        leader = adds[0].pid
        first_filters = held.filter_pids()
        job = daemon.store.get_job(job_id)
        assert (job["state"], job["wait_reason"]) == ("waiting", "workspace")
        if denied == "first-pass":
            assert len(calls) >= 2 and all(refused for *_, refused in calls)
            assert {(group, sig) for group, sig, _ in calls} == {(leader, signal.SIGKILL)}
            assert any(alive(pid) for pid in first_filters), "nothing stopped the group yet"
            refusing["first-pass"] = False
            due(daemon, job_id)
            daemon._admit()
            job = daemon.store.get_job(job_id)
            assert (job["state"], job["wait_reason"]) == ("waiting", "workspace")
        mine = [refused for group, sig, refused in calls if (group, sig) == (leader, signal.SIGKILL)]
        assert mine[0] and False in mine, calls
        wait_until(lambda: not any(alive(pid) for pid in first_filters), 10, "the group outlived its signal")
        assert daemon.store.list_attempts(job_id) == []
    finally:
        held.release()
    daemon.policy["caps"]["worktree_add_timeout_s"] = normal
    due(daemon, job_id)
    daemon._admit()
    assert [a["state"] for a in daemon.store.list_attempts(job_id)] == ["reserved"]
    assert_finished(daemon.root / "worktrees" / job_id, held.head)


def test_each_later_census_signals_a_capped_group_before_it_reaps(state_daemon, monkeypatch, tmp_path):  # noqa: F811
    """C-5.4, C-6.8: on each later census a stopped add's group is signalled, then its leader
    polled; a live leader holds the job, and once it is reaped its group is never signalled
    again and the census decides."""
    daemon, _ = state_daemon
    daemon.kill_settle_s = 0.01
    job = {"job_id": "resignal", "workdir_head": "commit"}
    (daemon.root / "jobs" / job["job_id"]).mkdir(parents=True)
    monkeypatch.setattr(daemon, "_tree_git", lambda *args: ["git"])
    steps = []

    class Child:
        pid, returncode, dying, ended = 4242, None, False, False

        def __init__(self, *args, **kwargs):
            pass

        def wait(self, **kwargs):
            steps.append("wait")
            raise subprocess.TimeoutExpired(["git", "worktree", "add"], kwargs["timeout"])

        def poll(self):
            steps.append("poll")
            if self.ended:
                self.returncode = -signal.SIGKILL
            self.ended = self.dying          # a signalled leader is gone by the next look
            return self.returncode

    denials = [PermissionError(errno.EPERM, "signal deliberately denied")]

    def killpg(group, sig):
        steps.append(("killpg", group, sig))
        if denials:
            raise denials.pop()
        daemon._worktree_add_children[job["job_id"]].dying = True

    monkeypatch.setattr(daemon_module.subprocess, "Popen", Child)
    monkeypatch.setattr(daemon_module.os, "killpg", killpg)
    kill = ("killpg", 4242, signal.SIGKILL)
    with pytest.raises(SalvageError, match="leader has not exited"):
        daemon._add_worktree(job, str(tmp_path / "tree"), None, 1)
    assert steps == ["wait", kill, "wait"]
    del steps[:]
    with pytest.raises(SalvageError, match="still has processes"):
        daemon._require_add_empty(job)
    assert steps == [kill, "poll"]                    # signalled again, then found alive
    assert job["job_id"] in daemon._worktree_adds
    del steps[:]
    daemon._require_add_empty(job)
    assert steps == [kill, "poll"]                    # signalled before the poll that reaps it
    assert not daemon._worktree_adds and not daemon._worktree_add_children and not daemon._worktree_add_stopped
    del steps[:]
    daemon._require_add_empty(job)
    assert steps == []


def test_a_census_never_signals_an_add_its_cap_did_not_stop(state_daemon, monkeypatch):  # noqa: F811
    """Only a stopped add is signalled again: an add still inside its cap only holds the job."""
    daemon, _ = state_daemon
    job = {"job_id": "running-add"}

    class Child:
        pid, returncode = 4242, None

        def poll(self):
            return None

    daemon.__dict__.setdefault("_worktree_add_children", {})[job["job_id"]] = Child()
    daemon.__dict__.setdefault("_worktree_adds", {})[job["job_id"]] = (4242, None)
    monkeypatch.setattr(daemon_module.os, "killpg", lambda *args: pytest.fail("signalled an add inside its cap"))
    with pytest.raises(SalvageError, match="still has processes"):
        daemon._require_add_empty(job)
    assert job["job_id"] in daemon._worktree_adds


def test_the_add_pins_fsmonitor_off_after_the_config_it_inherits(state_daemon, monkeypatch, tmp_path):  # noqa: F811
    """Review r1 of #329 (P2): with `core.fsmonitor=true` git leaves a detached fsmonitor daemon
    carrying the add's marker, which holds the job. The add's environment pins it off, after
    any `GIT_CONFIG_COUNT` entries it already has, and real git reads the pin over the
    repository's own setting while keeping the inherited entry."""
    daemon, _ = state_daemon
    daemon.kill_settle_s = 0
    job = {"job_id": "pinned-add", "workdir_head": "commit"}
    (daemon.root / "jobs" / job["job_id"]).mkdir(parents=True)
    monkeypatch.setattr(daemon, "_tree_git", lambda *args: ["git"])
    inherited = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "Synthetic Name"}
    real_env = daemon_module._git_env
    monkeypatch.setattr(daemon_module, "_git_env", lambda env: {**real_env(env), **inherited})
    seen = []

    class Child:
        pid, returncode = 4242, None

        def __init__(self, command, **kwargs):
            seen.append(kwargs["env"])

        def wait(self, **kwargs):
            self.returncode = 0
            return 0

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon_module.subprocess, "Popen", Child)
        assert daemon._add_worktree(job, str(tmp_path / "tree"), None, 1).returncode == 0
    env = seen[0]
    assert env["SUBFLEET_ATTEMPT"] == "worktree-add:pinned-add"
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("user.name", "Synthetic Name")
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("core.fsmonitor", "false")
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-q", str(repo))
    git(repo, "config", "core.fsmonitor", "true")

    def read(name):
        return subprocess.run(["git", "-C", str(repo), "config", "--get", name], env=env,
                              capture_output=True, text=True, timeout=60).stdout.strip()
    assert (read("core.fsmonitor"), read("user.name")) == ("false", "Synthetic Name")


@pytest.mark.parametrize("count,index", [(None, 0), ("", 0), ("0", 0), ("2", 2), (" 2", 2), ("+2", 2), ("-0", 0),
                                         ("abc", None), ("2 ", None), ("-1", None), ("2147483648", None)])
def test_the_pin_follows_whatever_count_git_reads(tmp_path, count, index):
    """`pinned` appends where git will read it, and leaves a count git refuses (git then reads no
    config and runs nothing). Real git is the oracle for every count."""
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "LC_ALL": "C"}
    if count is not None:
        base.update({"GIT_CONFIG_COUNT": count, "GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "Synthetic",
                     "GIT_CONFIG_KEY_1": "user.name", "GIT_CONFIG_VALUE_1": "Synthetic"})
    env = worktree_add.pinned(base)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], env=base | {"GIT_CONFIG_COUNT": "0"}, check=True, timeout=60)
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", "true"], env=base | {"GIT_CONFIG_COUNT": "0"},
                   check=True, timeout=60)
    read = subprocess.run(["git", "-C", str(repo), "config", "--get", "core.fsmonitor"], env=env,
                          capture_output=True, text=True, timeout=60)
    if index is None:
        assert env == base and read.returncode == 128
    else:
        assert env["GIT_CONFIG_COUNT"] == str(index + 1)
        assert (env[f"GIT_CONFIG_KEY_{index}"], env[f"GIT_CONFIG_VALUE_{index}"]) == ("core.fsmonitor", "false")
        assert (read.returncode, read.stdout.strip()) == (0, "false")


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
