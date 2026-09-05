"""v1 invocation compatibility (C-17.1, C-17.2, plan.md amendment 1).

Every test names the clause it proves (C-20.5). The bulk of the suite is a
replay of `tests/fixtures/compat/cases.json`, one row per v1 invocation found
in the v1 sources; the tests after it are the rules those rows are graded
against, so a wrong row fails a rule rather than quietly becoming the contract.
"""

from __future__ import annotations

import json
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
#: promises to keep trains agents to change commands that work.
PERMANENT_HEADS = {"status", "capacity", "runs", "jobs", "show", "wait", "kill",
                   "resume", "resume-codex", "notify", "ping", "run", "lanes",
                   "why", "daemon", "doctor", "hook"}


def ids(cases):
    return [case["id"] for case in cases]


# --- the case table -----------------------------------------------------------

def test_the_case_table_is_not_empty_and_has_no_duplicate_ids():
    assert len(CASES) > 200, "the harvest should cover every v1 source"
    assert len(BY_ID) == len(CASES)


def test_no_v1_command_that_worked_is_refused_for_a_reason_that_is_not_recorded():
    """A refusal of something v1 accepted is a deliberate break, and there are
    exactly two kinds: the direct provider verbs the agent contract forbids, and
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
    """The lane brief: the README lines the table came from are cited."""
    assert ":" in case["cite"] and case["cite"].rsplit(":", 1)[1].isdigit(), \
        f"{case['id']} has no path:line citation"
    assert case["v1_meaning"].strip(), f"{case['id']} does not say what v1 does"


MAPPED = [case for case in CASES if case["expect"]["disposition"] in ("map", "note")]


@pytest.mark.parametrize("case", MAPPED, ids=ids(MAPPED))
def test_nothing_v1_accepted_reaches_a_v2_usage_error(case):
    """The cutover's actual question, asked one row at a time.

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
    """plan.md amendment 1: the v1 verb spellings are permanent.

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
    (["runs", "show", "x"], ["runs", "show", "x"]),
    (["show", "x"], ["runs", "show", "x"]),
    (["runs", "reap"], ["runs", "reap"]),
    (["wait", "x"], ["wait", "x"]),
    (["kill", "x"], ["kill", "x"]),
    (["resume-codex", "x"], ["resume", "x"]),
    (["notify", "hi"], ["ping", "hi"]),
    (["run", "-p", "p.md"], ["run", "-p", "p.md"]),
    ([], ["status"]),
])
def test_the_permanent_aliases_rewrite_silently(argv, expected):
    """C-17.1's alias list, each with no note at all."""
    mapping = compat.translate(argv, env={})
    assert mapping.disposition == "map"
    assert mapping.argv == expected
    assert mapping.notes == []


# --- the front-door refusal ---------------------------------------------------

@pytest.mark.parametrize("verb", ["codex", "claude", "mirror"])
def test_the_direct_provider_verbs_are_refused_with_the_front_door(verb):
    """C-17.3 exit 7 is "refused (message names the rule and the fix)"; the
    agent contract's own rule is `subfleet run`, and the message says so."""
    mapping = compat.translate([verb, "-C", "/repo", "-p", "p.md"], env={})
    assert mapping.disposition == "refuse"
    assert mapping.exit_code == int(Exit.REFUSED) == 7
    assert any("subfleet run --task" in note for note in mapping.notes)
    assert mapping.argv == []


def test_refusing_a_provider_verb_never_dispatches_anything(monkeypatch):
    """The refusal is the whole behaviour: nothing reaches cli.main or v1."""
    called: list = []
    monkeypatch.setattr(cli, "main", lambda argv: called.append(argv) or 0)
    monkeypatch.setattr(compat, "delegate",
                        lambda *a, **k: called.append("v1") or 0)
    assert compat.dispatch(["codex", "-p", "p.md"], env={}) == 7
    assert called == []


# --- flags v2 has no name for -------------------------------------------------

def test_no_v1_flag_is_unaccounted_for():
    """Every option v1 accepts for a verb v2 owns is either in v2's parser or in
    `V1_ONLY_FLAGS` with a decision recorded.

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
             "run": "run", "enroll": "lanes.enroll"}
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
    """v1's own parsers, read out of a subprocess that imports the v1 package."""
    v1_root = Path("~/chief-of-staff/subfleet").expanduser()
    if not (v1_root / "subfleet" / "cli.py").exists():
        return None
    code = _V1_PROBE % str(v1_root)
    done = subprocess.run([sys.executable, "-P", "-c", code],
                          capture_output=True, text=True)
    if done.returncode != 0:
        return None
    return json.loads(done.stdout)


_V1_PROBE = r'''
import sys, argparse, json, io, contextlib
sys.path.insert(0, "%s")
from subfleet import cli as v1cli, delegate
def opts(p):
    return sorted({o for a in p._actions for o in a.option_strings})
def walk(p, prefix=""):
    res = {prefix or "(root)": opts(p)}
    for a in p._actions:
        ch = getattr(a, "choices", None)
        if isinstance(ch, dict) and hasattr(a, "add_parser"):
            for name, sub in ch.items():
                res.update(walk(sub, f"{prefix}.{name}" if prefix else name))
    return res
parser = None
orig = argparse.ArgumentParser.parse_args
def grab(self, *a, **k):
    global parser
    if self.prog == "subfleet":
        parser = self
    raise SystemExit(0)
argparse.ArgumentParser.parse_args = grab
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        v1cli.main(["status"])
except SystemExit:
    pass
argparse.ArgumentParser.parse_args = orig
res = walk(parser)
res["run"] = opts(delegate._parser())
print(json.dumps(res))
'''


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
    (["run", "--independent-review", "--review-root", "/r", "-p", "p.md"],
     "--independent-review"),
])
def test_a_flag_whose_absence_would_change_what_happens_is_refused(argv, flag):
    """C-17.3 exit 2 is invalid input. Continuing without `--dry-run` would do
    the thing the caller asked to preview, so nothing is dropped quietly."""
    mapping = compat.translate(argv, env={})
    assert mapping.disposition == "refuse"
    assert mapping.exit_code == int(Exit.INVALID_INPUT) == 2
    assert any(flag in note for note in mapping.notes)


@pytest.mark.parametrize("typed,resolved", [
    ("--independent-rev", "--independent-review"),
    ("--reuse", "--reuse-out"),
    ("--review-r", "--review-root"),
])
def test_v1s_argparse_abbreviations_resolve_to_the_flag_they_abbreviated(
        typed, resolved):
    """v1's parser accepted any unambiguous abbreviation, so these were real v1
    spellings; v2 would answer them with a usage error naming nothing useful."""
    mapping = compat.translate(["run", typed, "-p", "p.md"], env={})
    assert mapping.disposition == "refuse"
    assert any(resolved in note for note in mapping.notes)


def test_independent_is_a_different_flag_in_each_version():
    """The one spelling that means two things: `--independent` abbreviated v1's
    hidden `--independent-review`, and is a real v2 flag (C-7.3). v2's meaning
    wins — it is the version being run — and the refusal message for the v1
    flag says so out loud so the difference is not discovered in production."""
    v2_meaning = compat.translate(["run", "--independent", "-p", "p.md"], env={})
    assert v2_meaning.disposition == "map"
    assert "--independent" in v2_meaning.argv
    warned = compat.translate(["run", "--independent-review", "-p", "p.md"], env={})
    assert any("--independent" in note and "C-7.3" in note
               for note in warned.notes)


def test_run_status_was_never_a_dispatch():
    """v1 `delegate.py:378`: `run --status` printed the lane table and re-exec'd
    `pick codex`. Mapping it to `run` would dispatch a job nobody asked for."""
    mapping = compat.translate(["run", "--status"], env={})
    assert mapping.disposition == "note" and mapping.argv == ["status"]
    assert compat.translate(["run", "--status", "--json"], env={}).argv == \
        ["status", "--json"]


# --- delegation ---------------------------------------------------------------

DELEGATED_CASES = [case for case in CASES
                   if case["expect"]["disposition"] == "delegate"]


@pytest.mark.parametrize("case", DELEGATED_CASES, ids=ids(DELEGATED_CASES))
def test_a_delegated_case_hands_v1_the_argv_it_was_given(case):
    """The argv is not rewritten on the way to v1: v1 parses what was typed."""
    mapping = compat.translate(case["argv"], env=case["env"])
    assert mapping.argv == case["argv"]


@pytest.mark.parametrize("verb", ["_record-run", "_record-lane-run",
                                  "_canonical-model", "_api-lane-check",
                                  "_record-codex-cooldown", "_session-hook",
                                  "_tickle"])
def test_the_hidden_verbs_are_delegated_without_a_word_on_stderr(verb):
    """v1's runners call these back through $SUBFLEET_RUN_SUBFLEET on every
    record they write, and a note per call would land in a captured err.log."""
    mapping = compat.translate([verb, "arg"], env={})
    assert mapping.disposition == "delegate" and mapping.notes == []


def test_gate_is_delegated_and_its_exit_code_comes_back_unchanged():
    """The gate's 0-to-5 codes mean things no other verb's codes mean
    (v1 README:717-720: agreement, operational, invalid input, changes
    requested, blocked review, failed action). Remapping them onto C-17.3
    would tell a driving agent that a blocked review was a clean merge."""
    mapping = compat.translate(["gate", "pr", "42", "--peer", "astra"], env={})
    assert mapping.disposition == "delegate"

    class Done:
        returncode = 3

    seen: list[list[str]] = []

    def runner(argv, **kwargs):
        seen.append(argv)
        return Done()

    code = compat.delegate(mapping.argv, runner=runner)
    assert code == 3
    assert seen and seen[0][1:] == ["gate", "pr", "42", "--peer", "astra"]


def test_delegation_says_so_when_the_v1_install_is_gone(monkeypatch, capsys):
    """C-17.3 exit 1 is an operational error; the message names what is missing."""
    monkeypatch.setattr(compat, "v1_binary", lambda env=None: None)
    assert compat.delegate(["sessions"]) == int(Exit.OPERATIONAL)
    assert "SUBFLEET_V1_BIN" in capsys.readouterr().err


def test_the_v1_binary_is_never_this_process(monkeypatch, tmp_path):
    """After the flip `which subfleet` is v2; delegating to it would recurse
    until the process table gave out."""
    binary = tmp_path / "subfleet"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.delenv(compat.V1_BIN_ENV, raising=False)
    monkeypatch.setattr(compat, "V1_BIN_DEFAULT", str(tmp_path / "absent"))
    monkeypatch.setattr(compat.shutil, "which", lambda name: str(binary))
    monkeypatch.setattr(sys, "argv", [str(binary)])
    assert compat.v1_binary() is None


def test_the_v1_binary_override_is_honoured(monkeypatch, tmp_path):
    binary = tmp_path / "v1sf"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv(compat.V1_BIN_ENV, str(binary))
    assert compat.v1_binary() == str(binary)


# --- environment --------------------------------------------------------------

def test_carpool_names_are_aliased_the_way_v1s_launcher_aliases_them():
    """v1's `bin/subfleet:8-12` is a bash launcher that renames every CARPOOL_*
    to its SUBFLEET_* twin before exec'ing Python, and only when the twin is
    unset. v2 is a console script with no launcher, so the rule lives here or
    it stops happening at the cutover."""
    updates, notes = compat.map_env({"CARPOOL_STATE_DIR": "/x",
                                     "CARPOOL_CODEX_HOMES": "/y"})
    assert updates == {"SUBFLEET_STATE_DIR": "/x", "SUBFLEET_CODEX_HOMES": "/y"}
    assert len(notes) == 1 and "carpool-era" in notes[0]


def test_an_alias_never_overwrites_the_name_it_aliases_to():
    """v1's guard: `[ -n "${!_new:-}" ] || ... export`."""
    updates, _notes = compat.map_env({"CARPOOL_STATE_DIR": "/old",
                                      "SUBFLEET_STATE_DIR": "/new"})
    assert updates == {}


@pytest.mark.parametrize("old,new", [
    ("CLAUDE_LANE_CARPOOL", "CLAUDE_LANE_SUBFLEET"),
    ("DELEGATE_CARPOOL", "DELEGATE_SUBFLEET"),
])
def test_the_two_names_v1s_launcher_aliases_unconditionally(old, new):
    """`${_legacy/CARPOOL/SUBFLEET}` replaces the FIRST occurrence, which is why
    these two land where they do rather than gaining a SUBFLEET_ prefix."""
    updates, _notes = compat.map_env({old: "/v"})
    assert updates == {new: "/v"}


@pytest.mark.parametrize("name", sorted(compat.NOTED_ENV))
def test_a_v1_state_or_binary_variable_is_noted_and_not_obeyed(name):
    """These name v1's own trees and binaries. Pointing v2 at one would make v2
    read v1's store or run v1's code under a v2 verb, which is worse than
    ignoring it — so each is read, reported, and then ignored."""
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
    """A note about the environment is printed whatever the verb turns out to be."""
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
    seen: list = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv) or 0)
    compat.dispatch(["jobs", "--mine"], env={})
    assert seen == [["runs", "--mine"]]


def test_dispatch_exports_the_aliased_names(monkeypatch):
    monkeypatch.setattr(cli, "main", lambda argv: 0)
    monkeypatch.delenv("SUBFLEET_CODEX_HOMES", raising=False)
    compat.dispatch(["runs"], env={"CARPOOL_CODEX_HOMES": "/y"})
    import os
    assert os.environ["SUBFLEET_CODEX_HOMES"] == "/y"


# --- the doctor row -----------------------------------------------------------

def test_self_check_reports_a_reachable_table():
    """`doctor` calls this before the cutover: a rule whose target verb was
    renamed is a command that used to work and now prints a usage error."""
    report = compat.self_check()
    assert report["unreachable"] == []
    assert report["rules"] > 40 and report["verbs"] > 30 and report["env"] > 5


def test_self_check_notices_a_target_that_stopped_existing(monkeypatch):
    monkeypatch.setitem(compat.PERMANENT, ("jobs",), ["runzz"])
    assert compat.self_check()["unreachable"] == ["jobs -> runzz"]


def test_every_table_target_is_a_real_v2_verb():
    """Read the other way round from `self_check`: nothing in the tables points
    at a verb path `cli.build_parser()` does not define."""
    paths = compat.verb_paths()
    for target in list(compat.PERMANENT.values()) + \
            [value for value, _note in compat.RENAMED.values()]:
        assert compat._target_path(target) in paths, target


def test_no_verb_is_in_two_tables_at_once():
    """A verb that is both permanent and delegated would resolve by table order
    rather than by intent."""
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
    """v1's own `known` set (cli.py:1581-1589) plus its hidden verbs: each one
    resolves to a disposition rather than to an argparse usage error."""
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
