"""C-12.5 and C-12.6: served-model attestation and the deliverable's transcript range.

Attestation must never be a false positive. Every case here that cannot see the whole
truth — no transcript, two transcripts, an unreadable one, a session id that does not
match, a turn with no model field — must come back `unattested`, and the evidence must
say which of those it was.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from subfleet.adapters.claude import ClaudeAdapter, encode_project_dir
from subfleet.contracts import Attestation, Outcome, OutcomeClass
from tests.conftest import NOW, exit_info, make_launch, stage_case

WORKDIR = "/Users/maxghenis/subfleet-v2"
FABLE = "claude-fable-5-1"
OPUS = "claude-opus-5"


def _adapter(projects: Path) -> ClaudeAdapter:
    return ClaudeAdapter(now=lambda: NOW, projects_dir=projects)


def _assistant_row(session_id: str, model: str | None, text: str, index: int = 0) -> dict:
    message: dict = {
        "id": f"msg_{index}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
    }
    if model is not None:
        message["model"] = model
    return {
        "type": "assistant",
        "cwd": WORKDIR,
        "sessionId": session_id,
        "uuid": f"u{index}",
        "timestamp": "2026-09-05T11:30:00.000Z",
        "message": message,
    }


def _write_transcript(projects: Path, session_id: str, rows: list[dict], *,
                      workdir: str = WORKDIR) -> Path:
    target = projects / encode_project_dir(workdir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{session_id}.jsonl"
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _stream(session_id: str, model: str, text: str) -> str:
    rows = [
        {"type": "system", "subtype": "init", "session_id": session_id, "model": model},
        {"type": "assistant", "session_id": session_id,
         "message": {"model": model, "content": [{"type": "text", "text": text}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": text,
         "session_id": session_id},
    ]
    return "".join(json.dumps(r) + "\n" for r in rows)


def _attempt(tmp_path: Path, session_id: str, model: str, text: str) -> Path:
    attempt = tmp_path / "a1"
    attempt.mkdir(parents=True, exist_ok=True)
    (attempt / "stream.jsonl").write_text(_stream(session_id, model, text),
                                          encoding="utf-8")
    (attempt / "stderr").write_text("", encoding="utf-8")
    return attempt


def _outcome(session_id: str) -> Outcome:
    return Outcome(cls=OutcomeClass.OK, detail="ok", native_session_id=session_id)


# --- the three verdicts ------------------------------------------------------


def test_every_assistant_turn_on_the_requested_model_is_attested(tmp_path):
    """C-12.5 all assistant messages inside the attempt's range name the requested
    model: `attested`, with the served model recorded."""
    projects = tmp_path / "projects"
    sid = "aaaa1111-2222-4333-8444-555566667777"
    _write_transcript(projects, sid, [
        _assistant_row(sid, OPUS, "one", 0),
        _assistant_row(sid, OPUS, "two", 1),
    ])
    attempt = _attempt(tmp_path, sid, OPUS, "two")
    launch = make_launch(attempt, session_id=sid, model_id=OPUS, projects_dir=projects)
    result = _adapter(projects).attest(attempt, launch, _outcome(sid), OPUS)
    assert result.status is Attestation.ATTESTED
    assert result.served_model == OPUS
    assert "2 assistant turn" in result.evidence


def test_a_different_served_model_is_a_mismatch_naming_it(tmp_path):
    """C-12.5, C-21 milestone 2: the model-downgrade case yields `mismatch` naming the
    model that actually served the turn."""
    projects = tmp_path / "projects"
    attempt, rc = stage_case("model-downgrade", tmp_path / "a1")
    sid = "99999999-9999-4999-8999-999999999999"
    source = Path(__file__).resolve().parent.parent / "fixtures" / "claude"
    target = projects / encode_project_dir(WORKDIR)
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{sid}.jsonl").write_text(
        (source / "model-downgrade" / "transcript.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    launch = make_launch(attempt, session_id=sid, model_id=FABLE, projects_dir=projects)
    adapter = _adapter(projects)
    outcome = adapter.classify(attempt, launch, exit_info(rc))
    result = adapter.attest(attempt, launch, outcome, FABLE)
    assert result.status is Attestation.MISMATCH
    assert result.served_model == OPUS
    assert FABLE in result.evidence and OPUS in result.evidence


def test_a_transcript_that_cannot_be_found_is_unattested(tmp_path):
    """C-12.5 no transcript for the session id: `unattested`, never `attested`."""
    projects = tmp_path / "projects"
    projects.mkdir()
    sid = "bbbb1111-2222-4333-8444-555566667777"
    attempt = _attempt(tmp_path, sid, OPUS, "text")
    launch = make_launch(attempt, session_id=sid, model_id=OPUS, projects_dir=projects)
    result = _adapter(projects).attest(attempt, launch, _outcome(sid), OPUS)
    assert result.status is Attestation.UNATTESTED
    assert result.served_model is None
    assert "no transcript found" in result.evidence
    assert sid in result.evidence


def test_two_candidate_transcripts_are_unattested_with_the_ambiguity_recorded(tmp_path):
    """C-12.5 exactly one transcript match is required; two is ambiguity, and the
    evidence names both files."""
    projects = tmp_path / "projects"
    sid = "cccc1111-2222-4333-8444-555566667777"
    first = _write_transcript(projects, sid, [_assistant_row(sid, OPUS, "one")])
    second = _write_transcript(
        projects, sid, [_assistant_row(sid, OPUS, "one")],
        workdir="/Users/maxghenis/subfleet-v2-lanes/claude-adapter",
    )
    attempt = _attempt(tmp_path, sid, OPUS, "one")
    launch = make_launch(attempt, session_id=sid, model_id=OPUS, projects_dir=projects)
    result = _adapter(projects).attest(attempt, launch, _outcome(sid), OPUS)
    assert result.status is Attestation.UNATTESTED
    assert result.served_model is None
    assert "ambiguous" in result.evidence
    assert str(first) in result.evidence and str(second) in result.evidence


def test_a_transcript_with_no_assistant_turn_is_unattested(tmp_path):
    """C-12.5 a transcript with no assistant message proves nothing about the model."""
    projects = tmp_path / "projects"
    sid = "dddd1111-2222-4333-8444-555566667777"
    _write_transcript(projects, sid, [{
        "type": "user", "sessionId": sid, "cwd": WORKDIR,
        "message": {"role": "user", "content": "[prompt redacted]"},
    }])
    attempt = _attempt(tmp_path, sid, OPUS, "text")
    launch = make_launch(attempt, session_id=sid, model_id=OPUS, projects_dir=projects)
    result = _adapter(projects).attest(attempt, launch, _outcome(sid), OPUS)
    assert result.status is Attestation.UNATTESTED
    assert "no assistant turn" in result.evidence


def test_an_assistant_turn_with_no_model_field_is_unattested(tmp_path):
    """C-12.5 a message with no `model` cannot attest anything: never a false positive."""
    projects = tmp_path / "projects"
    sid = "eeee1111-2222-4333-8444-555566667777"
    _write_transcript(projects, sid, [_assistant_row(sid, None, "text")])
    attempt = _attempt(tmp_path, sid, OPUS, "text")
    launch = make_launch(attempt, session_id=sid, model_id=OPUS, projects_dir=projects)
    result = _adapter(projects).attest(attempt, launch, _outcome(sid), OPUS)
    assert result.status is Attestation.UNATTESTED


def test_an_unreadable_transcript_is_unattested(tmp_path):
    """C-12.5 a transcript that exists and cannot be read is `unattested`, and the
    evidence says so rather than pretending the model was right."""
    projects = tmp_path / "projects"
    sid = "ffff1111-2222-4333-8444-555566667777"
    path = _write_transcript(projects, sid, [_assistant_row(sid, OPUS, "text")])
    os.chmod(path, 0o000)
    try:
        attempt = _attempt(tmp_path, sid, OPUS, "text")
        launch = make_launch(attempt, session_id=sid, model_id=OPUS,
                             projects_dir=projects)
        result = _adapter(projects).attest(attempt, launch, _outcome(sid), OPUS)
    finally:
        os.chmod(path, 0o600)
    assert result.status is Attestation.UNATTESTED
    assert "unreadable" in result.evidence


def test_no_session_id_is_unattested(tmp_path):
    """C-12.5 without a session id there is nothing to look up."""
    projects = tmp_path / "projects"
    projects.mkdir()
    attempt = _attempt(tmp_path, "s", OPUS, "text")
    launch = make_launch(attempt, session_id="s", model_id=OPUS, projects_dir=projects)
    launch = replace(launch, native_session_id=None)
    result = _adapter(projects).attest(
        attempt, launch, Outcome(cls=OutcomeClass.OK, detail="ok"), OPUS
    )
    assert result.status is Attestation.UNATTESTED
    assert "no session id" in result.evidence


def test_a_short_alias_request_attests_against_the_full_served_id(tmp_path):
    """C-12.5 v1's alias rule: `opus` requested, `claude-opus-5` served, attested."""
    projects = tmp_path / "projects"
    sid = "1111aaaa-2222-4333-8444-555566667777"
    _write_transcript(projects, sid, [_assistant_row(sid, OPUS, "text")])
    attempt = _attempt(tmp_path, sid, OPUS, "text")
    launch = make_launch(attempt, session_id=sid, model_id="opus", projects_dir=projects)
    result = _adapter(projects).attest(attempt, launch, _outcome(sid), "opus")
    assert result.status is Attestation.ATTESTED


def test_one_downgraded_turn_among_many_is_a_mismatch(tmp_path):
    """C-12.5 a single swapped turn is a mismatch: `attested` means *every* turn."""
    projects = tmp_path / "projects"
    sid = "2222aaaa-2222-4333-8444-555566667777"
    _write_transcript(projects, sid, [
        _assistant_row(sid, FABLE, "one", 0),
        _assistant_row(sid, OPUS, "two", 1),
        _assistant_row(sid, FABLE, "three", 2),
    ])
    attempt = _attempt(tmp_path, sid, FABLE, "three")
    launch = make_launch(attempt, session_id=sid, model_id=FABLE, projects_dir=projects)
    result = _adapter(projects).attest(attempt, launch, _outcome(sid), FABLE)
    assert result.status is Attestation.MISMATCH
    assert result.served_model == OPUS


# --- the attempt's own range (C-12.5, C-12.6) --------------------------------


def test_only_turns_after_the_recorded_offset_are_this_attempts(tmp_path):
    """C-12.5 a resumed session appends to one transcript; the recorded offset is what
    makes an attempt's own range knowable. A predecessor's downgraded turn must not
    make this attempt a mismatch."""
    projects = tmp_path / "projects"
    sid = "3333aaaa-2222-4333-8444-555566667777"
    path = _write_transcript(projects, sid, [_assistant_row(sid, OPUS, "earlier", 0)])
    offset = path.stat().st_size
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_assistant_row(sid, FABLE, "later", 1)) + "\n")

    attempt = _attempt(tmp_path, sid, FABLE, "later")
    launch = make_launch(attempt, session_id=sid, model_id=FABLE, projects_dir=projects,
                         transcript_offset=offset)
    adapter = _adapter(projects)
    assert adapter.attest(attempt, launch, _outcome(sid), FABLE).status is (
        Attestation.ATTESTED
    )
    # Without the offset the earlier Opus turn is in range and the verdict flips.
    whole = make_launch(attempt, session_id=sid, model_id=FABLE, projects_dir=projects)
    assert adapter.attest(attempt, whole, _outcome(sid), FABLE).status is (
        Attestation.MISMATCH
    )


def test_an_offset_that_lands_mid_line_discards_the_partial_row(tmp_path):
    """C-12.5 a byte offset inside a line must never be parsed as a whole one."""
    projects = tmp_path / "projects"
    sid = "4444aaaa-2222-4333-8444-555566667777"
    path = _write_transcript(projects, sid, [
        _assistant_row(sid, OPUS, "earlier", 0),
        _assistant_row(sid, FABLE, "later", 1),
    ])
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    mid = len(lines[0].encode("utf-8")) - 20        # inside the first row
    attempt = _attempt(tmp_path, sid, FABLE, "later")
    launch = make_launch(attempt, session_id=sid, model_id=FABLE, projects_dir=projects,
                         transcript_offset=mid)
    result = _adapter(projects).attest(attempt, launch, _outcome(sid), FABLE)
    assert result.status is Attestation.ATTESTED    # only the whole second row was read


def test_rows_of_another_session_in_the_same_file_are_ignored(tmp_path):
    """C-12.5 v1 required the transcript row's `sessionId` to match; so does this."""
    projects = tmp_path / "projects"
    sid = "5555aaaa-2222-4333-8444-555566667777"
    _write_transcript(projects, sid, [
        _assistant_row("someone-elses-session", FABLE, "not ours", 0),
        _assistant_row(sid, OPUS, "ours", 1),
    ])
    attempt = _attempt(tmp_path, sid, OPUS, "ours")
    launch = make_launch(attempt, session_id=sid, model_id=OPUS, projects_dir=projects)
    result = _adapter(projects).attest(attempt, launch, _outcome(sid), OPUS)
    assert result.status is Attestation.ATTESTED


# --- the deliverable's range (C-12.6) ---------------------------------------


def test_the_transcript_wins_when_the_envelope_rendering_is_shorter(tmp_path):
    """C-12.6 v1's `prefer_transcript_text` rule: the envelope's `result` is a
    rendering and can silently lose interior characters (64,613 B lost 1,912 on
    2026-09-04). When the transcript's final text is longer and differs, it wins."""
    projects = tmp_path / "projects"
    sid = "6666aaaa-2222-4333-8444-555566667777"
    full = "A" * 400 + "B" * 400
    truncated = "A" * 400
    _write_transcript(projects, sid, [_assistant_row(sid, OPUS, full)])
    attempt = _attempt(tmp_path, sid, OPUS, truncated)
    launch = make_launch(attempt, session_id=sid, model_id=OPUS, projects_dir=projects)
    adapter = _adapter(projects)
    outcome = adapter.classify(attempt, launch, exit_info(0))
    assert adapter.deliverable(attempt, launch, outcome) == full.encode("utf-8")


def test_the_envelope_wins_when_it_is_the_longer_text(tmp_path):
    """C-12.6 the rule is one-way: a shorter transcript never overrides the envelope."""
    projects = tmp_path / "projects"
    sid = "7777aaaa-2222-4333-8444-555566667777"
    _write_transcript(projects, sid, [_assistant_row(sid, OPUS, "short")])
    attempt = _attempt(tmp_path, sid, OPUS, "a much longer envelope rendering")
    launch = make_launch(attempt, session_id=sid, model_id=OPUS, projects_dir=projects)
    adapter = _adapter(projects)
    outcome = adapter.classify(attempt, launch, exit_info(0))
    assert adapter.deliverable(attempt, launch, outcome) == (
        b"a much longer envelope rendering"
    )


def test_the_deliverable_ignores_a_predecessors_turns(tmp_path):
    """C-12.6 an attempt's deliverable comes from its own transcript range; a resumed
    session's earlier, longer answer is not this attempt's output."""
    projects = tmp_path / "projects"
    sid = "8888aaaa-2222-4333-8444-555566667777"
    path = _write_transcript(projects, sid, [_assistant_row(sid, OPUS, "X" * 5000, 0)])
    offset = path.stat().st_size
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_assistant_row(sid, OPUS, "the new answer", 1)) + "\n")

    attempt = _attempt(tmp_path, sid, OPUS, "the new answer")
    launch = make_launch(attempt, session_id=sid, model_id=OPUS, projects_dir=projects,
                         transcript_offset=offset)
    adapter = _adapter(projects)
    outcome = adapter.classify(attempt, launch, exit_info(0))
    assert adapter.deliverable(attempt, launch, outcome) == b"the new answer"


def test_no_transcript_falls_back_to_the_streams_result_text(tmp_path):
    """C-12.6 with no transcript at all, the stream's `result` text is the deliverable."""
    projects = tmp_path / "projects"
    projects.mkdir()
    sid = "9999aaaa-2222-4333-8444-555566667777"
    attempt = _attempt(tmp_path, sid, OPUS, "from the stream")
    launch = make_launch(attempt, session_id=sid, model_id=OPUS, projects_dir=projects)
    adapter = _adapter(projects)
    outcome = adapter.classify(attempt, launch, exit_info(0))
    assert adapter.deliverable(attempt, launch, outcome) == b"from the stream"


def test_an_empty_deliverable_is_none(tmp_path):
    """C-12.6 whitespace is not a deliverable."""
    projects = tmp_path / "projects"
    projects.mkdir()
    sid = "0000aaaa-2222-4333-8444-555566667777"
    attempt = _attempt(tmp_path, sid, OPUS, "   \n  ")
    launch = make_launch(attempt, session_id=sid, model_id=OPUS, projects_dir=projects)
    adapter = _adapter(projects)
    outcome = adapter.classify(attempt, launch, exit_info(0))
    assert adapter.deliverable(attempt, launch, outcome) is None
    assert outcome.cls is OutcomeClass.UNKNOWN


def test_classify_records_the_transcript_it_resolved(tmp_path):
    """C-12.5 the outcome names the transcript, so the daemon records it as an artifact
    without repeating the search."""
    projects = tmp_path / "projects"
    sid = "abcd1111-2222-4333-8444-555566667777"
    path = _write_transcript(projects, sid, [_assistant_row(sid, OPUS, "text")])
    attempt = _attempt(tmp_path, sid, OPUS, "text")
    launch = make_launch(attempt, session_id=sid, model_id=OPUS, projects_dir=projects)
    outcome = _adapter(projects).classify(attempt, launch, exit_info(0))
    assert outcome.transcript_path == str(path)
    assert outcome.served_model == OPUS
