"""Which sessions revive admits, and on what: C-23.20, C-23.31, C-23.35, C-23.39.

Every test names the clause it proves (C-20.5). Revive is the one exclusive tool
in the kit — it starts a second process inside somebody else's conversation — so
most of these tests are refusals, and each refusal names the fix.
"""

from __future__ import annotations

import os
from pathlib import Path

import hypothesis
import pytest

from subfleet.contracts import OutcomeClass, Sandbox
from subfleet.sessions import revive, transcripts
from tests import sessions_fixtures as fx
from tests import spellings

COLD = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
LANE = "8f2c1d90-4a7b-4f31-9c22-0d5b6e7a1234"
ACCOUNT = "1c4f0b2a-9d3e-4a5b-8c7d-6e5f4a3b2c1d"
ORG = "2d5e1c3b-0e4f-4b6c-9d8e-7f6a5b4c3d2e"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A `~/.claude` and a desktop session store, both relocated."""
    home = fx.claude_home(tmp_path, monkeypatch)
    store = fx.desktop_store(tmp_path, monkeypatch)
    return home, store


@pytest.fixture
def policy():
    return fx.policy()


def cold_session(world, session_id: str = COLD, *, age_s: float = 1800,
                 mode: str = "bypassPermissions", model: str = "claude-fable-5-1",
                 desktop_owned: bool = True, entries=None, cwd: str | None = None):
    """A cold, interrupted, desktop-owned session in `bypassPermissions`."""
    home, store = world
    body = entries if entries is not None else fx.interrupted(age_s=age_s)
    fx.transcript(home, session_id, fx.with_mode(body, mode))
    if desktop_owned:
        fx.index_entry(store, ACCOUNT, ORG, session_id, mode=mode, model=model,
                       cwd=cwd or fx.WORKDIR)
    return session_id


def attempt(daemon, policy, session_id: str, tmp_path: Path, **kwargs):
    staged = tmp_path / "prompt.md"

    def stage(text: str) -> Path:
        staged.write_text(text, encoding="utf-8")
        return staged

    return revive.revive(daemon, policy, session_id, stage_prompt=stage,
                         now=fx.NOW, **kwargs)


# --- plan decision 7: off by default for desktop-owned sessions ---------------

def test_a_desktop_owned_session_is_refused_and_the_fix_names_revive_and_handoff(
        world, policy, tmp_path):
    """Plan decision 7 and C-6.5's shape: a refusal names the rule and the fix.

    A lease in subfleet's store binds only launches subfleet makes, so it cannot
    make a headless revive exclusive against a desktop restart that lands
    between the census and the launch — the 2026-09-04 twin. Recovery of a cold
    session therefore defaults to an explicit handoff.
    """
    cold_session(world)
    daemon = fx.FakeSessions()
    result = attempt(daemon, policy, COLD, tmp_path)
    assert result.admitted is False
    assert daemon.submits == []
    assert "the desktop app owns this session" in result.reason
    assert "sessions.auto_revive_desktop_owned" in result.reason
    assert "--revive" in result.fix and "handoff" in result.fix


def test_the_opt_in_submits_the_job(world, policy, tmp_path):
    """Plan decision 7: `--revive` is how the operator says otherwise."""
    cold_session(world)
    daemon = fx.FakeSessions()
    result = attempt(daemon, policy, COLD, tmp_path, opt_in=True)
    assert result.admitted is True
    assert result.job_id == "job-1"
    assert len(daemon.submits) == 1


def test_the_policy_switch_admits_without_the_flag(world, tmp_path):
    """C-6.4: `sessions.auto_revive_desktop_owned` is a policy change, not a code one."""
    cold_session(world)
    daemon = fx.FakeSessions()
    result = attempt(daemon, fx.policy(auto_revive_desktop_owned=True), COLD, tmp_path)
    assert result.admitted is True


def test_a_session_the_desktop_store_does_not_know_needs_no_opt_in(world, policy, tmp_path):
    """Plan decision 7 applies to sessions the APP owns; a tmux CLI session is
    not one, and the clause's whole reason — the app restarting it under us —
    does not apply."""
    cold_session(world, desktop_owned=False)
    daemon = fx.FakeSessions()
    result = attempt(daemon, policy, COLD, tmp_path)
    assert result.admitted is True
    assert result.candidate.desktop_owned is False


@pytest.mark.parametrize("bad", ["symlink", "oversized", "cut-short"])
def test_an_unreadable_desktop_record_leaves_ownership_unknown_and_revival_refused(
        world, policy, tmp_path, bad):
    """C-23.35, plan decision 7 (review of 7da13417, finding 3): a desktop record
    that cannot be read may be this session's own. The session then reads as
    possibly owned, not as one the app does not know, and automatic revival is
    refused as for an owned one; `--revive` still admits it. A FIFO record is
    covered in a child process by `test_sessions_revive_reads.py`."""
    _home, store = world
    cold_session(world, desktop_owned=False)
    record = fx.index_entry(store, ACCOUNT, ORG, COLD)
    body = record.read_text()
    if bad == "symlink":
        target = tmp_path / "elsewhere.json"
        target.write_text(body)
        record.unlink()
        record.symlink_to(target)
    elif bad == "oversized":
        record.write_text(body[:-1] + ', "padding": "' + "a" * (1024 * 1024) + '"}')
    else:
        record.write_text(body[: len(body) // 2])     # a write the app has not finished
    daemon = fx.FakeSessions()
    result = attempt(daemon, policy, COLD, tmp_path)
    assert result.admitted is False
    assert daemon.submits == []
    assert result.candidate.desktop_owned is None
    assert "may own this session" in result.reason and str(record) in result.reason
    assert result.candidate.to_dict()["unreadable_records"] == [str(record)]
    assert result.fix == revive.OPT_IN_FIX
    assert attempt(daemon, policy, COLD, tmp_path, opt_in=True).admitted is True


def test_a_store_directory_that_cannot_be_listed_leaves_ownership_unknown(world, policy, tmp_path):
    """C-23.35 (review of the round-2 fixes, F4): `Path.glob` skipped a directory it
    could not list, and the session whose record it held read as not owned. The
    directory is now reported, and ownership is unknown until it can be read."""
    _home, store = world
    cold_session(world)
    locked = store / ACCOUNT / ORG
    locked.chmod(0)
    try:
        daemon = fx.FakeSessions()
        result = attempt(daemon, policy, COLD, tmp_path)
        assert result.admitted is False and daemon.submits == []
        assert result.candidate.desktop_owned is None
        assert result.candidate.unreadable_records == (str(locked),)
        assert str(locked) in result.reason
    finally:
        locked.chmod(0o700)
    assert attempt(fx.FakeSessions(), policy, COLD, tmp_path).candidate.desktop_owned is True


def test_a_desktop_record_without_a_cwd_still_marks_the_session_owned(world, policy, tmp_path):
    """C-23.35, plan decision 7: a session present in the desktop store at all is
    one the app owns. A record naming it without a cwd had read as not owned, and
    lifted the refusal; the cwd then comes from the transcript."""
    _home, store = world
    cold_session(world, desktop_owned=False)
    fx.index_entry(store, ACCOUNT, ORG, COLD, cwd="")
    daemon = fx.FakeSessions()
    result = attempt(daemon, policy, COLD, tmp_path)
    assert result.admitted is False
    assert result.candidate.desktop_owned is True
    assert "the desktop app owns this session" in result.reason
    assert result.candidate.cwd == transcripts.last_cwd(Path(result.candidate.transcript))


# --- the candidate filters (C-23.31, C-23.35) ---------------------------------

def test_revive_never_continues_a_headless_lane_run(world, policy, tmp_path):
    """C-23.31: a lane's continuation has no reader; it only burns a window.

    Ledger row 187. On 2026-09-04 the sweep revived five dead `claude -p` lane
    runs as untracked continuations on lane tokens.
    """
    cold_session(world, LANE, entries=fx.headless(age_s=1800), desktop_owned=False)
    daemon = fx.FakeSessions(lane_sessions=[LANE])
    result = attempt(daemon, policy, LANE, tmp_path, opt_in=True)
    assert result.admitted is False
    assert "headless lane run" in result.reason
    assert daemon.submits == []


def test_a_lane_is_refused_from_its_transcript_even_with_no_recorded_marker(
        world, policy, tmp_path):
    """C-23.31: the recorded marker wins; the transcript shape is the fallback."""
    cold_session(world, LANE, entries=fx.headless(age_s=1800), desktop_owned=False)
    result = attempt(fx.FakeSessions(), policy, LANE, tmp_path, opt_in=True)
    assert "headless lane run" in result.reason


def test_retired_session_never_listed_or_revived(world, policy, tmp_path):
    """C-23.35: retirement is a durable operator flag; a retired session is never
    a revive candidate until the operator clears it.

    Ledger row 191. Killing a replaced orchestrator's tmux leaves an
    "interrupted" transcript the sweep would otherwise resurrect headlessly
    (observed 2026-09-04 11:5x, pid 4808 on dca16909).
    """
    cold_session(world)
    daemon = fx.FakeSessions(retired={COLD: {"reason": "replaced orchestrator"}})
    result = attempt(daemon, policy, COLD, tmp_path, opt_in=True)
    assert result.admitted is False
    assert "retired by the operator (replaced orchestrator)" in result.reason
    assert result.fix == f"subfleet sessions unretire {COLD}"
    assert daemon.submits == []

    daemon.unretire(COLD)
    assert attempt(daemon, policy, COLD, tmp_path, opt_in=True).admitted is True


def test_revive_requires_bypass_permissions_mode(world, policy, tmp_path):
    """C-23.35: a headless run in any other mode would deny its own tools.

    Ledger row 188.
    """
    cold_session(world, mode="acceptEdits")
    daemon = fx.FakeSessions()
    result = attempt(daemon, policy, COLD, tmp_path, opt_in=True)
    assert result.admitted is False
    assert "permission mode acceptEdits" in result.reason
    assert "deny its own tools" in result.reason
    assert daemon.submits == []


def test_a_live_session_is_nudged_never_revived(world, policy, tmp_path):
    """C-23.55's premise: revive is for a session whose process died.

    A live one has an inbox, and pushing a message into it creates no second
    writer — which is the whole reason tickle stays automatic and revive does not.
    """
    home, _store = world
    cold_session(world)
    fx.register(home, COLD, os.getpid(), started_at=1.0)
    result = attempt(fx.FakeSessions(), policy, COLD, tmp_path, opt_in=True)
    assert result.admitted is False
    assert "already running" in result.reason and str(os.getpid()) in result.reason
    assert "sessions continue" in result.fix


def test_a_one_shot_husk_is_not_resumable_work(world, policy, tmp_path):
    """C-23.35's spirit: there is no conversation to resume.

    Our own lane probes, and a `-p` that never answered, end this way.
    """
    home, _store = world
    fx.transcript(home, COLD, fx.with_mode(
        [fx.typed_prompt("hello", uuid="p", at=fx.ago(1800))], "bypassPermissions"))
    result = attempt(fx.FakeSessions(), policy, COLD, tmp_path, opt_in=True)
    assert "no assistant history" in result.reason


def test_a_session_younger_than_the_minimum_is_left_to_the_app(world, policy, tmp_path):
    """C-23.33's cap read from the other end: the app may still restart it."""
    cold_session(world, age_s=30)
    result = attempt(fx.FakeSessions(), policy, COLD, tmp_path, opt_in=True)
    assert result.admitted is False
    assert "the app may still restart it" in result.reason


def test_a_completed_session_is_not_a_revive_candidate(world, policy, tmp_path):
    """C-23.35: only a cut-off turn is work that was interrupted."""
    cold_session(world, entries=fx.completed(age_s=1800))
    result = attempt(fx.FakeSessions(), policy, COLD, tmp_path, opt_in=True)
    assert result.admitted is False
    assert result.reason.startswith("completed:")


def test_a_session_with_no_cwd_has_nowhere_to_run(world, policy, tmp_path):
    """C-23.35: v1 skipped these too; the fix names `-C`."""
    home, _store = world
    entries = [dict(entry) for entry in fx.with_mode(fx.interrupted(), "bypassPermissions")]
    for entry in entries:
        entry.pop("cwd", None)
    fx.transcript(home, COLD, entries)
    result = attempt(fx.FakeSessions(), policy, COLD, tmp_path, opt_in=True)
    assert result.admitted is False
    assert "no cwd found" in result.reason and "-C" in result.fix


# --- the tier (C-23.39) -------------------------------------------------------

def test_revive_without_model_flag_keeps_recorded_tier(world, policy, tmp_path):
    """C-23.39: revive launches on the tier recorded in the session's own row.

    Ledger row 186; 2026-08-26 (Max): a Fable-grade session on Opus is worse
    than a parked one, so cross-tier substitution is an explicit choice.
    """
    cold_session(world, model="claude-opus-5")
    daemon = fx.FakeSessions()
    result = attempt(daemon, policy, COLD, tmp_path, opt_in=True)
    assert result.model == "claude-opus-5"
    assert daemon.submits[0].pinned_model == "claude-opus-5"
    assert "the session's own recorded model" in result.reason


def test_an_explicit_model_is_used_and_the_substitution_is_recorded(world, policy, tmp_path):
    """C-23.39: a different tier is used only with `--model`, and it is recorded."""
    cold_session(world, model="claude-fable-5-1")
    daemon = fx.FakeSessions()
    result = attempt(daemon, policy, COLD, tmp_path, opt_in=True, model="opus")
    assert result.model == "opus"
    assert "substituted opus for claude-fable-5-1" in result.reason


def test_a_retired_pin_revives_on_the_current_id_of_that_tier(world, policy, tmp_path):
    """C-23.39: a session last served by a retired pin never revives on it."""
    cold_session(world, model="claude-fable-5")
    daemon = fx.FakeSessions()
    result = attempt(daemon, policy, COLD, tmp_path, opt_in=True)
    assert result.model == "fable"
    assert "is retired" in result.reason


def test_a_session_with_no_recorded_model_is_routed_by_task_and_tier(world, policy, tmp_path):
    """C-23.39's edge: without a recorded model, routing picks — it is not silent."""
    home, _store = world
    entries = fx.with_mode(fx.interrupted(), "bypassPermissions")
    for entry in entries:
        entry.get("message", {}).pop("model", None)
    fx.transcript(home, COLD, entries)
    daemon = fx.FakeSessions()
    result = attempt(daemon, policy, COLD, tmp_path, opt_in=True)
    assert result.model is None
    assert daemon.submits[0].task and daemon.submits[0].tier
    assert "routed as" in result.reason



def test_previous_revive_history_does_not_replace_live_lease_admission(
        world, policy, tmp_path):
    """C-23.55 limits live revives; history alone cannot refuse a later retry."""
    cold_session(world)
    daemon = fx.FakeSessions(revives={COLD: {"dedupe_key": "cut",
                                           "job_id": "finished-revive"}})
    result = attempt(daemon, policy, COLD, tmp_path, opt_in=True)
    assert result.admitted is True
    assert len(daemon.submits) == 1

def test_the_substitution_is_recorded_durably(world, policy, tmp_path):
    """C-23.39: "a different tier is used only when the operator passes
    `--model`, and the substitution is recorded"."""
    cold_session(world, model="claude-fable-5-1")
    daemon = fx.FakeSessions()
    attempt(daemon, policy, COLD, tmp_path, opt_in=True, model="opus")
    record = daemon.revives[COLD]
    assert record["model"] == "opus"
    assert record["recorded_model"] == "claude-fable-5-1"
    assert record["substituted"] is True
    assert "substituted opus for claude-fable-5-1" in record["why"]


# --- the job it submits (C-23.54, C-6.5) --------------------------------------

def test_the_revive_job_is_writable_in_place_and_owned_by_its_own_session(
        world, policy, tmp_path):
    """C-23.54 and C-6.5: a revive is an ordinary submission with the session as
    its caller, so the one-writable-job-per-session rule refuses the twin.

    `in_place` because a revive continues the session in its own worktree;
    `no_preamble` because the prompt IS the continuation instruction.
    """
    cold_session(world)
    daemon = fx.FakeSessions()
    attempt(daemon, policy, COLD, tmp_path, opt_in=True)
    args = daemon.submits[0]
    assert args.kind == "revive"
    assert args.sandbox == Sandbox.WORKSPACE_WRITE.value
    assert args.in_place is True
    assert args.no_preamble is True
    assert args.caller_session == COLD, "the job belongs to the session it continues"
    assert args.workdir == fx.WORKDIR
    assert args.max_attempts == 1, "a retry would be a second continuation"
    assert args.name == f"revive-{COLD[:8]}"


def test_the_prompt_is_the_revive_message_with_its_question_exception(
        world, policy, tmp_path):
    """The message a revived session receives, carried over from v1 verbatim.

    Its exception exists because a session whose last message asked Max a
    question must not answer it on his behalf.
    """
    cold_session(world)
    daemon = fx.FakeSessions()
    attempt(daemon, policy, COLD, tmp_path, opt_in=True)
    prompt = Path(daemon.submits[0].prompt_path).read_text(encoding="utf-8")
    assert prompt == revive.REVIVE_MESSAGE
    assert "re-state the open question in one line and stop" in prompt


def test_a_dry_run_decides_everything_and_submits_nothing(world, policy, tmp_path):
    """C-17.4: the verdict is the whole point of `--dry-run`."""
    cold_session(world)
    daemon = fx.FakeSessions()
    result = attempt(daemon, policy, COLD, tmp_path, opt_in=True, dry_run=True)
    assert daemon.submits == []
    assert "would revive on claude-fable-5-1" in result.reason


# --- the cold sweep -----------------------------------------------------------

def test_the_cold_sweep_sees_neither_live_sessions_nor_lanes(world, policy):
    """C-23.31 and C-23.55: a cold candidate has no process and is not a lane."""
    home, _store = world
    cold_session(world)
    cold_session(world, LANE, entries=fx.headless(age_s=1800), desktop_owned=False)
    fx.transcript(home, "live-one", fx.with_mode(fx.interrupted(), "bypassPermissions"))
    fx.register(home, "live-one", os.getpid(), started_at=1.0)
    daemon = fx.FakeSessions(lane_sessions=[LANE])
    found = revive.cold_candidates(daemon, policy, now=fx.NOW)
    assert [item.session_id for item in found] == [COLD]


# --- the CLI's own vocabulary (C-17.3) ----------------------------------------

def test_old_claude_cli_reported_as_host_fault_not_no_lane(world, policy, tmp_path):
    """C-17.3: a too-old Claude binary is a host fault, never "no lane serves".

    Ledger row 190; 2026-09-02, launchd ran a Homebrew cask at 2.1.87 while
    `~/.local/bin/claude` was 2.1.258. v1 probed lanes itself and had to make
    this distinction by hand. In v2 the probe is the daemon's and the adapter
    already classifies it as its own outcome class with its own exit code, so
    the kit's job is to never invent capacity language of its own — walking more
    lanes cannot help, and reporting it as capacity parks sessions indefinitely.
    """
    from subfleet.adapters.claude import CLI_TOO_OLD_RE
    from subfleet.contracts import Exit
    banner = ("API Error: 400 Claude Code 2.1.87 does not support this model; "
              "version 2.1.251 or newer is required")
    assert CLI_TOO_OLD_RE.search(banner)
    assert OutcomeClass.CLI_TOO_OLD.value == "cli-too-old" != OutcomeClass.LIMITED.value
    assert int(Exit.CLI_TOO_OLD) == 6 != int(Exit.NO_LANE)

    cold_session(world)
    daemon = fx.FakeSessions()
    result = attempt(daemon, policy, COLD, tmp_path, opt_in=True)
    every_reason = " ".join(filter(None, (result.reason, result.fix)))
    assert "no lane serves" not in every_reason


# --- a conversation's session (C-26.13) ---------------------------------------

CONVERSATION = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"


@pytest.mark.parametrize("opt_in,force", [(False, False), (True, False), (True, True)])
def test_a_conversations_session_is_never_revived(world, policy, tmp_path, opt_in, force):
    """C-26.13: the conversation is the session's one writer, so a headless
    revive beside it is the 2026-09-04 twin; `--revive` and `--force` do not
    change that, and the fix points at the Subfleet app."""
    cold_session(world, CONVERSATION, entries=fx.conversation_turns(turns=3))
    daemon = fx.FakeSessions(conversation_sessions=[CONVERSATION])
    result = attempt(daemon, policy, CONVERSATION, tmp_path, opt_in=opt_in, force=force)
    assert result.admitted is False
    assert daemon.submits == [] and daemon.revives == {}
    assert result.reason.startswith("bound to a Subfleet conversation")
    assert "Subfleet app" in result.fix
    assert result.candidate.conversation is True


def test_a_short_conversation_is_refused_as_a_conversation_not_as_a_lane(world, policy, tmp_path):
    """C-26.13 before C-23.31: a one-turn conversation's transcript looks like a
    lane run, and the lane refusal's reason would send the operator the wrong way."""
    cold_session(world, CONVERSATION, entries=fx.conversation_turns(turns=1), desktop_owned=False)
    daemon = fx.FakeSessions(conversation_sessions=[CONVERSATION])
    result = attempt(daemon, policy, CONVERSATION, tmp_path, opt_in=True)
    assert result.candidate.lane is True
    assert result.reason.startswith("bound to a Subfleet conversation")


def test_the_cold_sweep_never_sees_a_conversations_session(world, policy):
    """C-26.13: a cold sweep is a directory scan, and a conversation's session is
    not in it; named, it is inspected so the refusal can say why."""
    cold_session(world)
    cold_session(world, CONVERSATION, entries=fx.conversation_turns(turns=3))
    daemon = fx.FakeSessions(conversation_sessions=[CONVERSATION])
    assert [item.session_id for item in revive.cold_candidates(daemon, policy, now=fx.NOW)] == [COLD]
    named = revive.cold_candidates(daemon, policy, only=[CONVERSATION], now=fx.NOW)
    assert [item.conversation for item in named] == [True]


@pytest.mark.parametrize("request_id,given,minted", [
    (None, None, True), ("operator-rid", None, False),
    ("cli-minted", True, True),            # what every sessions CLI path passes
    ("cli-minted", False, False)])
def test_c16_3_revive_says_whether_it_minted_the_request_id(world, policy, tmp_path,
                                                             request_id, given, minted):
    """C-16.3: a revive's own fresh id makes a job found after a refused re-send its own;
    an operator's id keeps the refusal. The kit is told which, and an explicit `minted`
    (the CLI mints the id before calling) reaches it unchanged."""
    cold_session(world)
    daemon = fx.FakeSessions()
    extra = {} if given is None else {"minted": given}
    attempt(daemon, policy, COLD, tmp_path, opt_in=True, request_id=request_id, **extra)
    assert daemon.minted == [minted]


@pytest.mark.parametrize("direction", sorted(spellings.DIRECTIONS))
def test_a_conversation_bound_session_is_never_revived_or_cold_swept_in_either_direction(
        world, policy, tmp_path, direction):
    """C-26.3, C-26.13 (review of 3c1a34e, finding 5): the daemon lists the
    session as its store recorded it and its transcript is named in another
    case, either way round. An unnamed cold sweep leaves it out; named in
    either spelling it is held as a conversation's and never admitted, even
    opted in and forced; revive refuses it."""
    listed_as, named_as = spellings.DIRECTIONS[direction]
    cold_session(world, named_as(COLD), desktop_owned=False)
    daemon = fx.FakeSessions(conversation_sessions=[listed_as(COLD)])
    assert revive.cold_candidates(daemon, policy, now=fx.NOW) == []
    for named in (named_as(COLD), listed_as(COLD)):
        (candidate,) = revive.cold_candidates(daemon, policy, only=[named], now=fx.NOW)
        assert candidate.conversation
        assert revive.admits(candidate, policy=policy, opt_in=True, force=True)[0] is False
        result = attempt(daemon, policy, named, tmp_path, opt_in=True, force=True)
        assert result.admitted is False and result.reason.startswith("bound to a Subfleet conversation")
    assert daemon.submits == []


@hypothesis.settings(max_examples=100, deadline=None,
                     suppress_health_check=[hypothesis.HealthCheck.function_scoped_fixture])
@hypothesis.given(session=spellings.uuids, named=spellings.masks, listed=spellings.masks)
def test_a_cold_sweep_leaves_out_a_conversations_session_in_any_spelling(world, session, named, listed):
    """C-26.3, C-26.13 (review of 3c1a34e, finding 5): whatever case its
    transcript and the daemon's list spell the UUID in, the cold scan never
    returns a conversation's session, and still returns another."""
    home, _ = world
    for path in (home / "projects").rglob("*.jsonl"):
        path.unlink()
    other = spellings.other_uuid(session)
    for session_id in (spellings.spell(session, named), other):
        fx.transcript(home, session_id, fx.interrupted(age_s=1800))
    bound = {spellings.spell(session, listed)}
    cold = transcripts.cold_sessions(live_ids=set(), lane_ids=set(), max_age_s=8 * 3600, now=fx.NOW,
                                     conversation_ids=bound)
    assert [row.session_id for row in cold] == [other]


def test_a_conversation_bound_session_is_never_revived_in_any_spelling(world, policy, tmp_path):
    """C-26.13 with review L1 and M3: a session the daemon lists in another case is
    still its conversation's. Revive refuses it, even opted in and forced, and a
    named cold sweep holds it."""
    cold_session(world, desktop_owned=False)
    daemon = fx.FakeSessions(conversation_sessions=[COLD.upper()])
    result = attempt(daemon, policy, COLD, tmp_path, opt_in=True, force=True)
    assert result.admitted is False and daemon.submits == []
    assert result.reason.startswith("bound to a Subfleet conversation")
    (candidate,) = revive.cold_candidates(daemon, policy, only=[COLD], now=fx.NOW)
    assert candidate.conversation
    assert revive.admits(candidate, policy=policy, opt_in=True, force=True)[0] is False
