"""Review r2 of #248 (D-ENV2), P3-2: every helper subprocess the daemon starts
withholds the daemon's names.

The daemon's launches withheld every lane's credential reference and startup's
ignored hooks, but its helpers (salvage, retention_git, gate merge, the
conversation catalog, wakes) and the Codex isolated-review inspection called
`build_environment()` with no withheld list, so a harness hook stored as a
lane's credential, or an ignored lanes.json row's hook, reached them. A helper
scope (`launch_env.helper_policy`) now carries the daemon's withheld names
beside its policy, as a callable that `build_environment` reads at each call.

Invariants, each tested below:
- I1: inside a helper scope with withheld set W, for every parent environment
  E, additions A and explicit `withheld` X, `build_environment(A, environ=E,
  withheld=X)` has no key in (W | X) - keys(A).
- I2: outside any scope `build_environment` is release's, differentially
  (tests/fixtures/launch_env_release_217_env2.py).
- I3: the scope reads W at each call, not at entry.
- I4: nested scopes restore the outer W on exit, and an exception inside a
  scope leaks no W.

The daemon-level regression with review r2's executed input (the real
`CodexAdapter._launch`, a fake `codex` binary) is
tests/fake/test_launch_env_helper_scope.py. Every value is a dummy, and
failures name variables, never values. The named mutations are in
tests/unit/test_launch_env_mutations.py.
"""

import ast
import contextlib
import os
from pathlib import Path
import textwrap
import threading
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import launch_env, retention_git, salvage, uncommitted
from subfleet.conversations import catalog, wakes
from subfleet.conversations.service import ConversationService
from subfleet.daemon import Daemon
from subfleet.gate import merge
from subfleet.gate import service as gate_service
from subfleet.provider_env import ProviderEnvironment
from subfleet.sessions import handoff, nudge
from subfleet.store import Store
from subfleet.timers import Timers
from tests.fixtures.launch_env_release_217_env2 import release_build_environment
from tests.unit.test_launch_env_allowlist import (
    ALLOWED, NAMES, PARENT_NAMES, VALUES, ParentEnvironment, assert_environment,
)
from tests.unit.test_launch_env_round3 import env_lane

PACKAGE = Path(launch_env.__file__).resolve().parent
RELEASE = release_build_environment()

# Review r2's hook: claude-9's stored credential, so the lane is held and its
# hook withheld from every launch (`credentials.withheld_references`).
HOOK = "SUBFLEET_FAKE_LANE_EIGHT"
# A hook no lane names: every child still inherits it, so a recorder that never
# sees a hook cannot pass.
CONTROL = "SUBFLEET_FAKE_LANE_SEVEN"
PARENT = {"PATH": "/usr/bin:/bin", "HOME": "/fake/home", HOOK: "SECRET7-eight", CONTROL: "SECRET7-seven"}
HOOKS = (HOOK, CONTROL, "SUBFLEET_FAKE_LANE_NINE", "SUBFLEET_E2E_LANE_SIX")
# Names a lane may hold or a launch adds: hooks, listed names, credential words.
LANE_NAMES = HOOKS + ("SUBFLEET_TEST_OAUTH", "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_HOME", "HOME", "PATH", "GH_TOKEN")


def kept_names(env, names) -> list[str]:
    """Which of `names` an environment carries: names only, never a value."""
    return sorted(name for name in names if name in env)


# --- I1 to I4, at launch_env itself ------------------------------------------------------------

VALID = st.one_of(NAMES, st.sampled_from(sorted(ALLOWED)), st.sampled_from(LANE_NAMES))
DUMMY = VALUES.map(lambda value: "SECRET7" + value)
ENVIRONMENTS = st.dictionaries(st.one_of(PARENT_NAMES, VALID), DUMMY, max_size=25).map(ParentEnvironment)
ADDITIONS = st.dictionaries(VALID, DUMMY, max_size=4).map(ParentEnvironment)
NAME_SETS = st.frozensets(VALID, max_size=5)


def scopes(stack, withheld_sets, policy_pass=()):
    """Open one helper scope per set, outermost first; the innermost carries `policy_pass`."""
    for index, names in enumerate(withheld_sets):
        policy = {"env": {"pass": list(policy_pass)}} if index == len(withheld_sets) - 1 else {}
        stack.enter_context(launch_env.helper_policy(policy, withheld=lambda names=names: names))


@settings(max_examples=150, deadline=None, derandomize=True, database=None)
@given(parent=ENVIRONMENTS, additions=ADDITIONS, explicit=NAME_SETS,
       withheld_sets=st.lists(NAME_SETS, min_size=1, max_size=3),
       passed=st.one_of(st.none(), st.lists(VALID, max_size=4)),
       policy_pass=st.lists(VALID, max_size=3), removals=st.lists(VALID, max_size=3))
def test_i1_a_helper_scope_withholds_its_names_from_every_parent(parent, additions, explicit, withheld_sets,
                                                                 passed, policy_pass, removals):
    """I1, and exactly: inside nested scopes withholding W, a build equals the same
    build outside any scope with W added to its explicit `withheld`."""
    scope_names = frozenset().union(*withheld_sets)
    with contextlib.ExitStack() as stack:
        scopes(stack, withheld_sets, policy_pass)
        built = launch_env.build_environment(additions, removals, pass_names=passed, environ=parent,
                                             withheld=explicit)
    leaked = set(built) & ((scope_names | explicit) - set(additions))
    assert not leaked, f"withheld names reached the child: {sorted(leaked)}"
    assert_environment(built, launch_env.build_environment(
        additions, removals, pass_names=policy_pass if passed is None else passed, environ=parent,
        withheld=scope_names | explicit))


@settings(max_examples=150, deadline=None, derandomize=True, database=None)
@given(parent=ENVIRONMENTS, additions=ADDITIONS, explicit=NAME_SETS,
       passed=st.one_of(st.none(), st.lists(st.one_of(VALID, PARENT_NAMES), max_size=4)),
       policy_pass=st.lists(VALID, max_size=3), removals=st.lists(VALID, max_size=3))
def test_i2_outside_any_scope_build_environment_is_releases(parent, additions, explicit, passed,
                                                            policy_pass, removals):
    """I2: with no scope open, and in a scope opened without withheld names (the
    CLI's `gate --dry-run`), every build is release's, refusals included."""
    assert launch_env._HELPER_WITHHELD.get() == (), "a scope leaked into this test"

    def outcome(build):
        try:
            return build(additions, removals, pass_names=passed, environ=parent, withheld=explicit)
        except ValueError as exc:
            return ("refused", str(exc))

    for opened in (contextlib.nullcontext(), launch_env.helper_policy({"env": {"pass": policy_pass}})):
        with opened:
            actual, expected = outcome(launch_env.build_environment), outcome(RELEASE)
        if isinstance(expected, tuple):
            assert actual == expected
        else:
            assert_environment(actual, expected)


def test_i2_daemon_start_still_receives_every_lane_variable(monkeypatch):
    """C-12.3: `daemon start`'s parent runs in no scope, so the lane variables it
    passes by name reach the daemon; the same call in a daemon scope withholds them."""
    monkeypatch.setattr(os, "environ", {**PARENT, "SUBFLEET_TEST_OAUTH": "SECRET7-oauth"})
    start = launch_env.build_environment({"SUBFLEET_HOME": "/fake/root"}, pass_names=("SUBFLEET_TEST_OAUTH",))
    assert kept_names(start, ("SUBFLEET_TEST_OAUTH", HOOK)) == sorted(("SUBFLEET_TEST_OAUTH", HOOK))
    with launch_env.helper_policy({}, withheld=lambda: {"SUBFLEET_TEST_OAUTH", HOOK}):
        scoped = launch_env.build_environment({"SUBFLEET_HOME": "/fake/root"}, pass_names=("SUBFLEET_TEST_OAUTH",))
    assert kept_names(scoped, ("SUBFLEET_TEST_OAUTH", HOOK)) == []


def test_i3_the_scope_reads_its_names_at_each_call():
    """I3: a set that changes between two calls in one scope is read anew at each,
    and not at all when the scope opens."""
    current, reads = {"names": frozenset()}, []

    def source():
        reads.append(current["names"])
        return current["names"]

    parent = {"PATH": "/usr/bin:/bin", HOOK: "SECRET7-eight", CONTROL: "SECRET7-seven"}
    with launch_env.helper_policy({}, withheld=source):
        assert reads == [], "the scope read its names when it opened"
        current["names"] = frozenset({HOOK})
        first = launch_env.build_environment(environ=parent)
        current["names"] = frozenset({CONTROL})
        second = launch_env.build_environment(environ=parent)
    assert kept_names(first, (HOOK, CONTROL)) == [CONTROL], "the first call's set"
    assert kept_names(second, (HOOK, CONTROL)) == [HOOK], "the second call's set, read anew"
    assert len(reads) == 2


def test_i3_a_lane_held_after_the_scope_opened_is_withheld(tmp_path, monkeypatch):
    """I3 at the daemon: claude-9 stored on the hook mid-operation, inside one
    `_run_worker` call, is withheld from that operation's next helper."""
    monkeypatch.setattr(os, "environ", dict(PARENT))
    with Store(tmp_path / "state.sqlite3") as store:
        daemon = Daemon.__new__(Daemon)
        daemon.policy, daemon.store = {}, store
        daemon._provider_environment = ProviderEnvironment({}, references=daemon._withheld_references)

        def operation():
            before = launch_env.build_environment()
            store.put_lane(env_lane("claude-9", HOOK))
            return before, launch_env.build_environment()

        before, after = daemon._run_worker(operation)
    assert kept_names(before, (HOOK, CONTROL)) == [HOOK, CONTROL]
    assert kept_names(after, (HOOK, CONTROL)) == [CONTROL], "the lane held later is covered"


def test_a_scope_repeating_an_outer_scopes_source_reads_it_once_per_build():
    """Review r1 of #262: a scope whose source an enclosing scope already carries
    (the gate's inside `Daemon.dispatch`, `dispatch` inside `_run_worker`) adds no
    second read of it; a different source is still read, and both are withheld."""
    reads = []

    def daemon_set():
        reads.append("daemon")
        return {HOOK}

    def other_set():
        reads.append("other")
        return {CONTROL}

    parent = {"PATH": "/usr/bin:/bin", HOOK: "SECRET7-eight", CONTROL: "SECRET7-seven"}
    with launch_env.helper_policy({}, withheld=daemon_set), launch_env.helper_policy({}, withheld=daemon_set):
        assert kept_names(launch_env.build_environment(environ=parent), (HOOK, CONTROL)) == [CONTROL]
        assert reads == ["daemon"], "the repeated source was read twice"
        with launch_env.helper_policy({}, withheld=other_set):
            assert kept_names(launch_env.build_environment(environ=parent), (HOOK, CONTROL)) == []
        assert kept_names(launch_env.build_environment(environ=parent), (HOOK, CONTROL)) == [CONTROL]
    assert reads == ["daemon", "daemon", "other", "daemon"]
    assert launch_env._HELPER_WITHHELD.get() == ()


class Boom(Exception):
    pass


def withheld_now(parent) -> frozenset[str]:
    """The parent names a build drops now: with only hooks there, the scope's names."""
    return frozenset(parent) - set(launch_env.build_environment(environ=parent))


def nest(levels, depth, parent):
    """Open `levels[depth]` inside the ones before it, then check that its exit,
    raised through or not, leaves exactly the outer names withheld."""
    if depth == len(levels):
        return
    names, raises = levels[depth]
    outer = frozenset().union(*(level for level, _ in levels[:depth]))
    passed = launch_env._HELPER_PASS.get()
    with contextlib.suppress(Boom):
        with launch_env.helper_policy({"env": {"pass": [f"LEVEL_{depth}"]}}, withheld=lambda: names):
            assert withheld_now(parent) == outer | names
            nest(levels, depth + 1, parent)
            assert withheld_now(parent) == outer | names, "an inner scope's exit dropped this one's names"
            if raises:
                raise Boom
    assert withheld_now(parent) == outer, "a scope's names outlived it"
    assert launch_env._HELPER_PASS.get() == passed, "a scope's policy outlived it"


@settings(max_examples=100, deadline=None, derandomize=True, database=None)
@given(levels=st.lists(st.tuples(st.frozensets(st.sampled_from(HOOKS), max_size=3), st.booleans()),
                       min_size=1, max_size=4))
def test_i4_nested_scopes_restore_the_outer_names_even_through_an_exception(levels):
    """I4: every exit, normal or by an exception, restores the outer scope's names."""
    parent = dict.fromkeys(HOOKS, "SECRET7-hook")
    nest(levels, 0, parent)
    assert withheld_now(parent) == frozenset()
    assert launch_env._HELPER_WITHHELD.get() == ()


def test_i4_an_exception_inside_a_scope_leaks_no_names():
    """I4 by example: an inner scope left by an exception, then the outer one,
    each give back exactly what was withheld before it opened."""
    parent = {"PATH": "/usr/bin:/bin", HOOK: "SECRET7-eight", CONTROL: "SECRET7-seven"}
    with launch_env.helper_policy({}, withheld=lambda: {CONTROL}):
        with pytest.raises(Boom), launch_env.helper_policy({}, withheld=lambda: {HOOK}):
            assert kept_names(launch_env.build_environment(environ=parent), (HOOK, CONTROL)) == []
            raise Boom
        assert kept_names(launch_env.build_environment(environ=parent), (HOOK, CONTROL)) == [HOOK], \
            "the inner scope's names outlived it"
    assert kept_names(launch_env.build_environment(environ=parent), (HOOK, CONTROL)) == [HOOK, CONTROL], \
        "the outer scope's names outlived it"


def test_i4_a_scope_on_one_thread_withholds_nothing_on_another():
    """ContextVars are per thread: a pool thread opens its own scope (`_run_worker`)."""
    parent = {"PATH": "/usr/bin:/bin", HOOK: "SECRET7-eight"}
    seen = {}
    with launch_env.helper_policy({}, withheld=lambda: {HOOK}):
        worker = threading.Thread(target=lambda: seen.update(other=launch_env.build_environment(environ=parent)))
        worker.start()
        worker.join()
        here = launch_env.build_environment(environ=parent)
    assert kept_names(seen["other"], (HOOK,)) == [HOOK]
    assert kept_names(here, (HOOK,)) == []


# --- the daemon's scope entries ------------------------------------------------------------------


@contextlib.contextmanager
def held_hook_daemon(directory):
    """A hand-built daemon whose store holds claude-9 on the hook (held), and
    whose withheld set is its launches' (`_withheld_references`)."""
    with Store(directory / "state.sqlite3") as store:
        store.put_lane(env_lane("claude-9", HOOK))
        service = Daemon.__new__(Daemon)
        service.policy, service.store = {}, store
        service._provider_environment = ProviderEnvironment({}, references=service._withheld_references)
        yield service


@pytest.fixture
def daemon(tmp_path):
    with held_hook_daemon(tmp_path) as service:
        yield service


def dispatch_entry(service, capture):
    """`Daemon.dispatch`'s submit, the scope a policy review's uncommitted copy runs
    in (`Daemon.submit`, then `uncommitted.plan`)."""
    seen = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(service, "submit", lambda args: seen.append(capture()) or {})
        service.dispatch("submit", {"request_id": "fake", "kind": "dispatch", "workdir": "/fake/repo",
                                    "prompt_path": "/fake/prompt.md", "sandbox": "policy"})
    return seen[0]


def gate_entry(service, capture):
    """`gate.service.dispatch`, the gate's scope entry, around a helper."""
    seen = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(gate_service, "_dispatch", lambda daemon, op, args: seen.append(capture()) or {})
        gate_service.dispatch(service, "gate.status", {})
    return seen[0]


def wakes_entry(service, capture):
    """`WakeEngine._poll_prs`, the PR poller thread's scope entry."""
    engine = wakes.WakeEngine.__new__(wakes.WakeEngine)
    engine.service = SimpleNamespace(daemon=service)
    seen = []
    engine._query_pending_prs = lambda pending: seen.append(capture())
    engine._poll_prs([])
    return seen[0]


def resume_entry(service, capture):
    """`ConversationService._resume_start`, the file pool's scope entry."""
    conversations = ConversationService.__new__(ConversationService)
    conversations.daemon = service
    seen = []
    conversations._resume_start_scoped = lambda sid, tries, began: seen.append(capture())
    conversations._resume_start("fake-suggestion", 1, 0.0)
    return seen[0]


def timers_entry(service, capture):
    """`Timers._run_worker`, every timer pool's scope entry, with the daemon's set."""
    timer = Timers.__new__(Timers)
    timer.policy, timer.store, timer.references = {}, service.store, service._withheld_references
    return timer._run_worker(capture)


ENTRIES = {
    "daemon-worker": lambda service, capture: service._run_worker(capture),
    "daemon-dispatch": dispatch_entry,
    "timers": timers_entry,
    "conversation-resume": resume_entry,
    "wakes-poll": wakes_entry,
    "gate-dispatch": gate_entry,
}


@pytest.mark.parametrize("entry", sorted(ENTRIES))
def test_each_daemon_scope_entry_withholds_the_daemons_names(daemon, monkeypatch, entry):
    """Each entry that opens a helper scope for the daemon passes its withheld set,
    so a held lane's hook reaches no helper while a hook no lane names still does."""
    monkeypatch.setattr(os, "environ", dict(PARENT))
    built = ENTRIES[entry](daemon, launch_env.build_environment)
    assert kept_names(built, (HOOK, CONTROL)) == [CONTROL]
    assert kept_names(launch_env.build_environment(), (HOOK, CONTROL)) == [HOOK, CONTROL], "outside, unchanged"


def test_dispatch_and_the_control_loop_open_the_same_scope_as_the_workers():
    """`Daemon.dispatch` and `_control` are `_helper_environment`'s, as `_run_worker` is."""
    scoped = Daemon._run_worker.__code__
    assert Daemon.dispatch.__code__ is scoped and Daemon._control.__code__ is scoped
    assert "_helper_withheld" in scoped.co_names


def test_timers_withhold_the_stored_lanes_without_the_daemons_set(tmp_path, monkeypatch):
    """Timers built without the daemon's callable fall back to the stored lanes'."""
    monkeypatch.setattr(os, "environ", dict(PARENT))
    with Store(tmp_path / "state.sqlite3") as store:
        store.put_lane(env_lane("claude-9", HOOK))
        timer = Timers.__new__(Timers)
        timer.policy, timer.store, timer.references = {}, store, None
        assert kept_names(timer._run_worker(launch_env.build_environment), (HOOK, CONTROL)) == [CONTROL]


def test_the_gate_under_daemon_dispatch_reads_the_lanes_once_per_build(daemon, monkeypatch):
    """Review r1 of #262: `gate.service.dispatch` inside `Daemon.dispatch` opens its
    scope with the same source, `Daemon._helper_withheld`, so a helper build there
    reads the stored lanes once, not once per scope."""
    monkeypatch.setattr(os, "environ", dict(PARENT))
    reads, list_lanes = [], daemon.store.list_lanes
    monkeypatch.setattr(daemon.store, "list_lanes", lambda: reads.append("list_lanes") or list_lanes())
    built = []
    monkeypatch.setattr(gate_service, "_dispatch",
                        lambda service, op, args: built.append(launch_env.build_environment()) or {})
    daemon.dispatch("gate.poll", {"gate_id": "fake"})
    assert kept_names(built[0], (HOOK, CONTROL)) == [CONTROL]
    assert reads == ["list_lanes"]


# --- one test per helper family -------------------------------------------------------------------


def spy(seen, result):
    """A subprocess stand-in that keeps each child environment it is handed."""
    def run(argv, **kwargs):
        seen.append(dict(kwargs["env"]))
        return result
    return run


def salvage_family(tmp_path, patch):
    return [salvage._git_env(None)]


def retention_family(tmp_path, patch):
    return [retention_git.environment()]


def gate_merge_family(tmp_path, patch):
    seen = []
    merge.run_command(["fake-gh"], cwd=tmp_path, runner=spy(seen, SimpleNamespace(returncode=0, stdout="", stderr="")))
    return seen


def catalog_family(tmp_path, patch):
    seen = []
    patch.setattr(catalog, "refresh_running", lambda root: False)
    patch.setattr(catalog.subprocess, "Popen", spy(seen, SimpleNamespace(pid=123)))
    patch.setattr(catalog.subprocess, "run", spy(seen, SimpleNamespace(returncode=0, stdout="")))
    catalog.spawn_refresh(tmp_path)
    catalog._subfleet_owned(123)
    return seen


def wakes_family(tmp_path, patch):
    seen = []
    patch.setattr(wakes.subprocess, "run", spy(seen, SimpleNamespace(returncode=0, stdout='{"data": {}}')))
    wakes.query_prs(["owner/repo#1"])
    return seen


def nudge_family(tmp_path, patch):
    seen = []
    nudge.spawn("fake-session", source="startup", transcript=None, delay_s=0, root=tmp_path,
                popen=spy(seen, SimpleNamespace(pid=123)))
    return seen


def handoff_family(tmp_path, patch):
    seen = []
    patch.setattr(handoff.subprocess, "run", spy(seen, SimpleNamespace(returncode=0, stdout="", stderr="")))
    handoff._run_git(tmp_path, ["status"])
    return seen


def uncommitted_family(tmp_path, patch):
    """Review r1 of #262, P2-1: #221's uncommitted copy (`uncommitted._check_applies`),
    whose `read-tree` and `apply --check` each get the scratch index as their one
    addition. The index's folder is a fresh temporary one, so it is named, not compared."""
    seen, commands, scratch = [], [], tmp_path / "uncommitted-scratch"

    def git(top, *args, env=None, **kwargs):
        index = Path(env["GIT_INDEX_FILE"])
        assert index.name == "index" and index.parent.parent == scratch, "the copy's git lost its scratch index"
        commands.append(args[0])
        seen.append({**env, "GIT_INDEX_FILE": "<scratch index>"})
        return b""

    patch.setattr(uncommitted, "_git_bytes", git)
    uncommitted._check_applies(tmp_path, "0" * 40, b"fake patch", scratch, None)
    assert commands == ["read-tree", "apply"]
    return seen


# Each family, and the scope entry the daemon reaches it through. Nudge and
# handoff are never reached from the daemon (the SessionStart hook process and
# the sessions CLI, docs/reports/2026-10-24-env2-p3.md), so they run under the
# workers' scope here: were one ever reached there, it would withhold too.
FAMILIES = {
    "salvage": (salvage_family, "daemon-worker"),
    "retention_git": (retention_family, "daemon-worker"),
    "gate-merge": (gate_merge_family, "gate-dispatch"),
    "conversation-catalog": (catalog_family, "daemon-worker"),
    "wakes": (wakes_family, "wakes-poll"),
    "nudge": (nudge_family, "daemon-worker"),
    "handoff": (handoff_family, "daemon-worker"),
    "uncommitted-copy": (uncommitted_family, "daemon-dispatch"),
}


def family_environments(family, service, tmp_path, monkeypatch):
    """The child environments a family's helper builds in the daemon's scope and in
    none (the CLI), from one parent carrying the held lane's hook."""
    monkeypatch.setattr(os, "environ", dict(PARENT))
    capture, entry = FAMILIES[family]
    with pytest.MonkeyPatch.context() as patch:
        scoped = ENTRIES[entry](service, lambda: capture(tmp_path, patch))
        plain = capture(tmp_path, patch)
    assert scoped and len(scoped) == len(plain), "the helper did not reach its subprocess boundary"
    return scoped, plain


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_each_helper_family_withholds_the_daemons_names_under_its_scope(daemon, tmp_path, monkeypatch, family):
    """Review r2, P3-2: each helper family's child environment, built under the
    daemon's scope, lacks the held lane's hook and is otherwise exactly what the
    same helper builds outside any scope, where the hook is still inherited."""
    scoped, plain = family_environments(family, daemon, tmp_path, monkeypatch)
    for inside, outside in zip(scoped, plain):
        assert HOOK not in inside, "the daemon's withheld hook reached a helper child"
        assert kept_names(outside, (HOOK, CONTROL)) == [HOOK, CONTROL], "outside a scope nothing changed"
        assert_environment(inside, {name: value for name, value in outside.items() if name != HOOK})


# --- source pins: every scope passes the daemon's set; every call site is classified ---------------


def calls(name, package=None):
    """(path, enclosing function, call node) for each call of `name` in the package
    (`subfleet/`, or the scratch `package` a lint test writes)."""
    package = PACKAGE if package is None else package
    found = []
    for path in sorted(package.rglob("*.py")):
        tree, stack = ast.parse(path.read_text()), []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_ClassDef = visit_FunctionDef

            def visit_Call(self, node):
                func = node.func
                if (func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)) == name:
                    found.append((path.relative_to(package.parent).as_posix(), ".".join(stack), node))
                self.generic_visit(node)

        Visitor().visit(tree)
    return found


#: helper_policy calls that open a scope with no daemon set, and why.
SCOPES_WITHOUT_THE_DAEMONS_SET = {
    ("subfleet/gate/service.py", "preview"):
        "the CLI's `gate --dry-run` (C-19.1): no daemon, no lane store; nested in "
        "`gate.service.dispatch` in the daemon, that scope's names still hold",
}


def references(name, package=None):
    """(path, enclosing function) of each use of `name` in the package that is
    neither a direct call (`name(...)`, `module.name(...)`) nor an import under its
    own name: an `import ... as`, an assignment, an argument, an attribute read."""
    package = PACKAGE if package is None else package
    found = set()
    for path in sorted(package.rglob("*.py")):
        tree, stack, where = ast.parse(path.read_text()), [], path.relative_to(package.parent).as_posix()

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_ClassDef = visit_FunctionDef

            def visit_Call(self, node):
                # The called name itself is a direct call; what it is looked up on,
                # and every argument, are still uses.
                if isinstance(node.func, ast.Attribute):
                    self.visit(node.func.value)
                elif not isinstance(node.func, ast.Name):
                    self.visit(node.func)
                for child in (*node.args, *node.keywords):
                    self.visit(child)

            def visit_Name(self, node):
                if node.id == name:
                    found.add((where, ".".join(stack)))

            def visit_Attribute(self, node):
                if node.attr == name:
                    found.add((where, ".".join(stack)))
                self.generic_visit(node)

            def visit_alias(self, node):
                if node.name.rpartition(".")[2] == name and node.asname not in (None, name):
                    found.add((where, ".".join(stack)))

        Visitor().visit(tree)
    return found


def opts_out(call):
    """Whether a `helper_policy(...)` call can open its scope without the daemon's
    set: it has no `withheld=`, a constant one (`withheld=None`), or `**` keywords,
    whose contents the lint cannot read."""
    withheld = [keyword.value for keyword in call.keywords if keyword.arg == "withheld"]
    return (not withheld or isinstance(withheld[0], ast.Constant)
            or any(keyword.arg is None for keyword in call.keywords))


def scopes_without_the_daemons_set(package=None):
    """(path, function) of each `helper_policy` use in the package that opens, or
    can open, a scope without the daemon's withheld names (review r1 of #262, P3-1).

    The rule: every use of `helper_policy` is a direct call whose `withheld=` is
    not a constant and which has no `**` keywords. Any other use is refused where
    it is written, since the lint cannot follow the calls made through it; an
    import under its own name is the one exception."""
    return ({(path, function) for path, function, node in calls("helper_policy", package) if opts_out(node)}
            | references("helper_policy", package))


def test_every_helper_scope_passes_the_daemons_withheld_names():
    """Source lint: every `helper_policy(` call passes a `withheld=` that is not a
    constant and no `**` keywords, and is reached by its own name, or is listed
    with its reason; a listed one that starts passing it must leave the list."""
    assert scopes_without_the_daemons_set() == set(SCOPES_WITHOUT_THE_DAEMONS_SET)


def scratch_package(tmp_path, source):
    """A scratch `subfleet` package of one module, which the lint reads as it reads the real one."""
    package = tmp_path / "subfleet"
    package.mkdir()
    (package / "scratch.py").write_text(textwrap.dedent(source))
    return package


#: The real entries' spellings, which the lint accepts. Each refusal test below
#: puts its opt-out beside them, so a lint that refused every scope could not pass.
ACCEPTED = '''
    from . import launch_env
    from .launch_env import helper_policy

    def bound(self):
        with helper_policy(getattr(self, "policy", {}), withheld=self._helper_withheld):
            pass

    def looked_up(daemon):
        with helper_policy(daemon.policy, withheld=getattr(daemon, "_helper_withheld", None)):
            pass

    def through_the_module(timers):
        with launch_env.helper_policy(timers.policy, withheld=timers._withheld):
            pass
'''


def test_the_lint_accepts_the_daemons_own_spellings(tmp_path):
    """The control: a `withheld=` the lint cannot evaluate, `getattr(..., None)`
    included, is not an opt-out."""
    assert scopes_without_the_daemons_set(scratch_package(tmp_path, ACCEPTED)) == set()


def test_the_lint_refuses_withheld_none(tmp_path):
    """Review r1 of #262, P3-1: `withheld=None` opens a scope without the daemon's
    set, exactly as a missing keyword does, so the lint refuses it the same way."""
    package = scratch_package(tmp_path, ACCEPTED + '''
    def opted_out(policy):
        with helper_policy(policy, withheld=None):
            pass
    ''')
    assert scopes_without_the_daemons_set(package) == {("subfleet/scratch.py", "opted_out")}


def test_the_lint_refuses_double_star_keywords(tmp_path):
    """P3-1: a call with `**` keywords is refused, with or without `withheld=`
    beside them, since the lint cannot read what they hold."""
    package = scratch_package(tmp_path, ACCEPTED + '''
    def spread(policy, options):
        with helper_policy(policy, **options):
            pass

    def beside(daemon, options):
        with helper_policy(daemon.policy, withheld=daemon._helper_withheld, **options):
            pass
    ''')
    assert scopes_without_the_daemons_set(package) == {("subfleet/scratch.py", "spread"),
                                                       ("subfleet/scratch.py", "beside")}


#: Uses of `helper_policy` the lint cannot follow to their calls, and the function
#: each is refused in ("" for a module-level import).
ALIASED = {
    "import-as": ("", '''
    from .launch_env import helper_policy as scope

    def aliased(policy):
        with scope(policy):
            pass
    '''),
    "assignment": ("aliased", '''
    def aliased(policy):
        scope = helper_policy
        with scope(policy):
            pass
    '''),
    "module-attribute": ("aliased", '''
    def aliased(policy):
        scope = launch_env.helper_policy
        with scope(policy):
            pass
    '''),
    "argument": ("aliased", '''
    import functools

    def aliased(policy):
        with functools.partial(helper_policy, policy)():
            pass
    '''),
}


@pytest.mark.parametrize("alias", sorted(ALIASED))
def test_the_lint_refuses_helper_policy_through_an_alias(tmp_path, alias):
    """P3-1: a use of `helper_policy` other than a direct call or an import under
    its own name (an `import ... as`, an assignment, an argument) is refused where
    it is written, since the calls made through it cannot be followed."""
    function, source = ALIASED[alias]
    package = scratch_package(tmp_path, ACCEPTED + source)
    assert scopes_without_the_daemons_set(package) == {("subfleet/scratch.py", function)}


SCOPE = "daemon: only inside a helper scope ({})"
CLI = "no lane store: {}"
#: Every `build_environment(` call in the package, by enclosing function, with
#: whether the daemon runs it inside a helper scope (after review r2's P3-2) and
#: how else it is covered. The audit is in docs/reports/2026-10-24-env2-p3.md.
CALL_SITES = {
    ("subfleet/adapters/claude.py", "_keychain_blob"): SCOPE.format("Timers._run_worker, Daemon._run_worker, dispatch"),
    ("subfleet/adapters/claude.py", "ClaudeAdapter._runner_environment"):
        SCOPE.format("dispatch, Daemon._run_worker") + "; " + CLI.format("doctor --live"),
    ("subfleet/adapters/codex.py", "CodexAdapter._launch"):
        "explicit withheld=; " + SCOPE.format("Daemon._run_worker"),
    ("subfleet/adapters/isolation.py", "inspect_codex"):
        "parent is CodexAdapter._launch's withheld env; " + SCOPE.format("Daemon._run_worker"),
    ("subfleet/cli.py", "daemon_env"): CLI.format("daemon start's parent, which passes every lane variable (C-12.3)"),
    ("subfleet/cli.py", "cmd_daemon_install"): CLI.format("daemon install's launchctl"),
    ("subfleet/compat.py", "delegate"): CLI.format("v1 delegation"),
    ("subfleet/conversations/catalog.py", "_outside_claude"):
        SCOPE.format("Daemon._run_worker") + "; the catalog child, whose parent spawn_refresh built in scope",
    ("subfleet/conversations/catalog.py", "_subfleet_owned"):
        SCOPE.format("Daemon._run_worker") + "; the catalog child; " + CLI.format("the importer"),
    ("subfleet/conversations/catalog.py", "spawn_refresh"): SCOPE.format("Daemon._run_worker"),
    ("subfleet/conversations/service.py", "ConversationService._cut_worktree.git"):
        SCOPE.format("Daemon._run_worker, ConversationService._resume_start"),
    ("subfleet/conversations/wakes.py", "query_prs"): SCOPE.format("WakeEngine._poll_prs"),
    ("subfleet/credentials.py", "resolve_credential"): SCOPE.format("Timers._run_worker, Daemon._run_worker"),
    ("subfleet/descriptors.py", "kernel_open_files_ceiling"): CLI.format("daemon install, doctor"),
    ("subfleet/doctor.py", "_run_full"): CLI.format("doctor"),
    ("subfleet/doctor.py", "_run"): CLI.format("doctor"),
    ("subfleet/gate/merge.py", "run_command"):
        SCOPE.format("gate.service.dispatch") + "; " + CLI.format("gate --dry-run, in preview's scope"),
    ("subfleet/guard/preflight.py", "_version"):
        "parent is preflight's selection; " + SCOPE.format("Daemon._run_worker, Timers._run_worker"),
    ("subfleet/guard/preflight.py", "_hooks_list"):
        "parent is preflight's selection; " + SCOPE.format("Daemon._run_worker, Timers._run_worker"),
    ("subfleet/guard/preflight.py", "preflight"):
        "daemon: parent is ProviderEnvironment.assemble's, " + SCOPE.format("Daemon._run_worker, Timers._run_worker")
        + "; " + CLI.format("doctor --live"),
    ("subfleet/guardian.py", "run_guardian"): "the guardian process: its parent is the daemon's withheld launch env",
    ("subfleet/provider_env.py", "ProviderEnvironment.assemble"):
        "explicit withheld=; " + SCOPE.format("many") + "; " + CLI.format("doctor"),
    ("subfleet/retention_git.py", "environment"):
        SCOPE.format("Daemon._run_worker, ConversationService._resume_start") + "; " + CLI.format("retention"),
    ("subfleet/retention_holders.py", "lsof_holders"):
        SCOPE.format("Daemon._run_worker") + "; " + CLI.format("retention survey"),
    ("subfleet/salvage.py", "_git_env"):
        SCOPE.format("Daemon._run_worker, dispatch, ConversationService._resume_start"),
    ("subfleet/salvage.py", "snapshot_tree"): SCOPE.format("Daemon._run_worker"),
    ("subfleet/sessions/handoff.py", "_run_git"): CLI.format("the sessions CLI; the daemon's brief passes repository=False"),
    ("subfleet/sessions/nudge.py", "spawn"): CLI.format("the SessionStart hook process"),
    ("subfleet/uncommitted.py", "_check_applies"):
        SCOPE.format("dispatch (a worktree-write review's submit), Daemon._run_worker"),
}


def test_every_build_environment_call_site_is_classified():
    """A new `build_environment(` call fails here until the audit classifies it:
    whether the daemon runs it inside a helper scope, or how else it is covered."""
    found = {(path, function) for path, function, _ in calls("build_environment")}
    assert found == set(CALL_SITES)
    assert len(calls("build_environment")) == 31, "a function gained or lost a call: reclassify it"
