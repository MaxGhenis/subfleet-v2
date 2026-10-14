"""C-4.3: a frozen provider stream stays authoritative at every size and type."""

from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

from hypothesis import example, given, settings, strategies as st
import pytest

from subfleet.adapters.claude import ClaudeAdapter
from subfleet.adapters.codex import CodexAdapter, completion_evidence
from subfleet.contracts import OutcomeClass
from subfleet.daemon import Daemon
from subfleet.guardian import atomic_publish
from subfleet.sessions.transcripts import NotRegularFile
from tests.conftest import make_launch
from tests.frozen_streams import FROZEN_KINDS, NONREGULAR_KINDS, make_frozen
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


def freeze(directory, launch):
    # Exercise the daemon's capture with its real atomic writer, without a store
    # or worker pool for each generated example.
    publisher = SimpleNamespace(_publish=lambda role, path, data: atomic_publish(path, data))
    path = Daemon._freeze_stream(publisher, directory, launch)
    return replace(launch, raw_stream_path=str(path) if path else None)


@pytest.mark.parametrize("name", ("codex", "claude"))
@pytest.mark.parametrize("kind", NONREGULAR_KINDS)
@pytest.mark.parametrize("raw", ("stream.jsonl", "custom.jsonl", "missing.jsonl", None))
def test_nonregular_frozen_stream_never_reads_stdout(tmp_path, monkeypatch, name, kind, raw):
    adapter, launch = provider(tmp_path, name)
    launch = replace(launch, raw_stream_path=str(tmp_path / raw) if raw else None)
    (tmp_path / "stdout").write_bytes(encode([MESSAGE, COMPLETED, CLAUDE_RESULT]))
    frozen = tmp_path / ("custom.jsonl" if raw == "custom.jsonl" else "stream.jsonl")
    make_frozen(frozen, kind)
    original_open = os.open
    def guard(path, *args, **kwargs):
        if os.fspath(path) == str(frozen):
            pytest.fail("non-regular frozen entry must never be opened")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", guard)
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
    # The existing parser treats an undecodable final line as a truncated stream.
    expected = OutcomeClass.TRANSIENT if contents == b"garbage\n" else OutcomeClass.UNKNOWN
    assert outcome.cls == expected
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
@example(kind="hardlink", frozen=b"", before=b"", after=encode([MESSAGE, COMPLETED, CLAUDE_RESULT]))
@example(kind="socket", frozen=b"", before=b"", after=encode([MESSAGE, COMPLETED, CLAUDE_RESULT]))
@given(kind=st.sampled_from(FROZEN_KINDS), frozen=STREAM, before=STREAM, after=STREAM)
def test_frozen_authority_survives_stdout_rewrite_and_delete(name, kind, frozen, before, after):
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        adapter, launch = provider(directory, name)
        (directory / "stdout").write_bytes(before)
        make_frozen(directory / "stream.jsonl", kind, frozen)
        launch = freeze(directory, launch)
        expected = readings(adapter, directory, launch)
        (directory / "stdout").write_bytes(after)
        assert readings(adapter, directory, launch) == expected
        (directory / "stdout").unlink()
        assert readings(adapter, directory, launch) == expected


@pytest.mark.parametrize("name", ("codex", "claude"))
@settings(max_examples=120, deadline=None, derandomize=True)
@example(stream=b"", kind="hardlink")
@example(stream=encode([MESSAGE, COMPLETED, CLAUDE_RESULT]), kind="regular")
@example(stream=encode([MESSAGE, COMPLETED, CLAUDE_RESULT]), kind="socket")
@given(stream=STREAM, kind=st.sampled_from(FROZEN_KINDS))
def test_absent_frozen_stream_matches_freezing_stdout(name, stream, kind):
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        adapter, launch = provider(directory, name)
        # Unreadable and empty entries are equivalent to freezing no events.
        readable = kind in ("regular", "hardlink")
        (directory / "stdout").write_bytes(stream if readable else b"")
        expected = readings(adapter, directory, launch)
        (directory / "stdout").write_bytes(stream)
        make_frozen(directory / "stream.jsonl", kind, stream)
        launch = freeze(directory, launch)
        assert readings(adapter, directory, launch) == expected


@pytest.mark.parametrize("name", ("codex", "claude"))
@settings(max_examples=80, deadline=None, derandomize=True)
@example(kind="socket", stream=encode([MESSAGE, COMPLETED, CLAUDE_RESULT]))
@example(kind="hardlink", stream=encode([MESSAGE, COMPLETED, CLAUDE_RESULT]))
@given(kind=st.sampled_from(FROZEN_KINDS), stream=STREAM)
def test_frozen_readers_never_open_nonregular_entries(name, kind, stream):
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        adapter, launch = provider(directory, name)
        (directory / "stdout").write_bytes(stream)
        frozen = directory / "stream.jsonl"
        make_frozen(frozen, kind, stream)
        original_open = os.open
        with pytest.MonkeyPatch.context() as patch:
            def guard(path, *args, **kwargs):
                if os.fspath(path) == str(frozen):
                    assert kind not in NONREGULAR_KINDS
                return original_open(path, *args, **kwargs)
            patch.setattr(os, "open", guard)
            readings(adapter, directory, launch)
            artifact = Daemon._artifact(frozen, "raw-stream")
        assert (artifact is None) == (kind in NONREGULAR_KINDS)


@pytest.mark.parametrize("reader", ("codex-completion", "codex-classify", "codex-deliverable",
                                    "claude-raw", "claude-summary", "claude-classify",
                                    "claude-deliverable", "artifact"))
@pytest.mark.parametrize("kind", (*NONREGULAR_KINDS, "absent"))
def test_replacement_after_type_check_supplies_no_events(tmp_path, monkeypatch, reader, kind):
    """Review #209 input: replace a regular entry immediately before os.open."""
    adapter, launch = provider(tmp_path, "claude" if reader.startswith("claude") else "codex")
    frozen = tmp_path / "stream.jsonl"
    frozen.write_bytes(encode([MESSAGE, COMPLETED, CLAUDE_RESULT]))
    (tmp_path / "stdout").write_bytes(frozen.read_bytes())
    original_open = os.open
    seen = []
    def racing_open(path, flags, *args, **kwargs):
        if os.fspath(path) == str(frozen):
            seen.append(flags)
            assert flags & os.O_NOFOLLOW
            assert flags & os.O_NONBLOCK
            frozen.unlink()
            if kind != "absent":
                make_frozen(frozen, kind)
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", racing_open)
    if reader == "codex-completion":
        assert not completion_evidence(tmp_path, launch)["turn_completed"]
    elif reader.endswith("classify"):
        assert adapter.classify(tmp_path, launch, _exit(0)).cls != OutcomeClass.OK
    elif reader.endswith("deliverable"):
        assert adapter.deliverable(tmp_path, launch, None) is None
    elif reader == "claude-raw":
        assert adapter.raw_stream_text(tmp_path, launch) == ""
    elif reader == "claude-summary":
        assert adapter.stream_summary(tmp_path, launch).lines_total == 0
    else:
        assert Daemon._artifact(frozen, "raw-stream") is None
    assert len(seen) == 1


@pytest.mark.parametrize("error", sorted({errno.ENXIO, errno.EOPNOTSUPP, 102,
                                         errno.ELOOP, errno.EISDIR, errno.EINVAL}))
def test_nonregular_open_errors_are_typed(tmp_path, monkeypatch, error):
    from subfleet.adapters.base import open_stream
    frozen = tmp_path / "stream.jsonl"
    frozen.write_bytes(b"")
    def fail_open(*args, **kwargs):
        raise OSError(error, "non-regular replacement")
    monkeypatch.setattr(os, "open", fail_open)
    with pytest.raises(NotRegularFile) as caught:
        open_stream(frozen)
    assert caught.value.errno == error


def test_stream_open_keeps_unrelated_io_errors(tmp_path, monkeypatch):
    from subfleet.adapters.base import open_stream
    frozen = tmp_path / "stream.jsonl"
    frozen.write_bytes(b"")
    def fail_open(*args, **kwargs):
        raise OSError(errno.EIO, "disk failure")
    monkeypatch.setattr(os, "open", fail_open)
    with pytest.raises(OSError) as caught:
        open_stream(frozen)
    assert not isinstance(caught.value, NotRegularFile)
    assert caught.value.errno == errno.EIO
