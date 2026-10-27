"""D-ENV1: execute one ambient-inheritance mutation at each spawn boundary.

Mutants live only in memory. Each runs its named regression oracle; no nested
pytest process, source rewrite, provider, credential lookup or live daemon.
"""

import ast
import __future__
import inspect
import os
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
    test_review_r5_a_refused_row_for_a_stored_lane_is_ignored as ignored_row_oracle,
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
from tests.unit.test_launch_env_round6 import (
    test_daemon_start_check_lists_each_inheriting_env_row_and_reads_only_lanes_json as install_check_oracle,
)
from tests.fake.test_launch_env_ignored_hook import (
    test_review_r1_an_ignored_rows_harness_hook_reaches_no_launch as ignored_hook_oracle,
)
from tests.unit.test_launch_env_round7 import (
    repair_root,
    test_daemon_start_check_scrubs_a_token_shaped_lane_id as check_scrub_oracle,
    test_review_r1_a_transfer_repairs_the_lanes_refused_row as transfer_repair_oracle,
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


def test_named_transcript_regression_kills_ambient_mutant(tmp_path, monkeypatch, production_process):
    monkeypatch.setattr(claude.ClaudeAdapter, "_config_projects_dir",
                        mutated(claude.ClaudeAdapter._config_projects_dir, assignment="env"))
    with pytest.raises(AssertionError):
        transcript_oracle(tmp_path, monkeypatch, "default", production_process)


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
    """P2-1: seeding lanes.json before the check stores the refused row.

    Since review r5 (P3-1) the check then finds that lane stored and ignores
    its row, so the mutant's daemon serves, holding a lane it should never have
    stored, where it used to exit 2 having stored it. The oracle fails either way.
    """
    checked = daemon_module.Daemon._open_lanes

    def seeded_first(self, policy_path):
        self._seed_lanes(self._roster())
        return checked(self, policy_path)

    monkeypatch.setattr(daemon_module.Daemon, "_open_lanes", seeded_first)
    with pytest.raises((AssertionError, pytest.fail.Exception),
                       match="before lanes.json is seeded|a refused roster served"):
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


@pytest.mark.parametrize("caller", ["daemon start", "daemon startup"])
def test_named_ignored_row_regression_kills_refusing_a_stored_lanes_row(tmp_path, monkeypatch, caller):
    """Review r5, P3-1: a refused lanes.json row for a lane the store holds stops
    startup again, at either caller, when it is checked as a new lane's row."""
    from subfleet import credentials
    original = credentials.startup_lanes

    def refusing(roster, stored):
        return [(lane_id, name, launch_env.ROSTER if source == launch_env.IGNORED else source)
                for lane_id, name, source in original(roster, stored)]

    monkeypatch.setattr(cli if caller == "daemon start" else daemon_module, "startup_lanes", refusing)
    with pytest.raises(AssertionError):
        ignored_row_oracle(tmp_path, monkeypatch, "HOME")


def test_named_install_check_regression_kills_reading_a_value(tmp_path, monkeypatch, capsys):
    """D-ENV2: an install check that reads each listed variable's value, here to
    say whether it is set, fails the check's own test."""
    original = cli.lanes_json_refusals

    def reading(root):
        found = original(root)
        for _, name in found:
            os.environ.get(name)
        return found

    monkeypatch.setattr(cli, "lanes_json_refusals", reading)
    with pytest.raises(AssertionError, match="read a lane variable's value"):
        install_check_oracle(tmp_path, monkeypatch, capsys, "SUBFLEET_FAKE_LANE_NINE")


# --- review r1 of #248 (D-ENV2, fix round 1): each P3's named regression kills its mutant ---


def test_named_ignored_hook_regression_kills_dropping_the_ignored_hooks(tmp_path, monkeypatch):
    """P3-1: the daemon remembers no ignored row's hook, so neither withheld set
    names it, and the hook reaches codex-1's launch and claude-9's own again."""
    monkeypatch.setattr(daemon_module, "ignored_hooks", lambda found: frozenset())
    with pytest.raises(AssertionError, match="a withheld set misses the hook"):
        ignored_hook_oracle(tmp_path, monkeypatch, "ignored-row")


def test_named_ignored_hook_regression_kills_the_timers_withholding_the_store_alone(tmp_path, monkeypatch):
    """P3-1, the second set: the timers' own launches withhold the stored lanes'
    names alone again, as before the fix, while the daemon's set is right."""
    from subfleet import timers
    from subfleet.credentials import withheld_references
    monkeypatch.setattr(timers.Timers, "_provider_environment", lambda self: ProviderEnvironment(
        self.policy, references=lambda: withheld_references(self.store.list_lanes())))
    with pytest.raises(AssertionError, match="a withheld set misses the hook"):
        ignored_hook_oracle(tmp_path, monkeypatch, "ignored-row")


class JsonRowsUnscrubbed(ast.NodeTransformer):
    """P3-2's mutant: `daemon start --check --json` lists lanes.json's rows as read,
    unscrubbed; the text listing still scrubs."""
    found = False

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == "emit":
            for inner in ast.walk(node):
                if isinstance(inner, ast.comprehension) and isinstance(inner.iter, ast.Name) and inner.iter.id == "rows":
                    inner.iter = ast.Name("found", ast.Load())
                    self.found = True
        return node


@pytest.mark.parametrize("variable", ["HOME", "SUBFLEET_FAKE_LANE_EIGHT"])
def test_named_check_scrub_regression_kills_an_unscrubbed_json_listing(tmp_path, monkeypatch, capsys, variable):
    """P3-2: the install check's `--json` names a token-shaped lane id raw."""
    monkeypatch.setattr(cli, "_daemon_start_check", rewritten(cli._daemon_start_check, JsonRowsUnscrubbed()))
    with pytest.raises(AssertionError, match="printed the lane id unscrubbed"):
        check_scrub_oracle(tmp_path, monkeypatch, capsys, "json", variable)


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "apply"])
def test_named_transfer_repair_regression_kills_writing_only_owner(tmp_path, monkeypatch, dry_run):
    """P3-3: a transfer that sets only `owner` on the lane's existing row, as
    before the fix, leaves the refused row a rebuilt store would refuse."""
    from subfleet import lanes_transfer
    monkeypatch.setattr(lanes_transfer, "_repair_refused_row", lambda row, lane, home: None)
    with repair_root(tmp_path) as world, pytest.raises(AssertionError):
        transfer_repair_oracle(world, "v1", dry_run)


# --- review r2 of #248 (D-ENV2, P3-2): each decision's named regression kills its mutant ---


def mutant_code(function, transformer):
    """`function`'s own code recompiled from its source after `transformer` rewrites
    it. Swapped into the live function object (`__code__`), the mutant reaches every
    caller, including those that imported the function by name."""
    function = inspect.unwrap(function)
    tree = transformer.visit(ast.parse(textwrap.dedent(inspect.getsource(function))))
    assert transformer.found, "mutation did not find its site"
    tree.body[0].decorator_list = []
    ast.increment_lineno(tree, function.__code__.co_firstlineno - 1)    # tracebacks name the file's lines
    ast.fix_missing_locations(tree)
    namespace = dict(function.__globals__)
    exec(compile(tree, inspect.getfile(function), "exec", flags=__future__.annotations.compiler_flag), namespace)
    return namespace[function.__name__].__code__


class ScopeNamesDropped(ast.NodeTransformer):
    """Decision 2's mutant: `build_environment` withholds its argument alone, as at release."""
    found = False

    def visit_Assign(self, node):
        if (any(isinstance(target, ast.Name) and target.id == "withheld" for target in node.targets)
                and isinstance(node.value, ast.BinOp)):
            node.value = node.value.left
            self.found = True
        return node


class CodexWithheldDropped(ast.NodeTransformer):
    """Decision 3's mutant: the isolated branch selects with no withheld names, as at release."""
    found = False

    def visit_Call(self, node):
        self.generic_visit(node)
        if (isinstance(node.func, ast.Name) and node.func.id == "build_environment"
                and any(keyword.arg == "withheld" for keyword in node.keywords)):
            node.keywords = [keyword for keyword in node.keywords if keyword.arg != "withheld"]
            self.found = True
        return node


class NamesReadAtEntry(ast.NodeTransformer):
    """I3's mutant: the scope reads its withheld names once, when it opens."""
    found = False

    def visit_Tuple(self, node):
        self.generic_visit(node)
        if len(node.elts) == 1 and isinstance(node.elts[0], ast.Name) and node.elts[0].id == "withheld":
            node.elts = [ast.parse("lambda names=frozenset(withheld()): names", mode="eval").body]
            self.found = True
        return node


class ScopeNeverReset(ast.NodeTransformer):
    """I4's mutant: the scope's withheld names are never reset on exit."""
    found = False

    def visit_Try(self, node):
        self.generic_visit(node)
        kept = [statement for statement in node.finalbody if "_HELPER_WITHHELD.reset" not in ast.unparse(statement)]
        if len(kept) < len(node.finalbody):
            node.finalbody, self.found = kept, True
        return node


@pytest.mark.parametrize("family", ["salvage", "retention_git", "gate-merge", "conversation-catalog", "wakes"])
def test_named_helper_scope_regression_kills_dropping_the_scopes_names(tmp_path, monkeypatch, family):
    """P3-2, decision 2: `build_environment` withholds its `withheld` argument
    alone again, so each helper family under the daemon's scope inherits the held
    lane's hook. The control, unmutated, passes first."""
    from tests.unit.test_launch_env_round8 import (
        held_hook_daemon, test_each_helper_family_withholds_the_daemons_names_under_its_scope as family_oracle,
    )
    with held_hook_daemon(tmp_path) as service:
        family_oracle(service, tmp_path, monkeypatch, family)
        monkeypatch.setattr(launch_env.build_environment, "__code__",
                            mutant_code(launch_env.build_environment, ScopeNamesDropped()))
        with pytest.raises(AssertionError, match="withheld hook reached a helper child"):
            family_oracle(service, tmp_path, monkeypatch, family)


@pytest.mark.parametrize("case", ["held-lane", "ignored-row"])
def test_named_codex_inspection_regression_kills_dropping_its_explicit_withheld(tmp_path, monkeypatch, case):
    """P3-2, decision 3: the isolated branch selects with no withheld names again.
    Outside every scope the inspection then receives the hook (review r2's
    result); inside the daemon's worker scope the scope alone still covers it."""
    from tests.fake.test_launch_env_helper_scope import (
        test_review_r2_the_isolated_inspection_receives_no_withheld_hook as inspection_oracle,
    )
    for name in ("control", "scoped", "mutant"):
        (tmp_path / name).mkdir()
    inspection_oracle(tmp_path / "control", monkeypatch, case, "no-scope")
    monkeypatch.setattr(CodexAdapter._launch, "__code__", mutant_code(CodexAdapter._launch, CodexWithheldDropped()))
    inspection_oracle(tmp_path / "scoped", monkeypatch, case, "worker")
    with pytest.raises(AssertionError, match="the inspection received the withheld hook"):
        inspection_oracle(tmp_path / "mutant", monkeypatch, case, "no-scope")


@pytest.mark.parametrize("oracle", ["changing-set", "lane-held-later"])
def test_named_scope_regression_kills_reading_the_names_once_at_entry(tmp_path, monkeypatch, oracle):
    """I3: a scope that reads its names when it opens misses a set that changes
    in it, and a lane held after it opened."""
    from tests.unit import test_launch_env_round8 as round8

    def run(directory):
        directory.mkdir()
        if oracle == "changing-set":
            round8.test_i3_the_scope_reads_its_names_at_each_call()
        else:
            round8.test_i3_a_lane_held_after_the_scope_opened_is_withheld(directory, monkeypatch)

    run(tmp_path / "control")
    monkeypatch.setattr(inspect.unwrap(launch_env.helper_policy), "__code__",
                        mutant_code(launch_env.helper_policy, NamesReadAtEntry()))
    with pytest.raises(AssertionError, match="read its names when it opened|the lane held later is covered"):
        run(tmp_path / "mutant")


def test_named_scope_regression_kills_never_resetting_the_names(monkeypatch):
    """I4: a scope that never resets its names leaks them past its exit. The
    mutant runs in a copied context, so its leak cannot reach later tests."""
    import contextvars
    from tests.unit.test_launch_env_round8 import test_i4_an_exception_inside_a_scope_leaks_no_names as oracle
    oracle()
    monkeypatch.setattr(inspect.unwrap(launch_env.helper_policy), "__code__",
                        mutant_code(launch_env.helper_policy, ScopeNeverReset()))
    with pytest.raises(AssertionError, match="outlived it"):
        contextvars.copy_context().run(oracle)
    assert launch_env._HELPER_WITHHELD.get() == ()


# --- review r1 of #262: opt-outs the helper-scope lint refuses, and the reviewer's M5 and M7 ------

WAKES_ENTRY = 'helper_policy(getattr(daemon, "policy", {}), withheld=getattr(daemon, "_helper_withheld", None))'

#: Source mutants of the wakes scope entry (`WakeEngine._poll_prs`), each an edit
#: of the real `wakes.py`: review r1's M6 (the keyword dropped) and C2
#: (`withheld=None`), then one per rule P3-1 added. `**` keywords cannot opt out
#: beside `withheld=` today (a second `withheld` raises), but the lint cannot read
#: them, so it refuses them. Each maps to its edits and the site the lint names.
WAKES_OPT_OUTS = {
    "M6-no-keyword": ([(WAKES_ENTRY, 'helper_policy(getattr(daemon, "policy", {}))')], "WakeEngine._poll_prs"),
    "C2-withheld-none": ([(WAKES_ENTRY, 'helper_policy(getattr(daemon, "policy", {}), withheld=None)')],
                         "WakeEngine._poll_prs"),
    "double-star": ([(WAKES_ENTRY, WAKES_ENTRY[:-1] + ', **getattr(daemon, "scope_options", {}))')],
                    "WakeEngine._poll_prs"),
    "import-as": ([("import build_environment, helper_policy\n", "import build_environment, helper_policy as scope\n"),
                   (WAKES_ENTRY, 'scope(getattr(daemon, "policy", {}))')], ""),
}


@pytest.mark.parametrize("mutant", sorted(WAKES_OPT_OUTS))
def test_named_scope_lint_regression_kills_an_opted_out_wakes_entry(tmp_path, mutant):
    """Review r1 of #262, P3-1: the lint itself refuses each opt-out at the wakes
    entry. It reads each module on its own and unites what it refuses, so a
    package of the real `wakes.py` alone stands for the whole one: the control,
    unedited, is refused nowhere, and the mutant is refused where it is written,
    which is exactly when `test_every_helper_scope_passes_the_daemons_withheld_names`
    fails on the package."""
    from tests.unit import test_launch_env_round8 as round8
    package = tmp_path / "subfleet"
    wakes = package / "conversations" / "wakes.py"
    wakes.parent.mkdir(parents=True)
    source = (round8.PACKAGE / "conversations" / "wakes.py").read_text()
    wakes.write_text(source)
    assert round8.scopes_without_the_daemons_set(package) == set()
    edits, function = WAKES_OPT_OUTS[mutant]
    for old, new in edits:
        assert source.count(old) == 1, "mutation did not find its site"
        source = source.replace(old, new)
    wakes.write_text(source)
    assert round8.scopes_without_the_daemons_set(package) == {("subfleet/conversations/wakes.py", function)}


class InnerScopeReplacesOuter(ast.NodeTransformer):
    """Review r1 of #262's M5: an inner scope's withheld names replace the outer ones."""
    found = False

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if isinstance(node.left, ast.Name) and node.left.id == "outer":
            self.found = True
            return node.right
        return node


def test_named_scope_regression_kills_an_inner_scope_replacing_the_outer_names(monkeypatch):
    """I4 (review r1's M5): the gate's scope inside `Daemon.dispatch`, or `preview`
    inside the gate's, would drop the names of the scope around it."""
    from tests.unit.test_launch_env_round8 import test_i4_an_exception_inside_a_scope_leaks_no_names as oracle
    oracle()
    monkeypatch.setattr(inspect.unwrap(launch_env.helper_policy), "__code__",
                        mutant_code(launch_env.helper_policy, InnerScopeReplacesOuter()))
    with pytest.raises(AssertionError):
        oracle()
    assert launch_env._HELPER_WITHHELD.get() == ()


@pytest.mark.parametrize("entry", ["conversation-resume", "daemon-dispatch", "daemon-worker", "gate-dispatch",
                                   "wakes-poll"])
def test_named_scope_entry_regression_kills_an_empty_daemon_set(tmp_path, monkeypatch, entry):
    """Review r1 of #262's M7: `Daemon._helper_withheld` returns nothing, so each
    daemon scope entry passes the held lane's hook. The timers entry is not here:
    it reads `Timers._withheld`, which the mutant leaves alone."""
    from tests.unit.test_launch_env_round8 import (
        held_hook_daemon, test_each_daemon_scope_entry_withholds_the_daemons_names as entry_oracle,
    )
    with held_hook_daemon(tmp_path) as service:
        entry_oracle(service, monkeypatch, entry)
        monkeypatch.setattr(Daemon, "_helper_withheld", lambda self: frozenset())
        with pytest.raises(AssertionError):
            entry_oracle(service, monkeypatch, entry)
