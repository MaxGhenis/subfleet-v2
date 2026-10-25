"""D-SH1 round 8: #221's review launch meets #222's launch environment (D-ENV1).

A review launch's own variables (C-12.4's git denial and the launch marker)
are launch authority, added after C-12.3's selection: neither the allowlist
nor its credential-word backstop withholds them, the guardian receives them by
name and passes them to the provider, and a parent's values of the same names
reach no launch. The one `SUBFLEET_*` name among them is classified as
launch-owned (`launch_env.LAUNCH_NAMES`). The uncommitted copy's applicability
check, a local helper, selects its git environment as every other helper does.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import guardian, launch_env, review_publish, salvage, uncommitted
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.adapters.codex import CodexAdapter
from subfleet.contracts import Credential, JobSpec, Lane, LaneOwner, Sandbox
from subfleet.daemon import Daemon
from subfleet.guard.preflight import override_string
from subfleet.provider_env import ProviderEnvironment
from tests.unit.test_launch_env_allowlist import ALLOWED, PARENTS, POLICY, POLLUTION, assert_environment

PINNED = review_publish.pinned_environment()
#: What a parent might hold under the pinned names: values the launch must replace.
PARENT_PINS = {"GIT_ALLOW_PROTOCOL": "file:https:ssh", "GIT_CONFIG_PARAMETERS": "'core.pager'='cat'",
               "SUBFLEET_PUBLISH_DENIED": "0"}
GUARD = "/fixture/reviewed/never-rules-hook.sh"
PUBLISH = "/fixture/state/publish-guard/review-publish-hook"
CASES = [(provider, sandbox, mode) for provider in ("codex", "claude")
         for sandbox in (Sandbox.READ_ONLY, Sandbox.WORKSPACE_WRITE) for mode in ("detached", "resume")]


def launch_for(provider: str, sandbox: Sandbox, mode: str, directory: Path, *, task: str = "review"):
    directory.mkdir(parents=True, exist_ok=True)
    home = directory / "home"
    home.mkdir(exist_ok=True)
    prompt = directory / "prompt.md"
    prompt.write_text("Reply OK")
    credential = {"CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR": str(home)}
    lane = Lane(provider + "-1", provider, provider + ":fake", Credential(provider, str(home), "home"),
                str(home), LaneOwner.V2, False)
    adapter = CodexAdapter() if provider == "codex" else ClaudeAdapter(projects_dir=home / "projects")
    job = JobSpec(request_id="fake", kind="dispatch", workdir=str(directory), prompt_path=str(prompt),
                  task=task, tier="standard", pinned_model=None, pinned_lane=None, sandbox=sandbox,
                  out_path=None, name="round8")
    # The daemon's override for a Codex review carries the publish group (C-12.3).
    override = (override_string(GUARD, publish_command=PUBLISH if task == "review" else None)
                if provider == "codex" else None)
    if mode == "resume":
        launch = adapter.resume_launch(job, "fake/a1", directory, lane, credential, "fake-session", prompt, override)
    else:
        launch = adapter.build_launch(job, "fake/a1", directory, lane, credential, "fake", None, prompt, override)
    return lane, launch


def daemon_environment(lane, launch, environment: ProviderEnvironment) -> dict[str, str]:
    """What the daemon gives the guardian: C-12.3's selection, then the C-5.1 markers."""
    daemon = Daemon.__new__(Daemon)
    daemon._provider_environment = environment
    env = daemon._launch_environment(lane, launch)
    env.update(SUBFLEET_JOB="fake", SUBFLEET_ATTEMPT="fake/a1", SUBFLEET_ROOT="/fixture/root")
    return env


def provider_environment(env: dict[str, str], tmp_path: Path, monkeypatch) -> dict[str, str]:
    """The environment the guardian gives the provider, from the names the daemon authorizes."""
    args = launch_env.guardian_pass_args(env)
    assert args[0::2] == ["--env-pass"] * (len(args) // 2)
    seen = []
    with monkeypatch.context() as patch:
        # The guardian's own environment is the daemon's, plus its settle setting.
        patch.setattr(os, "environ", {**env, guardian.LINGER_SETTLE_ENV: "1"})
        patch.setattr(guardian.os, "setsid", lambda: None)
        patch.setattr(guardian.signal, "signal", lambda *args: None)
        patch.setattr(guardian, "boot_id", lambda: "fake")
        patch.setattr(guardian, "proc_start", lambda pid: "fake")
        patch.setattr(guardian.subprocess, "Popen",
                      lambda argv, **kwargs: seen.append(kwargs["env"]) or SimpleNamespace(pid=123, wait=lambda: 0))
        directory = tmp_path / "guardian"
        previous = os.umask(0o077)
        try:
            assert guardian.run_guardian(["fake-provider"], attempt_dir=directory, cwd=str(tmp_path),
                                         stdin_path=None, stdout_path=str(tmp_path / "stdout"),
                                         stderr_path=str(tmp_path / "stderr"), env_pass=tuple(args[1::2])) == 0
        finally:
            os.umask(previous)
    assert len(seen) == 1
    return seen[0]


def test_the_review_launch_s_subfleet_name_is_classified_launch_owned():
    """#222's classification (test_launch_env_round3): the marker a review launch
    adds is launch-owned, never ambient configuration or a harness hook."""
    names = {name for name in PINNED if name.startswith("SUBFLEET_")}
    assert names == {"SUBFLEET_PUBLISH_DENIED"} and names <= launch_env.LAUNCH_NAMES
    assert not names & (launch_env.ALLOWED_NAMES | launch_env.TEST_SESSION_NAMES | launch_env.RETIRED_NAMES)
    assert not any(launch_env.inherited_name(name) for name in PINNED)


@pytest.mark.parametrize("name", sorted(PINNED))
def test_no_pinned_name_holds_a_word_the_backstop_or_codex_excludes(name):
    """Neither #222's credential words nor Codex's default `*KEY*`, `*SECRET*`
    and `*TOKEN*` excludes match a pinned name (#221 review r1b, P3-1)."""
    assert launch_env.CREDENTIAL_PATTERN.search(name) is None
    assert not any(word in name.upper() for word in ("KEY", "SECRET", "TOKEN"))


@pytest.mark.parametrize("provider,sandbox,mode", CASES)
def test_each_review_launch_keeps_its_pins_through_selection_and_the_guardian(
        provider, sandbox, mode, tmp_path, monkeypatch):
    """C-12.3/4: the pins are launch additions, so a parent's own values of those
    names, and its credentials, never replace or join them."""
    parent = {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", **POLLUTION, **PARENT_PINS}
    monkeypatch.setattr(os, "environ", parent)
    lane, launch = launch_for(provider, sandbox, mode, tmp_path)
    assert launch.notes.get("publish_denied") is True
    env = daemon_environment(lane, launch, ProviderEnvironment(POLICY))
    assert {name: env[name] for name in PINNED} == PINNED
    child = provider_environment(env, tmp_path, monkeypatch)
    assert {name: child[name] for name in PINNED} == PINNED
    assert not set(child) & set(POLLUTION) and guardian.LINGER_SETTLE_ENV not in child
    assert set(child) == set(env)
    if provider == "codex":
        # A lane's shell_environment_policy cannot drop them inside Codex either (P3-3).
        assert all(f"shell_environment_policy.set.{name}=" in " ".join(launch.argv) for name in PINNED)


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_a_parent_s_pinned_names_reach_no_launch_that_is_not_a_review(provider, tmp_path, monkeypatch):
    """Not ambient authority: a build inherits none of the names a review pins."""
    parent = {"PATH": "/usr/bin:/bin", **POLLUTION, **PARENT_PINS}
    monkeypatch.setattr(os, "environ", parent)
    lane, launch = launch_for(provider, Sandbox.READ_ONLY, "detached", tmp_path, task="build")
    assert not launch.notes.get("publish_denied")
    env = daemon_environment(lane, launch, ProviderEnvironment(POLICY))
    assert not set(env) & set(PINNED)
    assert not set(provider_environment(env, tmp_path, monkeypatch)) & set(PINNED)


@settings(max_examples=25, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(parent=PARENTS, passed=st.lists(st.sampled_from(sorted({*PINNED, *POLLUTION, "OPENAI_API_KEY"})),
                                       max_size=4, unique=True),
       case=st.sampled_from(CASES))
def test_bounded_property_the_pins_hold_whatever_the_parent_or_env_pass(parent, passed, case, tmp_path, monkeypatch):
    """For any parent and any `env.pass`, even one naming a pinned name, the
    provider gets exactly the pinned values, and only names the launch or
    C-12.3's selection admits."""
    parent = {**dict.fromkeys(ALLOWED, "dummy"), **parent, **POLLUTION, **PARENT_PINS}
    for name in passed:
        parent.setdefault(name, "dummy")
    environment = ProviderEnvironment({**POLICY, "env": {"pass": passed}})
    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", parent)
        provider, sandbox, mode = case
        lane, launch = launch_for(provider, sandbox, mode, tmp_path / "-".join((provider, sandbox.value, mode)))
        env = daemon_environment(lane, launch, environment)
    child = provider_environment(env, tmp_path, monkeypatch)
    assert {name: child[name] for name in PINNED} == PINNED
    admitted = {name for name in parent if launch_env.allowed_name(name) or name in passed}
    removed = {*launch.env_remove, "CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
    assert set(child) <= (admitted - removed) | set(launch.env_add) | {"SUBFLEET_JOB", "SUBFLEET_ATTEMPT", "SUBFLEET_ROOT"}


# --- the uncommitted copy's git helper (C-6.6 meets C-12.3) -------------------------

def check_environments(parent, tmp_path, monkeypatch) -> list[dict[str, str]]:
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", parent)
        patch.setattr(salvage.subprocess, "run", run)
        uncommitted._check_applies(tmp_path / "repo", "0" * 40, b"diff\n", tmp_path / "scratch", None)
    assert len(seen) == 2, "read-tree and apply --check each reach git"
    return seen


def expected_check_environment(parent, index: str) -> dict[str, str]:
    expected = {name: value for name, value in parent.items() if launch_env.allowed_name(name)}
    for name in salvage.GIT_LOCATION_ENV + salvage.GIT_PATHSPEC_ENV:
        expected.pop(name, None)
    expected.update(dict.fromkeys(salvage.GIT_TRACE2_TARGETS, "0"), LC_ALL="C", GIT_INDEX_FILE=index)
    return expected


def test_the_uncommitted_check_selects_its_git_environment(tmp_path, monkeypatch):
    """C-12.3: a local helper adds only its own variable (the scratch index) to
    the selected names; a credential in the daemon's environment reaches no git."""
    parent = {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", **POLLUTION}
    seen = check_environments(parent, tmp_path, monkeypatch)
    index = seen[0]["GIT_INDEX_FILE"]
    assert Path(index).name == "index" and Path(index).parent.parent == tmp_path / "scratch"
    for env in seen:
        assert_environment(env, expected_check_environment(parent, index))


@settings(max_examples=20, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(parent=PARENTS)
def test_bounded_property_the_uncommitted_check_never_inherits_an_unlisted_name(parent, tmp_path, monkeypatch):
    parent = {**dict.fromkeys(ALLOWED, "dummy"), **parent, **POLLUTION}
    seen = check_environments(parent, tmp_path, monkeypatch)
    for env in seen:
        assert_environment(env, expected_check_environment(parent, seen[0]["GIT_INDEX_FILE"]))
