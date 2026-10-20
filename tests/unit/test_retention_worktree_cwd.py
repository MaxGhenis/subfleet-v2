"""D-KILL3, C-13.4: only job-owned worktrees are held by an observer's cwd.

The kernel pid listing is restricted to our scratch process. Cwd itself is read
through the real in-process libproc call, never through lsof or a live job.
"""

from contextlib import contextmanager
import os
import json
from pathlib import Path
import subprocess
import sys

import pytest

from subfleet import procs, retention
from tests.unit.test_retention_worktrees import owned  # noqa: F401
from tests.unit.test_daemon_settle import daemon  # noqa: F401

pytestmark = [pytest.mark.skipif(sys.platform != "darwin", reason="macOS in-process cwd read"),
              pytest.mark.real_cwd]


@contextmanager
def observer(folder):
    ready = folder / "observer.ready"
    child = subprocess.Popen([sys.executable, "-c",
                              "import pathlib,time; pathlib.Path('observer.ready').touch(); time.sleep(120)"],
                             cwd=folder, start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import time
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert child.poll() is None and time.monotonic() < deadline
            time.sleep(.01)
        yield child
    finally:
        child.kill()
        child.wait(timeout=10)
        ready.unlink(missing_ok=True)


@pytest.mark.parametrize("spelling", ["resolved", "tmp-alias"])
def test_d_kill3_retention_holds_an_owned_worktree_and_names_only_the_pid(owned, monkeypatch, spelling):
    store, root, _, worktree = owned
    if spelling == "tmp-alias":
        # tmp_path may live in DARWIN_USER_TEMP_DIR: create an explicit alias
        # of its tree below /tmp, so resolution still crosses /private/tmp.
        alias = Path("/tmp") / ("sf-kill3-alias-" + str(os.getpid()))
        alias.symlink_to(root, target_is_directory=True)
        recorded = alias / "worktrees" / "job"
    else:
        alias, recorded = None, worktree
    try:
        store.update_job("job", worktree=str(recorded))
        nested = worktree / "nested"
        nested.mkdir()
        with observer(nested) as child:
            monkeypatch.setattr(procs.kernel, "pids", lambda: frozenset({child.pid}))
            result = retention.maintenance(store, root, max_jobs=0, holders=lambda *a, **k: {})
            assert result["pruned"] == []
            assert worktree.exists() and store.get_job("job") is not None
            reason = result["deferred"]["job"]
            assert f"pid {child.pid}" in reason and "cwd in worktree" in reason
            assert sys.executable not in reason and "time.sleep" not in reason
        result = retention.maintenance(store, root, max_jobs=0, holders=lambda *a, **k: {})
        assert result["pruned"] == ["job"]
    finally:
        if alias is not None:
            alias.unlink(missing_ok=True)


def test_d_kill3_retention_does_not_hold_an_in_place_workdir_for_cwd(owned, monkeypatch):
    store, root, repository, _ = owned
    store.update_job("job", in_place=1, worktree=str(repository))
    with observer(repository) as child:
        monkeypatch.setattr(procs.kernel, "pids", lambda: frozenset({child.pid}))
        result = retention.maintenance(store, root, max_jobs=0, holders=lambda *a, **k: {})
        assert result["pruned"] == ["job"]
        assert repository.exists() and child.poll() is None


@pytest.mark.parametrize("in_place", [False, True], ids=["owned", "in-place"])
def test_d_kill3_cwd_holds_only_owned_attempts_and_never_becomes_a_walk_seed(daemon, monkeypatch, in_place):
    from tests.unit.test_daemon_settle import JOB, attempt
    worktree = daemon.root / "worktrees" / JOB
    worktree.mkdir(parents=True)
    daemon.store.update_job(JOB, sandbox="workspace-write", worktree=str(worktree), in_place=in_place)
    monkeypatch.setattr(procs, "containment", lambda *a, **k: procs.Containment())
    with observer(worktree) as child:
        monkeypatch.setattr(procs.kernel, "pids", lambda: frozenset({child.pid}))
        census = daemon._contain(attempt(daemon))
        assert census.verified_empty is in_place
        if not in_place:
            assert census.to_dict()["cwd_holders"] == [f"pid {child.pid}: cwd in worktree"]
        evidence = json.loads(attempt(daemon)["evidence_json"])
        assert not evidence.get("owned_identities") and not evidence.get("counted_identities")
        assert child.poll() is None
    assert daemon._contain(attempt(daemon)).verified_empty
