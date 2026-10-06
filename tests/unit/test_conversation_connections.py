"""C-16.7 with the desktop line's own socket ops (C-25.4, C-29.9, C-26.4).

The conversation ops share the daemon's socket, its connection cap and its idle
close. The two long polls (`conversation.events`, `conversation.watch`) hold a
thread for up to 50 s, so a poll whose client has left must end at its next
look, a queued read whose client has left must not run, and the app's pending
poll must keep its connection from the idle close. Each running turn holds its
relay connection (and a stdout read) of its own, which the cap leaves room for.

These run a real daemon core in this process on a short temp root, with its
control loop stubbed out (the `serve` fixture of test_daemon_connections.py).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from subfleet import daemon as daemon_module
from subfleet import descriptors

from test_daemon_connections import connect, counts, reply, send, serve, until  # noqa: F401 - serve: a fixture

SETTINGS = {"model": "opus", "effort": None, "fast": False, "permission": "ask", "auto_continue": True}
NEVER = 10**9                        # a cursor no change reaches, so a poll waits


def conversation(service) -> str:
    created, _ = service.conversations.store.create_conversation(
        provider="claude", workspace="/tmp", workspace_kind="in-place", settings=SETTINGS, origin="new")
    return created["conversation_id"]


def watched(service, name: str):
    """Wrap one poll handler: events for when it starts and when it returns."""
    started, ended = threading.Event(), threading.Event()
    real = getattr(service.conversations, name)

    def handler(args, peer, **kwargs):
        started.set()
        try:
            return real(args, peer, **kwargs)
        finally:
            ended.set()
    setattr(service.conversations, name, handler)
    return started, ended


def test_c16_7_a_conversation_watch_ends_when_its_client_leaves(serve):
    """C-16.7, C-25.4: a watch whose client has closed its socket ends at its store's
    next look (0.5 s), not at its 50 s deadline, and is counted as dropped."""
    service = serve()
    started, ended = watched(service, "op_conversation_watch")
    sock = connect(service)
    send(sock, "conversation.watch", after=NEVER, wait_s=50)
    assert started.wait(5)
    time.sleep(.3)                                       # it is waiting on the store now
    left = time.monotonic()
    sock.close()
    assert ended.wait(5) and time.monotonic() - left < 2
    until(lambda: counts(service)["abandoned"] == 1)
    until(lambda: counts(service)["connections"] == 0)


def test_c16_7_conversation_events_ends_when_its_client_leaves(serve):
    service = serve()
    cid = conversation(service)
    started, ended = watched(service, "op_conversation_events")
    sock = connect(service)
    send(sock, "conversation.events", conversation_id=cid, after=NEVER, wait_s=50)
    assert started.wait(5)
    time.sleep(.3)
    left = time.monotonic()
    sock.close()
    assert ended.wait(5) and time.monotonic() - left < 2
    until(lambda: counts(service)["abandoned"] == 1)
    until(lambda: counts(service)["connections"] == 0)


def test_c16_7_a_half_closed_poll_client_still_gets_its_page(serve):
    """Shutting down only the write half is not leaving: the poll runs to its answer."""
    service = serve()
    sock = connect(service)
    send(sock, "conversation.watch", after=NEVER, wait_s=1)
    sock.shutdown(socket.SHUT_WR)
    started = time.monotonic()
    page = reply(sock)
    assert page["ok"] and page["result"]["changes"] == [] and time.monotonic() - started >= .8
    assert counts(service)["abandoned"] == 0
    sock.close()


def test_c16_7_the_apps_pending_poll_keeps_its_connection_from_the_idle_close(serve):
    """C-16.7 point by point with the app's transport (one request per connection, a
    long poll of up to 50 s): with a 1 s idle close, a 3 s watch and a 3 s events poll
    are answered at their deadline, never closed early; once the reply is out and
    nothing is outstanding, the connection is closed after its idle time."""
    service = serve(connection_idle_s=1)
    cid = conversation(service)
    for op, args in (("conversation.watch", {"after": NEVER, "wait_s": 3}),
                     ("conversation.events", {"conversation_id": cid, "after": NEVER, "wait_s": 3}),
                     ("wait", {"job_ids": ["20260925-000000-still-running"], "deadline_s": 3})):
        sock = connect(service)
        sock.settimeout(10)
        started = time.monotonic()
        send(sock, op, **args)
        answer = reply(sock)
        took = time.monotonic() - started
        assert answer["ok"] and 2.5 <= took < 6, (op, answer, took)
        # Nothing outstanding and silent from here: closed after 1 to 2 s of idle.
        assert sock.recv(1) == b""
        assert time.monotonic() - started - took < 4
        sock.close()
    assert counts(service)["idle_closed"] == 3 and counts(service)["abandoned"] == 0


def test_c16_7_a_departed_clients_queued_conversation_read_is_dropped_its_write_runs(serve):
    """C-16.7 for the conversation ops: behind a stalled file pool (C-25.3), a
    `conversation.diff` whose client left is cancelled, while an `attachment.add`
    whose client left still runs, because a write's outcome is the store's."""
    service = serve()
    service.conversations.files.shutdown(wait=True)
    service.conversations.files = ThreadPoolExecutor(max_workers=1, thread_name_prefix="subfleet-files")
    release, ran = threading.Event(), []

    def handle(op, args, peer, **kwargs):
        ran.append(op)
        if op == "turn.diff":
            release.wait(10)
        return {"op": op}
    service.conversations.handle = handle
    blocker = connect(service)
    # The stall is a file op: `conversation.history` has its own pool since #127 (C-25.3).
    send(blocker, "turn.diff", message_id="m")
    until(lambda: ran == ["turn.diff"])
    departed_read, departed_write = connect(service), connect(service)
    send(departed_read, "conversation.diff", conversation_id="c")
    send(departed_write, "attachment.add", path="/nonexistent")
    until(lambda: service.conversations.files._work_queue.qsize() == 2)
    departed_read.close()
    departed_write.close()
    until(lambda: counts(service)["abandoned"] == 1)            # the read, cancelled while queued
    release.set()
    assert reply(blocker)["result"] == {"op": "turn.diff"}
    until(lambda: len(ran) == 2)
    assert ran == ["turn.diff", "attachment.add"]
    until(lambda: counts(service)["connections"] == 1)
    blocker.close()



def test_c16_7_a_departed_clients_queued_history_read_is_dropped(serve):
    """C-16.7 holds on #127's history pool too: behind two stalled
    `conversation.history` reads, a third whose client left is cancelled."""
    service = serve()
    release, ran = threading.Event(), []

    def handle(op, args, peer, **kwargs):
        ran.append((op, args.get("conversation_id")))
        if args.get("conversation_id") in ("a", "b"):
            release.wait(10)
        return {"op": op}
    service.conversations.handle = handle
    blockers = [connect(service), connect(service)]
    for sock, cid in zip(blockers, ("a", "b")):
        send(sock, "conversation.history", conversation_id=cid)
    until(lambda: len(ran) == 2)
    departed = connect(service)
    send(departed, "conversation.history", conversation_id="c")
    until(lambda: service.conversations.history_reads._work_queue.qsize() == 1)
    departed.close()
    until(lambda: counts(service)["abandoned"] == 1)
    release.set()
    for sock in blockers:
        assert reply(sock)["result"] == {"op": "conversation.history"}
        sock.close()
    until(lambda: counts(service)["connections"] == 0)
    assert sorted(cid for _, cid in ran) == ["a", "b"]

def test_c16_7_a_stopping_daemon_ends_its_conversation_polls(serve):
    """A stop does not wait out a 50 s poll: the store's wait sees `stopping`."""
    service = serve()
    started, ended = watched(service, "op_conversation_watch")
    sock = connect(service)
    send(sock, "conversation.watch", after=NEVER, wait_s=50)
    assert started.wait(5)
    stopped = time.monotonic()
    service.stopping.set()
    assert ended.wait(5) and time.monotonic() - stopped < 3
    sock.close()


class Runner:
    """A turn runner as the cap sees one: going until `finished` is set. It stops
    and joins at once when the service closes."""

    def __init__(self, attempt_id: str = "job/a1"):
        self.attempt_id = attempt_id
        self.finished = threading.Event()

    def stop(self) -> None:
        self.finished.set()

    def join(self, timeout: float) -> bool:
        return True


def test_c16_7_the_cap_leaves_room_for_each_running_turns_relay(serve, monkeypatch):
    """C-16.7, C-26.4: each turn runner still going holds its relay connection and a
    stdout read, so the cap falls by one connection per running turn (two
    descriptors) and rises again as they finish. A lower cap closes no connection it
    holds; it refuses the next one."""
    service = serve()
    monkeypatch.setattr(daemon_module.descriptors, "open_file_limits", lambda: (200, 200))
    assert service.max_connections == (200 - 64) // 2 == 68
    runners = [Runner(f"job/a{n}") for n in range(20)]
    service.conversations.runners.update({f"job/a{n}": runner for n, runner in enumerate(runners)})
    assert service.conversations.live_runners() == 20
    assert service.max_connections == descriptors.max_connections(200, live_turns=20) == 48
    status = counts(service)
    assert status["live_turns"] == 20 and status["reserve"] == 64 + 2 * 20 and status["max_connections"] == 48
    held = [connect(service) for _ in range(48)]
    try:
        until(lambda: counts(service)["connections"] == 48)
        with connect(service) as refused:
            send(refused, "ping")
            answer = reply(refused)
            assert answer["ok"] is False and answer["error"]["code"] == 69
        for runner in runners[:10]:
            runner.finished.set()                        # ten turns end: room for ten more
        assert service.max_connections == 58
        with connect(service) as admitted:
            send(admitted, "ping")
            assert reply(admitted)["result"]["pong"] is True
    finally:
        for sock in held:
            sock.close()
    until(lambda: counts(service)["connections"] == 0)


def test_c16_7_daemon_status_reports_the_turns_in_the_reserve(serve):
    service = serve()
    service.conversations.runners["job/a1"] = Runner()
    with connect(service) as sock:
        send(sock, "daemon.status")
        budget = reply(sock)["result"]["descriptors"]
    assert budget["live_turns"] == 1 and budget["reserve"] == 64 + descriptors.TURN_DESCRIPTORS
    assert json.dumps(budget)                            # plain JSON, as the CLI and doctor read it
