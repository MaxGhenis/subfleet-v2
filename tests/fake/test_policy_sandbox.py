"""d261, C-11.1: a submit that names no sandbox takes the policy's permissions for its task.

Before, `subfleet run` sent read-only whenever `-s` was missing, so `--task build`
jobs ran unable to write (5 in the week to 2026-09-24) while the policy already
said `build: workspace-write`.
"""

from pathlib import Path

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
    assert f"Your workspace is {worktree}:" in prepared
    assert f"is relative to {worktree / 'pkg'} here" in prepared
    assert f"never write under {workdir.resolve()}" in prepared or f"never write under {workdir}" in prepared
    assert "refs/subfleet-salvage/" in prepared and prepared.index("Your workspace") < prepared.index('{"scenario"')
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert job["worktree"] == str(worktree)
    assert daemon._launch_dir(job, "claude") == str(worktree / "pkg") and (worktree / "pkg" / "mod.py").exists()
    # Review B-1: Codex's workspace-write sandbox writes only under its working
    # directory, so a Codex job starts at the worktree's top, where it can reach all of it.
    assert daemon._launch_dir(job, "codex") == str(worktree)


def committed_package(daemon, harness):
    from tests.unit.test_salvage import git
    workdir = repository(daemon, harness)
    (workdir / "pkg").mkdir()
    (workdir / "pkg" / "mod.py").write_text("x = 1\n")
    git(workdir, "add", ".")
    git(workdir, "commit", "-m", "package")
    return workdir


def test_a_directory_the_commit_lacks_starts_the_job_at_the_top_and_says_so(state_daemon):
    """Review B-4: a job run from a directory HEAD does not hold (untracked, ignored)
    gets a worktree without it: it starts at the top, and its note says why rather
    than naming a directory that is not there."""
    import json
    daemon, harness = state_daemon
    workdir = committed_package(daemon, harness)
    scratch = workdir / "scratch"
    scratch.mkdir()
    (scratch / "notes.txt").write_text("not committed\n")
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox=protocol.POLICY_SANDBOX, task="build",
                                                           tier="standard", workdir=str(scratch)))["job_id"]
    jobdir = daemon.root / "jobs" / job_id
    worktree = daemon.root / "worktrees" / job_id
    assert json.loads((jobdir / "manifest.json").read_text())["workspace"] == {"worktree": str(worktree), "prefix": "."}
    prepared = (jobdir / "prompt.prepared.md").read_text()
    assert "/scratch, which commit" in prepared and "your workspace has no scratch" in prepared
    daemon._admit()
    job = daemon.store.get_job(job_id)
    assert daemon._launch_dir(job, "claude") == str(worktree) == daemon._launch_dir(job, "codex")


def test_no_preamble_drops_the_template_but_never_the_workspace_note(state_daemon):
    """Review B-3: `--no-preamble` means no write template (C-6.7); the note on where
    the job writes is not the template, and a resume of the job still has none."""
    import json
    from subfleet.daemon import WRITE_PREAMBLE
    daemon, harness = state_daemon
    workdir = committed_package(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", workdir=str(workdir / "pkg"),
                                                           no_preamble=True))["job_id"]
    jobdir = daemon.root / "jobs" / job_id
    prepared = (jobdir / "prompt.prepared.md").read_text()
    assert "Your workspace is" in prepared and WRITE_PREAMBLE.strip() not in prepared
    assert json.loads((jobdir / "manifest.json").read_text())["preamble"] is False


def test_a_resume_starts_where_its_source_started(state_daemon):
    """Review B-2: a Claude session made in `<worktree>/pkg` is found, and its
    transcript is looked for, from that directory, so its resume starts there too;
    a resume keeps its source's choice of preamble."""
    import json
    from tests.fake.test_state_contract import receipt_fixture
    daemon, harness = state_daemon
    workdir = committed_package(daemon, harness)
    source = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", workdir=str(workdir / "pkg"),
                                                           no_preamble=True))["job_id"]
    daemon._admit()
    attempt, = daemon.store.list_attempts(source)
    daemon._pending_launches.discard(attempt["attempt_id"])
    daemon.store.update_attempt(attempt["attempt_id"], native_session_id="source-session")
    adir = daemon.root / "jobs" / source / "a1"
    adir.mkdir()
    daemon._finalize(receipt_fixture(daemon, attempt, adir))
    worktree = daemon.store.get_job(source)["worktree"]
    assert daemon.store.get_job(source)["state"] == "succeeded" and worktree
    resumed = daemon.dispatch("submit", harness.submit_args(kind="resume", parent_job_id=source,
                                                            sandbox="workspace-write"))["job_id"]
    manifest = json.loads((daemon.root / "jobs" / resumed / "manifest.json").read_text())
    assert manifest["workspace"] == {"worktree": worktree, "prefix": "pkg"}
    assert "workspace" not in manifest["resume"] and manifest["preamble"] is False
    job = daemon.store.get_job(resumed)
    assert Path(worktree, "pkg").is_dir()        # the source's worktree is kept for its resume
    assert daemon._launch_dir(job, "claude") == str(Path(worktree) / "pkg")
    assert daemon._launch_dir(job, "codex") == job["workdir"]


def test_a_writers_refusal_says_the_policy_made_it_write_and_others_do_not(state_daemon, monkeypatch):
    """Review of d261, finding 5: a policy-made writer refused as a second instance
    of a session hears that its task writes by policy and that `-s read-only` runs it
    anyway; a refusal a read-only job would meet too (a cancelled parent) is not
    dressed up that way."""
    from subfleet import daemon as daemon_module
    from subfleet.procs import ProcessIdentity
    daemon, harness = state_daemon
    workdir = committed_package(daemon, harness)
    daemon.policy["caps"].update(max_in_flight_per_lane=8, max_active_attempts=8)
    table = {100: ProcessIdentity(100, "boot-1", "Sun Sep 20 12:00:00 2026"),
             200: ProcessIdentity(200, "boot-1", "Sun Sep 20 13:00:00 2026")}
    monkeypatch.setattr(daemon_module.procs, "identity", lambda pid: table.get(pid))
    daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", in_place=True, workdir=str(workdir),
                                                  caller_session="s-1", caller_pid=100))
    with pytest.raises(AdapterError) as refused:
        daemon.dispatch("submit", harness.submit_args(sandbox=protocol.POLICY_SANDBOX, task="build", tier="standard",
                                                      workdir=str(workdir), caller_session="s-1", caller_pid=200))
    assert "(build jobs write by policy)" in str(refused.value) and refused.value.code == 7
    assert refused.value.fix.endswith("; or pass -s read-only")
    parent = daemon.dispatch("submit", harness.submit_args(sandbox="read-only"))["job_id"]
    daemon.dispatch("kill", {"job_id": parent})
    with pytest.raises(AdapterError) as orphan:
        daemon.dispatch("submit", harness.submit_args(sandbox=protocol.POLICY_SANDBOX, task="build", tier="standard",
                                                      workdir=str(workdir), parent_job_id=parent))
    assert str(orphan.value) == "parent is cancelled" and "read-only" not in (orphan.value.fix or "")


def test_an_unborn_repository_is_named_as_one(state_daemon, tmp_path):
    """Review of d261, finding 6: a repository with no commit yet is not "not a git
    repository"; the refusal says which, and the way out."""
    from tests.unit.test_salvage import git
    daemon, harness = state_daemon
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    git(unborn, "init", "-b", "main")
    with pytest.raises(AdapterError) as refused:
        daemon.dispatch("submit", harness.submit_args(sandbox=protocol.POLICY_SANDBOX, task="build", tier="standard",
                                                      workdir=str(unborn)))
    assert "this repository has no commit yet" in str(refused.value) and "-s read-only" in refused.value.fix


def test_an_in_place_writable_job_gets_no_workspace_note(state_daemon):
    """--in-place writes where the caller stands (C-6.6): no worktree to name."""
    import json
    daemon, harness = state_daemon
    repository(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", in_place=True))["job_id"]
    jobdir = daemon.root / "jobs" / job_id
    assert "workspace" not in json.loads((jobdir / "manifest.json").read_text())
    assert "Your workspace is" not in (jobdir / "prompt.prepared.md").read_text()
