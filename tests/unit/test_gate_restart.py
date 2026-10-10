"""C-15.4: `subfleet gate` rides out a daemon restart (defect D-WT1's gate half).

`subfleet gate` started or continued a gate and then polled it; any lost answer
(`ResponseLost`) or socket gone or refused ended it with exit 1 while the gate
carried on in the daemon, so an install or a `launchctl kickstart -k` during a
gate woke its session, which had to know to resume. `gate.poll` advances the gate
from its journaled state, so a poll sent again does what the next poll would have
done (`tests/unit/test_gate_poll_resend.py` proves that on the real service). The
poll loop therefore asks again, for `RESTART_WINDOW_S` per outage, as `wait` does.

The first half drives the real gate CLI and the real client over a Unix socket
held by `FakeGateDaemon`, which each test stops and starts. The second half states
the loop's invariants as properties over every interleaving Hypothesis draws of
answered, lost, refused, gone, recovering, busy and hung polls on a fake clock,
each run checked against `reference`, an independent statement of the rule:

* G1 (C-17.1) the exit is the gate's code exactly when an answer with a code
  reached the CLI, and 1 otherwise; never 69, 124 or anything outside 0 to 5;
* G2 after one `gate.start`, nothing is sent but `gate.poll` for that gate;
* G3 no poll follows an answer with a code;
* G4 the loop never gives up inside the restart window, and a daemon gone for good
  ends it within the window, one poll budget and one pause of the first failure;
* G5 one stderr line per outage that an answer ended, saying "restarted" exactly
  when `daemon.lock` names another process than at the answer before;
* G6 an exit 1 after the gate is known names `subfleet gate poll <id>`.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import cli
from subfleet import client as client_module
from subfleet.client import DaemonError, DaemonUnavailable, ResponseLost
from subfleet.gate import cli as gate_cli
from subfleet.gate.service import RECOVERING

GATE = "20261010-090600-plan-0123abcd"
LINE = re.compile(r"subfleet gate: daemon (restarted|answered again); still polling gate (\S+)")
RESUME = f"resume with: subfleet gate poll {GATE}"


@pytest.fixture(autouse=True)
def unmanaged(monkeypatch):
    for key in gate_cli.MANAGED:
        monkeypatch.delenv(key, raising=False)


def plan_args(tmp_path: Path, *extra: str):
    plan = tmp_path / "plan.md"
    plan.write_text("A plan.\n")
    return cli.build_parser().parse_args(
        ["gate", "plan", str(plan), "--peer", "astra", "--main-approve",
         "--expect-sha256", hashlib.sha256(plan.read_bytes()).hexdigest(), *extra])


# --- a gate daemon this test restarts ---------------------------------------------

class Gate:
    """The daemon's gate, as far as a poller can tell: `steps` executed polls take
    it to a code, and a poll on a gate with a code only reads (D4)."""

    def __init__(self, steps: int, code: int):
        self.steps, self.code = steps, code
        self.executed = 0           # polls the daemon ran (an answer may still be lost)
        self.advanced = 0

    def poll(self) -> dict:
        self.executed += 1
        if self.advanced < self.steps:
            self.advanced += 1
        done = self.advanced >= self.steps
        return {"gate_id": GATE, "status": "completed" if done else "reviewing",
                "code": self.code if done else None, "job_id": "job-1", "round": 1}


class FakeGateDaemon:
    """A `daemon.sock` that answers `gate.start`, `gate.continue` and `gate.poll`.

    `stop()` does to its clients what an install or a kickstart does: each request
    it holds is closed unanswered, and its socket file goes (`unlink=True`, a stop
    that cleans up) or stays with nothing listening (a crash), so a connect finds it
    gone or refused. `start()` writes the new process's identity to `daemon.lock`
    before it listens, as `Daemon.__init__` does. `hold` holds each poll's answer
    that long after running it (so a stop loses an answer whose poll ran);
    `recovering` answers every gate op as a daemon still in startup recovery does;
    `drop` closes named ops unanswered after reading them; `mute` never answers.
    """

    def __init__(self, root: Path, gate: Gate):
        self.root, self.gate = root, gate
        self.path = root / "daemon.sock"
        self.requests: list[dict] = []
        self.hold = 0.0
        self.recovering = False
        self.drop: set[str] = set()
        self.mute = False
        self._lock = threading.Lock()
        self._held: set[socket.socket] = set()
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self, identity: dict) -> "FakeGateDaemon":
        (self.root / "daemon.lock").write_text(json.dumps(identity))
        self.path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.path))
        listener.listen(16)
        # A close from another thread does not wake a blocked accept on macOS, so
        # the accepting thread polls its stop flag and closes its own listener.
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
        for conn in held:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                conn.close()
        if unlink:
            self.path.unlink(missing_ok=True)

    def ops(self) -> list[str]:
        with self._lock:
            return [r["op"] for r in self.requests]

    def _accept(self, listener, stop) -> None:
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

    def _serve(self, conn) -> None:
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
            op = request.get("op")
            if self.mute or op in self.drop:
                if self.mute:
                    self._held_until_stop(conn, float("inf"))
                return
            if self.recovering and op.startswith("gate."):
                result = dict(RECOVERING)
            elif op in ("gate.start", "gate.continue"):
                result = {"gate_id": GATE, "job_id": "job-1", "code": None, "status": "reviewing"}
            elif op == "gate.poll":
                result = self.gate.poll()
                if self.hold and not self._held_until_stop(conn, self.hold):
                    return                              # closed by stop() while held
            else:
                result = {}
            conn.sendall((json.dumps({"v": 1, "id": request.get("id", ""), "ok": True,
                                      "result": result}) + "\n").encode())
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                self._held.discard(conn)
            with contextlib.suppress(OSError):
                conn.close()

    def _held_until_stop(self, conn, seconds: float) -> bool:
        ends = time.monotonic() + seconds
        while time.monotonic() < ends:
            with self._lock:
                if conn not in self._held:
                    return False
            time.sleep(.01)
        return True


def this_process() -> dict:
    """A lock naming this process as it is, so the client's one lock check passes."""
    pid = os.getpid()
    return {"pid": pid, "boot_id": client_module.boot_id(),
            "proc_start": client_module.proc_start(pid), "version": "test"}


def next_process(number: int) -> dict:
    return {**this_process(), "proc_start": f"restarted-{number}"}


def until(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(.01)
    raise AssertionError("condition timed out")


@pytest.fixture
def fake(root):
    """`root` (tests/unit/conftest.py) is short enough to hold a unix socket."""
    daemon = FakeGateDaemon(Path(root).resolve(), Gate(steps=3, code=3))
    try:
        yield daemon
    finally:
        daemon.stop(unlink=True)


def gate(fake, tmp_path, *argv, background: bool = False):
    """Run `subfleet gate` against `fake`, in a thread when `background`."""
    args = plan_args(tmp_path) if not argv else cli.build_parser().parse_args(["gate", *argv])
    out, err, box = io.StringIO(), io.StringIO(), {}

    def body():
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            box["rc"] = gate_cli.run(args, root=fake.root, poll_interval=.02)
            box["at"] = time.monotonic()
    if not background:
        body()
        return box["rc"], out.getvalue(), err.getvalue()
    thread = threading.Thread(target=body, daemon=True)
    thread.start()

    def joined(timeout=30.0):
        thread.join(timeout)
        assert not thread.is_alive(), "subfleet gate did not end"
        return box["rc"], out.getvalue(), err.getvalue(), box["at"]
    return joined


# --- the real CLI and client over a socket that goes away -------------------------

def test_c15_4_a_poll_closed_unanswered_by_a_stop_is_asked_of_the_next_daemon(fake, tmp_path):
    """A kickstart's SIGTERM: the held poll is closed unanswered (its poll ran), the
    socket goes, and the next daemon answers. The gate ends with its own code, and
    the gate advanced once per poll the daemons ran, never more (the lost answer's
    poll is not run twice: it ran once, and the next poll took the next step)."""
    fake.start(this_process())
    fake.hold = 30.0
    joined = gate(fake, tmp_path, background=True)
    until(lambda: fake.ops().count("gate.poll") >= 1)
    fake.stop(unlink=True)
    executed = fake.gate.executed
    time.sleep(.3)
    fake.hold = 0.0
    fake.start(next_process(1))
    rc, out, err, _ = joined()
    assert rc == 3, err                       # changes requested: the gate's code (C-17.1)
    assert [m.group(1) for m in LINE.finditer(err)] == ["restarted"], err
    assert "without a response" not in err and "restart window" not in err
    assert fake.gate.advanced == fake.gate.steps == 3 and executed >= 1
    assert fake.ops()[0] == "gate.start" and set(fake.ops()[1:]) == {"gate.poll"}
    assert "completed" in out


def test_c15_4_connects_refused_for_a_while_are_ridden_out(fake, tmp_path):
    """A crash leaves the socket file with nothing listening, so a connect is
    refused, for 1.5 s here, until launchd's KeepAlive starts the next daemon."""
    fake.start(this_process())
    fake.hold = 30.0
    joined = gate(fake, tmp_path, background=True)
    until(lambda: fake.ops().count("gate.poll") >= 1)
    fake.stop(unlink=False)
    assert fake.path.exists()
    time.sleep(1.5)
    fake.hold = 0.0
    fake.start(next_process(1))
    rc, _out, err, _ = joined()
    assert rc == 3 and LINE.findall(err) == [("restarted", GATE)], err


def test_c15_4_a_daemon_gone_for_good_ends_the_gate_after_the_window(fake, tmp_path, monkeypatch):
    """Past the window: exit 1 with the reason, after one line saying how long, and
    a line naming the command that resumes the gate (C-15.4, C-17.1)."""
    monkeypatch.setattr(client_module, "RESTART_WINDOW_S", 1.0)
    fake.start(this_process())
    fake.hold = 30.0
    joined = gate(fake, tmp_path, background=True)
    until(lambda: fake.ops().count("gate.poll") >= 1)
    stopped = time.monotonic()
    fake.stop(unlink=True)
    rc, _out, err, ended = joined()
    assert rc == 1, err
    assert 1.0 <= ended - stopped <= 1.0 + 1.0 + 1.0, ended - stopped
    assert re.search(rf"has not served gate {GATE} for \d+s, and the 1s restart window from its "
                     r"first failed poll has passed", err), err
    assert "no daemon at" in err and RESUME in err
    assert not LINE.search(err)


def test_c15_4_a_daemon_still_recovering_is_asked_again_and_runs_nothing(fake, tmp_path):
    """The next daemon answers gate ops before its startup recovery has run
    (`service.RECOVERING`); that answer did nothing, so the poll is asked again,
    within the window, and the outage ends at the first real answer."""
    fake.start(this_process())
    fake.hold = 30.0
    joined = gate(fake, tmp_path, background=True)
    until(lambda: fake.ops().count("gate.poll") >= 1)
    fake.stop(unlink=True)
    executed = fake.gate.executed
    fake.hold, fake.recovering = 0.0, True
    fake.start(next_process(1))
    until(lambda: fake.ops().count("gate.poll") >= 3)
    assert fake.gate.executed == executed     # recovering answers ran nothing
    fake.recovering = False
    rc, _out, err, _ = joined()
    assert rc == 3 and LINE.findall(err) == [("restarted", GATE)], err


def test_c15_4_a_daemon_recovering_past_the_window_ends_with_its_answer(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(client_module, "RESTART_WINDOW_S", 1.0)
    fake.gate.steps = 10**6                   # never done: the test, not the gate, ends it
    fake.start(this_process())
    joined = gate(fake, tmp_path, background=True)
    until(lambda: fake.ops().count("gate.poll") >= 1)
    fake.recovering = True
    rc, _out, err, _ = joined()
    assert rc == 1 and "daemon recovery is in progress" in err and RESUME in err, err
    assert "the 1s restart window from its first failed poll has passed" in err


def test_c15_4_a_daemon_that_drops_every_poll_ends_after_the_window(fake, tmp_path, monkeypatch):
    """A daemon that takes each poll and closes it unanswered (not a restart: its
    lock never changes) is asked for the window, then the lost answer is the exit."""
    monkeypatch.setattr(client_module, "RESTART_WINDOW_S", 1.0)
    fake.start(this_process())
    fake.drop = {"gate.poll"}
    rc, _out, err = gate(fake, tmp_path)
    assert rc == 1 and "closed the connection without a response" in err and RESUME in err, err
    assert fake.ops().count("gate.poll") >= 3
    assert fake.ops().count("gate.start") == 1


def test_c15_4_a_hung_daemon_is_bounded_by_the_window_and_one_poll_budget(fake, tmp_path, monkeypatch):
    """A daemon that accepts and never answers: each poll waits its whole budget
    (`POLL_TIMEOUT_S`, shortened here), and the loop gives up within the window,
    one poll budget and one pause of the first poll that failed (G4)."""
    monkeypatch.setattr(client_module, "RESTART_WINDOW_S", .6)
    monkeypatch.setattr(gate_cli, "POLL_TIMEOUT_S", .4)
    fake.gate.steps = 10**6
    fake.start(this_process())
    joined = gate(fake, tmp_path, background=True)
    until(lambda: fake.ops().count("gate.poll") >= 1)
    fake.mute = True
    muted = time.monotonic()
    rc, _out, err, ended = joined()
    assert rc == 1 and "no response from the daemon within 0.4s" in err and RESUME in err, err
    # The first poll sent after the mute fails one budget later; from there, the
    # window, one more budget and one pause, with scheduling slack.
    first_failure = muted + .05 + .4
    assert ended - first_failure <= .6 + .4 + 1.0 + .5, ended - muted


def test_c15_4_each_outage_has_a_window_of_its_own(fake, tmp_path, monkeypatch):
    """An install followed by a kickstart: two outages, each under the window and
    together over it, are ridden out twice, with one line each."""
    monkeypatch.setattr(client_module, "RESTART_WINDOW_S", 1.0)
    fake.gate.steps = 10**6                   # never done until the test says so
    fake.start(this_process())
    joined = gate(fake, tmp_path, background=True)
    for number in (1, 2):
        until(lambda: fake.ops().count("gate.poll") >= 1)
        fake.stop(unlink=True)
        time.sleep(.7)
        fake.start(next_process(number))
        polls = fake.ops().count("gate.poll")
        until(lambda: fake.ops().count("gate.poll") > polls + 1)
    fake.gate.steps = fake.gate.advanced + 1
    rc, _out, err, _ = joined()
    assert rc == 3 and [m.group(1) for m in LINE.finditer(err)] == ["restarted", "restarted"], err


def test_c15_4_an_answer_lost_by_a_daemon_that_never_restarted_says_answered_again(fake, tmp_path):
    """A lost answer from a daemon whose lock never changed is no restart, so the
    line does not claim one."""
    fake.start(this_process())
    fake.drop = {"gate.poll"}
    joined = gate(fake, tmp_path, background=True)
    until(lambda: fake.ops().count("gate.poll") >= 2)
    fake.drop = set()
    rc, _out, err, _ = joined()
    assert rc == 3 and LINE.findall(err) == [("answered again", GATE)], err


def test_c15_4_a_lost_start_is_not_sent_again(fake, tmp_path):
    """A start may have made a gate (C-16.3), and its id was never received: it is
    not sent again, and nothing names a gate it cannot know."""
    fake.start(this_process())
    fake.drop = {"gate.start"}
    rc, _out, err = gate(fake, tmp_path)
    assert rc == 1 and "without a response" in err and "gate poll" not in err, err
    assert fake.ops() == ["gate.start"]


def test_c15_4_a_lost_continue_is_not_sent_again_and_names_the_poll(fake, tmp_path):
    """A continue may have reserved a round: it is not sent again, and the exit names
    `subfleet gate poll`, which shows where the gate stands and follows it."""
    fake.start(this_process())
    fake.drop = {"gate.continue"}
    rc, _out, err = gate(fake, tmp_path, "continue", GATE)
    assert rc == 1 and "without a response" in err and RESUME in err, err
    assert fake.ops() == ["gate.continue"]


def test_c15_4_a_start_answered_recovering_is_sent_again(fake, tmp_path):
    """A recovering daemon did nothing with the start, so it is sent again."""
    fake.recovering = True
    fake.start(this_process())
    joined = gate(fake, tmp_path, background=True)
    until(lambda: fake.ops().count("gate.start") >= 3)
    fake.recovering = False
    rc, _out, err, _ = joined()
    assert rc == 3, err
    assert fake.gate.executed >= 1 and set(fake.ops()) == {"gate.start", "gate.poll"}


def test_c15_4_gate_poll_resumes_a_gate_and_finds_no_daemon_at_once(fake, tmp_path):
    """`subfleet gate poll <id>`: the loop alone, for a gate whose command has gone.
    It sends nothing but polls, ends with the gate's code, and, when no daemon has
    answered it at all, ends at once (no restart is in progress)."""
    started = time.monotonic()
    rc, _out, err = gate(fake, tmp_path, "poll", GATE)
    assert rc == 1 and "no daemon at" in err and time.monotonic() - started < 1.0, err
    fake.start(this_process())
    rc, out, err = gate(fake, tmp_path, "poll", GATE, "--json")
    assert rc == 3 and json.loads(out)["status"] == "completed", err
    assert set(fake.ops()) == {"gate.poll"}


def test_gate_poll_is_a_gate_verb():
    """C-17.1: `poll` joins `pr`, `plan` and `continue`, on both entry points."""
    args = cli.build_parser().parse_args(["gate", "poll", GATE, "--json"])
    assert (args.gate_command, args.gate_id, args.json) == ("poll", GATE, True)
    parser = __import__("argparse").ArgumentParser()
    gate_cli.configure(parser)
    assert parser.parse_args(["poll", GATE]).gate_id == GATE


def test_an_interrupt_after_the_start_names_the_poll(tmp_path, capsys):
    """An interrupted `subfleet gate` leaves its gate in the daemon and says how to
    resume it, keeping the line it has always printed."""
    class Client:
        def call(self, op, args, **kwargs):
            if op == "gate.start":
                return {"gate_id": GATE, "job_id": "job-1", "code": None}
            raise KeyboardInterrupt

        def lock_info(self):
            return None
    assert gate_cli.run(plan_args(tmp_path), client=Client(), root=tmp_path, poll_interval=0) == 1
    err = capsys.readouterr().err
    assert "interrupted; the peer job remains in the daemon store" in err and RESUME in err


# --- properties over every interleaving (G1 to G6) --------------------------------

POLL_BUDGET = gate_cli.POLL_TIMEOUT_S
INTERVAL = .25
OUTCOMES = ("answer", "lost-after", "lost-before", "timeout", "refused", "gone",
            "recovering", "busy-out")


class Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.now += seconds


class ScriptedClient:
    """A daemon reached through `call`, its polls' fates drawn from `script`.

    Each entry is (outcome, epoch): `epoch` numbers the daemon process that
    `daemon.lock` names while that poll is tried. `answer`, `lost-after` and
    `timeout` run the poll (a lost answer's poll may have run); `lost-before`,
    `refused`, `gone`, `recovering` and `busy-out` run nothing. `timeout` and
    `busy-out` take time: a poll's whole budget, half of it. Past the script every
    poll is answered by the last epoch's daemon.
    """

    def __init__(self, script, gate: Gate, clock: Clock):
        self.script, self.gate, self.clock = list(script), gate, clock
        self.sent: list[tuple[str, str | None]] = []
        self.epoch = 0
        self.answered_code = False
        self.after_code = 0

    def lock_info(self):
        return {"pid": 1000 + self.epoch, "proc_start": f"epoch-{self.epoch}"}

    def call(self, op, args, *, timeout=None, **_):
        self.sent.append((op, (args or {}).get("gate_id")))
        if self.answered_code:
            self.after_code += 1                        # G3 counts these
        if op == "gate.start":
            return {"gate_id": GATE, "job_id": "job-1", "code": None}
        outcome, epoch = self.script.pop(0) if self.script else ("answer", self.epoch)
        self.epoch = epoch
        if outcome == "answer":
            result = self.gate.poll()
            self.answered_code = result["code"] is not None
            return result
        if outcome == "lost-after":
            self.gate.poll()
            raise ResponseLost("the daemon closed the connection without a response")
        if outcome == "lost-before":
            raise ResponseLost("the daemon closed the connection without a response")
        if outcome == "timeout":
            self.gate.poll()
            self.clock.now += timeout
            raise ResponseLost(f"no response from the daemon within {timeout:g}s")
        if outcome in ("refused", "gone"):
            error = ConnectionRefusedError() if outcome == "refused" else FileNotFoundError()
            raise DaemonUnavailable(f"no daemon at daemon.sock: {error!r}") from error
        if outcome == "recovering":
            return dict(RECOVERING)
        if outcome == "busy-out":
            self.clock.now += timeout / 2
            raise DaemonError(69, "the daemon is busy: serving 512 connections", "try again shortly")
        raise AssertionError(outcome)


def pause(streak: int) -> float:
    """C-16.7's pause with no random shortening, stated again here."""
    return min(1.0, .05 * 2 ** min(streak - 1, 10))


def reference(script, steps: int, code: int, window: float) -> dict:
    """The rule of C-15.4 for the gate's poll loop, stated separately from `follow`.

    One `gate.start`, answered at 0; a pause of the poll interval; then polls. An
    answer with no code waits the interval and polls again; one with a code is the
    exit. A lost, refused, gone or recovering poll is an outage: it starts at the
    first such poll's failure and each failure inside `window` of that start waits
    C-16.7's pause and polls again; past it the loop gives up with exit 1. A busy
    poll that ran out its budget is the exit 1 it always was. An outage that an
    answer ends prints one line: restarted when that answer's lock names another
    epoch than the answer before it.
    """
    now, epoch_seen, since, streak, advanced = INTERVAL, 0, None, 0, 0
    lines, polls = [], 0
    last = script[-1][1] if script else 0          # past the script, that daemon answers
    script = list(script)
    while True:
        outcome, epoch = script.pop(0) if script else ("answer", last)
        polls += 1
        if outcome in ("answer", "lost-after", "timeout") and advanced < steps:
            advanced += 1
        if outcome == "timeout":
            now += POLL_BUDGET
        if outcome == "busy-out":
            now += POLL_BUDGET / 2
            return {"rc": 1, "polls": polls, "lines": lines, "end": now, "gave_up": False}
        if outcome == "answer":
            if since is not None:
                lines.append("restarted" if epoch != epoch_seen else "answered again")
                since = None
            epoch_seen = epoch
            if advanced >= steps:
                return {"rc": code, "polls": polls, "lines": lines, "end": now, "gave_up": False}
            streak = 0
            now += INTERVAL
            continue
        if since is None:
            since = now
        if now - since > window:
            return {"rc": 1, "polls": polls, "lines": lines, "end": now, "gave_up": True,
                    "since": since}
        streak += 1
        now += pause(streak)


@st.composite
def scripts(draw):
    """Polls' fates; the epoch moves on only at a failure (a restart is an outage)."""
    entries, epoch = [], 0
    for outcome in draw(st.lists(st.sampled_from(OUTCOMES), max_size=40)):
        if outcome in ("lost-after", "lost-before", "refused", "gone", "timeout", "recovering") \
                and draw(st.booleans()):
            epoch += 1
        entries.append((outcome, epoch))
    return entries


@settings(max_examples=400, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(script=scripts(), steps=st.integers(1, 6), code=st.sampled_from([0, 2, 3, 4, 5]),
       window=st.sampled_from([.5, 3.0, 180.0]))
def test_c15_4_gate_poll_loop_properties(script, steps, code, window):
    """G1 to G6 on a fake clock, and the run equal to `reference` (a differential
    test of the loop's decisions: exit, polls sent, lines, when it ended)."""
    clock = Clock()
    gate = Gate(steps, code)
    client = ScriptedClient(script, gate, clock)
    err, out = io.StringIO(), io.StringIO()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(gate_cli, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=clock.sleep))
        patch.setattr(client_module, "random", SimpleNamespace(random=lambda: 0.0))
        patch.setattr(client_module, "RESTART_WINDOW_S", window)
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out), \
                contextlib.ExitStack() as stack:
            directory = stack.enter_context(__import__("tempfile").TemporaryDirectory())
            rc = gate_cli.run(plan_args(Path(directory)), client=client, root=Path(directory),
                              poll_interval=INTERVAL)
    expected = reference(script, steps, code, window)
    text = err.getvalue()
    polls = [sent for sent in client.sent if sent[0] == "gate.poll"]
    # G1: the gate's code exactly when an answer with one came; otherwise 1.
    assert rc in {0, 1, 2, 3, 4, 5}
    assert (rc == code and client.answered_code) or (rc == 1 and not client.answered_code)
    # G2: one start, then polls of that gate and nothing else.
    assert client.sent[0] == ("gate.start", None)
    assert all(sent == ("gate.poll", GATE) for sent in client.sent[1:])
    # G3: no poll after an answer with a code.
    assert client.after_code == 0
    # G4: no give-up inside the window; a give-up within window + budget + pause.
    if expected["gave_up"]:
        assert clock.now - expected["since"] > window
        assert clock.now <= expected["since"] + window + POLL_BUDGET + 1.0
        assert "restart window" in text
    else:
        assert "restart window" not in text
    # G5: one line per outage an answer ended, naming a restart only for a new epoch.
    assert [m.group(1) for m in LINE.finditer(text)] == expected["lines"]
    assert all(m.group(2) == GATE for m in LINE.finditer(text))
    # G6: an exit 1 names the command that resumes the gate.
    assert (RESUME in text) == (rc == 1)
    # The differential half: the same decisions as the reference.
    assert (rc, len(polls), clock.now) == (expected["rc"], expected["polls"], pytest.approx(expected["end"]))
