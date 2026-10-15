"""D-ENV1: execute one ambient-inheritance mutation at each spawn boundary.

Mutants live only in memory. Each runs its named regression oracle; no nested
pytest process, source rewrite, provider, credential lookup or live daemon.
"""

import ast
import __future__
import inspect
import textwrap
from types import FunctionType

import pytest

from subfleet import claude_cards, cli, compat, descriptors, doctor, guardian, retention_git, salvage
from subfleet.adapters import claude, isolation
from subfleet.adapters.codex import CodexAdapter
from subfleet.conversations import catalog
from subfleet.daemon import Daemon
from subfleet.gate import merge
from subfleet.guard import preflight
from subfleet.provider_env import ProviderEnvironment
from subfleet.sessions import nudge
from tests.fake.test_launch_env_daemon import test_actual_daemon_spawn_rejects_pollution as daemon_oracle
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401
from tests.unit.test_launch_env_allowlist import (
    test_each_lane_launch_drops_polluted_parent as lane_oracle,
    test_daemon_start_environment_and_launchd_are_differentially_equal as start_oracle,
    test_claude_direct_probe_filters_its_parent as probe_oracle,
    test_guardian_provider_spawn_filters_again as guardian_oracle,
    test_claude_transcript_location_uses_selected_launch_environment as transcript_oracle,
)
from tests.unit.test_launch_env_helpers import test_helper_spawn_drops_polluted_parent as helper_oracle


def mutated(function, *, assignment=None, keyword_index=None, return_value=False, spawn_environment=False):
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    ambient = ast.parse("dict(__import__('os').environ)", mode="eval").body
    candidates = []
    for node in ast.walk(tree):
        if (spawn_environment and isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "_spawn_guardian"):
            candidates.append(node)
        elif assignment is not None and isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == assignment for target in node.targets):
                candidates.append(node)
        elif keyword_index is not None and isinstance(node, ast.Call):
            func = node.func
            spawn = ((isinstance(func, ast.Attribute) and func.attr in {"Popen", "run"}) or
                     (isinstance(func, ast.Name) and func.id in {"runner", "popen"}))
            if spawn:
                candidates.extend(keyword for keyword in node.keywords if keyword.arg == "env")
        elif return_value and isinstance(node, ast.Return):
            candidates.append(node)
    candidates.sort(key=lambda node: node.lineno)
    assert candidates, "mutation did not find its boundary"
    node = candidates[keyword_index or 0]
    if spawn_environment:
        node.args[1] = ambient
    else:
        node.value = ambient
    ast.fix_missing_locations(tree)
    namespace = dict(function.__globals__)
    exec(compile(tree, inspect.getfile(function), "exec", flags=__future__.annotations.compiler_flag), namespace)
    compiled = namespace[function.__name__]
    replacement = FunctionType(compiled.__code__, function.__globals__, function.__name__,
                               compiled.__defaults__, compiled.__closure__)
    replacement.__kwdefaults__ = compiled.__kwdefaults__
    return replacement


HELPER_MUTANTS = [
    ("heal", claude_cards, "heal_turn", {"assignment": "env"}),
    ("claude-version", claude_cards, "cli_version", {"keyword_index": 0}),
    ("doctor-version", doctor, "_run_full", {"keyword_index": 0}),
    ("codex-inspection", isolation, "inspect_codex", {"keyword_index": 0}),
    ("codex-inspection", isolation, "inspect_codex", {"keyword_index": 1}),
    ("guard-version", preflight, "_version", {"keyword_index": 0}),
    ("guard-hooks", preflight, "_hooks_list", {"keyword_index": 0}),
    ("salvage", salvage, "_git_env", {"assignment": "env"}),
    ("retention", retention_git, "environment", {"assignment": "env"}),
    ("gate", merge, "run_command", {"keyword_index": 0}),
    ("catalog", catalog, "spawn_refresh", {"assignment": "env"}),
    ("nudge", nudge, "spawn", {"assignment": "environment"}),
    ("descriptors", descriptors, "kernel_open_files_ceiling", {"keyword_index": 0}),
    ("compat", compat, "delegate", {"keyword_index": 0}),
]


@pytest.mark.parametrize("path,owner,name,mutation", HELPER_MUTANTS,
                         ids=[path + "-" + str(i) for i, (path, *_) in enumerate(HELPER_MUTANTS)])
def test_named_helper_regression_kills_ambient_mutant(path, owner, name, mutation, tmp_path, monkeypatch):
    monkeypatch.setattr(owner, name, mutated(getattr(owner, name), **mutation))
    with pytest.raises(AssertionError):
        helper_oracle(path, tmp_path, monkeypatch)


@pytest.mark.parametrize("provider,mode", [(p, m) for p in ("codex", "claude")
                         for m in ("detached", "resume", "turn", "probe", "isolated")])
def test_named_lane_regression_kills_ambient_mutant(provider, mode, tmp_path, monkeypatch):
    monkeypatch.setattr(ProviderEnvironment, "assemble", mutated(ProviderEnvironment.assemble, return_value=True))
    with pytest.raises(AssertionError):
        lane_oracle(provider, mode, tmp_path, monkeypatch)


def test_named_daemon_start_regression_kills_ambient_mutant(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "daemon_env", mutated(cli.daemon_env, return_value=True))
    with pytest.raises(AssertionError):
        start_oracle(tmp_path, monkeypatch, "codex")


def test_named_direct_probe_regression_kills_ambient_mutant(tmp_path, monkeypatch):
    monkeypatch.setattr(claude.ClaudeAdapter, "_run_turn", mutated(claude.ClaudeAdapter._run_turn, assignment="env"))
    with pytest.raises(AssertionError):
        probe_oracle(tmp_path, monkeypatch)


def test_named_isolated_inspection_regression_kills_ambient_mutant(tmp_path, monkeypatch):
    monkeypatch.setattr(CodexAdapter, "_launch", mutated(CodexAdapter._launch, assignment="env"))
    with pytest.raises(AssertionError):
        lane_oracle("codex", "isolated", tmp_path, monkeypatch)


def test_named_transcript_regression_kills_ambient_mutant(tmp_path, monkeypatch):
    monkeypatch.setattr(claude.ClaudeAdapter, "_config_projects_dir",
                        mutated(claude.ClaudeAdapter._config_projects_dir, assignment="env"))
    with pytest.raises(AssertionError):
        transcript_oracle(tmp_path, monkeypatch, "default")


@pytest.mark.parametrize("relay", [False, True], ids=["stdin", "relay"])
def test_named_guardian_regression_kills_ambient_mutant(tmp_path, monkeypatch, relay):
    monkeypatch.setattr(guardian, "run_guardian", mutated(guardian.run_guardian, keyword_index=0 if relay else 1))
    with pytest.raises(AssertionError):
        guardian_oracle(tmp_path, monkeypatch, relay)


@pytest.mark.parametrize("mode,name", [("detached", "_launch"), ("turn", "_launch"),
                                      ("probe", "_execute_probe"), ("enrollment", "_enrollment_turn")])
def test_named_daemon_spawn_regression_kills_ambient_mutant(routing_state, monkeypatch, mode, name):
    monkeypatch.setattr(Daemon, name, mutated(getattr(Daemon, name), spawn_environment=True))
    with pytest.raises(AssertionError):
        daemon_oracle(routing_state, monkeypatch, mode)
