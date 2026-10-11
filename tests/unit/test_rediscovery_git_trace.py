"""C-8.4: an `unspelled` turn's folder lookup writes nothing in a fenced tree, even
with git's diagnostic traces set in the daemon's environment.

Review of #141 at 708d6557 (P3): `GIT_TRACE` or `GIT_TRACE2_EVENT`, inherited by the
daemon and naming a file inside the tree retention is retiring, made the one
`git rev-parse --show-toplevel` the look runs write that file there. Subfleet's git
helpers now drop every `GIT_TRACE*` variable (`salvage._git_env`). Adapted from the
reviewer's probe `probe_rediscovery_config.py`.
"""

import os
from pathlib import Path

import pytest

from subfleet import folders
from subfleet import salvage as salvage_module
from subfleet.cli import daemon_env
from tests.fake.test_admission_latency import fleet_daemon, measure
from tests.fake.test_admission_liveness import CODEX, _checkout, _live
from tests.unit.retention_world import snapshot
from tests.unit.test_retention_rediscovery import overtaken
from tests.unit.test_retention_shared_folders import ctimes, git_spawns, retiring_tree

TRACES = ["GIT_TRACE", "GIT_TRACE_SETUP", "GIT_TRACE2", "GIT_TRACE2_EVENT", "GIT_TRACE2_PERF"]


@pytest.mark.parametrize("name", TRACES)
def test_salvage_git_env_drops_every_trace_variable(monkeypatch, name):
    monkeypatch.setenv(name, "/tmp/should-not-be-written")
    assert name not in salvage_module._git_env(None)
    assert name not in salvage_module._git_env({**os.environ, "GIT_INDEX_FILE": "/t/index"})


@pytest.mark.parametrize("trace_variable", TRACES)
@pytest.mark.parametrize("writable", [False, True], ids=["reader", "writer"])
def test_ambient_trace_does_not_write_into_a_fenced_tree(tmp_path, writable, trace_variable):
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        tree, nested = retiring_tree(daemon, harness)
        turn = overtaken(daemon, harness, nested, tree, writable)
        fence = folders.exclusive_key(tree)
        assert daemon.store.acquire_lease(fence, "retention:review")
        trace = Path(tree) / "ambient-trace.txt"
        patch.setenv(trace_variable, str(trace))
        # The daemon's own environment still carries it: the guarantee comes from git's.
        assert daemon_env(daemon.root)[trace_variable] == str(trace)
        before = (snapshot(Path(tree)), ctimes(Path(tree)))
        real_workspace, prepared = daemon._workspace, []
        patch.setattr(daemon, "_workspace", lambda job: prepared.append(job["job_id"]) or real_workspace(job))
        spawned = git_spawns(patch)
        daemon._admit_turns()
        hold = daemon._holds.get(turn) or {}
        assert not _live(daemon, turn) and prepared == []
        assert hold.get("reason") == "lease-held" and hold.get("leases") == [fence], hold
        assert len(spawned) == 1 and spawned[0][0][spawned[0][0].index("-C") + 2:] == ["rev-parse", "--show-toplevel"]
        assert not trace.exists(), trace_variable
        assert (snapshot(Path(tree)), ctimes(Path(tree))) == before, trace_variable
