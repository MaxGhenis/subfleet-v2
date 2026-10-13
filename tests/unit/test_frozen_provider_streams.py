"""C-4.3: a frozen provider stream stays authoritative at every size and type."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile

from hypothesis import example, given, settings, strategies as st
import pytest

from subfleet.adapters.claude import ClaudeAdapter
from subfleet.adapters.codex import CodexAdapter, completion_evidence
from subfleet.contracts import OutcomeClass
from tests.conftest import make_launch
from tests.frozen_streams import FROZEN_KINDS, make_frozen
from tests.unit.test_codex_adapter import _exit, _launch
from tests.unit.test_codex_turn_completed import COMPLETED, MESSAGE, EVENT


CLAUDE_RESULT = {"type": "result", "is_error": False, "result": "Finished."}


def encode(events):
    return "".join(json.dumps(event) + "\n" for event in events).encode()


def provider(directory, name):
    (directory / "stderr").write_bytes(b"")
    if name == "codex":
        return CodexAdapter(), _launch(directory)
    return ClaudeAdapter(projects_dir=directory / "projects"), make_launch(
        directory, session_id="missing-session", model_id="haiku", projects_dir=directory / "projects")


def readings(adapter, directory, launch):
    outcome = adapter.classify(directory, launch, _exit(0))
    result = (outcome.cls, adapter.deliverable(directory, launch, outcome))
    if isinstance(adapter, CodexAdapter):
        return (*result, completion_evidence(directory, launch))
    return (*result, adapter.raw_stream_text(directory, launch), adapter.stream_summary(directory, launch))


@pytest.mark.parametrize("name", ("codex", "claude"))
@pytest.mark.parametrize("kind", ("directory", "fifo", "symlink", "dangling-symlink"))
@pytest.mark.parametrize("raw", ("stream.jsonl", "custom.jsonl", "missing.jsonl", None))
def test_nonregular_frozen_stream_never_reads_stdout(tmp_path, name, kind, raw):
    adapter, launch = provider(tmp_path, name)
    launch = replace(launch, raw_stream_path=str(tmp_path / raw) if raw else None)
    (tmp_path / "stdout").write_bytes(encode([MESSAGE, COMPLETED, CLAUDE_RESULT]))
    make_frozen(tmp_path / ("custom.jsonl" if raw == "custom.jsonl" else "stream.jsonl"), kind)
    outcome = adapter.classify(tmp_path, launch, _exit(0))
    assert outcome.cls == (OutcomeClass.TRANSIENT if name == "codex" else OutcomeClass.UNKNOWN)
    assert adapter.deliverable(tmp_path, launch, outcome) is None
    if name == "codex":
        assert completion_evidence(tmp_path, launch)["turn_completed"] is False
    else:
        assert adapter.raw_stream_text(tmp_path, launch) == ""
        assert adapter.raw_stream_text(tmp_path) == ("" if raw != "custom.jsonl" else (tmp_path / "stdout").read_text())
        assert adapter.stream_summary(tmp_path, launch).lines_total == 0


@pytest.mark.parametrize("raw", ("stream.jsonl", "custom.jsonl", "missing.jsonl", None))
@pytest.mark.parametrize("contents", (b"", b" \n\t", b"garbage\n"))
def test_claude_empty_frozen_stream_is_authoritative(tmp_path, raw, contents):
    adapter, launch = provider(tmp_path, "claude")
    launch = replace(launch, raw_stream_path=str(tmp_path / raw) if raw else None)
    (tmp_path / "stdout").write_bytes(encode([CLAUDE_RESULT]))
    (tmp_path / ("custom.jsonl" if raw == "custom.jsonl" else "stream.jsonl")).write_bytes(contents)
    assert adapter.raw_stream_text(tmp_path, launch) == contents.decode()
    assert adapter.stream_summary(tmp_path, launch).result is None
    outcome = adapter.classify(tmp_path, launch, _exit(0))
    assert outcome.cls == OutcomeClass.UNKNOWN
    assert adapter.deliverable(tmp_path, launch, outcome) is None


STREAM = st.lists(st.one_of(EVENT, st.sampled_from([
    CLAUDE_RESULT, {"type": "result", "is_error": False, "result": ""},
    {"type": "result", "is_error": True, "result": "stream disconnected"},
    {"type": "system", "subtype": "init"},
])), max_size=10).map(encode)


@pytest.mark.parametrize("name", ("codex", "claude"))
@settings(max_examples=120, deadline=None, derandomize=True)
@example(kind="empty", frozen=b"", before=b"", after=encode([MESSAGE, COMPLETED, CLAUDE_RESULT]))
@example(kind="fifo", frozen=b"", before=b"", after=encode([MESSAGE, COMPLETED, CLAUDE_RESULT]))
@given(kind=st.sampled_from(FROZEN_KINDS), frozen=STREAM, before=STREAM, after=STREAM)
def test_frozen_authority_survives_stdout_rewrite_and_delete(name, kind, frozen, before, after):
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        adapter, launch = provider(directory, name)
        (directory / "stdout").write_bytes(before)
        make_frozen(directory / "stream.jsonl", kind, frozen)
        expected = readings(adapter, directory, launch)
        (directory / "stdout").write_bytes(after)
        assert readings(adapter, directory, launch) == expected
        (directory / "stdout").unlink()
        assert readings(adapter, directory, launch) == expected


@pytest.mark.parametrize("name", ("codex", "claude"))
@settings(max_examples=120, deadline=None, derandomize=True)
@example(stream=b"")
@example(stream=encode([MESSAGE, COMPLETED, CLAUDE_RESULT]))
@given(stream=STREAM)
def test_absent_frozen_stream_matches_freezing_stdout(name, stream):
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        adapter, launch = provider(directory, name)
        (directory / "stdout").write_bytes(stream)
        expected = readings(adapter, directory, launch)
        (directory / "stream.jsonl").write_bytes(stream)
        assert readings(adapter, directory, launch) == expected
