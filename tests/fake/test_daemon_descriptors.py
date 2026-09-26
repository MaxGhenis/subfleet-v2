"""The daemon keeps serving when it runs short of descriptors, and never queues a
client until it gives up (2026-09-25: launchd's soft limit of 256 descriptors,
32 reader threads, and `accept` failing with EMFILE stopped the daemon)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import json
import math
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
from subfleet import protocol
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
            daemon._admit_connection(server)
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
    """`accept` failing without a break for `ACCEPT_GIVE_UP_S` is a leak, not a
    moment: the daemon exits so launchd starts a fresh one, and meanwhile it tries
    at most about five times a second."""
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
    assert len(calls) <= took / .2 + 2, (len(calls), took)
    assert "exiting so a fresh daemon starts" in log


def test_idle_connections_past_the_old_pool_never_starve_a_request(serve):
    """The core of the fix: 40 connections held open (more than the 32 readers the
    daemon had) and a ping is still answered at once; every pool a connection's
    request may wait in is as large as the connection cap."""
    daemon, path, _ = serve()
    assert daemon.readers._max_workers >= module.MAX_CONNECTIONS
    assert daemon.waiters._max_workers >= module.MAX_CONNECTIONS
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


def test_a_connection_past_the_cap_is_told_the_daemon_is_busy_at_once(serve, monkeypatch):
    """Each open connection holds a reader; one past `MAX_CONNECTIONS` gets an
    answer (exit 69, try again) at once instead of waiting out its timeout, and is
    served again once a connection closes."""
    monkeypatch.setattr(module, "MAX_CONNECTIONS", 2)
    daemon, path, _ = serve()
    idle = []
    try:
        # Under load the fixture's readiness probe can still be in the listen
        # backlog, and be admitted after the first idle client: then the second
        # is the one refused. So each idle client proves it was admitted (its own
        # ping answered) and stays open; a refused one connects again.
        limit = time.monotonic() + 10
        while len(idle) < 2:
            client = socket.socket(socket.AF_UNIX)
            client.settimeout(5)
            client.connect(str(path))
            try:
                client.sendall(b'{"v":1,"id":"idle","op":"ping","args":{}}\n')
                with client.makefile("rb") as stream:
                    reply = json.loads(stream.readline())
            except OSError:
                reply = {"ok": False}
            if reply.get("ok"):
                idle.append(client)
                continue
            client.close()
            assert time.monotonic() < limit, reply
            time.sleep(.05)
        until(lambda: len(daemon._connections) == 2)
        started = time.monotonic()
        refused = ping(path)
        assert time.monotonic() - started < 2
        assert refused["ok"] is False and refused["error"]["code"] == 69
        assert "serving 2 connections" in refused["error"]["message"]
        assert "telling new clients the daemon is busy" in (daemon.root / "daemon.log").read_text()
        # The CLI's client reads the answer even when its write lost the race with
        # the daemon's close (review F1: it reported exit 1, "Broken pipe").
        from subfleet.client import Client, DaemonError
        client = Client(daemon.root, timeout=3)
        client._checked = True           # the lock records the fixture's fake boot id
        for _ in range(30):
            with pytest.raises(DaemonError) as busy:
                client.call("ping")
            assert busy.value.code == 69
        # A client descheduled between connect and send (as under load) always
        # loses that race; it still reads the busy answer.
        import subfleet.client as client_module
        real_encode = client_module.encode
        monkeypatch.setattr(client_module, "encode", lambda request: (time.sleep(.1), real_encode(request))[1])
        for _ in range(3):
            with pytest.raises(DaemonError) as busy:
                client.call("ping")
            assert busy.value.code == 69
        monkeypatch.setattr(client_module, "encode", real_encode)
        status = daemon.connection_status()
        assert status["reading"] == 2 and status["cap"] == 2 and status["refused_busy"] >= 34
        idle.pop().close()
        until(lambda: len(daemon._connections) == 1)
        assert ping(path)["ok"]
    finally:
        for client in idle:
            client.close()


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


def test_clients_that_left_their_waits_no_longer_count_against_the_cap(admitted, monkeypatch):
    """Review F1: the cap counts connections still read, not ones whose client has
    gone while its `wait` runs on. Two clients send a 30 s `wait` on a queued job
    and close; with a cap of 2 a ping is still served (before, it was refused with
    69 until the waits ended)."""
    monkeypatch.setattr(module, "MAX_CONNECTIONS", 2)
    queued_jobs(monkeypatch)
    daemon, connect = admitted
    started = [threading.Event(), threading.Event()]
    real_wait = daemon.wait

    def wait(args, arrived=None):
        started[int(args.job_ids[0].split("-")[-1])].set()
        return real_wait(args, arrived)

    monkeypatch.setattr(daemon, "wait", wait)
    clients = [connect("wait", {"job_ids": [f"job-{n}"], "deadline_s": 30}, f"w{n}") for n in range(2)]
    assert all(event.wait(5) for event in started)   # running waits cannot be cancelled
    for client in clients:
        client.close()
    until(lambda: daemon.connection_status()["reading"] == 0)
    assert daemon.connection_status()["open"] == 2                   # the waits still run
    with connect("ping") as client:
        assert answer(client)["ok"]


def test_a_request_no_pool_has_started_is_dropped_when_its_client_leaves(admitted, monkeypatch, one_waiter):
    """Review F1: a `wait` queued behind a busy pool whose client has gone is
    cancelled, and its connection closed, instead of running later for no one."""
    queued_jobs(monkeypatch)
    daemon, connect = admitted
    pool = one_waiter(daemon)
    client = connect("wait", {"job_ids": ["job-1"], "deadline_s": 30})
    try:
        until(lambda: len(pool.queued) == 1)                        # read, and queued behind the held thread
        client.close()
        until(lambda: pool.queued[0].cancelled())
        until(lambda: daemon.connection_status()["open"] == 0)      # while the one waiter is still held
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
        until(lambda: daemon.connection_status()["reading"] == 0)   # its reader has seen the end
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


def test_a_reader_that_cannot_start_is_answered_busy_and_the_daemon_serves_on(serve):
    """Review F6: `submit` raising `RuntimeError` (a thread that cannot start) had
    ended `serve_forever` with nothing in the log. The connection is answered busy,
    the log says why at most once a minute, and the next client is served. CPython
    queues the work before it starts a thread, so the fake queues it too: the
    reader it gets later finds the connection refused and leaves the count alone."""
    daemon, path, thread = serve()
    assert ping(path)["ok"]
    real = daemon.readers.submit
    failures = {"left": 2}

    def submit(fn, *args):
        future = real(fn, *args)
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("can't start new thread")
        return future

    daemon.readers.submit = submit
    refused = [ping(path) for _ in range(2)]
    assert [(r["ok"], r["error"]["code"]) for r in refused] == [(False, 69)] * 2
    assert "cannot start a reader" in refused[0]["error"]["message"]
    assert ping(path)["ok"] and thread.is_alive()
    until(lambda: daemon.connection_status() == {"reading": 0, "open": 0, "cap": module.MAX_CONNECTIONS,
                                                 "refused_busy": 2})
    log = (daemon.root / "daemon.log").read_text()
    assert log.count("cannot start a reader (can't start new thread)") == 1


def test_a_reader_queued_before_submit_raises_never_dispatches(admitted, monkeypatch):
    """Busy means no operation ran, even if a worker picks up the queued reader
    before submit reports that another thread could not start (F6)."""
    daemon, connect = admitted
    real_submit, real_dispatch = daemon.readers.submit, daemon.dispatch
    dispatched = threading.Event()

    def dispatch(*args, **kwargs):
        dispatched.set()
        return real_dispatch(*args, **kwargs)

    def submit(fn, *args):
        entered = threading.Event()

        def run():
            entered.set()
            fn(*args)

        real_submit(run)
        assert entered.wait(5)
        dispatched.wait(.25)  # before the fix this lets the request run before refusal
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(daemon, "dispatch", dispatch)
    monkeypatch.setattr(daemon.readers, "submit", submit)
    for _ in range(2):
        with connect("ping") as client:
            reply = answer(client)
            assert reply["ok"] is False and reply["error"]["code"] == 69
            assert not dispatched.is_set(), "a request ran before its busy refusal"
    assert daemon.connection_status() == {"reading": 0, "open": 0, "cap": module.MAX_CONNECTIONS,
                                         "refused_busy": 2}
    log = (daemon.root / "daemon.log").read_text()
    assert log.count("cannot start a reader (can't start new thread)") == 1
    monkeypatch.setattr(daemon.readers, "submit", real_submit)
    with connect("ping") as client:
        assert answer(client)["ok"] and dispatched.is_set()


def test_daemon_status_reports_the_connections(admitted, monkeypatch):
    """Review F9: `daemon.status` says how many connections are read against the cap,
    how many are open, and how many were answered busy."""
    monkeypatch.setattr(module, "MAX_CONNECTIONS", 2)
    daemon, connect = admitted
    idle = connect()
    try:
        until(lambda: daemon.connection_status()["open"] == 1)
        reply = connect("daemon.status", {}, "status")
        try:
            result = answer(reply)["result"]
        finally:
            reply.close()
        assert result["connections"] == {"reading": 2, "open": 2, "cap": 2, "refused_busy": 0}
        until(lambda: daemon.connection_status()["reading"] == 1)
        idle2 = connect()
        try:
            until(lambda: daemon.connection_status()["reading"] == 2)
            with connect("ping") as client:
                assert answer(client)["error"]["code"] == 69
        finally:
            idle2.close()
        until(lambda: daemon.connection_status()["reading"] == 1)
        reply = connect("daemon.status", {}, "status")
        try:
            assert answer(reply)["result"]["connections"]["refused_busy"] == 1
        finally:
            reply.close()
    finally:
        idle.close()


def test_the_first_shortage_is_logged_in_the_machines_first_minute(monkeypatch):
    """Review F10: `time.monotonic()` counts from boot, so a daemon started within a
    minute of boot logged no accept or busy warning until uptime passed 60 s."""
    monkeypatch.setattr(module.procs, "boot_id", lambda: "descriptor-boot")
    monkeypatch.setattr(module.procs, "proc_start", lambda pid: "descriptor-start")
    with tempfile.TemporaryDirectory(prefix="sfd-", dir="/tmp") as temporary:
        daemon = Daemon(quiet(Path(temporary)), tick_s=.05)
        assert daemon._accept_trouble_logged == daemon._busy_logged == -math.inf
        monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 5.0, sleep=lambda seconds: None))
        daemon._accept_trouble(OSError(errno.EMFILE, "Too many open files"))
        ours, theirs = socket.socketpair()
        with ours, theirs:
            daemon._refuse_busy(ours, "the daemon is serving 512 connections")
            assert json.loads(theirs.makefile().readline())["error"]["code"] == 69
        monkeypatch.setattr(module, "time", time)
        log = (Path(temporary) / "daemon.log").read_text()
        daemon.close()
    assert "accept failed" in log and "telling new clients the daemon is busy" in log


def run_limit(code: str, soft: int = 256, hard: int | None = None) -> list[int]:
    """Run `code` in a fresh interpreter whose limits start at (soft, hard), by
    default launchd's 256 and the current hard limit; it prints numbers."""
    prelude = ("import resource; _, h = resource.getrlimit(resource.RLIMIT_NOFILE); "
               f"resource.setrlimit(resource.RLIMIT_NOFILE, ({soft}, {hard if hard is not None else 'h'})); "
               "from subfleet.daemon import raise_open_file_limit; ")
    out = subprocess.run([sys.executable, "-c", prelude + code], capture_output=True, text=True, check=True,
                         cwd=Path(__file__).resolve().parents[2])
    return [int(x) for x in out.stdout.split()]


def test_the_descriptor_soft_limit_is_raised_but_never_past_the_hard_limit():
    import resource
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    want = 4096 if hard == resource.RLIM_INFINITY else min(4096, hard)
    report = "print(raise_open_file_limit(4096)[0], resource.getrlimit(resource.RLIMIT_NOFILE)[0])"
    soft, now = run_limit(report)
    assert soft == now == max(256, want)
    assert run_limit(report, 256, 1024) == [1024, 1024]          # never past a finite hard limit
    assert run_limit(report, 256, 300) == [300, 300]
    assert run_limit(report, 5000) == [5000, 5000]               # a higher soft limit is never lowered


def test_main_raises_the_limit_before_it_starts_the_daemon(monkeypatch):
    calls = []
    monkeypatch.setattr(module, "raise_open_file_limit", lambda want=module.OPEN_FILES: calls.append(want) or (want, want))

    class Refused:
        def __init__(self, root):
            calls.append("daemon")
            raise module.DaemonUnavailable("another daemon holds daemon.lock")

    monkeypatch.setattr(module, "Daemon", Refused)
    assert module.main(["--state-root", "/nonexistent"]) == 69
    assert calls == [module.OPEN_FILES, "daemon"] and module.OPEN_FILES == 4096
