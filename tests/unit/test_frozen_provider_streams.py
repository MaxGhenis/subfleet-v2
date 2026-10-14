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
from subfleet.contracts import Outcome, OutcomeClass
from subfleet.daemon import Daemon
from subfleet.guardian import atomic_publish
from subfleet.sessions.transcripts import NotRegularFile
from tests.conftest import make_launch
from tests.frozen_streams import FREEZE_STEPS, FROZEN_KINDS, NONREGULAR_KINDS, link_during_freeze, make_frozen
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
    publisher = SimpleNamespace(_publish=lambda role, path, data: atomic_publish(path, data),
                                publish_hook=None, _read_json=Daemon._read_json)
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
        assert adapter.deliverable(tmp_path, launch, Outcome(OutcomeClass.UNKNOWN, "replacement")) is None
    elif reader == "claude-raw":
        assert adapter.raw_stream_text(tmp_path, launch) == ""
    elif reader == "claude-summary":
        assert adapter.stream_summary(tmp_path, launch).lines_total == 0
    else:
        assert Daemon._artifact(frozen, "raw-stream") is None
    assert len(seen) == 1


@pytest.mark.parametrize("error", sorted({errno.ENXIO, errno.EOPNOTSUPP, errno.ENOTSUP,
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


@pytest.mark.parametrize("name", ("codex", "claude"))
@pytest.mark.parametrize("source", ("stdout", "stderr", "last.md"))
@pytest.mark.parametrize("raw", ("stream.jsonl", "custom.jsonl"))
def test_freeze_detaches_aliases_to_any_writable_artifact(tmp_path, name, source, raw):
    adapter, launch = provider(tmp_path, name)
    launch = replace(launch, raw_stream_path=str(tmp_path / raw))
    payload = encode([MESSAGE, COMPLETED, CLAUDE_RESULT])
    live = tmp_path / source
    live.write_bytes(payload)
    frozen = tmp_path / raw
    os.link(live, frozen)
    with live.open("wb", buffering=0) as writer:
        # Restore after opening in write mode; the descriptor shares the alias.
        writer.write(payload)
        launch = freeze(tmp_path, launch)
        frozen = Path(launch.raw_stream_path)
        writer.seek(0)
        writer.truncate()
        writer.write(b"later provider output\n")
    assert frozen.read_bytes() == payload
    assert frozen.stat().st_nlink == 1
    assert not os.path.samefile(frozen, live)
    assert completion_evidence(tmp_path, launch)["turn_completed"]


@pytest.mark.parametrize("name", ("codex", "claude"))
@pytest.mark.parametrize("layout", ("parent-alias", "same-canonical", "unlinked-writer"))
def test_freeze_identity_detaches_live_names_and_unlinked_writers(tmp_path, name, layout):
    adapter, launch = provider(tmp_path, name)
    payload = encode([MESSAGE])
    live = tmp_path / "stdout"
    live.write_bytes(payload)
    if layout == "parent-alias":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        launch = replace(launch, raw_stream_path=str(alias / "stdout"))
    elif layout == "same-canonical":
        live = tmp_path / "stream.jsonl"
        live.write_bytes(payload)
        launch = replace(launch, stdout_path=str(live), raw_stream_path=str(live))
    else:
        os.link(live, tmp_path / "stream.jsonl")
    with live.open("ab", buffering=0) as writer:
        if layout == "unlinked-writer":
            live.unlink()
        frozen_launch = freeze(tmp_path, launch)
        expected = readings(adapter, tmp_path, frozen_launch)
        writer.write(encode([COMPLETED, CLAUDE_RESULT]))
        assert readings(adapter, tmp_path, frozen_launch) == expected
        assert readings(adapter, tmp_path, freeze(tmp_path, launch)) == expected
    assert Path(frozen_launch.raw_stream_path).read_bytes() == payload


@pytest.mark.parametrize("name", ("codex", "claude"))
@pytest.mark.parametrize("step", FREEZE_STEPS)
@pytest.mark.parametrize("kind", ("hardlink", "symlink"))
def test_freeze_authority_and_fallback_equivalence_at_every_link_step(tmp_path, name, step, kind):
    assert_link_authority(tmp_path, name, step, kind, encode([MESSAGE]), encode([COMPLETED, CLAUDE_RESULT]))


def assert_link_authority(directory, name, step, kind, before, after):
    adapter, launch = provider(directory, name)
    live, candidate = directory / "stdout", directory / "stream.jsonl"
    live.write_bytes(before)
    expected = readings(adapter, directory, launch)  # Legacy stdout fallback.
    candidate.write_bytes(before)
    with pytest.MonkeyPatch.context() as patch:
        with link_during_freeze(patch, candidate, live, step, kind) as fired:
            frozen_launch = freeze(directory, launch)
    assert fired == [step]
    snapshot = Path(frozen_launch.raw_stream_path)
    assert not os.path.samefile(snapshot, live)
    assert snapshot.read_bytes() == before
    assert readings(adapter, directory, frozen_launch) == expected
    live.write_bytes(after)
    assert snapshot.read_bytes() == before
    assert readings(adapter, directory, frozen_launch) == expected
    replayed = freeze(directory, launch)
    assert Path(replayed.raw_stream_path) == snapshot
    assert readings(adapter, directory, replayed) == expected


@pytest.mark.parametrize("name", ("codex", "claude"))
@settings(max_examples=80, deadline=None, derandomize=True)
@given(step=st.sampled_from(FREEZE_STEPS), kind=st.sampled_from(("hardlink", "symlink")),
       before=STREAM, after=STREAM)
def test_property_frozen_authority_and_fallback_over_link_timing(name, step, kind, before, after):
    with tempfile.TemporaryDirectory() as temporary:
        assert_link_authority(Path(temporary), name, step, kind, before, after)


@pytest.mark.parametrize("name", ("codex", "claude"))
def test_freeze_replay_never_recopies_a_replaced_candidate(tmp_path, name):
    adapter, launch = provider(tmp_path, name)
    live = tmp_path / "stdout"
    live.write_bytes(encode([MESSAGE]))
    os.link(live, tmp_path / "stream.jsonl")
    frozen_launch = freeze(tmp_path, launch)
    expected = readings(adapter, tmp_path, frozen_launch)
    candidate = tmp_path / "stream.jsonl"
    candidate.unlink()
    os.link(live, candidate)
    with live.open("ab") as writer:
        writer.write(encode([COMPLETED, CLAUDE_RESULT]))
    assert readings(adapter, tmp_path, freeze(tmp_path, launch)) == expected


@pytest.mark.parametrize("error", (errno.ENOENT, errno.EIO, errno.EACCES))
def test_freeze_propagates_post_check_regular_file_errors(tmp_path, monkeypatch, error):
    _, launch = provider(tmp_path, "codex")
    candidate = tmp_path / "stream.jsonl"
    candidate.write_bytes(b"")
    original_open = os.open
    def opening(path, *args, **kwargs):
        if os.fspath(path) == str(candidate):
            raise OSError(error, "injected post-check error")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", opening)
    with pytest.raises(OSError) as caught:
        freeze(tmp_path, launch)
    assert not isinstance(caught.value, NotRegularFile)
    assert caught.value.errno == error


@pytest.mark.parametrize("replacement", (False, True), ids=("regular-denial", "mode-000-directory"))
def test_stream_open_distinguishes_permission_denial_from_nonregular_replacement(tmp_path, monkeypatch, replacement):
    from subfleet.adapters.base import open_stream
    candidate = tmp_path / "stream.jsonl"
    candidate.write_bytes(b"")
    def opening(*args, **kwargs):
        if replacement:
            candidate.unlink()
            candidate.mkdir(mode=0)
        raise PermissionError(errno.EACCES, "permission denied", str(candidate))
    monkeypatch.setattr(os, "open", opening)
    try:
        with pytest.raises(NotRegularFile if replacement else PermissionError):
            open_stream(candidate)
    finally:
        if replacement:
            candidate.chmod(0o700)


def test_freeze_copy_uses_bounded_reads_from_one_candidate_descriptor(tmp_path, monkeypatch):
    from subfleet import daemon as module
    _, launch = provider(tmp_path, "codex")
    candidate = tmp_path / "stream.jsonl"
    # Force the old implementation's copy branch too.
    (tmp_path / "stdout").write_bytes(b"x" * (2 * 1024 * 1024 + 3))
    os.link(tmp_path / "stdout", candidate)
    original_stream = module.open_stream
    original_open = os.open
    source_info = candidate.stat()
    opened, sizes, descriptors = [], [], []
    def kernel_open(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (source_info.st_dev, source_info.st_ino):
            descriptors.append(fd)
        return fd
    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024
            sizes.append(size)
            return self.stream.read(size)
    def opening(path, *args, **kwargs):
        stream = original_stream(path, *args, **kwargs)
        if Path(path) == candidate:
            opened.append(path)
            return Reader(stream)
        return stream
    monkeypatch.setattr(module, "open_stream", opening)
    monkeypatch.setattr(os, "open", kernel_open)
    frozen_launch = freeze(tmp_path, launch)
    assert len(opened) == 1
    assert len(descriptors) == 1
    assert len(sizes) >= 3
    assert Path(frozen_launch.raw_stream_path).read_bytes() == (tmp_path / "stdout").read_bytes()


@pytest.mark.parametrize("step", ("file-fsync", "rename", "directory-fsync"))
def test_copy_linked_to_live_output_during_publication_is_not_frozen(tmp_path, monkeypatch, step):
    adapter, launch = provider(tmp_path, "codex")
    payload = encode([MESSAGE])
    candidate, live = tmp_path / "stream.jsonl", tmp_path / "stdout"
    candidate.write_bytes(payload)
    live.write_bytes(payload)
    import tempfile as temporary_files
    original_mkstemp, original_rename, original_fsync = temporary_files.mkstemp, os.rename, os.fsync
    outputs, published, fired = {}, [], []
    def link(path):
        if fired:
            return
        fired.append(step)
        live.unlink()
        os.link(path, live)
    def creating(*args, **kwargs):
        fd, path = original_mkstemp(*args, **kwargs)
        if Path(path).name.startswith(".stream.frozen-"):
            outputs[fd] = path
        return fd, path
    def renaming(source, destination, *args, **kwargs):
        result = original_rename(source, destination, *args, **kwargs)
        if Path(destination).name.startswith("stream.frozen-"):
            published.append(destination)
            if step == "rename":
                link(destination)
        return result
    def syncing(fd):
        import stat
        if step == "file-fsync" and fd in outputs:
            link(outputs[fd])
        elif (step == "directory-fsync" and published
              and stat.S_ISDIR(os.fstat(fd).st_mode)):
            link(published[-1])
        return original_fsync(fd)
    monkeypatch.setattr(temporary_files, "mkstemp", creating)
    monkeypatch.setattr(os, "rename", renaming)
    monkeypatch.setattr(os, "fsync", syncing)
    frozen_launch = freeze(tmp_path, launch)
    assert fired == [step]
    snapshot = Path(frozen_launch.raw_stream_path)
    assert not os.path.samefile(snapshot, live)
    expected = readings(adapter, tmp_path, frozen_launch)
    live.write_bytes(encode([COMPLETED]))
    assert snapshot.read_bytes() == payload
    assert readings(adapter, tmp_path, frozen_launch) == expected
