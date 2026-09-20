"""v1 invocation compatibility (C-17.1, C-17.2, plan.md amendment 1).

Every test names the clause it proves (C-20.5). The bulk of the suite is a
replay of `tests/fixtures/compat/cases.json`, one row per v1 invocation found
in the v1 sources; the tests after it are the rules those rows are graded
against, so a wrong row fails a rule rather than quietly becoming the contract.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from subfleet import cli, compat
from subfleet.contracts import Exit

CASES_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "compat" / "cases.json"
CASES = json.loads(CASES_PATH.read_text())["cases"]
BY_ID = {case["id"]: case for case in CASES}

#: plan.md amendment 1: every v1 verb spelling is permanent. A permanent verb
#: never carries a deprecation note, because a note on a spelling the contract
#: promises to keep trains agents to change commands that work. `jobs` and a
#: top-level `show` are in this set as C-17.1's own additions rather than as v1
#: spellings — neither string exists anywhere in the v1 tree.
PERMANENT_HEADS = {"status", "capacity", "runs", "jobs", "show", "wait", "kill",
                   "resume", "resume-codex", "notify", "ping", "run", "lanes",
                   "why", "daemon", "doctor", "hook", "gate", "enroll", "pick", "_api-lane-check",
                   "errors", "brief", "watch", "keepalive", "reset", "login", "_canonical-model", "_session-hook",
                   # milestone 6: the sessions kit. `sessions` and `handoff` are
                   # first-class in C-17.1; `tickle`, `muster`, `revive` and
                   # `mirror` are v1 spellings amendment 1 keeps, mapped onto
                   # `sessions continue --scope ...` and `sessions mirror`.
                   "sessions", "handoff", "tickle", "muster", "revive", "mirror"}


def ids(cases):
    return [case["id"] for case in cases]


# --- the case table -----------------------------------------------------------

def test_the_case_table_is_not_empty_and_has_no_duplicate_ids():
    """C-20.5 the fixture is the evidence for C-17.1, so it has to be real."""
    assert len(CASES) > 200, "the harvest should cover every v1 source"
    assert len(BY_ID) == len(CASES)


def test_no_v1_command_that_worked_is_refused_for_a_reason_that_is_not_recorded():
    """C-17.3 a refusal of something v1 accepted is a deliberate break, and
    there are exactly two kinds: the direct provider verbs the agent contract forbids, and
    the flags whose absence would change what happens. Anything else in this
    list would be an accident."""
    deliberate = set(compat.REFUSED)
    for case in CASES:
        if not case["v1_parses"] or case["expect"]["disposition"] != "refuse":
            continue
        head = case["argv"][0]
        assert head in deliberate or case["expect"]["rule"].endswith(
            "v1-only-flag"), f"{case['id']}: {case['argv']} is refused by "\
                             f"rule {case['expect']['rule']!r}, which is neither"


@pytest.mark.parametrize("case", CASES, ids=ids(CASES))
def test_every_v1_invocation_maps_to_the_recorded_disposition(case):
    """C-17.1 every command line in v1's README and in the agent contract
    reaches a v2 disposition, and the one it reaches is the one recorded."""
    mapping = compat.translate(case["argv"], env=case["env"])
    expected = case["expect"]
    assert mapping.disposition == expected["disposition"], case["cite"]
    assert mapping.argv == expected["argv"], case["cite"]
    assert mapping.verb == expected["verb"], case["cite"]
    assert mapping.exit_code == expected["exit_code"], case["cite"]
    assert mapping.rule == expected["rule"], case["cite"]
    assert len(mapping.notes) == expected["note_count"], case["cite"]
    assert mapping.env == expected["env"], case["cite"]


@pytest.mark.parametrize("case", CASES, ids=ids(CASES))
def test_every_case_cites_the_line_it_came_from(case):
    """C-20.5 every case names the line it proves, as every test names its
    clause; a case with no citation is an assertion with no source."""
    assert ":" in case["cite"] and case["cite"].rsplit(":", 1)[1].isdigit(), \
        f"{case['id']} has no path:line citation"
    assert case["v1_meaning"].strip(), f"{case['id']} does not say what v1 does"


MAPPED = [case for case in CASES if case["expect"]["disposition"] in ("map", "note")]


@pytest.mark.parametrize("case", MAPPED, ids=ids(MAPPED))
def test_nothing_v1_accepted_reaches_a_v2_usage_error(case):
    """C-17.1 the cutover's actual question, asked one row at a time.

    `v1_parses` was recorded by asking v1's own parsers (mirroring v1
    `cli.py:main`'s dispatch, including its `status` prepend and its handoff of
    `run` to `delegate._parser()`). A row v1 accepted and v2 maps must reach a
    v2 handler or v2's help; a row v1 refused may fail on either side.
    """
    mapping = compat.translate(case["argv"], env=case["env"])
    if mapping.verb and mapping.verb != compat.HELP:
        assert mapping.verb in compat.verb_paths(), case["cite"]
    if case["v1_parses"]:
        assert mapping.verb, (
            f"{case['id']}: v1 accepted {case['argv']} and v2 answers it with a "
            f"usage error — that is a working command broken by the cutover")


# --- the permanence rule ------------------------------------------------------

PERMANENT_CASES = [case for case in CASES
                   if case["argv"] and case["argv"][0] in PERMANENT_HEADS]


@pytest.mark.parametrize("case", PERMANENT_CASES, ids=ids(PERMANENT_CASES))
def test_a_permanent_spelling_is_never_noted_for_being_a_permanent_spelling(case):
    """C-17.1 and plan.md amendment 1: the v1 verb spellings are permanent.

    A note is allowed on one of these only when it is about a FLAG v2 dropped,
    never about the verb itself — so no note may name the verb as deprecated.
    """
    mapping = compat.translate(case["argv"], env=case["env"])
    verb = case["argv"][0]
    for note in mapping.notes:
        assert not (f"`{verb}`" in note and "is now" in note), \
            f"{case['id']}: {verb} is permanent but was noted as renamed: {note}"
        assert "deprecated" not in note or "--" in note, \
            f"{case['id']}: a permanent verb was called deprecated: {note}"


@pytest.mark.parametrize("argv,expected", [
    (["status"], ["status"]),
    (["capacity"], ["status"]),
    (["runs"], ["runs"]),
    (["jobs"], ["runs"]),
    (["runs", "show", "x", "--out"], ["runs", "show", "x", "--out"]),
    (["show", "x", "--json"], ["runs", "show", "x", "--json"]),
    (["runs", "reap"], ["runs", "reap"]),
    (["wait", "x"], ["wait", "x"]),
    (["kill", "x"], ["kill", "x"]),
    (["resume-codex", "x"], ["resume", "x"]),
    (["notify", "hi"], ["ping", "hi"]),
    (["enroll", "person@example.com"], ["lanes", "enroll", "person@example.com"]),
    (["run", "-p", "p.md"], ["run", "-p", "p.md"]),
    ([], ["status"]),
])
def test_the_permanent_aliases_rewrite_silently(argv, expected):
    """C-17.1's alias list, each with no note at all.

    `runs show` appears here only in the forms that already name a stream: the
    bare form carries one line about where the deliverable went, which
    `test_runs_show_says_where_the_deliverable_went` pins.
    """
    mapping = compat.translate(argv, env={})
    assert mapping.disposition == "map"
    assert mapping.argv == expected
    assert mapping.notes == []


# --- the front-door refusal ---------------------------------------------------

@pytest.mark.parametrize("verb", ["codex", "claude"])
def test_the_direct_provider_verbs_are_refused_with_the_front_door(verb):
    """C-17.3 exit 7 is "refused (message names the rule and the fix)"; the
    agent contract's own rule is `subfleet run`, and the message says so."""
    mapping = compat.translate([verb, "-C", "/repo", "-p", "p.md"], env={})
    assert mapping.disposition == "refuse"
    assert mapping.exit_code == int(Exit.REFUSED) == 7
    assert any("subfleet run --task" in note for note in mapping.notes)
    assert mapping.argv == []


def test_refusing_a_provider_verb_never_dispatches_anything(monkeypatch):
    """C-17.3 exit 7 is the whole behaviour: nothing reaches cli.main or v1."""
    called: list = []
    monkeypatch.setattr(cli, "main", lambda argv: called.append(argv) or 0)
    monkeypatch.setattr(compat, "delegate",
                        lambda *a, **k: called.append("v1") or 0)
    assert compat.dispatch(["codex", "-p", "p.md"], env={}) == 7
    assert called == []


# --- flags v2 has no name for -------------------------------------------------

def test_no_v1_flag_is_unaccounted_for():
    """C-17.2 every option v1 accepts for a verb v2 owns is either in v2's
    parser or in `V1_ONLY_FLAGS` with a decision recorded.

    The diff is redone here against v1's own parsers rather than trusted from
    the table, so a v1 that grows a flag fails this test instead of silently
    dropping it at the cutover.
    """
    v1 = _v1_option_map()
    if v1 is None:
        pytest.skip("the v1 install is not present on this machine")
    v2 = _v2_option_map()
    pairs = {"status": "status", "capacity": "status", "runs": "runs",
             "runs.show": "runs.show", "runs.reap": "runs.reap", "wait": "wait",
             "kill": "kill", "resume-codex": "resume", "notify": "ping",
             "run": "run", "enroll": "lanes.enroll",
             # milestone 6. v1's three sweeps land on one v2 verb, so every flag
             # they accept has to be registered there (or decided about in
             # V1_ONLY_FLAGS). `mirror` is absent from this map on purpose: v1's
             # `subfleet mirror` is an `add_help=False` REMAINDER passthrough to
             # `bin/subfleet-mirror`, so its flags are not in v1's argparse tree
             # at all and are diffed by the test below instead.
             "sessions": "sessions", "tickle": "sessions.continue",
             "muster": "sessions.continue", "revive": "sessions.continue",
             "handoff": "handoff"}
    unaccounted: list[str] = []
    for v1_path, v2_path in pairs.items():
        if v1_path not in v1:
            continue
        table = compat.V1_ONLY_FLAGS.get(v1_path.replace(".", " "), {})
        for option in v1[v1_path]:
            if option in ("-h", "--help") or option in v2.get(v2_path, set()):
                continue
            if option in table or option == "--status":
                continue
            unaccounted.append(f"{v1_path} {option}")
    assert unaccounted == [], (
        "v1 accepts these and neither v2's parser nor compat.V1_ONLY_FLAGS has "
        f"a decision about them: {unaccounted}")


def test_no_v1_mirror_flag_is_unaccounted_for():
    """C-17.2 for the one v1 verb whose flags are not in v1's argparse tree.

    `subfleet mirror` was an `add_help=False` REMAINDER passthrough that exec'd
    `bin/subfleet-mirror` (v1 `cli.py:1599-1606`), so the diff `_v1_option_map`
    performs cannot see its options. They are harvested from the script's own
    `add_argument` lines instead, and each one must be on `sessions mirror` or
    in `V1_ONLY_FLAGS["mirror"]`.
    """
    script = Path("~/chief-of-staff/subfleet/bin/subfleet-mirror").expanduser()
    if not script.exists():
        pytest.skip("the v1 install is not present on this machine")
    options = set(re.findall(r'add_argument\(\s*"(--[a-z-]+)"', script.read_text()))
    assert options, "the harvest found no flags; the script's shape changed"
    v2 = _v2_option_map().get("sessions.mirror", set())
    table = compat.V1_ONLY_FLAGS.get("mirror", {})
    unaccounted = sorted(option for option in options
                         if option not in v2 and option not in table)
    assert unaccounted == [], (
        "v1's mirror accepts these and neither `sessions mirror` nor "
        f"compat.V1_ONLY_FLAGS has a decision about them: {unaccounted}")


def _v2_option_map() -> dict[str, set[str]]:
    def walk(parser, prefix=""):
        found = {prefix or "(root)": {option for action in parser._actions
                                      for option in action.option_strings}}
        for action in parser._actions:
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict) and hasattr(action, "add_parser"):
                for name, sub in choices.items():
                    found.update(walk(sub, f"{prefix}.{name}" if prefix else name))
        return found
    return walk(cli.build_parser())


def _v1_option_map() -> dict[str, list[str]] | None:
    """C-17.1: inspect v1 source as data; never import or invoke its commands."""
    root = Path("~/chief-of-staff/subfleet/subfleet").expanduser()
    if not (root / "cli.py").is_file():
        return None
    tree = ast.parse((root / "cli.py").read_text())
    names = {"p_status": "status", "p_capacity": "capacity", "p_runs": "runs",
             "p_runs_show": "runs.show", "p_runs_reap": "runs.reap", "p_wait": "wait",
             "p_kill": "kill", "p_resume": "resume-codex", "p_notify": "notify", "p_enroll": "enroll"}
    result = {name: [] for name in names.values()}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument" and isinstance(node.func.value, ast.Name)):
            target = names.get(node.func.value.id)
            if target:
                result[target].extend(arg.value for arg in node.args if isinstance(arg, ast.Constant)
                                      and isinstance(arg.value, str) and arg.value.startswith("-"))
    tree = ast.parse((root / "delegate.py").read_text())
    parser = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_parser")
    result["run"] = [arg.value for node in ast.walk(parser) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"
                     for arg in node.args if isinstance(arg, ast.Constant)
                     and isinstance(arg.value, str) and arg.value.startswith("-")]
    return result


@pytest.mark.parametrize("argv,dropped", [
    (["status", "--cached"], "--cached"),
    (["capacity", "--cached"], "--cached"),
    (["--cached"], "--cached"),
    (["runs", "reap", "--grace", "30"], "--grace"),
    (["wait", "--mine", "--interval", "2"], "--interval"),
    (["kill", "x", "--grace", "5"], "--grace"),
    (["notify", "--force", "hi"], "--force"),
    (["notify", "--mode", "bypass", "hi"], "--mode"),
])
def test_a_dropped_flag_is_named_and_its_value_goes_with_it(argv, dropped):
    """C-17.2 a flag v2 does not have is dropped out loud, never in silence, and
    a flag that took a value does not leave the value behind as a positional."""
    mapping = compat.translate(argv, env={})
    assert mapping.disposition == "note"
    assert dropped not in mapping.argv
    assert any(dropped in note for note in mapping.notes)
    assert mapping.verb and mapping.verb != compat.HELP, \
        f"{argv} stopped parsing after the drop"


@pytest.mark.parametrize("argv,flag", [
    (["runs", "reap", "--dry-run"], "--dry-run"),
    (["wait", "x", "--cat"], "--cat"),
    (["run", "-b", "feat", "-p", "p.md"], "-b"),
    (["run", "--reuse-out", "-o", "x", "-p", "p.md"], "--reuse-out"),
])
def test_a_flag_whose_absence_would_change_what_happens_is_refused(argv, flag):
    """C-17.3 exit 2 is invalid input. Continuing without `--dry-run` would do
    the thing the caller asked to preview, so nothing is dropped quietly."""
    mapping = compat.translate(argv, env={})
    assert mapping.disposition == "refuse"
    assert mapping.exit_code == int(Exit.INVALID_INPUT) == 2
    assert any(flag in note for note in mapping.notes)


@pytest.mark.parametrize("flags", [["--mint"], ["--paste"], ["--mint", "--paste"]])
def test_enroll_login_flags_name_the_manual_credential_flow(flags):
    """C-23.52: compatibility never mints a provider login or drops its flags."""
    mapping = compat.translate(["enroll", "person@example.com", *flags], env={})
    assert mapping.disposition == "refuse"
    assert mapping.exit_code == int(Exit.INVALID_INPUT)
    assert "claude setup-token" in " ".join(mapping.notes)
    assert "subfleet lanes enroll claude-quota-<email>" in " ".join(mapping.notes)


def test_run_status_resolves_v1s_abbreviations_of_it():
    """C-17.2 `--stat` and `--statu` were unambiguous v1 spellings of
    `--status`, and v2's `run` has no `--stat*` flag at all, so without this
    they reach a usage error naming nothing."""
    for typed in ("--status", "--statu", "--stat"):
        mapping = compat.translate(["run", typed], env={})
        assert mapping.argv == ["status"], typed
        assert mapping.disposition == "note", typed


@pytest.mark.parametrize("typed,resolved", [
    ("--independent-rev", "--independent-review"),
    ("--reuse", "--reuse-out"),
    ("--review-r", "--review-root"),
])
def test_v1s_argparse_abbreviations_resolve_to_the_flag_they_abbreviated(
        typed, resolved):
    """C-17.2 v1's parser accepted any unambiguous abbreviation, so these were
    real v1 spellings; v2 would answer them with a usage error naming nothing
    useful, which C-17.3 reserves for input that is actually invalid."""
    mapping = compat.translate(["run", typed, "-p", "p.md"], env={})
    if resolved == "--reuse-out":
        assert mapping.disposition == "refuse"
        assert any(resolved in note for note in mapping.notes)
    else:
        assert mapping.disposition == "map"


def test_independent_is_a_different_flag_in_each_version():
    """The one spelling that means two things: `--independent` abbreviated v1's
    hidden `--independent-review`, and is a real v2 flag (C-7.3). v2's meaning
    wins — it is the version being run — and the refusal message for the v1
    flag says so out loud so the difference is not discovered in production."""
    v2_meaning = compat.translate(["run", "--independent", "-p", "p.md"], env={})
    assert v2_meaning.disposition == "map"
    assert "--independent" in v2_meaning.argv
    warned = compat.translate(["run", "--independent-review", "-p", "p.md"], env={})
    assert warned.disposition == "map"
    args = cli.build_parser().parse_args(warned.argv)
    assert args.isolated_review and not args.independent


@pytest.mark.parametrize("argv,expected", [
    (["run", "--task=build", "--tier=standard", "-p", "p.md"],
     ["run", "--task=build", "--tier=standard", "-p", "p.md"]),
    (["run", "-dp", "p.md"], ["run", "-dp", "p.md"]),
    (["run", "-C/repo", "-mastra", "-p", "p.md"],
     ["run", "-C/repo", "-mastra", "-p", "p.md"]),
    (["run", "-C", "/repo", "--", "--why is this so slow?"],
     ["run", "-C", "/repo", "--", "--why is this so slow?"]),
    (["run", "--task", "lookup", "--tier", "trivial", "summarize HEAD", "-C", "/repo"],
     ["run", "--task", "lookup", "--tier", "trivial", "summarize HEAD", "-C", "/repo"]),
])
def test_the_token_shapes_argparse_accepts_reach_the_parser_untouched(argv, expected):
    """C-17.2 `--opt=value`, clustered short flags, attached short values, `--`,
    and a flag after the positional prompt are all shapes argparse handles and
    agents type. The scanner must not rewrite them on the way past: it looks at
    a token only to decide whether it is a v1-only flag, and argparse does the
    rest."""
    mapping = compat.translate(argv, env={})
    assert mapping.disposition == "map"
    assert mapping.argv == expected
    assert mapping.verb == "run"
    assert mapping.notes == []


def test_runs_show_says_where_the_deliverable_went():
    """C-17.4 v1's `cmd_runs` (`cli.py:691-710`) printed the metadata JSON and
    then `--- out.md ---` and the deliverable, unconditionally. v2 keeps stdout
    to one thing at a time, and neither v2 form reproduces v1 — `--out` returns
    before the metadata block — so nothing is rewritten and the difference is
    said out loud on exactly the invocation that used to print output."""
    bare = compat.translate(["runs", "show", "x"], env={})
    assert bare.disposition == "note"
    assert bare.argv == ["runs", "show", "x"], "the argv is not rewritten"
    assert any("--out" in note for note in bare.notes)
    for flag in ("--out", "--err", "--json"):
        asked = compat.translate(["runs", "show", "x", flag], env={})
        assert asked.notes == [], f"{flag} already says which stream it wants"


def test_the_delegation_fallback_stays_outside_gates_own_exit_codes(monkeypatch,
                                                                    capsys):
    """C-17.3 exit 7 is "refused (message names the rule and the fix)". `gate`'s
    own codes run 0 to 5 and 1 means "operational error" inside that scheme (v1
    README:717-720), so returning 1 when the v1 install is missing would hand a
    driving agent a gate verdict that no gate produced."""
    monkeypatch.setattr(compat, "v1_binary", lambda env=None: None)
    code = compat.delegate(["gate", "pr", "42", "--peer", "astra"])
    assert code == int(Exit.REFUSED) == 7
    assert code not in range(6), "never a value gate itself can return"
    err = capsys.readouterr().err
    assert "SUBFLEET_V1_BIN" in err and "not a v2 verb" in err


@pytest.mark.parametrize("verb", ["codex", "claude"])
def test_the_provider_refusal_is_narrower_than_v1s_guard_and_says_so(verb):
    """C-17.3 the message names the rule and the fix. v1's PreToolUse guard let
    `subfleet codex -d` through (`bin/subfleet-hook:61-67`: "the runners' own -d
    re-execs under setsid: survivable"), and v2 has no such runner to re-exec,
    so the refusal is a deliberate narrowing and the message says why."""
    mapping = compat.translate([verb, "-d", "-C", "/repo", "-p", "p.md"], env={})
    assert mapping.disposition == "refuse"
    assert mapping.exit_code == int(Exit.REFUSED)
    assert any("setsid" in note for note in mapping.notes)


def test_run_status_was_never_a_dispatch():
    """C-17.2 v1 `delegate.py:378`: `run --status` printed the lane table and
    re-exec'd `pick codex`; mapping it to `run` would submit a job (C-16.3)
    nobody asked for."""
    mapping = compat.translate(["run", "--status"], env={})
    assert mapping.disposition == "note" and mapping.argv == ["status"]
    assert compat.translate(["run", "--status", "--json"], env={}).argv == \
        ["status", "--json"]


# --- delegation ---------------------------------------------------------------

DELEGATED_CASES = [case for case in CASES
                   if case["expect"]["disposition"] == "delegate"]


@pytest.mark.parametrize("case", DELEGATED_CASES, ids=ids(DELEGATED_CASES))
def test_a_delegated_case_hands_v1_the_argv_it_was_given(case):
    """C-17.1 the argv is not rewritten on the way to v1: v1 parses what the
    caller typed, so its own verb table decides."""
    mapping = compat.translate(case["argv"], env=case["env"])
    assert mapping.argv == case["argv"]


@pytest.mark.parametrize("verb", ["_record-run", "_record-lane-run",
                                  "_record-codex-cooldown",
                                  "_tickle"])
def test_the_hidden_verbs_are_delegated_without_a_word_on_stderr(verb):
    """C-17.4 stdout carries the contract and stderr the prose — but v1's
    runners call these back through $SUBFLEET_RUN_SUBFLEET on every record they
    write, so a note per call would land in a captured err.log."""
    mapping = compat.translate([verb, "arg"], env={})
    assert mapping.disposition == "delegate" and mapping.notes == []


@pytest.mark.parametrize("code", range(6))
def test_gate_reaches_native_handler_with_unchanged_exit_codes(monkeypatch, code):
    """C-17.1: gate keeps v1's 0–5 meanings while reaching the new implementation."""
    from subfleet.gate import cli as gate_cli
    seen = []
    monkeypatch.setattr(gate_cli, "run", lambda args: seen.append(args) or code)
    argv = ["gate", "pr", "42", "--peer", "astra"]
    mapping = compat.translate(argv, env={})
    assert mapping.disposition == "map" and mapping.notes == []
    assert compat.dispatch(argv, env={}) == code
    assert seen[0].gate_command == "pr"


def test_delegation_says_so_when_the_v1_install_is_gone(monkeypatch, capsys):
    """C-17.3 exit 7 is "refused (message names the rule and the fix)", and the
    message names both: the verb is not a v2 verb yet, and SUBFLEET_V1_BIN is
    how to point at the install that has it."""
    monkeypatch.setattr(compat, "v1_binary", lambda env=None: None)
    assert compat.delegate(["sessions"]) == int(Exit.REFUSED)
    assert "SUBFLEET_V1_BIN" in capsys.readouterr().err


def test_the_v1_binary_is_never_this_process(monkeypatch, tmp_path):
    """C-5.3's reasoning applied to a binary: after the flip `which subfleet`
    is v2, and delegating to it would recurse until the process table gave out,
    so identity is checked before the call and not after."""
    binary = tmp_path / "subfleet"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.delenv(compat.V1_BIN_ENV, raising=False)
    monkeypatch.setattr(compat, "V1_BIN_DEFAULT", str(tmp_path / "absent"))
    monkeypatch.setattr(compat.shutil, "which", lambda name: str(binary))
    monkeypatch.setattr(sys, "argv", [str(binary)])
    assert compat.v1_binary() is None


def test_the_v1_binary_override_is_honoured(monkeypatch, tmp_path):
    """C-20.5 the tests need a seam that names the v1 install explicitly."""
    binary = tmp_path / "v1sf"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv(compat.V1_BIN_ENV, str(binary))
    assert compat.v1_binary() == str(binary)


# --- environment --------------------------------------------------------------

def test_carpool_names_are_aliased_the_way_v1s_launcher_aliases_them():
    """C-17.1 v1's `bin/subfleet:8-12` is a bash launcher that renames every
    CARPOOL_* to its SUBFLEET_* twin before exec'ing Python, and only when the
    twin is unset. v2 is a console script with no launcher, so the rule lives
    here or it stops happening at the cutover."""
    updates, notes = compat.map_env({"CARPOOL_STATE_DIR": "/x",
                                     "CARPOOL_CODEX_HOMES": "/y"})
    assert updates == {"SUBFLEET_STATE_DIR": "/x", "SUBFLEET_CODEX_HOMES": "/y"}
    assert len(notes) == 1 and "carpool-era" in notes[0]


def test_an_alias_never_overwrites_the_name_it_aliases_to():
    """C-17.1 v1's own guard, kept: `[ -n "${!_new:-}" ] || ... export`."""
    updates, _notes = compat.map_env({"CARPOOL_STATE_DIR": "/old",
                                      "SUBFLEET_STATE_DIR": "/new"})
    assert updates == {}


@pytest.mark.parametrize("old,new", [
    ("CLAUDE_LANE_CARPOOL", "CLAUDE_LANE_SUBFLEET"),
    ("DELEGATE_CARPOOL", "DELEGATE_SUBFLEET"),
])
def test_the_two_names_v1s_launcher_aliases_unconditionally(old, new):
    """C-17.1 `${_legacy/CARPOOL/SUBFLEET}` replaces the FIRST occurrence,
    which is why these two land where they do rather than gaining a SUBFLEET_
    prefix."""
    updates, _notes = compat.map_env({old: "/v"})
    assert updates == {new: "/v"}


@pytest.mark.parametrize("name", sorted(compat.NOTED_ENV))
def test_a_v1_state_or_binary_variable_is_noted_and_not_obeyed(name):
    """C-2.1 these name v1's own trees and binaries. Pointing v2 at one would
    make v2 read v1's store or run v1's code under a v2 verb, which is worse
    than ignoring it — so each is read, reported, and then ignored."""
    updates, notes = compat.map_env({name: "/somewhere"})
    assert updates == {}
    assert any(f"${name}" in note for note in notes)


def test_the_state_root_names_are_not_quietly_equated():
    """v1's SUBFLEET_STATE_DIR and v2's SUBFLEET_HOME are different trees with
    different schemas (C-2.1); mapping one onto the other would point v2's
    daemon at v1's directory."""
    updates, notes = compat.map_env({"SUBFLEET_STATE_DIR": "/v1/state"})
    assert "SUBFLEET_HOME" not in updates
    assert any("different trees" in note for note in notes)


def test_env_notes_ride_along_with_the_verb(monkeypatch):
    """C-17.4 the prose goes to stderr whatever the verb turns out to be."""
    mapping = compat.translate(["runs"], env={"DELEGATE_STATE_DIR": "/x"})
    assert mapping.disposition == "map" and mapping.argv == ["runs"]
    assert any("DELEGATE_STATE_DIR" in note for note in mapping.notes)


# --- the front door -----------------------------------------------------------

def test_dispatch_puts_every_note_on_stderr_and_nothing_on_stdout(monkeypatch,
                                                                  capsys):
    """C-17.4 stdout carries the contract; a script piping `runs --json` sees
    exactly what it saw under v1."""
    monkeypatch.setattr(cli, "main", lambda argv: 0)
    compat.dispatch(["capacity", "--cached", "--json"], env={})
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--cached" in captured.err


def test_dispatch_hands_the_rewritten_argv_to_cli_main(monkeypatch):
    """C-17.1 the alias is resolved before `cli.main`, never inside it."""
    seen: list = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv) or 0)
    compat.dispatch(["jobs", "--mine"], env={})
    assert seen == [["runs", "--mine"]]


def test_dispatch_exports_the_aliased_names(monkeypatch):
    """C-17.1 an aliased name has to reach the process that reads it."""
    monkeypatch.setattr(cli, "main", lambda argv: 0)
    monkeypatch.delenv("SUBFLEET_CODEX_HOMES", raising=False)
    compat.dispatch(["runs"], env={"CARPOOL_CODEX_HOMES": "/y"})
    import os
    assert os.environ["SUBFLEET_CODEX_HOMES"] == "/y"


# --- the entry point ----------------------------------------------------------

def test_the_console_script_and_python_m_both_enter_through_compat():
    """C-17.1 the compatibility layer is only a compatibility layer if every
    invocation goes through it. `pyproject.toml`'s `subfleet` script and
    `subfleet/__main__.py` are the two ways a process starts, and both name
    `compat.dispatch`; `cli.main` is what compat hands a v2 argv to."""
    repo = Path(__file__).resolve().parents[2]
    pyproject = (repo / "pyproject.toml").read_text()
    assert 'subfleet = "subfleet.compat:dispatch"' in pyproject
    assert 'subfleet.cli:main' not in pyproject
    main_module = (repo / "subfleet" / "__main__.py").read_text()
    assert "from .compat import dispatch" in main_module
    assert "sys.exit(dispatch())" in main_module


def test_python_m_subfleet_runs_a_hook_end_to_end(root, tmp_path):
    """C-15.2 `daemon install --hooks` writes `<interpreter> -m subfleet hook
    <Event>` rather than a bare `subfleet`, because during the shadow period a
    bare `subfleet` may still be v1. That command line has to actually run."""
    from subfleet import hooks
    command = hooks.hook_command()
    assert command.endswith("-m subfleet hook")
    done = subprocess.run(
        [*command.split(), "SessionStart"],
        input=json.dumps({"session_id": "sess-entry"}), capture_output=True,
        text=True, cwd=str(Path(__file__).resolve().parents[2]),
        env={**os.environ, "SUBFLEET_HOME": str(root)})
    assert done.returncode == 0, done.stderr


# --- the doctor row -----------------------------------------------------------

def test_self_check_reports_a_reachable_table():
    """C-17.1 `doctor` calls this before the cutover: a rule whose target verb
    was renamed is a command that used to work and now prints a usage error."""
    report = compat.self_check()
    assert report["unreachable"] == []
    assert report["rules"] > 40 and report["verbs"] > 30 and report["env"] > 5


def test_self_check_notices_a_target_that_stopped_existing(monkeypatch):
    """C-17.1 the check has to fail when the table is wrong, or it checks
    nothing at all."""
    monkeypatch.setitem(compat.PERMANENT, ("jobs",), ["runzz"])
    assert compat.self_check()["unreachable"] == ["jobs -> runzz"]


def test_every_table_target_is_a_real_v2_verb():
    """C-17.1 read the other way round from `self_check`: nothing in the tables
    points at a verb path `cli.build_parser()` does not define."""
    paths = compat.verb_paths()
    for target in list(compat.PERMANENT.values()) + \
            [value for value, _note in compat.RENAMED.values()]:
        assert compat._target_path(target) in paths, target


def test_no_verb_is_in_two_tables_at_once():
    """C-17.1 a verb that is both permanent and delegated would resolve by
    table order rather than by intent."""
    heads = [{tokens[0] for tokens in compat.PERMANENT},
             {tokens[0] for tokens in compat.RENAMED},
             set(compat.REFUSED), set(compat.DELEGATED), set(compat.HIDDEN)]
    for index, first in enumerate(heads):
        for second in heads[index + 1:]:
            overlap = first & second
            # `hooks` is deliberately in both RENAMED and DELEGATED_PAIRS: the
            # sub-verb decides, and DELEGATED_PAIRS is consulted first.
            assert not overlap - {"hooks"}, overlap


def test_every_v1_verb_has_a_home():
    """C-17.1 v1's own `known` set (cli.py:1581-1589) plus its hidden verbs:
    each one resolves to a disposition rather than to an argparse usage
    error."""
    v1_verbs = {
        "status", "capacity", "runs", "pick", "run", "codex", "claude", "mirror",
        "login", "errors", "watch", "keepalive", "brief", "enroll", "reset",
        "wait", "kill", "sessions", "notify", "hooks", "tickle", "muster",
        "revive", "resume-codex", "handoff", "gate",
        "_record-lane-run", "_record-run", "_canonical-model", "_api-lane-check",
        "_record-codex-cooldown", "_session-hook", "_tickle",
    }
    homeless = []
    for verb in sorted(v1_verbs):
        mapping = compat.translate([verb], env={})
        if mapping.rule.startswith("unknown"):
            homeless.append(verb)
    assert homeless == []
