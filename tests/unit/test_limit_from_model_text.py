"""C-9.2–9.8: provider verdicts never come from a model's report."""

import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings, strategies as st

from subfleet.adapters import claude as claude_module, codex as codex_module
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.adapters.claude_stream import cli_placeholder, parse_stream
from subfleet.adapters.codex import CodexAdapter
from subfleet.contracts import ClockSource, OutcomeClass
from tests.conftest import FIXTURES, NOW, case_names, exit_info, make_launch
from tests.unit.test_codex_adapter import _events as codex_events, _exit as codex_exit


MODEL = "claude-opus-5-5"
SESSION = "limit-from-model-text"
LIVE_REPORTS = (
    "The first US-hub triage session hit its usage limit.",
    "The second US-hub triage session hit its usage limit.",
    'App inventory: "Continues elsewhere when an account hits its usage limit".',
)


def assistant(text):
    return {"type": "assistant", "session_id": SESSION,
            "message": {"model": MODEL, "content": [{"type": "text", "text": text}]}}


def success(text):
    return {"type": "result", "subtype": "success", "is_error": False,
            "session_id": SESSION, "result": text}


def classify(directory, rows, rc=0, stderr=""):
    return classify_stream(directory, "".join(json.dumps(row) + "\n" for row in rows), rc, stderr)


def classify_stream(directory, stream, rc=0, stderr=""):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stream.jsonl").write_text(stream)
    (directory / "stderr").write_text(stderr)
    projects = directory / "projects"
    adapter = ClaudeAdapter(now=lambda: NOW, projects_dir=projects)
    launch = make_launch(directory, session_id=SESSION, model_id=MODEL,
                         projects_dir=projects, identity=None, label=None)
    return adapter.classify(directory, launch, exit_info(rc))


@pytest.mark.parametrize("report", LIVE_REPORTS, ids=("triage-first", "triage-second", "inventory"))
def test_live_success_report_never_limits(tmp_path, report):
    """2026-10-11: reconstructed reports from the three incident job shapes."""
    outcome = classify(tmp_path, [assistant(report), success(report)])
    assert outcome.cls is OutcomeClass.OK, outcome.detail
    assert outcome.closure is None


# Generate from every text-classifier pattern, including clock signatures, plus
# arbitrary Unicode. Regex generation covers each pattern's alternatives and
# case variants instead of maintaining a hand-picked subset of limit vocabulary.
PATTERNS = tuple(value for module in (claude_module, codex_module)
                 for name, value in vars(module).items()
                 if name in {"CLI_TOO_OLD_RE", "ORG_BLOCK_RE", "AUTH_SIGNATURE_RE", "CREDITS_RE",
                             "LIMIT_RE", "TRANSIENT_RE", "AUTH_RE", "OLD_CLI_RE", "CONTENT_RE",
                             "_EPOCH_LIMIT_RE", "_RESET_CLOCK_RE"})
MODEL_TEXT = st.one_of(st.text(max_size=120), st.sampled_from(LIVE_REPORTS),
                      *(st.from_regex(pattern, fullmatch=True) for pattern in PATTERNS))
INVARIANTS = settings(max_examples=150, derandomize=True, deadline=None, database=None)


def fixture_rows(case):
    return [json.loads(line) for line in (FIXTURES / case / "stdout").read_text().splitlines()
            if line.strip().startswith("{") and line.strip().endswith("}")]


FIXTURE_ROWS = [row for case in case_names() for row in fixture_rows(case)]
STREAMS = st.lists(st.one_of(st.sampled_from(FIXTURE_ROWS), st.builds(assistant, MODEL_TEXT),
                            st.builds(success, MODEL_TEXT)), max_size=10)
LIMIT_CASES = tuple(case for case in case_names()
                    if json.loads((FIXTURES / case / "expected.json").read_text())["class"] == "limited")


def replace_model_text(rows, text):
    rows = deepcopy(rows)
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("type") == "assistant" and not cli_placeholder(row):
            content = row["message"].get("content")
            if isinstance(content, str):
                row["message"]["content"] = text
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        block["text"] = text
        if row.get("type") == "result" and row.get("is_error") is not True:
            row["result"] = text
    return rows


def erase_model_text(stream):
    """Preserve invalid rows/tails, including the captured disconnect fixture."""
    try:
        whole = json.loads(stream)
    except ValueError:
        whole = None
    if isinstance(whole, dict):
        return json.dumps(replace_model_text([whole], "")[0])
    out = []
    for line in stream.splitlines(keepends=True):
        try:
            row = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        out.append(json.dumps(replace_model_text([row], "")[0]) + ("\n" if line.endswith("\n") else ""))
    return "".join(out)


def verdict(outcome):
    return outcome.cls, outcome.closure


@INVARIANTS
@given(rows=STREAMS, text=MODEL_TEXT, rc=st.sampled_from((0, 1, 5, -15)))
def test_model_text_mutation_and_insertion_preserve_class_and_closure(rows, text, rc):
    """C-9.2–9.5: change model text or add model frames to any generated stream."""
    with TemporaryDirectory() as tmp:
        directory = Path(tmp)
        before = classify(directory, rows, rc)
        mutated = [assistant(text), *replace_model_text(rows, text), assistant(text)]
        after = classify(directory, mutated, rc)
        assert verdict(after) == verdict(before)
        if before.closure is None:
            assert after.closure is None


@INVARIANTS
@given(case=st.sampled_from(LIMIT_CASES), before=MODEL_TEXT, after=MODEL_TEXT)
def test_fixture_limit_keeps_its_scope_and_clock_with_surrounding_model_text(case, before, after):
    """C-9.4, C-9.8: replay existing limit evidence, including real provider verdicts."""
    with TemporaryDirectory() as tmp:
        directory = Path(tmp)
        rows = fixture_rows(case)
        stderr = (FIXTURES / case / "stderr").read_text()
        rc = int((FIXTURES / case / "rc").read_text())
        original = classify(directory, rows, rc, stderr)
        surrounded = classify(directory, [assistant(before), *rows, assistant(after)], rc, stderr)
        assert original.cls is OutcomeClass.LIMITED
        assert verdict(surrounded) == verdict(original)


@INVARIANTS
@given(rows=STREAMS, rc=st.sampled_from((0, 1, 5, -15)), stderr=MODEL_TEXT,
       tail=st.sampled_from(("", "not json\n", '{"type":')))
def test_classifier_equals_stream_with_all_model_text_erased(rows, rc, stderr, tail):
    """Differential: all assistant/success prose can be erased without changing the verdict."""
    with TemporaryDirectory() as tmp:
        directory = Path(tmp)
        stream = "".join(json.dumps(row) + "\n" for row in rows) + tail
        assert verdict(classify_stream(directory, stream, rc, stderr)) == verdict(
            classify_stream(directory, erase_model_text(stream), rc, stderr))


@pytest.mark.parametrize("case", case_names())
def test_fixture_differential_with_model_text_erased(tmp_path, case):
    stream = (FIXTURES / case / "stdout").read_text()
    rc = int((FIXTURES / case / "rc").read_text())
    stderr = (FIXTURES / case / "stderr").read_text()
    assert verdict(classify_stream(tmp_path, stream, rc, stderr)) == verdict(
        classify_stream(tmp_path, erase_model_text(stream), rc, stderr))


@pytest.mark.parametrize("text,until", [
    ("Claude AI usage limit reached|1788624000", "2026-09-05T16:00:00Z"),
    ("You've hit your limit · resets 6:40pm (America/New_York)", "2026-09-05T22:40:00Z"),
])
def test_synthetic_cli_limit_placeholder_supplies_reported_clock(tmp_path, text, until):
    # Existing <synthetic> placeholder shape; epoch wording is already covered
    # in test_claude_adapter, and the reset wording is specified by this incident.
    frame = assistant(text)
    frame["message"]["model"] = "<synthetic>"
    assert cli_placeholder(frame)
    assert parse_stream(json.dumps(frame)).cli_texts() == (text,)
    outcome = classify(tmp_path, [frame], rc=1)
    assert outcome.cls is OutcomeClass.LIMITED
    assert outcome.closure.scope == "account"
    assert outcome.closure.clock_source is ClockSource.REPORTED
    assert outcome.closure.until_at == until


@pytest.mark.parametrize("case", ("limit-session-with-clock", "limit-no-clock"))
@pytest.mark.parametrize("rc", (0, 1))
def test_error_result_fixture_text_is_cli_evidence(tmp_path, case, rc):
    rows = fixture_rows(case)
    result = next(row for row in rows if row["type"] == "result")
    assert result["is_error"] is True
    assert result["result"] in parse_stream(json.dumps(result)).cli_texts()
    assert classify(tmp_path, [result], rc=rc).cls is OutcomeClass.LIMITED


@pytest.mark.parametrize("text", ("usage limit", "session limit", "weekly limit", "limit reached",
    "out of usage credits", "monthly spend limit", "organization has disabled", "401",
    "Update Claude Code to use this model", "internal server error"))
@pytest.mark.parametrize("source", ("assistant", "success-result", "failed-model-only"))
def test_classifier_phrases_in_each_model_source_never_classify(tmp_path, text, source):
    rows = [assistant(text), success("Finished")] if source == "assistant" else [success(text)]
    if source == "failed-model-only":
        rows = [assistant(text)]
    outcome = classify(tmp_path, rows, rc=1 if source == "failed-model-only" else 0)
    assert outcome.cls is (OutcomeClass.UNKNOWN if source == "failed-model-only" else OutcomeClass.OK)
    assert outcome.closure is None


@pytest.mark.parametrize("text", ("usage limit", "out of usage credits", "401",
                                  "organization has disabled Claude subscription access"))
@pytest.mark.parametrize("source", ("stderr", "placeholder", "errors", "error-kind"))
def test_success_supersedes_cli_text_failures(tmp_path, text, source):
    rows, stderr = [], ""
    if source == "stderr":
        stderr = text
    elif source == "errors":
        rows.append({"type": "result", "is_error": True, "errors": [text]})
    else:
        frame = assistant(text)
        frame["message"]["model"] = "<synthetic>"
        if source == "error-kind":
            frame["error"] = "authentication_failed"
        rows.append(frame)
    rows.append(success("Finished."))
    outcome = classify(tmp_path, rows, stderr=stderr)
    assert outcome.cls is OutcomeClass.OK
    assert outcome.closure is None


def test_structured_rejection_overrides_success(tmp_path):
    rows = fixture_rows("rejected-credits-fable") + [success("Finished.")]
    outcome = classify(tmp_path, rows)
    assert outcome.cls is OutcomeClass.LIMITED
    assert outcome.closure.clock_source is ClockSource.REPORTED
    assert outcome.closure.source_event == "rate_limit_event"


def test_model_clock_cannot_supply_cli_limit_reset(tmp_path):
    error = {"type": "result", "is_error": True, "result": "You've hit your usage limit."}
    outcome = classify(tmp_path, [assistant("Claude AI usage limit reached|1788624000"), error], rc=1)
    assert outcome.cls is OutcomeClass.LIMITED
    assert outcome.closure.clock_source is ClockSource.GUESSED


@INVARIANTS
@given(text=MODEL_TEXT, rc=st.sampled_from((0, 1, 5)), terminal=st.booleans())
def test_codex_agent_text_and_erasure_preserve_verdict(text, rc, terminal):
    """Codex model messages never reach quota, credits, auth or other failure patterns."""
    with TemporaryDirectory() as tmp:
        directory = Path(tmp)
        adapter = CodexAdapter(now=lambda: NOW)
        outcomes = []
        for value in ("", text):
            rows = [{"type": "item.completed", "item": {"type": "agent_message", "text": value}}]
            if terminal:
                rows.append({"type": "turn.failed", "error": {"message": "usage limit reached"}})
            launch = codex_events(directory, *rows)
            outcomes.append(adapter.classify(directory, launch, codex_exit(rc)))
        assert verdict(outcomes[0]) == verdict(outcomes[1])


@pytest.mark.parametrize("text", ("usage limit reached", "out of credits", "invalid_api_key"))
@pytest.mark.parametrize("source", ("stderr", "error"))
def test_codex_completed_success_supersedes_cli_failure_text(tmp_path, text, source):
    rows = [{"type": "error", "message": text}] if source == "error" else []
    rows += [{"type": "item.completed", "item": {"type": "agent_message", "text": "Finished"}},
             {"type": "turn.completed"}]
    launch = codex_events(tmp_path, *rows, stderr=text if source == "stderr" else "")
    outcome = CodexAdapter(now=lambda: NOW).classify(tmp_path, launch, codex_exit(0))
    assert outcome.cls is OutcomeClass.OK
    assert outcome.closure is None


@pytest.mark.parametrize("text", ("usage limit reached", "out of credits", "invalid_api_key"))
def test_codex_last_answer_is_not_failure_evidence(tmp_path, text):
    launch = codex_events(tmp_path)
    (tmp_path / "last.md").write_text(text)
    for rc in (0, 1):
        outcome = CodexAdapter(now=lambda: NOW).classify(tmp_path, launch, codex_exit(rc))
        assert outcome.cls is (OutcomeClass.OK if rc == 0 else OutcomeClass.UNKNOWN)
        assert outcome.closure is None
