"""C-8.5 invariants through real submit, acceptance and offline Git pushes.

No daemon thread, provider, live state root, or network remote is used.
"""
from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from uuid import uuid4

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import cli, daemon as daemon_module, host_push, protocol
from subfleet.adapters.base import AdapterError
from subfleet.contracts import (Attestation, AttestationResult, Credential, Lane,
                               LaneOwner, Launch, Outcome, OutcomeClass, attempt_dir)
from subfleet.daemon import Daemon
from subfleet.ids import payload_digest
from subfleet.offline import Offline
from subfleet.policy import DEFAULT_POLICY_PATH, PUSH_DEFAULTS, PolicyError, load_policy
from subfleet.procs import Containment
from subfleet.store import Store


def git(path, *args, check=True, raw=False):
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.test",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.test"}
    for key in list(env):
        if key.startswith("GIT_") and key not in {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
                                                    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                                                    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"}:
            del env[key]
    result = subprocess.run([host_push.GIT, "-C", str(path), "-c", "core.hooksPath=" + os.devnull,
                             "-c", "core.fsmonitor=false", *args], env=env,
                            capture_output=True, timeout=60)
    if check:
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout if raw else result.stdout.decode().strip()


def commit(repo, path="change.txt", text="change"):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git(repo, "add", "--", path)
    git(repo, "commit", "-m", "test change")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def worlds(tmp_path, monkeypatch):
    home = tmp_path / "host-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "test-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "test-start")
    adapter = SimpleNamespace(
        classify=lambda *args: Outcome(OutcomeClass.OK, "finished"),
        attest=lambda *args: AttestationResult(Attestation.ATTESTED, "test-model", "test"),
        deliverable=lambda *args: b"Finished and tested.\n")
    monkeypatch.setattr(daemon_module, "get_adapter", lambda provider: adapter)

    @contextmanager
    def make(**overrides):
        with tempfile.TemporaryDirectory(prefix="case-", dir=tmp_path) as directory:
            base = Path(directory)
            repo, origin, root = base / "work", base / "origin.git", base / "state"
            repo.mkdir()
            git(repo, "init", "-b", "feature/source", "--template=")
            head = commit(repo, "baseline.txt", "baseline")
            git(base, "init", "--bare", "--initial-branch=trunk", "--template=", str(origin))
            remote = origin.as_uri()
            git(repo, "remote", "add", "origin", remote)
            git(repo, "push", "origin", f"{head}:refs/heads/trunk")
            policy = json.loads(DEFAULT_POLICY_PATH.read_text())
            policy["push"] = {**PUSH_DEFAULTS, "enabled": True, "allowed_remotes": [remote], **overrides}
            root.mkdir()
            (root / "policy.json").write_text(json.dumps(policy))
            core = Daemon(root, desktop_prober=lambda: None)
            core.store.put_lane(Lane("test-lane", "codex", "codex:test", Credential("codex", str(home), "home"),
                                     str(home), LaneOwner.V2, False))
            core._contain = lambda attempt: Containment()
            core._saved_launch = lambda attempt: Launch((), {}, (), str(repo), str(root / "prompt"),
                str(attempt_dir(root, attempt["job_id"], attempt["seq"]) / "stdout"),
                str(attempt_dir(root, attempt["job_id"], attempt["seq"]) / "stderr"), None, None)
            prompt = base / "prompt.md"
            prompt.write_text("Do the work.")
            world = SimpleNamespace(repo=repo, origin=origin, remote=remote, core=core, base=head, prompt=prompt,
                                    calls=[])
            actual_git = host_push.git

            def watch(quarantine_repo, *args, **kwargs):
                assert not core.store.connection.in_transaction, "Git ran inside a store transaction"
                assert core.root / "push-quarantine" in quarantine_repo.parents, "job-controlled Git directory"
                if args[0] in {"push", "ls-remote"}:
                    url = args[1] if args[0] == "push" else args[2]
                    assert url.startswith("file://"), "test attempted a network remote"
                if args[0] == "push":
                    assert len(args) == 3 and args[1] == remote
                    assert not args[2].startswith("+")
                    assert host_push.SHA.fullmatch(args[2].split(":")[0]), "delete or symbolic source"
                    assert args[2].split(":")[1].startswith("refs/heads/")
                    assert core._job(world.job_id)["state"] == "succeeded", "push preceded acceptance"
                world.calls.append((quarantine_repo, args))
                return actual_git(quarantine_repo, *args, **kwargs)

            with monkeypatch.context() as patch:
                world.patch = patch
                patch.setattr(host_push, "git", watch)
                try:
                    yield world
                finally:
                    core.close()
    return make


def submit(world, branch="jobs/finished", **extra):
    args = protocol.SubmitArgs(request_id=str(uuid4()), kind="dispatch", workdir=str(world.repo),
        prompt_path=str(world.prompt), sandbox="workspace-write", task="build", pinned_model="astra", push_branch=branch,
        allow_tmp=True, **extra)
    response = world.core.dispatch("submit", asdict(args))
    world.job_id = response["job_id"]
    return world.job_id


def accept(world, job=None, bundle=True, include_base=False):
    job = job or world.job_id
    core = world.core
    attempt_id = job + "/a1"
    directory = attempt_dir(core.root, job, 1)
    directory.mkdir()
    if bundle:
        if include_base:
            # One advertised HEAD, but include the old base as a dangling
            # object. This makes ancestry enforcement independently necessary;
            # absence of the base object cannot accidentally catch the mutant.
            git(world.repo, "update-ref", "refs/keep/base", world.base)
            tip = git(world.repo, "rev-parse", "HEAD")
            pack = git(world.repo, "pack-objects", "--stdout", "--all", raw=True)
            (directory / "push.bundle").write_bytes(f"# v2 git bundle\n{tip} HEAD\n\n".encode() + pack)
        else:
            git(world.repo, "bundle", "create", str(directory / "push.bundle"), "HEAD")
    (directory / "exit.json").write_text(json.dumps({"rc": 0, "signal": None, "wall_s": 1}))
    core.store.add_attempt(attempt_id=attempt_id, job_id=job, seq=1, lane_id="test-lane",
                           model_requested="astra", state="running", evidence_json="{}")
    with core.store.transaction("test.running") as tx:
        tx.execute("UPDATE jobs SET state='running' WHERE job_id=?", (job,))
    core._finalize(core.store.get_attempt(attempt_id))
    row = core._job(job)
    assert row["state"] == "succeeded" and row["accepted_attempt_id"] == attempt_id and row["rc"] == 0
    return row


def remote_heads(world):
    return dict(line.split(" ", 1)[::-1] for line in git(world.origin, "show-ref", "--heads").splitlines())


def assert_failed(world, row, phrase):
    assert row["push_error"] and phrase in row["push_error"]
    assert not row["push_sha"]
    assert remote_heads(world) == {"refs/heads/trunk": world.base}
    pushes = world.core.dispatch("show", {"job_id": world.job_id})["pushes"]
    assert len(pushes) == 1 and pushes[0]["result"] == "failed"
    assert "human or hub push" in world.core.store.list_notices()[0]["text"]


PROPERTY = settings(max_examples=6, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


@PROPERTY
@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=15))
def test_new_branch_push_is_non_forcing_non_deleting_and_reexport_is_idempotent(worlds, suffix):
    with worlds() as world:
        branch = "jobs/" + suffix
        submit(world, branch)
        sha = commit(world.repo, text=suffix)
        row = accept(world)
        assert row["push_sha"] == sha and not row["push_error"]
        assert remote_heads(world) == {"refs/heads/trunk": world.base, "refs/heads/" + branch: sha}
        for _ in range(3):
            world.core._export(world.job_id)
        assert len([args for _, args in world.calls if args[0] == "push"]) == 1
        assert len(world.core.store.query("SELECT * FROM job_pushes")) == 1
        assert world.core.store.query("SELECT * FROM job_owned_branches")[0]["sha"] == sha
        assert "[succeeded]" in cli._format_job(world.core.dispatch("show", {"job_id": world.job_id}))
        assert Offline(world.core.root).show_job(world.job_id)["pushes"][0]["sha"] == sha


@PROPERTY
@given(st.sampled_from(["main", "master", "release/217", "trunk", "private/secret"]))
def test_never_submits_or_pushes_a_protected_branch(worlds, branch):
    with worlds(protected=["private/*"]) as world:
        with pytest.raises(AdapterError) as refused:
            submit(world, branch)
        assert refused.value.code == 7 and refused.value.fix and "protected" in str(refused.value)
        assert not world.core.store.query("SELECT * FROM jobs")
        assert not any(args[0] == "push" for _, args in world.calls)
        assert remote_heads(world) == {"refs/heads/trunk": world.base}


@PROPERTY
@given(st.sampled_from(["orphan", "rewrite"]))
def test_never_pushes_a_tip_not_descending_from_recorded_base(worlds, kind):
    with worlds() as world:
        submit(world)
        if kind == "orphan":
            git(world.repo, "checkout", "--orphan", "unrelated")
            git(world.repo, "rm", "-rf", ".")
            commit(world.repo)
        else:
            git(world.repo, "commit", "--amend", "-m", "rewritten baseline")
        assert_failed(world, accept(world, include_base=True), "does not descend")


@PROPERTY
@given(st.sampled_from([".github/workflows/ci.yml", ".github/settings.yml", ".github/deep/file"]), st.booleans())
def test_never_pushes_github_changes_including_reverted_intermediate_commits(worlds, path, revert):
    with worlds() as world:
        submit(world)
        commit(world.repo, path, "secret-using CI")
        if revert:
            git(world.repo, "revert", "--no-edit", "HEAD")
        assert_failed(world, accept(world), ".github/")


@PROPERTY
@given(st.sampled_from(["core.fsmonitor", "core.sshCommand", "credential.helper", "includeIf", "core.hooksPath"]))
def test_never_executes_job_controlled_git_config_or_hooks(worlds, setting):
    with worlds() as world:
        sentinel = world.repo.parent / "sentinel"
        script = world.repo.parent / "malicious"
        script.write_text(f'#!/bin/sh\nprintf executed > "{sentinel}"\n')
        script.chmod(0o755)
        hooks = world.repo / ".git-local/hooks"
        hooks.mkdir(parents=True)
        for name in ("post-checkout", "pre-push", "post-commit"):
            shutil.copyfile(script, hooks / name)
            (hooks / name).chmod(0o755)
        malicious = (f'[core]\nfsmonitor = {script}\nsshCommand = {script}\nhooksPath = {hooks}\n'
                     f'[credential]\nhelper = !{script}\n')
        include = world.repo.parent / "included-config"
        include.write_text(malicious)
        malicious += f'[includeIf "gitdir:{world.repo}/"]\npath = {include}\n'
        (world.repo / ".git-local/config").write_text(malicious)
        with (world.repo / ".git/config").open("a") as config:
            config.write(malicious + "[extensions]\nworktreeConfig = true\n")
        (world.repo / ".git/config.worktree").write_text(malicious)
        world.patch.setenv("GIT_CONFIG_COUNT", "1")
        world.patch.setenv("GIT_CONFIG_KEY_0", "core.sshCommand")
        world.patch.setenv("GIT_CONFIG_VALUE_0", str(script))
        world.patch.setenv("GIT_CONFIG_GLOBAL", str(include))
        world.patch.setenv("GIT_SSH_COMMAND", str(script))
        # All five payloads are planted on every example; the drawn name makes
        # each attack explicit in Hypothesis's counterexample descriptions.
        assert setting in {"core.fsmonitor", "core.sshCommand", "credential.helper", "includeIf", "core.hooksPath"}
        submit(world)
        workspace, _, _, _ = world.core._workspace(world.core._job(world.job_id))
        assert (Path(workspace) / "baseline.txt").read_text() == "baseline"
        assigned = Path(workspace)
        (assigned / ".git-local").mkdir()
        (assigned / ".git-local/config").write_text(malicious)
        with (assigned / ".git/config").open("a") as config:
            config.write(malicious + "[extensions]\nworktreeConfig = true\n")
        (assigned / ".git/config.worktree").write_text(malicious)
        host_push.validate_write_location(assigned)
        with world.core.store.transaction("test.workspace") as tx:
            tx.execute("UPDATE jobs SET worktree=? WHERE job_id=?", (workspace, world.job_id))
        commit(world.repo)
        row = accept(world)
        assert row["push_sha"] and not row["push_error"]
        assert not sentinel.exists()


@pytest.mark.parametrize("branch", ["", "../evil", "/evil", "evil/", "evil.lock", "a//b", "a/.b", "a/b.lock/c", "a@{b", "+branch", "-branch", "x" * 201])
def test_invalid_branch_refused_at_real_submit_with_exit_seven_and_fix(worlds, branch):
    with worlds() as world:
        with pytest.raises(AdapterError) as error:
            submit(world, branch)
        assert error.value.code == 7 and error.value.fix


@pytest.mark.parametrize("policy", [{"enabled": False}, {"allowed_remotes": []}])
def test_disabled_and_disallowed_origin_refused_at_submit(worlds, policy):
    with worlds(**policy) as world:
        with pytest.raises(AdapterError) as error:
            submit(world)
        assert error.value.code == 7 and error.value.fix
        assert not world.core.store.query("SELECT * FROM jobs")


def test_submit_pins_host_origin_and_policy_rechecks_before_push(worlds):
    with worlds() as world:
        submit(world)
        git(world.repo, "remote", "set-url", "origin", "ext::malicious")
        commit(world.repo)
        assert accept(world)["push_sha"]
    with worlds() as world:
        submit(world)
        world.core.policy["push"]["protected"] = ["jobs/*"]
        assert_failed(world, accept(world), "protected")


def test_push_is_independent_of_a_reassigned_output_lease(worlds):
    with worlds() as world:
        out = world.prompt.parent / "output.md"
        out.write_text("a newer owner's output")
        submit(world, out_path=str(out))
        sha = commit(world.repo)
        assert accept(world)["push_sha"] == sha
        world.core._export(world.job_id)
        assert out.read_text() == "a newer owner's output"
        assert len(world.core.store.query("SELECT * FROM job_pushes")) == 1


def test_acceptance_crash_leaves_a_recoverable_push_lease(worlds):
    with worlds() as world:
        submit(world)
        sha = commit(world.repo)
        def crash(boundary, *args):
            if boundary == "terminal":
                raise RuntimeError("simulated crash after acceptance")
        world.core._boundary = crash
        with pytest.raises(RuntimeError, match="simulated crash"):
            accept(world)
        assert world.core._job(world.job_id)["state"] == "succeeded"
        assert world.core._pending_exports() == [world.job_id]
        assert world.core.store.query("SELECT * FROM job_pushes") == []
        world.core._boundary = lambda *args: None
        world.core._export(world.job_id)
        assert world.core._job(world.job_id)["push_sha"] == sha
        assert world.core._pending_exports() == []


@PROPERTY
@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=12))
def test_failed_git_push_keeps_acceptance_for_any_declared_child_branch(worlds, suffix):
    with worlds() as world:
        git(world.repo, "push", "origin", f"{world.base}:refs/heads/jobs")
        submit(world, "jobs/" + suffix)
        commit(world.repo, text=suffix)
        row = accept(world)
        assert row["push_error"] and not row["push_sha"]
        assert remote_heads(world) == {"refs/heads/trunk": world.base, "refs/heads/jobs": world.base}
        world.core._export(world.job_id)
        assert len(world.core.store.query("SELECT * FROM job_pushes")) == 1


@pytest.mark.parametrize("limit", ["max_commits", "max_bundle_mb"])
def test_bundle_size_and_commit_count_are_bounded(worlds, limit):
    with worlds(**{limit: 1}) as world:
        submit(world)
        if limit == "max_commits":
            commit(world.repo, text="one")
            commit(world.repo, text="two")
        else:
            (world.repo / "large").write_bytes(os.urandom(1024 * 1024 + 16384))
            git(world.repo, "add", "large")
            git(world.repo, "commit", "-m", "too large")
        assert_failed(world, accept(world), limit)


@pytest.mark.parametrize("target", ["../outside", "/etc/passwd", "dir/b/../.."])
def test_escaping_symlinks_including_chains_are_refused(worlds, target):
    with worlds() as world:
        submit(world)
        (world.repo / "link").symlink_to(target)
        if target == "dir/b/../..":
            (world.repo / "dir").mkdir()
            (world.repo / "dir/b").symlink_to("..")
        git(world.repo, "add", ".")
        git(world.repo, "commit", "-m", "escaping link")
        assert_failed(world, accept(world), "symlink")


def test_internal_symlink_allowed_and_gitlink_refused(worlds):
    with worlds() as world:
        submit(world)
        (world.repo / "link").symlink_to("baseline.txt")
        git(world.repo, "add", "link")
        git(world.repo, "commit", "-m", "internal link")
        assert accept(world)["push_sha"]
    with worlds() as world:
        submit(world)
        git(world.repo, "update-index", "--add", "--cacheinfo", f"160000,{world.base},module")
        git(world.repo, "commit", "-m", "gitlink")
        assert_failed(world, accept(world), "gitlinks")


def test_unowned_existing_branch_refused_and_owned_family_fast_forward_allowed(worlds):
    with worlds() as world:
        branch = "jobs/finished"
        git(world.repo, "push", "origin", f"{world.base}:refs/heads/{branch}")
        submit(world, branch)
        commit(world.repo)
        row = accept(world)
        assert "not owned" in row["push_error"]
        assert remote_heads(world)["refs/heads/" + branch] == world.base
    with worlds() as world:
        parent = submit(world)
        first = commit(world.repo)
        assert accept(world)["push_sha"] == first
        second_job = submit(world, parent_job_id=parent)
        second = commit(world.repo, text="second")
        assert accept(world, second_job)["push_sha"] == second
        assert len(world.core.store.query("SELECT * FROM job_pushes")) == 2
        # An unrelated job cannot use the same owned branch.
        submit(world)
        assert "another job family" in accept(world)["push_error"]


def test_owned_branch_cannot_be_rewound(worlds):
    with worlds() as world:
        parent = submit(world)
        first = commit(world.repo)
        assert accept(world)["push_sha"] == first
        git(world.repo, "checkout", "--detach", world.base)
        submit(world, parent_job_id=parent)
        divergent = commit(world.repo, "different", "divergent")
        assert accept(world)["push_error"]
        assert remote_heads(world)["refs/heads/jobs/finished"] == first != divergent


@pytest.mark.parametrize("failure", ["ref_conflict", "missing", "timeout", "interrupted"])
def test_push_failure_never_changes_acceptance_and_replay_never_retries(worlds, monkeypatch, failure):
    with worlds() as world:
        submit(world)
        commit(world.repo)
        if failure == "ref_conflict":
            git(world.repo, "push", "origin", f"{world.base}:refs/heads/jobs")
        elif failure == "timeout":
            actual_popen = subprocess.Popen
            def expire(command, **kwargs):
                if "push" in command and any("--git-dir=" in part for part in command):
                    raise subprocess.TimeoutExpired(command, host_push.TIMEOUT_S)
                return actual_popen(command, **kwargs)
            monkeypatch.setattr(subprocess, "Popen", expire)
        elif failure == "interrupted":
            world.core.store.connection.execute(
                "INSERT INTO job_pushes(job_id,branch,remote,started_at,result) VALUES(?,?,?,'now','pending')",
                (world.job_id, "jobs/finished", world.remote))
        heads_before = remote_heads(world)
        row = accept(world, bundle=failure != "missing")
        assert row["push_error"] and not row["push_sha"]
        records = world.core.store.query("SELECT * FROM job_pushes")
        pushes = len([args for _, args in world.calls if args[0] == "push"])
        world.core._export(world.job_id)
        assert world.core.store.query("SELECT * FROM job_pushes") == records
        assert len([args for _, args in world.calls if args[0] == "push"]) == pushes
        assert remote_heads(world) == heads_before


@pytest.mark.parametrize("key,value", [("enabled", 1), ("allowed_remotes", "*"), ("protected", [None]),
                                      ("max_bundle_mb", 0), ("max_commits", True)])
def test_push_policy_validation(tmp_path, key, value):
    policy = json.loads(DEFAULT_POLICY_PATH.read_text())
    policy["push"][key] = value
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    with pytest.raises(PolicyError) as error:
        load_policy(path)
    assert error.value.key == "push." + key


def test_push_default_is_disabled_cli_protocol_and_digest_include_opt_in(tmp_path):
    policy = json.loads(DEFAULT_POLICY_PATH.read_text())
    assert policy["push"]["enabled"] is False and PUSH_DEFAULTS["enabled"] is False
    del policy["push"]
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    assert load_policy(path)["push"]["enabled"] is False
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--push-branch", "jobs/test", "--task", "build", "-C", str(tmp_path), "work"])
    assert args.push_branch == "jobs/test"
    assert protocol.coerce_args(protocol.SubmitArgs, {"request_id": "test", "kind": "dispatch", "workdir": "/",
        "prompt_path": "/prompt", "sandbox": "read-only", "push_branch": "jobs/test"}).push_branch == "jobs/test"
    assert payload_digest(b"x", workdir=tmp_path) != payload_digest(b"x", workdir=tmp_path,
        push_branch="jobs/test", push_remote="file:///origin")


def test_schema_six_migration_preserves_jobs_without_authorizing_push(tmp_path):
    path = tmp_path / "store.sqlite3"
    with Store(path) as store:
        store.add_job(job_id="legacy", request_id="legacy", payload_digest="legacy", kind="dispatch",
                      state="queued", workdir=str(tmp_path), prompt_path=str(tmp_path / "prompt"), sandbox="read-only")
        store.connection.execute("DROP TABLE job_pushes")
        store.connection.execute("DROP TABLE job_owned_branches")
        for column in ("push_branch", "push_remote", "push_default_branch", "push_sha", "push_error"):
            store.connection.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
        store.connection.execute("UPDATE schema_version SET version=6")
    with Store(path) as store:
        assert store.get_job("legacy")["push_branch"] is None
        assert store.query("SELECT * FROM job_pushes") == []
        assert store.query("SELECT * FROM job_owned_branches") == []
        assert store.one("SELECT max(version) AS version FROM schema_version")["version"] == 7


def test_git_timeout_stops_the_transport_process_group(tmp_path, monkeypatch):
    calls = []
    class Process:
        pid = 999999
        returncode = -9
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            if timeout is not None:
                raise subprocess.TimeoutExpired("git", timeout)
            return b"", b""
    def launch(command, **kwargs):
        assert kwargs["start_new_session"] and kwargs["cwd"] == tmp_path
        assert kwargs["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
        return Process()
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: calls.append(("killpg", pid, sig)))
    with pytest.raises(host_push.PushError, match="TimeoutExpired"):
        host_push.git(tmp_path, "push", "file:///local", "a" * 40 + ":refs/heads/job")
    assert [call[0] for call in calls] == ["communicate", "killpg", "communicate"]


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789/._+-:", min_size=0, max_size=25))
def test_refspec_cannot_be_forcing_or_deleting(branch):
    try:
        refspec = host_push.push_refspec("a" * 40, branch)
    except host_push.PushError:
        return
    assert refspec == "a" * 40 + ":refs/heads/" + branch
    assert not refspec.startswith("+") and not refspec.startswith(":")
