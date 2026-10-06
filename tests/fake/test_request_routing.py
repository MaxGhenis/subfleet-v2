"""C-16.5: a liveness question and a hook's reads never queue behind slow requests.

On 2026-09-25 all 16 threads of the daemon's request pool were busy, so a
`ping`, which reads nothing, waited behind them for up to 90 s, and every
hook's `list` queued with it. Now `ping` without text is answered on the
connection's own thread, and `list`, `show` and `notice.pending` run on a pool
of their own; and none of them waits for an open transaction (C-3.7).
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from subfleet import daemon as module
from subfleet.daemon import Daemon


def request(path: Path, op: str, timeout: float = 10, **args) -> tuple[dict, float]:
    started = time.monotonic()
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        client.sendall((json.dumps({"v": 1, "id": "r", "op": op, "args": args}) + "\n").encode())
        reply = json.loads(client.makefile().readline())
    return reply, time.monotonic() - started


@pytest.fixture
def served(monkeypatch):
    monkeypatch.setattr(module.procs, "boot_id", lambda: "routing-boot")
    monkeypatch.setattr(module.procs, "proc_start", lambda pid: "routing-start")
    with tempfile.TemporaryDirectory(prefix="sfq-", dir="/tmp") as temporary:
        root = Path(temporary)
        daemon = Daemon(root, tick_s=.05)
        thread = threading.Thread(target=daemon.serve_forever, daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        while True:                     # the file exists from `bind`, before `listen`
            try:
                request(root / "daemon.sock", "ping", timeout=2)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.05)
        yield daemon, root / "daemon.sock"
        daemon.stopping.set()
        thread.join(5)


def test_ping_and_hook_reads_answer_while_every_request_thread_is_busy(served):
    daemon, path = served
    release = threading.Event()
    blockers = [daemon.requests.submit(release.wait, 30) for _ in range(40)]   # the pool, and a queue behind it
    opened, done = threading.Event(), threading.Event()

    def open_transaction():
        with daemon.store.transaction("test.held") as tx:
            tx.execute("INSERT INTO leases VALUES ('lane:held','x','t',NULL)")
            opened.set()
            done.wait(30)
    writer = threading.Thread(target=open_transaction)
    writer.start()
    assert opened.wait(5)
    try:
        for op, args in (("ping", {}), ("list", {"mine": "s", "running": True}),
                         ("show", {"job_id": "20260925-000000-none"}),
                         ("notice.pending", {"session_id": "s"})):
            reply, seconds = request(path, op, **args)
            assert seconds < 5, (op, seconds)
            if op == "show":
                assert not reply["ok"] and "unknown job" in reply["error"]["message"]
            else:
                assert reply["ok"], reply
        # A request that belongs on the busy pool still waits its turn there.
        slow = {}
        waiter = threading.Thread(target=lambda: slow.update(zip(("reply", "seconds"),
                                                                 request(path, "readings", timeout=60))))
        waiter.start()
        time.sleep(.5)
        assert not slow                                     # queued behind the 40
    finally:
        done.set()
        release.set()
        writer.join(5)
    waiter.join(30)
    assert slow["reply"]["ok"]
    for blocker in blockers:
        blocker.result(5)


def test_a_ping_with_text_is_a_write_and_is_not_answered_inline(served):
    daemon, path = served
    reply, _ = request(path, "ping", text="hello", session_id="s-1")
    assert reply["ok"] and reply["result"]["notice_id"] < 0
    assert daemon.store.one("SELECT text FROM service_notices WHERE session_id='s-1'")["text"] == "hello"


def test_a_closing_daemon_answers_every_wait_before_it_hangs_up(served):
    """C-15.5: waiters sleep until the hub wakes them, so `close` wakes them first and
    lets each answer go out before it shuts the connections down."""
    daemon, path = served
    daemon.store.add_job(job_id="20260925-000000-open", request_id="r-open", payload_digest="d",
                         kind="run", state="running", workdir="/tmp", prompt_path="/tmp/p.md",
                         sandbox="read-only")
    answers = []

    def wait():
        answers.append(request(path, "wait", timeout=90, job_ids=["20260925-000000-open"], deadline_s=120))
    waiters = [threading.Thread(target=wait) for _ in range(4)]
    for waiter in waiters:
        waiter.start()
    try:
        deadline = time.monotonic() + 60
        while daemon.wait_hub.watched < 4 and time.monotonic() < deadline:
            time.sleep(.01)
        assert daemon.wait_hub.watched == 4
        started = time.monotonic()
        daemon.close()
        for waiter in waiters:
            waiter.join(10)
        assert len(answers) == 4
        for reply, _seconds in answers:
            assert reply["ok"] and reply["result"] == {"timeout": True}, reply
        assert time.monotonic() - started < 8
    finally:
        daemon.close()
        for waiter in waiters:
            waiter.join(10)
        assert not any(waiter.is_alive() for waiter in waiters)
