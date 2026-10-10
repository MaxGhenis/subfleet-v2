"""Host push object intake has a finite allocation budget and independent data."""
import ctypes
import errno
import inspect
import os
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import host_push
from test_host_push import submit, worlds  # noqa: F401
from test_host_push_admission import notices, ordinary, pin_routing, placed
from test_host_push_build import admit_ready


def _refused_and_behind_placed(world, job_id, behind, words):
    admit_ready(world.core)
    row = world.core._job(job_id)
    assert (row["state"], row["rc"]) == ("failed", 7), dict(row)
    assert words in " ".join(notices(world.core, job_id))
    assert placed(world.core, behind)
    assert not (world.core.root / "worktrees" / job_id).exists()


def test_sparse_object_refusal_places_the_job_behind_it(worlds):
    with worlds() as world:
        pin_routing(world, world.patch)
        job_id = submit(world)
        behind = ordinary(world)
        pack = world.repo / ".git/objects/pack" / ("pack-" + "a" * 40 + ".pack")
        with pack.open("wb") as output:
            output.truncate(1024 * 1024)
        assert pack.stat().st_blocks * 512 < pack.stat().st_size
        _refused_and_behind_placed(world, job_id, behind, "sparse")


def test_object_budget_refusal_places_the_job_behind_it(worlds):
    with worlds() as world:
        pin_routing(world, world.patch)
        job_id = submit(world)
        behind = ordinary(world)
        world.patch.setattr(host_push, "OBJECT_COPY_CAP", 1, raising=False)
        _refused_and_behind_placed(world, job_id, behind, "object copy exceeds")


def _copy(source, target, remaining):
    fd = os.open(source, host_push.FILE_FLAGS)
    try:
        return host_push._copy_object(fd, target, remaining)
    finally:
        os.close(fd)


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(cap=st.integers(min_value=0, max_value=32768),
       sizes=st.lists(st.integers(min_value=0, max_value=16384), min_size=1, max_size=12))
def test_total_fallback_bytes_never_exceed_cap(tmp_path, monkeypatch, cap, sizes):
    monkeypatch.setattr(host_push, "_clone_object", lambda *args: False)
    monkeypatch.setattr(host_push, "OBJECT_COPY_CAP", cap)
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        folder = Path(directory)
        copied = 0
        outputs = []
        for index, size in enumerate(sizes):
            source, target = folder / f"source-{index}", folder / f"target-{index}"
            source.write_bytes(b"x" * size)
            outputs.append(target)
            try:
                copied += _copy(source, target, cap - copied)
            except host_push.PushError as exc:
                assert "object copy exceeds" in str(exc)
                break
        allocated = sum(path.stat().st_size for path in outputs if path.exists())
        assert allocated == copied <= cap


@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(size=st.integers(min_value=1, max_value=32768),
       growth=st.integers(min_value=1, max_value=65536))
def test_source_growth_cannot_amplify_fallback_copy(tmp_path, monkeypatch, size, growth):
    monkeypatch.setattr(host_push, "_clone_object", lambda *args: False)
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        source, target = Path(directory) / "source", Path(directory) / "target"
        source.write_bytes(b"x" * size)
        original = os.pread
        grew = False

        def grow(fd, count, offset):
            nonlocal grew
            if not grew:
                grew = True
                with source.open("ab") as output:
                    output.write(b"x" * growth)
            return original(fd, count, offset)

        with monkeypatch.context() as patch:
            patch.setattr(host_push.os, "pread", grow)
            with pytest.raises(host_push.PushError, match="changed while copying"):
                _copy(source, target, size)
        assert target.stat().st_size <= size


@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(size=st.integers(min_value=1, max_value=16384),
       growth=st.integers(min_value=1, max_value=32768),
       slack=st.integers(min_value=0, max_value=32768))
def test_oversized_clone_snapshot_never_survives_refusal(tmp_path, monkeypatch,
                                                       size, growth, slack):
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        source, target = Path(directory) / "source", Path(directory) / "target"
        source.write_bytes(b"x" * size)

        def clone_growing_source(fd, target):
            with source.open("ab") as output:
                output.write(b"x" * growth)
            # Stand in for APFS's atomic snapshot, which shares data blocks.
            target.write_bytes(os.pread(fd, size + growth, 0))
            return True

        with monkeypatch.context() as patch:
            patch.setattr(host_push, "_clone_object", clone_growing_source)
            with pytest.raises(host_push.PushError, match="exceeds|changed while copying"):
                _copy(source, target, size + slack)
        assert not target.exists() or target.stat().st_size <= size + slack


def test_cloned_or_copied_object_is_independent_of_source(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"x" * 16384)
    assert _copy(source, target, 16384) == 16384
    assert target.stat().st_ino != source.stat().st_ino
    source.write_bytes(b"y" * 16384)
    assert target.read_bytes() == b"x" * 16384
    target.write_bytes(b"z" * 16384)
    assert source.read_bytes() == b"y" * 16384


def test_apfs_clone_is_preferred_and_unsupported_volumes_fall_back(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"x" * 8192)
    calls = []

    def native(source_fd, target_fd, name, flags):
        calls.append((source_fd, target_fd, name, flags))
        # An independent snapshot stands in for the native APFS primitive.
        with os.fdopen(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                              dir_fd=target_fd), "wb") as output:
            output.write(os.pread(source_fd, 8192, 0))
        return 0

    monkeypatch.setattr(host_push.rfs, "_fclonefileat", native)
    assert _copy(source, tmp_path / "cloned", 8192) == 8192
    assert len(calls) == 1 and calls[0][3] == host_push.rfs.CLONE_NOFOLLOW
    monkeypatch.setattr(host_push.rfs, "_fclonefileat", None)
    assert _copy(source, tmp_path / "copied", 8192) == 8192

    def unsupported(*args):
        ctypes.set_errno(errno.EXDEV)
        return -1

    monkeypatch.setattr(host_push.rfs, "_fclonefileat", unsupported)
    assert _copy(source, tmp_path / "cross-volume-copy", 8192) == 8192


def _mutate_copy(monkeypatch, old):
    original = inspect.getsource(host_push._copy_object)
    assert old in original
    monkeypatch.setattr(host_push, "_clone_object", lambda *args: False)
    namespace = dict(host_push.__dict__)
    exec(compile(original.replace(old, "if False:"), "<copy mutant>", "exec"), namespace)
    monkeypatch.setattr(host_push, "_copy_object", namespace["_copy_object"])


def _assert_refused(source, target, remaining, words):
    try:
        _copy(source, target, remaining)
    except host_push.PushError as exc:
        assert words in str(exc)
        return
    raise AssertionError("object was copied instead of refused")


def test_mutation_removing_copy_budget_is_killed(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"x" * 32)
    _assert_refused(source, tmp_path / "correct", 1, "object copy exceeds")
    _mutate_copy(monkeypatch, "if before.st_size > remaining:")
    with pytest.raises(AssertionError, match="instead of refused"):
        _assert_refused(source, tmp_path / "mutant", 1, "object copy exceeds")


def test_mutation_removing_sparse_refusal_is_killed(tmp_path, monkeypatch):
    source = tmp_path / "source"
    with source.open("wb") as output:
        output.truncate(32768)
    _assert_refused(source, tmp_path / "correct", 32768, "sparse")
    _mutate_copy(monkeypatch, "if before.st_blocks * 512 < before.st_size:")
    with pytest.raises(AssertionError, match="instead of refused"):
        _assert_refused(source, tmp_path / "mutant", 32768, "sparse")
