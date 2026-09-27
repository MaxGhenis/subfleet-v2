"""C-16.8: a stopping daemon answers every request it read, then lets go.

Review of #43, 2026-09-25: `close()` shut every client socket (SHUT_RDWR) before
it drained the pools. A submit or kill still running committed, then answered
into a socket already shut down; its client read "the daemon closed the
connection without a response", sent the request again to a daemon that was
gone, and reported the outcome unknown although the write had happened.

These run a real daemon core in this process, with its background control loop
stubbed out, as `test_daemon_connections.py` does.

Invariants, for any moment of the stop and any mix of requests:
- answered: every request a client sent on a connection the daemon held gets a
  line: its real reply, or a code-69 answer saying it was not run. None reads end
  of stream, unless its handler is still running at the reply deadline;
- truthful: 69 is given only to a request that did nothing, so a write answered
  69 left no row, and a write that committed got its real reply;
- bounded: with no handler wedged, `close()` ends within the reply deadline.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import uuid

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
import pytest

from subfleet import daemon as daemon_module
from subfleet.client import Client, DaemonError, DaemonUnavailable, SEND_MET_CLOSE_ERRNOS
from subfleet.contracts import STOP_GRACE_S, STOP_REPLY_S, Credential, Exit, Lane, LaneOwner
from subfleet.daemon import READER_JOIN_S, STOPPING_MESSAGE, Daemon, Stopping
from subfleet.store import Store

#: How much later than asked a step may finish on a loaded machine (load
#: averages above 140 were seen while these were written, 2026-09-27).
SLACK_S = 5.0


@pytest.fixture
def daemon():
    """Start daemon cores on one short temp root; yields `start(**options)`.

    The daemon records its real identity in `daemon.lock`, because `Client`
    checks it before it sends (C-5.8)."""
    started = []
    with tempfile.TemporaryDirectory(prefix="sfs-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            with socket.socket(socket.AF_UNIX) as probe:
                probe.bind(str(root / "socket-check"))
        except PermissionError:
            pytest.skip("sandbox denies unix socket binding")

        def start(**options):
            options.setdefault("desktop_prober", lambda: None)
            service = Daemon(root, tick_s=.05, **options)
            service._recovery_complete.set()           # nothing here needs recovery or timers
            service._control = lambda: service.stopping.wait()
            service.serving = threading.Thread(target=service.serve_forever, daemon=True)
            service.serving.start()
            started.append(service)
            until(lambda: answered(service))
            until(lambda: counts(service)["connections"] == 0)
            return service
        try:
            yield start
        finally:
            for service in started:
                service.stopping.set()
                service.serving.join(30)
                assert not service.serving.is_alive(), "serve_forever did not end"


def until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(.01)
    raise AssertionError("condition not reached in time")


def answered(service) -> bool:
    try:
        with connect(service) as sock:
            send(sock, "ping")
            return reply(sock)["result"]["pong"] is True
    except OSError:
        return False


def connect(service) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(10)
    sock.connect(str(service.root / "daemon.sock"))
    return sock


def send(sock, op, request_id=None, **args):
    # As `Client.call` does: a send that met the daemon's close is read past (C-16.7).
    try:
        sock.sendall((json.dumps({"v": 1, "id": request_id or op, "op": op, "args": args}) + "\n").encode())
    except OSError as exc:
        if exc.errno not in SEND_MET_CLOSE_ERRNOS:
            raise


def lines_until_closed(sock) -> list[dict]:
    data = b""
    while chunk := sock.recv(65536):
        data += chunk
    return [json.loads(line) for line in data.splitlines()]


def reply(sock) -> dict | None:
    """The first line the daemon sends, or None for end of stream without one.

    What follows it is left unread, as `Client` leaves it: a stopping daemon
    ends each connection with one more line (C-16.8)."""
    data = b""
    while b"\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    line = data.split(b"\n", 1)[0]
    return json.loads(line) if line.strip() else None


def counts(service) -> dict:
    return service._descriptor_status()


def stop(service) -> float:
    """Stop as SIGTERM does (set `stopping`; serving ends and calls close()); return its duration."""
    began = time.monotonic()
    service.stopping.set()
    service.serving.join(STOP_REPLY_S + READER_JOIN_S + 2 + SLACK_S)
    assert not service.serving.is_alive(), "close() did not return"
    return time.monotonic() - began


def not_run(line) -> bool:
    return (line is not None and line["ok"] is False and line["error"]["code"] == int(Exit.DAEMON_UNAVAILABLE)
            and line["error"]["message"] == STOPPING_MESSAGE)


def submit_args(root: Path, request_id: str) -> dict:
    """What `subfleet run` sends, pointed at a scratch directory (`--allow-tmp`)."""
    work = root / "work"
    work.mkdir(exist_ok=True)
    prompt = root / "prompt.md"
    prompt.write_text("Summarise the scratch directory.\n")
    return {"request_id": request_id, "kind": "dispatch", "workdir": str(work),
            "prompt_path": str(prompt), "sandbox": "read-only", "task": "review",
            "tier": "standard", "allow_tmp": True}


def rows(root: Path, sql: str, params=()) -> list[dict]:
    store = Store(root / "state.sqlite3")
    try:
        return [dict(row) for row in store.query(sql, params)]
    finally:
        store.close()


def held(service, name: str):
    """Hold `service.<name>` at its start until released; returns (entered, release)."""
    entered, release = threading.Event(), threading.Event()
    real = getattr(service, name)

    def hold(*args, **kwargs):
        entered.set()
        assert release.wait(30), f"{name} was never released"
        return real(*args, **kwargs)
    setattr(service, name, hold)
    return entered, release


def call_in_thread(fn, *args, **kwargs):
    """Run a client call on a thread; returns a function that joins it and gives its outcome."""
    outcome = {}

    def run():
        try:
            outcome["result"] = fn(*args, **kwargs)
        except Exception as exc:                  # noqa: BLE001 - the test inspects it
            outcome["error"] = exc
    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def result(timeout=30):
        thread.join(timeout)
        assert not thread.is_alive(), "the client call did not return"
        return outcome
    return result


# --- the finding ---------------------------------------------------------------

def test_c16_8_a_submit_running_when_the_stop_begins_gets_its_real_reply(daemon):
    """C-16.8, C-16.3: the incident shape. The submit commits during the stop's
    drain and its client reads the real answer: the job, created, asked once."""
    service = daemon()
    entered, release = held(service, "submit")
    lost = []
    outcome = call_in_thread(Client(service.root).call_settled, "submit",
                             submit_args(service.root, "r-in-flight"), request_id="r-in-flight",
                             minted=True, on_lost=lost.append)
    assert entered.wait(10)
    service.stopping.set()                         # SIGTERM, as `stop_request` sets it
    until(lambda: service._closed)                 # close() has begun
    release.set()                                  # the submit commits now, mid-drain
    result = outcome()
    assert "error" not in result, result
    answer = result["result"]
    assert answer["created"] is True and answer["request_id"] == "r-in-flight"
    assert "requeried" not in answer and lost == []   # answered the first time
    service.serving.join(STOP_REPLY_S + SLACK_S)
    assert not service.serving.is_alive()
    [job] = rows(service.root, "SELECT job_id, state FROM jobs WHERE request_id=?", ("r-in-flight",))
    assert job == {"job_id": answer["job_id"], "state": "queued"}


def test_c16_8_a_kill_running_when_the_stop_begins_gets_its_real_reply(daemon):
    """C-16.8, C-7.1: a cancel that commits during the drain is answered, not lost."""
    service = daemon()
    job_id = service.dispatch("submit", submit_args(service.root, "r-to-kill"))["job_id"]
    entered, release = held(service, "kill")
    outcome = call_in_thread(Client(service.root).call_settled, "kill", {"job_id": job_id})
    assert entered.wait(10)
    service.stopping.set()
    until(lambda: service._closed)
    release.set()
    result = outcome()
    assert result.get("result") == {"job_id": job_id, "status": "cancel requested"}, result
    service.serving.join(STOP_REPLY_S + SLACK_S)
    [job] = rows(service.root, "SELECT state, rc FROM jobs WHERE job_id=?", (job_id,))
    assert job == {"state": "cancelled", "rc": 130}


def test_c16_8_a_reply_owed_holds_the_drain_even_while_its_reader_is_slow(daemon):
    """C-16.8: the drain waits for the reply a pool still owes, not for readers:
    a connection whose reader has not yet returned still gets its real reply."""
    service = daemon()
    entered, release = held(service, "submit")
    returned = threading.Event()
    real_connection = service._connection

    def slow_reader(conn):
        real_connection(conn)
        returned.wait(30)                          # the reader returns only after the reply
    service._connection = slow_reader
    outcome = call_in_thread(Client(service.root).call_settled, "submit",
                             submit_args(service.root, "r-slow-reader"), request_id="r-slow-reader",
                             minted=True)
    assert entered.wait(10)
    service.stopping.set()
    until(lambda: service._closed)
    # Past the readers' bounded join, into the drain: this reader has not
    # returned, so only the reply the pool owes keeps the connection open.
    time.sleep(READER_JOIN_S + .5)
    release.set()
    result = outcome()
    returned.set()
    assert result.get("result", {}).get("created") is True, result
    assert "requeried" not in result["result"]
    stop(service)


# --- what the stop keeps out, and how it says so --------------------------------

def test_c16_8_a_request_queued_when_the_stop_begins_is_not_run_and_says_so(daemon):
    """C-16.8: a request still waiting for a thread is not started; its client reads
    69, sends it again, meets no daemon and says so, never an unknown outcome."""
    service = daemon()
    service.requests.shutdown(wait=True)
    service.requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="subfleet-api")
    entered, release = threading.Event(), threading.Event()
    real = service.dispatch

    def dispatch(op, args, **kwargs):
        if op == "readings":
            entered.set()
            assert release.wait(30)
            return {"held": True}
        return real(op, args, **kwargs)
    service.dispatch = dispatch
    blocker = connect(service)
    send(blocker, "readings")
    assert entered.wait(10)
    text = f"queued behind the blocker {uuid.uuid4()}"
    outcome = call_in_thread(Client(service.root, timeout=4).call, "ping", {"text": text})
    until(lambda: service.requests._work_queue.qsize() == 1)
    service.stopping.set()
    until(lambda: service._closed)
    release.set()
    assert reply(blocker)["result"] == {"held": True}      # the running one still answers
    error = outcome().get("error")
    assert isinstance(error, DaemonUnavailable), error
    assert f"before that it answered: {STOPPING_MESSAGE}" in str(error)
    stop(service)
    assert rows(service.root, "SELECT * FROM service_notices WHERE text=?", (text,)) == []
    assert counts(service)["not_run"] >= 1
    blocker.close()


def test_c16_8_a_request_whose_turn_comes_after_the_stop_began_is_not_run(core):
    """C-16.8: a handler a pool thread reaches once `stopping` is set runs nothing
    and answers 69 under its own request id."""
    ran = []
    core.dispatch = lambda op, args, **kwargs: ran.append(op) or {}
    server, client = socket.socketpair()
    core.stopping.set()
    core._respond(server, threading.Lock(), daemon_module.protocol.Request(op="ping", args={"text": "x"}, id="r-late"))
    line = reply(client)
    assert ran == [] and not_run(line) and line["id"] == "r-late"
    server.close()
    client.close()


def test_c16_8_a_request_sent_on_a_held_connection_after_the_stop_is_answered(daemon):
    """C-16.8: a connection held when the stop begins whose request comes later (or
    whose unread bytes SHUT_RD discards) reads 69 before the daemon closes it."""
    service = daemon()
    idle = connect(service)
    until(lambda: counts(service)["connections"] == 1)
    service.stopping.set()
    until(lambda: service._closed)
    text = f"sent after the stop {uuid.uuid4()}"
    send(idle, "ping", text=text)                  # may block until the daemon closes, then EPIPE
    # Read before SHUT_RD, it is answered itself, then the connection's last line
    # follows; sent after, only the last line answers it. Either way: 69, then closed.
    answers = lines_until_closed(idle)
    assert answers and all(not_run(line) for line in answers), answers
    stop(service)
    assert rows(service.root, "SELECT * FROM service_notices WHERE text=?", (text,)) == []
    idle.close()


def test_c16_8_the_listen_backlog_is_answered_not_dropped(daemon):
    """C-16.8: a connection still in the listen backlog when the listener closes is
    accepted and told nothing ran; closing the listener alone gives it end of stream."""
    service = daemon()
    gate = threading.Event()
    real_hold = service._hold_connection

    def hold_after_the_gate(conn):
        gate.wait(30)                              # the accept loop sits here, so later connects queue
        real_hold(conn)
    service._hold_connection = hold_after_the_gate
    first = connect(service)                       # accepted; the loop now waits at the gate
    queued = []
    for _ in range(3):
        sock = connect(service)                    # in the backlog: connect succeeds, nothing accepts
        send(sock, "ping", text=f"backlog {uuid.uuid4()}")
        queued.append(sock)
    closing = threading.Thread(target=service.close, daemon=True)
    closing.start()                                # close() answers the backlog, then the gate opens
    for sock in queued:
        assert not_run(reply(sock))
        sock.close()
    gate.set()
    closing.join(STOP_REPLY_S + SLACK_S)
    assert not closing.is_alive()
    assert not_run(reply(first))                   # accepted as the stop began: answered too
    first.close()
    assert rows(service.root, "SELECT * FROM service_notices WHERE text LIKE 'backlog %'") == []


def test_c16_8_a_wait_answers_at_once_when_the_stop_begins(daemon):
    """C-16.8, C-5.11: a long poll returns its timeout answer when the stop begins."""
    service = daemon()
    job_id = service.dispatch("submit", submit_args(service.root, "r-waited"))["job_id"]
    sock = connect(service)
    send(sock, "wait", job_ids=[job_id], deadline_s=60)
    until(lambda: counts(service)["connections"] == 1)
    time.sleep(.3)                                 # into the poll
    began = time.monotonic()
    duration = stop(service)
    assert reply(sock)["result"] == {"timeout": True}
    assert time.monotonic() - began < 2 + SLACK_S and duration < 2 + SLACK_S
    sock.close()


# --- the stop stays bounded ------------------------------------------------------

def test_c16_8_a_client_that_stops_reading_cannot_hold_the_stop(daemon):
    """C-16.8, C-16.7: a reply blocked on a client that never reads fails at the
    reply deadline; the stop does not wait out C-16.7's 60 s send timeout."""
    service = daemon(stop_reply_s=.5)
    real = service.dispatch
    asked = threading.Event()

    def dispatch(op, args, **kwargs):
        if op != "readings":
            return real(op, args, **kwargs)
        asked.set()
        return {"rows": ["x" * 1024] * 2048}       # 2 MiB: far past the socket buffers
    service.dispatch = dispatch
    stuck = connect(service)
    stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    send(stuck, "readings")                        # and never read the reply
    assert asked.wait(10)
    time.sleep(.2)                                 # the reply is blocked on the full buffer
    duration = stop(service)
    assert duration < .5 + READER_JOIN_S + 2 + SLACK_S, duration
    assert service.connection_idle_s == 60 and duration < service.connection_idle_s
    log = (service.root / "daemon.log").read_text()
    assert "request(s) still unanswered" in log and "their connections were shut down" in log
    stuck.close()


def test_c16_8_the_reply_deadline_leaves_the_grace_for_the_pools():
    """C-16.8, C-5.8a: close()'s own bounded waits (control join, readers, replies)
    end well inside the grace, so a stop that is only slow to reply ends cleanly."""
    control_join_s = 2
    assert STOP_REPLY_S + control_join_s + READER_JOIN_S < STOP_GRACE_S
    assert STOP_REPLY_S <= STOP_GRACE_S / 3


@pytest.fixture
def core(tmp_path):
    """A daemon core that is not serving, so setting `stopping` starts no close()."""
    service = Daemon(tmp_path / "state", desktop_prober=lambda: None)
    yield service
    service.close()


def test_c16_8_quarantine_resolution_is_not_reported_requested_once_stopping(core):
    """C-16.8: `kill --confirm-dead` schedules work held only in memory; once the stop
    has begun it answers 69 instead of "resolution requested" for work never started."""
    home = core.root / "home"
    core.store.put_lane(Lane("codex-1", "codex", "codex:test", Credential("codex", str(home), "home"),
                             str(home), LaneOwner.V2, False))
    job_id = core.dispatch("submit", submit_args(core.root, "r-quarantined"))["job_id"]
    core.store.update_job(job_id, state="running")
    core.store.add_attempt(attempt_id=f"{job_id}/a1", job_id=job_id, seq=1, lane_id="codex-1",
                           model_requested="astra", state="quarantined")
    core.stopping.set()
    with pytest.raises(Stopping) as refused:
        core.dispatch("kill", {"job_id": job_id, "confirm_dead": True})
    assert refused.value.code == Exit.DAEMON_UNAVAILABLE and str(refused.value) == STOPPING_MESSAGE
    assert "resolve:" + job_id not in core._busy
    core.stopping.clear()                          # before the stop, the same call is accepted
    ran = threading.Event()
    core._resolve_quarantine = lambda a, args: ran.set()
    assert core.dispatch("kill", {"job_id": job_id, "confirm_dead": True})["status"] == "resolution requested"
    assert ran.wait(10)


def test_c16_8_schedule_says_nothing_started_when_the_pool_shut_first(core):
    """C-16.8: close() may shut the worker pool between `_schedule`'s check and its
    submit; that is "not started", and the key is not left marked busy."""
    core.workers.shutdown(wait=True)
    core.stopping.set()
    assert core._schedule("resolve:x", lambda: None) is False
    core.stopping.clear()                          # a shut pool with no stop is a bug, and raises
    with pytest.raises(RuntimeError):
        core._schedule("resolve:y", lambda: None)
    assert "resolve:y" not in core._busy


def test_c16_8_schedule_reports_a_key_already_running_as_started(core):
    """`_schedule` still runs a key once at a time; a second call for a running key
    is not a refusal (kill --confirm-dead twice is "resolution requested" twice)."""
    release = threading.Event()
    assert core._schedule("resolve:z", lambda: release.wait(10)) is True
    assert core._schedule("resolve:z", lambda: None) is True
    release.set()


# --- the property ---------------------------------------------------------------

#: What a client is doing when the stop begins.
RUNNING, QUEUED, IDLE = "running", "queued", "idle"


@settings(max_examples=12, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(plan=st.lists(st.tuples(st.sampled_from([RUNNING, QUEUED, IDLE]), st.booleans()),
                     min_size=1, max_size=6),
       release_after=st.floats(min_value=0.0, max_value=.4))
def test_c16_8_every_request_is_answered_and_69_means_nothing_ran(daemon, plan, release_after):
    """C-16.8 property: for any mix of running, queued and not-yet-sent requests,
    reads and writes, every client gets one line; a running request gets its real
    reply (a write's row exists), every other gets 69 (a write's row does not)."""
    service = daemon(stop_reply_s=5)
    running = [i for i, (phase, _) in enumerate(plan) if phase == RUNNING]
    service.requests.shutdown(wait=True)
    # One thread per running request and one for a blocker, so queued ones wait.
    service.requests = ThreadPoolExecutor(max_workers=len(running) + 1, thread_name_prefix="subfleet-api")
    tag = uuid.uuid4().hex
    entered = {i: threading.Event() for i in running + ["blocker"]}
    release = threading.Event()
    real = service.dispatch

    def dispatch(op, args, **kwargs):
        who = args.get("who")
        if who in entered:
            entered[who].set()
            assert release.wait(30)
        return real(op, args, **kwargs)
    service.dispatch = dispatch

    def request(i, write):
        # `who` is not a ping field; C-16.2 ignores unknown fields.
        return ({"text": f"{tag}-{i}", "who": i} if write else {"who": i})
    socks = {}
    blocker = connect(service)
    send(blocker, "ping", who="blocker")
    for i in running:
        socks[i] = connect(service)
        send(socks[i], "ping", request_id=f"r{i}", **request(i, plan[i][1]))
    for event in entered.values():
        assert event.wait(10)
    queued = [i for i, (phase, _) in enumerate(plan) if phase == QUEUED]
    for i in queued:
        socks[i] = connect(service)
        send(socks[i], "ping", request_id=f"r{i}", **request(i, plan[i][1]))
    until(lambda: service.requests._work_queue.qsize() == len(queued))
    idle = [i for i, (phase, _) in enumerate(plan) if phase == IDLE]
    for i in idle:
        socks[i] = connect(service)
    until(lambda: counts(service)["connections"] == 1 + len(plan))

    began = time.monotonic()
    service.stopping.set()
    senders = [threading.Thread(target=send, args=(socks[i], "ping"), kwargs={"request_id": f"r{i}", **request(i, plan[i][1])},
                                daemon=True) for i in idle]
    for sender in senders:
        sender.start()
    time.sleep(release_after)
    release.set()
    lines = {i: reply(sock) for i, sock in socks.items()}
    service.serving.join(5 + READER_JOIN_S + 2 + SLACK_S)
    assert not service.serving.is_alive(), "close() did not return"
    assert time.monotonic() - began < 5 + SLACK_S       # nothing here is wedged
    for sender in senders:
        sender.join(10)
    assert reply(blocker)["result"]["pong"] is True

    committed = {row["text"] for row in rows(service.root, "SELECT text FROM service_notices WHERE text LIKE ?",
                                              (f"{tag}-%",))}
    for i, (phase, write) in enumerate(plan):
        line = lines[i]
        assert line is not None, (i, phase, "end of stream without an answer")
        if phase == RUNNING:
            assert line["ok"] is True and line["result"]["pong"] is True, (i, line)
            assert (f"{tag}-{i}" in committed) == write, (i, committed)
        else:
            assert not_run(line), (i, phase, line)
            assert f"{tag}-{i}" not in committed, (i, phase)
    for sock in [blocker, *socks.values()]:
        sock.close()
