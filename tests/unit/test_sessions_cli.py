"""The sessions verbs and the spellings that reach them (C-17.1, C-17.3, C-17.4).

Every test names the clause it proves (C-20.5). `tests/unit/test_compat.py`
proves that v1's spellings translate; this file proves that what they translate
INTO parses, reaches a handler, and prints the contract on stdout and the prose
on stderr.
"""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from subfleet import cli, compat
from subfleet.contracts import Exit
from subfleet.sessions import cli as sessions_cli
from tests import sessions_fixtures as fx

ALICE = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"


def parse(argv: list[str]):
    return cli.build_parser().parse_args(cli.rewrite_aliases(argv))


def translated(argv: list[str]):
    """What `compat` hands `cli.main`, parsed by the real parser."""
    mapping = compat.translate(argv, env={})
    assert mapping.disposition in ("map", "note"), (argv, mapping)
    return parse(mapping.argv)


def run(argv: list[str], monkeypatch, **fakes):
    """Dispatch one argv with the handlers stubbed; returns (rc, stdout, stderr)."""
    for name, value in fakes.items():
        monkeypatch.setattr(sessions_cli, name, value)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def run_v1(argv: list[str], monkeypatch, **fakes):
    """The same, for a v1 spelling: through `compat` first, as the front door does."""
    mapping = compat.translate(argv, env={})
    assert mapping.disposition in ("map", "note"), (argv, mapping)
    return run(mapping.argv, monkeypatch, **fakes)


# --- the verb table (C-17.1) --------------------------------------------------

@pytest.mark.parametrize("argv,verb", [
    (["sessions"], None),
    (["sessions", "list"], "list"),
    (["sessions", "continue"], "continue"),
    (["sessions", "tickle"], "tickle"),
    (["sessions", "muster"], "muster"),
    (["sessions", "revive", ALICE], "revive"),
    (["sessions", "mirror"], "mirror"),
    (["sessions", "retire", ALICE], "retire"),
    (["sessions", "unretire", ALICE], "unretire"),
    (["sessions", "handoff", ALICE, "--to", "opus"], "handoff"),
])
def test_every_sessions_sub_verb_parses_to_the_shared_handler(argv, verb):
    """C-17.1: `sessions [list|continue|revive|mirror]` is permanent, and the
    parent carries the handler exactly as `runs` and `lanes` do."""
    args = parse(argv)
    assert args.handler is cli.cmd_sessions
    assert getattr(args, "sessions_command", None) == verb
    assert sessions_cli.HANDLERS.get(verb or "list") is not None


def test_handoff_is_a_first_class_verb_of_its_own():
    """C-17.1: `handoff <session> --to <model>` is first-class and permanent."""
    args = parse(["handoff", ALICE, "--to", "astra", "-C", "/repo"])
    assert args.handler is cli.cmd_handoff
    assert (args.session_id, args.target, args.workdir) == (ALICE, "astra", "/repo")


def test_the_standalone_entry_point_parses_the_same_verbs():
    """C-17.1: `subfleet sessions` dispatches to the `subfleet-sessions` entry
    point, so the two surfaces must not drift."""
    parser = sessions_cli.build_parser()
    for argv in (["list"], ["continue", "--scope", "cold"], ["revive", ALICE],
                 ["mirror", "--once"], ["handoff", "--last", "--to", "opus"]):
        args = parser.parse_args(argv)
        assert sessions_cli.HANDLERS[args.sessions_command] is not None
    assert parser.parse_args([]).sessions_command is None


def test_the_console_script_is_registered():
    """Plan B decision 3: the kit is a second entry point in the same repo."""
    text = Path("pyproject.toml").read_text(encoding="utf-8")
    assert 'subfleet-sessions = "subfleet.sessions.cli:main"' in text


# --- every v1 spelling reaches the new code (C-17.1, plan amendment 1) --------

@pytest.mark.parametrize("argv,scope", [
    (["tickle"], "interrupted"),
    (["tickle", "--all"], "interrupted"),
    (["tickle", "--all", "--dry-run"], "interrupted"),
    (["tickle", "--json"], "interrupted"),
    (["tickle", "--session", ALICE, "--force"], "interrupted"),
    (["tickle", "--session", ALICE, "--transcript", "/x.jsonl", "--dry-run"],
     "interrupted"),
    (["muster"], "idle"),
    (["muster", "--dry-run"], "idle"),
    (["revive"], "cold"),
    (["revive", "--dry-run"], "cold"),
    (["revive", "--model", "claude-opus-5", "--max", "3"], "cold"),
])
def test_v1_sweep_spellings_reach_sessions_continue(argv, scope):
    """C-17.1 and plan amendment 1: every v1 spelling for these tools parses and
    reaches the new code, with the flags it always accepted."""
    args = translated(argv)
    assert args.handler is cli.cmd_sessions
    assert args.sessions_command == "continue"
    assert args.scope == scope


@pytest.mark.parametrize("argv", [
    ["sessions"], ["sessions", "--all"], ["sessions", "--json"],
])
def test_v1_sessions_spellings_reach_the_listing(argv):
    """C-17.1: `subfleet sessions --all` is a v1 spelling the contract keeps."""
    args = translated(argv)
    assert args.handler is cli.cmd_sessions and args.sessions_command is None


@pytest.mark.parametrize("argv", [
    ["mirror"], ["mirror", "--dry-run"], ["mirror", "--list"], ["mirror", "--quiet"],
    ["mirror", "--no-flag-sync"],
    ["mirror", "--prune", "--dead-home", "org-1"],
])
def test_v1_mirror_spellings_reach_sessions_mirror(argv):
    """C-23.28: v2 owns the mirror now, so `subfleet mirror` is mapped, not refused."""
    args = translated(argv)
    assert args.handler is cli.cmd_sessions and args.sessions_command == "mirror"


@pytest.mark.parametrize("argv", [
    ["handoff", ALICE, "--to", "astra"],
    ["handoff", "--last", "--to", "opus", "-C", "/repo"],
    ["handoff", "--last", "--to", "sol"],
    ["handoff", "--last", "--to", "terra", "-C", "/repo"],
])
def test_v1_handoff_spellings_reach_the_handoff_verb(argv):
    """C-17.1: v1's four `--to` targets all still parse."""
    args = translated(argv)
    assert args.handler is cli.cmd_handoff


def test_the_retired_sol_target_still_parses():
    """`--to sol` is a retired alias; `policy.retired` resolves it at submit."""
    args = parse(["handoff", "--last", "--to", "sol"])
    assert args.target == "sol"
    from subfleet.policy import DEFAULT_POLICY_PATH, load_policy, resolve_model
    assert resolve_model(load_policy(DEFAULT_POLICY_PATH), "sol", note=False) == "astra"


def test_a_v1_only_revive_flag_is_dropped_with_one_note():
    """C-17.2: a flag v2 has no name for is decided about, never silently lost."""
    mapping = compat.translate(["revive", "--no-fallback"], env={})
    assert mapping.disposition == "note"
    assert "--no-fallback" not in mapping.argv
    assert len(mapping.notes) == 1
    assert "pins never fall back" in mapping.notes[0]


def test_the_mirror_version_flag_is_refused_rather_than_performing_a_pass():
    """C-17.2: dropping it would run the pass the caller asked to identify."""
    mapping = compat.translate(["mirror", "--version"], env={})
    assert mapping.disposition == "refuse"
    assert mapping.exit_code == int(Exit.INVALID_INPUT)
    assert "subfleet -V" in mapping.notes[0]


# --- what the handlers print (C-17.3, C-17.4) ---------------------------------

def test_a_refused_revive_exits_seven_with_the_fix_on_stderr(monkeypatch, tmp_path):
    """C-17.3: exit 7 is "refused (message names the rule and the fix)"."""
    from subfleet.sessions import revive as revive_module
    held = revive_module.Attempted(session_id=ALICE, admitted=False,
                                   reason="the desktop app owns this session",
                                   fix=revive_module.OPT_IN_FIX)
    monkeypatch.setattr(sessions_cli, "_sessions", lambda args: object())
    monkeypatch.setattr(sessions_cli, "_policy", lambda args: {})
    monkeypatch.setattr(sessions_cli, "_stage", lambda args, request_id: (lambda t: t))
    monkeypatch.setattr(revive_module, "revive", lambda *a, **k: held)
    code, out, err = run(["sessions", "revive", ALICE], monkeypatch)
    assert code == int(Exit.REFUSED) == 7
    assert out == "", "stdout carries the contract, and there is none"
    assert "the desktop app owns this session" in err
    assert "fix: " in err and "--revive" in err and "handoff" in err


def test_an_admitted_revive_puts_only_the_job_id_on_stdout(monkeypatch):
    """C-17.4: stdout is one thing at a time; the prose goes to stderr."""
    from subfleet.sessions import revive as revive_module
    admitted = revive_module.Attempted(session_id=ALICE, admitted=True,
                                       job_id="20260905-120000-revive-3f9c1a2e",
                                       model="fable", reason="its own recorded model")
    monkeypatch.setattr(sessions_cli, "_sessions", lambda args: object())
    monkeypatch.setattr(sessions_cli, "_policy", lambda args: {})
    monkeypatch.setattr(sessions_cli, "_stage", lambda args, request_id: (lambda t: t))
    monkeypatch.setattr(revive_module, "revive", lambda *a, **k: admitted)
    code, out, err = run(["sessions", "revive", ALICE, "--revive"], monkeypatch)
    assert code == int(Exit.OK)
    assert out.strip() == "20260905-120000-revive-3f9c1a2e"
    assert "continues 3f9c1a2e" in err


def test_json_emits_objects_only(monkeypatch, tmp_path):
    """C-17.4: `--json` prints JSON objects and nothing else."""
    from subfleet.sessions import mirror as mirror_module
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv("SUBFLEET_HOME", str(root))
    monkeypatch.setenv("SUBFLEET_SESSION_STORE", str(tmp_path / "absent"))
    code, out, err = run(["sessions", "mirror", "--status", "--json"], monkeypatch)
    assert code == int(Exit.OK)
    assert json.loads(out)["status"] == "absent"
    assert err == ""


def test_the_nested_json_flag_does_not_overwrite_the_parents():
    """C-17.4: `_add_json(child, nested=True)`'s whole reason."""
    assert parse(["sessions", "--json", "list"]).json is True
    assert parse(["sessions", "list", "--json"]).json is True
    assert parse(["sessions", "list"]).json is False


@pytest.mark.parametrize("argv", [
    ["sessions", "--all", "--json"],
    ["sessions", "list", "--all", "--json"],
    ["sessions", "--all", "list", "--json"],
])
def test_retired_sessions_are_absent_even_when_listing_all(argv, monkeypatch, tmp_path):
    """C-23.35: --all includes dead/lane rows but never retired sessions."""
    home = fx.claude_home(tmp_path, monkeypatch)
    other = "8f2c1d90-4a7b-4f31-9c22-0d5b6e7a1234"
    fx.register(home, ALICE, os.getpid(), started_at=1.0)
    fx.register(home, other, 99999999, started_at=2.0)
    daemon = fx.FakeSessions(retired={ALICE: {"reason": "replaced"}})
    monkeypatch.setattr(sessions_cli, "_sessions", lambda args: daemon)
    code, output, errors = run(argv, monkeypatch)
    assert code == int(Exit.OK)
    assert [json.loads(row)["session_id"] for row in output.splitlines()] == [other]
    assert ALICE not in output + errors


def test_a_daemon_that_is_older_than_the_op_says_so(monkeypatch, tmp_path):
    """C-16.2: an unknown op is a version signal, not "invalid arguments"."""
    from subfleet.sessions.client import SessionsUnsupported

    def unsupported(args):
        raise SessionsUnsupported("this daemon does not answer `sessions`")

    monkeypatch.setenv("SUBFLEET_HOME", str(tmp_path))
    code, out, err = run(["sessions", "list"], monkeypatch, _sessions=unsupported)
    assert code == int(Exit.DAEMON_UNAVAILABLE) == 69
    assert "older than the sessions kit" in err or "does not answer" in err
    assert "daemon stop" in err


def test_a_bare_tickle_surveys_and_sends_nothing(monkeypatch):
    """C-17.1 preserves v1: without `--all` or `--session`, tickle printed the
    survey and nudged nobody.

    `subfleet tickle` is a command agents run to LOOK. Making it deliver would
    turn every diagnostic into a fleet-wide nudge.
    """
    from subfleet.sessions import nudge as nudge_module
    seen: list[bool] = []

    def record(sessions, policy, **kwargs):
        seen.append(kwargs["dry_run"])
        return nudge_module.Report(scope=kwargs["scope"])

    monkeypatch.setattr(sessions_cli, "_sessions", lambda args: object())
    monkeypatch.setattr(sessions_cli, "_policy", lambda args: fx.policy())
    monkeypatch.setattr(nudge_module, "sweep", record)

    for argv in (["tickle"], ["tickle", "--force"]):
        seen.clear()
        code, out, err = run_v1(argv, monkeypatch)
        assert code == int(Exit.OK) and seen == [True]
        assert "a survey, because no session was named" in err

    for argv in (["tickle", "--all"], ["tickle", "--session", ALICE], ["muster"]):
        seen.clear()
        run_v1(argv, monkeypatch)
        assert seen == [False], argv


# --- the cold sweep's two recoveries (plan decision 7) ------------------------

def test_a_cold_sweep_dispatches_nothing_without_an_explicit_recovery(monkeypatch,
                                                                      tmp_path):
    """Plan decision 7: recovery of a cold session is an EXPLICIT handoff, so a
    bare `--scope cold` lists and refuses rather than spending a lane."""
    from subfleet.sessions import revive as revive_module
    held = revive_module.Attempted(session_id=ALICE, admitted=False,
                                   reason="the desktop app owns this session",
                                   fix=revive_module.OPT_IN_FIX)
    monkeypatch.setattr(sessions_cli, "_sessions", lambda args: object())
    monkeypatch.setattr(sessions_cli, "_policy", lambda args: fx.policy())
    monkeypatch.setattr(sessions_cli, "_stage", lambda args, request_id: (lambda t: t))
    monkeypatch.setattr(revive_module, "cold_candidates",
                        lambda *a, **k: [revive_module.Candidate(session_id=ALICE)])
    monkeypatch.setattr(revive_module, "revive", lambda *a, **k: held)
    code, out, err = run(["sessions", "continue", "--scope", "cold"], monkeypatch)
    assert code == int(Exit.OK)
    assert "held" in out and ALICE[:8] in out
    assert "automatic revival of desktop-owned sessions is off" in err
    assert revive_module.OPT_IN_FIX in err


def test_a_batch_cap_of_zero_means_zero(monkeypatch):
    """C-17.1 preserves `--max`: an explicit zero requests no recoveries."""
    from subfleet.sessions import revive as revive_module
    tried: list[str] = []
    monkeypatch.setattr(sessions_cli, "_sessions", lambda args: object())
    monkeypatch.setattr(sessions_cli, "_policy", lambda args: fx.policy())
    monkeypatch.setattr(sessions_cli, "_stage", lambda args, request_id: (lambda t: t))
    monkeypatch.setattr(revive_module, "cold_candidates",
                        lambda *a, **k: [revive_module.Candidate(session_id=ALICE)])
    monkeypatch.setattr(revive_module, "revive",
                        lambda s, p, sid, **k: tried.append(sid) or
                        revive_module.Attempted(session_id=sid))
    run(["sessions", "continue", "--scope", "cold", "--max", "0"], monkeypatch)
    assert tried == []
    run(["sessions", "continue", "--scope", "cold", "--max", "1"], monkeypatch)
    assert tried == [ALICE]


def test_a_refused_revive_reports_seven_under_json_too(monkeypatch):
    """C-17.3, C-17.4: `--json` changes the output format, not the verdict."""
    from subfleet.sessions import revive as revive_module
    held = revive_module.Attempted(session_id=ALICE, admitted=False,
                                   reason="the desktop app owns this session",
                                   fix=revive_module.OPT_IN_FIX,
                                   candidate=revive_module.Candidate(session_id=ALICE))
    monkeypatch.setattr(sessions_cli, "_sessions", lambda args: object())
    monkeypatch.setattr(sessions_cli, "_policy", lambda args: fx.policy())
    monkeypatch.setattr(sessions_cli, "_stage", lambda args, request_id: (lambda t: t))
    monkeypatch.setattr(revive_module, "revive", lambda *a, **k: held)
    code, out, err = run(["sessions", "revive", ALICE, "--json"], monkeypatch)
    assert code == int(Exit.REFUSED) == 7
    assert json.loads(out)["admitted"] is False


def test_the_cold_sweep_can_hand_off_instead_of_reviving(monkeypatch):
    """Plan decision 7's other recovery, made explicit: `--handoff --to <model>`
    dispatches a continuity brief per cold session (C-23.54)."""
    from subfleet.sessions import handoff as handoff_module
    from subfleet.sessions import revive as revive_module
    dispatched: list[str] = []

    def fake_handoff(sessions, policy, *, session_id, **kwargs):
        dispatched.append(session_id)
        brief = handoff_module.Brief(text="", original="", session_id=session_id,
                                     transcript="/t.jsonl", workdir="/repo",
                                     source_cwd="/repo", redactions=2)
        return handoff_module.Dispatched(brief=brief, job_id="job-1")

    monkeypatch.setattr(sessions_cli, "_sessions", lambda args: fx.FakeSessions())
    monkeypatch.setattr(sessions_cli, "_policy", lambda args: fx.policy())
    monkeypatch.setattr(sessions_cli, "_stage", lambda args, request_id: (lambda t: t))
    monkeypatch.setattr(revive_module, "cold_candidates",
                        lambda *a, **k: [revive_module.Candidate(session_id=ALICE,
                                                                 cwd="/repo")])
    monkeypatch.setattr(handoff_module, "handoff", fake_handoff)
    code, out, err = run(["sessions", "continue", "--scope", "cold", "--handoff",
                          "--to", "astra", "--max", "0"], monkeypatch)
    assert code == int(Exit.OK) and dispatched == []
    assert "1 more cold sessions were not attempted" in err
    code, out, err = run(["sessions", "continue", "--scope", "cold", "--handoff",
                          "--to", "astra"], monkeypatch)
    assert code == int(Exit.OK)
    assert dispatched == [ALICE]
    assert "job-1" in out and "handed off to astra" in out


def test_handoff_on_the_wrong_scope_is_invalid_input(monkeypatch):
    """A live session is nudged, not handed off; saying so beats guessing."""
    code, out, err = run(["sessions", "continue", "--scope", "interrupted",
                          "--handoff", "--to", "astra"], monkeypatch)
    assert code == int(Exit.INVALID_INPUT)
    assert "--handoff applies to --scope cold" in err


def test_handoff_without_a_target_is_invalid_input(monkeypatch):
    """C-17.3: exit 2 naming the missing flag, not a traceback."""
    code, out, err = run(["sessions", "continue", "--scope", "cold", "--handoff"],
                         monkeypatch)
    assert code == int(Exit.INVALID_INPUT)
    assert "--handoff needs --to <model>" in err


def test_naming_a_lane_run_in_a_sweep_is_refused_with_the_reason(monkeypatch):
    """C-23.31: a sweep passes over a lane silently, but a person who NAMES one
    asked a question whose answer is a refusal."""
    from subfleet.sessions import nudge as nudge_module
    lane = "8f2c1d90-4a7b-4f31-9c22-0d5b6e7a1234"
    report = nudge_module.Report(scope="interrupted", outcomes=[
        nudge_module.Outcome(session_id=lane, scope="interrupted",
                             reason="headless lane run — never nudged (C-23.31)")])
    monkeypatch.setattr(sessions_cli, "_sessions", lambda args: object())
    monkeypatch.setattr(sessions_cli, "_policy", lambda args: fx.policy())
    monkeypatch.setattr(nudge_module, "sweep", lambda *a, **k: report)
    code, out, err = run(["sessions", "continue", "--session", lane], monkeypatch)
    assert code == int(Exit.REFUSED) == 7
    assert "is a headless lane run" in err and "fix: " in err


CONVERSATION = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"


@pytest.mark.parametrize("json_flag", [False, True])
def test_naming_a_conversations_session_is_refused_with_the_apps_fix(
        monkeypatch, tmp_path, json_flag):
    """C-26.13 and C-17.3: a person who names a conversation's session gets
    exit 7, the reason, and a fix that names the Subfleet app; nothing is sent."""
    home = fx.claude_home(tmp_path, monkeypatch)
    fx.register(home, CONVERSATION, os.getpid(), started_at=1.0)
    fx.transcript(home, CONVERSATION, fx.conversation_turns(turns=3))
    daemon = fx.FakeSessions(conversation_sessions=[CONVERSATION])
    monkeypatch.setattr(sessions_cli, "_sessions", lambda args: daemon)
    monkeypatch.setattr(sessions_cli, "_policy", lambda args: fx.policy())
    argv = ["sessions", "continue", "--session", CONVERSATION, "--delay", "0"]
    code, out, err = run(argv + (["--json"] if json_flag else []), monkeypatch)
    assert code == int(Exit.REFUSED) == 7
    assert daemon.pings == [] and daemon.records == []
    if json_flag:
        assert json.loads(out)["sessions"][0]["reason"].startswith("bound to a Subfleet conversation")
    else:
        assert "is bound to a Subfleet conversation" in err
        assert "fix: " in err and "Subfleet app" in err


@pytest.mark.parametrize("argv", [["sessions", "--json"], ["sessions", "list", "--all", "--json"]])
def test_a_conversations_session_is_absent_from_every_listing(argv, monkeypatch, tmp_path):
    """C-26.13: not listed, not even with `--all`."""
    home = fx.claude_home(tmp_path, monkeypatch)
    fx.register(home, CONVERSATION, os.getpid(), started_at=1.0)
    fx.transcript(home, CONVERSATION, fx.conversation_turns(turns=3))
    fx.register(home, ALICE, os.getpid(), started_at=2.0)
    fx.transcript(home, ALICE, fx.interrupted())
    monkeypatch.setattr(sessions_cli, "_sessions",
                        lambda args: fx.FakeSessions(conversation_sessions=[CONVERSATION]))
    code, output, errors = run(argv, monkeypatch)
    assert code == int(Exit.OK)
    assert [json.loads(row)["session_id"] for row in output.splitlines()] == [ALICE]


def test_handoff_of_a_conversations_session_is_refused_by_the_verb(monkeypatch, tmp_path):
    """C-26.13 through `subfleet handoff`: the verb hands the daemon's list to
    the kit, and the refusal is exit 7 with the app's fix."""
    home = fx.claude_home(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    fx.transcript(home, CONVERSATION, fx.conversation_turns(turns=3), cwd=str(repo))
    daemon = fx.FakeSessions(conversation_sessions=[CONVERSATION])
    monkeypatch.setattr(sessions_cli, "_sessions", lambda args: daemon)
    monkeypatch.setattr(sessions_cli, "_policy", lambda args: fx.policy())
    code, out, err = run(["handoff", CONVERSATION, "--to", "opus", "-C", str(repo),
                          "--dry-run"], monkeypatch)
    assert code == int(Exit.REFUSED) == 7
    assert "bound to a Subfleet conversation" in err and "Subfleet app" in err
    assert daemon.submits == []


# --- the cold scope names a conversation's session (C-26.13) ------------------

@pytest.fixture
def cold_kit(tmp_path, monkeypatch):
    """A `~/.claude` and an empty desktop store under `tmp_path`, a real repo
    directory, and the kit's daemon seams pointed at a `FakeSessions` that
    reports CONVERSATION as a conversation's and LANE as a lane run's."""
    home = fx.claude_home(tmp_path, monkeypatch)
    fx.desktop_store(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    fx.transcript(home, CONVERSATION, [{**entry, "cwd": str(repo)}
                                       for entry in fx.conversation_turns(turns=3)])
    fx.transcript(home, ALICE, [{**entry, "cwd": str(repo)} for entry in
                                fx.with_mode(fx.interrupted(), "bypassPermissions")])
    fx.transcript(home, LANE, [{**entry, "cwd": str(repo)} for entry in fx.headless()])
    daemon = fx.FakeSessions(conversation_sessions=[CONVERSATION], lane_sessions=[LANE])
    monkeypatch.setattr(sessions_cli, "_sessions", lambda args: daemon)
    monkeypatch.setattr(sessions_cli, "_policy", lambda args: fx.policy())
    monkeypatch.setattr(sessions_cli, "_stage", lambda args, request_id: (lambda t: t))
    return daemon, repo


LANE = "8f2c1d90-4a7b-4f31-9c22-0d5b6e7a1234"


@pytest.mark.parametrize("argv", [
    ["sessions", "continue", "--scope", "cold", CONVERSATION, "--revive", "--force"],
    ["sessions", "continue", "--scope", "cold", "--session", CONVERSATION],
    ["sessions", "continue", "--scope", "cold", "--session", CONVERSATION, "--dry-run"],
    ["sessions", "continue", "--scope", "cold", CONVERSATION, "--handoff", "--to", "opus"],
    ["sessions", "continue", "--scope", "cold", "--session", CONVERSATION, "--handoff",
     "--to", "astra", "--dry-run"],
], ids=["revive-force", "bare", "dry-run", "handoff", "handoff-dry-run"])
def test_the_cold_scope_refuses_a_named_conversations_session(argv, cold_kit, monkeypatch):
    """C-26.13 and C-17.3 in the cold scope, the one `subfleet revive <id>`
    reaches: naming a conversation's session exits 7 with the reason and the
    app's fix on stderr, the held row (with the fix) on stdout, and nothing
    submitted, whether the recovery asked for is a revive or a handoff."""
    daemon, _repo = cold_kit
    code, out, err = run(argv, monkeypatch)
    assert code == int(Exit.REFUSED) == 7, (out, err)
    assert f"{CONVERSATION[:8]} is bound to a Subfleet conversation" in err
    assert "fix: " in err and "Subfleet app" in err
    assert CONVERSATION[:8] in out and "Subfleet app" in out
    assert daemon.submits == [] and daemon.records == []


@pytest.mark.parametrize("scope", [["--scope", "cold", "--revive", "--force"], ["--scope", "cold", "--dry-run"],
                                   ["--scope", "cold", "--handoff", "--to", "opus"], ["--delay", "0"]],
                         ids=["cold-revive", "cold-dry-run", "cold-handoff", "interrupted"])
@pytest.mark.parametrize("named_as", [str.upper, str.lower], ids=["typed-upper", "typed-lower"])
@pytest.mark.parametrize("listed_as", [str.upper, str.lower], ids=["listed-upper", "listed-lower"])
def test_a_conversations_session_named_in_either_case_is_refused(scope, named_as, listed_as, cold_kit, monkeypatch):
    """C-26.3, C-26.13 (review of 3c1a34e's fixes): the refusal's exit code and
    reason do not depend on the case the person types the session in or the
    daemon lists it in: naming only a conversation's session exits 7, and
    nothing is sent or submitted."""
    daemon, _repo = cold_kit
    daemon.conversation_sessions = [listed_as(CONVERSATION)]
    code, out, err = run(["sessions", "continue", *scope, "--session", named_as(CONVERSATION)], monkeypatch)
    assert code == int(Exit.REFUSED) == 7, (out, err)
    assert "bound to a Subfleet conversation" in err and "Subfleet app" in err
    assert daemon.submits == [] and daemon.records == [] and daemon.pings == []


@pytest.mark.parametrize("named_as, refused_as", [(str.upper, str.lower), (str.lower, str.upper)])
def test_the_refusal_compares_the_named_and_refused_sessions_in_either_case(named_as, refused_as):
    """C-26.3 (review of 3c1a34e's fixes): a request that names only sessions the
    kit refused is refused (exit 7) whichever case the request and the refusal
    spell each UUID in; one that also names another session is not."""
    assert sessions_cli._fenced_refusal("v", [named_as(CONVERSATION)], conversations=[refused_as(CONVERSATION)],
                                        lanes=[], quiet=True) == int(Exit.REFUSED)
    assert sessions_cli._fenced_refusal("v", [named_as(LANE)], conversations=[], lanes=[refused_as(LANE)],
                                        quiet=True) == int(Exit.REFUSED)
    assert sessions_cli._fenced_refusal("v", [named_as(CONVERSATION), ALICE],
                                        conversations=[refused_as(CONVERSATION)], lanes=[], quiet=True) is None


@pytest.mark.parametrize("handoff", [False, True], ids=["revive", "handoff"])
def test_the_cold_scope_refusal_holds_under_json(handoff, cold_kit, monkeypatch):
    """C-26.13 with C-17.3 and C-17.4: `--json` changes the format, not the
    verdict; stdout is the JSON object, and the refusal is its exit code."""
    daemon, _repo = cold_kit
    argv = ["sessions", "continue", "--scope", "cold", CONVERSATION, "--json"]
    argv += ["--handoff", "--to", "opus"] if handoff else ["--revive"]
    code, out, err = run(argv, monkeypatch)
    assert code == int(Exit.REFUSED) == 7
    rows = json.loads(out)["sessions"]
    assert len(rows) == 1 and rows[0]["session_id"] == CONVERSATION
    assert rows[0]["reason"].startswith("bound to a Subfleet conversation")
    assert "Subfleet app" in rows[0]["fix"]
    assert err == "" and daemon.submits == []


@pytest.mark.parametrize("argv", [
    ["revive", CONVERSATION],
    ["revive", CONVERSATION, "--revive"],
    ["revive", "--session", CONVERSATION, "--revive", "--dry-run"],
])
def test_v1s_revive_spelling_naming_a_conversations_session_exits_seven(argv, cold_kit, monkeypatch):
    """C-26.13 through C-17.1's v1 spelling: `compat` maps `subfleet revive
    <id>` to `sessions continue --scope cold`, so agents and skills that still
    use it get the same refusal."""
    daemon, _repo = cold_kit
    code, out, err = run_v1(argv, monkeypatch)
    assert code == int(Exit.REFUSED) == 7, (out, err)
    assert "bound to a Subfleet conversation" in err and "Subfleet app" in err
    assert daemon.submits == []


def test_the_cold_scope_refuses_a_named_lane_run_the_same_way(cold_kit, monkeypatch):
    """C-23.31 in the cold scope: a named headless lane run is refused with the
    reason and the lane's fix, as the interrupted scope refuses one."""
    daemon, _repo = cold_kit
    code, out, err = run(["sessions", "continue", "--scope", "cold", LANE, "--revive"],
                         monkeypatch)
    assert code == int(Exit.REFUSED) == 7, (out, err)
    assert f"{LANE[:8]} is a headless lane run" in err and "runs show" in err
    assert daemon.submits == []


def test_a_cold_handoff_holds_a_conversations_session_and_hands_off_the_rest(cold_kit, monkeypatch):
    """C-26.13 in `--scope cold --handoff`: the conversation's session is held
    with its reason and fix and nothing is submitted for it; the other named
    session is still handed off. A request that is not refused whole exits 0."""
    daemon, repo = cold_kit
    code, out, err = run(["sessions", "continue", "--scope", "cold", CONVERSATION, ALICE,
                          "--handoff", "--to", "astra"], monkeypatch)
    assert code == int(Exit.OK), (out, err)
    assert [args.name for args in daemon.submits] == [f"handoff-{ALICE[:8]}"]
    assert daemon.submits[0].workdir == str(repo.resolve())
    held = next(line for line in out.splitlines() if CONVERSATION[:8] in line)
    assert "bound to a Subfleet conversation" in held
    assert "fix: continue it in the Subfleet app" in out
    assert "handed off to astra" in out


def test_a_cold_handoff_checks_the_daemons_list_again_at_handoff(cold_kit, monkeypatch):
    """C-26.13, the second fence: a candidate the census did not mark (here a
    session that became a conversation's after the scan) is refused by
    `handoff` against the daemon's list read for the handoff, with exit 7 and
    the fix, and the rest of the batch still goes (it once raised out of the
    loop and dropped the rest)."""
    from subfleet.sessions import revive as revive_module
    daemon, repo = cold_kit
    missed = [revive_module.Candidate(session_id=CONVERSATION, cwd=str(repo)),
              revive_module.Candidate(session_id=ALICE, cwd=str(repo))]
    monkeypatch.setattr(revive_module, "cold_candidates", lambda *a, **k: missed)
    code, out, err = run(["sessions", "continue", "--scope", "cold", "--handoff",
                          "--to", "astra", "--json"], monkeypatch)
    assert code == int(Exit.OK), (out, err)
    rows = {row["session_id"]: row for row in json.loads(out)["sessions"]}
    assert rows[CONVERSATION]["job_id"] is None
    assert "bound to a Subfleet conversation" in rows[CONVERSATION]["reason"]
    assert "Subfleet app" in rows[CONVERSATION]["fix"]
    assert rows[ALICE]["job_id"] == "job-1"
    assert [args.name for args in daemon.submits] == [f"handoff-{ALICE[:8]}"]
    assert [] in daemon.state_calls, "the handoff read the daemon's lists"

    code, out, err = run(["sessions", "continue", "--scope", "cold", "--handoff",
                          "--to", "astra", CONVERSATION], monkeypatch)
    assert code == int(Exit.REFUSED) == 7, (out, err)
    assert "Subfleet app" in err


def test_sessions_revive_dry_run_of_a_conversations_session_is_still_refused(cold_kit, monkeypatch):
    """C-26.13: `--dry-run` rehearses a revive; it does not turn the refusal of
    a conversation's session into an answer (as `handoff --dry-run` does not)."""
    daemon, _repo = cold_kit
    for extra in ([], ["--json"]):
        code, out, err = run(["sessions", "revive", CONVERSATION, "--revive", "--dry-run",
                              *extra], monkeypatch)
        assert code == int(Exit.REFUSED) == 7, (extra, out, err)
    assert daemon.submits == []


def test_sessions_revive_dry_run_of_an_ordinary_hold_still_exits_zero(monkeypatch):
    """C-17.3: any other hold under `--dry-run` is the answer the rehearsal was
    asked for, as before."""
    from subfleet.sessions import revive as revive_module
    held = revive_module.Attempted(session_id=ALICE, admitted=False,
                                   reason="the desktop app owns this session",
                                   fix=revive_module.OPT_IN_FIX,
                                   candidate=revive_module.Candidate(session_id=ALICE))
    monkeypatch.setattr(sessions_cli, "_sessions", lambda args: object())
    monkeypatch.setattr(sessions_cli, "_policy", lambda args: fx.policy())
    monkeypatch.setattr(sessions_cli, "_stage", lambda args, request_id: (lambda t: t))
    monkeypatch.setattr(revive_module, "revive", lambda *a, **k: held)
    code, out, err = run(["sessions", "revive", ALICE, "--dry-run"], monkeypatch)
    assert code == int(Exit.OK) and "the desktop app owns this session" in out


# --- a daemon older than C-26.13 (no `conversation_sessions`) -----------------

@pytest.fixture
def older_daemon(tmp_path, monkeypatch):
    """The window between a code update and `subfleet daemon stop/start`: the
    CLI is new, and the daemon answers `sessions state` as b739a12's did, with
    `sessions` and `lane_sessions` only, although it already ran conversations.

    CONVERSATION is a conversation's session after three turns, in
    `bypassPermissions`, cold and unregistered: `headless_transcript` no longer
    calls it a lane (C-23.31), and nothing else would stop `--revive --force`.
    LIVE is the same shape with a live registry row, for the nudge and the
    listing."""
    home = fx.claude_home(tmp_path, monkeypatch)
    fx.desktop_store(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    for session in (CONVERSATION, LIVE):
        fx.transcript(home, session, [{**entry, "cwd": str(repo)} for entry in
                                      fx.with_mode(fx.conversation_turns(turns=3),
                                                   "bypassPermissions")])
    fx.register(home, LIVE, os.getpid(), started_at=1.0)
    daemon = fx.FakeSessions(conversation_sessions=[CONVERSATION, LIVE],
                             reports_conversations=False)
    monkeypatch.setattr(sessions_cli, "_sessions", lambda args: daemon)
    monkeypatch.setattr(sessions_cli, "_policy", lambda args: fx.policy())
    monkeypatch.setattr(sessions_cli, "_stage", lambda args, request_id: (lambda t: t))
    return daemon, repo


LIVE = "5b4a3c2d-1e0f-4a9b-8c7d-6e5f4a3b2c1d"


@pytest.mark.parametrize("argv", [
    ["sessions", "revive", CONVERSATION, "--revive", "--force"],
    ["sessions", "revive", CONVERSATION, "--revive", "--force", "--json"],
    ["sessions", "continue", "--scope", "cold", "--revive", "--force"],
    ["sessions", "continue", "--scope", "cold", CONVERSATION, "--revive", "--force"],
    ["sessions", "continue", "--scope", "cold", "--handoff", "--to", "astra"],
    ["sessions", "continue", "--session", LIVE, "--delay", "0"],
    ["sessions", "continue", "--scope", "idle", "--all", "--delay", "0"],
    ["sessions", "list", "--all", "--json"],
    ["handoff", CONVERSATION, "--to", "opus", "--dry-run"],
], ids=["revive", "revive-json", "cold-sweep", "cold-named", "cold-handoff",
        "interrupted", "idle", "list", "handoff"])
def test_a_daemon_older_than_the_conversation_fence_is_refused_not_trusted(
        argv, older_daemon, monkeypatch):
    """C-26.13 with C-25.1 and C-17.3: a `state` reply without
    `conversation_sessions` is a daemon older than the clause, not a daemon
    with no conversations. Every verb that reads it exits 69 with the restart
    as its fix, and nothing is submitted, nudged or recorded. Before this, the
    kit read the silence as an empty list and `sessions revive --revive
    --force` submitted a revive of a conversation's session."""
    daemon, repo = older_daemon
    if argv[0] == "handoff":
        argv = [*argv, "-C", str(repo)]
    code, out, err = run(argv, monkeypatch)
    assert code == int(Exit.DAEMON_UNAVAILABLE) == 69, (out, err)
    assert "conversation_sessions" in err and "C-26.13" in err
    assert "fix: subfleet daemon stop && subfleet daemon start" in err
    assert out == ""
    assert daemon.submits == [] and daemon.pings == [] and daemon.records == []
    assert daemon.revives == {} and daemon.state_calls, "the reply was read, then refused"


def test_a_sweep_that_merely_passed_over_a_lane_still_exits_zero(monkeypatch):
    """The exit code reports whether the sweep ran, not whether every session
    in the fleet qualified."""
    from subfleet.sessions import nudge as nudge_module
    lane = "8f2c1d90-4a7b-4f31-9c22-0d5b6e7a1234"
    report = nudge_module.Report(scope="interrupted", outcomes=[
        nudge_module.Outcome(session_id=lane, scope="interrupted",
                             reason="headless lane run — never nudged (C-23.31)")])
    monkeypatch.setattr(sessions_cli, "_sessions", lambda args: object())
    monkeypatch.setattr(sessions_cli, "_policy", lambda args: fx.policy())
    monkeypatch.setattr(nudge_module, "sweep", lambda *a, **k: report)
    code, out, err = run(["sessions", "continue", "--all"], monkeypatch)
    assert code == int(Exit.OK)


# --- the help text stays honest ------------------------------------------------

def test_the_scope_choices_match_the_module(monkeypatch):
    """A scope the parser accepts and the sweep does not is a broken verb."""
    from subfleet.sessions import nudge
    assert tuple(sessions_cli.SCOPE_CHOICES) == nudge.SCOPES
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["sessions", "continue", "--scope", "nonsense"])
