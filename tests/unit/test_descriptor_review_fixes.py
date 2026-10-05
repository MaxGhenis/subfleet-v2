"""Regressions for the independent reviews of the descriptor reconciliation (3c8fe55).

Opus and GPT reviewed the release line's merge of PR #43 (both REQUEST CHANGES,
~/reviews/descriptor-reconcile-2026-09-27/review-{opus,astra}.md). Each test
below is a reviewer's reproduction, turned around to require the fixed
behaviour:

- C-15.5: a `wait` reads once more when its deadline passes, so a job that ended
  after the hub's last look is answered, not reported as a timeout (P0, both).
- C-16.7: a reply that fails part way ends the stream under the write lock, so no
  second reply joins the broken line (P1, both).
- C-3.6: a failed `__init__` clears `stack_dumps` from `daemon.lock` before it
  lets SIGUSR1 go (P0, GPT).
- C-16.7: a long poll takes busy as an empty poll, and a connect refused after a
  busy answer reports busy (P1, Opus; #55 on main).
- C-16.7: a request whose pool cannot start a thread does not end its reader,
  and `_end_stream` records only a connection still held (P2, Opus).
- C-16.7: every kind of malformed line is answered exit 2 and the connection
  goes on (#55 on main).
"""

from __future__ import annotations

import json
import socket
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest

from subfleet import client as client_module
from subfleet import daemon as daemon_module
from subfleet import descriptors, protocol
from subfleet.client import Client, DaemonError, DaemonUnavailable
from subfleet.daemon import Daemon

from test_daemon_connections import connect, counts, reply, send, serve, until  # noqa: F401 - serve: a fixture
from test_wait_hub import add_job, daemon  # noqa: F401 - daemon: a fixture


@pytest.fixture
def isolated_dumps():
    """C-3.6 state a test leaves behind stays with it: a daemon whose lock could not
    stop saying `stack_dumps` stays in `_ADVERTISED` for good, and a later test's
    daemon would hand SIGUSR1 to its stream (review of 4fc5b49, P2: it broke
    tests/unit/test_lockwatch.py's disposition check)."""
    import faulthandler
    import signal
    before = dict(daemon_module._ADVERTISED)
    yield
    for token in list(daemon_module._ADVERTISED):
        if token not in before:
            del daemon_module._ADVERTISED[token]
    if not daemon_module._ADVERTISED:
        faulthandler.unregister(signal.SIGUSR1)
        daemon_module._STACK_DUMPS = None
        daemon_module._HELD_STREAMS.clear()


# --- C-15.5: the last read when the deadline passes ----------------------------

def commit_elsewhere(core, job_id: str) -> None:
    """End a job through a connection of its own, which the store's generation does
    not count, so the hub does not see it until its recheck (an hour here)."""
    db = sqlite3.connect(core.root / "state.sqlite3", timeout=5)
    try:
        db.execute("UPDATE jobs SET state='succeeded',rc=0 WHERE job_id=?", (job_id,))
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("with_client", [False, True], ids=["no-client-check", "client-check"])
def test_c15_5_a_wait_reads_once_more_when_its_deadline_passes(daemon, with_client):
    """Review P0 (both reviewers): the waiter slept through to its deadline with no
    wake from the hub, skipped its read and answered `{"timeout": true}` for a job
    that had already succeeded; a CLI at its own deadline then exited 124."""
    daemon.wait_hub.recheck_s = 3600
    job = add_job(daemon, "20261004-000001-ended-unseen")
    first_read, real = threading.Event(), daemon._wait_answer

    def reading(job_ids):
        try:
            return real(job_ids)
        finally:
            first_read.set()
    daemon._wait_answer = reading
    result = {}
    waiter = threading.Thread(target=lambda: result.update(daemon.wait(
        protocol.WaitArgs(job_ids=[job], deadline_s=2),
        client_gone=(lambda: False) if with_client else None)))
    waiter.start()
    assert first_read.wait(5)                               # the waiter has read: still running
    commit_elsewhere(daemon, job)                           # unseen by the hub, well before the deadline
    waiter.join(10)
    assert not waiter.is_alive()
    assert result["timeout"] is False and result["jobs"][0]["state"] == "succeeded"


def test_c15_5_a_wait_still_times_out_when_nothing_ended(daemon):
    daemon.wait_hub.recheck_s = 3600
    job = add_job(daemon, "20261004-000002-still-running")
    started = time.monotonic()
    assert daemon.wait(protocol.WaitArgs(job_ids=[job], deadline_s=.3),
                       client_gone=lambda: False) == {"timeout": True}
    assert .25 < time.monotonic() - started < 2


# --- C-16.7: no reply after a broken one ---------------------------------------

class HalfWritten:
    """A socket whose first send writes part of its line and fails after a pause,
    as a client that stops reading makes a 60 s send fail; later sends succeed
    unless the stream was shut down. It records what happened, in order."""

    def __init__(self):
        self.events: list[str] = []
        self.inside = threading.Event()
        self.shut = False
        self._first = True

    def sendall(self, data: bytes) -> None:
        if self._first:
            self._first = False
            self.events.append("partial")
            self.inside.set()
            time.sleep(.2)
            raise TimeoutError("timed out")
        if self.shut:
            raise BrokenPipeError(32, "Broken pipe")
        self.events.append("second")

    def shutdown(self, how: int) -> None:
        self.shut = True


def test_c16_7_no_reply_joins_a_line_that_failed_part_way():
    """Review P1 (both): the first writer's lock was released before the stream was
    ended, so a second writer could append its reply to the broken line. With the
    end inside the lock, the second reply meets the shut stream instead."""
    conn, lock = HalfWritten(), threading.Lock()

    def end_stream(sock):
        time.sleep(.1)                       # as `_end_stream` may wait for `_connection_lock`
        sock.events.append("ended")
        sock.shutdown(socket.SHUT_RDWR)
    outcomes = {}
    first = threading.Thread(target=lambda: outcomes.__setitem__(
        "first", descriptors.send_reply(conn, lock, {"id": "first"}, end_stream=end_stream)))
    first.start()
    assert conn.inside.wait(5)
    second = threading.Thread(target=lambda: outcomes.__setitem__(
        "second", descriptors.send_reply(conn, lock, {"id": "second"}, end_stream=end_stream)))
    second.start()
    first.join(5)
    second.join(5)
    assert conn.events[:2] == ["partial", "ended"] and "second" not in conn.events
    assert outcomes == {"first": False, "second": False}


# --- C-3.6: a failed construction takes back what it advertised ----------------

def test_c3_6_a_failed_construction_clears_stack_dumps_before_it_lets_the_signal_go(monkeypatch, isolated_dumps):
    """Review P0 (GPT): built off the main thread, a daemon whose construction failed
    after it advertised `stack_dumps` unregistered the handler but left the flag, so
    `daemon stacks` would send SIGUSR1, default action fatal, to a living host."""
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fake-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fake-start")

    def broken(path):
        raise RuntimeError("policy unreadable")
    monkeypatch.setattr(daemon_module, "load_policy", broken)
    with tempfile.TemporaryDirectory(prefix="sfi-", dir="/tmp") as directory:
        root = Path(directory)
        raised = []

        def build():
            try:
                Daemon(root)
            except RuntimeError as exc:
                raised.append(exc)
        builder = threading.Thread(target=build)            # off the main thread, as reported
        builder.start()
        builder.join(10)
        assert raised and "policy unreadable" in str(raised[0])
        record = json.loads((root / "daemon.lock").read_text())
        assert "stack_dumps" not in record and record["pid"]
        assert daemon_module._STACK_DUMPS is None


# --- C-16.7: busy in a long poll, and a refusal behind busy ---------------------

class Scripted(Client):
    """Each send below `call` plays the next step of a script."""

    def __init__(self, root, *steps):
        super().__init__(root, verify_lock=False)
        self.steps, self.timeouts = list(steps), []

    def _call_once(self, op, args, *, request_id, timeout, stated):
        self.timeouts.append(timeout)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def busy() -> DaemonError:
    return DaemonError(69, "the daemon is busy: it holds 512 client connections, its limit",
                       "try again shortly")


def refused() -> DaemonUnavailable:
    """What `_call_once` raises when the connect is refused (a full listen backlog)."""
    try:
        raise ConnectionRefusedError(61, "Connection refused")
    except ConnectionRefusedError as cause:
        try:
            raise DaemonUnavailable(f"no daemon at daemon.sock: {cause}") from cause
        except DaemonUnavailable as exc:
            return exc


def socket_gone() -> DaemonUnavailable:
    """What `_call_once` raises once a stopped daemon has unlinked its socket."""
    try:
        raise FileNotFoundError(2, "No such file or directory")
    except FileNotFoundError as cause:
        try:
            raise DaemonUnavailable(f"no daemon at daemon.sock: {cause}") from cause
        except DaemonUnavailable as exc:
            return exc


def test_c16_7_a_connect_refused_after_busy_reports_busy_not_an_absent_daemon(tmp_path, monkeypatch):
    """A full listen backlog behind a busy daemon refuses the connect; that is not
    an absent daemon, and must not send a caller to offline mode (#55)."""
    monkeypatch.setattr(client_module, "_sleep", lambda seconds: None)
    client = Scripted(tmp_path, busy(), refused())
    with pytest.raises(DaemonError) as caught:
        client.call("list", timeout=10)
    assert caught.value.busy
    fresh = Scripted(tmp_path, refused())
    with pytest.raises(DaemonUnavailable):                   # with no busy answer first, it is absent
        fresh.call("list", timeout=10)
    # Review r3, P2: only a refused connect while the lock names a living daemon is busy.
    gone = Scripted(tmp_path, busy(), socket_gone())          # stopped: the socket was unlinked
    with pytest.raises(DaemonUnavailable):
        gone.call("list", timeout=10)
    dead = Scripted(tmp_path, busy(), refused())              # crashed: the lock names a dead process
    dead.lock_holder_alive = lambda: False
    with pytest.raises(DaemonUnavailable):
        dead.call("list", timeout=10)


def test_c16_7_a_call_can_take_busy_at_once(tmp_path):
    """`retry_busy=False` on one call: a long poll that loops takes busy as an empty
    poll, so its next poll has its whole deadline (review P1, Opus)."""
    client = Scripted(tmp_path, busy(), {"jobs": []})
    with pytest.raises(DaemonError) as caught:
        client.call("wait", {"job_ids": ["j"], "deadline_s": 60}, timeout=75, retry_busy=False)
    assert caught.value.busy and client.timeouts == [75]


def test_c16_7_the_cli_wait_loop_asks_without_retrying_inside_the_call(monkeypatch):
    """Each poll of `wait_jobs` is a whole 60 s poll with its 75 s budget: busy goes
    back to the loop. A retry inside `call` asked the daemon to hold a 60 s poll with
    55 s left, and the lost answer ended even an unbounded `wait` with exit 1."""
    from types import SimpleNamespace
    from subfleet import cli
    seen = []

    def call(op, args, **kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            raise busy()
        return {"jobs": [{"job_id": "20261004-000003-done", "state": "succeeded", "rc": 0}], "timeout": False}
    monkeypatch.setattr(cli, "_client", lambda *a, **k: SimpleNamespace(call=call))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    args = cli.build_parser().parse_args(["wait", "20261004-000003-done"])
    assert cli.wait_jobs(args, ["20261004-000003-done"], timeout=None, quiet=True) == 0
    assert len(seen) == 2 and all(kw.get("retry_busy") is False for kw in seen)
    assert all(kw["timeout"] == 75 for kw in seen)


def test_c16_7_the_hook_wait_takes_busy_as_an_empty_poll(monkeypatch):
    from subfleet import hooks
    delivered, calls = [], []

    class Hooked:
        def call(self, op, args, **kwargs):
            calls.append(kwargs)
            if len(calls) <= 2:
                raise busy()
            return {"jobs": [{"job_id": "j", "state": "succeeded"}], "timeout": False}
    monkeypatch.setattr(hooks, "_deliver", lambda client, session, job, stderr: delivered.append(job) or 0)
    clock = [0.0]
    assert hooks._wait_and_deliver(Hooked(), "s", "j", 100.0, stderr=None, now=lambda: clock[0],
                                   sleep=lambda s: clock.__setitem__(0, clock[0] + s)) == 0
    assert delivered and len(calls) == 3 and all(kw.get("retry_busy") is False for kw in calls)


# --- C-16.7: a pool with no thread to give, and `_end_stream`'s bookkeeping -----

class NoThread:
    """An executor that cannot start a thread for its first call, as CPython's
    raises RuntimeError from `submit` after queuing the work."""

    def __init__(self, real):
        self.real, self.failed = real, False

    def submit(self, fn, *args, **kwargs):
        if not self.failed:
            self.failed = True
            raise RuntimeError("can't start new thread")
        return self.real.submit(fn, *args, **kwargs)

    def shutdown(self, *args, **kwargs):
        return self.real.shutdown(*args, **kwargs)


def test_c16_7_a_pool_that_cannot_start_a_thread_does_not_end_the_reader(serve):
    """Review P2 (Opus): `_start_request` raising RuntimeError ended the reader with
    an unhandled exception; the connection's later requests went unread."""
    service = serve()
    service.requests = NoThread(service.requests)
    with connect(service) as sock:
        send(sock, "readings")                              # its pool cannot start a thread
        send(sock, "ping")                                  # answered on the same reader
        answer = reply(sock)
        assert answer["result"]["pong"] is True
    assert counts(service)["unscheduled"] == 1


def test_c16_7_end_stream_records_only_a_connection_still_held(serve):
    """Review P2 (Opus): a queued request a stalled pool ran after its reader had
    closed and released the socket added it to `_shut_down`, where nothing removed it."""
    service = serve()
    ours, theirs = socket.socketpair()
    with ours, theirs:
        service._end_stream(ours)                           # never held, or already let go
        assert not service._shut_down
        with service._connection_lock:
            service._connections.add(ours)
        service._end_stream(ours)
        assert ours in service._shut_down
        with service._connection_lock:
            service._connections.discard(ours)
            service._shut_down.discard(ours)


# --- C-16.7: malformed lines of every kind are answered ------------------------

@pytest.mark.parametrize("line", [
    b"[" * 100_000 + b"]" * 100_000,                        # nests past the parser
    b'{"v":1,"id":"x","op":"ping","args":{"n":' + b"9" * 5000 + b"}}",   # past int_max_str_digits
    b'{"v":1,"id":"\xff","op":"ping","args":{}}',            # not UTF-8
], ids=["deep", "long-integer", "not-utf8"])
def test_c16_7_a_malformed_line_is_answered_and_the_connection_goes_on(serve, line):
    service = serve()
    with connect(service) as sock:
        sock.sendall(line + b"\n")
        answer = reply(sock)
        assert answer["ok"] is False and answer["error"]["code"] == 2
        send(sock, "ping")
        assert reply(sock)["result"]["pong"] is True


# --- review round 2 (of 02c6320) -----------------------------------------------

def test_c15_5_a_wake_during_the_last_read_asks_for_one_more(daemon):
    """Review r2, P2: the pass's read began before the deadline, the job committed
    while it ran (its snapshot older) and the hub woke the waiter; the deadline had
    passed when it returned. The waiter must read again, not answer a timeout."""
    daemon.wait_hub.recheck_s = 3600
    job = add_job(daemon, "20261004-000004-ended-mid-read")
    real, calls = daemon._wait_answer, []

    def stale_then_real(job_ids):
        calls.append(time.monotonic())
        if len(calls) == 1:
            answer = real(job_ids)                          # the snapshot: still running
            time.sleep(.4)                                  # past the 0.3 s deadline
            commit_elsewhere(daemon, job)
            for waiter in list(daemon.wait_hub._waiters.values()):
                waiter.event.set()                          # the hub's wake, during this read
            return answer
        return real(job_ids)
    daemon._wait_answer = stale_then_real
    result = daemon.wait(protocol.WaitArgs(job_ids=[job], deadline_s=.3), client_gone=lambda: False)
    assert result["timeout"] is False and result["jobs"][0]["state"] == "succeeded" and len(calls) == 2


def test_c16_7_the_cli_wait_takes_a_connect_refused_after_busy_as_busy(monkeypatch):
    """Review r2, P1: with `retry_busy=False`, a refused connect after a busy answer
    reached `_daemon_down` and ended even an unbounded `subfleet wait` with 69 and
    "start the daemon". It is the busy daemon's full backlog: the loop asks again."""
    from types import SimpleNamespace
    from subfleet import cli
    steps = [busy(), refused(),
             {"jobs": [{"job_id": "20261004-000005-done", "state": "succeeded", "rc": 0}], "timeout": False}]

    def call(op, args, **kwargs):
        step = steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step
    monkeypatch.setattr(cli, "_client", lambda *a, **k: SimpleNamespace(call=call))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    args = cli.build_parser().parse_args(["wait", "20261004-000005-done"])
    assert cli.wait_jobs(args, ["20261004-000005-done"], timeout=None, quiet=True) == 0
    assert steps == []


def test_c16_7_the_cli_wait_ends_when_the_busy_daemon_stops(monkeypatch, capsys):
    """Review r3, P2: after a busy answer, the daemon stopped and unlinked its socket.
    Every poll then failed and an unbounded `wait` asked for ever; it now reports the
    daemon absent at once."""
    from types import SimpleNamespace
    from subfleet import cli
    steps = [busy(), socket_gone(), {"jobs": [], "timeout": True}]

    def call(op, args, **kwargs):
        step = steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step
    monkeypatch.setattr(cli, "_client", lambda *a, **k: SimpleNamespace(call=call))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    args = cli.build_parser().parse_args(["wait", "20261004-000007-any"])
    assert cli.wait_jobs(args, ["20261004-000007-any"], timeout=None, quiet=True) != 0
    assert len(steps) == 1                                  # it stopped at the unlinked socket
    capsys.readouterr()


def test_c16_7_the_cli_wait_still_reports_a_daemon_absent_from_the_start(monkeypatch, capsys):
    from types import SimpleNamespace
    from subfleet import cli

    def call(op, args, **kwargs):
        raise DaemonUnavailable("no daemon at daemon.sock: Connection refused")
    monkeypatch.setattr(cli, "_client", lambda *a, **k: SimpleNamespace(call=call))
    args = cli.build_parser().parse_args(["wait", "20261004-000006-any"])
    assert cli.wait_jobs(args, ["20261004-000006-any"], timeout=None, quiet=True) != 0
    capsys.readouterr()


def test_c16_7_the_hook_wait_takes_a_connect_refused_after_busy_as_busy(monkeypatch):
    from subfleet import hooks
    delivered = []
    steps = [busy(), refused(), {"jobs": [{"job_id": "j", "state": "succeeded"}], "timeout": False}]

    class Hooked:
        def call(self, op, args, **kwargs):
            step = steps.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step
    monkeypatch.setattr(hooks, "_deliver", lambda client, session, job, stderr: delivered.append(job) or 0)
    clock = [0.0]
    assert hooks._wait_and_deliver(Hooked(), "s", "j", 100.0, stderr=None, now=lambda: clock[0],
                                   sleep=lambda s: clock.__setitem__(0, clock[0] + s)) == 0
    assert delivered and steps == []
    # Refused with no busy answer first, or a socket gone after one: the daemon is
    # absent, and the hook gives up quietly.
    for script in ([refused()], [busy(), socket_gone()]):
        pending = list(script)

        class Gone:
            def call(self, op, args, **kwargs):
                raise pending.pop(0)
        assert hooks._wait_and_deliver(Gone(), "s", "j", 100.0, stderr=None, now=lambda: 0.0,
                                       sleep=lambda s: None) == 0 and not pending
    absent = [refused()]

    class Absent:
        def call(self, op, args, **kwargs):
            raise absent.pop(0)
    assert hooks._wait_and_deliver(Absent(), "s", "j", 100.0, stderr=None, now=lambda: 0.0,
                                   sleep=lambda s: None) == 0 and not absent


def test_c3_6_a_lock_that_cannot_be_cleared_keeps_the_handler_and_its_stream(monkeypatch, isolated_dumps):
    """Review r2 (GPT P1, Opus P3): if clearing `stack_dumps` from the lock raised
    (ENOSPC, EIO), the handler stayed registered on a stream the cleanup then closed,
    so a SIGUSR1 dumped into whatever reused the descriptor. Unregistering instead
    would let `daemon stacks`, still told `stack_dumps`, kill the host. The handler
    and its stream stay together: open, and a dump lands in daemon.log."""
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fake-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fake-start")
    real_write = Daemon._write_lock

    def write(self, *, stack_dumps):
        if not stack_dumps:
            raise OSError(28, "No space left on device")
        return real_write(self, stack_dumps=stack_dumps)
    monkeypatch.setattr(Daemon, "_write_lock", write)
    monkeypatch.setattr(daemon_module, "load_policy", lambda path: (_ for _ in ()).throw(RuntimeError("bad policy")))
    import faulthandler
    import signal
    with tempfile.TemporaryDirectory(prefix="sfi-", dir="/tmp") as directory:
        with pytest.raises(RuntimeError, match="bad policy"):
            Daemon(Path(directory))
        record = json.loads((Path(directory) / "daemon.lock").read_text())
        assert record.get("stack_dumps") is True                   # the write that failed
        log = Path(directory) / "daemon.log"
        size = log.stat().st_size
        try:
            os_kill_self(signal.SIGUSR1)                           # as `daemon stacks` would
            assert wait_until(lambda: log.stat().st_size > size), "the dump did not reach daemon.log"
        finally:
            pass                                            # `isolated_dumps` forgets the kept entry


def os_kill_self(sig) -> None:
    import os
    os.kill(os.getpid(), sig)


def wait_until(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.02)
    return False


# --- review round 3 (of eac0706) -----------------------------------------------

def test_c15_5_the_deadline_read_does_not_wait_for_the_hubs_wake(daemon):
    """Review r3, P2: the job committed while the pass read (its snapshot older),
    and the hub, which wakes a waiter only after its own read, had not woken this one
    when the deadline passed. The waiter reads once more regardless."""
    daemon.wait_hub.recheck_s = 3600
    job = add_job(daemon, "20261004-000008-ended-unannounced")
    real, calls = daemon._wait_answer, []

    def stale_then_real(job_ids):
        calls.append(time.monotonic())
        if len(calls) == 1:
            answer = real(job_ids)                          # the snapshot: still running
            time.sleep(.4)                                  # past the 0.3 s deadline
            commit_elsewhere(daemon, job)                   # no wake: the hub has not read it yet
            return answer
        return real(job_ids)
    daemon._wait_answer = stale_then_real
    result = daemon.wait(protocol.WaitArgs(job_ids=[job], deadline_s=.3), client_gone=lambda: False)
    assert result["timeout"] is False and result["jobs"][0]["state"] == "succeeded" and len(calls) == 2


def test_c3_6_close_keeps_the_handler_and_its_stream_when_the_lock_cannot_be_cleared(monkeypatch, isolated_dumps):
    """Review r3, P3: `close()` unregistered SIGUSR1 even when `daemon.lock` still said
    `stack_dumps`, so `daemon stacks` could end a host built off the main thread. As a
    failed construction does, it now keeps the handler and the stream it writes to."""
    import signal
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fake-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fake-start")
    with tempfile.TemporaryDirectory(prefix="sfi-", dir="/tmp") as directory:
        core = Daemon(Path(directory))
        real_write = Daemon._write_lock

        def write(self, *, stack_dumps):
            if not stack_dumps:
                raise OSError(28, "No space left on device")
            return real_write(self, stack_dumps=stack_dumps)
        monkeypatch.setattr(Daemon, "_write_lock", write)
        try:
            core.close()
            log = Path(directory) / "daemon.log"
            assert "could not drop stack_dumps" in log.read_text()
            size = log.stat().st_size
            os_kill_self(signal.SIGUSR1)                    # as `daemon stacks` would
            assert wait_until(lambda: log.stat().st_size > size), "the dump did not reach daemon.log"
        finally:
            pass                                            # `isolated_dumps` forgets the kept entry


SCENARIO = r"""
import json, os, signal, sys, threading, time
from pathlib import Path
from subfleet import daemon as dm
from subfleet.daemon import Daemon
dm.procs.boot_id = lambda: "fake-boot"
dm.procs.proc_start = lambda pid: "fake-start"
signal.signal(signal.SIGUSR1, signal.SIG_DFL)            # as a fresh host starts: this run may have
                                                         # left it ignored, and children inherit that
real_write, real_policy = Daemon._write_lock, dm.load_policy
state = {"fail_clear": True, "fail_policy": True}

def write(self, *, stack_dumps):
    if not stack_dumps and state["fail_clear"]:
        raise OSError(5, "Input/output error")
    return real_write(self, stack_dumps=stack_dumps)

def policy(path):
    if state["fail_policy"]:
        raise RuntimeError("bad policy")
    return real_policy(path)
Daemon._write_lock, dm.load_policy = write, policy

def build(root, close):
    try:
        core = Daemon(root)
    except RuntimeError:
        return
    if close:
        core.close()

base, later = Path(sys.argv[1]), sys.argv[2]
a, b = base / "a", base / "b"
built = {}

def run(name, root, close):
    try:
        built[name] = Daemon(root)
    except RuntimeError:
        return
    if close:
        built[name].close()

def thread(*args):
    worker = threading.Thread(target=run, args=args)     # off the main thread, as reported
    worker.start(); worker.join()

if later in ("fails", "closes"):
    thread("a", a, False)                                 # A failed and kept its handler and stream
    state["fail_clear"] = False
    state["fail_policy"] = later == "fails"
    thread("b", b, later == "closes")                     # B cleared its own flag and let go
else:
    state["fail_clear"] = False
    state["fail_policy"] = False
    thread("a", a, False)                                 # A runs on, its lock saying stack_dumps
    thread("b", b, False)                                 # B takes SIGUSR1 over
    if later == "nonholder-fails-clear":
        state["fail_clear"] = True
        threading.Thread(target=built["a"].close).start() or None
        time.sleep(3)                                     # A could not clear its flag
        state["fail_clear"] = False
    threading.Thread(target=built["b"].close).start()
    time.sleep(3)                                         # B closed cleanly
size = (a / "daemon.log").stat().st_size
os.kill(os.getpid(), signal.SIGUSR1)                      # `daemon stacks` against A
deadline = time.monotonic() + 5
while time.monotonic() < deadline and (a / "daemon.log").stat().st_size <= size:
    time.sleep(.02)
print(json.dumps({"grew": (a / "daemon.log").stat().st_size > size,
                  "a_flag": json.loads((a / "daemon.lock").read_text()).get("stack_dumps"),
                  "b_flag": json.loads((b / "daemon.lock").read_text()).get("stack_dumps")}))
"""


@pytest.mark.parametrize("later", ["fails", "closes", "live-earlier", "nonholder-fails-clear"])
def test_c3_6_a_later_daemon_never_takes_a_kept_handler_away(tmp_path, later):
    """Review r3, P1 (GPT): daemon A's construction failed and could not clear its
    lock, so it kept its handler and stream. Daemon B, built later in the same host
    off the main thread, then failed (or ran and closed) and let SIGUSR1 go, and
    `daemon stacks` against A, whose lock still says `stack_dumps`, ended the host
    (exit -30). While A's flag stands, the signal now dumps into A's log (with SIG_IGN
    beneath every fresh registration since review of 2300b43, a regression here
    dumps nothing rather than ending the process; `grew` catches either). Review r4,
    P2: the same when A is a running daemon B took SIGUSR1 over from, and when A, not
    holding it, could not clear its flag at close. In a subprocess, so a regression
    ends that process, not this test run."""
    import os
    import subprocess
    import sys
    repo = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH" and not k.startswith("SUBFLEET_")}
    env["PYTHONPATH"] = str(repo)
    done = subprocess.run([sys.executable, "-c", SCENARIO, str(tmp_path), later], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, (done.returncode, done.stderr[-2000:])
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result == {"grew": True, "a_flag": True, "b_flag": None}


# --- review round 4 (of 4fc5b49): every daemon whose lock says `stack_dumps` ----

def test_c3_6_a_newer_daemon_closing_hands_sigusr1_to_an_older_one_still_running(tmp_path, isolated_dumps):
    """Review r4, P2: the newer daemon held SIGUSR1 and closed; the older one ran on,
    its lock still saying `stack_dumps`, but the handler went (off the main thread,
    to the fatal default). It is handed to the older one now."""
    from test_lockwatch import DUMPED, dumped_into, log_text
    older = Daemon(tmp_path / "older")
    newer = Daemon(tmp_path / "newer")
    try:
        newer.close()
        assert daemon_module._STACK_DUMPS() is older
        assert dumped_into(older, len(log_text(older)))
    finally:
        older.close()
    assert daemon_module._STACK_DUMPS is None and not daemon_module._ADVERTISED


def test_c3_6_a_non_holder_whose_lock_cannot_be_cleared_keeps_its_stream(tmp_path, monkeypatch, isolated_dumps):
    """Review r4, P2: the older daemon, not holding SIGUSR1, could not clear its
    flag at close and kept nothing; the newer closed cleanly and let the handler go
    while the older's lock still said `stack_dumps`."""
    from test_lockwatch import dumped_into, log_text
    older = Daemon(tmp_path / "older")
    newer = Daemon(tmp_path / "newer")
    real_write = Daemon._write_lock

    def failing_for_older(self, *, stack_dumps):
        if self is older and not stack_dumps:
            raise OSError(5, "Input/output error")
        return real_write(self, stack_dumps=stack_dumps)
    monkeypatch.setattr(Daemon, "_write_lock", failing_for_older)
    older.close()
    newer.close()
    assert json.loads((tmp_path / "older" / "daemon.lock").read_text()).get("stack_dumps") is True
    assert daemon_module._STACK_DUMPS() is older                # a dead-or-alive reference: the kept one
    assert dumped_into(older, len(log_text(older)))


def test_c16_7_refused_while_busy_says_busy_absent_or_unverifiable(tmp_path):
    """The three answers: a refused connect while the lock names a living holder is
    busy; with a dead holder, or a socket gone, absent; with a lock that cannot say,
    unverifiable (None), which only a bounded caller may take as busy."""
    from types import SimpleNamespace
    assert client_module.refused_while_busy(SimpleNamespace(lock_holder_alive=lambda: True), refused()) is True
    assert client_module.refused_while_busy(SimpleNamespace(lock_holder_alive=lambda: False), refused()) is False
    assert client_module.refused_while_busy(SimpleNamespace(lock_holder_alive=lambda: None), refused()) is None
    assert client_module.refused_while_busy(SimpleNamespace(lock_holder_alive=lambda: True), socket_gone()) is False


@pytest.mark.parametrize("alive,timeout,expected", [(None, None, "gives-up"), (True, None, "waits-on"),
                                                    (None, 600.0, "waits-on")],
                         ids=["unverifiable", "alive", "unverifiable-with-timeout"])
def test_c16_7_a_cli_wait_bounds_refusals_only_while_the_lock_cannot_say(monkeypatch, alive, timeout, expected):
    """Review of 4fc5b49, P2: a refused connect after busy, with a lock that cannot say
    whether its holder lives, kept an unbounded `wait` asking for ever (a replacement
    daemon had truncated the lock and gone). It is busy for at most
    REFUSED_UNVERIFIED_MAX_S, then absent; a lock naming a living holder waits on.
    Review of 1efa0ef, P3: a `wait --timeout` is bounded by that, not cut off at the
    60 s meant for a wait with no deadline."""
    from types import SimpleNamespace
    from subfleet import cli
    clock = [0.0]
    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: clock[0],
                                                     sleep=lambda s: clock.__setitem__(0, clock[0] + s)))
    monkeypatch.setattr(cli, "REFUSED_UNVERIFIED_MAX_S", 5.0)
    polls = [0]

    def call(op, args, **kwargs):
        polls[0] += 1
        if polls[0] == 1:
            raise busy()
        if polls[0] < 40:                                   # well past 5 s of refusals
            raise refused()
        return {"jobs": [{"job_id": "20261004-000009-done", "state": "succeeded", "rc": 0}], "timeout": False}
    monkeypatch.setattr(cli, "_client", lambda *a, **k: SimpleNamespace(call=call, lock_holder_alive=lambda: alive))
    args = cli.build_parser().parse_args(["wait", "20261004-000009-done"])
    code = cli.wait_jobs(args, ["20261004-000009-done"], timeout=timeout, quiet=True)
    if expected == "gives-up":
        assert code != 0 and polls[0] < 40 and clock[0] >= 5.0
    else:
        assert code == 0 and polls[0] == 40


# --- review round 5 (of 1efa0ef) ----------------------------------------------

RESTORED = r"""
import ctypes, faulthandler, json, os, signal, sys, threading
from pathlib import Path
from types import SimpleNamespace
from subfleet import daemon as dm
from subfleet.daemon import Daemon
dm.procs.boot_id = lambda: "fake-boot"
dm.procs.proc_start = lambda pid: "fake-start"
signal.signal(signal.SIGUSR1, signal.SIG_DFL)            # as a fresh host starts: this run may have
                                                         # left it ignored, and children inherit that

def disposition():
    libc = ctypes.CDLL(None, use_errno=True)
    action = ctypes.create_string_buffer(256)
    assert libc.sigaction(signal.SIGUSR1, None, action) == 0
    return {0: "default", 1: "ignore"}.get(ctypes.c_void_p.from_buffer(action).value or 0, "handler")

restored = []
def unregister(signum):
    result = faulthandler.unregister(signum)
    restored.append(disposition())
    os.kill(os.getpid(), signal.SIGUSR1)                  # a `daemon stacks` that read the flag just before it cleared
    return result

def racing():
    dm.faulthandler = SimpleNamespace(register=faulthandler.register, unregister=unregister,
                                      dump_traceback_later=faulthandler.dump_traceback_later)

def off_main(fn):
    worker = threading.Thread(target=fn)
    worker.start(); worker.join()

base, order = Path(sys.argv[1]), sys.argv[2]
built = {}
if order == "off-then-main":
    off_main(lambda: built.__setitem__("a", Daemon(base / "a")))
    b = Daemon(base / "b")                                # on the main thread while A stands
    racing()
    built["a"].close()
    b.close()                                             # the last to leave, on the main thread
elif order == "off-only":
    def build_and_close():
        core = Daemon(base / "a")
        racing()
        core.close()                                      # the last to leave, off the main thread
    off_main(build_and_close)
elif order == "off-at-exit":
    off_main(lambda: built.__setitem__("a", Daemon(base / "a")))
    unregister(signal.SIGUSR1)                            # what the interpreter's exit does to a user signal
print(json.dumps({"restored": restored, "now": disposition()}))
"""


@pytest.mark.parametrize("order", ["off-then-main", "off-only", "off-at-exit"])
def test_c3_6_letting_sigusr1_go_always_restores_it_ignored(tmp_path, order):
    """Review of 1efa0ef, P3, and of 2300b43, P2 (GPT): daemon A, built off the main
    thread, took SIGUSR1 first, so faulthandler saved the fatal default beneath its
    handler. Whenever the handler then went, at the last close (B, built on the main
    thread while A stood, closing last there; or A alone, off it) or as the
    interpreter exited, faulthandler put that default back, and a `daemon stacks`
    that had read the flag just before it cleared ended the host (exit -30), even
    with SIG_IGN set straight afterwards. SIG_IGN is now beneath the handler
    whenever faulthandler takes the signal fresh, on either thread, so what it puts
    back ends nothing. The signal is raised the moment `unregister` returns."""
    import os
    import subprocess
    import sys
    repo = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH" and not k.startswith("SUBFLEET_")}
    env["PYTHONPATH"] = str(repo)
    done = subprocess.run([sys.executable, "-c", RESTORED, str(tmp_path), order], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, (done.returncode, done.stderr[-2000:])
    assert json.loads(done.stdout.strip().splitlines()[-1]) == {"restored": ["ignore"], "now": "ignore"}


def test_c3_6_taking_and_handing_on_sigusr1_hold_one_lock(tmp_path, monkeypatch, isolated_dumps):
    """Review of 1efa0ef, P3: a holder closing on one thread while another daemon was
    built on a second could hand the signal on to nobody between the second's
    registration and its joining `_ADVERTISED`, leaving it advertised with no
    handler. Registering and joining, and leaving and handing on, each run under
    `_DUMPS_LOCK`, so neither can fall between the other's two steps. Reviews of
    9a514a7 and 674b99b, P3: a close that decided outside the lock whether it held
    the signal passed the forced interleaving. There is no such decision now
    (every leave settles), and every step is checked here: joining, leaving,
    settling and registering, for a close that holds the signal and one that
    does not."""
    import faulthandler
    from types import SimpleNamespace
    seen = []
    real_settle = daemon_module._settle_sigusr1

    def register(*args, **kwargs):
        seen.append(("register", daemon_module._DUMPS_LOCK._is_owned()))
        return faulthandler.register(*args, **kwargs)

    def settle():
        seen.append(("settle", daemon_module._DUMPS_LOCK._is_owned()))
        return real_settle()

    class Advertised(dict):
        def __setitem__(self, key, value):
            seen.append(("join", daemon_module._DUMPS_LOCK._is_owned()))
            super().__setitem__(key, value)

        def pop(self, key, *default):
            seen.append(("leave", daemon_module._DUMPS_LOCK._is_owned()))
            return super().pop(key, *default)
    monkeypatch.setattr(daemon_module, "faulthandler",
                        SimpleNamespace(register=register, unregister=faulthandler.unregister,
                                        dump_traceback_later=faulthandler.dump_traceback_later))
    monkeypatch.setattr(daemon_module, "_settle_sigusr1", settle)
    monkeypatch.setattr(daemon_module, "_ADVERTISED", Advertised(daemon_module._ADVERTISED))
    older = Daemon(tmp_path / "older")
    core = Daemon(tmp_path / "solo")
    token = core._dumps_token
    assert token in daemon_module._ADVERTISED and daemon_module._STACK_DUMPS() is core
    taking = len(seen)
    older.close()                                           # not holding it
    core.close()                                            # holding it, the last to leave
    assert token not in daemon_module._ADVERTISED and daemon_module._STACK_DUMPS is None
    assert not daemon_module._HELD_STREAMS
    assert {step for step, _ in seen} == {"join", "leave", "settle", "register"}, seen
    assert all(owned for _, owned in seen), seen
    # Review of 8ebe6b6, P3 (GPT): each leave settles, holder or not.
    leaving = [step for step, _ in seen[taking:] if step in ("leave", "settle")]
    assert leaving == ["leave", "settle", "leave", "settle"], seen[taking:]


def test_c3_6_a_daemon_closing_while_the_signal_is_handed_to_it_hands_it_on(tmp_path, monkeypatch, isolated_dumps):
    """Review of 1efa0ef (GPT, hard): the newest daemon, closing, had registered
    SIGUSR1 on the next daemon's stream but not yet recorded it as the holder; that
    daemon closed at the same moment, saw itself not holding the signal and closed
    its stream, and a SIGUSR1 then wrote stacks into whatever reused the descriptor
    while a third daemon stood advertised. The second close now waits for the hand-
    off, finds itself the holder, and hands the signal on to the third.

    The interleaving is forced, not hoped for (reviews of 78bfbf0, 3cac1e8 and
    67b9adc, P3). Middle's close passes its membership check and stops at its lock
    write; newest's close then hands the signal to middle and stops inside the
    hand-off, holding `_DUMPS_LOCK`; middle's close goes on to the step where it
    leaves the set and decides whether it holds the signal. `_DUMPS_LOCK`, wrapped,
    lets middle take it only without blocking and says when it is refused, which
    can happen only there and only while the hand-off holds it; middle cannot pass
    that step until the hand-off lets go. A close that decides outside the lock,
    or no lock, is never refused, and one that leaves the set outside it is
    refused with its token already gone (review of 6506619, P3): the test fails
    either way, whatever the scheduling."""
    import faulthandler
    from types import SimpleNamespace
    from test_lockwatch import dumped_into, log_text
    oldest = Daemon(tmp_path / "oldest")
    middle = Daemon(tmp_path / "middle")
    newest = Daemon(tmp_path / "newest")
    assert daemon_module._STACK_DUMPS() is newest
    writing, go_on, entered, release, refused = (threading.Event() for _ in range(5))
    real_lock, real_write, middle_closing = daemon_module._DUMPS_LOCK, Daemon._write_lock, []

    class Watched:
        """`_DUMPS_LOCK`, which middle's close takes only without blocking, saying
        when it is refused."""

        def __enter__(self):
            if middle_closing and threading.current_thread() is middle_closing[0]:
                while not real_lock.acquire(blocking=False):
                    refused.set()
                    time.sleep(.01)
                return True
            return real_lock.acquire()

        def __exit__(self, *exc):
            real_lock.release()
            return False

        def _is_owned(self):
            return real_lock._is_owned()

    def write(self, *, stack_dumps):
        if self is middle and not stack_dumps:
            writing.set()                                   # past its membership check, the lock let go
            go_on.wait()                                    # until newest is inside the hand-off
        return real_write(self, stack_dumps=stack_dumps)

    def register(*args, **kwargs):
        result = faulthandler.register(*args, **kwargs)
        if kwargs.get("file") is middle._log_handler.stream and not entered.is_set():
            entered.set()                                   # registered on middle's stream, not yet recorded
            release.wait()                                  # until the test releases it, in `finally`
        return result
    monkeypatch.setattr(daemon_module, "_DUMPS_LOCK", Watched())
    monkeypatch.setattr(Daemon, "_write_lock", write)
    monkeypatch.setattr(daemon_module, "faulthandler",
                        SimpleNamespace(register=register, unregister=faulthandler.unregister,
                                        dump_traceback_later=faulthandler.dump_traceback_later))
    closing_middle = threading.Thread(target=middle.close)
    middle_closing.append(closing_middle)
    closing_newest = threading.Thread(target=newest.close)
    try:
        closing_middle.start()
        assert writing.wait(10), "middle's close never came to its lock write"
        assert not refused.is_set()                         # nothing held the lock at its membership check
        closing_newest.start()
        assert entered.wait(10), "newest never handed the signal to middle"
        go_on.set()
        assert refused.wait(10), "middle left the set or decided without waiting for the hand-off"
        assert middle._dumps_token in daemon_module._ADVERTISED, "middle left the set without the lock"
        assert closing_middle.is_alive(), "middle closed between the hand-off's two steps"
        assert not middle._log_handler.stream.closed
    finally:
        go_on.set()
        release.set()
        closing_newest.join(10)
        closing_middle.join(10)
    assert not closing_middle.is_alive() and middle._log_handler.stream.closed
    assert daemon_module._STACK_DUMPS() is oldest
    try:
        assert dumped_into(oldest, len(log_text(oldest)))
    finally:
        oldest.close()
    assert daemon_module._STACK_DUMPS is None and not daemon_module._ADVERTISED


# --- review round 7 (of 78bfbf0): a daemon that cannot make SIGUSR1 safe -------

def test_c3_6_a_daemon_that_cannot_ignore_sigusr1_beneath_does_not_say_stack_dumps(tmp_path, monkeypatch,
                                                                                    isolated_dumps):
    """Review of 78bfbf0, P2 (GPT): when setting SIGUSR1 ignored failed, the daemon
    logged it and registered over the default action anyway, and its lock said
    `stack_dumps`; the last close or the interpreter's exit put the default back,
    and a `daemon stacks` that had read the flag ended the host (exit -30). Now it
    neither registers nor says `stack_dumps`, and `daemon stacks` refuses it."""
    assert not daemon_module._ADVERTISED                    # so this daemon takes the signal fresh

    def failing():
        raise OSError(22, "Invalid argument")
    monkeypatch.setattr(daemon_module, "_ignore_sigusr1", failing)
    core = Daemon(tmp_path / "unsafe")
    try:
        assert "stack_dumps" not in json.loads((tmp_path / "unsafe" / "daemon.lock").read_text())
        assert core._dumps_token not in daemon_module._ADVERTISED and daemon_module._STACK_DUMPS is None
        core._log_handler.flush()
        assert "does not dump stacks on SIGUSR1" in (tmp_path / "unsafe" / "daemon.log").read_text()
    finally:
        core.close()
    assert daemon_module._STACK_DUMPS is None and not daemon_module._ADVERTISED


HOST = r"""
import json, os, signal, sys, threading
from pathlib import Path
from subfleet import daemon as dm
from subfleet.daemon import Daemon
dm.procs.boot_id = lambda: "fake-boot"
dm.procs.proc_start = lambda pid: "fake-start"
signal.signal(signal.SIGUSR1, signal.SIG_DFL)            # as a fresh host starts: this run may have
                                                         # left it ignored, and children inherit that
base, case = Path(sys.argv[1]), sys.argv[2]
built = {}

def build():
    built["d"] = Daemon(base / "d")

if case.startswith("host-"):
    signal.signal(signal.SIGUSR1, lambda *_: None)        # the host's own Python handler
if case == "host-main":
    build()
else:
    worker = threading.Thread(target=build)
    worker.start(); worker.join()
table = signal.getsignal(signal.SIGUSR1)
print(json.dumps({"stack_dumps": json.loads((base / "d" / "daemon.lock").read_text()).get("stack_dumps"),
                  "table": "ignore" if table == signal.SIG_IGN else "callable" if callable(table) else "other"}),
      flush=True)
if case == "off-main-exit":
    class AtExit:
        def __del__(self):
            os.kill(os.getpid(), signal.SIGUSR1)          # a `daemon stacks` while the interpreter exits
    at_exit = AtExit()                                    # dropped as the modules are torn down, the daemon open
"""


@pytest.mark.parametrize("case,expected", [
    ("host-off-main", {"stack_dumps": None, "table": "callable"}),
    ("host-main", {"stack_dumps": True, "table": "ignore"}),
    ("off-main-exit", {"stack_dumps": True, "table": "other"}),
])
def test_c3_6_a_daemon_says_stack_dumps_only_where_the_signal_stays_safe_through_exit(tmp_path, case, expected):
    """Review of 78bfbf0, P2 (GPT) and P3-1 (Opus): a host with a Python SIGUSR1
    handler of its own built a daemon off the main thread. libc set SIGUSR1 ignored,
    but Python's table kept the host's handler, and Python's exit reset that to the
    default action before faulthandler let go: a `daemon stacks` during the exit
    ended the host (exit -30). Such a daemon now does not say `stack_dumps`. On the
    main thread `signal.signal` puts SIG_IGN in the table too, and the daemon says
    so. With no host handler, a daemon built off the main thread and left open
    dumps a signal sent while the interpreter tears its modules down, and the
    process exits 0 (the real exit, not a stand-in for it)."""
    import os
    import subprocess
    import sys
    repo = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH" and not k.startswith("SUBFLEET_")}
    env["PYTHONPATH"] = str(repo)
    done = subprocess.run([sys.executable, "-c", HOST, str(tmp_path), case], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, (done.returncode, done.stderr[-2000:])
    assert json.loads(done.stdout.strip().splitlines()[-1]) == expected
    if case == "off-main-exit":
        assert "(most recent call first)" in (tmp_path / "d" / "daemon.log").read_text(errors="replace")



# --- review round 8 (of 3cac1e8): a declined daemon never joins ---------------

def test_c3_6_a_declined_daemon_whose_lock_write_fails_never_joins_the_advertised(tmp_path, monkeypatch,
                                                                                isolated_dumps):
    """Review of 3cac1e8, P2 (GPT; P3-1, Opus): daemon A declined SIGUSR1 (setting it
    ignored failed), so its lock never said `stack_dumps`; its close then met EIO
    rewriting the lock, and that put A into `_ADVERTISED`. Daemon B, built next,
    found the set not empty, skipped the SIG_IGN beneath its fresh registration,
    and said `stack_dumps` over the default action (GPT: exit -30 at the last
    close and at exit). A now never joins, and B takes the signal fresh."""
    assert not daemon_module._ADVERTISED
    ignored, closing, real_ignore, real_write = [], [], daemon_module._ignore_sigusr1, Daemon._write_lock

    def ignore():
        if not ignored:
            ignored.append("failed")
            raise OSError(22, "Invalid argument")
        ignored.append("set")
        return real_ignore()

    def write(self, *, stack_dumps):
        if self.root.name == "declined" and closing:
            raise OSError(5, "Input/output error")
        return real_write(self, stack_dumps=stack_dumps)
    monkeypatch.setattr(daemon_module, "_ignore_sigusr1", ignore)
    monkeypatch.setattr(Daemon, "_write_lock", write)
    declined = Daemon(tmp_path / "declined")
    assert "stack_dumps" not in json.loads((tmp_path / "declined" / "daemon.lock").read_text())
    closing.append(True)
    declined.close()                                        # its lock rewrite meets EIO
    assert not daemon_module._ADVERTISED and declined._log_handler.stream.closed
    later = Daemon(tmp_path / "later")
    try:
        assert ignored == ["failed", "set"], ignored        # a fresh take, SIG_IGN beneath
        assert json.loads((tmp_path / "later" / "daemon.lock").read_text()).get("stack_dumps") is True
        assert daemon_module._STACK_DUMPS() is later
    finally:
        later.close()
    assert daemon_module._STACK_DUMPS is None and not daemon_module._ADVERTISED



# --- review round 9 (of 67b9adc): a registration interrupted part way -----------

INTERRUPTED = r"""
import faulthandler, json, os, signal, sys
from pathlib import Path
from types import SimpleNamespace
from subfleet import daemon as dm
from subfleet.daemon import Daemon
dm.procs.boot_id = lambda: "fake-boot"
dm.procs.proc_start = lambda pid: "fake-start"
signal.signal(signal.SIGUSR1, signal.SIG_DFL)            # as a fresh host starts
base, where = Path(sys.argv[1]), sys.argv[2]
a = Daemon(base / "a")                                    # holds SIGUSR1, its lock says stack_dumps
real_write, real_enable = Daemon._write_lock, Daemon._enable_stack_dumps

def register(*args, **kwargs):
    faulthandler.register(*args, **kwargs)
    if where == "registered" and kwargs.get("file") is not a._log_handler.stream:
        raise KeyboardInterrupt                           # SIGINT, after registering, before recording

class Inserting(dict):
    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if where == "inserted" and len(self) > 1:
            raise KeyboardInterrupt                       # SIGINT, B recorded, before the take returns

def enable(self):
    taken = real_enable(self)
    if where == "taken" and self.root.name == "b":
        raise KeyboardInterrupt                           # SIGINT, the take done, before the lock says so
    return taken
at_cleanup = []
real_release = Daemon._release_partial_init

def release(self):
    at_cleanup.append(len(dm._ADVERTISED))                # a take that raised has left the set by now
    return real_release(self)
dm._ADVERTISED = Inserting(dm._ADVERTISED)
Daemon._enable_stack_dumps = enable
Daemon._release_partial_init = release

def write(self, *, stack_dumps):
    if self.root.name == "b" and not stack_dumps and where != "cleanup-settle":
        raise OSError(5, "Input/output error")            # B's cleanup cannot clear its lock
    return real_write(self, stack_dumps=stack_dumps)

real_settle, settle_armed = dm._settle_sigusr1, []

def settle():
    if settle_armed:
        settle_armed.clear()
        raise RuntimeError("inside the settle")           # B has left; its cleanup swallows this
    return real_settle()

def policy(path):
    if where == "cleanup-settle":
        settle_armed.append(True)                         # B took the signal; construction now fails
        raise RuntimeError("policy unreadable")
    return real_policy(path)
real_policy = dm.load_policy
dm.load_policy = policy
dm._settle_sigusr1 = settle
dm.faulthandler = SimpleNamespace(register=register, unregister=faulthandler.unregister,
                                  dump_traceback_later=faulthandler.dump_traceback_later)
Daemon._write_lock = write
try:
    Daemon(base / "b")
except (KeyboardInterrupt, RuntimeError):
    pass
reused = os.pipe()                                        # what B's closed descriptor may become
os.set_blocking(reused[0], False)
size = (base / "a" / "daemon.log").stat().st_size
os.kill(os.getpid(), signal.SIGUSR1)                      # `daemon stacks` against A
import time
deadline = time.monotonic() + 5
while time.monotonic() < deadline and (base / "a" / "daemon.log").stat().st_size <= size:
    time.sleep(.02)
try:
    stray = len(os.read(reused[0], 65536))
except BlockingIOError:
    stray = 0
print(json.dumps({"a_grew": (base / "a" / "daemon.log").stat().st_size > size, "stray": stray,
                  "holder_is_a": dm._STACK_DUMPS() is a, "members": len(dm._ADVERTISED),
                  "at_cleanup": at_cleanup}))
"""


@pytest.mark.parametrize("where,at_cleanup", [("registered", 1), ("inserted", 1), ("taken", 2),
                                              ("cleanup-settle", 2)])
def test_c3_6_a_registration_interrupted_part_way_hands_the_signal_back(tmp_path, where, at_cleanup):
    """Review of 67b9adc, P2 (GPT): daemon A held SIGUSR1 and its lock said
    `stack_dumps`. Daemon B registered its own stream, and a KeyboardInterrupt
    landed before B recorded it; B was not a member, so its cleanup closed its
    stream, which faulthandler still held, and A's dumps went to whatever reused
    the descriptor. The interrupted take now points the signal back at A first.
    Review of 6506619, P2 (GPT): the interrupt landed after B joined the set
    (before the take returned, or after it but before B's lock said
    `stack_dumps`), and B's cleanup, unable to rewrite its lock, kept B in the set
    for good, A's dumps going to B's abandoned log. A lock never written with the
    flag no longer keeps a daemon in, and a take that raises has left the set
    before its cleanup starts (one that completed is a member until then). Review
    of 674b99b: construction failed after B took the signal, and an exception in
    its leave's settle was swallowed by the cleanup loop, which then closed B's
    stream while faulthandler held it; a stream that may be held now closes only
    after a settle has moved the signal off it. In a subprocess, so a
    regression's stray dump lands in that process."""
    import os
    import subprocess
    import sys
    repo = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH" and not k.startswith("SUBFLEET_")}
    env["PYTHONPATH"] = str(repo)
    done = subprocess.run([sys.executable, "-c", INTERRUPTED, str(tmp_path), where], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, (done.returncode, done.stderr[-2000:])
    assert json.loads(done.stdout.strip().splitlines()[-1]) == {"a_grew": True, "stray": 0, "holder_is_a": True,
                                                               "members": 1, "at_cleanup": [at_cleanup]}



# --- review round 11 (of 9a514a7): a close interrupted part way -----------------

CLOSE_INTERRUPTED = r"""
import faulthandler, json, os, signal, sys, threading, time
from pathlib import Path
from types import SimpleNamespace
from subfleet import daemon as dm
from subfleet.daemon import Daemon
dm.procs.boot_id = lambda: "fake-boot"
dm.procs.proc_start = lambda pid: "fake-start"
signal.signal(signal.SIGUSR1, signal.SIG_DFL)            # as a fresh host starts
base, case = Path(sys.argv[1]), sys.argv[2]
three = case in ("handoff", "before-leaving", "settle-entry", "collected")
names = ["a", "b", "c"] if three else ["a", "b"]
built = {name: Daemon(base / name) for name in names}     # the last built holds SIGUSR1
real_write, armed = Daemon._write_lock, [True]

def register(*args, **kwargs):
    faulthandler.register(*args, **kwargs)
    if case == "handoff" and armed and kwargs.get("file") is built["b"]._log_handler.stream:
        armed.clear()
        raise KeyboardInterrupt                           # C's hand-off: registered on B's stream

def write(self, *, stack_dumps):
    if self is built["b"] and not stack_dumps and armed and case in ("before-rewrite", "after-rewrite"):
        armed.clear()
        if case == "before-rewrite":
            raise KeyboardInterrupt                       # before anything is written: the flag stands
        real_write(self, stack_dumps=stack_dumps)
        raise KeyboardInterrupt                           # the rewrite durable, as fsync returns
    return real_write(self, stack_dumps=stack_dumps)
real_leave, real_settle, settling = Daemon._leave_advertised, dm._settle_sigusr1, []

def leave(self, token):
    if case in ("before-leaving", "collected") and armed and self is built["c"]:
        armed.clear()
        raise KeyboardInterrupt                           # C's lock rewritten, C not yet left
    return real_leave(self, token)

def settle():
    if case == "settle-entry" and armed and settling:
        armed.clear()
        raise KeyboardInterrupt                           # C has left the set, the settle not begun
    return real_settle()
Daemon._leave_advertised = leave
dm._settle_sigusr1 = settle

class Leaving(dict):
    def pop(self, key, *default):
        value = super().pop(key, *default)
        if case == "leaving" and armed and key == built["b"]._dumps_token:
            armed.clear()
            raise KeyboardInterrupt                       # B has left the set, not yet handed on
        return value
dm.faulthandler = SimpleNamespace(register=register, unregister=faulthandler.unregister,
                                  dump_traceback_later=faulthandler.dump_traceback_later)
dm._ADVERTISED = Leaving(dm._ADVERTISED)
Daemon._write_lock = write
closing = built["c"] if three else built["b"]
settling.append(True)
try:
    closing.close()
except KeyboardInterrupt:
    pass
collected = None
if case == "collected":
    import gc, weakref
    gone = weakref.ref(built.pop("c"))                    # the host drops C, its close cut short
    closing = None
    gc.collect()
    collected = gone() is None
    names.remove("c")
if three:
    built["b"].close()                                    # B leaves; its settle repairs what C's left
reused = os.pipe()                                        # what a closed descriptor may become
os.set_blocking(reused[0], False)
logs = {name: base / name / "daemon.log" for name in names}
sizes = {name: path.stat().st_size for name, path in logs.items()}
os.kill(os.getpid(), signal.SIGUSR1)                      # `daemon stacks` against A
deadline = time.monotonic() + 5
while time.monotonic() < deadline and not any(logs[n].stat().st_size > sizes[n] for n in names):
    time.sleep(.02)
time.sleep(.2)
try:
    stray = len(os.read(reused[0], 65536))
except BlockingIOError:
    stray = 0
holder = next((name for name, core in built.items() if dm._STACK_DUMPS and dm._STACK_DUMPS() is core), None)
print(json.dumps({"grew": sorted(n for n in names if logs[n].stat().st_size > sizes[n]), "stray": stray,
                  "members": sorted(n for n, core in built.items() if core._dumps_token in dm._ADVERTISED),
                  "holder": holder, "advertised": len(dm._ADVERTISED),
                  **({"collected": collected} if collected is not None else {})}))
"""


@pytest.mark.parametrize("case,expected", [
    ("handoff", {"grew": ["a"], "stray": 0, "members": ["a"], "holder": "a", "advertised": 1}),
    ("after-rewrite", {"grew": ["a"], "stray": 0, "members": ["a"], "holder": "a", "advertised": 1}),
    ("before-rewrite", {"grew": ["b"], "stray": 0, "members": ["a", "b"], "holder": "b", "advertised": 2}),
    ("leaving", {"grew": ["a"], "stray": 0, "members": ["a"], "holder": "a", "advertised": 1}),
    ("before-leaving", {"grew": ["a"], "stray": 0, "members": ["a"], "holder": "a", "advertised": 1}),
    ("settle-entry", {"grew": ["a"], "stray": 0, "members": ["a"], "holder": "a", "advertised": 1}),
    ("collected", {"grew": ["a"], "stray": 0, "members": ["a"], "holder": "a", "advertised": 1,
                   "collected": True}),
])
def test_c3_6_a_close_interrupted_part_way_leaves_no_stream_faulthandler_holds_closed(tmp_path, case, expected):
    """Review of 9a514a7 (Opus). P2: C, closing, handed SIGUSR1 to B by registering
    B's stream, and a KeyboardInterrupt landed before it recorded B as holder; B,
    closing next, saw itself not holding the signal and closed the stream
    faulthandler held, so A's dumps were lost or went to whatever reused it. The
    holder is now recorded first, and B hands the signal on to A. P3: B's close
    was interrupted after its lock rewrite was durable, and B stayed in the set
    for good, A's dumps going to B's abandoned log. Membership now follows what
    the lock says when read back: B leaves and hands the signal to A. Interrupted
    before the rewrite, B's lock still says `stack_dumps`, and B stays, holding the
    signal, its stream open (its dumps arrive). Interrupted just after leaving the
    set, B still hands the signal on, so no non-member holds it. Review of
    674b99b (both): C's close was interrupted after its rewrite but before it
    left, or after it left but before the settle began, so C stayed in the set,
    or held the signal as a non-member, for good, and A's dumps went to C's
    abandoned log. Every leave now settles, and a settle drops a member that
    began to leave once its lock reads back without the flag: B's close repairs
    both. Review of 8ebe6b6, P3: when the host dropped C and it was collected
    before that settle, C could never be judged and stayed in the set for good;
    what a settle needs of a member now lives in its entry. In a subprocess, so
    a regression's stray dump lands in that process."""
    import os
    import subprocess
    import sys
    repo = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH" and not k.startswith("SUBFLEET_")}
    env["PYTHONPATH"] = str(repo)
    done = subprocess.run([sys.executable, "-c", CLOSE_INTERRUPTED, str(tmp_path), case], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, (done.returncode, done.stderr[-2000:])
    assert json.loads(done.stdout.strip().splitlines()[-1]) == expected



def test_c3_6_a_failed_construction_whose_revoke_raises_keeps_its_stream(tmp_path, monkeypatch, isolated_dumps):
    """Review of 9a514a7, P2 (GPT): construction failed after the daemon took
    SIGUSR1, and its cleanup's revoke raised; the cleanup loop swallowed that, and
    its "may close" default closed the stream faulthandler held. It stays open
    now, unless the revoke returns saying it may close."""
    built = []
    real_enable = Daemon._enable_stack_dumps

    def enable(self):
        built.append(self)
        return real_enable(self)

    def broken(path):
        raise RuntimeError("policy unreadable")

    def revoke(self):
        raise RuntimeError("an exception inside the revoke")
    monkeypatch.setattr(Daemon, "_enable_stack_dumps", enable)
    monkeypatch.setattr(daemon_module, "load_policy", broken)
    monkeypatch.setattr(Daemon, "_disable_stack_dumps", revoke)
    with pytest.raises(RuntimeError, match="policy unreadable"):
        Daemon(tmp_path / "failed")
    stream = built[0]._log_handler.stream
    try:
        assert daemon_module._STACK_DUMPS() is built[0] and not stream.closed
    finally:
        with daemon_module._DUMPS_LOCK:                     # let it go, then close what it held
            daemon_module._ADVERTISED.pop(built[0]._dumps_token, None)
            daemon_module._settle_sigusr1()
        stream.close()


def test_c3_6_a_settle_prunes_only_a_member_whose_lock_may_no_longer_say_stack_dumps(tmp_path, isolated_dumps):
    """Review of 8ebe6b6, P3: what a settle needs of a member lives in its entry, so
    one whose daemon object is gone is judged too. The rule, case by case: a member
    that began to leave, or whose daemon is gone, leaves once its lock, read back
    by path, cannot say `stack_dumps` (never came to write the flag, reads back
    without it, or is missing); one whose lock reads back with the flag, or cannot
    be read, stays; a live member not leaving always stays (it may be between its
    take and its flagged write)."""
    import weakref

    class Member:
        pass
    assert not daemon_module._ADVERTISED
    live = Member()

    def lock(name, content):
        path = tmp_path / name / "daemon.lock"
        path.parent.mkdir()
        if content == "unreadable":
            path.mkdir()                                    # reading it raises IsADirectoryError
        elif content is not None:
            path.write_text(json.dumps(content))
        return path
    flagged, plain = {"pid": 1, "stack_dumps": True}, {"pid": 1}
    cases = {                       # name: (alive, leaving, may_advertise, lock content, stays)
        "dead-flagged": (False, False, True, flagged, True),
        "dead-plain": (False, False, True, plain, False),
        "dead-never-flagged": (False, False, False, flagged, False),
        "dead-missing": (False, False, True, None, False),
        "dead-unreadable": (False, False, True, "unreadable", True),
        "live-plain": (True, False, True, plain, True),
        "leaving-plain": (True, True, True, plain, False),
        "leaving-flagged": (True, True, True, flagged, True),
    }
    streams = []
    with daemon_module._DUMPS_LOCK:
        for token, (name, (alive, leaving, may, content, _)) in enumerate(cases.items()):
            state = daemon_module._DumpsState(lock(name, content), leaving=leaving, may_advertise=may)
            ref = weakref.ref(live) if alive else weakref.ref(Member())        # a dead reference
            stream = open(tmp_path / f"{name}.log", "a")
            streams.append(stream)
            daemon_module._ADVERTISED[("case", token)] = (ref, stream, state)
        try:
            daemon_module._settle_sigusr1()
            stayed = {name for token, name in enumerate(cases) if ("case", token) in daemon_module._ADVERTISED}
        finally:
            for token in range(len(cases)):
                daemon_module._ADVERTISED.pop(("case", token), None)
            daemon_module._settle_sigusr1()                 # no member: the handler goes
    for stream in streams:
        stream.close()
    assert stayed == {name for name, case in cases.items() if case[-1]}, stayed
