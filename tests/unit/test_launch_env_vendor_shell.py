"""D-ENV3: no other vendor's credential, and no shell startup file's export, reaches a lane.

On 2026-10-26 a Codex lane command's environment held ANTHROPIC_API_KEY. The
daemon's selection (D-ENV1) had dropped it from the process; Codex's shell
snapshot, on by default in codex-cli 0.159.0, replayed the person's ~/.zshrc
exports into each command after that. These tests pin both halves: every
Codex launch that runs a model turn turns the snapshot and the login profile
off (`CODEX_SHELL_CONFIG`), and no launch carries another vendor's credential,
whatever explicit authority (`env.pass`, a launch's own additions) names it.
`tests/process/test_provider_shell_environment.py` runs the real Codex and Claude Code binaries.

Failures disclose names only; every value here is a dummy.
"""

import dataclasses
import os

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet.adapters.codex import CodexAdapter
from subfleet.adapters.isolation import CODEX_CONFIG
from subfleet.contracts import Credential, JobSpec, Lane, LaneOwner, Sandbox
from subfleet.conversations import codex_turn
from subfleet.daemon import Daemon
from subfleet.guard.preflight import PUBLISH_HOOK_KEY
from subfleet.launch_env import (ALLOWED_NAMES, CODEX_SHELL_CONFIG, LAUNCH_NAMES, TEST_SESSION_NAMES,
                                 codex_shell_args, credential_vendors, foreign_credentials)
from subfleet.provider_env import ProviderEnvironment
from tests.unit.test_launch_env_allowlist import (
    ALLOWED, NAMES, PATHS, POLICY, VALUES, ParentEnvironment, assert_environment, launch_for, vendor_foreign,
)

DUMMY = "dummy-not-a-credential"
# Each vendor's credentials as Claude Code, the Anthropic SDK, Codex and the
# OpenAI SDK name them; the 2026-10-26 incident's key is the first.
ANTHROPIC = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
             "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "CLAUDE_API_KEY")
OPENAI = ("OPENAI_API_KEY", "CODEX_API_KEY", "AZURE_OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_ADMIN_KEY")
FOREIGN = {"codex": ANTHROPIC, "claude": OPENAI}


def _daemon(policy=POLICY):
    daemon = Daemon.__new__(Daemon)
    daemon._provider_environment = ProviderEnvironment(policy)
    return daemon


@pytest.mark.parametrize("provider,mode", PATHS)
def test_parent_anthropic_key_reaches_no_codex_launch(provider, mode, tmp_path, monkeypatch):
    """The incident's shape: ANTHROPIC_API_KEY=<dummy> in the daemon's environment."""
    parent = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "ANTHROPIC_API_KEY": DUMMY}
    monkeypatch.setattr(os, "environ", parent)
    lane, launch = launch_for(provider, mode, tmp_path, monkeypatch)
    actual = _daemon()._launch_environment(lane, launch)
    assert "ANTHROPIC_API_KEY" not in actual
    assert DUMMY not in actual.values()


@pytest.mark.parametrize("provider,mode", PATHS)
def test_passed_vendor_credentials_reach_only_their_own_vendor(provider, mode, tmp_path, monkeypatch):
    """Even explicit authority cannot hand one vendor's credential to the other's lane."""
    vendor_names = (*ANTHROPIC, *OPENAI)
    parent = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **dict.fromkeys(vendor_names, DUMMY)}
    monkeypatch.setattr(os, "environ", parent)
    lane, launch = launch_for(provider, mode, tmp_path, monkeypatch)
    actual = _daemon({**POLICY, "env": {"pass": list(vendor_names)}})._launch_environment(lane, launch)
    assert not set(FOREIGN[provider]) & set(actual), "a foreign vendor credential reached the launch"


@pytest.mark.parametrize("provider,mode", PATHS)
def test_foreign_credential_in_a_launch_addition_is_dropped(provider, mode, tmp_path, monkeypatch):
    """A launch's own additions cannot carry the other vendor's credential either."""
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin"})
    lane, launch = launch_for(provider, mode, tmp_path, monkeypatch)
    own, foreign = ("CODEX_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN") if provider == "codex" else \
        ("CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY")
    added = dataclasses.replace(launch, env_add={**launch.env_add, own: DUMMY, foreign: DUMMY})
    actual = _daemon()._launch_environment(lane, added)
    assert foreign not in actual
    if provider == "claude":
        # A Claude lane's own token arrives as its addition (C-12.4) and stays.
        assert actual[own] == DUMMY


def test_vendor_classification_examples():
    assert credential_vendors("ANTHROPIC_API_KEY") == {"claude"}
    assert credential_vendors("CLAUDE_CODE_OAUTH_TOKEN") == {"claude"}
    assert credential_vendors("anthropic_auth_token") == {"claude"}
    assert credential_vendors("OPENAI_API_KEY") == {"codex"}
    assert credential_vendors("AZURE_OPENAI_API_KEY") == {"codex"}
    assert credential_vendors("CODEX_API_KEY") == {"codex"}
    assert credential_vendors("CLAUDE_TO_OPENAI_TOKEN") == {"claude", "codex"}
    # Homes and configuration carry no credential word; other services are no vendor's.
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "SUBFLEET_CLAUDE_SETTINGS",
                 "CODEX_GUARD_PREFLIGHT_TIMEOUT", "GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "PATH"):
        assert credential_vendors(name) == frozenset(), name
    assert foreign_credentials("codex", ANTHROPIC + OPENAI) == ANTHROPIC
    assert foreign_credentials("claude", ANTHROPIC + OPENAI) == OPENAI
    # A provider with no vendor here carries neither vendor's credential.
    assert foreign_credentials("gemini", ANTHROPIC + OPENAI) == ANTHROPIC + OPENAI
    # Without a provider, only the caller's own additions keep a vendor credential.
    assert foreign_credentials(None, ANTHROPIC + OPENAI, additions=("CLAUDE_CODE_OAUTH_TOKEN",)) == tuple(
        name for name in ANTHROPIC + OPENAI if name != "CLAUDE_CODE_OAUTH_TOKEN")


def test_no_ambient_or_launch_name_is_a_vendor_credential():
    """D-ENV3 removes nothing D-ENV1 selects, a test session inherits, or a launch owns."""
    names = ALLOWED_NAMES | ALLOWED | TEST_SESSION_NAMES | LAUNCH_NAMES | {"CODEX_HOME", "CLAUDE_CONFIG_DIR"}
    assert sorted(name for name in names if credential_vendors(name)) == []


def test_assemble_without_provider_keeps_only_added_vendor_credentials(monkeypatch):
    parent = {"PATH": "/usr/bin:/bin", "OPENAI_ADMIN_KEY": DUMMY, "CLAUDE_CODE_OAUTH_TOKEN": DUMMY}
    environment = ProviderEnvironment({**POLICY, "env": {"pass": sorted(parent)}})
    actual = environment.assemble({"CLAUDE_CODE_OAUTH_TOKEN": "added"}, environ=parent)
    assert_environment(actual, {"PATH": "/usr/bin:/bin", "CLAUDE_CODE_OAUTH_TOKEN": "added"})


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_build_drops_foreign_vendor_credentials(provider):
    """Guard preflight, doctor and Claude's probe turns build with a provider (D-ENV3)."""
    parent = {"PATH": "/usr/bin:/bin", **dict.fromkeys((*ANTHROPIC, *OPENAI), DUMMY)}
    environment = ProviderEnvironment({**POLICY, "env": {"pass": sorted(parent)}})
    actual = environment.build(provider, {}, environ=parent)
    assert not set(FOREIGN[provider]) & set(actual)


# Names that carry a vendor's word and a credential word in any case and
# position, beside the allowlist's own names and arbitrary ones.
VENDOR_WORD = st.sampled_from(["ANTHROPIC", "CLAUDE", "OPENAI", "CODEX", "anthropic", "Claude", "openai", "codex"])
CREDENTIAL_WORD = st.sampled_from(["_API_KEY", "_TOKEN", "_OAUTH_TOKEN", "_SECRET", "_AUTH", "_PASSWORD", "KEY", "_PAT"])
AFFIX = st.from_regex(r"[A-Z_]{0,6}", fullmatch=True)
VENDOR_NAMES = st.builds(lambda pre, word, cred, post: pre + word + cred + post, AFFIX, VENDOR_WORD,
                         CREDENTIAL_WORD, AFFIX).filter(lambda name: name[0].isalpha() or name[0] == "_")
PARENT = st.dictionaries(st.one_of(NAMES, VENDOR_NAMES, st.sampled_from(sorted(ALLOWED)),
                                   st.sampled_from([*ANTHROPIC, *OPENAI])),
                         VALUES, max_size=30).map(ParentEnvironment)


@settings(max_examples=60, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(parent=PARENT, data=st.data())
def test_property_every_launch_is_the_allowlist_minus_foreign_vendors(parent, data, tmp_path, monkeypatch):
    """For every parent and `env.pass`, each launch path's environment is exactly
    D-ENV1's selection plus the launch's additions, less mandatory removals and
    every other vendor's credential, by an oracle independent of launch_env."""
    passed = data.draw(st.lists(st.sampled_from(sorted(parent)), unique=True), label="passed names") if parent else []
    environment = ProviderEnvironment({**POLICY, "env": {"pass": passed}})
    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", dict(parent))
        daemon = Daemon.__new__(Daemon)
        daemon._provider_environment = environment
        for provider, mode in PATHS:
            lane, launch = launch_for(provider, mode, tmp_path / (provider + mode), patch)
            actual = daemon._launch_environment(lane, launch)
            assert not [name for name in actual if vendor_foreign(provider, name)], "foreign vendor credential"
            expected = {k: v for k, v in parent.items() if k in ALLOWED or k in passed}
            expected.update(launch.env_add)
            for name in (*launch.env_remove, "CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
                expected.pop(name, None)
            expected = {k: v for k, v in expected.items() if not vendor_foreign(provider, k)}
            assert_environment(actual, expected)


@settings(max_examples=200, deadline=None, derandomize=True)
@given(names=st.lists(st.one_of(NAMES, VENDOR_NAMES), max_size=12, unique=True),
       provider=st.sampled_from(["codex", "claude", "gemini"]))
def test_property_foreign_credentials_matches_the_oracle(names, provider):
    """`foreign_credentials` is the oracle's set, in input order, for every name list."""
    assert foreign_credentials(provider, names) == tuple(name for name in names if vendor_foreign(provider, name))


# --- the shell snapshot half -------------------------------------------------

GUARD = 'hooks={PreToolUse=[], state={"' + PUBLISH_HOOK_KEY + '"={trusted_hash="x"}}}'


def _key(config):
    return config.split("=", 1)[0]


def assert_shell_pinned(argv):
    """Each `CODEX_SHELL_CONFIG` key is set to its pinned value by a `-c` among the
    options (before any `resume`), and no later `-c` sets it or a table holding it."""
    argv = list(argv)
    stop = argv.index("resume") if "resume" in argv else len(argv)
    pairs = [(at, argv[at + 1]) for at in range(stop - 1) if argv[at] == "-c"]
    for config in CODEX_SHELL_CONFIG:
        key = _key(config)
        ours = [at for at, value in pairs if value == config]
        assert ours, f"{config} missing from the launch options"
        later = [value for at, value in pairs if at > ours[-1]
                 and (_key(value) == key or key.startswith(_key(value) + "."))]
        assert not later, f"{config} is overridden later by {[_key(v) for v in later]}"


def _codex_job(tmp_path, sandbox, *, task=None, network=False, isolated=False):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Reply OK")
    return JobSpec(request_id="shell", kind="dispatch", workdir=str(tmp_path), prompt_path=str(prompt),
                   task=task, tier="standard", pinned_model=None, pinned_lane=None, sandbox=sandbox,
                   out_path=None, name="shell", network=network, isolated_review=isolated,
                   review_root=str(tmp_path) if isolated else None)


def _codex_lane(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return Lane("codex-1", "codex", "codex:fake", Credential("codex", str(home), "home"),
                str(home), LaneOwner.V2, False)


@pytest.mark.parametrize("sandbox", [Sandbox.READ_ONLY, Sandbox.WORKSPACE_WRITE])
@pytest.mark.parametrize("task", [None, "review"])
@pytest.mark.parametrize("network", [False, True])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("unified_exec", [None, "off"])
def test_every_codex_exec_launch_pins_the_shell_config(sandbox, task, network, resume, unified_exec,
                                                        tmp_path, monkeypatch):
    """Exhaustive over the options that change a `codex exec` launch's argv."""
    if unified_exec:
        monkeypatch.setenv("SUBFLEET_CODEX_UNIFIED_EXEC", unified_exec)
    else:
        monkeypatch.delenv("SUBFLEET_CODEX_UNIFIED_EXEC", raising=False)
    job, lane = _codex_job(tmp_path, sandbox, task=task, network=network), _codex_lane(tmp_path)
    adapter, credential = CodexAdapter(), {"CODEX_HOME": lane.home}
    guard = GUARD if sandbox == Sandbox.WORKSPACE_WRITE else None
    prompt = tmp_path / "prompt.md"
    if resume:
        launch = adapter.resume_launch(job, "shell/a1", tmp_path / "a1", lane, credential, "thread-1", prompt, guard)
        assert launch.argv[-3:] == ("resume", "thread-1", "-")
    else:
        launch = adapter.build_launch(job, "shell/a1", tmp_path / "a1", lane, credential, "gpt-6.1-sol", "high",
                                      prompt, guard)
    assert_shell_pinned(launch.argv)


def test_isolated_review_pins_the_shell_config_once(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin"})

    def inspect(*args, **kwargs):
        return {"requirements": None}, {"layers": [{"name": {"type": "user"}, "config": {}}]}, []
    adapter, lane = CodexAdapter(), _codex_lane(tmp_path)
    adapter.isolation_inspector = inspect
    job = _codex_job(tmp_path, Sandbox.READ_ONLY, task="review", isolated=True)
    launch = adapter.build_launch(job, "shell/a1", tmp_path / "a1", lane, {"CODEX_HOME": lane.home},
                                  "gpt-6.1-sol", None, tmp_path / "prompt.md", None)
    assert_shell_pinned(launch.argv)
    assert all(launch.argv.count(config) == 1 for config in CODEX_SHELL_CONFIG)
    assert set(CODEX_SHELL_CONFIG) <= set(CODEX_CONFIG)


@pytest.mark.parametrize("unified_exec_off", [False, True])
def test_every_codex_turn_pins_the_shell_config(unified_exec_off):
    assert_shell_pinned(codex_turn.argv("codex", "hooks={}", unified_exec_off=unified_exec_off))


def test_codex_shell_args_are_config_pairs():
    assert codex_shell_args() == [arg for config in CODEX_SHELL_CONFIG for arg in ("-c", config)]
    assert {_key(config) for config in CODEX_SHELL_CONFIG} == {
        "features.shell_snapshot", "features.shell_snapshot_v2", "shell_environment_policy.experimental_use_profile"}
    assert all(config.endswith("=false") for config in CODEX_SHELL_CONFIG)


def test_assert_shell_pinned_refuses_a_missing_or_overridden_key():
    """The checker above is not vacuous."""
    with pytest.raises(AssertionError, match="missing"):
        assert_shell_pinned(["codex", "exec", "-c", CODEX_SHELL_CONFIG[0]])
    with pytest.raises(AssertionError, match="overridden"):
        assert_shell_pinned(["codex", "exec", *codex_shell_args(), "-c", "features.shell_snapshot=true"])
    with pytest.raises(AssertionError, match="overridden"):
        assert_shell_pinned(["codex", "exec", *codex_shell_args(), "-c", "features={shell_snapshot=true}"])
    with pytest.raises(AssertionError, match="missing"):
        assert_shell_pinned(["codex", "exec", "resume", "t", *codex_shell_args()])
