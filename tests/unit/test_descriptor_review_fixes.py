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
    result = {}
    waiter = threading.Thread(target=lambda: result.update(daemon.wait(
        protocol.WaitArgs(job_ids=[job], deadline_s=.6),
        client_gone=(lambda: False) if with_client else None)))
    waiter.start()
    time.sleep(.15)
    commit_elsewhere(daemon, job)
    waiter.join(5)
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

def test_c3_6_a_failed_construction_clears_stack_dumps_before_it_lets_the_signal_go(monkeypatch):
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


def test_c16_7_a_connect_refused_after_busy_reports_busy_not_an_absent_daemon(tmp_path, monkeypatch):
    """A full listen backlog behind a busy daemon refuses the connect; that is not
    an absent daemon, and must not send a caller to offline mode (#55)."""
    monkeypatch.setattr(client_module, "_sleep", lambda seconds: None)
    client = Scripted(tmp_path, busy(), DaemonUnavailable("connection refused"))
    with pytest.raises(DaemonError) as caught:
        client.call("list", timeout=10)
    assert caught.value.busy
    fresh = Scripted(tmp_path, DaemonUnavailable("connection refused"))
    with pytest.raises(DaemonUnavailable):                   # with no busy answer first, it is absent
        fresh.call("list", timeout=10)


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
    assert counts(service)["no_thread"] == 1


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
