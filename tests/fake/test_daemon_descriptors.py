"""The daemon keeps serving when it runs short of descriptors, and never queues a
client until it gives up (2026-09-25: launchd's soft limit of 256 descriptors,
32 reader threads, and `accept` failing with EMFILE stopped the daemon).

These began as the release line's own hotfix tests. PR #43's design replaced
the hotfix (C-16.6, C-16.7), and #43's tests cover what it built
(tests/unit/test_daemon_connections.py, test_daemon_accept.py,
test_descriptors.py). What stays here is what the line kept, or what its review
found and #43's tests do not ask: `accept` failing for good ends the daemon, a
connection no reader can start for is answered busy, a `wait`'s deadline runs
from when it was read, a departed client's `wait` gives its place back, and a
half-closed client still gets an answer queued after its reader returned.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import json
import operator
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

from subfleet import daemon as module
from subfleet import descriptors, protocol
from subfleet.daemon import Daemon


def until(predicate, timeout=5.0):
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("condition timed out")


def ping(path: Path, timeout: float = 5.0) -> dict:
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        try:
            client.sendall(b'{"v":1,"id":"p","op":"ping","args":{}}\n')
        except OSError as exc:  # a busy daemon answers and closes before reading
            if exc.errno not in (errno.EPIPE, errno.ECONNRESET, errno.ENOTCONN):
                raise
        return json.loads(client.makefile().readline())


def send(path: Path, op: str, args: dict, request_id: str = "r") -> socket.socket:
    """A client that has sent one request and not yet read its answer."""
    client = socket.socket(socket.AF_UNIX)
    client.settimeout(10)
    client.connect(str(path))
    client.sendall(protocol.encode(protocol.Request(op=op, args=args, id=request_id)))
    return client


def answer(client: socket.socket) -> dict | None:
    """The client's answer, or None when the daemon closed without one."""
    line = client.makefile().readline()
    return json.loads(line) if line else None


def quiet(root: Path) -> Path:
    """The default policy with the catalog timer off: these daemons run under the
    real HOME, and a timed catalog run would index the host's own sessions into
    the test's state root (as tests/fake/conftest.py's Harness does)."""
    policy = json.loads((Path(__file__).resolve().parents[2] / "subfleet/default_policy.json").read_text())
    policy.setdefault("conversations", {})["catalog_interval_s"] = 0
    (root / "policy.json").write_text(json.dumps(policy))
    return root


def listening(path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(path))
        return True
    except OSError:
        return False


@pytest.fixture
def serve(monkeypatch):
    """Start a daemon's `serve_forever` on a short socket path; yield a starter so a
    test can patch the module first."""
    monkeypatch.setattr(module.procs, "boot_id", lambda: "descriptor-boot")
    monkeypatch.setattr(module.procs, "proc_start", lambda pid: "descriptor-start")
    started = []
    with tempfile.TemporaryDirectory(prefix="sfd-", dir="/tmp") as temporary:
        root = Path(temporary)
        try:
            with socket.socket(socket.AF_UNIX) as probe:
                probe.bind(str(root / "probe"))
        except PermissionError:
            pytest.skip("sandbox denies Unix socket binding")

        def start(**options):
            daemon = Daemon(quiet(root), tick_s=.05, **options)
            thread = threading.Thread(target=daemon.serve_forever, daemon=True)
            thread.start()
            started.append((daemon, thread))
            until(lambda: listening(root / "daemon.sock"))  # the file exists before `listen`
            return daemon, root / "daemon.sock", thread

        yield start
        for daemon, thread in started:
            daemon.stopping.set()
            thread.join(3)


@pytest.fixture
def admitted(monkeypatch):
    """Real admission and readers without binding a listener (denied in sandboxes)."""
    monkeypatch.setattr(module.procs, "boot_id", lambda: "descriptor-boot")
    monkeypatch.setattr(module.procs, "proc_start", lambda pid: "descriptor-start")
    with tempfile.TemporaryDirectory(prefix="sfd-", dir="/tmp") as temporary:
        daemon = Daemon(quiet(Path(temporary)), desktop_prober=lambda: None)
        clients = []

        def connect(op=None, args=None, request_id="r"):
            client, server = socket.socketpair()
            clients.append(client)
            client.settimeout(5)
            if op is not None:
                client.sendall(protocol.encode(protocol.Request(op=op, args=args or {}, id=request_id)))
            daemon._hold_connection(server)
            return client

        try:
            yield daemon, connect
        finally:
            for client in clients:
                client.close()
            daemon.close()


@pytest.fixture
def unbound_listener(monkeypatch):
    """Accept-error tests replace accept itself, so need no actual listener."""
    monkeypatch.setattr(socket.socket, "bind", lambda self, path: Path(path).touch())
    monkeypatch.setattr(socket.socket, "listen", lambda self, backlog: None)


def test_accept_out_of_descriptors_keeps_the_daemon_serving(serve, monkeypatch):
    """EMFILE from `accept` is a moment's shortage: the daemon says so in its log
    and serves the next client (before, `serve_forever` re-raised it and the
    daemon exited). The connection `accept` failed on is lost, as macOS drops it
    (review F4): the fake accepts it and closes it before raising."""
    real = socket.socket.accept
    failures = {"left": 0}

    def accept(self):
        conn, address = real(self)
        if failures["left"]:
            failures["left"] -= 1
            conn.close()
            raise OSError(errno.EMFILE, "Too many open files")
        return conn, address

    monkeypatch.setattr(socket.socket, "accept", accept)
    daemon, path, thread = serve()
    assert ping(path)["ok"]          # every earlier connection (the readiness probes) is accepted
    failures["left"] = 2
    dropped = [socket.socket(socket.AF_UNIX) for _ in range(2)]
    try:
        for client in dropped:
            client.settimeout(5)
            client.connect(str(path))
        assert [answer(client) for client in dropped] == [None, None]
    finally:
        for client in dropped:
            client.close()
    reply = ping(path)
    assert reply["ok"] and failures["left"] == 0 and thread.is_alive()
    assert "accept failed" in (daemon.root / "daemon.log").read_text()


def test_connections_queue_while_the_accept_loop_pauses(serve, monkeypatch):
    """The listen queue is the kernel's largest (`SOMAXCONN`, 128 on macOS), not
    64: a connection past it is refused outright, which a client reads as "no
    daemon" and "subfleet daemon start" (review F4)."""
    limit = int(subprocess.run(["sysctl", "-n", "kern.ipc.somaxconn"], capture_output=True, text=True).stdout or 0)
    wanted = min(socket.SOMAXCONN, limit) - 4        # the loop may accept a few before it pauses
    if wanted <= 64:
        pytest.skip(f"kern.ipc.somaxconn is {limit}: no room above the old queue of 64")
    real = socket.socket.accept
    paused, inside, resume = threading.Event(), threading.Event(), threading.Event()

    def accept(self):
        if paused.is_set():
            inside.set()
            resume.wait(20)
        return real(self)

    monkeypatch.setattr(socket.socket, "accept", accept)
    daemon, path, _ = serve()
    assert ping(path)["ok"]
    paused.set()
    assert inside.wait(5)            # the loop is paused before any client connects
    clients = []
    try:
        for _ in range(wanted):
            client = socket.socket(socket.AF_UNIX)
            client.settimeout(5)
            clients.append(client)
            client.connect(str(path))          # ECONNREFUSED past the queue
    finally:
        resume.set()
        for client in clients:
            client.close()
    assert len(clients) == wanted
    assert ping(path)["ok"]


def test_accept_drops_failed_socket_and_serves_next_pair(monkeypatch, unbound_listener):
    """F4's macOS accept outcome with real socket endpoints and a fake listener;
    the failed connection is gone, and the next one still receives its ping."""
    monkeypatch.setattr(module.procs, "boot_id", lambda: "descriptor-boot")
    monkeypatch.setattr(module.procs, "proc_start", lambda pid: "descriptor-start")
    backlogs = []
    monkeypatch.setattr(socket.socket, "listen", lambda self, backlog: backlogs.append(backlog))
    first, dropped = socket.socketpair()
    second, accepted = socket.socketpair()
    first.settimeout(5)
    second.settimeout(5)
    calls, errors = [], []

    def accept(self):
        calls.append(None)
        if len(calls) == 1:
            dropped.close()
            raise OSError(errno.EMFILE, "Too many open files")
        if len(calls) == 2:
            return accepted, None
        time.sleep(.01)
        raise socket.timeout()

    monkeypatch.setattr(socket.socket, "accept", accept)
    with tempfile.TemporaryDirectory(prefix="sfd-", dir="/tmp") as temporary:
        daemon = Daemon(quiet(Path(temporary)), desktop_prober=lambda: None)

        def run():
            try:
                daemon.serve_forever()
            except OSError as exc:
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            assert answer(first) is None
            second.sendall(protocol.encode(protocol.Request(op="ping", args={}, id="next")))
            assert answer(second)["ok"]
            assert backlogs == [socket.SOMAXCONN]
            assert not errors and thread.is_alive()
        finally:
            first.close()
            second.close()
            dropped.close()
            daemon.stopping.set()
            thread.join(5)
            accepted.close()
        assert not thread.is_alive()


def test_a_shortage_that_never_clears_ends_the_daemon_without_spinning(monkeypatch, unbound_listener):
    """C-16.6: `accept` failing without a break for `ACCEPT_GIVE_UP_S` is a leak, not
    a moment: the daemon exits so launchd starts a fresh one, and meanwhile it backs
    off (50 ms doubling to 2 s), so it tries a handful of times, never spins."""
    monkeypatch.setattr(module.procs, "boot_id", lambda: "descriptor-boot")
    monkeypatch.setattr(module.procs, "proc_start", lambda pid: "descriptor-start")
    monkeypatch.setattr(module, "ACCEPT_GIVE_UP_S", 1.0)
    calls = []

    def accept(self):
        calls.append(time.monotonic())
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(socket.socket, "accept", accept)
    with tempfile.TemporaryDirectory(prefix="sfd-", dir="/tmp") as temporary:
        daemon = Daemon(quiet(Path(temporary)), tick_s=.05)
        started = time.monotonic()
        with pytest.raises(OSError) as raised:
            daemon.serve_forever()
        took = time.monotonic() - started
        log = (Path(temporary) / "daemon.log").read_text()
    assert raised.value.errno == errno.EMFILE and 1.0 <= took < 5
    assert 4 <= len(calls) <= 8, (len(calls), took)       # 0, .05, .15, .35, .75, 1.55 s
    assert "exiting so launchd starts a fresh daemon" in log
    assert daemon._descriptor_status()["accept_failures"] == len(calls)


def test_idle_connections_past_the_old_pool_never_starve_a_request(serve):
    """The core of the fix: 40 connections held open (more than the 32 readers the
    daemon had) and a ping is still answered at once; every pool a long poll may
    wait in, `wait`'s and the conversation polls', has a thread for every connection
    the daemon may hold (C-16.5, C-16.7)."""
    daemon, path, _ = serve()
    assert daemon.waiters._max_workers >= descriptors.CONNECTIONS_CEILING >= daemon.max_connections
    assert daemon.conversations.polls._max_workers >= descriptors.CONNECTIONS_CEILING
    idle = [socket.socket(socket.AF_UNIX) for _ in range(40)]
    try:
        for client in idle:
            client.connect(str(path))
        until(lambda: len(daemon._connections) == 40)
        started = time.monotonic()
        assert ping(path, timeout=3)["ok"] and time.monotonic() - started < 1
    finally:
        for client in idle:
            client.close()


@pytest.mark.parametrize(("pool_name", "threads"), [("waiters", "subfleet-wait"),
                                                     ("conversations.polls", "subfleet-poll")])
def test_a_thread_going_idle_as_a_call_arrives_leaves_no_call_waiting(admitted, monkeypatch, pool_name, threads):
    """The race that left a ping unanswered behind 40 idle connections when each
    connection's reader was a call on a pool (release/217 before #43, 2026-09-27:
    on the free-threaded 3.14.7 the test above failed up to 14 runs in 40), forced
    so it happens every run, on the pools whose long polls each hold a thread for
    up to a minute and which have one for every connection the daemon may hold
    (C-16.5, C-16.7). A pool thread finishes a call and finds nothing queued;
    before it says it is idle, a call arrives, finds no idle thread and starts a
    new one; the old thread then takes that call, and the new thread finds
    nothing and goes idle. The standard pool counted that as two idle threads
    (permits of a `threading.Semaphore`), so of the next two calls the second was
    left queued behind calls that hold their threads, with the pool nowhere near
    its size: a long poll queued past its client's deadline. Here the finishing
    thread is held just before it counts itself idle and let go once the call has
    found no idle thread; then every call must still start at once. A pool that
    counts no permits (`subfleet.pool.Pool`) has no such moment, and the calls
    start as they come."""
    daemon, _ = admitted
    pool = operator.attrgetter(pool_name)(daemon)
    let_go, holding, finisher_took_it, new_thread_idle = (threading.Event() for _ in range(4))
    finisher, submitter = {}, threading.get_ident()
    real_release, real_acquire = threading.Semaphore.release, threading.Semaphore.acquire

    def release(self, n=1):
        me = threading.get_ident()
        if me == finisher.get("thread") and not holding.is_set():
            holding.set()
            let_go.wait(10)                 # the call arrives meanwhile and starts a thread
        elif (let_go.is_set() and me != finisher.get("thread")
              and threading.current_thread().name.startswith(threads)):
            new_thread_idle.set()           # the thread that call started, finding nothing
        return real_release(self, n)

    def acquire(self, blocking=True, timeout=None):
        got = real_acquire(self, blocking, timeout)
        if not got and threading.get_ident() == submitter and holding.is_set() and not let_go.is_set():
            let_go.set()                    # no idle thread was found: the finisher goes on
            finisher_took_it.wait(10)       # and takes the call before the new thread exists
        return got

    monkeypatch.setattr(threading.Semaphore, "release", release)
    monkeypatch.setattr(threading.Semaphore, "acquire", acquire)
    release_all = threading.Event()
    started = {name: threading.Event() for name in ("first", "second", "third")}

    def holds(name):
        if name == "first" and threading.get_ident() == finisher.get("thread"):
            finisher_took_it.set()
        started[name].set()
        release_all.wait(30)

    try:
        pool.submit(lambda: finisher.setdefault("thread", threading.get_ident())).result(5)
        holding.wait(1)                     # only a pool that counts permits gets here
        pool.submit(holds, "first")
        assert started["first"].wait(5)
        if holding.is_set():
            assert finisher_took_it.is_set() and new_thread_idle.wait(5)
        pool.submit(holds, "second")
        pool.submit(holds, "third")
        # Three calls hold threads of a pool sized for MAX_CONNECTIONS: each starts.
        assert started["second"].wait(5)
        assert started["third"].wait(5), f"a call waited in daemon.{pool_name} with room to spare"
    finally:
        let_go.set()
        release_all.set()


def test_every_pool_the_daemon_runs_calls_on_starts_them_while_it_has_room(admitted):
    """C-16.5: the daemon's own pools, its conversation service's and its timers'
    are `Pool`s, whose calls never wait while a thread could run them; the
    standard pool's count of idle threads could drift above the truth."""
    from concurrent.futures import Executor
    from subfleet.pool import Pool
    daemon, _ = admitted
    pools = {f"{owner}.{name}": value
             for owner, holder in (("daemon", daemon), ("conversations", daemon.conversations),
                                   ("timers", daemon.timers))
             for name, value in vars(holder).items() if isinstance(value, Executor)}
    assert {"daemon.waiters", "daemon.requests", "daemon.lookups", "daemon.workers",
            "conversations.polls", "conversations.files",
            "timers._cycles", "timers._mirror", "timers._lanes"} <= set(pools)
    assert [name for name, value in pools.items() if type(value) is not Pool] == []


def test_accept_raises_what_is_not_a_shortage(monkeypatch, unbound_listener):
    """Any other error still ends the loop, as before: it is not retried blindly."""
    monkeypatch.setattr(module.procs, "boot_id", lambda: "descriptor-boot")
    monkeypatch.setattr(module.procs, "proc_start", lambda pid: "descriptor-start")

    def accept(self):
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(socket.socket, "accept", accept)
    with tempfile.TemporaryDirectory(prefix="sfd-", dir="/tmp") as temporary:
        daemon = Daemon(quiet(Path(temporary)), tick_s=.05)
        errors = []

        def run():
            try:
                daemon.serve_forever()
            except OSError as exc:
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=5)
        stopped = not thread.is_alive()
        if not stopped:
            daemon.stopping.set()
            thread.join(timeout=5)
        assert stopped, "non-shortage accept error was retried"
    assert len(errors) == 1 and errors[0].errno == errno.EBADF


def queued_jobs(monkeypatch) -> None:
    """Every job the daemon looks up is queued, so a `wait` on one runs to its deadline."""
    monkeypatch.setattr(module.Daemon, "_job", lambda self, job_id: {"job_id": job_id, "state": "queued"})


class OneWaiter(ThreadPoolExecutor):
    """The daemon's `wait` pool with one thread, which the test holds, as when every
    waiter is taken; it keeps what it was given, so a test knows when a request is
    queued."""

    def __init__(self, held: threading.Event):
        super().__init__(max_workers=1, thread_name_prefix="test-wait")
        self.queued = []
        super().submit(held.wait, 30)
        self.release = held.set

    def submit(self, fn, *args, **kwargs):
        future = super().submit(fn, *args, **kwargs)
        self.queued.append(future)
        return future


@pytest.fixture
def one_waiter():
    held = threading.Event()

    def install(daemon) -> OneWaiter:
        daemon.waiters = OneWaiter(held)
        return daemon.waiters

    yield install
    held.set()


def test_clients_that_left_their_waits_give_their_places_back(admitted, monkeypatch):
    """C-16.7 (review F1 of the hotfix): a `wait` whose client has gone ends at its
    next look, within `CLIENT_POLL_S`, so its connection and its place under the cap
    go. Two clients send a 30 s `wait` on a queued job and close; with a cap of 2 a
    ping is served soon after (before F1 it was refused with 69 until the waits ended)."""
    queued_jobs(monkeypatch)
    daemon, connect = admitted
    daemon._pinned_max_connections = 2
    started = [threading.Event(), threading.Event()]
    real_wait = daemon.wait

    def wait(args, arrived=None, **kwargs):
        started[int(args.job_ids[0].split("-")[-1])].set()
        return real_wait(args, arrived, **kwargs)

    monkeypatch.setattr(daemon, "wait", wait)
    clients = [connect("wait", {"job_ids": [f"job-{n}"], "deadline_s": 30}, f"w{n}") for n in range(2)]
    assert all(event.wait(5) for event in started)   # running, so the reader's cancel cannot stop them
    left = time.monotonic()
    for client in clients:
        client.close()
    until(lambda: not daemon._connections, timeout=5)
    assert time.monotonic() - left < 3               # at their next look, not their 30 s deadline
    assert daemon._descriptor_status()["abandoned"] == 2
    with connect("ping") as client:
        assert answer(client)["ok"]


def test_a_request_no_pool_has_started_is_dropped_when_its_client_leaves(admitted, monkeypatch, one_waiter):
    """Review F1, C-16.7: a `wait` (a read) queued behind a busy pool whose client has
    gone is cancelled, and its connection closed, instead of running later for no one."""
    queued_jobs(monkeypatch)
    daemon, connect = admitted
    pool = one_waiter(daemon)
    client = connect("wait", {"job_ids": ["job-1"], "deadline_s": 30})
    try:
        until(lambda: len(pool.queued) == 1)                        # read, and queued behind the held thread
        client.close()
        until(lambda: pool.queued[0].cancelled())
        until(lambda: not daemon._connections)                      # while the one waiter is still held
        assert daemon._descriptor_status()["abandoned"] == 1
    finally:
        pool.release()
        client.close()


def test_a_client_that_closed_only_its_write_half_still_gets_its_answer(admitted, monkeypatch, one_waiter):
    """What a client sent is dropped only when it has gone: one that closed only its
    write half (`shutdown(SHUT_WR)`, as `nc -N` does) still reads the answer to a
    request that was queued when its reader returned."""
    queued_jobs(monkeypatch)
    daemon, connect = admitted
    pool = one_waiter(daemon)
    client = connect("wait", {"job_ids": ["job-1"], "deadline_s": 0}, "half")
    try:
        until(lambda: len(pool.queued) == 1)
        client.shutdown(socket.SHUT_WR)
        until(lambda: not daemon._readers)                          # its reader has seen the end
        assert not pool.queued[0].cancelled()
        pool.release()
        reply = answer(client)
        assert reply is not None and reply["ok"] and reply["id"] == "half" and reply["result"] == {"timeout": True}
    finally:
        pool.release()
        client.close()


def test_a_wait_that_starts_after_its_deadline_answers_at_once(admitted, monkeypatch, one_waiter):
    """Review F1: a `wait`'s deadline runs from when its request was read, not from
    when a thread picked it up, so one queued past its client's deadline looks once
    and answers `{"timeout": true}` at once."""
    queued_jobs(monkeypatch)
    daemon, connect = admitted
    started = time.monotonic()
    assert daemon.wait(protocol.WaitArgs(job_ids=["job-1"], deadline_s=30), arrived=started - 31) == {"timeout": True}
    assert time.monotonic() - started < 1
    pool = one_waiter(daemon)
    client = connect("wait", {"job_ids": ["job-1"], "deadline_s": 3}, "late")
    try:
        until(lambda: len(pool.queued) == 1)
        time.sleep(3.5)                  # past its deadline while it waits for the one thread
        released = time.monotonic()
        pool.release()
        reply = answer(client)
        assert reply["ok"] and reply["result"] == {"timeout": True}
        assert time.monotonic() - released < 2, time.monotonic() - released   # before: its full 3 s again
    finally:
        pool.release()
        client.close()


def test_a_reader_that_cannot_start_is_answered_busy_and_the_daemon_serves_on(serve, monkeypatch):
    """Review F6, C-16.7: a connection whose reader thread cannot start (the process
    is out of threads) had ended `serve_forever` with nothing in the log. It is
    answered busy before anything is read, so nothing it sent runs; the log says so
    on the 1st, 2nd, 4th ... refusal; and the next client is served."""
    daemon, path, thread = serve()
    assert ping(path)["ok"]
    failures = {"left": 2}
    real_thread = threading.Thread

    class NoReader(real_thread):
        def start(self):
            if self.name == "subfleet-socket" and failures["left"]:
                failures["left"] -= 1
                raise RuntimeError("can't start new thread")
            super().start()

    dispatched = []
    real_dispatch = daemon.dispatch
    monkeypatch.setattr(daemon, "dispatch", lambda op, args, *a, **kw: dispatched.append(op) or real_dispatch(op, args, *a, **kw))
    monkeypatch.setattr(module.threading, "Thread", NoReader)
    refused = [ping(path) for _ in range(2)]
    assert [(r["ok"], r["error"]["code"]) for r in refused] == [(False, 69)] * 2
    assert "could not start a thread for this connection" in refused[0]["error"]["message"]
    assert "try again shortly" in refused[0]["error"]["fix"]
    assert dispatched == [], "a request ran on a connection refused busy"
    monkeypatch.setattr(module.threading, "Thread", real_thread)
    assert ping(path)["ok"] and thread.is_alive() and dispatched == ["ping"]
    until(lambda: not daemon._connections)
    assert daemon._descriptor_status()["refused"] == 2
    log = (daemon.root / "daemon.log").read_text()
    assert log.count("no reader thread could start for (can't start new thread)") == 2   # the 1st and the 2nd


def test_the_first_shortage_is_logged_in_the_machines_first_minute(monkeypatch, unbound_listener):
    """Review F10: `time.monotonic()` counts from boot, and a once-a-minute log that
    started from 0 said nothing in the machine's first minute. The log is counted
    now (the 1st, 2nd, 4th, 8th ... of each kind), so the first accept failure and
    the first refusal are logged whatever the clock says."""
    monkeypatch.setattr(module.procs, "boot_id", lambda: "descriptor-boot")
    monkeypatch.setattr(module.procs, "proc_start", lambda pid: "descriptor-start")
    with tempfile.TemporaryDirectory(prefix="sfd-", dir="/tmp") as temporary:
        daemon = Daemon(quiet(Path(temporary)), tick_s=.05)
        failures = [OSError(errno.EMFILE, "Too many open files")]

        def accept(self):
            if failures:
                raise failures.pop()
            daemon.stopping.set()
            raise socket.timeout()

        monkeypatch.setattr(socket.socket, "accept", accept)
        monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 5.0, sleep=lambda seconds: None))
        daemon._pinned_max_connections = 1
        daemon._connections.add(object())                # the one place is taken
        ours, theirs = socket.socketpair()
        with ours, theirs:
            daemon._hold_connection(ours)
            assert json.loads(theirs.makefile().readline())["error"]["code"] == 69
        daemon._connections.clear()
        daemon.serve_forever()                           # one EMFILE, then stopping
        monkeypatch.setattr(module, "time", time)
        log = (Path(temporary) / "daemon.log").read_text()
    assert "accept failed: EMFILE (1 in a row" in log
    assert "1 connections refused busy so far, the last a connection over the cap" in log


def run_limit(code: str, soft: int = 256, hard: int | None = None) -> list[int]:
    """Run `code` in a fresh interpreter whose limits start at (soft, hard), by
    default launchd's 256 and the current hard limit; it prints numbers."""
    prelude = ("import resource; _, h = resource.getrlimit(resource.RLIMIT_NOFILE); "
               f"resource.setrlimit(resource.RLIMIT_NOFILE, ({soft}, {hard if hard is not None else 'h'})); "
               "from subfleet.descriptors import raise_open_file_limit; ")
    out = subprocess.run([sys.executable, "-c", prelude + code], capture_output=True, text=True, check=True,
                         cwd=Path(__file__).resolve().parents[2])
    return [int(x) for x in out.stdout.split()]


def test_the_descriptor_soft_limit_is_raised_but_never_past_the_hard_limit():
    """C-16.6 with a real kernel: from launchd's 256 the soft limit is raised to what
    is asked, never past a finite hard limit, and a higher one is never lowered."""
    import resource
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    want = 4096 if hard == resource.RLIM_INFINITY else min(4096, hard)
    report = "print(raise_open_file_limit(4096)[1], resource.getrlimit(resource.RLIMIT_NOFILE)[0])"
    soft, now = run_limit(report)
    assert soft == now == max(256, want)
    assert run_limit(report, 256, 1024) == [1024, 1024]          # never past a finite hard limit
    assert run_limit(report, 256, 300) == [300, 300]
    assert run_limit(report, 5000) == [5000, 5000]               # a higher soft limit is never lowered
