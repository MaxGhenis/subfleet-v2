"""How a transcript ends, and what is not a session: C-23.31, C-23.33, C-23.34.

Every test names the clause it proves (C-20.5). The fixtures are built in
`tests/sessions_fixtures.py` from the record shapes v1's reader parses; no test
here touches the operator's own `~/.claude`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from subfleet.sessions import registry, transcripts
from tests import sessions_fixtures as fx
from tests.nonblocking import run_child

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


# --- opening a session file (reviews of 39223c9) --------------------------------------


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


@pytest.mark.parametrize("kind", ["fifo", "directory", "device"])
def test_open_regular_refuses_anything_but_a_regular_file_at_once(tmp_path, kind):
    """A FIFO (no writer), a directory or a device is refused with `NotRegularFile`,
    an OSError, without blocking in open() and without leaking its descriptor. A
    FIFO where a rollout belongs had blocked a file op, and with it close(). In a
    child process (`tests.nonblocking`): a regression that blocks is killed and
    reaped, where a helper thread had stayed blocked for the rest of the run."""
    out = run_child(f"""
        import os
        from pathlib import Path
        from subfleet.sessions import transcripts
        kind, path = {kind!r}, Path({str(tmp_path / "entry")!r})
        if kind == "fifo":
            os.mkfifo(path)
        elif kind == "directory":
            path.mkdir()
        else:
            path = Path(os.devnull)
        before = len(os.listdir("/dev/fd"))
        try:
            transcripts.open_regular(path)
        except transcripts.NotRegularFile as exc:
            print("refused", isinstance(exc, OSError), len(os.listdir("/dev/fd")) - before)
        else:
            print("opened")
    """)
    assert out.split() == ["refused", "True", "0"], out


def test_open_regular_reads_a_regular_file_or_a_link_to_one_as_open_would(tmp_path):
    """Binary and text modes as `open`; a symlink to a regular file is followed; the
    descriptor is close-on-exec and blocking again once checked."""
    import fcntl
    path = tmp_path / "t.jsonl"
    path.write_text("one\ntwo\n")
    link = tmp_path / "link.jsonl"
    link.symlink_to(path)
    with transcripts.open_regular(path) as stream:
        assert stream.read() == b"one\ntwo\n"
        flags = fcntl.fcntl(stream.fileno(), fcntl.F_GETFL)
        assert not flags & os.O_NONBLOCK
        assert fcntl.fcntl(stream.fileno(), fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    with transcripts.open_regular(link, "r", encoding="utf-8", errors="replace") as stream:
        assert list(stream) == ["one\n", "two\n"]
    with pytest.raises(FileNotFoundError):
        transcripts.open_regular(tmp_path / "missing.jsonl")


@pytest.mark.parametrize("kwargs, error", [({"encoding": "no-such-encoding"}, LookupError),
                                           ({"newline": "neither"}, ValueError)])
def test_a_stream_open_regular_cannot_build_raises_its_own_error_and_closes_once(tmp_path, kwargs, error):
    """`open` owns the descriptor once `regular_fd` returns it. The helper had
    closed it after `os.fdopen` already had, so an unknown encoding surfaced as
    EBADF, and the second close could close a descriptor another thread had just
    been given (review of aa41312)."""
    path = tmp_path / "t.jsonl"
    path.write_text("one\n")
    before = _open_fds()
    with pytest.raises(error):
        transcripts.open_regular(path, "r", **kwargs)
    assert _open_fds() == before


@pytest.mark.parametrize("mode, kwargs", [("wb", {}), ("r+", {}), ("a", {}), ("rb", {"closefd": False}),
                                          ("rb", {"opener": os.open})])
def test_open_regular_only_reads_through_its_own_descriptor(tmp_path, mode, kwargs):
    """A write mode, `closefd=False` (which left the helper's descriptor open after
    the stream closed) or another opener is refused before anything is opened."""
    path = tmp_path / "t.jsonl"
    path.write_text("one\n")
    before = _open_fds()
    with pytest.raises(ValueError):
        transcripts.open_regular(path, mode, **kwargs)
    assert _open_fds() == before
    assert path.read_text() == "one\n"


#: Every reader of a native session's files, as a call on `fifo`, a FIFO named
#: `session_index.jsonl`, and its answer for an unreadable file (nothing), at once.
#: The last seven had no test of their own (review of aa41312, finding 4): reverting
#: any one of them to a plain open() had failed nothing.
FIFO_READERS = {
    "lines_reversed": ("list(transcripts.lines_reversed(fifo))", []),
    "lines_reversed_with_offsets": ("list(transcripts.lines_reversed_with_offsets(fifo))", []),
    "lines_forward_with_offsets": ("list(transcripts.lines_forward_with_offsets(fifo, 0, 1 << 20))", []),
    "line_start": ("transcripts.line_start(fifo, 10)", 0),
    "headless_transcript": ("transcripts.headless_transcript(str(fifo))", False),
    "registry row": ("registry._row(fifo)", None),
    "history._earlier": ("history._earlier(fifo, 10)", 10),          # unreadable: the cursor stays
    "catalog._codex_names": ("catalog._codex_names(fifo.parent)", {}),
    "catalog._claude_record": ("catalog._claude_record(fifo)", {}),
    "codex_brief._records": ("list(codex_brief._records(fifo))", "refused"),
    "handoff.first_task": ("handoff.first_task(fifo, 1024)", "refused"),
    "handoff._read_bounded": ("handoff._read_bounded(fifo)", ""),
    "last_permission_mode": ("transcripts.last_permission_mode(fifo)", None),
}


@pytest.mark.parametrize("reader", list(FIFO_READERS))
def test_every_transcript_reader_skips_a_fifo_at_once(tmp_path, reader):
    """The readers a file op uses answer as for an unreadable file (nothing, or the
    handoff's own refusal), at once. Each in a child process of its own
    (`tests.nonblocking`), killed and reaped if it blocks: on a helper thread a
    regression had left the thread blocked for the rest of the run."""
    out = run_child(f"""
        import json, os
        from pathlib import Path
        from subfleet.conversations import catalog, codex_brief, history
        from subfleet.sessions import handoff, registry, transcripts
        fifo = Path({str(tmp_path / "session_index.jsonl")!r})
        os.mkfifo(fifo)
        try:
            answer = {FIFO_READERS[reader][0]}
        except handoff.HandoffError:
            answer = "refused"
        print(json.dumps(answer))
    """)
    assert json.loads(out) == FIFO_READERS[reader][1], out     # each reader's own "nothing"


# --- what a reader reads is capped (C-25.3, review of aa41312, finding 2) --------------------

class _Counted:
    """A stream that counts the bytes read through it."""

    def __init__(self, stream, counter):
        self.stream, self.counter = stream, counter

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stream.close()

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def read(self, *args):
        data = self.stream.read(*args)
        self.counter[0] += len(data)
        return data

    def readline(self, *args):
        data = self.stream.readline(*args)
        self.counter[0] += len(data)
        return data


def _counting(monkeypatch) -> list[int]:
    counter = [0]
    real = transcripts.open_regular
    monkeypatch.setattr(transcripts, "open_regular", lambda *a, **kw: _Counted(real(*a, **kw), counter))
    return counter


def test_capped_lines_reads_its_budget_and_skips_a_line_too_long_to_hold(tmp_path):
    """Whole lines within the budget; a line longer than `max_line` is skipped, not
    held; nothing past the budget is read."""
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"one\n" + b"x" * 5000 + b"\ntwo\nthree\n" + b"y" * 10_000 + b"\n")
    with path.open("rb") as stream:
        assert list(transcripts.capped_lines(stream, 10**6, max_line=100)) == [b"one\n", b"two\n", b"three\n"]
    with path.open("rb") as stream:                   # the budget ends inside "two"
        assert list(transcripts.capped_lines(stream, 5_006, max_line=100)) == [b"one\n"]
        assert stream.tell() <= 5_006 + 1                # and one byte to see whether the file ends there
    with path.open("rb") as stream:
        assert list(transcripts.capped_lines(stream, 5_012, max_line=10**6)) == [b"one\n", b"x" * 5000 + b"\n", b"two\n"]


def test_the_forward_reader_skips_a_line_longer_than_it_holds(tmp_path, monkeypatch):
    """A history page reads forward from its cursor for calls' results. It returned a
    line however long past its window (an 8 MiB window returned an 8 MiB + 1 KiB line);
    it now skips a line longer than `max_line`, reading at most the window and one
    line more, and keeps every other line's offset."""
    path = tmp_path / "rollout.jsonl"
    lines = [b"first", b"z" * 50_000, b"after", b"last"]
    path.write_bytes(b"".join(line + b"\n" for line in lines))
    counter = _counting(monkeypatch)
    rows = list(transcripts.lines_forward_with_offsets(path, 0, 10**6, max_line=1_000))
    assert rows == [(0, "first"), (50_007, "after"), (50_013, "last")]
    counter[0] = 0
    assert list(transcripts.lines_forward_with_offsets(path, 6, 2_000, max_line=1_000)) == []
    assert counter[0] <= 2_000 + 1_001


def test_finding_a_lines_start_reads_no_further_back_than_its_budget(tmp_path, monkeypatch):
    """History's cursor recovery scanned back to the file's start for the row the
    cursor falls in: past its budget, as far as the file is long. With `max_bytes` it
    scans no further, and a row that starts further back is cut there."""
    from subfleet.conversations import history
    monkeypatch.setattr(history, "READ_BUDGET", 1_000_000)
    path = tmp_path / "rollout.jsonl"
    with path.open("wb") as stream:
        stream.truncate(3_000_000)                         # one row, no newline
    counter = _counting(monkeypatch)
    assert transcripts.line_start(path, 2_999_999, max_bytes=1_000_000) == 1_999_999
    assert counter[0] <= 1_000_000
    counter[0] = 0
    history._next_cursor(path, 3_000_000, None)
    assert counter[0] <= 1_000_000 + history.BLANK_PROBE


def test_a_first_task_is_looked_for_only_in_the_scan_budget(tmp_path, monkeypatch):
    """Both first-task scanners (a Claude transcript, a Codex rollout) read on past
    `FULL_SCAN_BYTES` (64 MiB) to a user turn however far in; each now looks no
    further, and a transcript with no user turn within it has no task."""
    from subfleet.conversations import codex_brief
    from subfleet.sessions import handoff
    monkeypatch.setattr(handoff, "FULL_SCAN_BYTES", 64 * 1024)
    path = tmp_path / "native.jsonl"
    with path.open("w") as stream:
        for _ in range(100):
            stream.write(json.dumps({"type": "system", "text": "x" * 1000}) + "\n")
        stream.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "late user task"}]}}) + "\n")
        stream.write(json.dumps({"type": "user", "uuid": "first", "message": {"content": "late user task"}}) + "\n")
    for first_task in (handoff.first_task, codex_brief.first_task):
        with pytest.raises(handoff.HandoffError, match="no user task"):
            first_task(path, 256)


def test_a_headless_check_reads_its_byte_budget_at_most(tmp_path, monkeypatch):
    """`headless_transcript` read up to 5000 lines of any length; it also stops at
    `max_bytes`, and decides on what it read, as it does at its line limit."""
    path = tmp_path / "t.jsonl"
    sdk = {"type": "user", "promptSource": "sdk", "message": {"content": "brief"}}
    typed = {"type": "user", "promptSource": "typed", "message": {"content": "hello"}}
    path.write_text(json.dumps(sdk) + "\n" + json.dumps({"type": "system", "text": "x" * 20_000}) + "\n"
                    + json.dumps(typed) + "\n")
    counter = _counting(monkeypatch)
    assert transcripts.headless_transcript(path) is False                    # all read: a typed prompt
    counter[0] = 0
    assert transcripts.headless_transcript(path, max_bytes=10_000) is True   # stopped before it
    assert counter[0] <= 10_000 + 1


def test_a_registry_row_is_read_only_up_to_its_cap(tmp_path, monkeypatch):
    """Each registry file was read whole on every dispatch check; one past
    `ROW_MAX` reads as no row."""
    monkeypatch.setattr(registry, "ROW_MAX", 4_096)
    path = tmp_path / "123.json"
    path.write_text(json.dumps({"sessionId": "s-1", "pid": 123}) + " " * 10_000)
    assert registry._row(path) is None
    path.write_text(json.dumps({"sessionId": "s-1", "pid": 123}))
    assert registry._row(path).session_id == "s-1"


def test_capped_lines_keeps_a_last_line_that_ends_where_the_budget_does():
    """A last line with no newline that ends exactly at the budget is the file's
    last line, not one the budget cut."""
    import io
    assert list(transcripts.capped_lines(io.BytesIO(b"ab\ncd"), 5, max_line=5)) == [b"ab\n", b"cd"]
    assert list(transcripts.capped_lines(io.BytesIO(b"ab\ncde"), 5, max_line=5)) == [b"ab\n"]
