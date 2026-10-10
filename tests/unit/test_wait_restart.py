"""C-15.4: a `wait` rides out a daemon restart (defect D-WT1, 2026-10-10).

The 2.1.11.4 install (09:06Z) and a `launchctl kickstart -k` (09:08Z) each ended
all five of the Subfleet hub's `subfleet wait`s with "the daemon closed the
connection without a response" while their jobs ran on, waking the hub. A `wait`
only reads, so once it has reached a daemon it asks again after a lost answer
or a socket gone or refused, for `RESTART_WINDOW_S` and always within
`--timeout`.

The first half drives the real CLI, the real client and the PostToolUse hook over
a Unix socket held by `FakeDaemon`, which each test stops and starts. The second
half states the loops' invariants as properties over every interleaving
Hypothesis draws of busy, lost, refused, gone and answered polls on a fake clock,
each run checked against `reference`, an independent statement of the rule:

* W1 the exit is 0 only when every requested job was answered terminal and
  succeeded; once all were answered, the exit is theirs;
* W2 with `--timeout T` the wait ends by T plus one poll budget (in fact by T
  plus the under-0.2 s poll an immediate answer's pause may follow), and exits
  124 only once T has passed;
* W3 the wait never gives up inside the restart window, and a daemon gone for
  good ends it within the window, one poll budget and one pause of the first
  poll that daemon failed;
* W4 no job outside the requested set changes the exit, the polls or the time
  (C-17.3);
* W5 nothing is sent but `wait`, for requested jobs only;
* W6 one stderr line per outage that ended with an answer, saying "restarted"
  exactly when `daemon.lock` names another process than before.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import socket
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import cli, hooks
from subfleet import client as client_module
from subfleet.client import Client, DaemonError, DaemonUnavailable, ResponseLost
from subfleet.contracts import WAIT_POLL_MAX_S, Exit

JOB = "20261010-090600-restart-proof"
#: The transport budget of one poll of a wait with no `--timeout` (`wait_jobs`).
POLL_BUDGET_S = WAIT_POLL_MAX_S + 15
LINE = re.compile(r"subfleet wait: daemon (restarted|answered again); still waiting")


# --- a daemon socket this test restarts ------------------------------------------

class FakeDaemon:
    """A `daemon.sock` that answers `wait`, and that a test stops and starts again.

    It answers from `states`: when every job asked for is there, at once; else
    `{"timeout": true}` after `hold` seconds. `stop()` does to its clients what
    the install and the kickstart did on 2026-10-10: each poll it holds is closed
    unanswered, and its socket file goes (`unlink=True`, a stop that cleans up) or
    stays with nothing listening (a crash), so a connect finds it gone or
    refused. `start()` writes the new process's identity to `daemon.lock` before
    it listens, as `Daemon.__init__` does before `serve_forever`.
    """

    def __init__(self, root: Path):
        self.root = root
        self.path = root / "daemon.sock"
        self.states: dict[str, tuple[str, int]] = {}
        self.requests: list[dict] = []
        self.answered = 0
        self.closed_unanswered = 0
        self.hold = 30.0
        self.drop = False                      # close every request unanswered
        self._lock = threading.Lock()
        self._held: set[socket.socket] = set()
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self, identity: dict) -> "FakeDaemon":
        (self.root / "daemon.lock").write_text(json.dumps(identity))
        self.path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.path))
        listener.listen(16)
        # A short accept timeout: a close from another thread does not wake a
        # blocked accept on macOS, so the accepting thread closes its own listener.
        listener.settimeout(.02)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept, args=(listener, self._stop), daemon=True)
        self._thread.start()
        return self

    def stop(self, *, unlink: bool) -> None:
        if self._stop is not None:
            self._stop.set()
            self._thread.join(5)
            self._stop = self._thread = None
        with self._lock:
            held, self._held = list(self._held), set()
            self.closed_unanswered += len(held)
        for conn in held:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                conn.close()
        if unlink:
            self.path.unlink(missing_ok=True)

    def polls(self) -> int:
        with self._lock:
            return len(self.requests)

    def _accept(self, listener: socket.socket, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                conn.settimeout(None)
                with self._lock:
                    self._held.add(conn)
                threading.Thread(target=self._serve, args=(conn,), daemon=True).start()
        finally:
            listener.close()

    def _serve(self, conn: socket.socket) -> None:
        try:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            request = json.loads(data)
            with self._lock:
                self.requests.append(request)
            if self.drop:
                with self._lock:
                    self.closed_unanswered += 1
                return
            result = self._wait(conn, request.get("args") or {})
            if result is None:
                return                                   # closed by stop() while held
            conn.sendall((json.dumps({"v": 1, "id": request.get("id", ""), "ok": True,
                                      "result": result}) + "\n").encode())
            with self._lock:
                self.answered += 1
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                self._held.discard(conn)
            with contextlib.suppress(OSError):
                conn.close()

    def _wait(self, conn: socket.socket, args: dict) -> dict | None:
        ids = [str(job_id) for job_id in args.get("job_ids") or ()]
        ends = time.monotonic() + min(self.hold, float(args.get("deadline_s", 60)))
        while True:
            with self._lock:
                if conn not in self._held:
                    return None
                done = {job_id: self.states[job_id] for job_id in ids if job_id in self.states}
            if ids and len(done) == len(ids):
                return {"jobs": [{"job_id": job_id, "state": state, "rc": rc}
                                 for job_id, (state, rc) in done.items()]}
            if time.monotonic() >= ends:
                return {"timeout": True}
            time.sleep(.01)


def this_process() -> dict:
    """A lock naming this process as it is, so the CLI's one lock check (C-5.8) passes."""
    pid = os.getpid()
    return {"pid": pid, "boot_id": client_module.boot_id(),
            "proc_start": client_module.proc_start(pid), "version": "test"}


def next_process(number: int) -> dict:
    """A later daemon: only its identity has to differ (the lock is checked once)."""
    return {**this_process(), "proc_start": f"restarted-{number}"}


def until(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(.01)
    raise AssertionError("condition not reached in time")


def in_background(steps):
    """Run `steps()` on a thread, keeping what it raised for the test to re-raise."""
    raised: list[BaseException] = []

    def run():
        try:
            steps()
        except BaseException as exc:                    # noqa: BLE001 - re-raised below
            raised.append(exc)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def join():
        thread.join(30)
        assert not thread.is_alive(), "the daemon script did not finish"
        if raised:
            raise raised[0]
    return join


@pytest.fixture
def root(monkeypatch):
    # macOS's AF_UNIX path holds 104 bytes; pytest's temporary root does not fit.
    with tempfile.TemporaryDirectory(prefix="sfwt-", dir="/tmp") as directory:
        path = Path(directory)
        try:
            with socket.socket(socket.AF_UNIX) as probe:
                probe.bind(str(path / "socket-check"))
        except PermissionError:
            pytest.skip("sandbox denies unix socket binding")
        (path / "socket-check").unlink()
        monkeypatch.setenv("SUBFLEET_HOME", str(path))
        yield path


@pytest.fixture
def fake(root):
    daemon = FakeDaemon(root)
    try:
        yield daemon
    finally:
        daemon.stop(unlink=True)


def wait(*argv: str) -> int:
    return cli.main(["wait", *argv])


def only_waits_for(fake: FakeDaemon, ids: list[str]) -> bool:
    """W5 on the wire: every request was a `wait` for the requested jobs."""
    return all(request.get("op") == "wait" and request["args"]["job_ids"] == ids
               for request in fake.requests)


# --- the restarts of 2026-10-10, on a real socket ----------------------------------

def test_c15_4_a_wait_rides_out_a_restart_that_closes_its_poll_unanswered(fake, capsys):
    """The install and the kickstart: the held poll was closed with no answer and
    the socket went; launchd's next daemon answered seconds later. The wait asks
    again, says so once, and ends with the job's own exit."""
    fake.start(this_process())

    def restart():
        until(lambda: fake.polls() >= 1)
        time.sleep(.05)                                 # the poll is held
        fake.stop(unlink=True)
        time.sleep(.6)
        fake.states[JOB] = ("succeeded", 0)
        fake.start(next_process(1))
    joined = in_background(restart)
    assert wait(JOB, "--timeout", "60") == 0
    joined()
    err = capsys.readouterr().err
    assert fake.closed_unanswered >= 1                  # the old daemon dropped the poll
    assert LINE.findall(err) == ["restarted"]
    assert "without a response" not in err and "restart window" not in err
    assert only_waits_for(fake, [JOB]) and len(fake.requests) >= 2


def test_c15_4_a_wait_rides_out_refused_connects_until_a_daemon_listens(fake, capsys):
    """A crash under KeepAlive: the held poll is cut, the socket file stays with
    nothing listening, so each connect is refused until the next daemon binds it."""
    fake.start(this_process())
    outage = []

    def crash_then_restart():
        until(lambda: fake.polls() >= 1)
        time.sleep(.05)
        fake.stop(unlink=False)
        assert fake.path.exists()                       # refused, not gone
        began = time.monotonic()
        time.sleep(1.5)
        fake.states[JOB] = ("failed", 3)
        fake.start(next_process(1))
        outage.append(time.monotonic() - began)
    joined = in_background(crash_then_restart)
    started = time.monotonic()
    assert wait(JOB, "--timeout", "60") == 3            # the job's own exit (C-17.3)
    joined()
    assert time.monotonic() - started >= outage[0] >= 1.5
    err = capsys.readouterr().err
    assert LINE.findall(err) == ["restarted"]
    assert only_waits_for(fake, [JOB])


def test_c15_4_the_same_daemon_answering_again_is_not_called_a_restart(fake, capsys):
    """The line names a restart only when `daemon.lock` names another process."""
    identity = this_process()
    fake.start(identity)

    def blip():
        until(lambda: fake.polls() >= 1)
        time.sleep(.05)
        fake.stop(unlink=True)
        time.sleep(.3)
        fake.states[JOB] = ("succeeded", 0)
        fake.start(identity)
    joined = in_background(blip)
    assert wait(JOB, "--timeout", "60") == 0
    joined()
    assert LINE.findall(capsys.readouterr().err) == ["answered again"]


def test_c15_4_each_outage_has_its_own_window(fake, capsys, monkeypatch):
    """An install and then a kickstart two minutes later: the window starts again
    at the first poll each outage fails. Two outages of 1.2 s against a 2 s window
    pass; one window across both would have run out."""
    monkeypatch.setattr(client_module, "RESTART_WINDOW_S", 2.0)
    fake.start(this_process())
    fake.hold = .2

    def two_restarts():
        until(lambda: fake.polls() >= 1)
        fake.stop(unlink=True)
        time.sleep(1.2)
        fake.start(next_process(1))
        answered = fake.answered
        until(lambda: fake.answered > answered)          # the wait was answered between
        fake.stop(unlink=True)
        time.sleep(1.2)
        fake.states[JOB] = ("succeeded", 0)
        fake.start(next_process(2))
    joined = in_background(two_restarts)
    assert wait(JOB, "--timeout", "60") == 0
    joined()
    assert LINE.findall(capsys.readouterr().err) == ["restarted", "restarted"]
    assert only_waits_for(fake, [JOB])


def test_c15_4_a_daemon_gone_for_good_ends_the_wait_after_the_window(fake, capsys, monkeypatch):
    """A daemon that does not come back still ends a wait with no `--timeout`: after
    the window, with today's exit and message (69, `subfleet daemon start`)."""
    monkeypatch.setattr(client_module, "RESTART_WINDOW_S", 1.5)
    fake.start(this_process())
    stopped = []

    def stop_for_good():
        until(lambda: fake.polls() >= 1)
        time.sleep(.05)
        fake.stop(unlink=True)
        stopped.append(time.monotonic())
    joined = in_background(stop_for_good)
    assert wait(JOB) == int(Exit.DAEMON_UNAVAILABLE)
    ended = time.monotonic()
    joined()
    # The window, then at most one pause (C-16.7's 1 s cap) and some scheduling slack.
    assert 1.5 <= ended - stopped[0] <= 1.5 + 1.0 + 2.0, ended - stopped[0]
    err = capsys.readouterr().err
    assert "past the 1.5s restart window" in err
    assert "no daemon at" in err and "subfleet daemon start" in err
    assert not LINE.findall(err)


def test_c15_4_a_daemon_that_drops_every_poll_ends_the_wait_after_the_window(fake, capsys, monkeypatch):
    """Lost answers past the window end the wait as a lost answer always did: exit 1."""
    monkeypatch.setattr(client_module, "RESTART_WINDOW_S", 1.5)
    fake.drop = True
    fake.start(this_process())
    started = time.monotonic()
    assert wait(JOB) == int(Exit.OPERATIONAL)
    assert 1.5 <= time.monotonic() - started <= 1.5 + 1.0 + 2.0
    err = capsys.readouterr().err
    assert "past the 1.5s restart window" in err
    assert "the daemon closed the connection without a response" in err
    assert fake.polls() >= 3 and only_waits_for(fake, [JOB])


def test_c15_4_a_timeout_shorter_than_the_window_still_exits_124(fake, capsys):
    """`--timeout` bounds the reconnecting too: 124, and the stderr says why."""
    fake.start(this_process())

    def stop_for_good():
        until(lambda: fake.polls() >= 1)
        time.sleep(.05)
        fake.stop(unlink=True)
    joined = in_background(stop_for_good)
    started = time.monotonic()
    assert wait(JOB, "--timeout", "3") == int(Exit.WAIT_TIMEOUT)
    assert time.monotonic() - started <= 3 + 1.0
    joined()
    err = capsys.readouterr().err
    assert f"{JOB} still running after" in err and "(timeout)" in err
    assert "the daemon has not answered for" in err and "restart window" not in err


def test_c15_4_a_wait_that_never_reached_a_daemon_reports_it_absent_at_once(root, capsys):
    """C-17.5: with no daemon from the start there is no restart to ride out; the
    wait says so at once, with `subfleet daemon start`, whatever its `--timeout`."""
    for argv in ([JOB], [JOB, "--timeout", "600"]):
        started = time.monotonic()
        assert wait(*argv) == int(Exit.DAEMON_UNAVAILABLE)
        assert time.monotonic() - started < 2.0
        err = capsys.readouterr().err
        assert "no daemon at" in err and "subfleet daemon start" in err
        assert "restart window" not in err and not LINE.findall(err)


def test_c15_4_run_and_kill_wait_have_reached_the_daemon_already(fake, capsys):
    """`run --wait` and `kill --wait` wait right after the daemon answered them, so
    a socket gone at their first poll is a restart (`reached=True`)."""
    fake.start(this_process())
    fake.stop(unlink=True)                              # it answered the submit, then went

    def come_back():
        time.sleep(.5)
        fake.states[JOB] = ("succeeded", 0)
        fake.start(next_process(1))
    joined = in_background(come_back)
    args = cli.build_parser().parse_args(["wait", JOB])
    assert cli.wait_jobs(args, [JOB], timeout=30, reached=True) == 0
    joined()
    assert LINE.findall(capsys.readouterr().err) == ["restarted"]


def test_c15_4_the_hook_rides_out_a_restart_silently(fake, monkeypatch):
    """C-15.2 layer 2: the PostToolUse hook's wait reconnects too, and still says
    nothing but the notice."""
    delivered = []
    monkeypatch.setattr(hooks, "_deliver",
                        lambda client, session, job, stderr: delivered.append(job) or 2)
    fake.start(this_process())

    def restart():
        until(lambda: fake.polls() >= 1)
        time.sleep(.05)
        fake.stop(unlink=True)
        time.sleep(.6)
        fake.states[JOB] = ("succeeded", 0)
        fake.start(next_process(1))
    joined = in_background(restart)
    stderr = io.StringIO()
    client = Client(fake.root, verify_lock=False)       # as `post_tool_use` builds it (C-15.6)
    assert hooks._wait_and_deliver(client, "s-1", JOB, time.monotonic() + 60, stderr=stderr,
                                   now=time.monotonic, sleep=time.sleep) == 2
    joined()
    assert [job["state"] for job in delivered] == ["succeeded"]
    assert stderr.getvalue() == ""
    assert only_waits_for(fake, [JOB])


def test_c15_4_the_hook_gives_up_quietly_after_the_window(fake, monkeypatch):
    monkeypatch.setattr(client_module, "RESTART_WINDOW_S", 1.0)
    fake.start(this_process())

    def stop_for_good():
        until(lambda: fake.polls() >= 1)
        time.sleep(.05)
        fake.stop(unlink=True)
    joined = in_background(stop_for_good)
    started = time.monotonic()
    stderr = io.StringIO()
    assert hooks._wait_and_deliver(Client(fake.root, verify_lock=False), "s-1", JOB,
                                   time.monotonic() + 60, stderr=stderr,
                                   now=time.monotonic, sleep=time.sleep) == 0
    joined()
    # It asked again for the whole window (before D-WT1 it returned at the first loss).
    assert 1.0 <= time.monotonic() - started <= 1.0 + 1.0 + 2.0
    assert fake.polls() == 1 and stderr.getvalue() == ""


# --- properties over every interleaving (W1 to W6) -------------------------------

#: What a refused connect or a busy answer costs on the fake clock.
EPS = .001
JOBS = ("20261010-000001-a", "20261010-000002-b", "20261010-000003-c")
OUTSIDERS = [{"job_id": "20261010-999999-outsider", "state": "failed", "rc": 3},
             {"job_id": "20261010-999998-outsider", "state": "running"}]


def busy_answer() -> DaemonError:
    return DaemonError(69, "the daemon is busy: it holds 512 client connections, its limit",
                       "try again shortly")


def unavailable(cause: OSError) -> DaemonUnavailable:
    """What `_call_once` raises for a failed connect, with the cause it chains."""
    try:
        raise cause
    except OSError as caught:
        try:
            raise DaemonUnavailable(f"no daemon at daemon.sock: {caught}") from caught
        except DaemonUnavailable as exc:
            return exc


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.now += seconds


class ModelDaemon:
    """The daemon a property run waits on: a script of failures and answers, then a
    tail repeated for ever, on a fake clock.

    Steps: `busy` (answered busy), `refused` (connect refused, the socket file
    there), `gone` (the socket unlinked; a new process, so `daemon.lock` changes),
    `lost` (taken, then no answer after the step's seconds, at most the budget),
    `timeout` (answered with nothing new), `progress` (the next requested job ends,
    and the answer reports every requested job ended so far), and as tails `done`
    (every job ends and the answer says so) and `lost` for ever (a wedged daemon).
    An answer that would come after the caller's transport budget is lost (C-16.3).
    """

    def __init__(self, clock: Clock, script, tail: str, alive, outcomes, requested,
                 outsiders: bool):
        self.clock, self.script, self.tail, self.alive = clock, list(script), tail, alive
        self.outcomes = dict(zip(requested, outcomes))
        self.requested = list(requested)
        self.outsiders = outsiders
        self.ended: dict[str, dict] = {}
        self.reported: set[str] = set()
        self.generation = 0
        self.calls: list[tuple[str, dict, object, object]] = []
        self.log: list[tuple[float, str, int]] = []      # (time after the call, kind, generation)
        self.tail_started: float | None = None

    def lock_info(self) -> dict:
        return {"pid": 4000 + self.generation, "proc_start": "Sat Oct 10 09:00:00 2026"}

    def lock_holder_alive(self):
        return self.alive

    def _end(self, job_id: str) -> None:
        state, rc = self.outcomes[job_id]
        self.ended.setdefault(job_id, {"job_id": job_id, "state": state, "rc": rc})

    def call(self, op, args=None, *, timeout=None, retry_busy=None, request_id=""):
        args = dict(args or {})
        self.calls.append((op, args, retry_busy, timeout))
        index = len(self.log)
        if index < len(self.script):
            kind, seconds = self.script[index]
        else:
            if self.tail_started is None:
                self.tail_started = self.clock.now
            kind, seconds = self.tail, (POLL_BUDGET_S if self.tail == "lost" else 0.0)
        budget = float(timeout)
        if kind in ("busy", "refused", "gone"):
            self.clock.now += min(EPS, budget)
            if kind == "gone":
                self.generation += 1
            self.log.append((self.clock.now, kind, self.generation))
            if kind == "busy":
                raise busy_answer()
            raise unavailable(ConnectionRefusedError(61, "Connection refused") if kind == "refused"
                              else FileNotFoundError(2, "No such file or directory"))
        if kind == "progress":
            pending = [job_id for job_id in self.requested if job_id not in self.ended]
            if pending:
                self._end(pending[0])
        elif kind == "done":
            for job_id in self.requested:
                self._end(job_id)
        if kind == "timeout":
            seconds = min(seconds, float(args.get("deadline_s", WAIT_POLL_MAX_S)))
        if kind == "lost" or seconds >= budget:
            self.clock.now += min(seconds, budget)
            self.log.append((self.clock.now, "lost", self.generation))
            raise ResponseLost("the daemon closed the connection without a response",
                               op=op, request_id=request_id)
        self.clock.now += seconds
        rows = [self.ended[job_id] for job_id in args.get("job_ids") or () if job_id in self.ended]
        self.reported.update(row["job_id"] for row in rows)
        self.log.append((self.clock.now, "answer", self.generation))
        return {"jobs": [dict(row) for row in rows] + (OUTSIDERS if self.outsiders else []),
                "timeout": not rows}


def reference(log, *, reached: bool, alive, timeout, window: float, hook: bool = False):
    """C-15.4 and C-16.7, stated apart from the code: what each poll's result means.

    `answered` and `back` (an answer that ended an outage) go on; `backlog` is a
    connect refused behind a busy daemon (C-16.7), which goes on too; `outage` is
    a poll a restart can explain, inside the window; `give-up` is one past it (or
    `timeout`, when `--timeout` has passed as well, which wins); `absent` is a
    connect that failed before any daemon answered this wait (C-17.5). Returns the
    verdicts and, for each outage, when it began."""
    busy, since, verdicts, began = False, None, [], []
    for at, kind, _generation in log:
        if kind in ("answer", "busy"):
            verdicts.append("back" if since is not None else "answered")
            since, busy, reached = None, kind == "busy", True
        elif kind in ("refused", "gone") and not reached:
            verdicts.append("absent")
        elif kind == "refused" and busy and (alive is not False if hook
                                             else alive is True or (alive is None and timeout is not None)):
            verdicts.append("backlog")
        else:
            busy, reached = False, True
            if since is None:
                since = at
                began.append(at)
            if at - since <= window:
                verdicts.append("outage")
            elif timeout is not None and at >= timeout:
                verdicts.append("timeout")
            else:
                verdicts.append("give-up")
    return verdicts, began


def expected_lines(log, verdicts) -> list[str]:
    """W6: per outage that an answer ended, "restarted" when the lock's process
    changed since the answer before it (or the wait's start)."""
    lines, generation = [], 0
    for (_at, _kind, now), verdict in zip(log, verdicts):
        if verdict == "back":
            lines.append("restarted" if now != generation else "answered again")
        if verdict in ("back", "answered"):
            generation = now
    return lines


STEP = st.one_of(
    st.tuples(st.sampled_from(("busy", "refused", "gone")), st.just(0.0)),
    st.tuples(st.sampled_from(("lost", "timeout", "progress")),
              st.floats(0.0, 80.0, allow_nan=False, allow_infinity=False)),
)
OUTCOME = st.sampled_from([("succeeded", 0), ("failed", 1), ("failed", 3), ("cancelled", 130)])


def run_wait(script, tail, alive, outcomes, count, timeout, reached, window, outsiders):
    clock = Clock()
    requested = list(JOBS[:count])
    model = ModelDaemon(clock, script, tail, alive, outcomes, requested, outsiders)
    stderr = io.StringIO()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))
        patch.setattr(cli, "_client", lambda *a, **k: model)
        patch.setattr(client_module, "RESTART_WINDOW_S", window)
        # The pause's jitter, fixed, so two runs of one interleaving are one run (W4).
        patch.setattr(client_module, "random", SimpleNamespace(random=lambda: .5))
        args = cli.build_parser().parse_args(["wait", *requested])
        with contextlib.redirect_stderr(stderr):
            code = cli.wait_jobs(args, requested, timeout=timeout, reached=reached)
    return code, model, clock.now, stderr.getvalue()


@settings(max_examples=300, deadline=None, derandomize=True)
@given(script=st.lists(STEP, max_size=24), tail=st.sampled_from(("gone", "lost", "done")),
       alive=st.sampled_from((True, False, None)), outcomes=st.lists(OUTCOME, min_size=3, max_size=3),
       count=st.integers(1, 3), reached=st.booleans(),
       timeout=st.one_of(st.none(), st.floats(.5, 400.0, allow_nan=False)),
       window=st.sampled_from((.5, 3.0, 180.0)))
def test_c15_4_wait_invariants_over_every_interleaving(script, tail, alive, outcomes, count,
                                                       reached, timeout, window):
    code, model, elapsed, err = run_wait(script, tail, alive, outcomes, count, timeout,
                                         reached, window, outsiders=True)
    requested = model.requested
    verdicts, began = reference(model.log, reached=reached, alive=alive, timeout=timeout,
                                window=window)
    stops = ("give-up", "absent")

    # W5: only reads, only for requested jobs, each poll handing busy back to the loop.
    for op, args, retry_busy, _budget in model.calls:
        assert op == "wait" and retry_busy is False
        assert set(args["job_ids"]) <= set(requested) and args["mine"] is None and not args["last"]
        assert 1 <= args["deadline_s"] <= WAIT_POLL_MAX_S

    # W3: no poll before the last was one the rule stops at; the CLI asked again.
    assert not any(verdict in stops for verdict in verdicts[:-1]), (verdicts, model.log)
    if verdicts and verdicts[-1] in stops:
        last_kind = model.log[-1][1]
        assert code == (int(Exit.OPERATIONAL) if last_kind == "lost" else int(Exit.DAEMON_UNAVAILABLE))
        assert ("restart window" in err) == (verdicts[-1] == "give-up")
        if verdicts[-1] == "absent":
            assert len(model.log) == 1                       # C-17.5: at once
    else:
        # W1: once every requested job was answered, the exit is theirs; else 124.
        rows = [model.ended[job_id] for job_id in requested if job_id in model.reported]
        exits = [cli.exit_for_job(row, quiet=True) for row in rows]
        missing = [job_id for job_id in requested if job_id not in model.reported]
        assert code == max(exits + ([int(Exit.WAIT_TIMEOUT)] if missing else [])), (code, verdicts)
        if missing:
            assert timeout is not None and elapsed >= timeout - 1e-9   # W2: never early
        else:
            assert model.log[-1][1] == "answer"
    if code == 0:
        assert all(model.ended.get(job_id, {}).get("state") == "succeeded" and job_id in model.reported
                   for job_id in requested)

    # W2: `--timeout` bounds every path that reached it.
    if timeout is not None:
        assert elapsed <= timeout + POLL_BUDGET_S
        assert elapsed <= timeout + .2 + 1e-6, (elapsed, timeout)
    # W3: a daemon gone for good ends the wait within the window of its outage.
    if timeout is None and tail != "done" and model.tail_started is not None and began:
        assert elapsed <= began[-1] + window + POLL_BUDGET_S + 1.0 + EPS, (elapsed, began)
        if tail == "gone":
            assert elapsed <= max(began[-1], model.tail_started) + window + 1.0 + 2 * EPS

    # W6: one line per outage an answer ended, naming a restart only for a new process.
    assert LINE.findall(err) == expected_lines(model.log, verdicts)

    # W4: the outsiders the daemon mentioned changed nothing.
    alone = run_wait(script, tail, alive, outcomes, count, timeout, reached, window, outsiders=False)
    assert (alone[0], alone[1].log, alone[2], alone[3]) == (code, model.log, elapsed, err)


def run_hook(script, tail, alive, budget, window):
    clock = Clock()
    clock.now = 1000.0
    model = ModelDaemon(clock, script, tail, alive, [("succeeded", 0)], [JOBS[0]], outsiders=False)
    delivered = []
    stderr = io.StringIO()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hooks, "_deliver", lambda client, session, job, stderr: delivered.append(job) or 2)
        patch.setattr(client_module, "RESTART_WINDOW_S", window)
        patch.setattr(client_module, "random", SimpleNamespace(random=lambda: .5))
        code = hooks._wait_and_deliver(model, "s-1", JOBS[0], clock.now + budget, stderr=stderr,
                                       now=clock.monotonic, sleep=clock.sleep)
    return code, model, clock.now, delivered, stderr.getvalue()


@settings(max_examples=300, deadline=None, derandomize=True)
@given(script=st.lists(STEP, max_size=24), tail=st.sampled_from(("gone", "lost", "done")),
       alive=st.sampled_from((True, False, None)),
       budget=st.floats(1.0, 600.0, allow_nan=False), window=st.sampled_from((.5, 3.0, 180.0)))
def test_c15_4_hook_wait_invariants_over_every_interleaving(script, tail, alive, budget, window):
    """The PostToolUse hook's wait: delivered only once its job was answered ended;
    silent otherwise; never gives up inside the window; bounded by its deadline
    plus one poll's transport slack (10 s); only `wait`."""
    code, model, clock_end, delivered, err = run_hook(script, tail, alive, budget, window)
    deadline = 1000.0 + budget
    verdicts, _began = reference(model.log, reached=True, alive=alive, timeout=None,
                                 window=window, hook=True)
    for op, args, retry_busy, _transport in model.calls:
        assert op == "wait" and retry_busy is False and args["job_ids"] == [JOBS[0]]
    assert err == ""
    assert not any(verdict == "give-up" for verdict in verdicts[:-1]), verdicts
    if code == 2:
        assert delivered and delivered[0]["job_id"] in model.reported
        assert model.log[-1][1] == "answer"
    else:
        assert code == 0 and not delivered
        # Silent: past the window at its last poll, or out of time.
        assert (verdicts and verdicts[-1] == "give-up") or clock_end >= deadline - 1e-9, verdicts
    assert clock_end <= deadline + 10 + EPS
