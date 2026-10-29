"""Scratch review regressions for PR 279; no live trees or providers."""
from __future__ import annotations

import contextlib
import dis
import errno
import os
from pathlib import Path
import sys
import types

import pytest
from subfleet import retention_fs as rfs
from tests.unit.test_retention_walk_descriptors import (
    DEEP, open_descriptors, tree, unchanged_descriptors,
)


def baseline():
    name = "subfleet.retention_fs_review_base"
    if name not in sys.modules:
        # release/217's subfleet/retention_fs.py at bc108d3ad, byte for byte (blob c9c1a5d6f436), frozen as a
        # fixture: CI checks out one commit, so `git show bc108d3ad:...` has no object to read there.
        source = (Path(__file__).resolve().parents[1] / "fixtures" / "retention_fs_bc108d3ad.py").read_text()
        module = types.ModuleType(name)
        sys.modules[name] = module
        exec(compile(source, "<retention_fs at bc108d3ad>", "exec"), module.__dict__)
    return sys.modules[name]


@contextlib.contextmanager
def raise_at(code, offset, injected):
    """Raise `injected` when `code` reaches the instruction at `offset`, through sys.monitoring.

    sys.settrace with f_trace_opcodes set in the call event misses the first instructions of a
    frame on Python 3.12 (CI's 3.12 shard: _layout's BUILD_MAP at offset 2 never fired), while
    sys.monitoring's INSTRUCTION event (PEP 669) reaches every instruction on 3.12 and 3.14.
    """
    monitoring = sys.monitoring
    tool = monitoring.DEBUGGER_ID
    fired = []
    def on_instruction(seen, at):
        if seen is code and at == offset:
            fired.append(at)
            raise injected
    monitoring.use_tool_id(tool, "descriptor review")
    try:
        monitoring.register_callback(tool, monitoring.events.INSTRUCTION, on_instruction)
        monitoring.set_local_events(tool, code, monitoring.events.INSTRUCTION)
        yield fired
    finally:
        monitoring.set_local_events(tool, code, 0)
        monitoring.register_callback(tool, monitoring.events.INSTRUCTION, None)
        monitoring.free_tool_id(tool)


def result(exc):
    return (type(exc).__name__, getattr(exc, "reason", None), getattr(exc, "errno", None), str(exc))


@pytest.mark.parametrize("operation", ["listing", "stat", "check"])
@pytest.mark.parametrize("error", ["os", "runtime", "keyboard", "permission", "missing", "tree"])
def test_walk_exit_exception_parity(monkeypatch, operation, error):
    entries = DEEP + [("z", "d"), ("z/file", "f")]
    total = 4 if operation == "listing" else 5
    for kth in range(1, total + 1):
        answers = []
        for module in (baseline(), rfs):
            with tree(entries) as (_, root_fd):
                before = open_descriptors()
                injected = {
                    "os": OSError(errno.EIO, "review"),
                    "runtime": RuntimeError("review"),
                    "keyboard": KeyboardInterrupt("review"),
                    "permission": PermissionError(errno.EACCES, "review"),
                    "missing": FileNotFoundError(errno.ENOENT, "review"),
                    "tree": module.TreeError("injected"),
                }[error]
                calls = 0
                check = None
                with monkeypatch.context() as patch:
                    original = module._names if operation == "listing" else os.stat
                    def fail(*args, **kwargs):
                        nonlocal calls
                        calls += 1
                        if calls == kth:
                            raise injected
                        return original(*args, **kwargs)
                    if operation == "check":
                        def check():
                            nonlocal calls
                            calls += 1
                            if calls == kth:
                                raise injected
                    else:
                        patch.setattr(module if operation == "listing" else os,
                                      "_names" if operation == "listing" else "stat", fail)
                    caught = None
                    try:
                        list(module.walk(root_fd, check))
                    except BaseException as exc:
                        caught = exc
                    assert caught is not None
                    assert calls == kth
                    answers.append(result(caught))
                after = open_descriptors()
                try:
                    os.fstat(root_fd)
                    if module is rfs:
                        assert after == before, (operation, kth, error, after - before)
                finally:
                    for fd in after - before:
                        os.close(fd)
        assert answers[0] == answers[1], (operation, kth, error, answers)


@pytest.mark.parametrize("action", ["close", "throw-runtime", "throw-keyboard"])
def test_walk_close_and_throw_at_every_yield(action):
    entries = DEEP + [("c", "d"), ("c/file", "f"), ("z", "l")]
    with tree(entries) as (_, root_fd):
        for step in range(len(entries) + 3):
            with unchanged_descriptors(root_fd):
                iterator = rfs.walk(root_fd)
                injected = KeyboardInterrupt("throw") if action == "throw-keyboard" else RuntimeError("throw")
                try:
                    for _ in range(step):
                        next(iterator, None)
                    if action == "close":
                        iterator.close()
                    else:
                        with pytest.raises(type(injected)) as caught:
                            iterator.throw(injected)
                        assert caught.value is injected
                finally:
                    iterator.close()


@pytest.mark.parametrize("action", ["close", "throw"])
def test_walk_cleanup_interruption_closes_remaining_children(monkeypatch, action):
    injected = KeyboardInterrupt("during cleanup")
    with tree(DEEP) as (_, root_fd), unchanged_descriptors(root_fd):
        opened = []
        original_open = rfs.open_dir
        def open_dir(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened.append(fd)
            return fd
        close = os.close
        fired = False
        def interrupted_close(fd):
            nonlocal fired
            close(fd)
            if not fired and fd in opened:
                fired = True
                raise injected
        with monkeypatch.context() as patch:
            patch.setattr(rfs, "open_dir", open_dir)
            iterator = rfs.walk(root_fd)
            for _ in range(4):
                next(iterator)
            patch.setattr(os, "close", interrupted_close)
            with pytest.raises(KeyboardInterrupt) as caught:
                if action == "close":
                    iterator.close()
                else:
                    iterator.throw(RuntimeError("caller"))
            assert caught.value is injected
            assert fired


@pytest.mark.parametrize("reason", ["mount-point", "too-deep", "changed", "swapped", "unreadable",
                                   "open-missing", "open-notdir", "open-unreadable"])
def test_walk_treeerror_parity(monkeypatch, reason):
    answers = []
    for module in (baseline(), rfs):
        with tree(DEEP) as (_, root_fd):
            before = open_descriptors()
            with monkeypatch.context() as patch:
                if reason == "too-deep":
                    patch.setattr(module, "MAX_DEPTH", 2)
                elif reason.startswith("open-"):
                    original = module.open_dir
                    def fail_open(name, *args, **kwargs):
                        if name == "b":
                            raise {"open-missing": FileNotFoundError(errno.ENOENT, "review"),
                                   "open-notdir": NotADirectoryError(errno.ENOTDIR, "review"),
                                   "open-unreadable": PermissionError(errno.EACCES, "review")}[reason]
                        return original(name, *args, **kwargs)
                    patch.setattr(module, "open_dir", fail_open)
                elif reason == "unreadable":
                    original = module._names
                    calls = 0
                    def names(fd):
                        nonlocal calls
                        calls += 1
                        if calls == 3:
                            raise PermissionError(errno.EACCES, "review")
                        return original(fd)
                    patch.setattr(module, "_names", names)
                elif reason == "swapped":
                    original = os.fstat
                    def fstat(fd):
                        st = original(fd)
                        if fd != root_fd:
                            values = list(st)
                            values[1] += 1
                            return os.stat_result(values)
                        return st
                    patch.setattr(os, "fstat", fstat)
                else:
                    original = os.stat
                    def stat(name, *args, **kwargs):
                        st = original(name, *args, **kwargs)
                        if name == "leaf":
                            if reason == "changed":
                                raise FileNotFoundError(errno.ENOENT, "review")
                            values = list(st)
                            values[2] += 1
                            return os.stat_result(values)
                        return st
                    patch.setattr(os, "stat", stat)
                with pytest.raises(module.TreeError) as caught:
                    list(module.walk(root_fd))
                answers.append(result(caught.value))
            after = open_descriptors()
            try:
                if module is rfs:
                    assert after == before
                os.fstat(root_fd)
            finally:
                for fd in after - before:
                    os.close(fd)
    assert answers[0] == answers[1]


def test_layout_allocation_before_open():
    """Inject an allocation failure at BUILD_MAP for the local children dict."""
    injected = MemoryError("children allocation")
    instructions = list(dis.get_instructions(rfs._layout))
    target = next(ins.offset for i, ins in enumerate(instructions)
                  if ins.opname == "BUILD_MAP" and instructions[i + 1].argval == "children")
    with tree(DEEP) as (_, root_fd), unchanged_descriptors(root_fd):
        st = os.stat("a", dir_fd=root_fd, follow_symlinks=False)
        with raise_at(rfs._layout.__code__, target, injected) as fired:
            with pytest.raises(MemoryError) as caught:
                rfs._layout(root_fd, "a", st, rfs._VENV)
        assert caught.value is injected
        assert fired


def test_walk_stack_handoff_failure_closes_child():
    injected = MemoryError("walk stack registration")
    target = [ins.offset for ins in dis.get_instructions(rfs.walk)
              if ins.opname in ("LOAD_ATTR", "LOAD_METHOD") and ins.argval == "append"][-1]
    with tree(DEEP) as (_, root_fd), unchanged_descriptors(root_fd):
        with raise_at(rfs.walk.__code__, target, injected) as fired:
            with pytest.raises(MemoryError) as caught:
                list(rfs.walk(root_fd))
        assert caught.value is injected
        assert fired


def test_in_repository_registration_failure_closes_descriptor():
    injected = MemoryError("registration")
    target = next(ins.offset for ins in dis.get_instructions(rfs.RegenerableWalk._in_repository)
                  if ins.opname in ("LOAD_ATTR", "LOAD_METHOD") and ins.argval == "append")
    with tree(DEEP) as (_, root_fd), unchanged_descriptors(root_fd):
        walker = rfs.RegenerableWalk(root_fd, lambda _: True)
        with raise_at(rfs.RegenerableWalk._in_repository.__code__, target, injected) as fired:
            with pytest.raises(MemoryError) as caught:
                walker._in_repository("a/b")
        assert caught.value is injected
        assert fired


def test_repository_cleanup_interruption_closes_remaining_children(monkeypatch):
    injected = KeyboardInterrupt("during repository cleanup")
    with tree(DEEP) as (_, root_fd), unchanged_descriptors(root_fd):
        opened = []
        original_open = rfs.open_dir
        def open_dir(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened.append(fd)
            return fd
        close = os.close
        fired = False
        def interrupted_close(fd):
            nonlocal fired
            close(fd)
            if not fired and fd in opened:
                fired = True
                raise injected
        with monkeypatch.context() as patch:
            patch.setattr(rfs, "open_dir", open_dir)
            patch.setattr(os, "close", interrupted_close)
            walker = rfs.RegenerableWalk(root_fd, lambda _: True)
            with pytest.raises(KeyboardInterrupt) as caught:
                walker._in_repository("a/b")
            assert caught.value is injected
            assert fired


@pytest.mark.parametrize("site", ["path", "conflict", "set-aside"])
@pytest.mark.parametrize("closed_first", [False, True])
def test_descent_close_failure_owns_new_child(monkeypatch, site, closed_first):
    injected = KeyboardInterrupt("handoff close")
    with tree(DEEP) as (root, root_fd), unchanged_descriptors(root_fd):
        close = os.close
        opened = []
        original_open = rfs.open_dir
        def open_dir(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened.append(fd)
            return fd
        fired = False
        def fail_close(fd):
            nonlocal fired
            if not fired and len(opened) == 2 and fd == opened[0]:
                fired = True
                if closed_first:
                    close(fd)
                raise injected
            return close(fd)
        reclaim = rfs.Reclaim({}, root / "conflicts", "label")
        with monkeypatch.context() as patch:
            patch.setattr(rfs, "open_dir", open_dir)
            patch.setattr(os, "close", fail_close)
            with pytest.raises(KeyboardInterrupt) as caught:
                if site == "path":
                    rfs._open_path(root_fd, "a/b")
                elif site == "conflict":
                    reclaim._conflict_dir("nested")
                else:
                    reclaim._set_aside_fd(root_fd, "a", "a", "new")
            assert fired
            assert caught.value is injected


@pytest.mark.parametrize("site", ["remove", "reclaim"])
@pytest.mark.parametrize("failure", ["stat", "chmod", "list"])
def test_recursive_owners_close_after_child_failure(monkeypatch, site, failure):
    injected = KeyboardInterrupt("recursive child")
    with tree(DEEP) as (root, root_fd), unchanged_descriptors(root_fd):
        entries = {rel: {"sig": rfs.signature(st)} for rel, st, _, _ in rfs.walk(root_fd)}
        opened = set()
        original_open = rfs.open_dir
        def open_dir(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened.add(fd)
            return fd
        operation = {"stat": "stat", "chmod": "fchmod", "list": "listdir"}[failure]
        original = getattr(os, operation)
        def fail(fd, *args, **kwargs):
            if fd in opened or kwargs.get("dir_fd") in opened:
                raise injected
            return original(fd, *args, **kwargs)
        if site == "reclaim" and failure == "chmod":
            os.chmod("a", 0o500, dir_fd=root_fd)
        with monkeypatch.context() as patch:
            patch.setattr(rfs, "open_dir", open_dir)
            patch.setattr(os, operation, fail)
            with pytest.raises(KeyboardInterrupt) as caught:
                if site == "remove":
                    rfs._remove_children(root_fd)
                else:
                    rfs.Reclaim(entries, root / "unused", "x")._one(root_fd, "", "a")
            assert caught.value is injected
