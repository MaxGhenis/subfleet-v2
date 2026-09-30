"""C-9.2, C-9.3, C-9.4: text is evidence only when the provider wrote it, and a
turn that finished and delivered is `ok` whatever any text says.

On 2026-09-30 job 20260929-230209-pb-gpt61sol-judge ran twice to completion on
Claude lanes (rc 0, `is_error: false`, `rate_limit_event` allowed, an 11 KB
deliverable each time) and was classified `limited` both times: the classifier
read the model's narration ("That usually means a usage limit") and its report
("hit its 5-hour session limit") as the lane's own limit, wrote an account closure
on each healthy lane (one with a reset clock parsed from that prose), threw both
deliverables away, and dispatched the job a third time.

Codex had the same hole one step removed: its stderr carries `codex_core::tools`
records that echo the model's blocked commands and failed patches word for word,
and its credential pattern ran before its success check, so a finished turn whose
echoed command mentioned a revoked token would have disabled the lane.

The properties here hold for every input, not only the incident's.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet.adapters import codex as codex_module
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.adapters.codex import CodexAdapter
from subfleet.contracts import (
    ClockSource, Credential, ExitInfo, Launch, OutcomeClass,
)
from tests.conftest import NOW, exit_info, make_launch

QUIET = dict(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
OPUS = "claude-opus-5-5"
SID = "s"

# Every phrase any classifier pattern answers to, from both adapters: limits,
# credits, reset clocks, credentials, organisation blocks, CLI versions,
# content filters and transport failures.
VOCABULARY = (
    "usage limit", "You've hit your limit", "You've hit your usage limit.",
    "You've reached your session limit.", "weekly limit", "5-hour limit",
    "limit reached", "Claude AI usage limit reached|1788624000", "usage_limit_reached",
    "resets 1:30am (America/New_York)", "try again at 11:33 PM (America/New_York)",
    "Try again at 2026-09-05T18:00:00Z.", "quota exceeded", "account quota",
    "out of usage credits", "monthly spend limit", "insufficient credits",
    "HTTP 429", "429 Too Many Requests", "rate limit", "401 Unauthorized",
    "authentication_error", "OAuth token has expired", "not logged in",
    "Your refresh token was revoked.", "the usage endpoint returned 401",
    "organization has disabled Claude subscription access",
    "does not have access to Claude", "organization has been disabled",
    "Update Claude Code to use this model", "does not support this model",
    "Codex CLI is too old", "unknown argument", "content filter",
    "I can't help with that", "API Error: 529 Overloaded", "connection reset",
    "timed out", "internal server error", "stream disconnected before completion",
    "(not your usage limit)",
)

words = st.lists(
    st.one_of(st.sampled_from(VOCABULARY), st.text(max_size=30)), min_size=1, max_size=6,
).map(" ".join)


def _claude_rows(scenario: str, narration: str, answer: str) -> tuple[list[dict], int]:
    """One Claude attempt in `scenario`, with the model saying `narration` along the
    way and, when the turn finishes, delivering `answer`."""
    init = {"type": "system", "subtype": "init", "session_id": SID, "model": OPUS}
    said = {"type": "assistant", "session_id": SID, "message": {
        "model": OPUS, "role": "assistant", "type": "message",
        "content": [{"type": "text", "text": narration}], "stop_reason": "tool_use"}}
    final = {"type": "assistant", "session_id": SID, "message": {
        "model": OPUS, "role": "assistant", "type": "message",
        "content": [{"type": "text", "text": answer}], "stop_reason": "end_turn"}}
    allowed = {"type": "rate_limit_event", "session_id": SID, "rate_limit_info": {
        "status": "allowed", "resetsAt": 1788624000, "rateLimitType": "five_hour",
        "unifiedWindows": {"five_hour": {"utilization": 0.32, "resetsAt": 1788624000}}}}
    rejected = {"type": "rate_limit_event", "session_id": SID, "rate_limit_info": {
        "status": "rejected", "resetsAt": 1789056000, "rateLimitType": "seven_day"}}
    success = {"type": "result", "subtype": "success", "is_error": False,
               "session_id": SID, "result": answer, "stop_reason": "end_turn"}
    if scenario == "finished":
        return [init, said, allowed, final, success], 0
    if scenario == "finished-no-event":
        return [init, said, final, success], 0
    if scenario == "limit-result":
        return [init, said, {"type": "result", "subtype": "success", "is_error": True,
                             "session_id": SID, "result": "You've hit your usage limit."}], 1
    if scenario == "rejected":
        return [init, said, rejected, {"type": "result", "subtype": "success",
                                        "is_error": True, "session_id": SID,
                                        "result": "You've hit your weekly limit."}], 1
    if scenario == "failed-quietly":
        return [init, said, {"type": "result", "subtype": "error_during_execution",
                             "is_error": True, "session_id": SID, "errors": ["boom"]}], 1
    if scenario == "killed":
        return [init, said], 143
    raise AssertionError(scenario)


def _classify_claude(rows: list[dict], rc: int, *, stderr: str = ""):
    with tempfile.TemporaryDirectory() as tmp:
        attempt = Path(tmp) / "a1"
        attempt.mkdir()
        (attempt / "stream.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        (attempt / "stderr").write_text(stderr, encoding="utf-8")
        launch = make_launch(attempt, session_id=SID, model_id=OPUS,
                             projects_dir=Path(tmp) / "projects")
        return ClaudeAdapter(now=lambda: NOW).classify(attempt, launch, exit_info(rc))


def _verdict(outcome) -> tuple:
    return outcome.cls, outcome.closure, outcome.readings


# --- Claude: the incident ----------------------------------------------------


def test_the_incident_shape_is_ok_with_no_closure():
    """C-9.2 the 2026-09-30 attempts: rc 0, `is_error: false`, `rate_limit_event`
    allowed, narration and a deliverable that talk about 429s, a usage limit, a
    session limit and its reset clock. `ok`, no closure, nothing kept as provider
    words (none of the words were the provider's)."""
    rows, rc = _claude_rows(
        "finished",
        "The pool failed fast on 110 cases. That usually means a usage limit or an "
        "auth error. Reading the error envelopes now.",
        "110 calls were refused with HTTP 429 and re-run. That login then hit its "
        "5-hour session limit; it resets 1:30am (America/New_York). You've hit your "
        "usage limit, the CLI would say.",
    )
    outcome = _classify_claude(rows, rc)
    assert outcome.cls is OutcomeClass.OK, outcome.detail
    assert outcome.closure is None
    assert "words_after_delivery" not in outcome.evidence
    assert [r.window for r in outcome.readings] == ["five_hour"]


def test_a_real_limit_is_still_limited():
    """C-9.2, C-9.4 the provider's own limit (an `is_error: true` result, rc 1) still
    closes the account, with its clock read from that result."""
    outcome = _classify_claude(*_claude_rows("limit-result", "Checking the usage limit.", ""))
    assert outcome.cls is OutcomeClass.LIMITED
    assert outcome.closure is not None and outcome.closure.scope == "account"
    assert outcome.closure.source_event == "result-text"


def test_a_reset_clock_is_read_only_from_the_providers_text():
    """C-9.4 a limit's reset clock comes from the provider's words, never from a clock
    the model mentioned earlier in the turn: here the provider gave none, so the
    closure is the guessed hour, not the model's 1:30am."""
    rows, rc = _claude_rows("limit-result", "The desktop login resets 1:30am "
                            "(America/New_York).", "")
    outcome = _classify_claude(rows, rc)
    assert outcome.cls is OutcomeClass.LIMITED
    assert outcome.closure.clock_source is ClockSource.GUESSED


def test_a_provider_limit_frame_after_real_work_is_still_limited():
    """C-9.2 a turn cut short by the provider's synthetic limit frame (the shape
    Claude Code emits mid-run) is `limited` even after real model output."""
    frame = {"type": "assistant", "session_id": SID, "error": "rate_limit",
             "isApiErrorMessage": True, "message": {
                 "model": "<synthetic>", "role": "assistant", "type": "message",
                 "content": [{"type": "text", "text": "You've hit your limit · resets 3pm"}]}}
    rows, _rc = _claude_rows("killed", "Halfway through the judge pool.", "")
    rows.append(frame)
    rows.append({"type": "result", "subtype": "success", "is_error": True,
                 "session_id": SID, "result": "You've hit your limit · resets 3pm"})
    outcome = _classify_claude(rows, 1)
    assert outcome.cls is OutcomeClass.LIMITED
    assert outcome.closure is not None


def test_provider_words_after_a_finished_turn_are_kept_but_not_acted_on():
    """C-9.2 when the provider's own channel mentions a limit (a subagent's limit
    frame, a hook on stderr) but the turn still finished and delivered, the class is
    `ok` with no closure, and the words are kept as `words_after_delivery`."""
    frame = {"type": "assistant", "session_id": SID, "error": "rate_limit",
             "isApiErrorMessage": True, "message": {
                 "model": "<synthetic>", "role": "assistant", "type": "message",
                 "content": [{"type": "text", "text": "You've hit your usage limit."}]}}
    rows, rc = _claude_rows("finished", "Starting.", "Done: all 110 verdicts kept.")
    rows.insert(2, frame)
    outcome = _classify_claude(rows, rc, stderr="MCP server github: 401 unauthorized\n")
    assert outcome.cls is OutcomeClass.OK, outcome.detail
    assert outcome.closure is None
    kept = outcome.evidence["words_after_delivery"]
    assert "usage limit" in kept["limit"]
    assert "401" in kept["auth"]


def _synthetic(kind: str, text: str) -> dict:
    return {"type": "assistant", "session_id": SID, "error": kind, "is_api_error_message": True,
            "message": {"model": "<synthetic>", "role": "assistant", "type": "message",
                        "content": [{"type": "text", "text": text}]}}


@pytest.mark.parametrize("shape,expected", [
    ("stale-result", OutcomeClass.CLI_TOO_OLD),
    ("no-is-error", OutcomeClass.LIMITED),
])
def test_the_providers_error_words_are_never_the_delivery(shape, expected):
    """C-9.2, C-12.6 a turn is delivered only by the model's text. When the last
    text in the stream is the provider's own error (a synthetic frame after the
    empty pre-turn result a stream-json turn writes, or a result with no `is_error`),
    the error is read, not accepted as the deliverable (review of c45acefe)."""
    init = {"type": "system", "subtype": "init", "session_id": SID, "model": OPUS}
    if shape == "stale-result":
        rows = [init, {"type": "result", "subtype": "success", "is_error": False, "result": "",
                       "num_turns": 0, "session_id": SID},
                _synthetic("unknown", "API Error: 400 Claude Code 2.1.284 does not support "
                           "this model; version 2.1.290 or newer is required")]
    else:
        said = "You've hit your session limit · resets 11:30pm (America/New_York)"
        rows = [init, _synthetic("rate_limit", said),
                {"type": "result", "subtype": "success", "session_id": SID, "result": said}]
    outcome = _classify_claude(rows, 0)
    assert outcome.cls is expected, outcome.detail


@pytest.mark.parametrize("change,expected", [
    ("rejected-event", OutcomeClass.LIMITED),
    ("auth-kind", OutcomeClass.AUTH_DEAD),
    ("refusal", OutcomeClass.CONTENT_FILTER),
])
def test_structured_verdicts_still_outrank_a_finished_turn(change, expected):
    """C-9.2, C-9.8 only the provider's structured verdicts outrank a finished turn:
    a rejected `rate_limit_event`, an auth error kind, a refusal stop reason."""
    rows, rc = _claude_rows("finished", "Working.", "Done.")
    if change == "rejected-event":
        rows[2] = {"type": "rate_limit_event", "session_id": SID, "rate_limit_info": {
            "status": "rejected", "resetsAt": 1789056000, "rateLimitType": "seven_day"}}
    elif change == "auth-kind":
        rows[1] = {**rows[1], "error": "authentication_failed"}
    else:
        rows[-1] = {**rows[-1], "stop_reason": "refusal"}
    assert _classify_claude(rows, rc).cls is expected


def test_enrolment_reads_only_the_providers_words(tmp_path):
    """C-9.2, C-10.2 an enrolment turn whose reply happens to mention an
    organisation block enrols; the refusal needs the provider to say it."""
    from tests.conftest import profile_opener
    from tests.unit.test_claude_adapter import _Runner, _enroll_stream

    stream = _enroll_stream().replace(
        '"text": "ok"', '"text": "Your organization has disabled Claude subscription access"')
    adapter = ClaudeAdapter(runner=_Runner(stdout=stream), now=lambda: NOW,
                            projects_dir=tmp_path, profile_opener=profile_opener())
    info = adapter.enroll(Credential(provider="claude", ref="claude-quota-max@axiom.org",
                                     kind="keychain-token"))
    assert info.identity_status == "verified"


# --- Claude: properties --------------------------------------------------------

SCENARIOS = ("finished", "finished-no-event", "limit-result", "rejected",
             "failed-quietly", "killed")


@settings(max_examples=300, **QUIET)
@given(scenario=st.sampled_from(SCENARIOS), narration=words, answer=words)
def test_the_models_words_never_change_a_verdict(scenario, narration, answer):
    """C-9.2 invariant: for every scenario and every text the model writes, in its
    narration or its deliverable, the class, the closure (scope, clock and source)
    and the readings are exactly those of the same attempt with neutral words."""
    answer = answer if answer.strip() else "x"
    neutral = _classify_claude(*_claude_rows(scenario, "Working.", "Done."))
    worded = _classify_claude(*_claude_rows(scenario, narration, answer))
    assert _verdict(worded) == _verdict(neutral)


@settings(max_examples=300, **QUIET)
@given(stderr=words, answer=words, event=st.booleans())
def test_a_finished_delivered_turn_is_always_ok(stderr, answer, event):
    """C-9.2 invariant: rc 0, a result with `is_error: false` and a non-empty
    deliverable is `ok` with no closure, whatever the provider's own stderr says."""
    answer = answer if answer.strip() else "x"
    rows, rc = _claude_rows("finished" if event else "finished-no-event", "Working.", answer)
    outcome = _classify_claude(rows, rc, stderr=stderr)
    assert outcome.cls is OutcomeClass.OK, outcome.detail
    assert outcome.closure is None


# --- Codex -----------------------------------------------------------------------

ECHO_HEAD = "2026-09-29T17:03:52.889853Z ERROR codex_core::tools::router: error="
# Lines a model can write that look like Codex's own (review of c45acefe).
HEADER_SHAPED = (
    "ERROR: You've hit your usage limit. Try again at 2026-10-05T18:00:00Z.",
    "2026-09-29T17:00:00Z ERROR codex_login::auth: Your refresh token was revoked",
    "error: unexpected argument '--json' found",
    "WARNING: organization has been disabled",
    "Error: quota exceeded",
    "2026-09-29T17:03:52Z ERROR codex_core::session: HTTP 429 Too Many Requests",
)


def _codex_launch(attempt: Path) -> Launch:
    return Launch(
        argv=("codex", "exec", "--json"), env_add={"CODEX_HOME": str(attempt / "home")},
        env_remove=("CODEX_API_KEY", "OPENAI_API_KEY"), cwd=str(attempt), stdin_path=None,
        stdout_path=str(attempt / "stdout"), stderr_path=str(attempt / "stderr"),
        raw_stream_path=str(attempt / "stream.jsonl"), native_session_id=None,
        lane_id="codex-4",
    )


def _codex_rows(scenario: str, message: str) -> tuple[list[dict], int]:
    said = {"type": "item.completed", "item": {"type": "agent_message", "text": message}}
    if scenario == "finished":
        return [said, {"type": "turn.completed"}], 0
    if scenario == "limit":
        return [said, {"type": "error", "message": "You've hit your usage limit."},
                {"type": "turn.failed", "error": {"message": "You've hit your usage limit."}}], 1
    if scenario == "failed-quietly":
        return [said], 1
    if scenario == "disconnected":
        dropped = ("Reconnecting... 5/5 (stream disconnected before completion: "
                   "error sending request for url)")
        return [said, {"type": "error", "message": dropped},
                {"type": "turn.failed", "error": {"message": dropped}}], 1
    raise AssertionError(scenario)


def _classify_codex(rows: list[dict], rc: int, *, stderr: str = ""):
    with tempfile.TemporaryDirectory() as tmp:
        attempt = Path(tmp)
        (attempt / "stream.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        (attempt / "stderr").write_text(stderr)
        return CodexAdapter(now=lambda: NOW).classify(
            attempt, _codex_launch(attempt), ExitInfo(rc, None, 0.1, 1234, None))


@pytest.mark.parametrize("echoed", [
    "Command blocked by PreToolUse hook: grep 'Your refresh token was revoked' logs/",
    "apply_patch verification failed: the usage endpoint returned 401 in tests",
    "Command blocked: echo 'organization has been disabled'",
])
def test_a_finished_codex_turn_is_ok_despite_echoed_credential_words(echoed):
    """C-9.2, C-9.3 a finished Codex turn whose tool router echoed the model's own
    command, credential words and all, is `ok` and keeps its lane; before, the
    credential pattern ran first and would have disabled it."""
    outcome = _classify_codex(*_codex_rows("finished", "Done."), stderr=f"{ECHO_HEAD}{echoed}\n")
    assert outcome.cls is OutcomeClass.OK
    assert "words_after_delivery" not in outcome.evidence


def test_a_finished_codex_turn_outranks_a_recovered_credential_error():
    """C-9.2 a non-terminal `error` event with credential words, then a completed
    turn and a deliverable, is `ok`: a lane whose token was revoked cannot finish.
    The words are kept as evidence."""
    rows, rc = _codex_rows("finished", "Done.")
    rows.insert(0, {"type": "error", "message": "Your refresh token was revoked."})
    outcome = _classify_codex(rows, rc)
    assert outcome.cls is OutcomeClass.OK
    assert "refresh token" in outcome.evidence["words_after_delivery"]["auth"]


def test_a_codex_deliverable_quoting_limits_is_ok():
    """C-9.2, C-12.6 a Codex deliverable that quotes "usage limit" and "HTTP 429" is
    `ok`, not retried."""
    outcome = _classify_codex(*_codex_rows(
        "finished", "110 calls got HTTP 429; the login hit its usage limit and quota exceeded."))
    assert outcome.cls is OutcomeClass.OK
    assert outcome.closure is None


def test_an_echoed_limit_in_a_failed_codex_turn_closes_nothing():
    """C-9.2, C-9.4 a failed Codex turn whose only limit words are the tool router's
    echo of a model command (a multi-line heredoc) is not `limited` and writes no
    closure."""
    stderr = (f"{ECHO_HEAD}Command blocked by PreToolUse hook: cat <<'EOF'\n"
              "You've hit your usage limit. Try again at 2026-09-05T18:00:00Z.\n"
              "HTTP 429 Too Many Requests\nEOF\n")
    outcome = _classify_codex(*_codex_rows("failed-quietly", "Trying."), stderr=stderr)
    assert outcome.cls is OutcomeClass.UNKNOWN
    assert outcome.closure is None


@pytest.mark.parametrize("quoted", HEADER_SHAPED)
def test_a_header_shaped_line_inside_an_echo_stays_the_models(quoted):
    """C-9.2 an echo is the model's text verbatim, so a line in it may look like one
    of Codex's own: a plain `ERROR:`, a clap `error:`, a quoted older log record.
    None of them ends the echo. A turn that failed on a dropped stream stays
    `transient`, with no closure, whatever the heredoc it tried to write said
    (found by review of c45acefe: before, each of these read as the lane's own
    limit, dead credential or old CLI)."""
    stderr = (f"Reading prompt from stdin...\n{ECHO_HEAD}Command blocked by PreToolUse "
              f"hook: cat > notes.txt <<'EOF'\n{quoted}\nand the rest of the note\nEOF\n")
    plain = _classify_codex(*_codex_rows("disconnected", "Writing the note."))
    echoed = _classify_codex(*_codex_rows("disconnected", "Writing the note."), stderr=stderr)
    assert plain.cls is OutcomeClass.TRANSIENT
    assert (echoed.cls, echoed.closure) == (plain.cls, plain.closure)


def test_a_real_codex_limit_is_still_limited():
    """C-9.2, C-9.4 the provider's own limit still closes the lane: an `error` and a
    `turn.failed` (rc 1), a plain line Codex wrote before its first tool call, and a
    tracing record Codex wrote after an echo ended."""
    outcome = _classify_codex(*_codex_rows("limit", "Working."))
    assert outcome.cls is OutcomeClass.LIMITED and outcome.closure is not None
    before_tools = _classify_codex(
        *_codex_rows("failed-quietly", "Working."),
        stderr=f"ERROR: You've hit your usage limit.\n{ECHO_HEAD}Command blocked\n")
    assert before_tools.cls is OutcomeClass.LIMITED and before_tools.closure is not None
    after_echo = _classify_codex(
        *_codex_rows("failed-quietly", "Working."),
        stderr=(f"{ECHO_HEAD}Command blocked\n2026-09-29T17:04:10.000001Z ERROR "
                "codex_api::endpoint::responses: You've hit your usage limit.\n"))
    assert after_echo.cls is OutcomeClass.LIMITED and after_echo.closure is not None


# --- Codex: properties ---------------------------------------------------------

TARGETS = ("codex_core::tools::router", "codex_core::tools::handlers::shell",
           "codex_api::endpoint::responses_websocket", "codex_models_manager::manager",
           "codex_core::session", "rmcp::transport::worker")
body_line = st.one_of(st.sampled_from(VOCABULARY), st.sampled_from(HEADER_SHAPED), st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc", "Zl", "Zp")), max_size=40))


def _starts_a_later_record(line: str, head: str) -> bool:
    """Whether `line` is a tracing record timestamped no earlier than `head`'s: the
    one shape an echo cannot hold, because Codex's next record is exactly that. The
    generators below leave out only this."""
    found, own = codex_module._TRACE_HEAD_RE.match(line), codex_module._TRACE_HEAD_RE.match(head)
    return bool(found) and codex_module._trace_time(found) >= codex_module._trace_time(own)


@st.composite
def stderr_records(draw):
    """Codex stderr as tracing records, each with its continuation lines. An echo's
    lines may look like anything the model wrote, headers included; Codex's own
    records carry no header-shaped continuation lines."""
    records = []
    for index in range(draw(st.integers(0, 5))):
        target = draw(st.sampled_from(TARGETS))
        head = f"2026-09-29T17:0{index}:52.889853Z ERROR {target}: {draw(body_line)}"
        lines = [head]
        for extra in draw(st.lists(body_line, max_size=3)):
            if not extra.strip() or _starts_a_later_record(extra, head):
                continue
            if target.startswith("codex_core::tools") or not codex_module._TRACE_HEAD_RE.match(extra):
                lines.append(extra)
        records.append((target, lines))
    return records


@settings(max_examples=300, **QUIET)
@given(records=stderr_records())
def test_stderr_keeps_exactly_the_non_echo_records(records):
    """C-9.2 invariant: of any Codex stderr, exactly the lines of the records that
    are not `codex_core::tools` echoes are read, in order."""
    stderr = "\n".join(line for _target, lines in records for line in lines)
    want = [line for target, lines in records
            if not target.startswith("codex_core::tools") for line in lines if line.strip()]
    assert codex_module._stderr_lines(stderr) == want


@settings(max_examples=300, **QUIET)
@given(scenario=st.sampled_from(("finished", "limit", "failed-quietly", "disconnected")),
       echo=st.lists(body_line, min_size=1, max_size=4), message=words)
def test_a_codex_echo_never_changes_a_verdict(scenario, echo, message):
    """C-9.2 invariant: for every scenario, the tool router's echo of anything the
    model sent (header-shaped lines included) and anything the model says leave
    class and closure unchanged."""
    lines = [line for line in echo[1:]
             if line.strip() and not _starts_a_later_record(line, ECHO_HEAD)]
    stderr = "\n".join([ECHO_HEAD + echo[0], *lines]) + "\n"
    neutral = _classify_codex(*_codex_rows(scenario, "Working."))
    worded = _classify_codex(*_codex_rows(scenario, message if message.strip() else "x"),
                             stderr=stderr)
    assert (worded.cls, worded.closure) == (neutral.cls, neutral.closure)
