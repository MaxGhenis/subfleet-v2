"""C-16.7: client connections are bounded, never wait for a reader, and die with their client.

Incident, 2026-09-24 and 2026-09-25: every connection held one of 32 reader
threads for its whole life, so a 33rd waited in the pool's queue with its
descriptor open; clients timed out after 15 s and retried, each retry added a
descriptor, and each abandoned request still ran when its turn came. The
daemon reached launchd's 256 descriptors and `accept` raised EMFILE.

These run a real daemon core in this process on a short temp root, with its
background control loop stubbed out.
"""

import errno
import json
import os
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from subfleet import daemon as daemon_module
from subfleet.client import SEND_MET_CLOSE_ERRNOS, Client, DaemonError, ResponseLost
from subfleet.daemon import Daemon

RUNNING = "20260925-000000-still-running"


@pytest.fixture
def serve(monkeypatch):
    """Start a daemon core with the given connection settings; yields a starter."""
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fake-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fake-start")
    started = []
    with tempfile.TemporaryDirectory(prefix="sfc-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            with socket.socket(socket.AF_UNIX) as probe:
                probe.bind(str(root / "socket-check"))
        except PermissionError:
            pytest.skip("sandbox denies unix socket binding")

        def start(**options):
            service = Daemon(root, tick_s=.05, **options)
            service._recovery_complete.set()           # nothing here needs recovery or timers
            service._control = lambda: service.stopping.wait()
            real_job = service._job
            # A job that never finishes, so a `wait` on it lasts until its deadline.
            service._job = lambda job_id: ({"job_id": job_id, "state": "running"}
                                           if job_id == RUNNING else real_job(job_id))
            thread = threading.Thread(target=service.serve_forever, daemon=True)
            thread.start()
            started.append((service, thread))
            # The socket file exists from bind(), before listen(): wait for an answer,
            # then for that probe's connection to be let go.
            until(lambda: answered(service))
            until(lambda: counts(service)["connections"] == 0)
            return service
        try:
            yield start
        finally:
            for service, thread in started:
                service.stopping.set()
                thread.join(10)
                assert not thread.is_alive(), "serve_forever did not end"


def until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(.01)
    raise AssertionError("condition not reached in time")


def answered(service) -> bool:
    try:
        return call(service, "ping")["result"]["pong"] is True
    except OSError:
        return False


def connect(service) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(5)
    sock.connect(str(service.root / "daemon.sock"))
    return sock


def send(sock, op, **args):
    # A connection over the cap may be answered and closed before the request is
    # sent (C-16.7); the answer is still there to read, whichever errno the send met.
    try:
        sock.sendall((json.dumps({"v": 1, "id": op, "op": op, "args": args}) + "\n").encode())
    except OSError as exc:
        if exc.errno not in SEND_MET_CLOSE_ERRNOS:
            raise


def reply(sock) -> dict:
    line = b""
    while not line.endswith(b"\n"):
        chunk = sock.recv(65536)
        if not chunk:
            break
        line += chunk
    return json.loads(line)


def call(service, op, **args) -> dict:
    with connect(service) as sock:
        send(sock, op, **args)
        return reply(sock)


def counts(service) -> dict:
    return service._descriptor_status()


def test_c16_7_more_long_waits_than_the_old_reader_pool_still_leave_a_ping_answered(serve):
    """C-16.7 the incident: 40 clients in `wait` held all 32 readers and no ping was answered."""
    service = serve()
    assert service.max_connections >= 64            # this test process has a generous limit
    holders = []
    for _ in range(40):
        sock = connect(service)
        send(sock, "wait", job_ids=[RUNNING], deadline_s=60)
        holders.append(sock)
    # Setup, not the claim: each accept starts a reader thread, and on a machine
    # at a load average near 140 accepting all 40 took anywhere from 0.0 to 9.6 s
    # (2026-09-26, on this branch and on its head before main was merged in).
    until(lambda: counts(service)["connections"] >= 40, timeout=30)
    # Every held connection has a reader thread of its own (review of #43, F1).
    assert len(service._readers) == counts(service)["connections"] >= 40
    started = time.monotonic()
    assert call(service, "ping")["result"]["pong"] is True
    assert time.monotonic() - started < 2
    for sock in holders:
        sock.close()
    # Each wait sees its client gone within a poll, not at its 60 s deadline.
    until(lambda: counts(service)["connections"] == 0, timeout=5)
    assert counts(service)["abandoned"] >= 40


def test_c16_7_over_the_cap_a_client_is_answered_busy_at_once_and_served_after(serve):
    """C-16.7 the connection past the cap gets a one-line refusal now, not a 15 s silence."""
    service = serve(max_connections=3)
    idle = [connect(service) for _ in range(3)]
    until(lambda: counts(service)["connections"] == 3)
    started = time.monotonic()
    refused = call(service, "ping")
    assert time.monotonic() - started < 1
    assert refused["ok"] is False and refused["error"]["code"] == 69
    assert "busy" in refused["error"]["message"] and "3 client connections" in refused["error"]["message"]
    assert "try again shortly" in refused["error"]["fix"]
    # The CLI's client reads the refusal even when its request met a closed socket,
    # sends the request again while the deadline lasts, and reports busy after it.
    client = Client(service.root, timeout=.6)
    client._checked = True                  # the lock records this fixture's fake boot identity
    started = time.monotonic()
    with pytest.raises(DaemonError) as caught:
        client.call("ping")
    assert caught.value.busy and caught.value.code == 69 and "busy" in str(caught.value)
    assert .3 < time.monotonic() - started < 1.5
    assert counts(service)["refused"] >= 3
    idle[0].close()
    until(lambda: counts(service)["connections"] == 2)
    assert call(service, "ping")["result"]["pong"] is True
    for sock in idle[1:]:
        sock.close()


@pytest.mark.parametrize("code", sorted(SEND_MET_CLOSE_ERRNOS), ids=errno.errorcode.get)
def test_c16_7_the_client_reads_busy_whichever_errno_its_send_met(serve, monkeypatch, code):
    """C-16.7 the busy answer is read even when the send met the daemon's close.

    On macOS the send that races the daemon's answer-and-close fails now and then
    with ENOTCONN rather than EPIPE (CI, 2026-09-25: one of 300 storm clients read
    `[Errno 57]` and never looked at its answer). Each errno is forced here on the
    client's own send, in this thread only; the daemon's answer is real.
    """
    service = serve(max_connections=1)
    idle = connect(service)
    until(lambda: counts(service)["connections"] == 1)
    caller = threading.current_thread()
    real_sendall = socket.socket.sendall

    def sendall(sock, data, *flags):
        if threading.current_thread() is caller:
            raise OSError(code, os.strerror(code))
        return real_sendall(sock, data, *flags)
    client = Client(service.root, timeout=5)
    client._checked = True                  # the lock records this fixture's fake boot identity
    client.timeout = .2                     # the busy retries end with the deadline
    with monkeypatch.context() as patch:
        patch.setattr(socket.socket, "sendall", sendall)
        with pytest.raises(DaemonError) as caught:
            client.call("ping")
    assert caught.value.busy and "busy" in str(caught.value)
    idle.close()


def test_c16_7_any_other_send_error_is_still_a_lost_answer(serve, monkeypatch):
    """C-16.7 only a send that met a close is read past; another error is C-16.3's lost answer."""
    service = serve()
    caller = threading.current_thread()
    real_sendall = socket.socket.sendall

    def sendall(sock, data, *flags):
        if threading.current_thread() is caller:
            raise OSError(errno.ENOBUFS, os.strerror(errno.ENOBUFS))
        return real_sendall(sock, data, *flags)
    client = Client(service.root, timeout=5)
    client._checked = True
    with monkeypatch.context() as patch:
        patch.setattr(socket.socket, "sendall", sendall)
        with pytest.raises(ResponseLost, match="daemon connection failed"):
            client.call("ping")


def test_c16_7_an_idle_client_is_closed_but_one_awaiting_its_reply_is_not(serve):
    """C-16.7 silence with nothing outstanding ends a connection; a pending reply keeps it."""
    service = serve(connection_idle_s=.3)
    idle = connect(service)
    waiting = connect(service)
    send(waiting, "wait", job_ids=[RUNNING], deadline_s=1.5)
    assert idle.recv(1) == b""                         # closed by the daemon after ~0.3 s
    assert counts(service)["idle_closed"] >= 1
    started = time.monotonic()
    assert reply(waiting)["result"] == {"timeout": True}
    assert time.monotonic() - started > .7             # it outlived several idle periods
    idle.close()
    waiting.close()


def test_c16_7_a_departed_clients_read_is_not_run_but_its_write_still_is(serve):
    """C-16.7 a read queued for a client that hung up is dropped; a write still commits (C-16.4)."""
    service = serve()
    service.requests.shutdown(wait=True)
    service.requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="subfleet-api")
    release = threading.Event()
    ran = []
    real = service.dispatch

    def dispatch(op, args, **kwargs):
        ran.append(op)
        if op == "readings":
            release.wait(10)
            return {"held": True}
        return real(op, args, **kwargs)
    service.dispatch = dispatch
    blocker = connect(service)
    send(blocker, "readings")
    until(lambda: ran == ["readings"])
    departed_read = connect(service)
    send(departed_read, "daemon.status")
    departed_write = connect(service)
    send(departed_write, "ping", text="left before the reply")
    live = connect(service)
    send(live, "list")
    until(lambda: service.requests._work_queue.qsize() == 3)
    departed_read.close()
    departed_write.close()
    release.set()
    assert reply(blocker)["result"] == {"held": True}
    assert "jobs" in reply(live)["result"]
    # Each connection has its own reader, so the queued two may run in either order.
    assert ran[0] == "readings" and sorted(ran[1:]) == ["list", "ping"]    # not daemon.status
    assert counts(service)["abandoned"] == 1
    notes = service.store.query("SELECT text FROM service_notices")
    assert [row["text"] for row in notes] == ["left before the reply"]
    blocker.close()
    live.close()


def test_c16_7_a_half_closed_client_still_gets_its_reply(serve):
    """C-16.7 shutting down only the write half is not leaving: the reply is still sent."""
    service = serve()
    real = service.dispatch
    service.dispatch = lambda op, args, **kw: (time.sleep(.3), real(op, args, **kw))[1]
    with connect(service) as sock:
        send(sock, "list")
        sock.shutdown(socket.SHUT_WR)
        assert "jobs" in reply(sock)["result"]
    assert counts(service)["abandoned"] == 0


def test_c16_7_an_oversized_request_is_refused_and_the_connection_goes_on(serve):
    """C-16.1 a request over 1 MiB is answered once, and its tail is not read as a request."""
    service = serve()
    with connect(service) as sock:
        sock.sendall(b'{"v":1,"op":"ping","args":{"text":"' + b"x" * (1024 * 1024) + b'"}}\n')
        send(sock, "ping")
        first, second = reply_lines(sock, 2)
    assert first["ok"] is False and "exceeds 1 MiB" in first["error"]["message"]
    assert second["result"]["pong"] is True
    assert service.store.query("SELECT * FROM service_notices") == []


def reply_lines(sock, n):
    data = b""
    while data.count(b"\n") < n:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    return [json.loads(line) for line in data.splitlines()[:n]]


def test_c16_6_daemon_status_reports_the_descriptor_budget(serve):
    """C-16.6, C-16.7 `daemon.status` says the limit, what is open, and what the cap has done."""
    service = serve(max_connections=7, connection_idle_s=9)
    result = call(service, "daemon.status")["result"]["descriptors"]
    assert result["max_connections"] == 7 and result["idle_s"] == 9
    assert result["connections"] == 1                  # this request's own connection
    assert isinstance(result["open"], int) and result["open"] > 0
    assert result["soft_limit"] is None or result["soft_limit"] >= result["open"]
    assert {"accepted", "refused", "idle_closed", "abandoned", "accept_failures"} <= set(result)


def test_c16_7_the_cap_follows_the_open_file_limit(serve, monkeypatch):
    """C-16.7 with launchd's 256 the daemon holds at most 96 connections."""
    monkeypatch.setattr(daemon_module.descriptors, "open_file_limits", lambda: (256, 1 << 63))
    service = serve()
    assert service.max_connections == 96


def test_c16_7_a_busy_answer_is_retried_until_the_daemon_has_room(serve):
    """C-16.7 `busy` is not an outcome: the client sends the same request again,
    and it is answered once a place under the cap comes free within the deadline."""
    service = serve(max_connections=1)
    idle = connect(service)
    until(lambda: counts(service)["connections"] == 1)
    threading.Timer(.4, idle.close).start()
    client = Client(service.root, timeout=5)
    client._checked = True
    started = time.monotonic()
    assert client.call("ping")["pong"] is True
    assert .3 < time.monotonic() - started < 3
    assert counts(service)["refused"] >= 1


def test_c16_7_a_departed_clients_queued_reads_give_up_their_place_at_once(serve):
    """C-16.7 when a client hangs up, its reads still waiting for a pool thread are
    cancelled, so its connection and its place under the cap go now, not when a
    stalled pool reaches them (review of #43, R1)."""
    service = serve()
    service.requests.shutdown(wait=True)
    service.requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="subfleet-api")
    release = threading.Event()
    real = service.dispatch
    ran = []

    def dispatch(op, args, **kwargs):
        ran.append(op)
        if op == "readings":
            release.wait(10)
            return {"held": True}
        return real(op, args, **kwargs)
    service.dispatch = dispatch
    blocker = connect(service)
    send(blocker, "readings")
    until(lambda: ran == ["readings"])
    gone = [connect(service) for _ in range(3)]
    for sock in gone:
        send(sock, "daemon.status")
    until(lambda: service.requests._work_queue.qsize() == 3)
    for sock in gone:
        sock.close()
    # Freed while the only request thread is still stalled.
    until(lambda: counts(service)["connections"] == 1, timeout=5)
    assert counts(service)["abandoned"] == 3 and ran == ["readings"]
    release.set()
    assert reply(blocker)["result"] == {"held": True}
    blocker.close()


def test_c16_7_a_client_that_stops_reading_cannot_hold_a_thread_for_ever(serve):
    """C-16.7 the idle timeout also bounds sending a reply, so a stopped client
    (Ctrl-Z on `subfleet list`) frees its pool thread and its descriptor."""
    service = serve(connection_idle_s=.5)
    real = service.dispatch
    big = {"rows": ["x" * 1024] * 2048}                  # 2 MiB: far past the socket buffers
    asked = threading.Event()

    def dispatch(op, args, **kw):
        if op != "readings":
            return real(op, args, **kw)
        asked.set()
        return big
    service.dispatch = dispatch
    stuck = connect(service)
    stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    send(stuck, "readings")                              # and never read the reply
    assert asked.wait(5)
    assert counts(service)["connections"] == 1           # the reply is being sent, into a full buffer
    until(lambda: counts(service)["connections"] == 0, timeout=5)
    assert call(service, "ping")["result"]["pong"] is True
    stuck.close()


def test_c16_7_shutdown_leaves_no_client_connection_open(serve, monkeypatch):
    """C-16.7 close() waits a bounded time for readers, then closes every connection
    it accepted, including those of a reader that has not returned."""
    monkeypatch.setattr(daemon_module, "READER_JOIN_S", .5)
    service = serve()
    wedged = threading.Event()
    real = service._connection

    def connection(conn):
        if len(service._readers) > 5:           # the last five readers never return
            wedged.wait(10)
            return
        real(conn)
    service._connection = connection
    held = [connect(service) for _ in range(10)]
    until(lambda: counts(service)["connections"] == 10)
    started = time.monotonic()
    service.stopping.set()
    until(lambda: service._closed and not service._connections, timeout=5)
    assert time.monotonic() - started < 3                # the join was bounded
    for sock in held:
        assert sock.recv(1) == b""                       # closed by the daemon, not left open
        sock.close()
    wedged.set()


def test_c16_7_idle_is_counted_from_the_last_reply_not_the_last_read(serve):
    """C-16.7 a request that ran almost the whole idle period, followed by another
    shortly after its reply, is served: silence is measured from the reply."""
    service = serve(connection_idle_s=1.0)
    real = service.dispatch
    service.dispatch = lambda op, args, **kw: (time.sleep(.8), {"slow": True})[1] if op == "readings" \
        else real(op, args, **kw)
    with connect(service) as sock:
        send(sock, "readings")
        assert reply(sock)["result"] == {"slow": True}    # at ~0.8 s; the read timed out at 1.0 s
        time.sleep(.4)                                     # 1.2 s since the last read, 0.4 s since the reply
        send(sock, "ping")
        assert reply(sock)["result"]["pong"] is True


def test_c16_7_nothing_follows_a_reply_that_failed_part_way(serve):
    """C-16.7 once a reply's send times out part way, the connection carries nothing
    more: a later reply would be appended to a broken line."""
    service = serve(connection_idle_s=.5)
    real = service.dispatch
    asked = threading.Event()

    def dispatch(op, args, **kw):
        if op != "readings":
            return real(op, args, **kw)
        asked.set()
        return {"rows": ["x" * 1024] * 2048}             # 2 MiB: its send times out
    service.dispatch = dispatch
    stuck = connect(service)
    stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    send(stuck, "readings")
    assert asked.wait(5)
    time.sleep(.8)                                         # past the 0.5 s send timeout
    send(stuck, "ping")
    data = b""
    stuck.settimeout(5)
    while chunk := stuck.recv(65536):
        data += chunk
    assert b'"pong"' not in data and not data.endswith(b"}\n")
    stuck.close()
