"""How a transcript ends, and what is not a session: C-23.31, C-23.33, C-23.34.

Every test names the clause it proves (C-20.5). The fixtures are built in
`tests/sessions_fixtures.py` from the record shapes v1's reader parses; no test
here touches the operator's own `~/.claude`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from subfleet.sessions import transcripts
from tests import sessions_fixtures as fx

SESSION = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    return fx.claude_home(tmp_path, monkeypatch)


# --- the four states (C-23.33) ------------------------------------------------

def test_an_unanswered_tool_call_is_interrupted(home):
    """C-23.33: a tool call whose result never arrived is a cut-off turn."""
    path = fx.transcript(home, SESSION, fx.interrupted(age_s=1800))
    state = transcripts.turn_state(path, now=fx.NOW)
    assert state.state == "interrupted"
    assert "never got its result" in state.detail
    assert state.age_s == 1800


def test_a_tool_result_the_model_never_continued_is_interrupted(home):
    """C-23.33: the model owed a continuation and never produced it."""
    path = fx.transcript(home, SESSION, [
        fx.typed_prompt("go", uuid="p", at=fx.ago(700)),
        fx.assistant_tool_use(uuid="a", at=fx.ago(650)),
        fx.user_tool_result(uuid="r", at=fx.ago(600))])
    state = transcripts.turn_state(path, now=fx.NOW)
    assert state.state == "interrupted"
    assert "never continued" in state.detail


def test_an_unanswered_human_prompt_is_interrupted(home):
    """C-23.33: a user message with real text and no assistant reply."""
    path = fx.transcript(home, SESSION, [
        fx.typed_prompt("first", uuid="p0", at=fx.ago(900)),
        fx.assistant_text("done", uuid="a0", at=fx.ago(800)),
        fx.typed_prompt("now do the other thing", uuid="p1", at=fx.ago(600))])
    state = transcripts.turn_state(path, now=fx.NOW)
    assert state.state == "interrupted"
    assert "an unanswered prompt" in state.detail


def test_assistant_text_is_a_completed_turn_and_is_never_nudged(home):
    """C-23.33: a session that finished its work is idle by choice."""
    path = fx.transcript(home, SESSION, fx.completed())
    assert transcripts.turn_state(path, now=fx.NOW).state == "completed"


def test_an_escaped_turn_is_stopped_not_interrupted(home):
    """C-23.33: the user interrupted it, so resuming would override a person."""
    path = fx.transcript(home, SESSION, fx.stopped())
    state = transcripts.turn_state(path, now=fx.NOW)
    assert state.state == "stopped"


def test_a_previous_nudge_is_tickled_not_an_unanswered_prompt(home):
    """C-23.33: subfleet's own message must not read as a human's next request."""
    path = fx.transcript(home, SESSION, [
        fx.typed_prompt("go", uuid="p", at=fx.ago(900)),
        fx.assistant_tool_use(uuid="a", at=fx.ago(800)),
        fx.user_text(transcripts.MARKER + " — continue", uuid="n", at=fx.ago(600))])
    assert transcripts.turn_state(path, now=fx.NOW).state == "tickled"


def test_an_absent_or_empty_transcript_is_empty(home, tmp_path):
    """C-23.33: nothing to judge is `empty`, never `interrupted`."""
    assert transcripts.turn_state(None).state == "empty"
    assert transcripts.turn_state(tmp_path / "nope.jsonl").state == "empty"
    blank = fx.transcript(home, SESSION, [])
    assert transcripts.turn_state(blank, now=fx.NOW).state == "empty"


# --- the app's synthetic resume stub (C-23.34) --------------------------------

def test_synthetic_resume_stub_judged_by_underlying_turn(home):
    """C-23.34: the app's resume stub makes an interrupted transcript look
    completed, so the classifier skips it and judges the turn underneath.

    Ledger row 184; observed four times in one session on 2026-08-23.
    """
    without = fx.transcript(home, SESSION, fx.interrupted(age_s=1800))
    bare = transcripts.turn_state(without, now=fx.NOW)
    with_stub = fx.transcript(home, SESSION, fx.interrupted(age_s=1800, stub=True))
    behind = transcripts.turn_state(with_stub, now=fx.NOW)
    assert bare.state == behind.state == "interrupted"
    assert behind.restart_stubs == 1
    assert "behind the app's resume stub" in behind.detail
    # The real turn is still the tool call, not the stub.
    assert behind.last_uuid == bare.last_uuid == "cut"


def test_each_restart_stub_earns_its_own_nudge_through_the_dedupe_key(home):
    """C-23.33: one nudge per interruption point AND per restart of it.

    The stub carries a fresh uuid every restart, so the dedupe key moves while
    the underlying stuck turn does not.
    """
    first = transcripts.turn_state(
        fx.transcript(home, SESSION, fx.interrupted(stub=True)), now=fx.NOW)
    entries = fx.interrupted() + fx.resume_stub(user_uuid="s2-u",
                                                assistant_uuid="s2-a")
    second = transcripts.turn_state(fx.transcript(home, SESSION, entries), now=fx.NOW)
    assert first.last_uuid == second.last_uuid
    assert first.dedupe_key != second.dedupe_key
    assert second.dedupe_key == "s2-a"


def test_without_a_stub_the_dedupe_key_is_the_stuck_turn(home):
    """C-23.33: no stub, so the interruption point is the turn's own uuid."""
    state = transcripts.turn_state(
        fx.transcript(home, SESSION, fx.interrupted()), now=fx.NOW)
    assert state.dedupe_key == state.last_uuid == "cut"


def test_a_usage_limit_banner_is_not_a_model_turn(home):
    """C-23.33: a limit banner under the model's last text is a cut-off turn.

    The banner is the rejected request, so it sits BELOW the narration it
    interrupted. Err toward resuming: a finished session answers a nudge with
    one cheap "nothing pending", while a missed resume strands real work
    (Max's 2026-08-23 sign-in).
    """
    path = fx.transcript(home, SESSION, [
        fx.typed_prompt("go", uuid="p", at=fx.ago(900)),
        fx.assistant_text("still working on it", uuid="a", at=fx.ago(700)),
        fx.limit_banner(at=fx.ago(600))])
    state = transcripts.turn_state(path, now=fx.NOW)
    assert state.state == "interrupted"
    assert state.limit_banner is True
    assert "cut off by a usage limit" in state.detail
    assert state.last_uuid == "a", "the banner is not the turn; the text is"


def test_a_finished_turn_stays_completed_when_no_banner_follows_it(home):
    """C-23.33: the banner is what makes the difference, not the text."""
    path = fx.transcript(home, SESSION, [
        fx.typed_prompt("go", uuid="p", at=fx.ago(900)),
        fx.assistant_text("done", uuid="a", at=fx.ago(600))])
    state = transcripts.turn_state(path, now=fx.NOW)
    assert (state.state, state.limit_banner) == ("completed", False)


def test_sidechain_and_meta_entries_never_decide_the_last_turn(home):
    """C-23.33: subagent and bookkeeping rows change the file, not the turn."""
    entries = fx.interrupted()
    noise = fx.assistant_text("subagent chatter", uuid="sub", at=fx.ago(1))
    noise["isSidechain"] = True
    meta = fx.user_text("queue-operation", uuid="meta", at=fx.ago(1))
    meta["isMeta"] = True
    path = fx.transcript(home, SESSION, entries + [noise, meta])
    state = transcripts.turn_state(path, now=fx.NOW)
    assert state.state == "interrupted"
    assert state.last_uuid == "cut"


# --- the re-check value (C-23.34) ---------------------------------------------

def test_the_fingerprint_ignores_the_stub_and_moves_on_a_real_turn(home):
    """C-23.34: the re-check must not fire on the app's own bookkeeping."""
    path = fx.transcript(home, SESSION, fx.interrupted())
    before = transcripts.fingerprint(path)
    fx.transcript(home, SESSION, fx.interrupted(stub=True))
    assert transcripts.fingerprint(path) == before
    fx.transcript(home, SESSION, fx.interrupted() + [
        fx.assistant_text("continuing", uuid="new", at=fx.ago(1))])
    assert transcripts.fingerprint(path) != before


# --- a headless lane run is not a session (C-23.31) ---------------------------

def test_a_headless_lane_transcript_is_recognised(home):
    """C-23.31: one sdk-sourced prompt is a `claude -p` run, not a session."""
    lane = fx.transcript(home, "lane-1", fx.headless())
    assert transcripts.headless_transcript(lane) is True


def test_a_typed_prompt_is_never_a_lane_run(home):
    """C-23.31: a human-typed prompt is an interactive session."""
    path = fx.transcript(home, SESSION, fx.interrupted())
    assert transcripts.headless_transcript(path) is False


def test_a_notified_lane_stays_a_lane_but_a_third_sdk_prompt_does_not(home):
    """C-23.31: inbox notices arrive as `sdk`, so up to two are still a lane.

    v1 measured this on 2026-09-04: the ceremony session had 93 sdk prompts.
    """
    two = fx.transcript(home, "lane-2", [
        fx.headless_prompt("brief", uuid="h1", at=fx.ago(900)),
        fx.headless_prompt("a notice", uuid="h2", at=fx.ago(800)),
        fx.assistant_text("ok", uuid="a", at=fx.ago(700))])
    assert transcripts.headless_transcript(two) is True
    three = fx.transcript(home, "lane-3", [
        fx.headless_prompt("brief", uuid="h1", at=fx.ago(900)),
        fx.headless_prompt("a notice", uuid="h2", at=fx.ago(800)),
        fx.headless_prompt("another notice", uuid="h3", at=fx.ago(700)),
        fx.assistant_text("ok", uuid="a", at=fx.ago(600))])
    assert transcripts.headless_transcript(three) is False


def test_tool_results_are_not_counted_as_prompts(home):
    """C-23.31: tool results are user entries too and carry no promptSource."""
    path = fx.transcript(home, "lane-4", [
        fx.headless_prompt("brief", uuid="h1", at=fx.ago(900)),
        fx.assistant_tool_use(uuid="a1", at=fx.ago(800)),
        fx.user_tool_result(uuid="r1", at=fx.ago(700)),
        fx.user_tool_result(uuid="r2", at=fx.ago(600)),
        fx.user_tool_result(uuid="r3", at=fx.ago(500))])
    assert transcripts.headless_transcript(path) is True


# --- the writer's entrypoint decides (C-23.31, 2026-10-03) --------------------

@pytest.mark.parametrize("entrypoint", ["claude-desktop", "cli"])
def test_a_session_whose_prompts_arrive_as_sdk_is_still_a_session(home, entrypoint):
    """C-23.31: the desktop app sends its prompts as `promptSource: sdk`, so a
    desktop session started by one or two messages has a lane's prompt shape.
    The process that wrote it says otherwise (19 desktop sessions holding
    pending notices read as lane runs on 2026-10-03)."""
    one = fx.transcript(home, SESSION, fx.stamped(fx.headless(), entrypoint))
    assert transcripts.headless_transcript(one) is False
    two = fx.transcript(home, "two-prompts", fx.stamped([
        fx.headless_prompt("the task", uuid="h1", at=fx.ago(900)),
        fx.assistant_text("working", uuid="a1", at=fx.ago(800)),
        fx.headless_prompt("one more thing", uuid="h2", at=fx.ago(700)),
        fx.assistant_tool_use(uuid="a2", at=fx.ago(600))], entrypoint))
    assert transcripts.headless_transcript(two) is False


@pytest.mark.parametrize("entrypoint", sorted(transcripts.HEADLESS_ENTRYPOINTS))
def test_a_headless_entrypoint_is_a_lane_run_however_many_prompts(home, entrypoint):
    """C-23.31: a `claude -p` or Agent SDK process is a lane run whatever its
    prompts: a resumed or notified lane takes more than two, and a v1 probe's
    prompt carries no `promptSource` at all."""
    notified = fx.transcript(home, "lane-many", fx.stamped([
        fx.headless_prompt(f"prompt {n}", uuid=f"h{n}", at=fx.ago(900 - n))
        for n in range(5)] + [fx.assistant_text("ok", uuid="a", at=fx.ago(100))], entrypoint))
    assert transcripts.headless_transcript(notified) is True
    probe = fx.transcript(home, "probe", fx.stamped([
        fx.user_text("Reply with exactly: ok", uuid="p", at=fx.ago(60)),
        fx.assistant_text("ok", uuid="a", at=fx.ago(59))], entrypoint))
    assert transcripts.headless_transcript(probe) is True


def test_one_entry_from_a_person_driven_process_makes_a_session(home):
    """C-23.31: a desktop session a headless process later continued (a revive,
    a Subfleet turn) and a lane run the desktop app later resumed both have a
    person's process in them; neither is a lane run."""
    continued = fx.transcript(home, SESSION, [
        *fx.stamped([fx.headless_prompt("start", uuid="h1", at=fx.ago(900))], "claude-desktop"),
        *fx.stamped([fx.headless_prompt("continue", uuid="h2", at=fx.ago(800)),
                     fx.assistant_tool_use(uuid="a", at=fx.ago(700))], "sdk-cli")])
    assert transcripts.headless_transcript(continued) is False
    resumed = fx.transcript(home, "resumed-lane", [
        *fx.stamped(fx.headless(), "sdk-cli"),
        *fx.stamped([fx.assistant_text("seen", uuid="late", at=fx.ago(10))], "claude-desktop")])
    assert transcripts.headless_transcript(resumed) is False


def test_an_entrypoint_outside_the_headless_set_is_a_session(home):
    """C-23.31: only `sdk-cli`, `sdk-ts` and `sdk-py` are headless. Another of
    Claude Code's entrypoints, or one it adds later, is never read as a lane:
    hiding a person's session is the failure this clause cannot afford."""
    path = fx.transcript(home, SESSION, fx.stamped(fx.headless(), "claude-vscode"))
    assert transcripts.headless_transcript(path) is False


@pytest.mark.parametrize("value", ["", None, 7, ["sdk-cli"]])
def test_an_entrypoint_that_names_nothing_falls_back_to_the_prompt_rule(home, value):
    """C-23.31: an empty or non-string `entrypoint` names no process, so the
    transcript is read as older Claude Code's: by its prompts."""
    lane = fx.transcript(home, "lane-1", [{**entry, "entrypoint": value} for entry in fx.headless()])
    assert transcripts.headless_transcript(lane) is True
    typed = fx.transcript(home, SESSION, [{**entry, "entrypoint": value} for entry in fx.interrupted()])
    assert transcripts.headless_transcript(typed) is False


def test_a_legacy_prompt_still_speaks_beside_headless_entries(home):
    """C-23.31: prompts that name no entrypoint keep v1's reading even when a
    later headless process continued the transcript: a typed prompt from older
    Claude Code made it a session."""
    path = fx.transcript(home, SESSION, [
        fx.typed_prompt("go", uuid="t", at=fx.ago(900)),
        *fx.stamped([fx.headless_prompt("continue", uuid="h", at=fx.ago(800))], "sdk-cli")])
    assert transcripts.headless_transcript(path) is False
    lane = fx.transcript(home, "lane-1", [
        fx.headless_prompt("brief", uuid="h0", at=fx.ago(900)),
        *fx.stamped([fx.headless_prompt("resumed", uuid="h1", at=fx.ago(800))], "sdk-cli")])
    assert transcripts.headless_transcript(lane) is True


def test_a_line_that_is_not_an_object_is_skipped(home):
    """C-23.31: a valid JSON line that is not an object is no entry, and a
    message that is not an object has no content; reading either never raises
    (both raised AttributeError before 2026-10-03)."""
    path = fx.transcript(home, "lane-1", fx.headless())
    odd = ["[1]", '"text"', json.dumps({"type": "user", "message": "x", "promptSource": "sdk"})]
    path.write_text("\n".join(odd) + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    assert transcripts.headless_transcript(path) is True       # two sdk prompts, no entrypoint
    stamped = fx.transcript(home, "lane-2", fx.stamped(fx.headless(), "sdk-cli"))
    stamped.write_text("\n".join(odd) + "\n" + stamped.read_text(encoding="utf-8"), encoding="utf-8")
    assert transcripts.headless_transcript(stamped) is True


# --- what revive reads off a transcript (C-23.35, C-23.39) --------------------

def test_the_permission_mode_is_read_from_the_last_stamped_turn(home):
    """C-23.35: revive admits only `bypassPermissions`, so the mode must be read."""
    path = fx.transcript(home, SESSION, fx.with_mode(fx.interrupted(), "bypassPermissions"))
    assert transcripts.last_permission_mode(path) == "bypassPermissions"
    other = fx.transcript(home, "b", fx.with_mode(fx.interrupted(), "acceptEdits"))
    assert transcripts.last_permission_mode(other) == "acceptEdits"
    assert transcripts.last_permission_mode(None) is None


def test_the_last_real_assistant_model_skips_the_apps_synthetic_entries(home):
    """C-23.39: revive keeps the session's own tier, so it must find the tier.

    Limit banners and resume stubs carry the model `<synthetic>`.
    """
    path = fx.transcript(home, SESSION, [
        fx.typed_prompt("go", uuid="p", at=fx.ago(900)),
        fx.assistant_text("working", uuid="a", at=fx.ago(800), model="claude-opus-5"),
        fx.limit_banner(at=fx.ago(700)),
        *fx.resume_stub()])
    assert transcripts.last_assistant_model(path) == "claude-opus-5"


def test_the_cwd_comes_from_the_last_main_chain_entry(home):
    """C-23.35: a revive with no cwd has nowhere to run."""
    path = fx.transcript(home, SESSION, fx.interrupted())
    assert transcripts.last_cwd(path) == fx.WORKDIR


# --- locating a transcript ----------------------------------------------------

def test_the_newest_project_copy_wins_when_a_session_moved_worktrees(home):
    """C-23.30's sibling: one session id, two project directories, one answer."""
    old = fx.transcript(home, SESSION, fx.completed(), cwd="/Users/fixture/old")
    new = fx.transcript(home, SESSION, fx.interrupted(), cwd=fx.WORKDIR)
    import os
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_800_000_000, 1_800_000_000))
    assert transcripts.transcript_path(SESSION) == new


def test_an_unreadable_line_never_stops_the_classifier(home):
    """C-23.33: a truncated write is a normal thing to find at a file's tail."""
    path = fx.transcript(home, SESSION, fx.interrupted())
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"type": "assistant", "uuid": "trunc"\n')
    assert transcripts.turn_state(path, now=fx.NOW).state == "interrupted"


# --- cold sessions (the `--scope cold` candidate scan) ------------------------

def test_cold_sessions_finds_interrupted_transcripts_with_no_live_process(home):
    """C-23.31 and C-23.35: a cold sweep sees neither lanes nor live sessions."""
    fx.transcript(home, SESSION, fx.interrupted(age_s=600))
    fx.transcript(home, "live-one", fx.interrupted(age_s=600))
    fx.transcript(home, "lane-one", fx.headless(age_s=600))
    fx.transcript(home, "finished", fx.completed(age_s=600))
    rows = transcripts.cold_sessions(live_ids={"live-one"}, lane_ids=set(),
                                     max_age_s=7200, now=fx.NOW)
    assert [row.session_id for row in rows] == [SESSION]


def test_cold_sessions_respects_the_age_window(home):
    """C-23.33's cap, applied to the cold scan: a stale transcript is not work."""
    fx.transcript(home, SESSION, fx.interrupted(age_s=9 * 3600))
    rows = transcripts.cold_sessions(live_ids=set(), lane_ids=set(),
                                     max_age_s=2 * 3600, now=fx.NOW)
    assert rows == []


def test_a_recorded_lane_id_excludes_a_transcript_the_shape_test_would_miss(home):
    """C-23.31: the recorded marker is authoritative, the shape is the fallback."""
    fx.transcript(home, "lane-two", fx.interrupted(age_s=600))
    rows = transcripts.cold_sessions(live_ids=set(), lane_ids={"lane-two"},
                                     max_age_s=7200, now=fx.NOW)
    assert rows == []
