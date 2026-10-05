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
from contextlib import suppress
from pathlib import Path

import pytest

from subfleet import daemon as daemon_module
from subfleet.client import SEND_MET_CLOSE_ERRNOS, Client, DaemonError, OutcomeUnknown, ResponseLost
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
    # sends the request again in the first half of its deadline, then reports busy.
    client = Client(service.root, timeout=.6)
    client._checked = True                  # the lock records this fixture's fake boot identity
    refused = counts(service)["refused"]
    started = time.monotonic()
    with pytest.raises(DaemonError) as caught:
        client.call("ping")
    assert caught.value.busy and caught.value.code == 69 and "busy" in str(caught.value)
    assert time.monotonic() - started < 1.5
    assert counts(service)["refused"] - refused >= 2     # it tried again at least once
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
    send(departed_write, "ping", text="left before the reply", session_id="s-departed")
    live = connect(service)
    send(live, "list")
    until(lambda: service.requests._work_queue.qsize() == 3)
    departed_read.close()
    departed_write.close()
    release.set()
    assert reply(blocker)["result"] == {"held": True}
    assert "jobs" in reply(live)["result"]
    # Each connection has its own reader, so the queued two may run in either order,
    # and the departed write may finish after `live` is answered; the departed read
    # is dropped by its reader or by the pool, neither ordered before that answer.
    until(lambda: sorted(ran[1:]) == ["list", "ping"] and counts(service)["abandoned"] == 1)
    assert ran[0] == "readings" and "daemon.status" not in ran
    notes = until(lambda: service.store.query("SELECT session_id, text FROM service_notices"))
    assert [(row["session_id"], row["text"]) for row in notes] == [("s-departed", "left before the reply")]
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


def test_c16_7_busy_retries_stay_in_the_first_half_of_the_deadline(monkeypatch):
    """C-16.7 property, over seeded deadlines and busy streaks (a stub daemon that is
    always busy, and a clock the test advances, with slow answers and overrun
    sleeps as on a loaded CI runner): no retry starts after half the deadline, the
    first try gets the whole deadline, and a retry gets what is left, at least
    half and never a non-positive socket timeout (CI 36348531450 hit one)."""
    import random
    from subfleet import client as client_module
    rng = random.Random(1607)
    for case in range(2000):
        deadline = rng.choice([.2, 1, 5, 15, 75, rng.uniform(.05, 120)])
        now = [1000.0]
        tries = []

        def once(self, op, args, *, request_id, timeout, stated):
            tries.append((now[0] - 1000.0, timeout, stated))
            # Usually at once; on a loaded machine a busy answer can take a while.
            now[0] += rng.choice([rng.uniform(0, .01), rng.uniform(0, deadline)])
            raise DaemonError(69, "the daemon is busy", "try again shortly")
        monkeypatch.setattr(client_module, "_clock", lambda: now[0])
        # Sleeps overrun on a loaded machine, sometimes by several times the pause.
        monkeypatch.setattr(client_module, "_sleep",
                            lambda s: now.__setitem__(0, now[0] + s * rng.choice([1, 1, rng.uniform(1, 40)])))
        monkeypatch.setattr(Client, "_call_once", once)
        with pytest.raises(DaemonError):
            Client("/nonexistent", timeout=deadline).call("ping")
        assert tries[0] == (0.0, deadline, deadline), case
        for started_at, timeout, stated in tries[1:]:
            assert started_at <= deadline / 2 + 1e-9, (case, deadline, tries)
            assert timeout >= deadline / 2 - 1e-9 and stated == deadline, (case, tries)
            assert timeout > 0, (case, tries)               # a socket timeout must be positive
            assert started_at + timeout <= deadline + 1e-9, (case, tries)   # and ends by the deadline
        monkeypatch.undo()


def test_c16_3_a_lost_answer_names_the_callers_deadline_even_on_a_slow_machine(serve, monkeypatch):
    """C-16.3 the message is the caller's deadline, not what was left of it when the
    try began (a local run at load 160 read "within 0.999999s")."""
    service = serve()
    real = service.dispatch
    service.dispatch = lambda op, args, **kw: (time.sleep(1.5), real(op, args, **kw))[1]
    client = Client(service.root, timeout=1)
    client._checked = True
    from subfleet import client as client_module
    clock = client_module._clock
    calls = [0]

    def slow_clock():                                    # time passes between reads of the clock
        calls[0] += 1
        return clock() + calls[0] * 1e-3
    monkeypatch.setattr(client_module, "_clock", slow_clock)
    with pytest.raises(ResponseLost) as lost:
        client.call("ping")
    assert str(lost.value) == "no response from the daemon within 1s"


# --- review of #43 at 32dfaefd ------------------------------------------------

def settling_client(monkeypatch, *, lookup):
    """A client whose first request is read and never answered, whose re-send
    meets only busy answers, and whose `list` lookup returns `lookup`."""
    from subfleet import client as client_module
    sent = []

    def once(self, op, args, *, request_id, **kw):
        sent.append(op)
        if op == "list":
            return {"jobs": lookup}
        if sent.count(op) == 1:
            raise ResponseLost("no response from the daemon within 15s", op=op, request_id=request_id)
        raise DaemonError(69, "the daemon is busy: it holds 512 client connections, its limit",
                          "try again shortly")
    monkeypatch.setattr(Client, "_call_once", once)
    monkeypatch.setattr(client_module, "_sleep", lambda s: None)
    return Client("/nonexistent", timeout=.2), sent


@pytest.mark.parametrize("op", ["submit", "kill"])
def test_c16_3_a_busy_resend_settles_nothing_and_the_outcome_stays_unknown(monkeypatch, op):
    """C-16.3, C-16.7 busy means the re-send was not read, so the first request's
    outcome is still unknown: never reported as busy (exit 69), which would read
    as "nothing was sent" and invite a duplicate (review of #43, finding 1)."""
    client, sent = settling_client(monkeypatch, lookup=[])
    args = {"job_id": "j"} if op == "kill" else {"request_id": "r-1"}
    with pytest.raises(OutcomeUnknown) as unknown:
        client.call_settled(op, args, request_id="r-1", minted=True, requery_timeout=.4)
    assert "was not read" in str(unknown.value)
    assert ("list" in sent) == (op == "submit")                     # a submit is looked up


def test_c16_3_a_busy_resend_of_a_minted_submit_is_answered_by_its_job(monkeypatch):
    """C-16.3 a job carrying a request id this call minted can only be its own."""
    client, _ = settling_client(monkeypatch, lookup=[{"job_id": "J1", "request_id": "r-1",
                                                      "state": "queued"}])
    result = client.call_settled("submit", {"request_id": "r-1"}, request_id="r-1",
                                 minted=True, requery_timeout=.4)
    assert result["job_id"] == "J1" and result["created"] is False and result["requeried"] is True


def test_c16_3_a_busy_resend_of_a_supplied_id_names_the_job_and_stays_unknown(monkeypatch):
    """C-16.3 under a supplied id a job may be an earlier request's (or a different
    payload's), so it is named, and the outcome of this one stays unknown."""
    client, _ = settling_client(monkeypatch, lookup=[{"job_id": "J1", "request_id": "r-1"}])
    with pytest.raises(OutcomeUnknown, match="job J1 carries request id r-1"):
        client.call_settled("submit", {"request_id": "r-1"}, request_id="r-1",
                            minted=False, requery_timeout=.4)


def test_c16_7_a_caller_can_take_busy_at_once(serve):
    """C-16.7 the prompt hooks read the store offline when the daemon is busy, so
    they ask not to wait out busy answers."""
    service = serve(max_connections=1)
    idle = connect(service)
    until(lambda: counts(service)["connections"] == 1)
    client = Client(service.root, timeout=5, retry_busy=False)
    client._checked = True
    started = time.monotonic()
    with pytest.raises(DaemonError) as caught:
        client.call("ping")
    assert caught.value.busy and time.monotonic() - started < 1
    idle.close()


def test_c16_7_a_deeply_nested_request_is_answered_not_a_dead_reader(serve, monkeypatch):
    """C-16.7 a line under 1 MiB that nests past the parser's depth is exit 2, and
    the connection goes on; before, RecursionError ended its reader thread."""
    service = serve()
    raised = []
    monkeypatch.setattr(threading, "excepthook", lambda args: raised.append(args.exc_type))
    with connect(service) as sock:
        sock.sendall(b"[" * 200000 + b"\n")
        send(sock, "ping")
        first, second = reply_lines(sock, 2)
    assert first["ok"] is False and first["error"]["code"] == 2 and "nested" in first["error"]["message"]
    assert second["result"]["pong"] is True and raised == []


def test_c16_7_a_connection_without_a_reader_is_answered_busy(serve, monkeypatch):
    """C-16.7 when no thread can be started for a connection, nothing was read,
    so the client is told busy rather than finding the connection closed."""
    service = serve()
    real_start = threading.Thread.start

    def start(thread):
        if thread.name == "subfleet-socket":
            raise RuntimeError("can't start new thread")
        return real_start(thread)
    monkeypatch.setattr(threading.Thread, "start", start)
    with connect(service) as sock:
        answer = reply(sock)
    assert answer["ok"] is False and answer["error"]["code"] == 69 and "thread" in answer["error"]["message"]


def test_c16_7_a_stopping_daemon_does_not_count_its_own_shutdown_as_departures(serve):
    """C-16.7 close()'s SHUT_RDWR makes getpeername fail as a departed client's
    would. Reads queued then are close()'s to cancel, and one a thread reaches in
    the window before the pools shut down is dropped: neither is 'client hung up'."""
    service = serve()
    service.requests.shutdown(wait=True)
    service.requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="subfleet-api")
    release = threading.Event()
    real = service.dispatch
    ran = []
    service.dispatch = lambda op, args, **kw: (release.wait(10), {"held": True})[1] if op == "readings" \
        else (ran.append(op), real(op, args, **kw))[1]
    blocker = connect(service)
    send(blocker, "readings")
    until(lambda: counts(service)["connections"] == 1)
    waiting = [connect(service) for _ in range(3)]
    for sock in waiting:
        send(sock, "daemon.status")
    until(lambda: service.requests._work_queue.qsize() == 3)
    real_stop = service.timers.stop

    def stop():
        # close() calls this after shutting every connection down and before its
        # pools shut down: the queued reads reach the thread in that window.
        release.set()
        until(lambda: service.requests._work_queue.qsize() == 0)
        real_stop()
    service.timers.stop = stop
    service.stopping.set()
    until(lambda: service._log_handler.stream.closed, timeout=15)   # close() has finished
    assert counts(service)["abandoned"] == 0 and ran == []
    for sock in [blocker, *waiting]:
        sock.close()


def test_c16_7_a_stream_ended_after_a_broken_error_reply_carries_nothing_more(serve):
    """C-16.7 `_decode`'s error replies end the stream too when one fails part way,
    so an earlier pipelined reply is not appended to the broken line."""
    service = serve(connection_idle_s=.5)
    real = service.dispatch
    service.dispatch = lambda op, args, **kw: (time.sleep(2.5), {"slow": "done"})[1] if op == "readings" \
        else real(op, args, **kw)
    sock = connect(service)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    send(sock, "readings")                                  # replies after ~2.5 s
    # 3.6 KB of bad lines fits in the daemon's receive buffer, so this send never
    # blocks, but their ~48 KB of error replies overflow the client's.
    with suppress(OSError):
        sock.sendall(b"not json\n" * 400)
    time.sleep(1.2)                                         # an error reply's send has timed out part way
    # Drain now, before the slow reply is due: had the stream not been ended,
    # that reply would find room and be appended to the broken line.
    data = b""
    sock.settimeout(5)
    with suppress(OSError):
        while chunk := sock.recv(65536):
            data += chunk
    sock.close()
    assert b'"slow"' not in data                            # nothing followed the broken line
    assert counts(service)["abandoned"] == 0                # our own shutdown is not a departure


# --- final review of #43 at daca842e ------------------------------------------

def test_c16_7_a_refused_connect_after_busy_reports_busy_not_an_absent_daemon(monkeypatch):
    """C-16.7 a full listen backlog behind a busy daemon is not an absent daemon:
    reported as busy, so no caller falls back to offline mode (an offline kill)."""
    from subfleet import client as client_module
    from subfleet.client import DaemonUnavailable
    tries = []

    def once(self, op, args, *, request_id, timeout, stated):
        tries.append(op)
        if len(tries) == 1:
            raise DaemonError(69, "the daemon is busy: it holds 512 client connections, its limit",
                              "try again shortly")
        raise DaemonUnavailable("no daemon at daemon.sock: [Errno 61] Connection refused")
    monkeypatch.setattr(Client, "_call_once", once)
    monkeypatch.setattr(client_module, "_sleep", lambda s: None)
    with pytest.raises(DaemonError) as caught:
        Client("/nonexistent", timeout=5).call("kill", {"job_id": "j"})
    assert caught.value.busy and not isinstance(caught.value, DaemonUnavailable)
    assert tries == ["kill", "kill"]

    def absent(self, op, args, **kw):
        raise DaemonUnavailable("no daemon at daemon.sock: [Errno 2] No such file")
    monkeypatch.setattr(Client, "_call_once", absent)
    with pytest.raises(DaemonUnavailable):          # with no busy answer, absent is absent
        Client("/nonexistent", timeout=5).call("kill", {"job_id": "j"})


def test_c16_7_one_call_can_take_busy_at_once(serve):
    """C-16.7 `retry_busy=False` on a call overrides the client's own setting."""
    service = serve(max_connections=1)
    idle = connect(service)
    until(lambda: counts(service)["connections"] == 1)
    client = Client(service.root, timeout=5)
    client._checked = True
    started = time.monotonic()
    with pytest.raises(DaemonError) as caught:
        client.call("ping", retry_busy=False)
    assert caught.value.busy and time.monotonic() - started < 1
    idle.close()


def test_c16_7_subfleet_wait_takes_busy_as_an_empty_poll_with_its_whole_deadline(root, monkeypatch):
    """C-16.7 `subfleet wait` asks again after a busy answer, and every poll is sent
    with its whole deadline: a retry inside `call` would have less than the 60 s
    poll it asks the daemon to hold (final review of #43, F1)."""
    import argparse
    from subfleet import cli
    from subfleet import client as client_module
    from subfleet.contracts import WAIT_POLL_MAX_S
    job = "20260927-000000-done"
    timeouts = []

    def once(self, op, args, *, request_id, timeout, stated):
        timeouts.append(timeout)
        if len(timeouts) <= 3:
            raise DaemonError(69, "the daemon is busy: it holds 512 client connections, its limit",
                              "try again shortly")
        return {"jobs": [{"job_id": job, "state": "succeeded", "rc": 0}], "timeout": False}
    monkeypatch.setattr(Client, "_call_once", once)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(client_module, "_sleep", lambda s: None)
    code = cli.wait_jobs(argparse.Namespace(json=True), [job], timeout=None, quiet=True)
    assert code == 0
    assert timeouts == [WAIT_POLL_MAX_S + 15] * 4                  # never a shortened retry


def test_c16_3_the_cli_says_how_a_busy_resend_found_the_job():
    """C-16.3 a job found by request id after a busy re-send is not "acknowledged on
    re-query": the re-send was never read."""
    from subfleet import cli
    created, note = cli._submitted({"job_id": "J1", "created": False, "requeried": True,
                                    "busy": "the daemon is busy"}, minted=True)
    assert created is True and "met a busy daemon and was not read" in note


def test_c16_7_a_stream_ended_here_frees_its_queued_reads_without_counting_a_departure(serve):
    """C-16.7 after a reply fails part way the daemon ends the stream; a read queued
    behind it can reach no one, so it is cancelled at once, freeing the place under
    the cap while the pool is still busy, and it is not counted as a client hanging
    up (final review of #43, F2)."""
    service = serve(connection_idle_s=.5)
    service.requests.shutdown(wait=True)
    service.requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="subfleet-api")
    release = threading.Event()
    real = service.dispatch
    ran = []

    def dispatch(op, args, **kw):
        ran.append(op)
        if op == "wait":
            return {"rows": ["x" * 1024] * 2048}             # 2 MiB: its send times out
        if op == "list":
            release.wait(30)                                 # holds the only request thread
        return real(op, args, **kw)
    service.dispatch = dispatch
    blocker = connect(service)
    send(blocker, "list")
    until(lambda: ran == ["list"])
    stuck = connect(service)
    stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    send(stuck, "wait", job_ids=[RUNNING], deadline_s=1)     # on the waiters pool; its reply breaks
    send(stuck, "daemon.status")                             # queued behind the blocker
    until(lambda: service.requests._work_queue.qsize() == 1)
    # The stuck connection goes while the only request thread is still held.
    until(lambda: counts(service)["connections"] == 1, timeout=5)
    assert counts(service)["abandoned"] == 0
    release.set()
    assert "jobs" in reply(blocker)["result"]
    assert "daemon.status" not in ran
    assert stuck.fileno() >= 0                               # the client itself never left
    stuck.close()
    blocker.close()


@pytest.mark.parametrize("line", [
    b'{"v":1,"op":' + b"[" * 100000 + b"]" * 100000 + b"}\n",             # nests past repr or the parser
    b'{"v":1,"op":"ping","args":{},"id":' + b"1" * 5000 + b"}\n",           # past int_max_str_digits
    b'{"v":1,"op":"ping","args":{"x":"\xff\xfe"}}\n',                      # not UTF-8
], ids=["deep-nesting", "5000-digit-integer", "not-utf8"])
def test_c16_7_every_malformed_line_is_answered_and_the_connection_goes_on(serve, monkeypatch, line):
    """C-16.7 no malformed line ends a reader: each is answered exit 2 and the next
    request on the connection is still served (final review of #43, F1)."""
    service = serve()
    raised = []
    monkeypatch.setattr(threading, "excepthook", lambda args: raised.append(args.exc_type.__name__))
    with connect(service) as sock:
        sock.sendall(line)
        send(sock, "ping")
        first, second = reply_lines(sock, 2)
    assert first["ok"] is False and first["error"]["code"] == 2
    assert second["result"]["pong"] is True and raised == []


def test_c16_7_a_read_reaching_a_thread_after_the_daemon_ended_its_stream_is_not_a_departure(serve):
    """C-16.7 `_respond` and `wait` drop a read whose stream this daemon ended itself,
    but count only a client that left (final review of #43, F2); the control, a
    peer that closed, is counted."""
    from subfleet import protocol
    service = serve()
    ran = []
    real = service.dispatch
    service.dispatch = lambda op, args, **kw: ran.append(op) or real(op, args, **kw)

    ended, peer = socket.socketpair(socket.AF_UNIX)
    service._end_stream(ended)                                # as after a broken reply
    service._respond(ended, threading.Lock(), protocol.Request(op="daemon.status", args={}, id="a"))
    assert ran == [] and counts(service)["abandoned"] == 0

    def gone():
        return True
    gone.ended_here = lambda: True
    assert service.wait(protocol.WaitArgs(job_ids=[RUNNING], deadline_s=5), client_gone=gone) == {"timeout": True}
    assert counts(service)["abandoned"] == 0

    left, other = socket.socketpair(socket.AF_UNIX)
    other.close()                                             # the control: the client left
    service._respond(left, threading.Lock(), protocol.Request(op="daemon.status", args={}, id="b"))
    assert ran == [] and counts(service)["abandoned"] == 1
    for sock in (ended, peer, left):
        sock.close()


def blocked_requests(service):
    """One request thread, held by a `list` until the returned event is set."""
    service.requests.shutdown(wait=True)
    service.requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="subfleet-api")
    release = threading.Event()
    real = service.dispatch
    ran = []

    def dispatch(op, args, **kw):
        ran.append(op)
        if op == "list":
            release.wait(30)
        return real(op, args, **kw)
    service.dispatch = dispatch
    blocker = connect(service)
    send(blocker, "list")
    until(lambda: ran == ["list"])
    return blocker, release, ran


@pytest.mark.parametrize("ending", ["broken error replies", "partial line at the end"])
def test_c16_7_a_stream_ended_by_a_broken_error_answer_frees_its_queued_read(serve, ending):
    """C-16.7 when an error answer fails part way the reader leaves through its
    OSError path, not end of stream; the queued read goes at once there too, and
    is not counted (review of #55, finding 1)."""
    service = serve(connection_idle_s=.5)
    blocker, release, ran = blocked_requests(service)
    stuck = connect(service)
    stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    send(stuck, "daemon.status")                             # queued behind the blocker
    until(lambda: service.requests._work_queue.qsize() == 1)
    with suppress(OSError):
        stuck.sendall(b"not json\n" * 400 + (b'{"v":1,"op":"pi' if ending.startswith("partial") else b""))
    until(lambda: counts(service)["connections"] == 1, timeout=5)   # freed while the thread is held
    assert counts(service)["abandoned"] == 0
    release.set()
    assert "jobs" in reply(blocker)["result"]
    assert "daemon.status" not in ran
    stuck.close()
    blocker.close()


def test_c16_1_a_request_id_with_no_utf8_form_is_still_answered(serve):
    """C-16.1 a lone surrogate id (a valid JSON escape) is echoed back escaped;
    before, encoding the reply failed and the client waited out its deadline."""
    service = serve()
    with connect(service) as sock:
        sock.sendall(b'{"v":1,"id":"\\ud800","op":"ping","args":{}}\n')
        send(sock, "ping")
        first, second = reply_lines(sock, 2)
    assert first["id"] == "\ud800" and first["result"]["pong"] is True
    assert second["result"]["pong"] is True


def test_c16_7_subfleet_wait_over_a_busy_daemon_ends_near_its_timeout(root, monkeypatch):
    """C-15.4, C-16.7 property over seeded cases: busy answers, one of them slow,
    never carry `subfleet wait --timeout T` more than 1 s past T, which is the
    bound the budget allows (review of #55, finding 3)."""
    import argparse
    import random
    from subfleet import cli
    rng = random.Random(1515)
    for case in range(200):
        now = [1000.0]
        monkeypatch.setattr(cli.time, "monotonic", lambda: now[0])
        monkeypatch.setattr(cli.time, "sleep", lambda s: now.__setitem__(0, now[0] + max(0.0, s)))
        calls = []
        slow_at = rng.randint(1, 12)
        limit = rng.choice([3.0, 10.0, 25.0])

        def once(self, op, args, *, request_id, timeout, stated):
            calls.append(timeout)
            if len(calls) == slow_at:
                now[0] += timeout - rng.uniform(0, .05)         # busy just inside the budget
            raise DaemonError(69, "the daemon is busy: it holds 512 client connections, its limit",
                              "try again shortly")
        monkeypatch.setattr(Client, "_call_once", once)
        started = now[0]
        code = cli.wait_jobs(argparse.Namespace(json=True), ["J"], timeout=limit, quiet=True)
        assert code == 124, (case, code)
        assert now[0] - started <= limit + 1.0 + 1e-9, (case, limit, now[0] - started)


# --- the release line's reconciliation reviews (Opus, Astra), 2026-09-27 ------

def test_c16_7_no_reply_waiting_on_the_lock_follows_a_broken_one(serve):
    """C-16.7 the stream is ended before the write lock is let go: a reply that was
    waiting on the lock meets a shut socket instead of being appended to the broken
    line. The client drains between the failed send and the stream's end, so a
    waiting reply would have room to go out (both reconciliation reviewers
    reproduced this with the end outside the lock)."""
    from subfleet import protocol
    service = serve()
    conn, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    conn.settimeout(.3)                                      # the send of a big reply times out
    with service._connection_lock:
        service._connections.add(conn)
    received = bytearray()

    def drain():
        peer.setblocking(False)
        with suppress(BlockingIOError):
            while chunk := peer.recv(65536):
                received.extend(chunk)
    real_end = service._end_stream

    def end_stream(c):
        drain()
        time.sleep(.3)                       # time for a reply waiting on the lock to go out, were it free
        real_end(c)
    service._end_stream = end_stream
    real = service.dispatch
    service.dispatch = lambda op, args, **kw: {"rows": ["x" * 1024] * 2048} if op == "readings" \
        else real(op, args, **kw)
    lock = threading.Lock()
    first = threading.Thread(target=service._respond,
                             args=(conn, lock, protocol.Request(op="readings", args={}, id="first")))
    first.start()
    until(lambda: lock.locked())
    second = threading.Thread(target=service._respond,
                              args=(conn, lock, protocol.Request(op="ping", args={}, id="second")))
    second.start()
    first.join(10)
    second.join(10)
    drain()
    assert b'"id":"second"' not in received
    assert conn in service._shut_down
    with service._connection_lock:
        service._connections.discard(conn)
        service._shut_down.discard(conn)
    conn.close()
    peer.close()


def test_c16_7_only_a_held_connection_is_recorded_as_ended_here(serve):
    """C-16.7 `_end_stream` on a connection the daemon already let go records
    nothing: it would never be discarded (Opus reconciliation review: a request
    queued before its reader died failed its reply after the close)."""
    service = serve()
    gone_conn, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    gone_conn.close()
    service._end_stream(gone_conn)
    assert service._shut_down == set()
    peer.close()


def test_c16_7_a_request_no_thread_could_start_for_does_not_end_its_reader(serve):
    """C-16.7 `submit` raising RuntimeError (no thread could start) is counted and
    the reader goes on: the next request on the connection is still answered."""
    service = serve()
    real_submit = service.requests.submit
    failures = [RuntimeError("can't start new thread")]

    def submit(*args, **kwargs):
        if failures:
            raise failures.pop()
        return real_submit(*args, **kwargs)
    service.requests.submit = submit
    with connect(service) as sock:
        send(sock, "ping")                                   # no thread could be started for this one
        send(sock, "readings")
        answer = reply(sock)
    assert answer["ok"] is True and "readings" in answer["result"]
    assert counts(service)["unscheduled"] == 1
