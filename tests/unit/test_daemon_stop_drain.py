"""C-16.8: a stopping daemon answers every request it read, then lets go.

Review of #43, 2026-09-25: `close()` shut every client socket (SHUT_RDWR) before
it drained the pools. A submit or kill still running committed, then answered
into a socket already shut down; its client read "the daemon closed the
connection without a response", sent the request again to a daemon that was
gone, and reported the outcome unknown although the write had happened.
Reproduced on release/217 at 266c3ab8b (2026-11-07): a kill held in dispatch
when the stop began; its client read end of stream 0.19 s into the stop with no
bytes, and the kill completed 1.3 s later with no one to answer.

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

from subfleet import client as client_module, daemon as daemon_module, protocol
from subfleet.client import Client, DaemonError, DaemonUnavailable, SEND_MET_CLOSE_ERRNOS
from subfleet.contracts import STOP_GRACE_S, STOP_REPLY_S, Credential, Exit, Lane, LaneOwner
from subfleet.daemon import READER_JOIN_S, Daemon, Stopping
from subfleet.protocol import STOPPING_MESSAGE
from subfleet.store import Store

#: How much later than asked a step may finish on a loaded machine (load
#: averages above 140 were seen while these were first written, 2026-09-27).
SLACK_S = 5.0
#: A session for pings that write a notice (C-15.8 names one or refuses).
SESSION = "s-stop-drain"


@pytest.fixture
def daemon(monkeypatch):
    """Start daemon cores on short temp roots; yields `start(**options)`."""
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fake-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fake-start")
    # C-5.13 checks identity on every send; both sides observe this fixture's identity.
    monkeypatch.setattr(client_module, "boot_id", lambda: "fake-boot")
    monkeypatch.setattr(client_module, "proc_status", lambda pid: ("S", "fake-start"))
    started = []
    with tempfile.TemporaryDirectory(prefix="sfs-", dir="/tmp") as directory:
        try:
            with socket.socket(socket.AF_UNIX) as probe:
                probe.bind(str(Path(directory) / "socket-check"))
        except PermissionError:
            pytest.skip("sandbox denies unix socket binding")

        def start(**options):
            root = Path(directory) / f"d{len(started)}"
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


@pytest.fixture
def core(tmp_path):
    """A daemon core that is not serving, so setting `stopping` starts no close()."""
    service = Daemon(tmp_path / "state", desktop_prober=lambda: None)
    yield service
    service.close()


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
        sock.sendall((json.dumps({"v": 1, "id": request_id or op.replace(".", "-"), "op": op,
                                  "args": args}) + "\n").encode())
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


def notices(root: Path, text: str) -> list[dict]:
    return rows(root, "SELECT * FROM service_notices WHERE text=?", (text,))


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


def one_request_thread(service) -> None:
    """The request pool with a single thread, so a second request waits in its queue."""
    service.requests.shutdown(wait=True)
    service.requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="subfleet-api")


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
    time.sleep(.3)                                 # past the point where it used to shut the sockets
    release.set()                                  # the submit commits now, mid-drain
    result = outcome()
    assert "error" not in result, result
    answer = result["result"]
    assert answer["created"] is True and answer["request_id"] == "r-in-flight"
    assert "requeried" not in answer and lost == []   # answered the first time
    service.serving.join(STOP_REPLY_S + SLACK_S)
    assert not service.serving.is_alive()
    [job] = rows(service.root, "SELECT job_id FROM jobs WHERE request_id=?", ("r-in-flight",))
    assert job == {"job_id": answer["job_id"]}


def test_c16_8_a_kill_running_when_the_stop_begins_gets_its_real_reply(daemon):
    """C-16.8, C-7.1: a cancel that commits during the drain is answered, not lost."""
    service = daemon()
    job_id = service.dispatch("submit", submit_args(service.root, "r-to-kill"))["job_id"]
    entered, release = held(service, "kill")
    outcome = call_in_thread(Client(service.root).call_settled, "kill", {"job_id": job_id})
    assert entered.wait(10)
    service.stopping.set()
    until(lambda: service._closed)
    time.sleep(.3)
    release.set()
    result = outcome()
    assert result.get("result", {}).get("status") == "cancel requested", result
    assert "requeried" not in result["result"]
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

    def slow_reader(conn, peer):
        real_connection(conn, peer)
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


def test_c16_8_a_request_that_ran_and_could_not_answer_gets_no_last_line(daemon):
    """C-16.8, C-16.3: a write that ran during the stop and whose reply would not
    encode sent nothing, so the stop must not tell its client that nothing was run:
    the client reads the end of the stream, an unknown outcome, which it settles.
    An idle connection closed by the same stop still gets the last line."""
    service = daemon()
    entered, release = threading.Event(), threading.Event()
    real = service.dispatch
    text = f"ran, then could not answer {uuid.uuid4()}"

    def dispatch(op, args, **kwargs):
        result = real(op, args, **kwargs)
        if op == "ping" and args.get("text") == text:
            entered.set()
            assert release.wait(30)
            return {**result, "path": "\udc80"}  # a lone surrogate: the reply will not encode
        return result
    service.dispatch = dispatch
    ran = connect(service)
    send(ran, "ping", text=text, session_id=SESSION)
    assert entered.wait(10)
    idle = connect(service)
    until(lambda: counts(service)["connections"] == 2)
    service.stopping.set()
    until(lambda: service._closed)
    release.set()
    assert lines_until_closed(ran) == []           # no reply, and no "not run" after it
    assert [not_run(line) for line in lines_until_closed(idle)] == [True]
    stop(service)
    assert len(notices(service.root, text)) == 1   # the request ran
    ran.close()
    idle.close()


# --- what the stop keeps out, and how it says so --------------------------------

def test_c16_8_a_request_queued_when_the_stop_begins_is_not_run_and_says_so(daemon):
    """C-16.8: a request still waiting for a thread is not started; its client reads
    69, sends it again, meets no daemon and says so, never an unknown outcome."""
    service = daemon()
    one_request_thread(service)
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
    outcome = call_in_thread(Client(service.root, timeout=4).call, "ping", {"text": text, "session_id": SESSION})
    until(lambda: service.requests._work_queue.qsize() == 1)
    service.stopping.set()
    until(lambda: service._closed)
    release.set()
    assert reply(blocker)["result"] == {"held": True}      # the running one still answers
    error = outcome().get("error")
    assert isinstance(error, DaemonUnavailable), error
    assert f"before that it answered: {STOPPING_MESSAGE}" in str(error)
    stop(service)
    assert notices(service.root, text) == []
    assert counts(service)["not_run"] >= 1
    blocker.close()


def test_c16_8_a_conversation_op_queued_when_the_stop_begins_is_not_run(daemon):
    """C-16.8: the conversation pools are not the daemon's, and close() does not shut
    them first; a request queued on one is cancelled all the same and told so."""
    service = daemon()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="subfleet-history")
    service.conversations.history_reads.shutdown(wait=True)
    service.conversations.history_reads = pool
    entered, release = threading.Event(), threading.Event()
    pool.submit(lambda: (entered.set(), release.wait(30)))     # the pool's one thread, held
    assert entered.wait(10)
    ran = []
    service.conversations.respond = lambda *args: ran.append(args)
    sock = connect(service)
    send(sock, "conversation.history", request_id="r-history", conversation_id="c-1")
    until(lambda: pool._work_queue.qsize() == 1)
    stopped = threading.Thread(target=stop, args=(service,), daemon=True)
    stopped.start()
    assert not_run(reply(sock))
    release.set()
    stopped.join(STOP_REPLY_S + READER_JOIN_S + 2 + SLACK_S)
    assert not stopped.is_alive() and ran == []
    sock.close()


def test_c16_8_a_request_whose_turn_comes_after_the_stop_began_is_not_run(core):
    """C-16.8: a request a pool thread reaches once `stopping` is set runs nothing
    and answers 69 under its own request id, whichever pool and handler it has."""
    ran = []
    core.dispatch = lambda op, args, **kwargs: ran.append(op) or {}
    for respond in (core._respond, core.conversations.respond):
        server, client = socket.socketpair()
        core.stopping.set()
        core._run_request(respond, server, threading.Lock(),
                          protocol.Request(op="ping", args={"text": "x", "session_id": SESSION}, id="r-late"))
        line = reply(client)
        assert ran == [] and not_run(line) and line["id"] == "r-late", line
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
    send(idle, "ping", text=text, session_id=SESSION)  # may block until the daemon closes, then EPIPE
    # Read before SHUT_RD, it is answered itself, then the connection's last line
    # follows; sent after, only the last line answers it. Either way: 69, then closed.
    answers = lines_until_closed(idle)
    assert answers and all(not_run(line) for line in answers), answers
    stop(service)
    assert notices(service.root, text) == []
    idle.close()


def test_c16_8_the_listen_backlog_is_answered_not_dropped(daemon):
    """C-16.8: a connection still in the listen backlog when the listener closes is
    accepted and told nothing ran; closing the listener alone gives it end of stream."""
    service = daemon()
    gate = threading.Event()
    real_hold = service._hold_connection

    def hold_after_the_gate(conn, peer=None):
        gate.wait(30)                              # the accept loop sits here, so later connects queue
        real_hold(conn, peer)
    service._hold_connection = hold_after_the_gate
    first = connect(service)                       # accepted; the loop now waits at the gate
    queued, texts = [], []
    for _ in range(3):
        sock = connect(service)                    # in the backlog: connect succeeds, nothing accepts
        texts.append(f"backlog {uuid.uuid4()}")
        send(sock, "ping", text=texts[-1], session_id=SESSION)
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
    assert all(notices(service.root, text) == [] for text in texts)


def test_c16_8_a_wait_answers_at_once_when_the_stop_begins(daemon):
    """C-16.8, C-15.5: a long poll returns its timeout answer when the stop begins."""
    service = daemon()
    job_id = service.dispatch("submit", submit_args(service.root, "r-waited"))["job_id"]
    sock = connect(service)
    send(sock, "wait", job_ids=[job_id], deadline_s=60)
    until(lambda: counts(service)["connections"] == 1)
    time.sleep(.3)                                 # into the poll
    duration = stop(service)
    assert reply(sock)["result"] == {"timeout": True}
    assert duration < 2 + SLACK_S, duration
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


def test_c16_8_the_reply_deadline_fits_inside_the_stop_grace():
    """C-16.8, C-5.8a: the reply deadline counts from the top of close(), so it
    overlaps the waits before it; inside the grace with room for the pools."""
    control_join_s = 2
    assert STOP_REPLY_S + control_join_s + READER_JOIN_S < STOP_GRACE_S
    assert STOP_REPLY_S <= STOP_GRACE_S / 3


# --- what a stop that has begun reports -----------------------------------------

def quarantined(core, request_id: str) -> str:
    home = core.root / "home"
    core.store.put_lane(Lane("codex-1", "codex", "codex:test", Credential("codex", str(home), "home"),
                             str(home), LaneOwner.V2, False))
    job_id = core.dispatch("submit", submit_args(core.root, request_id))["job_id"]
    core.store.update_job(job_id, state="running")
    core.store.add_attempt(attempt_id=f"{job_id}/a1", job_id=job_id, seq=1, lane_id="codex-1",
                           model_requested="astra", state="quarantined")
    return job_id


def test_c16_8_quarantine_resolution_is_not_reported_requested_once_stopping(core):
    """C-16.8: `kill --confirm-dead` schedules work held only in memory; once the stop
    has begun it answers 69 instead of "resolution requested" for work never started."""
    job_id = quarantined(core, "r-quarantined")
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


def test_c16_8_recontainment_is_not_reported_requested_once_stopping(core, monkeypatch):
    """C-16.8, D-KILL4: a kill of a finished job whose census is not empty schedules
    the kill protocol; once stopping it answers 69 before the cascade writes anything."""
    job_id = core.dispatch("submit", submit_args(core.root, "r-recontain"))["job_id"]
    core.store.update_job(job_id, state="failed", rc=1, cancel_requested_at="2026-11-07T00:00:00Z")
    monkeypatch.setattr(core, "_terminal_census", lambda job: ({"seq": 1, "job_id": job}, object()))
    monkeypatch.setattr(core, "_may_signal", lambda a, census: True)
    monkeypatch.setattr(core, "_census_words", lambda census: "1 process")
    cascades = []
    monkeypatch.setattr(core, "_request_cancel", lambda *args, **kwargs: cascades.append(args) or True)
    core.stopping.set()
    with pytest.raises(Stopping):
        core.dispatch("kill", {"job_id": job_id})
    assert cascades == [] and "recontain:" + job_id not in core._busy
    core.stopping.clear()
    core._recontain = lambda a, census: None
    assert core.dispatch("kill", {"job_id": job_id})["status"] == "containment requested"
    assert len(cascades) == 1


def test_c16_8_a_stopping_kill_is_answered_69_over_the_socket(core):
    """C-16.8: `Stopping` from a handler is the not-run answer, whatever the
    closed refusal templates (C-25.7) would make of an unknown reason."""
    job_id = quarantined(core, "r-socket")
    core.stopping.set()
    server, client = socket.socketpair()
    core._answer(server, threading.Lock(),
                 protocol.Request(op="kill", args={"job_id": job_id, "confirm_dead": True}, id="r-kill"),
                 None, lambda: False)
    line = reply(client)
    assert not_run(line) and line["id"] == "r-kill", line
    server.close()
    client.close()


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


def test_c16_8_a_worker_cancelled_by_the_stop_is_not_logged_as_failed(core):
    """C-16.8: close() cancels what waits on the worker pool; that is not a failure."""
    gate, started = threading.Event(), threading.Event()
    core.workers.shutdown(wait=True)
    core.workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix="subfleet-io")
    core.workers.submit(lambda: (started.set(), gate.wait(10)))
    assert started.wait(10)
    assert core._schedule("resolve:queued", lambda: None) is True
    core.workers.shutdown(wait=False, cancel_futures=True)
    gate.set()
    core.workers.shutdown(wait=True)
    assert "resolve:queued" not in core._busy
    assert "worker resolve:queued failed" not in (core.root / "daemon.log").read_text()


# --- the client's side ------------------------------------------------------------

def test_c16_8_the_client_tells_stopping_from_busy():
    """C-16.7, C-16.8: both are 69 and both mean nothing ran; only the words differ."""
    stopping = DaemonError(int(Exit.DAEMON_UNAVAILABLE), STOPPING_MESSAGE, protocol.STOPPING_FIX)
    busy = DaemonError(int(Exit.DAEMON_UNAVAILABLE), protocol.BUSY_MESSAGE + ": it holds 4 client connections")
    assert stopping.busy and stopping.stopping
    assert busy.busy and not busy.stopping
    assert not DaemonError(1, STOPPING_MESSAGE).busy


def test_c16_8_a_refused_connect_after_a_stopping_answer_is_a_daemon_gone(monkeypatch):
    """C-16.8, C-17.5: after "stopping", a refused connect is a daemon gone, so the
    caller may go offline; after "busy" the same refusal is still busy (C-16.7)."""
    def client_answering(first: DaemonError) -> Client:
        client = Client(Path("/nonexistent-subfleet-root"), timeout=4, verify_lock=False)
        tries = iter([first])

        def once(op, args, **kwargs):
            answer = next(tries, None)
            if answer is not None:
                raise answer
            raise DaemonUnavailable("connect refused") from ConnectionRefusedError()
        client._call_once = once
        client.lock_holder_alive = lambda **kwargs: True       # the lock names a living daemon
        return client
    monkeypatch.setattr(client_module, "_sleep", lambda s: None)
    stopping = DaemonError(int(Exit.DAEMON_UNAVAILABLE), STOPPING_MESSAGE, protocol.STOPPING_FIX)
    with pytest.raises(DaemonUnavailable) as gone:
        client_answering(stopping).call("ping", {"text": "x"})
    assert f"before that it answered: {STOPPING_MESSAGE}" in str(gone.value)
    busy = DaemonError(int(Exit.DAEMON_UNAVAILABLE), protocol.BUSY_MESSAGE + ": at its limit")
    with pytest.raises(DaemonError) as still_busy:
        client_answering(busy).call("ping", {"text": "x"})
    assert still_busy.value is busy


@pytest.mark.parametrize("op", ["submit", "kill"])
def test_c16_8_a_resend_a_stopping_daemon_did_not_run_settles_nothing(monkeypatch, op):
    """C-16.3, C-16.8: a re-send answered only "stopping" was not run, so the first
    request's outcome stays unknown, never reported as 69 ("nothing was sent"), and
    the reason says the re-send was not run (busy's says it was not read)."""
    sent = []

    def once(self, op_, args, *, request_id, **kwargs):
        sent.append(op_)
        if op_ == "list":
            return {"jobs": []}
        if sent.count(op_) == 1:
            raise client_module.ResponseLost("no response from the daemon within 15s",
                                             op=op_, request_id=request_id)
        raise DaemonError(int(Exit.DAEMON_UNAVAILABLE), STOPPING_MESSAGE, protocol.STOPPING_FIX)
    monkeypatch.setattr(Client, "_call_once", once)
    monkeypatch.setattr(client_module, "_sleep", lambda s: None)
    client = Client("/nonexistent-subfleet-root", timeout=.2)
    args = {"job_id": "j"} if op == "kill" else {"request_id": "r-1"}
    with pytest.raises(client_module.OutcomeUnknown) as unknown:
        client.call_settled(op, args, request_id="r-1", minted=True, requery_timeout=.4)
    assert f"the re-sent request was not run: {STOPPING_MESSAGE}" in str(unknown.value)
    assert ("list" in sent) == (op == "submit")                     # a submit is looked up


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
        # A write is a ping that records a notice (C-15.8); a read is daemon.status.
        # `who` is no field of either; C-16.2 ignores unknown fields.
        return (("ping", {"text": f"{tag}-{i}", "session_id": SESSION, "who": i}) if write
                else ("daemon.status", {"who": i}))
    socks = {}
    blocker = connect(service)
    send(blocker, "daemon.status", who="blocker")
    for i in running:
        socks[i] = connect(service)
        op, args = request(i, plan[i][1])
        send(socks[i], op, request_id=f"r{i}", **args)
    for event in entered.values():
        assert event.wait(10)
    queued = [i for i, (phase, _) in enumerate(plan) if phase == QUEUED]
    for i in queued:
        socks[i] = connect(service)
        op, args = request(i, plan[i][1])
        send(socks[i], op, request_id=f"r{i}", **args)
    until(lambda: service.requests._work_queue.qsize() == len(queued))
    idle = [i for i, (phase, _) in enumerate(plan) if phase == IDLE]
    for i in idle:
        socks[i] = connect(service)
    until(lambda: counts(service)["connections"] == 1 + len(plan))

    began = time.monotonic()
    service.stopping.set()
    senders = []
    for i in idle:
        op, args = request(i, plan[i][1])
        senders.append(threading.Thread(target=send, args=(socks[i], op), kwargs={"request_id": f"r{i}", **args},
                                        daemon=True))
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
    assert reply(blocker)["ok"] is True

    committed = {row["text"] for row in rows(service.root, "SELECT text FROM service_notices WHERE text LIKE ?",
                                              (f"{tag}-%",))}
    for i, (phase, write) in enumerate(plan):
        line = lines[i]
        assert line is not None, (i, phase, "end of stream without an answer")
        if phase == RUNNING:
            assert line["ok"] is True and line["id"] == f"r{i}", (i, line)
            assert (f"{tag}-{i}" in committed) == write, (i, committed)
        else:
            assert not_run(line), (i, phase, line)
            assert f"{tag}-{i}" not in committed, (i, phase)
    for sock in [blocker, *socks.values()]:
        sock.close()
