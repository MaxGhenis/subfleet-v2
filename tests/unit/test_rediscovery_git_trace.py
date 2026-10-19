"""C-8.4: an `unspelled` turn's folder lookup writes nothing in a fenced tree, even
with git's diagnostic traces set in the daemon's environment.

Review of #141 at 708d6557 (P3): `GIT_TRACE` or `GIT_TRACE2_EVENT`, inherited by the
daemon and naming a file inside the tree retention is retiring, made the one
`git rev-parse --show-toplevel` the look runs write that file there. Subfleet's git
helpers now drop every `GIT_TRACE*` variable and switch Trace2's three targets off
(`salvage._git_env`), so a `trace2.*Target` in the global config writes nothing either
(review at c20ee3d0, P3). Adapted from the reviewer's probe `probe_rediscovery_config.py`.
"""

import json
import os
from pathlib import Path

import pytest

from subfleet import folders
from subfleet import salvage as salvage_module
from subfleet.cli import daemon_env
from subfleet.launch_env import helper_policy
from tests.fake.test_admission_latency import fleet_daemon, measure
from tests.fake.test_admission_liveness import CODEX, _checkout, _live
from tests.unit.retention_world import snapshot
from tests.unit.test_retention_rediscovery import overtaken
from tests.unit.test_retention_shared_folders import ctimes, git_spawns, retiring_tree

TRACES = ["GIT_TRACE", "GIT_TRACE_SETUP", "GIT_TRACE2", "GIT_TRACE2_EVENT", "GIT_TRACE2_PERF"]


def authorize_environment(daemon, *names):
    """Exercise Git's mandatory controls even with exact policy authority."""
    path = daemon.root / "policy.json"
    policy = json.loads(path.read_text())
    policy["env"] = daemon.policy["env"] = {"pass": list(names)}
    path.write_text(json.dumps(policy))


@pytest.mark.parametrize("name", TRACES)
def test_salvage_git_env_drops_every_trace_variable(monkeypatch, name):
    monkeypatch.setenv(name, "/tmp/should-not-be-written")
    for env in (salvage_module._git_env(None),
                salvage_module._git_env({**os.environ, "GIT_INDEX_FILE": "/t/index"})):
        # Trace2's three targets are switched off ("0"), the rest are absent.
        assert env.get(name) == ("0" if name in salvage_module.GIT_TRACE2_TARGETS else None)


@pytest.mark.parametrize("writable", [False, True], ids=["reader", "writer"])
def test_configured_trace2_target_does_not_write_into_a_fenced_tree(tmp_path, writable):
    """Review of #141 at c20ee3d0 (P3): with an inherited `GIT_TRACE2_EVENT=0` dropped,
    a `trace2.*Target` in the global config made the lookup write inside the tree."""
    with fleet_daemon(tmp_path / "state") as (daemon, harness, patch):
        _checkout(harness)
        for lane in CODEX:
            measure(daemon, lane)
        tree, nested = retiring_tree(daemon, harness)
        turn = overtaken(daemon, harness, nested, tree, writable)
        fence = folders.exclusive_key(tree)
        assert daemon.store.acquire_lease(fence, "retention:review")
        targets = {key: Path(tree) / f"configured-{key}.trace" for key in ("normalTarget", "eventTarget", "perfTarget")}
        config = tmp_path / "global.gitconfig"
        config.write_text("[trace2]\n" + "".join(f"\t{key} = {path}\n" for key, path in targets.items()))
        patch.setenv("GIT_CONFIG_GLOBAL", str(config))
        patch.setenv("GIT_TRACE2_EVENT", "0")           # inherited, and dropped with every GIT_TRACE*
        authorize_environment(daemon, "GIT_CONFIG_GLOBAL", "GIT_TRACE2_EVENT")
        assert daemon_env(daemon.root)["GIT_CONFIG_GLOBAL"] == str(config)
        before = (snapshot(Path(tree)), ctimes(Path(tree)))
        real_workspace, prepared = daemon._workspace, []
        patch.setattr(daemon, "_workspace", lambda job: prepared.append(job["job_id"]) or real_workspace(job))
        spawned = git_spawns(patch)
        with helper_policy(daemon.policy):
            daemon._admit_turns()
        hold = daemon._holds.get(turn) or {}
        assert not _live(daemon, turn) and prepared == []
        assert hold.get("reason") == "lease-held" and hold.get("leases") == [fence], hold
        assert len(spawned) == 1 and spawned[0][0][spawned[0][0].index("-C") + 2:] == ["rev-parse", "--show-toplevel"]
        assert not any(path.exists() for path in targets.values()), targets
        assert (snapshot(Path(tree)), ctimes(Path(tree))) == before


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
        authorize_environment(daemon, trace_variable)
        # Policy carries it into the daemon; Git still has to remove it.
        assert daemon_env(daemon.root)[trace_variable] == str(trace)
        before = (snapshot(Path(tree)), ctimes(Path(tree)))
        real_workspace, prepared = daemon._workspace, []
        patch.setattr(daemon, "_workspace", lambda job: prepared.append(job["job_id"]) or real_workspace(job))
        spawned = git_spawns(patch)
        with helper_policy(daemon.policy):
            daemon._admit_turns()
        hold = daemon._holds.get(turn) or {}
        assert not _live(daemon, turn) and prepared == []
        assert hold.get("reason") == "lease-held" and hold.get("leases") == [fence], hold
        assert len(spawned) == 1 and spawned[0][0][spawned[0][0].index("-C") + 2:] == ["rev-parse", "--show-toplevel"]
        assert not trace.exists(), trace_variable
        assert (snapshot(Path(tree)), ctimes(Path(tree))) == before, trace_variable
