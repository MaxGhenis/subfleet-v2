"""Review #271 r4: the kernel exit peek (`relay.child_exited`) against real children.

Every child is a `/bin/sh` started here and reaped here, by a thread blocked in
`child.wait()` as the guardian's is (`guardian.py:295`) or by the test's own wait,
with at most two live at once. These characterize the helper at `c8904e4b2`: Popen's
`poll()` still reaps when its lock is free, a peek never meets EINTR, a failed libc
call or a platform with neither binding proves nothing, a reused pid reads by whose
child holds it, and a real guardian wait publishes the exit before any peek sees it.
The exit-in-progress timing inputs are in `docs/reports/2026-10-29-relay-exit-r4/`.
"""

from __future__ import annotations

from collections import Counter
import ctypes
import errno
import json
import os
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from subfleet import relay
from subfleet.relay import RelayServer
from tests.fake import test_turn_relay_exit_peek as peek


def _until(check, what: str) -> None:
    """`peek.until` without `time.sleep`, which the runner fixture replaces with its clock."""
    pause, deadline = threading.Event(), time.monotonic() + peek.TIMEOUT_S
    while not check():
        assert time.monotonic() < deadline, what
        pause.wait(0.001)


def _guardian(child):
    """A thread blocked in `child.wait()`, holding Popen's wait lock as the guardian's does."""
    waited = []
    thread = threading.Thread(target=lambda: waited.append(child.wait()), daemon=True)
    thread.start()
    _until(child._waitpid_lock.locked, "the guardian thread never entered its wait")
    return thread, waited


def _relay_for(tmp_path, child, gate):
    """The relay as `serve` leaves it: the child's stdin write end, non-blocking."""
    server = RelayServer(tmp_path / "unused.sock", tmp_path / "stdin.jsonl")
    server._pipe, gate.fd = gate.fd, None
    os.set_blocking(server._pipe, False)
    server._child = child
    return server


def test_the_relay_s_poll_reaps_and_stores_the_status_when_the_wait_lock_is_free(tmp_path):
    """C-26.4 and the PR say the guardian's wait stays the only reaper. `child_exited`
    keeps Popen's `poll()`, which reaps when Popen's lock is free (before the guardian's
    thread enters its wait, or in a relay with no guardian): what stays true is that the
    status it reaps is stored, so the guardian's wait still returns it."""
    child, gate = peek.start("exit 7")
    server = _relay_for(tmp_path, child, gate)
    try:
        assert server.apply(peek._frame(1, "write", "{}")) == {"seq": 1, "ok": True}
        peek.until(lambda: peek.binding("libc" if not peek.HAS_OS_WAITID else "os")(child.pid) is not None,
                   "the child never exited")
        assert peek.exists(child.pid)                    # a zombie, nobody has reaped it
        assert server.status()["status"]["child"] == "exited"
        assert not peek.exists(child.pid)                # the relay's status read reaped it, through poll()
        assert child.returncode == 7                     # and stored what it reaped
        assert child.wait(peek.TIMEOUT_S) == 7
    finally:
        server._close_pipe()
        peek.reap(child)


@pytest.mark.parametrize("name", peek.BINDINGS)
def test_a_peek_under_a_signal_storm_never_meets_eintr(monkeypatch, name):
    """WNOHANG never sleeps, so no signal can interrupt the peek: under a profiling timer
    firing every 200 µs of this process's CPU time (SIGPROF, so pytest-timeout's SIGALRM
    is left alone), neither binding raises, and each answers for a live child and then
    for its exit."""
    monkeypatch.setattr(relay, "_exit_peek", peek.binding(name))
    monkeypatch.setattr(relay, "_peek_failure_logged", False)
    fired = []
    previous = signal.signal(signal.SIGPROF, lambda *_: fired.append(1))
    child, gate = peek.start("exit 2")
    answers = Counter()
    try:
        signal.setitimer(signal.ITIMER_PROF, 0.0002, 0.0002)
        with peek.wait_lock_held(child):
            bound = time.monotonic() + 5
            while len(fired) < 20 and time.monotonic() < bound:
                for _ in range(500):
                    answers[relay.child_exited(child)] += 1
            assert set(answers) == {False} and len(fired) >= 5, (dict(answers), len(fired))
            gate.open()
            deadline = time.monotonic() + peek.TIMEOUT_S
            while not relay.child_exited(child):
                assert time.monotonic() < deadline
            for _ in range(3000):
                answers["after"] += relay.child_exited(child)
        signal.setitimer(signal.ITIMER_PROF, 0, 0)
        assert answers["after"] == 3000 and not relay._peek_failure_logged
        assert child.wait(peek.TIMEOUT_S) == 2
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0, 0)
        signal.signal(signal.SIGPROF, previous)
        gate.open()
        peek.reap(child)
    print(f"signal storm ({name}): {len(fired)} SIGPROF during {sum(answers.values())} reads")


@pytest.mark.skipif(sys.platform != "darwin", reason="the ctypes binding is Darwin's")
def test_an_interrupted_libc_peek_proves_nothing_and_is_logged_once(monkeypatch, capfd):
    """Were the libc call to fail with EINTR (it cannot with WNOHANG, above), the helper
    reads it as no proof, once logged; `os.waitid` would retry it itself (PEP 475)."""
    def interrupted(idtype, pid, info, options):
        ctypes.set_errno(errno.EINTR)
        return -1

    monkeypatch.setattr(relay, "_libc_waitid", interrupted)
    monkeypatch.setattr(relay, "_exit_peek", relay._waitid_libc)
    monkeypatch.setattr(relay, "_peek_failure_logged", False)
    child = SimpleNamespace(returncode=None, poll=lambda: None, pid=os.getpid())
    assert not relay.child_exited(child) and not relay.child_exited(child)
    lines = [line for line in capfd.readouterr().err.splitlines() if line.startswith("relay exit peek:")]
    assert lines == [relay.PEEK_FAILURE_LOG_LINE.format(errno="EINTR")]


def test_without_either_binding_the_relay_has_only_popen_s_evidence(monkeypatch, capfd):
    """Off macOS with no `os.waitid` (no platform CI or a lane runs), `_libc_waitid` is
    None and every peek fails ENOSYS: the relay is back to `poll()` alone, with one log
    line per guardian, and `EXIT_PEEK` still names the libc binding."""
    child, gate = peek.start("exit 9")
    try:
        with peek.wait_lock_held(child):
            gate.open()
            peek.until(lambda: relay._exit_peek(child.pid) is not None, "the child never exited")
            monkeypatch.setattr(relay, "_libc_waitid", None)
            monkeypatch.setattr(relay, "_exit_peek", relay._waitid_libc)
            monkeypatch.setattr(relay, "_peek_failure_logged", False)
            assert not relay.child_exited(child)         # exited, but nothing here can show it
        assert relay.child_exited(child)                 # the lock is free: poll() reaps and reads it
        assert child.returncode == 9
    finally:
        gate.open()
        peek.reap(child)
    lines = [line for line in capfd.readouterr().err.splitlines() if line.startswith("relay exit peek:")]
    assert lines == [relay.PEEK_FAILURE_LOG_LINE.format(errno="ENOSYS")]


def test_a_reused_pid_reads_by_whose_child_holds_it(monkeypatch):
    """After the guardian reaps, its pid can be another process's. A process that is not
    this one's child reads `exited` (ECHILD); another live child of this process would
    read `running`. The guardian has one child, so only the first can happen there."""
    other, other_gate = peek.start("exit 0")
    try:
        stranger = SimpleNamespace(returncode=None, poll=lambda: None, pid=os.getppid())
        sibling = SimpleNamespace(returncode=None, poll=lambda: None, pid=other.pid)
        assert relay.child_exited(stranger)              # ECHILD: not this process's child
        assert not relay.child_exited(sibling)           # a live child of this process at that pid
    finally:
        other_gate.open()
        peek.reap(other)


@pytest.mark.parametrize("name", peek.BINDINGS)
def test_peeks_across_a_real_guardian_wait(monkeypatch, name):
    """A thread in `child.wait()` and reads every 100 µs across the exit: nothing reads
    `exited` before the gate opens, every read after the first `exited` agrees, and the
    wait returns the real status. Counts which evidence answered."""
    sources = Counter()
    peek_fn = peek.binding(name)

    def counted(pid):
        try:
            found = peek_fn(pid)
        except ChildProcessError:
            sources["peek-echild"] += 1
            raise
        sources["peek-exited" if found else "peek-running"] += 1
        return found

    monkeypatch.setattr(relay, "_exit_peek", counted)
    for n in range(8):
        child, gate = peek.start(f"exit {n + 10}")
        thread, waited = _guardian(child)
        readings = []
        try:
            for _ in range(20):
                readings.append(relay.child_exited(child))
                time.sleep(0.0001)
            assert not any(readings)
            gate.open()
            deadline = time.monotonic() + peek.TIMEOUT_S
            while not readings[-1]:
                assert time.monotonic() < deadline
                before = Counter(sources)
                readings.append(relay.child_exited(child))
                if readings[-1]:
                    moved = [k for k in ("peek-exited", "peek-echild") if sources[k] > before[k]]
                    sources[f"first-exit-read-by-{moved[0] if moved else 'popen'}"] += 1
                time.sleep(0.0001)
            for _ in range(5):
                readings.append(relay.child_exited(child))
            first = readings.index(True)
            assert all(readings[first:])
            thread.join(peek.TIMEOUT_S)
            assert waited == [n + 10]
        finally:
            gate.open()
            thread.join(peek.TIMEOUT_S)
            peek.reap(child)
    print(f"guardian-wait reads ({name}):", json.dumps(dict(sorted(sources.items()))))
