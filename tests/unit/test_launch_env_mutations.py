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

from subfleet import claude_cards, cli, compat, descriptors, doctor, guardian, launch_env, retention_git, salvage
from subfleet import daemon as daemon_module
from subfleet.adapters import claude, isolation
from subfleet.adapters.codex import CodexAdapter
from subfleet.conversations import catalog
from subfleet.daemon import Daemon
from subfleet.gate import merge
from subfleet.guard import preflight
from subfleet.provider_env import ProviderEnvironment
from subfleet.sessions import nudge
from tests.fake.test_launch_env_daemon import test_actual_daemon_spawn_rejects_pollution as daemon_oracle
from tests.fake.test_launch_env_daemon import (
    test_review_r4_a_refused_first_start_stores_nothing as first_start_oracle,
    test_review_r4_a_removed_lane_is_held_and_never_blocks_startup as removed_lane_oracle,
)
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401
from tests.unit.test_launch_env_allowlist import (
    test_each_lane_launch_drops_polluted_parent as lane_oracle,
    test_daemon_start_environment_and_launchd_are_differentially_equal as start_oracle,
    test_claude_direct_probe_filters_its_parent as probe_oracle,
    test_guardian_provider_spawn_filters_again as guardian_oracle,
    test_claude_transcript_location_uses_selected_launch_environment as transcript_oracle,
)
from tests.unit.test_launch_env_helpers import test_helper_spawn_drops_polluted_parent as helper_oracle
from tests.unit.test_launch_env_round5 import (
    test_a_name_popped_between_iterating_and_reading_is_skipped as vanishing_oracle,
    test_provider_environment_never_reads_a_withheld_value as withheld_oracle,
    test_session_names_need_the_marker_and_a_live_session as shape_oracle,
)


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


# --- round 5 (review r4): each decision's named regression kills its mutant ---------------


def rewritten(function, transformer):
    """`function` recompiled from its own source after `transformer` rewrites it."""
    tree = transformer.visit(ast.parse(textwrap.dedent(inspect.getsource(function))))
    assert transformer.found, "mutation did not find its site"
    ast.fix_missing_locations(tree)
    namespace = dict(function.__globals__)
    exec(compile(tree, inspect.getfile(function), "exec", flags=__future__.annotations.compiler_flag), namespace)
    compiled = namespace[function.__name__]
    replacement = FunctionType(compiled.__code__, function.__globals__, function.__name__,
                               compiled.__defaults__, compiled.__closure__)
    replacement.__kwdefaults__ = compiled.__kwdefaults__
    return replacement


class EnvironTestFirst(ast.NodeTransformer):
    """P3-3's mutant: test `name in environ`, which reads the value, before `withheld`."""
    found = False

    def visit_comprehension(self, node):
        if any("withheld" in ast.unparse(test) for test in node.ifs):
            node.ifs = [ast.parse("name in environ and name not in withheld", mode="eval").body]
            self.found = True
        return node


class SessionOnBareCurrentTest(ast.NodeTransformer):
    """P3-4's mutant: a live session by `under_pytest` alone, so a bare
    `PYTEST_CURRENT_TEST` selects the session names again."""
    found = False

    def visit_Assign(self, node):
        if any(isinstance(target, ast.Name) and target.id == "session" for target in node.targets):
            node.value = ast.parse("under_pytest(probe)", mode="eval").body
            self.found = True
        return node


def test_named_first_start_regression_kills_seed_before_check(tmp_path, monkeypatch):
    """P2-1: seeding lanes.json before the check stores the refused row."""
    checked = daemon_module.Daemon._open_lanes

    def seeded_first(self, policy_path):
        self._seed_lanes(self._roster())
        return checked(self, policy_path)

    monkeypatch.setattr(daemon_module.Daemon, "_open_lanes", seeded_first)
    with pytest.raises(AssertionError, match="before lanes.json is seeded"):
        first_start_oracle(tmp_path, monkeypatch, "SUBFLEET_FAKE_LANE_NINE")


def test_named_removed_lane_regression_kills_refusing_the_daemon_on_a_stored_lane(tmp_path, monkeypatch):
    """P2-1: a stored lane the check refuses must hold that lane, never the daemon."""
    from subfleet.policy import PolicyError
    checked = daemon_module.check_lane_credentials

    def refusing(policy, path, lanes):
        held = checked(policy, path, lanes)
        if held:
            raise PolicyError(path, "credential_ref", "a stored lane names a variable every launch inherits")
        return held

    monkeypatch.setattr(daemon_module, "check_lane_credentials", refusing)
    with pytest.raises(AssertionError):
        removed_lane_oracle(tmp_path, monkeypatch, "HOME")


def test_named_race_regression_kills_dropping_the_key_error_tolerance(tmp_path, monkeypatch):
    """P3-2: reading every iterated name raises for one popped in between."""
    monkeypatch.setattr(launch_env, "_present", lambda parent, names: {name: parent[name] for name in names})
    with pytest.raises(KeyError):
        vanishing_oracle(tmp_path, ("PYTEST_CURRENT_TEST",))


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_named_withheld_regression_kills_testing_environ_first(provider, monkeypatch):
    """P3-3: the membership test reads the withheld value before rejecting it."""
    monkeypatch.setattr(ProviderEnvironment, "additions", rewritten(ProviderEnvironment.additions, EnvironTestFirst()))
    with pytest.raises(AssertionError, match="withheld lane variable"):
        withheld_oracle(provider, False)


@pytest.mark.parametrize("shape", ["bare-pytest-current-test", "expired-session-copied-current-test-no-marker"])
def test_named_shape_regression_kills_selecting_on_bare_pytest_current_test(tmp_path, monkeypatch, shape):
    """P3-4: selection that ignores the marker revives the session names."""
    monkeypatch.setattr(launch_env, "_selected", rewritten(launch_env._selected, SessionOnBareCurrentTest()))
    with pytest.raises(AssertionError):
        shape_oracle(tmp_path, shape)
