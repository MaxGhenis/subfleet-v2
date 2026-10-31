"""D-WALK-FD1: descriptor ownership survives every walk exit and failed handoff."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import resource
import tempfile

from hypothesis import example, given, settings, strategies as st
import pytest

from subfleet import retention_fs as rfs


class Interrupted(BaseException):
    """Exercise cleanup beyond Exception (like cancellation or GeneratorExit)."""


def open_descriptors() -> set[int]:
    """Snapshot this process only, excluding listdir's already closed descriptor."""
    result = set()
    directory = "/proc/self/fd" if Path("/proc/self/fd").is_dir() else "/dev/fd"
    for name in os.listdir(directory):
        if not name.isdigit():
            continue
        fd = int(name)
        try:
            fcntl.fcntl(fd, fcntl.F_GETFD)
        except OSError as exc:
            assert exc.errno == errno.EBADF
        else:
            result.add(fd)
    return result


@contextmanager
def unchanged_descriptors(root_fd: int | None = None):
    before = open_descriptors()
    try:
        yield
    finally:
        after = open_descriptors()
        try:
            assert after == before, f"leaked={after - before}, closed={before - after}"
            if root_fd is not None:
                os.fstat(root_fd)
        finally:
            # Red and mutant runs must not poison later examples or pytest.
            for fd in after - before:
                os.close(fd)


@contextmanager
def tree(entries):
    with tempfile.TemporaryDirectory(prefix="walk-descriptors-") as temporary:
        root = Path(temporary)
        for rel, kind in entries:
            path = root / rel
            if kind == "d":
                path.mkdir()
            elif kind == "l":
                path.symlink_to("missing")
            else:
                path.write_bytes(b"scratch")
        root_fd = rfs.open_dir(root)
        try:
            yield root, root_fd
        finally:
            os.close(root_fd)


@st.composite
def small_trees(draw):
    directories = [""]
    entries = []
    for n in range(draw(st.integers(min_value=1, max_value=12))):
        parent = draw(st.sampled_from(directories))
        rel = rfs.join(parent, f"n{n}")
        kind = draw(st.sampled_from(["d", "f", "l"]))
        entries.append((rel, kind))
        if kind == "d":
            directories.append(rel)
    return entries


DEEP = [("a", "d"), ("a/b", "d"), ("a/b/leaf", "f")]


@pytest.mark.parametrize("free", [1, 2, 3, 4])
def test_hub_low_soft_limit_reproduction(free):
    """The hub's 256-descriptor, three-folder reproduction, in this process."""
    entries = [(rel, kind) for n in range(3)
               for rel, kind in [(f"d{n}", "d"), (f"d{n}/x", "d"), (f"d{n}/x/f", "f")]]
    with tree(entries) as (_, root_fd), unchanged_descriptors(root_fd):
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        before = open_descriptors()
        # Keep room for pytest's existing descriptors even on a busy runner.
        target = max(256, max(before, default=0) + 64)
        if hard != resource.RLIM_INFINITY and target > hard:
            pytest.skip("hard descriptor bound leaves insufficient pytest headroom")
        fillers = []
        failure = None
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            try:
                while True:
                    fillers.append(os.open(os.devnull, os.O_RDONLY))
            except OSError as exc:
                assert exc.errno == errno.EMFILE
            for _ in range(free):
                os.close(fillers.pop())
            try:
                list(rfs.walk(root_fd))
            except OSError as exc:
                failure = exc
        finally:
            for fd in fillers:
                os.close(fd)
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        if free <= 2:
            assert failure is not None and failure.errno == errno.EMFILE
        else:
            assert failure is None


@settings(max_examples=100, deadline=None)
@example(entries=DEEP, operation="listing", offset=1, error="os")
@example(entries=DEEP, operation="listing", offset=2, error="interrupt")
@example(entries=DEEP, operation="stat", offset=2, error="runtime")
@given(entries=small_trees(), operation=st.sampled_from(["listing", "stat"]),
       offset=st.integers(min_value=0, max_value=30),
       error=st.sampled_from(["os", "runtime", "interrupt", "tree"]))
def test_walk_failure_preserves_descriptors_and_exception(entries, operation, offset, error):
    total = 1 + sum(kind == "d" for _, kind in entries) if operation == "listing" else len(entries)
    kth = offset % total + 1
    injected = {"os": OSError(errno.EIO, "injected"), "runtime": RuntimeError("injected"),
                "interrupt": Interrupted("injected"), "tree": rfs.TreeError("injected")}[error]
    with tree(entries) as (_, root_fd), unchanged_descriptors(root_fd):
        with pytest.MonkeyPatch.context() as patch:
            original = rfs._names if operation == "listing" else os.stat
            calls = 0

            def fail_kth(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == kth:
                    raise injected
                return original(*args, **kwargs)

            patch.setattr(rfs if operation == "listing" else os,
                          "_names" if operation == "listing" else "stat", fail_kth)
            with pytest.raises(type(injected)) as caught:
                list(rfs.walk(root_fd))
            assert caught.value is injected
            assert calls == kth


@settings(max_examples=60, deadline=None)
@example(entries=DEEP, step=3)
@example(entries=DEEP, step=4)
@example(entries=[], step=1)
@given(entries=small_trees(), step=st.integers(min_value=0, max_value=30))
def test_walk_early_close_and_normal_end_preserve_descriptors(entries, step):
    with tree(entries) as (_, root_fd):
        with unchanged_descriptors(root_fd):
            iterator = rfs.walk(root_fd)
            try:
                for _ in range(step % (len(entries) + 2)):
                    next(iterator)
            finally:
                iterator.close()
        with unchanged_descriptors(root_fd):
            assert len(list(rfs.walk(root_fd))) == len(entries) + 1


@pytest.mark.parametrize("failure", ["check", "mount-point", "too-deep", "changed", "unreadable", "swapped"])
def test_walk_other_exits_preserve_descriptors(monkeypatch, failure):
    with tree(DEEP) as (_, root_fd), unchanged_descriptors(root_fd):
        check = None
        with monkeypatch.context() as patch:
            if failure == "check":
                calls = 0
                injected = Interrupted("check")

                def check():
                    nonlocal calls
                    calls += 1
                    if calls == 3:
                        raise injected
            elif failure == "too-deep":
                patch.setattr(rfs, "MAX_DEPTH", 2)
            elif failure == "swapped":
                original = os.fstat

                def swapped(fd):
                    found = original(fd)
                    if fd != root_fd:
                        values = list(found)
                        values[1] += 1
                        return os.stat_result(values)
                    return found

                patch.setattr(os, "fstat", swapped)
            elif failure == "unreadable":
                original = rfs._names
                calls = 0

                def unreadable(fd):
                    nonlocal calls
                    calls += 1
                    if calls == 3:
                        raise PermissionError(errno.EACCES, "injected")
                    return original(fd)

                patch.setattr(rfs, "_names", unreadable)
            else:
                original = os.stat

                def changed(name, *args, **kwargs):
                    if name == "leaf":
                        if failure == "changed":
                            raise FileNotFoundError(errno.ENOENT, "injected")
                        values = list(original(name, *args, **kwargs))
                        values[2] += 1
                        return os.stat_result(values)
                    return original(name, *args, **kwargs)

                patch.setattr(os, "stat", changed)
            with pytest.raises(Interrupted if failure == "check" else rfs.TreeError) as caught:
                list(rfs.walk(root_fd, check))
            if failure == "check":
                assert caught.value is injected
            else:
                assert caught.value.reason == failure


@pytest.mark.parametrize("error", [OSError(errno.EIO, "fstat"), Interrupted("fstat")])
def test_open_dir_failed_validation_closes_descriptor(monkeypatch, error):
    with tree(DEEP) as (root, root_fd), unchanged_descriptors(root_fd):
        expected = os.stat("a", dir_fd=root_fd, follow_symlinks=False)
        with monkeypatch.context() as patch:
            def fail(_fd):
                raise error
            patch.setattr(os, "fstat", fail)
            with pytest.raises(type(error)) as caught:
                rfs.open_dir(root / "a", expect=expected)
            assert caught.value is error


@pytest.mark.parametrize("path", ["a/missing", "a/b/missing"])
def test_open_path_failed_descent_closes_intermediate_descriptors(path):
    with tree(DEEP) as (_, root_fd), unchanged_descriptors(root_fd):
        with pytest.raises(FileNotFoundError):
            rfs._open_path(root_fd, path)


@pytest.mark.parametrize("operation", ["mkdir", "open"])
@pytest.mark.parametrize("error", [OSError(errno.EIO, "conflict"), Interrupted("conflict")])
def test_conflict_dir_failed_descent_closes_descriptor(monkeypatch, operation, error):
    with tree(DEEP) as (root, root_fd), unchanged_descriptors(root_fd):
        reclaim = rfs.Reclaim({}, root / "conflicts", "label")
        with monkeypatch.context() as patch:
            original = os.mkdir if operation == "mkdir" else rfs.open_dir

            def fail(part, *args, **kwargs):
                if part == "nested":
                    raise error
                return original(part, *args, **kwargs)

            patch.setattr(os if operation == "mkdir" else rfs,
                          "mkdir" if operation == "mkdir" else "open_dir", fail)
            with pytest.raises(type(error)) as caught:
                reclaim._conflict_dir("nested")
            assert caught.value is error
