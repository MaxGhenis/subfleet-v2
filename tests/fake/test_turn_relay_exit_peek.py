"""C-26.4, #271 r4: the relay learns of its child's exit from the kernel, without reaping it.

The guardian's blocking `child.wait()` holds Popen's `_waitpid_lock` across `waitpid`,
and while it is held `poll()` answers None without asking the kernel, even for a
child that has exited (review r3's P2-1). `relay.child_exited` then peeks with
`waitid(P_PID, pid, WEXITED | WNOHANG | WNOWAIT)`: through `os.waitid` where `os` has
it, else libc's own `waitid` by ctypes, since CPython 3.12 leaves `os.waitid` out of
its macOS build. Both bindings are tested where they run: the ctypes one on every
Python, the `os` one where `os.waitid` exists.

Every child here is a `/bin/sh` started by the test and reaped by it, at most four
live at once. A child blocks reading its gate (its stdin, or fd 3 once it has closed
stdin) until the test opens it, so before then it is provably alive.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import errno
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from hypothesis import given, settings, strategies as st
import pytest

from subfleet import relay
from subfleet.relay import RelayServer, frame_sha256

HAS_OS_WAITID = hasattr(os, "waitid")
NO_OS_WAITID = "CPython 3.12 leaves os.waitid out of its macOS build: the relay peeks through libc there"
BINDINGS = [pytest.param("libc", id="libc-waitid", marks=pytest.mark.skipif(
                sys.platform != "darwin", reason="the ctypes binding reads Darwin's siginfo_t")),
            pytest.param("os", id="os-waitid", marks=pytest.mark.skipif(not HAS_OS_WAITID, reason=NO_OS_WAITID))]
# What a child's end looks like to Popen's wait (`returncode`) and to the kernel's
# siginfo (`si_status`: the exit code, or the signal that ended it).
ENDS = {"exit 5": (5, 5), "kill -9 $$": (-9, 9)}
TIMEOUT_S = 30.0


def binding(name: str):
    return relay._waitid_libc if name == "libc" else relay._waitid_os


class Gate:
    """The write end of a child's gate. Opening it lets the child read a line and end;
    it is closed once, so no later close can reach a descriptor number reused since."""

    def __init__(self, fd: int):
        self.fd: int | None = fd

    def open(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            try:
                os.write(fd, b"\n")
            except OSError:
                pass
            finally:
                os.close(fd)


def start(end: str = "exit 0", *, closed_stdin: bool = False) -> tuple[subprocess.Popen, Gate]:
    """A child that ends with `end` once its gate opens. With `closed_stdin` it moves
    the gate to fd 3 and closes its stdin first."""
    read_end, write_end = os.pipe()
    script = f"exec 3<&0 0<&-; read _ <&3; {end}" if closed_stdin else f"read _; {end}"
    try:
        # The session's own environment: its PATH puts the test recorder before the
        # system directories, as conftest's exec guard requires of a shell.
        child = subprocess.Popen(["/bin/sh", "-c", script], stdin=read_end)
    except BaseException:
        os.close(write_end)
        raise
    finally:
        os.close(read_end)
    return child, Gate(write_end)


def until(check, what: str) -> None:
    deadline = time.monotonic() + TIMEOUT_S
    while not check():
        assert time.monotonic() < deadline, what
        time.sleep(0.005)


@contextmanager
def wait_lock_held(child):
    """Hold the lock the guardian's blocking `child.wait()` holds (`guardian.py`)."""
    child._waitpid_lock.acquire()
    try:
        yield
    finally:
        child._waitpid_lock.release()


def reap(child: subprocess.Popen) -> None:
    if child.returncode is None:
        try:
            child.kill()
        except OSError:
            pass
        child.wait(TIMEOUT_S)


def exists(pid: int) -> bool:
    """Whether `pid` is still in the process table (a zombie is; a reaped child is not)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_the_binding_is_chosen_once_by_the_interpreter():
    assert relay.EXIT_PEEK == ("os.waitid" if HAS_OS_WAITID else "libc waitid")
    assert relay._exit_peek is (relay._waitid_os if HAS_OS_WAITID else relay._waitid_libc)
    print("EXIT_PEEK", relay.EXIT_PEEK)


@pytest.mark.parametrize("name", BINDINGS)
@pytest.mark.parametrize("end", list(ENDS))
def test_a_binding_reads_an_exit_without_reaping_it(name, end):
    peek = binding(name)
    returncode, si_status = ENDS[end]
    child, gate = start(end)
    try:
        assert peek(child.pid) is None                   # still blocked on its gate
        gate.open()
        until(lambda: peek(child.pid) is not None, "the child never exited")
        assert exists(child.pid)                         # the peek that read the exit left a zombie
        for _ in range(2):                               # and left the exit for the next one
            seen = peek(child.pid)
            assert (seen.si_pid, seen.si_status) == (child.pid, si_status)
        assert child.wait(TIMEOUT_S) == returncode       # the guardian's wait records the real status
        assert not exists(child.pid)
        with pytest.raises(ChildProcessError):           # reaped: ECHILD
            peek(child.pid)
    finally:
        gate.open()
        reap(child)


@pytest.mark.parametrize("name", BINDINGS)
@pytest.mark.parametrize("lock", ["lock-free", "wait-lock-held"])
def test_live_children_never_read_as_exited(monkeypatch, name, lock):
    monkeypatch.setattr(relay, "_exit_peek", binding(name))
    running, running_gate = start()
    closed, closed_gate = start(closed_stdin=True)
    try:
        time.sleep(0.2)                                  # the second child has closed its stdin
        for child in (running, closed):
            with wait_lock_held(child) if lock == "wait-lock-held" else _nothing():
                assert not relay.child_exited(child), child.args
            assert child.returncode is None
    finally:
        for child, gate in ((running, running_gate), (closed, closed_gate)):
            gate.open()
            reap(child)


@contextmanager
def _nothing():
    yield


@pytest.mark.parametrize("name", BINDINGS)
def test_a_peek_never_changes_the_status_the_guardian_records(monkeypatch, name):
    monkeypatch.setattr(relay, "_exit_peek", binding(name))
    child, gate = start("exit 3")
    try:
        with wait_lock_held(child):
            assert not relay.child_exited(child)
            gate.open()
            until(lambda: relay.child_exited(child), "the helper never read the exit")
            assert child.poll() is None                  # Popen's cache cannot see it: its lock is held
            assert relay.child_exited(child) and relay.child_exited(child)
            assert exists(child.pid) and child.returncode is None
        assert child.wait(TIMEOUT_S) == 3
    finally:
        gate.open()
        reap(child)


@pytest.mark.parametrize("name", BINDINGS)
def test_a_child_the_guardian_reaped_reads_as_exited(monkeypatch, name):
    monkeypatch.setattr(relay, "_exit_peek", binding(name))
    child, gate = start("exit 4")
    try:
        with wait_lock_held(child):
            gate.open()
            until(lambda: relay._exit_peek(child.pid) is not None, "the child never exited")
            assert os.waitpid(child.pid, 0) == (child.pid, 4 << 8)   # the reap the guardian's wait makes
            assert child.poll() is None and child.returncode is None
            assert relay.child_exited(child)             # ECHILD: reaped, so it has exited
        assert child.poll() == 0                         # Popen's own ECHILD fallback, once its lock is free
    finally:
        gate.open()
        reap(child)


@pytest.mark.parametrize("failure", [errno.EINVAL, errno.EIO, errno.EPERM])
def test_another_peek_failure_proves_nothing_and_is_logged_once(monkeypatch, capfd, failure):
    def failing(pid):
        raise OSError(failure, os.strerror(failure))

    monkeypatch.setattr(relay, "_exit_peek", failing)
    monkeypatch.setattr(relay, "_peek_failure_logged", False)
    child, gate = start("exit 6")
    try:
        with wait_lock_held(child):
            assert not relay.child_exited(child)         # alive
            gate.open()
            until(lambda: binding("libc")(child.pid) is not None, "the child never exited")
            assert not relay.child_exited(child)         # exited, but the failed peek proves nothing
        lines = [line for line in capfd.readouterr().err.splitlines() if line.startswith("relay exit peek:")]
        assert lines == [relay.PEEK_FAILURE_LOG_LINE.format(errno=errno.errorcode[failure])]
        assert child.wait(TIMEOUT_S) == 6
    finally:
        gate.open()
        reap(child)


@pytest.mark.parametrize("fields", [
    {},                                                  # the wall tests' fakes: poll (and send_signal) only
    {"send_signal": lambda sig: None},
    {"returncode": None},                                # the relay-exit fakes: returncode and poll
    {"returncode": None, "pid": "1"},
    {"returncode": None, "pid": 0},
], ids=["poll-only", "poll-and-signal", "returncode", "text-pid", "pid-zero"])
def test_a_child_without_a_real_pid_is_never_peeked(monkeypatch, fields):
    monkeypatch.setattr(relay, "_exit_peek", lambda pid: pytest.fail(f"peeked {pid!r}"))
    child = SimpleNamespace(poll=lambda: None, **fields)
    assert not relay.child_exited(child)
    child.poll = lambda: 1
    assert relay.child_exited(child)


def _server(tmp_path, child):
    """The relay as the fakes give it a child: no socket, a real pipe and log."""
    server = RelayServer(tmp_path / "unused.sock", tmp_path / "stdin.jsonl")
    read_end, server._pipe = os.pipe()
    server._child = child
    return server, read_end


def _frame(seq: int, op: str, line: str | None = None, sig: str | None = None) -> dict:
    return {"seq": seq, "op": op, "line": line, "sig": sig, "tag": op, "sha256": frame_sha256(op, line, sig)}


@pytest.mark.parametrize("name", BINDINGS)
def test_every_relay_site_reads_an_exit_the_guardian_wait_hides(tmp_path, monkeypatch, name):
    """The status, a refusal's exit flag and signal delivery each see the exit."""
    monkeypatch.setattr(relay, "_exit_peek", binding(name))
    child, gate = start("exit 1")
    server, read_end = _server(tmp_path, child)
    try:
        with wait_lock_held(child):
            gate.open()
            until(lambda: binding(name)(child.pid) is not None, "the child never exited")
            os.close(read_end)
            read_end = None
            assert child.poll() is None
            assert server.status()["status"]["child"] == "exited"
            refused = server.apply(_frame(1, "write", "{}"))
            assert refused == {"seq": 1, "ok": False, "error": "failed", "errno": errno.EPIPE,
                               "child_exited": True}
            # Not delivered: an exited child is no longer the guardian's to signal.
            assert server.apply(_frame(2, "signal", sig="INT")) == {"seq": 2, "ok": False, "error": "no-child"}
            assert exists(child.pid)
        assert child.wait(TIMEOUT_S) == 1
    finally:
        if read_end is not None:
            os.close(read_end)
        server._close_pipe()
        gate.open()
        reap(child)


@pytest.mark.parametrize("name", BINDINGS)
def test_every_relay_site_reads_a_live_child_that_closed_stdin_as_running(tmp_path, monkeypatch, name):
    monkeypatch.setattr(relay, "_exit_peek", binding(name))
    child, gate = start("exit 0", closed_stdin=True)
    server, read_end = _server(tmp_path, child)
    os.close(read_end)                                   # nothing reads stdin: a write gets EPIPE
    try:
        time.sleep(0.2)
        with wait_lock_held(child):
            assert server.status()["status"]["child"] == "running"
            refused = server.apply(_frame(1, "write", "{}"))
            assert refused == {"seq": 1, "ok": False, "error": "failed", "errno": errno.EPIPE}
            assert server.apply(_frame(2, "signal", sig="INT")) == {"seq": 2, "ok": True}
        assert child.wait(TIMEOUT_S) == -2               # the delivered SIGINT ended it
    finally:
        server._close_pipe()
        gate.open()
        reap(child)


# The property. A child ends with a random exit code or signal. Its wait lock is free;
# held (the guardian's wait between taking the lock and reaping); held while the test,
# as that wait, reaps the child once its exit is read and before Popen stores the status
# (the ECHILD window); or held by a real guardian thread blocked in `child.wait()`. Its
# gate opens before the peeks begin or, from another thread, while they run.
END = st.one_of(st.integers(0, 255).map(lambda n: f"exit {n}"),
                st.sampled_from(["kill -9 $$", "kill -15 $$", "kill -2 $$"]))
CHILD = st.fixed_dictionaries({
    "end": END,
    "lock": st.sampled_from(["free", "held", "reaped-unpublished", "guardian-waiting"]),
    "opens": st.sampled_from(["before-the-peeks", "during-the-peeks"]),
    "delay": st.sampled_from([0.0, 0.001, 0.005, 0.02]),
    "shut_peeks": st.integers(0, 3),
})


def _returncode(end: str) -> int:
    word, value = end.split()[:2]
    return int(value) if word == "exit" else -int(value[1:])


def _play(children, name, seen):
    with pytest.MonkeyPatch.context() as patch:
        peek = binding(name)

        def counted(pid):
            try:
                found = peek(pid)
            except ChildProcessError:
                seen["peek-echild"] += 1
                raise
            seen["peek-exited" if found is not None else "peek-running"] += 1
            return found

        patch.setattr(relay, "_exit_peek", counted)
        live, opener = [], None
        try:
            for spec in children:
                child, gate = start(spec["end"])
                live.append(SimpleNamespace(spec=spec, child=child, gate=gate, held=False, waiter=None,
                                            waited=[], opened_at=None, readings=[], reaped=None))
            for x in live:
                if x.spec["lock"] in ("held", "reaped-unpublished"):
                    x.child._waitpid_lock.acquire()
                    x.held = True
                elif x.spec["lock"] == "guardian-waiting":
                    x.waiter = threading.Thread(target=lambda x=x: x.waited.append(x.child.wait()), daemon=True)
                    x.waiter.start()
                    until(x.child._waitpid_lock.locked, "the guardian thread never entered its wait")
            for x in live:                               # every gate is shut: every child is alive
                for _ in range(x.spec["shut_peeks"]):
                    assert not relay.child_exited(x.child), x.spec
                    seen["shut-reads"] += 1

            def open_x(x):
                time.sleep(x.spec["delay"])
                x.opened_at = time.monotonic()
                x.gate.open()

            during = [x for x in live if x.spec["opens"] == "during-the-peeks"]
            for x in live:
                if x.spec["opens"] == "before-the-peeks":
                    open_x(x)
            opener = threading.Thread(target=lambda: [open_x(x) for x in during], daemon=True)
            opener.start()
            deadline = time.monotonic() + TIMEOUT_S
            pending = list(live)
            while pending:
                assert time.monotonic() < deadline, "a child's exit was never read"
                for x in list(pending):
                    began = time.monotonic()
                    exited = relay.child_exited(x.child)
                    x.readings.append((began, time.monotonic(), exited))
                    if exited:
                        pending.remove(x)
                        if x.spec["lock"] == "reaped-unpublished":
                            # The guardian's wait reaps, as Popen.wait does under its lock;
                            # the peeks below then meet ECHILD.
                            try:
                                x.reaped = os.waitpid(x.child.pid, 0)
                            except ChildProcessError:
                                raise AssertionError(f"a peek reaped the child before the wait: {x.spec}") from None
                        for _ in range(2):               # once read, an exit stays read
                            x.readings.append((time.monotonic(), time.monotonic(), relay.child_exited(x.child)))
                time.sleep(0.001)                        # interleaves with the opener without spinning a core
            opener.join(TIMEOUT_S)
            for x in live:
                assert x.opened_at is not None
                results = [exited for _, _, exited in x.readings]
                first = results.index(True)
                assert all(results[first:]), (x.spec, results)
                # Nothing read an exit before the gate began to open: the child was alive.
                assert not any(exited for _, ended, exited in x.readings if ended < x.opened_at), x.spec
                seen["reads-before-open"] += sum(1 for _, ended, _ in x.readings if ended < x.opened_at)
                seen["reads"] += len(results)
                if x.spec["lock"] == "reaped-unpublished":
                    pid, status = x.reaped
                    assert pid == x.child.pid and x.child.returncode is None and not exists(pid), x.spec
                    x.child._handle_exitstatus(status)   # what the guardian's wait stores next
                    x.child._waitpid_lock.release()
                    x.held = False
                    seen["reaped-then-read"] += 1
                elif x.held:
                    # Nothing else may reap while the lock is held: the exit was the kernel's,
                    # and the child is still a zombie after every peek.
                    assert x.child.returncode is None and exists(x.child.pid), x.spec
                    seen["never-reap-checked-under-held-lock"] += 1
                    seen["never-reap-checked-nonzero-status"] += _returncode(x.spec["end"]) != 0
                    x.child._waitpid_lock.release()
                    x.held = False
                if x.waiter is not None:
                    x.waiter.join(TIMEOUT_S)
                    assert x.waited == [_returncode(x.spec["end"])], x.spec
                else:
                    assert x.child.wait(TIMEOUT_S) == _returncode(x.spec["end"]), x.spec
                seen[f"lock-{x.spec['lock']}"] += 1
                seen[f"opens-{x.spec['opens']}"] += 1
                seen["signalled" if x.spec["end"].startswith("kill") else "exit-code"] += 1
                seen["children"] += 1
            seen[f"binding-{name}"] += 1
            seen[f"live-{len(live)}"] += 1
            seen["examples"] += 1
        finally:
            if opener is not None:
                opener.join(TIMEOUT_S)               # it opens no gate after the ones closed below
            for x in live:
                if x.held:
                    x.child._waitpid_lock.release()
                x.gate.open()
                if x.waiter is not None:
                    x.waiter.join(TIMEOUT_S)
                reap(x.child)


@pytest.mark.parametrize("name", BINDINGS)
def test_a_peek_reads_exit_only_after_it_and_never_reaps(name):
    seen = Counter()

    @settings(max_examples=60, deadline=None, derandomize=True, database=None)
    @given(children=st.lists(CHILD, min_size=1, max_size=4))
    def check(children):
        _play(children, name, seen)

    check()
    print(f"exit peek ({name}) exercised:", json.dumps(dict(sorted(seen.items()))))
    assert seen["peek-exited"] and seen["peek-echild"], dict(seen)
    assert seen["shut-reads"] and seen["reads-before-open"], dict(seen)
    assert all(seen[f"lock-{lock}"] for lock in ("free", "held", "reaped-unpublished", "guardian-waiting")), dict(seen)
    assert seen["never-reap-checked-nonzero-status"], dict(seen)
