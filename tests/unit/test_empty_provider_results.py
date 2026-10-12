"""C-9.2, C-12.6: pin the pre-PR-186 rc-0 deliverable boundaries."""

import json

import pytest

from subfleet.adapters.claude import ClaudeAdapter
from subfleet.adapters.codex import CodexAdapter
from subfleet.contracts import OutcomeClass
from tests.conftest import NOW, exit_info, make_launch
from tests.unit.test_claude_attest import _assistant_row, _write_transcript
from tests.unit.test_codex_adapter import _events, _exit


BLANKS = (None, "", " \t\n ")
CLAUDE_SHAPES = ("missing-result", "missing-text", "null", "empty", "whitespace",
                 "error-empty", "error-whitespace")
CLI_CASES = (
    ("none", "", OutcomeClass.UNKNOWN),
    ("auth", "organization has disabled Claude subscription access; invalid_api_key", OutcomeClass.AUTH_DEAD),
    ("version", "Update Claude Code to use this model; update Codex", OutcomeClass.CLI_TOO_OLD),
    ("quota", "You've hit your usage limit.", OutcomeClass.LIMITED),
    ("credits", "out of usage credits; out of credits", OutcomeClass.LIMITED),
    ("transient", "internal server error; stream disconnected", OutcomeClass.TRANSIENT),
)


def claude_rows(shape):
    if shape == "missing-result":
        return []
    row = {"type": "result", "subtype": "success", "is_error": shape.startswith("error-")}
    if shape != "missing-text":
        row["result"] = None if shape == "null" else " \t\n " if "whitespace" in shape else ""
    return [row]


def claude_classify(directory, rows, stderr="", tail=""):
    (directory / "stream.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows) + tail)
    (directory / "stderr").write_text(stderr)
    projects = directory / "projects"
    launch = make_launch(directory, session_id="blank-result", projects_dir=projects,
                         identity=None, label=None)
    adapter = ClaudeAdapter(now=lambda: NOW, projects_dir=projects)
    outcome = adapter.classify(directory, launch, exit_info(0))
    return adapter, launch, outcome


@pytest.mark.parametrize("shape", CLAUDE_SHAPES)
@pytest.mark.parametrize("name,stderr,expected", CLI_CASES, ids=[case[0] for case in CLI_CASES])
def test_claude_rc_zero_blank_results_keep_provider_evidence(tmp_path, shape, name, stderr, expected):
    # A non-error result without a deliverable is unknown before retryable prose.
    if name == "transient" and shape not in ("missing-result", "error-empty", "error-whitespace"):
        expected = OutcomeClass.UNKNOWN
    adapter, launch, outcome = claude_classify(tmp_path, claude_rows(shape), stderr)
    assert outcome.cls is expected
    assert adapter.deliverable(tmp_path, launch, outcome) is None
    assert (outcome.closure is not None) == (expected is OutcomeClass.LIMITED)
    assert outcome.evidence["rc"] == 0
    if name == "none" and shape not in ("missing-result", "error-empty", "error-whitespace"):
        assert outcome.detail == "unknown: empty deliverable with rc 0"


@pytest.mark.parametrize("shape", CLAUDE_SHAPES)
@pytest.mark.parametrize("provider_event,expected", [
    ({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}}, OutcomeClass.LIMITED),
    ({"type": "assistant", "message": {"model": "claude-opus-5", "content": [], "stop_reason": "refusal"}}, OutcomeClass.CONTENT_FILTER),
])
def test_claude_rc_zero_blank_results_keep_structured_verdicts(tmp_path, shape, provider_event, expected):
    _, _, outcome = claude_classify(tmp_path, [provider_event, *claude_rows(shape)])
    assert outcome.cls is expected
    assert (outcome.closure is not None) == (expected is OutcomeClass.LIMITED)


@pytest.mark.parametrize("shape", CLAUDE_SHAPES)
def test_claude_rc_zero_blank_results_keep_damaged_tail_precedence(tmp_path, shape):
    _, _, outcome = claude_classify(tmp_path, claude_rows(shape), tail='{"type":')
    assert outcome.cls is (OutcomeClass.TRANSIENT if shape == "missing-result" else OutcomeClass.UNKNOWN)
    assert outcome.closure is None


@pytest.mark.parametrize("shape,expected", [
    ("missing-result", OutcomeClass.UNKNOWN), ("missing-text", OutcomeClass.OK),
    ("null", OutcomeClass.OK), ("empty", OutcomeClass.OK),
    ("whitespace", OutcomeClass.UNKNOWN), ("error-empty", OutcomeClass.UNKNOWN),
    ("error-whitespace", OutcomeClass.UNKNOWN),
])
def test_claude_rc_zero_blank_result_uses_existing_assistant_fallback(tmp_path, shape, expected):
    row = {"type": "assistant", "message": {"model": "claude-opus-5", "content": [{"type": "text", "text": "Finished."}]}}
    adapter, launch, outcome = claude_classify(tmp_path, [row, *claude_rows(shape)])
    assert outcome.cls is expected
    assert (adapter.deliverable(tmp_path, launch, outcome) is not None) == ("whitespace" not in shape)
    assert outcome.closure is None


CODEX_AGENT_SHAPES = ("missing-event", "missing-text", "null", "empty", "whitespace")


def codex_rows(agent, completed):
    rows = []
    if agent != "missing-event":
        item = {"type": "agent_message"}
        if agent != "missing-text":
            item["text"] = None if agent == "null" else " \t\n " if agent == "whitespace" else ""
        rows.append({"type": "item.completed", "item": item})
    if completed:
        rows.append({"type": "turn.completed"})
    return rows


@pytest.mark.parametrize("last", BLANKS, ids=("missing-last", "empty-last", "whitespace-last"))
@pytest.mark.parametrize("agent", CODEX_AGENT_SHAPES)
@pytest.mark.parametrize("completed", (False, True), ids=("no-completion", "completed"))
@pytest.mark.parametrize("name,stderr,expected", (*CLI_CASES,
    ("content", "content filter", OutcomeClass.CONTENT_FILTER)), ids=[case[0] for case in CLI_CASES] + ["content"])
def test_codex_rc_zero_blank_results_keep_provider_evidence(tmp_path, last, agent, completed, name, stderr, expected):
    launch = _events(tmp_path, *codex_rows(agent, completed), stderr=stderr)
    if last is not None:
        (tmp_path / "last.md").write_text(last)
    adapter = CodexAdapter(now=lambda: NOW)
    outcome = adapter.classify(tmp_path, launch, _exit(0))
    assert outcome.cls is expected
    assert adapter.deliverable(tmp_path, launch, outcome) is None
    assert (outcome.closure is not None) == (expected is OutcomeClass.LIMITED)
    assert outcome.evidence["rc"] == 0
    if name == "none":
        assert outcome.detail == "Codex exited without a verified deliverable"


@pytest.mark.parametrize("last", BLANKS, ids=("missing-last", "empty-last", "whitespace-last"))
@pytest.mark.parametrize("completed", (False, True))
@pytest.mark.parametrize("message,expected", [
    ("You've hit your usage limit.", OutcomeClass.LIMITED),
    ("unrecognized terminal failure", OutcomeClass.UNKNOWN),
])
def test_codex_rc_zero_blank_results_keep_terminal_verdicts(tmp_path, last, completed, message, expected):
    rows = [*codex_rows("whitespace", completed), {"type": "turn.failed", "error": {"message": message}}]
    launch = _events(tmp_path, *rows)
    if last is not None:
        (tmp_path / "last.md").write_text(last)
    outcome = CodexAdapter(now=lambda: NOW).classify(tmp_path, launch, _exit(0))
    assert outcome.cls is expected
    assert (outcome.closure is not None) == (expected is OutcomeClass.LIMITED)


@pytest.mark.parametrize("last", BLANKS, ids=("missing-last", "empty-last", "whitespace-last"))
@pytest.mark.parametrize("completed", (False, True))
def test_codex_rc_zero_blank_last_file_uses_agent_fallback(tmp_path, last, completed):
    rows = [{"type": "item.completed", "item": {"type": "agent_message", "text": "Finished."}}]
    if completed:
        rows.append({"type": "turn.completed"})
    launch = _events(tmp_path, *rows)
    if last is not None:
        (tmp_path / "last.md").write_text(last)
    outcome = CodexAdapter().classify(tmp_path, launch, _exit(0))
    assert outcome.cls is OutcomeClass.OK
    assert outcome.detail == "Codex completed with a deliverable"
    assert outcome.evidence["admission"] == "deliverable with exit 0"
    assert CodexAdapter().deliverable(tmp_path, launch, outcome) == b"Finished."


@pytest.mark.parametrize("shape", CLAUDE_SHAPES)
@pytest.mark.parametrize("text", (" \t\n ", "Transcript deliverable longer than whitespace."))
def test_claude_rc_zero_blank_result_keeps_transcript_preference(tmp_path, shape, text):
    _write_transcript(tmp_path / "projects", "blank-result", [_assistant_row("blank-result", "claude-opus-5", text)])
    adapter, launch, outcome = claude_classify(tmp_path, claude_rows(shape))
    eligible = shape not in ("missing-result", "error-empty", "error-whitespace") and bool(text.strip())
    assert outcome.cls is (OutcomeClass.OK if eligible else OutcomeClass.UNKNOWN)
    assert (adapter.deliverable(tmp_path, launch, outcome) is not None) == bool(text.strip())
    assert outcome.closure is None


@pytest.mark.parametrize("agent", CODEX_AGENT_SHAPES)
@pytest.mark.parametrize("completed", (False, True))
def test_codex_rc_zero_blank_agent_keeps_nonblank_last_file(tmp_path, agent, completed):
    launch = _events(tmp_path, *codex_rows(agent, completed))
    (tmp_path / "last.md").write_text("Captured deliverable.")
    outcome = CodexAdapter().classify(tmp_path, launch, _exit(0))
    assert outcome.cls is OutcomeClass.OK
    assert outcome.detail == "Codex completed with a deliverable"
    assert outcome.evidence["admission"] == "deliverable with exit 0"


@pytest.mark.parametrize("last", BLANKS, ids=("missing-last", "empty-last", "whitespace-last"))
@pytest.mark.parametrize("final", ("", " \t\n "))
def test_codex_rc_zero_last_blank_agent_cannot_accept_earlier_text(tmp_path, last, final):
    launch = _events(tmp_path,
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Earlier progress."}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": final}},
        {"type": "turn.completed"})
    if last is not None:
        (tmp_path / "last.md").write_text(last)
    outcome = CodexAdapter().classify(tmp_path, launch, _exit(0))
    assert outcome.cls is OutcomeClass.UNKNOWN
    assert CodexAdapter().deliverable(tmp_path, launch, outcome) is None


@pytest.mark.parametrize("text", ("\u00a0", "\u2009"), ids=("nbsp", "thin-space"))
@pytest.mark.parametrize("source", ("last-file", "agent-message"))
def test_codex_keeps_existing_nonempty_utf8_byte_payloads(tmp_path, text, source):
    """C-12.6: Codex's byte check preserves these payloads; it does not normalize text."""
    rows = [{"type": "item.completed", "item": {"type": "agent_message", "text": text}}] if source == "agent-message" else []
    launch = _events(tmp_path, *rows)
    if source == "last-file":
        (tmp_path / "last.md").write_bytes(text.encode())
    outcome = CodexAdapter().classify(tmp_path, launch, _exit(0))
    assert outcome.cls is OutcomeClass.OK
    assert CodexAdapter().deliverable(tmp_path, launch, outcome) == text.encode()
