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



def test_a_resume_starts_where_its_source_started_under_a_state_root_typed_in_other_capitals(tmp_path, monkeypatch):
    """Review B-2 under a state root given in a case its volume does not store (review of
    c7595627, P3). The source's `jobs.worktree` is spelled from the root as given; the
    resume's workdir is recorded in its one spelling (`folders.canonical`). `_launch_dir`
    compared their `realpath`s, which keep case, so it started the resume at the
    worktree's top instead of `<worktree>/pkg`, where its Claude session was made. It
    compares the one spellings now. Failed with the `realpath` comparison put back."""
    import json
    from subfleet import daemon as daemon_module, folders
    from subfleet.adapters.registry import register
    from subfleet.daemon import Daemon
    from subfleet.procs import Containment
    from tests.fake.conftest import Harness
    from tests.fake.test_state_contract import receipt_fixture
    from tests.fake_adapter import FakeAdapter
    (tmp_path / "MixedCaseParent").mkdir()
    parent = tmp_path / "MIXEDCASEPARENT"
    if not parent.is_dir():
        pytest.skip("the volume tells case apart")
    state = parent / "state"
    state.mkdir()
    harness = Harness(state)
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "unit-test-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "unit-test-start")
    monkeypatch.setattr(daemon_module.procs, "same_process", lambda *args: False)
    monkeypatch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment())
    register("codex", FakeAdapter)
    daemon = Daemon(harness.root)

    def refuse_real_launch(*args):
        raise AssertionError("state-only fixtures must never launch a guardian")

    monkeypatch.setattr(daemon, "_launch", refuse_real_launch)
    try:
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
        assert "MIXEDCASEPARENT" in worktree        # the root's spelling, as given
        resumed = daemon.dispatch("submit", harness.submit_args(kind="resume", parent_job_id=source,
                                                                sandbox="workspace-write"))["job_id"]
        manifest = json.loads((daemon.root / "jobs" / resumed / "manifest.json").read_text())
        assert manifest["workspace"] == {"worktree": worktree, "prefix": "pkg"}
        job = daemon.store.get_job(resumed)
        assert job["workdir"] == folders.canonical(worktree) != worktree
        place = daemon._launch_dir(job, "claude")
        assert folders.canonical(place) == folders.canonical(Path(worktree) / "pkg"), place
        assert daemon._launch_dir(job, "codex") == job["workdir"]
    finally:
        daemon.close()

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


def test_the_callers_place_is_named_as_git_spells_it(state_daemon):
    """Review, 2026-09-25: a caller's directory named in another case, in decomposed
    Unicode or through the `/System/Volumes/Data` firmlink is the committed
    directory; the job starts there, and its note never names a place outside the
    worktree (`os.path.relpath` of the caller's spelling gave `PKG`, which a
    checkout does not hold, or `../..`)."""
    import json
    import sys
    import unicodedata
    from tests.unit.test_salvage import git
    if sys.platform != "darwin":
        pytest.skip("case-insensitive and firmlinked paths are macOS's")
    daemon, harness = state_daemon
    workdir = committed_package(daemon, harness)
    cafe = unicodedata.normalize("NFC", "café")
    (workdir / cafe).mkdir()
    (workdir / cafe / "a.txt").write_text("a\n")
    git(workdir, "add", ".")
    git(workdir, "commit", "-m", "accent")
    spellings = [(workdir / "PKG", "pkg"), (workdir / unicodedata.normalize("NFD", "café"), cafe)]
    data = Path("/System/Volumes/Data" + str((workdir / "pkg").resolve()))
    if data.is_dir():
        spellings.append((data, "pkg"))
    for spelled, committed in spellings:
        if not spelled.is_dir():
            continue                 # a case-sensitive volume: another directory
        job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", workdir=str(spelled)))["job_id"]
        jobdir = daemon.root / "jobs" / job_id
        worktree = daemon.root / "worktrees" / job_id
        assert json.loads((jobdir / "manifest.json").read_text())["workspace"] == {
            "worktree": str(worktree), "prefix": committed}, spelled
        prepared = (jobdir / "prompt.prepared.md").read_text()
        assert f"is relative to {worktree / committed} here" in prepared and ".." not in prepared.split("worktrees")[1][:80]
        daemon.dispatch("kill", {"job_id": job_id})


def test_a_place_git_cannot_check_is_named_with_its_fallback(state_daemon, monkeypatch):
    """When git cannot say whether the commit holds the caller's directory, the
    note says where the job starts if it does not (the top, `_launch_dir`)."""
    from subfleet import daemon as daemon_module
    daemon, harness = state_daemon
    workdir = committed_package(daemon, harness)
    monkeypatch.setattr(daemon_module, "_commit_holds_dir", lambda *args: None)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", workdir=str(workdir / "pkg")))["job_id"]
    prepared = (daemon.root / "jobs" / job_id / "prompt.prepared.md").read_text()
    worktree = daemon.root / "worktrees" / job_id
    assert f"does not hold it, you start at {worktree}; create it there" in prepared


def finished_source(daemon, harness, workdir):
    from tests.fake.test_state_contract import receipt_fixture
    source = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", workdir=str(workdir / "pkg")))["job_id"]
    daemon._admit()
    attempt, = daemon.store.list_attempts(source)
    daemon._pending_launches.discard(attempt["attempt_id"])
    daemon.store.update_attempt(attempt["attempt_id"], native_session_id="source-session")
    adir = daemon.root / "jobs" / source / "a1"
    adir.mkdir()
    daemon._finalize(receipt_fixture(daemon, attempt, adir))
    return source, adir, daemon.store.get_job(source)["worktree"]


@pytest.mark.parametrize("started", ["top", "pkg"])
def test_a_resume_starts_where_its_source_attempt_actually_started(state_daemon, started):
    """The resumed attempt's recorded launch `cwd` decides, not its manifest: a
    source that started at its worktree's top (a Codex attempt) is resumed at the
    top, one that started in `pkg` in `pkg`."""
    import json
    daemon, harness = state_daemon
    workdir = committed_package(daemon, harness)
    source, adir, worktree = finished_source(daemon, harness, workdir)
    cwd = worktree if started == "top" else str(Path(worktree) / "pkg")
    (adir / "launch.json").write_text(json.dumps({"argv": ["x"], "cwd": cwd}))
    resumed = daemon.dispatch("submit", harness.submit_args(kind="resume", parent_job_id=source,
                                                            sandbox="workspace-write"))["job_id"]
    manifest = json.loads((daemon.root / "jobs" / resumed / "manifest.json").read_text())
    job = daemon.store.get_job(resumed)
    if started == "top":
        assert "workspace" not in manifest and daemon._launch_dir(job, "claude") == worktree
    else:
        assert manifest["workspace"] == {"worktree": worktree, "prefix": "pkg"}
        assert daemon._launch_dir(job, "claude") == str(Path(worktree) / "pkg")


def test_a_resumes_start_stays_out_of_its_digest(state_daemon, monkeypatch):
    """C-6.2: where a resume starts is its source's, so it is not part of the
    request: a resume sent before an upgrade that began recording it and retried
    after is the same request."""
    from subfleet import daemon as daemon_module
    daemon, harness = state_daemon
    workdir = committed_package(daemon, harness)
    source, _, _ = finished_source(daemon, harness, workdir)
    seen = []
    digest = daemon_module.ids.payload_digest
    monkeypatch.setattr(daemon_module.ids, "payload_digest", lambda *a, **k: seen.append(k.get("resume")) or digest(*a, **k))
    args = harness.submit_args(kind="resume", parent_job_id=source, sandbox="workspace-write")
    first = daemon.dispatch("submit", args)["job_id"]
    assert seen[-1] and "workspace" not in seen[-1]
    again = daemon.dispatch("submit", args)
    assert again["job_id"] == first and again["created"] is False


def test_a_writer_only_conflict_says_the_policy_made_it_write(state_daemon, monkeypatch):
    """A policy-made writer refused by a conflict only writers meet (its checkout
    is held by another writer) hears why it writes and how to run it read-only;
    with no task named, "these" jobs (review B-6)."""
    daemon, harness = state_daemon
    workdir = committed_package(daemon, harness)
    daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", in_place=True, workdir=str(workdir)))
    monkeypatch.setattr(daemon, "_writable_precheck", lambda *args: frozenset())   # reach the conflict check
    with pytest.raises(AdapterError) as refused:
        daemon.dispatch("submit", harness.submit_args(sandbox=protocol.POLICY_SANDBOX, task="build", tier="standard",
                                                      in_place=True, workdir=str(workdir), caller_session="s-2"))
    assert "held by a live job" in str(refused.value) and "(build jobs write by policy)" in str(refused.value)
    assert refused.value.fix.endswith("; or pass -s read-only")
    daemon.policy["permissions"] = {"*": "workspace-write"}
    with pytest.raises(AdapterError) as untasked:
        daemon.dispatch("submit", harness.submit_args(sandbox=protocol.POLICY_SANDBOX, in_place=True, workdir=str(workdir),
                                                      caller_session="s-3"))
    assert "held by a live job" in str(untasked.value) and "(these jobs write by policy)" in str(untasked.value)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_a_launch_starts_codex_at_the_top_and_claude_in_the_callers_place(state_daemon, monkeypatch, provider):
    """Review B-1 at the launch itself, not `_launch_dir` alone: the attempt's
    recorded `cwd` is the worktree's top for a Codex lane (its sandbox writes only
    under its working directory) and `<worktree>/pkg` for a Claude lane. Git runs
    for real; only the guardian is not started."""
    import subprocess
    from dataclasses import replace
    from subfleet import daemon as module
    from subfleet.adapters.claude import ClaudeAdapter
    from subfleet.contracts import Credential
    daemon, harness = state_daemon
    workdir = committed_package(daemon, harness)
    monkeypatch.setattr(module.scheduler, "probe_required", lambda decision, job: False)
    monkeypatch.setattr(daemon, "_guard_override", lambda *args: "hooks={}")
    real = subprocess.Popen

    class Guardian:
        pid = 987654321

        def poll(self):
            return None

    def popen(command, *args, **kwargs):
        if any("subfleet.guardian" in str(part) for part in command):
            return Guardian()
        return real(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    model = "astra"
    if provider == "claude":
        lane = daemon.store.get_lane("codex-1")
        daemon.store.put_lane(replace(lane, lane_id="claude-1", provider="claude", account_key="claude:fixture",
                                      credential=Credential("claude", lane.credential.ref, "home")))
        monkeypatch.setattr(module, "get_adapter", lambda _: ClaudeAdapter())
        model = "haiku"
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", pinned_model=model,
                                                           workdir=str(workdir / "pkg")))["job_id"]
    daemon._admit()
    attempt, = daemon.store.list_attempts(job_id)
    module.Daemon._launch(daemon, attempt)          # the fixture refuses launches on the instance
    worktree = daemon.store.get_job(job_id)["worktree"]
    assert worktree and daemon.store.get_attempt(attempt["attempt_id"])["lane_id"] == ("codex-1" if provider == "codex" else "claude-1")
    want = worktree if provider == "codex" else str(Path(worktree) / "pkg")
    assert daemon._saved_launch(attempt).cwd == want


def test_a_committed_directory_named_with_leading_dots_is_inside_the_checkout(state_daemon):
    """Review of 5aa2718, finding 2: `..data` is a name, not the parent; the job
    starts there and its note names it (before, `startswith("..")` sent it to the top)."""
    import json
    from tests.unit.test_salvage import git
    daemon, harness = state_daemon
    workdir = committed_package(daemon, harness)
    (workdir / "..data").mkdir()
    (workdir / "..data" / "x.txt").write_text("x\n")
    git(workdir, "add", ".")
    git(workdir, "commit", "-m", "dots")
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write",
                                                           workdir=str(workdir / "..data")))["job_id"]
    jobdir = daemon.root / "jobs" / job_id
    worktree = daemon.root / "worktrees" / job_id
    assert json.loads((jobdir / "manifest.json").read_text())["workspace"]["prefix"] == "..data"
    assert f"is relative to {worktree / '..data'} here" in (jobdir / "prompt.prepared.md").read_text()
    daemon._admit()
    assert daemon._launch_dir(daemon.store.get_job(job_id), "claude") == str(worktree / "..data")


def test_a_place_outside_the_checkout_is_never_named(state_daemon, monkeypatch):
    """When git cannot give the prefix and the caller's path, compared as spelled,
    lies outside the top it found, the job starts at the top with no place note."""
    import json
    from subfleet import daemon as daemon_module
    daemon, harness = state_daemon
    workdir = committed_package(daemon, harness)
    monkeypatch.setattr(daemon_module, "_git_prefix", lambda *a: None)
    monkeypatch.setattr(daemon_module, "git_toplevel", lambda *a, **k: "/nonexistent/elsewhere")
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", workdir=str(workdir / "pkg")))["job_id"]
    jobdir = daemon.root / "jobs" / job_id
    assert json.loads((jobdir / "manifest.json").read_text())["workspace"]["prefix"] == "."
    assert "The caller ran this job from" not in (jobdir / "prompt.prepared.md").read_text()


def test_a_resume_whose_recorded_start_lies_outside_its_worktree_starts_at_the_top(state_daemon):
    import json
    daemon, harness = state_daemon
    workdir = committed_package(daemon, harness)
    source, adir, worktree = finished_source(daemon, harness, workdir)
    (adir / "launch.json").write_text(json.dumps({"argv": ["x"], "cwd": str(workdir)}))   # the caller's checkout
    resumed = daemon.dispatch("submit", harness.submit_args(kind="resume", parent_job_id=source,
                                                            sandbox="workspace-write"))["job_id"]
    assert "workspace" not in json.loads((daemon.root / "jobs" / resumed / "manifest.json").read_text())


def test_an_explicit_writer_refused_by_a_writer_conflict_is_not_told_it_writes_by_policy(state_daemon, monkeypatch):
    """Only a job the policy made a writer hears that; one that asked to write
    (`-s workspace-write`) gets the refusal as it is (review of 5aa2718, finding 8)."""
    daemon, harness = state_daemon
    workdir = committed_package(daemon, harness)
    daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", in_place=True, workdir=str(workdir)))
    monkeypatch.setattr(daemon, "_writable_precheck", lambda *args: frozenset())
    with pytest.raises(AdapterError) as refused:
        daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", task="build", tier="standard",
                                                      in_place=True, workdir=str(workdir), caller_session="s-4"))
    assert "held by a live job" in str(refused.value) and "write by policy" not in str(refused.value)
