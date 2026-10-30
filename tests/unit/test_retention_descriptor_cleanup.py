"""D-WALK-FD1 review r2: cleanup exception precedence in scratch trees."""
from __future__ import annotations

import errno
import os

import pytest

from subfleet import retention_fs as rfs
from tests.unit.test_retention_walk_descriptors import DEEP, tree, unchanged_descriptors


class Cancelled(BaseException):
    pass


def failure(kind):
    return {"os": lambda: OSError(errno.EIO, "close"), "runtime": lambda: RuntimeError("body"),
            "keyboard": lambda: KeyboardInterrupt("cancel"), "system": lambda: SystemExit("cancel"),
            "custom": lambda: Cancelled("cancel"), "generator": lambda: GeneratorExit("close")}[kind]()


def cancellation(exc):
    return not isinstance(exc, (Exception, GeneratorExit))


@pytest.mark.parametrize("body", [None, "os", "runtime", "keyboard", "system", "custom", "generator"])
@pytest.mark.parametrize("cleanup", ["os", "runtime", "keyboard", "system", "custom", "generator"])
def test_close_all_exception_precedence_and_drain(monkeypatch, body, cleanup):
    primary = failure(body) if body else None
    secondary = failure(cleanup)
    attempted = []
    def close(fd):
        attempted.append(fd)
        if fd == 101:
            raise secondary
    with monkeypatch.context() as patch:
        patch.setattr(os, "close", close)
        try:
            try:
                if primary is not None:
                    raise primary
            finally:
                rfs._close_all([101, 102, 103])
        except BaseException as caught:
            expected = secondary if primary is None or (cancellation(secondary) and not cancellation(primary)) else primary
            assert caught is expected
            if primary is not None and expected is secondary:
                assert caught.__context__ is primary
        else:
            pytest.fail("the original or cleanup failure must propagate")
    assert attempted == [101, 102, 103]


def test_close_all_without_body_raises_first_close_error(monkeypatch):
    errors = [OSError(errno.EIO, "first close"), RuntimeError("second close")]
    attempted = []
    def close(fd):
        attempted.append(fd)
        if fd < 103:
            raise errors[fd - 101]
    with monkeypatch.context() as patch:
        patch.setattr(os, "close", close)
        with pytest.raises(OSError) as caught:
            rfs._close_all([101, 102, 103])
    assert caught.value is errors[0]
    assert attempted == [101, 102, 103]


def test_set_aside_cleanup_cancellation_overrides_handoff_oserror(monkeypatch):
    primary = OSError(errno.EIO, "handoff close")
    secondary = KeyboardInterrupt("child cleanup")
    with tree(DEEP) as (root, root_fd), unchanged_descriptors(root_fd):
        opened, attempted = [], []
        original_open, original_close = rfs.open_dir, os.close
        def open_dir(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened.append(fd)
            return fd
        def close(fd):
            attempted.append(fd)
            original_close(fd)
            if len(opened) == 2:
                raise primary if fd == opened[0] else secondary
        reclaim = rfs.Reclaim({}, root / "conflicts", "label")
        with monkeypatch.context() as patch:
            patch.setattr(rfs, "open_dir", open_dir)
            patch.setattr(os, "close", close)
            with pytest.raises(KeyboardInterrupt) as caught:
                reclaim._set_aside_fd(root_fd, "a", "a", "new")
        assert caught.value is secondary
        assert secondary.__context__ is primary
        assert attempted == opened
        assert not reclaim.errors


def test_open_dir_swapped_identity_survives_close_error(monkeypatch):
    with tree(DEEP) as (_, root_fd), unchanged_descriptors(root_fd):
        st = os.stat("a", dir_fd=root_fd, follow_symlinks=False)
        wrong = os.stat_result((st.st_mode, st.st_ino + 1, *list(st)[2:]))
        original_close = os.close
        attempted = []
        def close(fd):
            attempted.append(fd)
            original_close(fd)
            raise OSError(errno.EIO, "validation close")
        with monkeypatch.context() as patch:
            patch.setattr(os, "close", close)
            with pytest.raises(rfs.TreeError) as caught:
                rfs.open_dir("a", dir_fd=root_fd, expect=wrong)
        assert caught.value.reason == "swapped"
        assert str(caught.value) == "swapped: a changed while it was opened"
        assert len(attempted) == 1


def test_tree_bytes_check_cancellation_survives_drain_close_error(monkeypatch):
    injected = KeyboardInterrupt("check cancellation")
    calls = 0
    def check():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise injected
    with tree(DEEP) as (root, root_fd), unchanged_descriptors(root_fd):
        opened, attempted = [], []
        original_open, original_close = rfs.open_dir, os.close
        def open_dir(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened.append(fd)
            return fd
        def close(fd):
            attempted.append(fd)
            original_close(fd)
            if opened and fd == opened[0]:
                raise OSError(errno.EIO, "drain close")
        with monkeypatch.context() as patch:
            patch.setattr(rfs, "open_dir", open_dir)
            patch.setattr(os, "close", close)
            with pytest.raises(KeyboardInterrupt) as caught:
                rfs.tree_bytes(root, check)
        assert caught.value is injected
        assert len(opened) == 2
        assert set(opened) <= set(attempted)
        assert len(attempted) == len(set(attempted))


def test_walk_close_preserves_generator_exit_over_close_oserror(monkeypatch):
    with tree(DEEP) as (_, root_fd), unchanged_descriptors(root_fd):
        opened, attempted = [], []
        original_open, original_close = rfs.open_dir, os.close
        def open_dir(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened.append(fd)
            return fd
        def close(fd):
            attempted.append(fd)
            original_close(fd)
            if fd == opened[0]:
                raise OSError(errno.EIO, "close during GeneratorExit")
        with monkeypatch.context() as patch:
            patch.setattr(rfs, "open_dir", open_dir)
            iterator = rfs.walk(root_fd)
            for _ in range(4):
                next(iterator)
            patch.setattr(os, "close", close)
            iterator.close()
        assert attempted == opened
        assert len(opened) == 2
