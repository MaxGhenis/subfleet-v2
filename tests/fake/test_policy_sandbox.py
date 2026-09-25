"""d261, C-11.1: a submit that names no sandbox takes the policy's permissions for its task.

Before, `subfleet run` sent read-only whenever `-s` was missing, so `--task build`
jobs ran unable to write (5 in the week to 2026-09-24) while the policy already
said `build: workspace-write`.
"""

import pytest

from subfleet import protocol
from subfleet.adapters.base import AdapterError
from tests.fake.test_state_contract import state_daemon  # noqa: F401  (fixture)
from tests.fake.test_workspace_contract import repository


def sandbox_of(daemon, job_id):
    return daemon.store.get_job(job_id)["sandbox"]


def test_d261_the_task_names_the_sandbox_and_an_explicit_one_wins(state_daemon):
    daemon, harness = state_daemon
    repository(daemon, harness)
    assert daemon.policy["permissions"] == {"build": "workspace-write", "*": "read-only"}
    policy = protocol.POLICY_SANDBOX
    build = daemon.dispatch("submit", harness.submit_args(sandbox=policy, task="build", tier="standard"))["job_id"]
    research = daemon.dispatch("submit", harness.submit_args(sandbox=policy, task="research", tier="standard"))["job_id"]
    untasked = daemon.dispatch("submit", harness.submit_args(sandbox=policy))["job_id"]
    pinned = daemon.dispatch("submit", harness.submit_args(sandbox="read-only", task="build", tier="standard"))["job_id"]
    assert [sandbox_of(daemon, j) for j in (build, research, untasked, pinned)] == [
        "workspace-write", "read-only", "read-only", "read-only"]


def test_d261_a_policy_sandbox_is_the_same_request_as_naming_it(state_daemon):
    """C-6.2: the digest carries the resolved sandbox, so a retry that names it
    is answered from the same job."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    args = harness.submit_args(sandbox=protocol.POLICY_SANDBOX, task="build", tier="standard")
    first = daemon.dispatch("submit", args)["job_id"]
    again = daemon.dispatch("submit", {**args, "sandbox": "workspace-write"})["job_id"]
    assert again == first and len(daemon.store.list_jobs()) == 1


def test_d261_a_build_job_outside_a_repository_is_refused_with_the_way_out(state_daemon):
    """Never downgraded silently: the caller hears why and how to run it read-only."""
    daemon, harness = state_daemon
    with pytest.raises(AdapterError) as error:
        daemon.dispatch("submit", harness.submit_args(sandbox=protocol.POLICY_SANDBOX, task="build", tier="standard"))
    assert "not a git repository" in str(error.value) and "-s read-only" in (error.value.fix or "")
    assert daemon.store.list_jobs() == []
    ok = daemon.dispatch("submit", harness.submit_args(sandbox="read-only", task="build", tier="standard"))["job_id"]
    assert sandbox_of(daemon, ok) == "read-only"


def test_d261_isolated_reviews_and_gate_rounds_read_only_whatever_the_policy(state_daemon):
    daemon, harness = state_daemon
    daemon.policy["permissions"] = {"*": "workspace-write"}
    base = protocol.SubmitArgs(request_id="r", kind="dispatch", workdir="/x", prompt_path="/p",
                               sandbox=protocol.POLICY_SANDBOX, task="review")
    assert daemon._policy_sandbox(base) == "workspace-write"
    assert daemon._policy_sandbox(protocol.SubmitArgs(**{**base.__dict__, "isolated_review": True})) == "read-only"
    assert daemon._policy_sandbox(protocol.SubmitArgs(**{**base.__dict__, "kind": "gate-review"})) == "read-only"
    daemon.policy["permissions"] = {"*": "danger-full-access"}                # not a sandbox Subfleet runs
    assert daemon._policy_sandbox(base) == "read-only"


def test_d260_the_launch_spec_carries_the_policy_network_grant(state_daemon):
    """The job row never stores it: a policy change reaches the next attempt."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write"))["job_id"]
    job = daemon.store.get_job(job_id)
    assert daemon.policy["network"] == {"codex_workspace_write": True}
    assert daemon._spec(job).network is True
    daemon.policy["network"] = {"codex_workspace_write": False}
    assert daemon._spec(job).network is False
    del daemon.policy["network"]
    assert daemon._spec(job).network is False


def test_a_writable_job_is_told_its_workspace_and_starts_where_its_caller_stands(state_daemon):
    """Review of d261: a writable job that is not in place works in a worktree cut
    at admission. Its prompt names that worktree, says what it lacks and that the
    caller's checkout is not to be touched, and where the result is kept; a job
    submitted from a directory inside the repository starts in the same place
    inside the worktree (C-6.6, C-13.1)."""
    import json
    from tests.unit.test_salvage import git
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    sub = workdir / "pkg"
    sub.mkdir()
    (sub / "mod.py").write_text("x = 1\n")
    git(workdir, "add", ".")
    git(workdir, "commit", "-m", "package")
    submitted = daemon.dispatch("submit", harness.submit_args(sandbox=protocol.POLICY_SANDBOX, task="build",
                                                              tier="standard", workdir=str(sub)))
    job_id = submitted["job_id"]
    jobdir = daemon.root / "jobs" / job_id
    assert (submitted["sandbox"], submitted["worktree"]) == ("workspace-write", str(daemon.root / "worktrees" / job_id))
    manifest = json.loads((jobdir / "manifest.json").read_text())
    worktree = daemon.root / "worktrees" / job_id
    assert manifest["workspace"] == {"worktree": str(worktree), "prefix": "pkg"}
    prepared = (jobdir / "prompt.prepared.md").read_text()
    assert f"Your workspace is {worktree / 'pkg'}" in prepared
    assert f"never write under {workdir.resolve()}" in prepared or f"never write under {workdir}" in prepared
    assert "refs/subfleet-salvage/" in prepared and prepared.index("Your workspace") < prepared.index('{"scenario"')
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert job["worktree"] == str(worktree)
    assert daemon._launch_dir(job) == str(worktree / "pkg") and (worktree / "pkg" / "mod.py").exists()


def test_an_in_place_writable_job_gets_no_workspace_note(state_daemon):
    """--in-place writes where the caller stands (C-6.6): no worktree to name."""
    import json
    daemon, harness = state_daemon
    repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", in_place=True))["job_id"]
    jobdir = daemon.root / "jobs" / job_id
    assert "workspace" not in json.loads((jobdir / "manifest.json").read_text())
    assert "Your workspace is" not in (jobdir / "prompt.prepared.md").read_text()
