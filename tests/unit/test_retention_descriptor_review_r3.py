"""Review r3 regressions for #279; every tree and descriptor belongs to the test."""
from __future__ import annotations

import dis
import errno
import os
import sys

import pytest

from subfleet import retention_fs as rfs
from tests.unit.test_retention_descriptor_review import raise_at
from tests.unit.test_retention_walk_descriptors import DEEP, open_descriptors, tree, unchanged_descriptors


@pytest.mark.parametrize("ambient_kind,close_kind", [
    pytest.param("runtime", "os", marks=pytest.mark.xfail(
        strict=True, reason="P2-4 (review r3): a handled caller exception suppresses a normal close failure")),
    pytest.param("keyboard", "os", marks=pytest.mark.xfail(
        strict=True, reason="P2-4 (review r3): a handled caller cancellation suppresses a normal close failure")),
    pytest.param("keyboard", "keyboard", marks=pytest.mark.xfail(
        strict=True, reason="P2-4 (review r3): a handled caller cancellation drops a new cleanup cancellation")),
    ("runtime", "keyboard"),
])
def test_normal_read_close_failure_is_not_suppressed_by_handled_caller_exception(
        tmp_path, monkeypatch, ambient_kind, close_kind):
    path = tmp_path / "file"
    path.write_bytes(b"scratch")
    ambient = RuntimeError("handled by caller") if ambient_kind == "runtime" else KeyboardInterrupt("handled by caller")
    cleanup = OSError(errno.EIO, "close") if close_kind == "os" else KeyboardInterrupt("new cleanup cancellation")
    original_close = os.close
    attempted = []

    def close(fd):
        attempted.append(fd)
        original_close(fd)
        raise cleanup

    with unchanged_descriptors():
        try:
            raise ambient
        except BaseException:
            with monkeypatch.context() as patch:
                patch.setattr(os, "close", close)
                with pytest.raises(type(cleanup)) as caught:
                    rfs.read_regular(path)
            assert caught.value is cleanup
            assert caught.value.__context__ is ambient
        assert len(attempted) == 1


@pytest.mark.xfail(strict=True, reason="P3-5 (review r3): an interrupt after stack.pop leaves its descriptor unowned")
def test_interrupt_after_stack_pop_closes_popped_child():
    instructions = list(dis.get_instructions(rfs.walk))
    at = next(i for i, instruction in enumerate(instructions)
              if instruction.argval == "pop" and instruction.opname in ("LOAD_ATTR", "LOAD_METHOD"))
    call = next(i for i in range(at, len(instructions)) if instructions[i].opname.startswith("CALL"))
    offset = instructions[call + 1].offset
    injected = KeyboardInterrupt("after stack.pop")
    with tree(DEEP) as (_, root_fd):
        before = open_descriptors()
        iterator = rfs.walk(root_fd)
        for _ in range(4):
            next(iterator)
        try:
            with raise_at(rfs.walk.__code__, offset, injected) as fired:
                with pytest.raises(KeyboardInterrupt) as caught:
                    list(iterator)
            assert fired and caught.value is injected
            os.fstat(root_fd)
            assert open_descriptors() == before, "the popped child's descriptor leaked"
        finally:
            iterator.close()
            for fd in open_descriptors() - before:
                os.close(fd)


class Cancelled(BaseException):
    pass


@pytest.mark.xfail(strict=True, reason="P2-5 (review r3): AttemptTemp.size's root close replaces a walk cancellation")
@pytest.mark.parametrize("cleanup_kind", ["os", "keyboard"])
def test_attempt_temp_size_root_close_preserves_walk_cancellation(tmp_path, monkeypatch, cleanup_kind):
    from pathlib import Path
    from subfleet.attempt_tmp import AttemptTemp

    allocation = AttemptTemp(tmp_path / "store")
    record = allocation.create("dummy")
    root = Path(record["path"])
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "leaf").write_bytes(b"scratch")
    primary = KeyboardInterrupt("walk cancellation")
    secondary = OSError(errno.EIO, "root close") if cleanup_kind == "os" else KeyboardInterrupt("root close")
    original_names, original_open, original_close = rfs._names, rfs.open_dir, os.close
    root_fds, attempted = [], []
    calls = 0

    def open_dir(name, **kwargs):
        fd = original_open(name, **kwargs)
        if Path(name) == root:
            root_fds.append(fd)
        return fd

    def names(fd):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise primary
        return original_names(fd)

    def close(fd):
        attempted.append(fd)
        original_close(fd)
        if fd in root_fds:
            raise secondary

    caught = value = None
    with unchanged_descriptors(), monkeypatch.context() as patch:
        patch.setattr(rfs, "open_dir", open_dir)
        patch.setattr(rfs, "_names", names)
        patch.setattr(os, "close", close)
        try:
            value = allocation.size(record)
        except BaseException as exc:
            caught = exc
    assert len(root_fds) == 1 and attempted.count(root_fds[0]) == 1
    assert caught is primary, (value, type(caught).__name__)


@pytest.mark.xfail(strict=True, reason="P3-6 (review r3): record_complete drops walk cleanup cancellation during finalization")
@pytest.mark.parametrize("failure", ["keyboard", "system", "custom"])
def test_record_complete_early_return_propagates_cleanup_cancellation(monkeypatch, failure):
    injected = {"keyboard": KeyboardInterrupt("walk cleanup"), "system": SystemExit("walk cleanup"),
                "custom": Cancelled("walk cleanup")}[failure]
    with tree(DEEP) as (_, root_fd), unchanged_descriptors(root_fd):
        original_open, original_close = rfs.open_dir, os.close
        opened, attempted, unraisable = [], [], []

        def open_dir(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def close(fd):
            attempted.append(fd)
            original_close(fd)
            if fd == opened[0]:
                raise injected

        caught = value = None
        with monkeypatch.context() as patch:
            patch.setattr(rfs, "open_dir", open_dir)
            patch.setattr(os, "close", close)
            patch.setattr(sys, "unraisablehook", unraisable.append)
            try:
                value = rfs.record_complete(root_fd, {}, "x/RECORD")
            except BaseException as exc:
                caught = exc
        assert attempted == opened
        assert caught is injected, (value, [(type(event.exc_value).__name__, event.err_msg) for event in unraisable])
        assert not unraisable
