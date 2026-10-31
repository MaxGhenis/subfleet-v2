"""Review r2 regressions for PR 279: cleanup closes each descriptor at most once; no live trees or providers.

A close(2) that raises has, in the realistic cases, already released its number: an interrupt delivered
after the call returns (the `closed_first=True` case of review r1's test), or an error close(2) reports after
releasing the descriptor. In the daemon another thread's open can take that number before cleanup runs.
"""
from __future__ import annotations

import dis
import errno
import fcntl
import os
import stat

import pytest
from subfleet import retention_fs as rfs
from tests.unit.test_retention_descriptor_review import raise_at
from tests.unit.test_retention_walk_descriptors import DEEP, tree, unchanged_descriptors


def is_open(fd: int) -> bool:
    try:
        fcntl.fcntl(fd, fcntl.F_GETFD)
    except OSError as exc:
        assert exc.errno == errno.EBADF
        return False
    return True


@pytest.mark.parametrize("site", ["path", "conflict", "set-aside"])
@pytest.mark.parametrize("error", ["keyboard", "os"])
def test_failed_handoff_close_never_closes_a_reused_number(monkeypatch, site, error):
    injected = KeyboardInterrupt("handoff close") if error == "keyboard" else OSError(errno.EIO, "handoff close")
    with tree(DEEP) as (root, root_fd), unchanged_descriptors(root_fd):
        close = os.close
        opened = []
        original_open = rfs.open_dir
        def open_dir(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened.append(fd)
            return fd
        stranger = None
        def released_then_raised(fd):
            nonlocal stranger
            if stranger is None and len(opened) == 2 and fd == opened[0]:
                close(fd)
                # Another thread's open takes the freed number before cleanup runs.
                spare = os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)
                if spare != fd:
                    os.dup2(spare, fd, inheritable=False)
                    close(spare)
                stranger = fd
                raise injected
            return close(fd)
        reclaim = rfs.Reclaim({}, root / "conflicts", "label")
        caught = None
        try:
            with monkeypatch.context() as patch:
                patch.setattr(rfs, "open_dir", open_dir)
                patch.setattr(os, "close", released_then_raised)
                try:
                    if site == "path":
                        rfs._open_path(root_fd, "a/b")
                    elif site == "conflict":
                        reclaim._conflict_dir("nested")
                    else:
                        reclaim._set_aside_fd(root_fd, "a", "a", "new")
                except BaseException as exc:  # noqa: BLE001
                    caught = exc
            assert stranger is not None
            if site == "set-aside" and error == "os":
                assert caught is None and reclaim.errors      # _set_aside_fd records an OSError and keeps going
            else:
                assert caught is injected
            assert is_open(stranger), "cleanup closed a descriptor it no longer owned"
            assert stat.S_ISCHR(os.fstat(stranger).st_mode)
        finally:
            if stranger is not None and is_open(stranger):
                close(stranger)


def after_append(func) -> int:
    """The instruction after the CALL that registers the new descriptor: the owner already holds it."""
    instructions = list(dis.get_instructions(func))
    at = [i for i, ins in enumerate(instructions)
          if ins.opname in ("LOAD_ATTR", "LOAD_METHOD") and ins.argval == "append"][-1]
    call = next(i for i in range(at, len(instructions)) if instructions[i].opname.startswith("CALL"))
    return instructions[call + 1].offset


@pytest.mark.parametrize("site", ["walk", "repository"])
def test_interrupt_after_registration_closes_child_once(monkeypatch, site):
    func = rfs.walk if site == "walk" else rfs.RegenerableWalk._in_repository
    injected = KeyboardInterrupt("after registration")
    closes = []
    close = os.close
    def counting_close(fd):
        closes.append(fd)
        return close(fd)
    with tree(DEEP) as (_, root_fd), unchanged_descriptors(root_fd):
        with monkeypatch.context() as patch:
            patch.setattr(os, "close", counting_close)
            with raise_at(func.__code__, after_append(func), injected) as fired:
                with pytest.raises(KeyboardInterrupt) as caught:
                    if site == "walk":
                        list(rfs.walk(root_fd))
                    else:
                        rfs.RegenerableWalk(root_fd, lambda _: True)._in_repository("a/b")
        assert fired
        assert caught.value is injected
        assert len(closes) == len(set(closes)), closes
