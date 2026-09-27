"""C-2.5: `daemon.log` is rotated by size, and every writer of the log lands
in the current file.

The installed daemon's log reached 68 MB without ever rotating (2026-09-27).
The log is written through the logging handler (whose stream the C-3.6 SIGUSR1
dump shares), the descriptor `watch_stop` opens for the C-5.8a dump, and
stdout and stderr when launchd points them at the log. These cases rotate
under all of them, under concurrent writers, and through failures, and check
that the readers (`tail`, `read_since`) follow across rotations.
"""

from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import daemonlog
from subfleet.daemonlog import DaemonLog, Mark, backup_path


REPO = Path(__file__).resolve().parents[2]


def files(path: Path) -> list[Path]:
    """The log and its rotated files, oldest first."""
    kept = [backup_path(path, n) for n in range(daemonlog.MAX_BACKUPS, 0, -1)
            if backup_path(path, n).exists()]
    return kept + [path]


def everything(path: Path) -> bytes:
    return b"".join(name.read_bytes() for name in files(path))


def open_files() -> set[tuple[int, int]]:
    """The (device, inode) of every file this process holds open."""
    held = set()
    for name in os.listdir("/dev/fd"):
        try:
            info = os.fstat(int(name))
        except OSError:
            continue
        held.add((info.st_dev, info.st_ino))
    return held


def write(log: DaemonLog, text: str) -> None:
    log.stream.write(text)
    log.stream.flush()


@pytest.fixture
def log(tmp_path):
    made = DaemonLog(tmp_path / "daemon.log")
    made.configure(max_bytes=100, backups=3, check_s=60)
    yield made
    made.close()


# --- rotation --------------------------------------------------------------------

def test_a_log_under_the_limit_is_left_alone(log):
    write(log, "x" * 99)
    assert log.check() is None
    assert not backup_path(log.path, 1).exists()


def test_a_log_at_the_limit_becomes_dot_one_and_a_new_log_takes_the_name(log):
    write(log, "a" * 100)
    before = os.stat(log.path)
    stream = log.stream
    assert log.check() == "rotated"
    assert log.stream is not stream and not stream.closed          # kept for a dump under way
    rotated = os.stat(backup_path(log.path, 1))
    assert (rotated.st_dev, rotated.st_ino) == (before.st_dev, before.st_ino)
    assert backup_path(log.path, 1).read_text() == "a" * 100
    fresh = os.stat(log.path)
    assert fresh.st_size == 0 and stat.S_IMODE(fresh.st_mode) & 0o077 == 0     # 0600 or tighter
    write(log, "after\n")
    assert log.path.read_text() == "after\n"                                  # the handler follows
    assert not log.path.with_name("daemon.log.new").exists()


def test_rotated_files_age_out_and_are_never_more_than_backups(log):
    for n in range(7):
        write(log, f"{n}" * 100)
        assert log.check() == "rotated"
    assert [name.name for name in files(log.path)] == ["daemon.log.3", "daemon.log.2", "daemon.log.1", "daemon.log"]
    assert [backup_path(log.path, n).read_text()[0] for n in (3, 2, 1)] == ["4", "5", "6"]
    assert log.rotations == 7


def test_a_policy_that_keeps_fewer_removes_the_extra_files(log):
    for n in range(4):
        write(log, f"{n}" * 100)
        log.check()
    log.configure(max_bytes=100, backups=1, check_s=60)
    write(log, "z" * 100)
    assert log.check() == "rotated"
    assert [name.name for name in files(log.path)] == ["daemon.log.1", "daemon.log"]
    assert backup_path(log.path, 1).read_text() == "z" * 100


def test_max_bytes_zero_never_rotates(log):
    log.configure(max_bytes=0, backups=3, check_s=60)
    write(log, "y" * 10_000)
    assert log.check() is None
    assert log.path.stat().st_size == 10_000


def test_a_log_behind_a_symlink_is_the_operators_and_is_not_rotated(tmp_path):
    real = tmp_path / "elsewhere.log"
    real.touch()
    (tmp_path / "daemon.log").symlink_to(real)
    made = DaemonLog(tmp_path / "daemon.log")
    made.configure(max_bytes=100, backups=3, check_s=60)
    try:
        write(made, "s" * 500)
        assert made.check() is None
        assert (tmp_path / "daemon.log").is_symlink() and real.stat().st_size == 500
    finally:
        made.close()


def test_a_replaced_stream_is_closed_a_minute_on_and_a_deleted_logs_space_with_it(log, monkeypatch):
    """A dump that began on the old stream finishes; a deleted log is not held
    open for longer than `REPLACED_KEEP_S` (then the next look closes it)."""
    first = log.stream
    write(log, "1" * 100)
    log.check()
    second = log.stream
    assert log.check() is None and not first.closed               # a look within the minute
    log._replaced = [(when - daemonlog.REPLACED_KEEP_S, stream) for when, stream in log._replaced]
    assert log.check() is None and first.closed and not second.closed
    deleted = {(info.st_dev, info.st_ino) for info in (os.stat(backup_path(log.path, 1)), os.stat(log.path))}
    backup_path(log.path, 1).unlink()
    log.path.unlink()                                             # an operator deletes the log
    assert log.check() == "reopened"
    assert deleted & open_files()                                 # the replaced stream, for a minute
    log._replaced = [(when - daemonlog.REPLACED_KEEP_S, stream) for when, stream in log._replaced]
    log.check()
    assert second.closed and not deleted & open_files()          # no descriptor holds their space
    log.close()
    assert log.stream.closed


def test_nothing_is_rotated_once_the_daemon_is_stopping(log):
    stopping = threading.Event()
    log._stopping = stopping
    stopping.set()
    write(log, "s" * 500)
    assert log.check() is None and not backup_path(log.path, 1).exists()


def test_a_leftover_new_file_from_a_rotation_that_died_is_replaced(log):
    leftover = log.path.with_name("daemon.log.new")
    leftover.write_text("half a rotation")
    write(log, "b" * 100)
    assert log.check() == "rotated"
    assert not leftover.exists() and log.path.stat().st_size == 0


# --- every writer follows --------------------------------------------------------

def test_a_follower_descriptor_follows_and_one_closed_without_release_is_never_touched(tmp_path, log):
    follower = daemonlog.open_follower(log.path)
    os.write(follower, b"f" * 100)
    assert log.check() == "rotated"
    os.write(follower, b"after\n")
    assert log.path.read_bytes() == b"after\n"
    # Closed without `release`, its number reused for another file: rotation
    # must not move that file's descriptor onto the log.
    os.close(follower)
    other = tmp_path / "unrelated"
    reused = os.open(other, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        write(log, "c" * 100)
        assert log.check() == "rotated"
        os.write(reused, b"mine\n")
        assert other.read_bytes() == b"mine\n"
        assert reused not in daemonlog._followers.get(daemonlog._key(log.path), set())
    finally:
        os.close(reused)


def test_release_takes_a_follower_out_of_rotation(log):
    follower = daemonlog.open_follower(log.path)
    daemonlog.release(log.path, follower)
    assert follower not in daemonlog._followers.get(daemonlog._key(log.path), set())


CHILD = r'''
import faulthandler, os, signal, sys, threading, time
from pathlib import Path
from subfleet.daemonlog import DaemonLog
from subfleet.daemon import watch_stop

def dumps_into(stream):
    # C-3.6 as the daemon registers it, and again on each new stream (C-2.5).
    faulthandler.register(signal.SIGUSR1, file=stream, all_threads=True, chain=False)

root = Path(sys.argv[1])
log = DaemonLog(root / "daemon.log", on_stream=dumps_into)
log.configure(max_bytes=1, backups=3, check_s=60)
dumps_into(log.stream)
arm = watch_stop(threading.Event(), 1.0, root / "daemon.log")      # C-5.8a's descriptor

def say(when):
    print(f"stdout {when}", flush=True)                          # launchd's StandardOutPath
    print(f"stderr {when}", file=sys.stderr, flush=True)          # and StandardErrorPath
    log.stream.write(f"handler {when}\n"); log.stream.flush()

say("before")
assert log.check() == "rotated"
say("after")

def parked_for_the_sigusr1_dump():
    os.kill(os.getpid(), signal.SIGUSR1)
    time.sleep(.2)

parked_for_the_sigusr1_dump()
arm()                                   # the stopping line, then the dump at 1 s and exit 1
assert log.check() == "rotated"         # between the two: the dump must follow

def parked_for_the_stop_dump():
    time.sleep(20)

parked_for_the_stop_dump()
'''


def test_the_sigusr1_dump_the_stop_dump_stdout_and_stderr_all_land_in_the_current_log(tmp_path):
    """C-2.5 with C-3.6 and C-5.8a: nothing is re-registered or reopened; the
    descriptors are moved, so each dump lands in the file current when it runs."""
    with open(tmp_path / "daemon.log", "ab") as out:           # as launchd and `daemon start` open it
        child = subprocess.run([sys.executable, "-c", CHILD, str(tmp_path)], cwd=REPO,
                               env={**os.environ, "PYTHONPATH": str(REPO), "PYTHON_GIL": "1"},
                               stdin=subprocess.DEVNULL, stdout=out, stderr=out, timeout=60)
    assert child.returncode == 1, (child.returncode, everything(tmp_path / "daemon.log"))  # the stop dump's exit
    first = backup_path(tmp_path / "daemon.log", 2).read_text(errors="replace")
    second = backup_path(tmp_path / "daemon.log", 1).read_text(errors="replace")
    current = (tmp_path / "daemon.log").read_text(errors="replace")
    assert all(f"{kind} before" in first for kind in ("stdout", "stderr", "handler"))
    assert all(f"{kind} after" in second for kind in ("stdout", "stderr", "handler"))
    assert "parked_for_the_sigusr1_dump" in second and "Thread 0x" in second
    assert "stopping: if this process is still running" in second
    assert "parked_for_the_stop_dump" in current and "Thread 0x" in current
    assert "after" not in first and "before" not in second and "parked_for_the_sigusr1_dump" not in current


def test_rotation_under_concurrent_writers_loses_and_repeats_nothing(tmp_path):
    """Each line logged by eight threads lands whole in exactly one file, in
    order per thread, through 25 rotations made while they write, and no
    write fails: the handler's stream is swapped under its lock, never moved
    under a writer with `dup2` (which fails concurrent writes with EBADF on
    macOS). The writers go on until the rotations are done, so a loaded
    machine changes how long this takes, not what it covers."""
    made = DaemonLog(tmp_path / "daemon.log")
    made.configure(max_bytes=4096, backups=daemonlog.MAX_BACKUPS, check_s=60)
    logger = logging.getLogger(f"test-rotation-{id(made)}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(made.handler)
    failures = []
    made.handler.handleError = failures.append
    enough, rotations, written = threading.Event(), [], [0] * 8
    deadline = time.monotonic() + 120

    def rotate():
        while len(rotations) < 25 and time.monotonic() < deadline:
            if made.check():
                rotations.append(1)
        enough.set()

    def log_lines(writer):
        n = 0
        while not enough.is_set() or n < 200:
            logger.info("w%d %06d %s", writer, n, "." * (n % 17))
            n += 1
        written[writer] = n

    rotator = threading.Thread(target=rotate)
    writers = [threading.Thread(target=log_lines, args=(w,)) for w in range(8)]
    for thread in writers:
        thread.start()
    rotator.start()
    for thread in writers + [rotator]:
        thread.join()
    try:
        assert failures == []
        assert len(rotations) == 25, f"{len(rotations)} rotations in 120 s"
        assert not backup_path(made.path, daemonlog.MAX_BACKUPS).exists(), "every rotated file is still kept"
        lines = everything(made.path).decode().splitlines()
        expected = [f"w{w} {n:06d} {'.' * (n % 17)}" for w in range(8) for n in range(written[w])]
        assert sorted(lines) == sorted(expected)                 # none lost, none twice, none torn
        for writer in range(8):
            numbers = [int(line.split()[1]) for line in lines if line.startswith(f"w{writer} ")]
            assert numbers == list(range(written[writer]))       # the files, oldest first, keep order
    finally:
        logger.removeHandler(made.handler)
        made.close()


# --- failures ---------------------------------------------------------------------

class Said(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[tuple[int, str]] = []

    def emit(self, record):
        self.lines.append((record.levelno, record.getMessage()))


def listening(log: DaemonLog) -> Said:
    said = Said()
    logger = logging.getLogger(f"test-said-{id(log)}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(said)
    log._log = logger
    return said


def test_a_failed_rotation_changes_nothing_the_writers_use_and_retries_delete_nothing_more(log, monkeypatch):
    for n in range(3):
        write(log, f"{n}" * 100)
        log.check()
    kept = {n: backup_path(log.path, n).read_text() for n in (1, 2, 3)}
    said = listening(log)
    real_rename = os.rename

    def rename(source, target):
        if Path(target) == log.path:
            raise OSError(errno.EIO, "injected")
        return real_rename(source, target)

    monkeypatch.setattr(daemonlog.os, "rename", rename)
    write(log, "q" * 100)
    before = os.stat(log.path)
    for _ in range(4):
        assert log.look() is None
    # The oldest went once (to make room), the rest moved up once, and `.1` is
    # free: each later retry found it free and deleted nothing more.
    assert not backup_path(log.path, 1).exists()
    assert {n: backup_path(log.path, n).read_text() for n in (2, 3)} == {2: kept[1], 3: kept[2]}
    write(log, "still\n")
    assert os.stat(log.path).st_ino == before.st_ino and log.path.read_text().endswith("still\n")
    assert len(said.lines) == 1 and "could not be rotated: OSError" in said.lines[0][1]   # said once
    monkeypatch.setattr(daemonlog.os, "rename", real_rename)
    assert log.look() == "rotated"
    assert backup_path(log.path, 1).read_text() == "q" * 100 + "still\n"
    assert {n: backup_path(log.path, n).read_text() for n in (2, 3)} == {2: kept[1], 3: kept[2]}
    assert said.lines[-1][0] == logging.INFO and "rotated at" in said.lines[-1][1]


def test_a_failure_after_the_handler_moved_never_closes_its_descriptor_and_a_follower_left_behind_catches_up(
        log, monkeypatch):
    """A step that fails once the handler writes the new file (here, moving a
    follower) must not close the new file's descriptor: the handler owns it,
    and its number could be reused under it. The follower it left on the old
    file is recognised as the log's and moved at the next rotation."""
    follower = daemonlog.open_follower(log.path)
    real = os.dup2

    def failing(fd, fd2, inheritable=True):
        if fd2 == follower:
            raise OSError(errno.EIO, "injected")
        return real(fd, fd2, inheritable=inheritable)

    monkeypatch.setattr(daemonlog.os, "dup2", failing)
    write(log, "x" * 100)
    assert log.look() is None                                    # reported, not raised
    write(log, "after\n")                                         # the handler's descriptor is open
    assert log.path.read_text() == "after\n"
    monkeypatch.setattr(daemonlog.os, "dup2", real)
    write(log, "y" * 100)
    assert log.check() == "rotated"
    os.write(follower, b"follower\n")
    assert log.path.read_text() == "follower\n"
    daemonlog.release(log.path, follower)


def test_a_full_volume_gets_the_oldest_file_back_before_anything_is_created(log, monkeypatch):
    for n in range(3):
        write(log, f"{n}" * 100)
        log.check()

    def full(self):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(DaemonLog, "_fresh", full)
    write(log, "w" * 100)
    assert log.look() is None and log.look() is None
    assert not backup_path(log.path, 3).exists()                 # the oldest, once
    assert backup_path(log.path, 2).read_text() == "1" * 100 and backup_path(log.path, 1).read_text() == "2" * 100


def test_a_rotation_failure_is_said_at_most_every_ten_minutes_with_the_count(log, monkeypatch):
    said = listening(log)

    def failing(self):
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(DaemonLog, "_fresh", failing)
    write(log, "e" * 100)
    for _ in range(3):
        log.look()
    log._trouble[0] -= daemonlog.TROUBLE_EVERY_S                # ten minutes on
    log.look()
    assert [text.split(";")[0] for _, text in said.lines] == [
        "daemon.log could not be rotated: OSError: [Errno 5] io",
        "daemon.log could not be rotated: OSError: [Errno 5] io (2 more since the last report)"]


# --- a log moved or deleted under the daemon ----------------------------------------

def test_a_deleted_log_is_replaced_and_its_space_freed(log):
    follower = daemonlog.open_follower(log.path)
    write(log, "old\n")
    log.path.unlink()
    assert log.check() == "reopened"
    write(log, "handler\n")
    os.write(follower, b"follower\n")
    assert log.path.read_text() == "handler\nfollower\n"
    assert stat.S_IMODE(log.path.stat().st_mode) & 0o077 == 0


def test_a_regular_file_now_at_the_name_is_written_to(log):
    log.path.rename(log.path.with_name("moved-away"))
    log.path.write_text("operator's\n")
    assert log.check() == "reopened"
    write(log, "mine\n")
    assert log.path.read_text() == "operator's\nmine\n"
    assert log.check() is None


def test_a_fifo_at_the_name_is_never_written_to(log):
    write(log, "kept\n")
    moved = log.path.with_name("moved-away")
    log.path.rename(moved)
    os.mkfifo(log.path)
    said = listening(log)
    assert log.look() is None                                    # open(O_NONBLOCK) on a FIFO: ENXIO, never a hang
    write(log, "still mine\n")
    assert moved.read_text() == "kept\nstill mine\n"
    assert "could not be rotated" in said.lines[0][1]


def test_close_never_closes_a_descriptor_a_rotation_may_still_move(log, monkeypatch):
    monkeypatch.setattr(daemonlog, "CLOSE_WAIT_S", .05)
    fd = log.fd
    with log._lock:
        log.close()
    os.fstat(fd)                                                 # still open: not closed under a rotation
    log.close()
    with pytest.raises(OSError):
        os.fstat(fd)
    assert log.check() is None


# --- readers ------------------------------------------------------------------------

def test_tail_reaches_into_rotated_files_and_reads_from_the_end(log):
    write(log, "".join(f"old {n}\n" for n in range(20)))
    log.check()
    write(log, "new 0\nnew 1\n")
    lines, at = daemonlog.tail(log.path, 4)
    assert lines == ["old 18", "old 19", "new 0", "new 1"]
    info = log.path.stat()
    assert at == Mark(info.st_dev, info.st_ino, info.st_size)
    assert daemonlog.tail(log.path, 0) == ([], at)
    assert daemonlog.tail(log.path.with_name("absent.log"), 5) == ([], None)


def test_read_since_follows_a_mark_through_several_rotations(log):
    write(log, "before\n")
    at = daemonlog.mark(log.path)
    write(log, "one " + "1" * 100 + "\n")
    log.check()
    write(log, "two " + "2" * 100 + "\n")
    log.check()
    write(log, "three\n")
    data, after = daemonlog.read_since(log.path, at)
    assert data == ("one " + "1" * 100 + "\ntwo " + "2" * 100 + "\nthree\n").encode()
    assert daemonlog.read_since(log.path, after) == (b"", after)


def test_read_since_a_truncated_or_aged_out_mark_or_no_mark(log):
    write(log, "abcdef\n")
    at = daemonlog.mark(log.path)
    os.truncate(log.path, 0)
    write(log, "xy\n")
    assert daemonlog.read_since(log.path, at)[0] == b"xy\n"      # smaller than the mark: from its start
    gone = Mark(at.dev, at.ino + 10_000_000, 3)
    write(log, "z" * 100)
    log.check()
    write(log, "last\n")
    assert daemonlog.read_since(log.path, gone)[0] == everything(log.path)
    assert daemonlog.read_since(log.path, None)[0] == b"last\n"


def test_readers_never_block_on_a_fifo_at_a_rotated_files_name(log):
    write(log, "line\n")
    os.mkfifo(backup_path(log.path, 1))
    assert daemonlog.tail(log.path, 10)[0] == ["line"]
    assert daemonlog.read_since(log.path, Mark(0, 0, 0))[0] == b"line\n"


# --- the model --------------------------------------------------------------------

@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(max_bytes=st.integers(1, 200), backups=st.integers(1, 4),
       steps=st.lists(st.one_of(
           st.tuples(st.just("log"), st.text(st.characters(min_codepoint=33, max_codepoint=126), max_size=60)),
           st.tuples(st.just("raw"), st.binary(min_size=1, max_size=60).map(lambda b: b.replace(b"\n", b"") + b"\n")),
           st.tuples(st.just("check"), st.none()),
           st.tuples(st.just("mark"), st.none())), max_size=60),
       tails=st.integers(0, 30))
def test_the_kept_files_are_always_a_suffix_of_what_was_written(max_bytes, backups, steps, tails):
    """For any writes and looks: the kept files, oldest first, are exactly the
    last bytes written (nothing lost, repeated or reordered in them); there
    are never more than `backups` rotated files and the name always exists; a
    file is rotated only once it reached the limit; and the readers agree with
    reading every kept file whole (`read_since` from any mark still kept,
    `tail` for any line count)."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "daemon.log"
        made = DaemonLog(path)
        made.configure(max_bytes=max_bytes, backups=backups, check_s=60)
        follower = daemonlog.open_follower(path)
        written, marks = b"", []
        try:
            for op, value in steps:
                if op == "log":
                    write(made, value + "\n")
                    written += (value + "\n").encode()
                elif op == "raw":
                    os.write(follower, value)
                    written += value
                elif op == "check":
                    size = path.stat().st_size
                    result = made.check()
                    assert result == ("rotated" if size >= max_bytes else None)
                    if result:
                        assert backup_path(path, 1).stat().st_size == size
                else:
                    marks.append((daemonlog.mark(path), len(written)))
                assert path.exists()
                kept = everything(path)
                assert written.endswith(kept) and len(kept) >= path.stat().st_size
                assert not backup_path(path, backups + 1).exists()
                for n in range(1, backups + 1):
                    if backup_path(path, n).exists():
                        assert backup_path(path, n).stat().st_size >= max_bytes
            kept = everything(path)
            for at, position in marks:
                data, _ = daemonlog.read_since(path, at)
                if len(written) - position <= len(kept):
                    assert data == written[position:]
                else:
                    assert data == kept                            # the marked file aged out
            expected = kept.decode(errors="replace").splitlines()
            assert daemonlog.tail(path, tails)[0] == (expected[-tails:] if tails else [])
        finally:
            daemonlog.release(path, follower)
            made.close()
