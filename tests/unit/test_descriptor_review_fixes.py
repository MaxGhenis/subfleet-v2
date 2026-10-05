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
        builder = threading.Thread(target=build)            # off the main thread: no SIG_IGN behind it
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
    worker = threading.Thread(target=run, args=args)     # off the main thread: no SIG_IGN beneath
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
    (exit -30). While A's flag stands, the signal now dumps into A's log. Review r4,
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


@pytest.mark.parametrize("alive,expected", [(None, "gives-up"), (True, "waits-on")])
def test_c16_7_a_cli_wait_bounds_refusals_only_while_the_lock_cannot_say(monkeypatch, alive, expected):
    """Review of 4fc5b49, P2: a refused connect after busy, with a lock that cannot say
    whether its holder lives, kept an unbounded `wait` asking for ever (a replacement
    daemon had truncated the lock and gone). It is busy for at most
    REFUSED_UNVERIFIED_MAX_S, then absent; a lock naming a living holder waits on."""
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
    code = cli.wait_jobs(args, ["20261004-000009-done"], timeout=None, quiet=True)
    if expected == "gives-up":
        assert code != 0 and polls[0] < 40 and clock[0] >= 5.0
    else:
        assert code == 0 and polls[0] == 40
