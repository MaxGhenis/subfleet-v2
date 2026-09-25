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


def test_the_offset_readers_agree_with_a_plain_split_of_the_bytes(tmp_path):
    """Property check of `lines_reversed_with_offsets` (with and without `end`, any
    chunk, a byte budget), `lines_forward_with_offsets` and `line_start` against
    the bytes split on newlines: CRLF, UTF-8 across chunk edges, blank lines, no
    trailing newline."""
    import random
    from subfleet.sessions import transcripts
    rng = random.Random(14)
    alphabet = ["a", "b", "é", "中", " ", "\r", "\n", "\n", "{", "}"]
    for trial in range(400):
        data = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 120))).encode()
        path = tmp_path / f"t{trial}.txt"
        path.write_bytes(data)
        starts, position = [], 0
        for part in data.split(b"\n"):
            starts.append((position, part))
            position += len(part) + 1
        end = rng.choice([None, rng.randint(0, len(data))])
        top = len(data) if end is None else end
        # Every line that starts before `end`, cut at `end`, non-blank, newest first.
        want = [(at, part[:max(0, top - at)].decode("utf-8", "replace")) for at, part in reversed(starts)
                if at < top and part[:max(0, top - at)].strip()]
        chunk = rng.randint(1, 9)
        got = list(transcripts.lines_reversed_with_offsets(path, chunk=chunk, end=end))
        assert got == want, (trial, data, end, chunk)
        budget = rng.randint(1, 60)
        bounded = list(transcripts.lines_reversed_with_offsets(path, chunk=chunk, end=end, max_bytes=budget))
        assert bounded == want[:len(bounded)], (trial, "a budget yields a prefix of whole lines")
        start = rng.choice([at for at, _ in starts])
        window = rng.randint(0, 80)
        forward = list(transcripts.lines_forward_with_offsets(path, start, window))
        assert forward == [(at, part.rstrip(b"\r").decode("utf-8", "replace")) for at, part in starts
                           if start <= at < start + window and part.strip()], trial
        offset = rng.randint(0, max(0, len(data) - 1))
        assert transcripts.line_start(path, offset, chunk=chunk) == data.rfind(b"\n", 0, offset) + 1, trial
