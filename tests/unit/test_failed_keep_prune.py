"""Kept roots do not pile up without bound (D-KR1, #238 review r1 finding 6).

At session start, before any test runs, the harness removes `<ROOT>/<pid>` folders whose process is gone
(`os.kill(pid, 0)` finds none) and in which nothing has been kept for more than 24 hours (the folder's own
mtime). Invariants, for every folder:
- a live process's folder is never removed, nor the current process's, however old;
- a folder younger than 24 hours is never removed;
- only a real directory whose name is a pid is removed (not a file, a symlink, `007`, `0` or `١٢`);
- every folder that meets the rule is removed, exactly once even with several pruners at once.
Every test here prunes a scratch folder, never the real one.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import subprocess
import threading
import time

from hypothesis import example, given, settings, strategies as st
import pytest

from tests import failed_keep

pytest_plugins = ["pytester"]

DAY_NS = 24 * 60 * 60 * 10**9


def dead_pid() -> int:
    """A pid no process has: a child's, after it has exited and been reaped."""
    for _ in range(20):
        child = subprocess.Popen(["/usr/bin/true"])
        child.wait()
        try:
            os.kill(child.pid, 0)
        except ProcessLookupError:
            return child.pid
    pytest.fail("every reaped child's pid was taken again at once")


def kept_root(root: Path, name: str, *, age_ns: int, now_ns: int) -> Path:
    """A pid folder holding one kept test root, last written `age_ns` before `now_ns`."""
    folder = root / name
    (folder / "test_x").mkdir(parents=True)
    (folder / "test_x" / "daemon.log").write_text("evidence\n")
    os.utime(folder, ns=(now_ns - age_ns, now_ns - age_ns))
    return folder


def graves(root: Path) -> list[str]:
    return sorted(path.name for path in root.iterdir() if not path.name[:1].isdigit())


def test_the_rule_is_a_day():
    assert failed_keep.PRUNE_AFTER_S == 24 * 60 * 60


def test_a_dead_process_folder_older_than_a_day_is_removed(tmp_path):
    now = time.time_ns()
    pid = dead_pid()
    folder = kept_root(tmp_path, str(pid), age_ns=DAY_NS + 60 * 10**9, now_ns=now)
    assert failed_keep.prune(tmp_path, now_ns=now) == [folder]
    assert list(tmp_path.iterdir()) == []


def test_a_live_process_folder_is_kept_however_old(tmp_path):
    now = time.time_ns()
    child = subprocess.Popen(["/bin/sleep", "300"])
    try:
        live = [kept_root(tmp_path, str(pid), age_ns=30 * DAY_NS, now_ns=now)
                for pid in (child.pid, os.getpid(), os.getppid())]
        assert failed_keep.prune(tmp_path, now_ns=now) == []
        assert all((folder / "test_x" / "daemon.log").read_text() == "evidence\n" for folder in live)
    finally:
        child.kill()
        child.wait()


def test_a_dead_process_folder_younger_than_a_day_is_kept(tmp_path):
    now = time.time_ns()
    first = dead_pid()
    second = next(pid for pid in iter(dead_pid, None) if pid != first)
    fresh = kept_root(tmp_path, str(first), age_ns=60 * 10**9, now_ns=now)
    exactly = kept_root(tmp_path, str(second), age_ns=DAY_NS, now_ns=now)
    assert failed_keep.prune(tmp_path, now_ns=now) == []
    assert fresh.is_dir() and exactly.is_dir()


def test_only_a_directory_named_by_a_pid_is_pruned(tmp_path):
    now = time.time_ns()
    pid = dead_pid()
    names = ["notes", "0", "007", "12x", "-5", "١٢", str(2**31), "99999999999", ".pruning-1-2-x"]
    others = [kept_root(tmp_path, name, age_ns=30 * DAY_NS, now_ns=now) for name in names]
    target = tmp_path.parent / f"{tmp_path.name}-target"
    target.mkdir()
    (target / "daemon.log").write_text("not ours\n")
    link = tmp_path / str(pid)
    link.symlink_to(target)
    os.utime(link, ns=(now - 30 * DAY_NS, now - 30 * DAY_NS), follow_symlinks=False)
    plain = tmp_path / str(next(other for other in iter(dead_pid, None) if other != pid))
    plain.write_text("a file, not a folder\n")
    os.utime(plain, ns=(now - 30 * DAY_NS, now - 30 * DAY_NS))
    assert failed_keep.prune(tmp_path, now_ns=now) == []
    assert all(folder.is_dir() for folder in others)
    assert link.is_symlink() and (target / "daemon.log").read_text() == "not ours\n"
    assert plain.read_text() == "a file, not a folder\n"


def test_a_missing_root_prunes_nothing(tmp_path):
    assert failed_keep.prune(tmp_path / "absent") == []


# A model of the rule, over generated folders: what the fake kill says of each pid, its age at the 24 h
# boundary and around it, and what kind of entry it is.

STATES = {
    "alive": None,
    "dead": ProcessLookupError(errno.ESRCH, "No such process"),
    "not ours": PermissionError(errno.EPERM, "Operation not permitted"),
    "unknown": OSError(errno.EINVAL, "Invalid argument"),
}
names = st.one_of(st.integers(1, 2**31 - 1).map(str), st.just(str(os.getpid())),
                  st.sampled_from(["0", "007", "-5", "12x", "notes", "１２", str(2**31), "1" * 12]))
ages = st.one_of(st.sampled_from([DAY_NS + 1, 30 * DAY_NS, DAY_NS, DAY_NS - 1, 0, -60 * 10**9]),
                st.integers(-DAY_NS, 3 * DAY_NS))
entries = st.lists(st.tuples(names, st.sampled_from(["dir", "file", "symlink"]), ages, st.sampled_from(sorted(STATES))),
                   max_size=8, unique_by=lambda entry: entry[0])


def is_pid(name: str) -> bool:
    return name.isascii() and name.isdigit() and not name.startswith("0") and int(name) < 2**31


# Derandomized draws favour simple values (young ages, short lists), so every answer kill can give for an
# old folder, the boundary, this process and each kind of entry are given explicitly as well.
EVERY_ANSWER = [("101", "dir", DAY_NS + 1, "alive"), ("102", "dir", 2 * DAY_NS, "dead"),
                ("103", "dir", 2 * DAY_NS, "not ours"), ("104", "dir", 2 * DAY_NS, "unknown"),
                ("105", "dir", DAY_NS, "dead"), ("106", "dir", DAY_NS + 1, "dead"),
                (str(os.getpid()), "dir", 2 * DAY_NS, "dead"), ("107", "symlink", 2 * DAY_NS, "dead"),
                ("108", "file", 2 * DAY_NS, "dead"), ("0109", "dir", 2 * DAY_NS, "dead")]


@settings(max_examples=40, derandomize=True, database=None, deadline=None)
@given(entries)
@example(EVERY_ANSWER)
def test_pruning_removes_exactly_the_folders_the_rule_names(tmp_path_factory, spec):
    root = tmp_path_factory.mktemp("prune") / "kept"
    root.mkdir()
    now = time.time_ns()
    target = root.parent / "target"
    target.mkdir()
    states = {}
    for name, kind, age, state in spec:
        path = root / name
        if kind == "dir":
            kept_root(root, name, age_ns=age, now_ns=now)
        elif kind == "file":
            path.write_text("a file\n")
        else:
            path.symlink_to(target)
        os.utime(path, ns=(now - age, now - age), follow_symlinks=False)
        if is_pid(name):
            states[int(name)] = state

    def kill(pid, signal):
        assert signal == 0 and pid in states, (pid, signal)
        if STATES[states[pid]] is not None:
            raise STATES[states[pid]]

    expected = {root / name for name, kind, age, state in spec
                if is_pid(name) and kind == "dir" and age > DAY_NS and state == "dead" and int(name) != os.getpid()}
    removed = failed_keep.prune(root, now_ns=now, kill=kill)
    assert len(removed) == len(set(removed)) and set(removed) == expected
    assert {root / name for name, *_ in spec} - expected == set(root.iterdir())
    assert graves(root) == sorted(name for name, *_ in spec if not name[:1].isdigit())
    assert failed_keep.prune(root, now_ns=now, kill=kill) == []      # nothing is left to prune


def test_a_folder_written_while_it_is_claimed_is_put_back(tmp_path):
    """A new process that took the pid and kept a root between the check and the claim keeps its folder."""
    now = time.time_ns()
    folder = kept_root(tmp_path, "4242", age_ns=2 * DAY_NS, now_ns=now)

    def kill(pid, signal):
        if not (folder / "test_new").exists():
            (folder / "test_new").mkdir()       # the folder's mtime is now fresh
        raise ProcessLookupError(errno.ESRCH, "No such process")

    assert failed_keep.prune(tmp_path, now_ns=now, kill=kill) == []
    assert sorted(path.name for path in folder.iterdir()) == ["test_new", "test_x"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["4242"]


def test_a_pid_that_comes_alive_while_its_folder_is_claimed_keeps_it(tmp_path):
    now = time.time_ns()
    folder = kept_root(tmp_path, "4242", age_ns=2 * DAY_NS, now_ns=now)
    calls = []

    def kill(pid, signal):
        calls.append(pid)
        if len(calls) == 1:
            raise ProcessLookupError(errno.ESRCH, "No such process")

    assert failed_keep.prune(tmp_path, now_ns=now, kill=kill) == []
    assert calls == [4242, 4242]
    assert (folder / "test_x" / "daemon.log").read_text() == "evidence\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["4242"]


def test_pruners_at_once_remove_each_folder_exactly_once(tmp_path):
    """Every xdist worker prunes at its own start: a folder is claimed by one of them."""
    now = time.time_ns()
    folders = {kept_root(tmp_path, str(1_000_000 + index), age_ns=2 * DAY_NS, now_ns=now) for index in range(40)}

    def dead(pid, signal):
        raise ProcessLookupError(errno.ESRCH, "No such process")

    removed: list[list[Path]] = []
    start = threading.Barrier(4)

    def prune():
        start.wait()
        removed.append(failed_keep.prune(tmp_path, now_ns=now, kill=dead))
    threads = [threading.Thread(target=prune) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    every = [path for each in removed for path in each]
    assert len(every) == len(set(every)) == len(folders) and set(every) == folders
    assert list(tmp_path.iterdir()) == []


def test_the_session_prunes_before_any_test_runs(pytester, monkeypatch, tmp_path):
    root = tmp_path / "kept"
    monkeypatch.setattr(failed_keep, "ROOT", root)
    now = time.time_ns()
    old = kept_root(root, str(dead_pid()), age_ns=2 * DAY_NS, now_ns=now)
    mine = kept_root(root, str(os.getpid()), age_ns=2 * DAY_NS, now_ns=now)
    pytester.makeconftest("from tests.conftest import pytest_sessionstart  # noqa: F401  the hook under test\n")
    pytester.makepyfile(test_scratch_prune=f'''
from pathlib import Path

def test_the_old_root_is_gone_already():
    assert not Path({str(old)!r}).exists()
    assert Path({str(mine)!r}).is_dir()
''')
    run = pytester.inline_run("-p", "no:cacheprovider", "-q")
    assert run.ret == 0
    run.assertoutcome(passed=1)
