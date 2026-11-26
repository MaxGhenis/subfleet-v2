"""A SIGSTOPped lock holder: the diagnosis, where it appears, and its fix.

Every test names the clause it proves (C-20.5). The holder is a real process
that is really stopped, because the whole question is what `ps` says about it;
it is stopped only after it has finished starting, no test begins until `ps`
reads it as stopped, and it is continued, killed, and reaped however the test
ends.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from subfleet import cli, client as client_module, doctor
from subfleet.client import (Client, DaemonUnavailable, OutcomeUnknown,
                             ResponseLost, boot_id, proc_start)
from subfleet.contracts import Exit


@pytest.fixture(autouse=True)
def no_real_marker(root, monkeypatch):
    """C-5.13 attribution reads a file on this machine; tests read their own or none."""
    monkeypatch.setattr(client_module, "KNOWN_PAUSERS", (
        {**getattr(client_module, "KNOWN_PAUSERS", ({"name": "clamshell-guard"},))[0], "marker": str(root / "no-such-marker")},), raising=False)


#: The holder's program: one line once the interpreter it runs in is up, then sleep.
HOLDER = "import fcntl, sys, time; lock = open(sys.argv[1], 'a+'); fcntl.flock(lock, fcntl.LOCK_EX); sys.stdout.write('ready\\n'); sys.stdout.flush(); time.sleep(300)"
#: How long the holder may take to start, and `ps` to read a SIGSTOPped one as stopped.
HOLDER_READY_S = 30.0
STOP_SETTLE_S = 10.0
CHILD_STATES: dict[int, list[str]] = {}


@contextlib.contextmanager
def holder(root, monkeypatch):
    """A live process past its own startup, recorded in `daemon.lock` (C-5.8).

    It is handed over only after it has written its line, because a SIGSTOP
    that lands while a process is still starting can be lost, and on the GitHub
    macOS runner that is how it landed. The runner's interpreter is a framework
    build (`/Library/Frameworks/Python.framework/Versions/3.x/bin/python3.x`, per
    the CI log), and a framework build starts through a launcher that execs
    `Python.app/Contents/MacOS/Python` (seen with Homebrew's framework build:
    `ps -o comm=` of the running interpreter names `Python.app`). Measured on
    macOS 26, a SIGSTOP sent a few milliseconds after `Popen` returned, around
    that exec, never took effect in 21 of 56 tries with `sh -c 'exec sleep 300'`
    (2 to 8 ms) and 7 of 18 with Homebrew's framework Python (4 to 6 ms): `ps`
    read `R`, then `S`, never `T`. The line is written by the interpreter the
    launcher execs, so a child that has written it is past the exec, and 150
    stops sent to children that had written it all took effect. Run against a
    venv on Homebrew's framework Python, this file failed in 3 of 6 runs when
    the fixture stopped the child right after one `ps` and the lock write, the
    way the runner's failures read, and in none of 30 runs since.
    """
    child = subprocess.Popen([sys.executable, "-c", HOLDER, str(root / "daemon.lock")], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        readable, _, _ = select.select([child.stdout], [], [], HOLDER_READY_S)
        line = child.stdout.readline() if readable else b""
        assert line == b"ready\n", f"the holder did not start within {HOLDER_READY_S:g}s ({line!r})"
        started = proc_start(child.pid)
        if started is None:
            # Restricted runners cannot exec ps. Observe this child's real
            # stop with waitpid below, then supply that observation at the ps
            # boundary. No state of an unrelated process is ever inspected.
            started = "Sun Sep 20 23:26:42 2026"
            state = CHILD_STATES[child.pid] = ["S"]
            original = client_module._run

            def child_ps(argv, *args, **kwargs):
                if argv[:3] == ["/bin/ps", "-p", str(child.pid)]:
                    return 0, state[0] + (" " + started if "lstart" in argv[-1] else "")
                return original(argv, *args, **kwargs)

            monkeypatch.setattr(client_module, "_run", child_ps)
        assert started, "ps could not read the holder's start time"
        (root / "daemon.lock").write_text(json.dumps(
            {"pid": child.pid, "boot_id": boot_id(), "proc_start": started,
             "version": "stub"}))
        yield child
    finally:
        # SIGKILL lands on a stopped process too, so the teardown holds even if
        # the SIGCONT is missed; the wait keeps it from lingering as a zombie,
        # which `proc_start` would then read as a dead holder (C-5.5).
        with contextlib.suppress(OSError):
            child.send_signal(signal.SIGCONT)
        with contextlib.suppress(OSError):
            child.kill()
        child.wait(timeout=5)
        child.stdout.close()
        CHILD_STATES.pop(child.pid, None)


def stop(child: subprocess.Popen) -> None:
    """SIGSTOP `pid` and return once `ps` reads it as stopped.

    `kill` only posts the signal, and the process stops when the kernel next
    acts on it. Every test here is about what the CLI makes of a holder that
    `ps` reads as stopped, so none starts before that is true, and if it never
    becomes true the fixture fails with the state `ps` did read, instead of the
    test failing on a diagnosis it was never given a stopped process to make.
    The state is matched by its letter here, independently of
    `client.is_stopped`, which is under test.
    """
    pid = child.pid
    child.send_signal(signal.SIGSTOP)
    deadline = time.monotonic() + STOP_SETTLE_S
    while True:
        if pid in CHILD_STATES:
            waited, status = os.waitpid(pid, os.WUNTRACED | os.WNOHANG)
            if waited and os.WIFSTOPPED(status):
                CHILD_STATES[pid][0] = "T"
            elif waited:
                child.returncode = os.waitstatus_to_exitcode(status)
                pytest.fail(f"holder exited before its stop was observed: {child.returncode}")
        rc, output = client_module._run(["/bin/ps", "-p", str(pid), "-o", "state="])
        state = output.strip() if rc == 0 else None
        if state and state[0] == "T":
            return
        if time.monotonic() >= deadline:
            pytest.fail(f"pid {pid} was sent SIGSTOP {STOP_SETTLE_S:g}s ago and ps "
                        f"still reads its state as {state!r}")
        time.sleep(0.01)


@pytest.fixture
def running_holder(root, monkeypatch):
    """A live process recorded in `daemon.lock` and left running (C-5.8)."""
    with holder(root, monkeypatch) as child:
        yield child


@pytest.fixture
def stopped_holder(root, monkeypatch):
    """A live process recorded in `daemon.lock` and then stopped (C-5.8)."""
    with holder(root, monkeypatch) as child:
        stop(child)
        yield child


@pytest.fixture
def marker(root, monkeypatch):
    """Factory: a known pauser whose marker file lists the pids given (C-5.13)."""
    path = root / "paused.pids"

    def write(*pids: int) -> Path:
        path.write_text("".join(f"{pid}\n" for pid in pids))
        monkeypatch.setattr(client_module, "KNOWN_PAUSERS", (
            {**getattr(client_module, "KNOWN_PAUSERS", ({"name": "clamshell-guard"},))[0], "marker": str(path)},))
        return path

    return write


# --- before the socket (C-5.13) -----------------------------------------------

def test_the_call_is_refused_before_anything_connects(stopped_holder, root):
    """C-5.13 a stopped holder is diagnosed from the lock, not from the wire."""
    assert not (root / "daemon.sock").exists(), "there is no socket to connect to"
    with pytest.raises(client_module.DaemonStopped) as caught:
        Client(root).call("daemon.status", {})
    assert isinstance(caught.value, DaemonUnavailable), "stopped is unavailable"
    assert caught.value.code == Exit.DAEMON_UNAVAILABLE
    assert str(stopped_holder.pid) in str(caught.value)
    assert "is stopped" in str(caught.value)
    assert caught.value.fix == f"kill -CONT {stopped_holder.pid}"


def test_a_live_socket_does_not_override_the_stopped_lock(stopped_holder, daemon, root):
    """C-5.13 the state of the recorded holder decides, not a socket that answers."""
    daemon({"daemon.status": lambda request: {"version": "test"}})
    with pytest.raises(client_module.DaemonStopped):
        Client(root).call("daemon.status", {})


@contextlib.contextmanager
def backlog(root):
    """A socket that accepts into its listen backlog and answers nothing.

    Yields a function that counts the connections queued so far, which is how a
    test tells whether a call wrote anything into the paused daemon's buffer.
    """
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(root / "daemon.sock"))
    listener.listen(8)

    def queued() -> int:
        listener.setblocking(False)
        count = 0
        while True:
            try:
                conn, _ = listener.accept()
            except BlockingIOError:
                return count
            conn.close()
            count += 1

    try:
        yield queued
    finally:
        listener.close()


def running_at_preflight(probe, monkeypatch):
    """Simulate a stop just after the first preflight's running observation."""
    report = probe.holder_report

    def first_report(**kwargs):
        monkeypatch.setattr(probe, "holder_report", report)
        pid, alive, reason, _ = report(**kwargs)
        return pid, alive, reason, "S"

    monkeypatch.setattr(probe, "holder_report", first_report)


# Failure-diagnosis cases budget more than one probe interval; the round-3
# slot-expiry cases below separately require short budgets to end without a probe.
def test_a_stop_during_the_call_is_named_and_its_outcome_stays_unknown(stopped_holder, root, monkeypatch):
    """C-5.13, C-16.3 a daemon stopped after the request was sent is named, not
    reported as silence, but the request is in its buffer, so the answer is lost
    rather than "not sent"; the next call asks the lock before it sends."""
    probe = Client(root, timeout=1.2)
    running_at_preflight(probe, monkeypatch)
    with backlog(root) as queued:
        with pytest.raises(ResponseLost) as caught:
            probe.call("daemon.status", {})
        assert not isinstance(caught.value, DaemonUnavailable), "the request was sent"
        assert caught.value.code == Exit.OPERATIONAL
        assert "no response from the daemon within 1.2s" in str(caught.value)
        assert "is stopped" in str(caught.value) and str(stopped_holder.pid) in str(caught.value)
        assert caught.value.fix == f"kill -CONT {stopped_holder.pid}"
        with pytest.raises(client_module.DaemonStopped):
            probe.call("daemon.status", {})
        assert queued() == 1, "the second call wrote into the paused buffer"


def test_a_submit_lost_to_a_stop_is_unknown_and_not_re_sent_into_the_buffer(stopped_holder, root, monkeypatch):
    """C-5.13, C-16.3 the re-send asks the lock first: the stopped holder refuses
    it before it is written, so the outcome is unknown at once, not after the
    re-send's own deadline."""
    probe = Client(root, timeout=1.2)
    running_at_preflight(probe, monkeypatch)
    lost: list[ResponseLost] = []
    with backlog(root) as queued:
        with pytest.raises(OutcomeUnknown) as caught:
            probe.call_settled("submit", {}, request_id="req-stopped", minted=True,
                               requery_timeout=2.0, on_lost=lost.append)
        assert queued() == 1, "the re-send was written into the paused buffer"
    assert len(lost) == 1 and "is stopped" in str(lost[0])
    assert caught.value.request_id == "req-stopped"
    assert len(caught.value.reasons) == 2
    assert all("is stopped" in reason for reason in caught.value.reasons)


def test_a_kill_lost_to_a_stop_goes_to_offline_mode(stopped_holder, root, monkeypatch):
    """C-5.13, C-16.3, C-17.5 a daemon gone before the re-send sends a kill to
    offline mode, and a stopped one is gone in exactly that sense."""
    probe = Client(root, timeout=1.2)
    running_at_preflight(probe, monkeypatch)
    with backlog(root) as queued:
        with pytest.raises(client_module.DaemonStopped) as caught:
            probe.call_settled("kill", {"job_id": "job-1"})
        assert queued() == 1
    assert caught.value.fix == f"kill -CONT {stopped_holder.pid}"


def test_a_connect_that_times_out_is_re_diagnosed_too(stopped_holder, root, monkeypatch):
    """C-5.13 a listen backlog nobody accepts from times out in `connect`, and
    that path asks the holder as well."""
    def timed_out(self, address):
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket.socket, "connect", timed_out)
    probe = Client(root, timeout=1.2)
    running_at_preflight(probe, monkeypatch)
    with pytest.raises(client_module.DaemonStopped) as caught:
        probe.call("daemon.status", {})
    assert str(stopped_holder.pid) in str(caught.value)


def silent_call(root):
    with backlog(root):
        with pytest.raises(ResponseLost) as caught:
            Client(root, timeout=1.2).call("daemon.status", {})
    return caught.value


def test_a_silent_daemon_that_is_not_stopped_keeps_its_exit_code(running_holder, root):
    """C-17.3, C-5.13 silence is still exit 1; with a verified running holder the fix is never a second daemon."""
    error = silent_call(root)
    assert error.code == Exit.OPERATIONAL
    assert "no response from the daemon within 1.2s" in str(error)
    assert "second daemon" in error.fix and str(running_holder.pid) in error.fix


def test_silence_with_no_lock_to_verify_says_start_one(root):
    """C-5.13 "do not start a second daemon" is said only when a holder was verified; without one, start it."""
    assert not (root / "daemon.lock").exists()
    error = silent_call(root)
    assert error.code == Exit.OPERATIONAL and error.fix == "subfleet daemon start"


def test_a_refused_connect_asks_the_holder_too(running_holder, root, monkeypatch):
    """C-5.13 a full backlog refuses rather than queues, so a refused connect is not "nobody is there"."""
    (root / "daemon.sock").touch()                      # a path nothing listens on: connect is refused
    with pytest.raises(DaemonUnavailable) as caught:
        Client(root, timeout=2).call("daemon.status", {})
    assert not isinstance(caught.value, client_module.DaemonStopped)
    assert str(running_holder.pid) in str(caught.value) and "second daemon" in caught.value.fix
    stop(running_holder)
    try:
        probe = Client(root, timeout=2)
        running_at_preflight(probe, monkeypatch)
        with pytest.raises(client_module.DaemonStopped):
            probe.call("daemon.status", {})
    finally:
        running_holder.send_signal(signal.SIGCONT)


def test_a_refused_connect_with_no_holder_is_the_plain_absence(root):
    """C-5.13 with no lock at all, a refused connect is the ordinary "no daemon" and its fix is to start one."""
    (root / "daemon.sock").touch()
    with pytest.raises(DaemonUnavailable) as caught:
        Client(root, timeout=2).call("daemon.status", {})
    assert "no daemon at" in str(caught.value) and caught.value.fix == "subfleet daemon start"


@pytest.mark.parametrize("state,stopped", [("T", True), ("T+", True), ("TN", True), ("S", False),
                                           ("Ss", False), ("R+", False), ("Z", False), (None, False), ("", False)])
def test_stopped_is_any_state_that_begins_with_t(state, stopped):
    """C-5.13 BSD ps prints flags after the letter; only the letter decides."""
    assert client_module.is_stopped(state) is stopped


def test_the_socket_is_named_only_when_it_exists(stopped_holder, root):
    """C-5.13 the message says what the holder holds, and a socket that is not there is not claimed."""
    message, _fix, _command = client_module.stopped_report(stopped_holder.pid, "T", root / "daemon.sock")
    assert "daemon.lock but" in message and "daemon.sock" not in message
    (root / "daemon.sock").touch()
    message, _fix, _command = client_module.stopped_report(stopped_holder.pid, "T", root / "daemon.sock")
    assert "daemon.lock and daemon.sock" in message


def test_an_undecodable_marker_is_no_evidence_not_a_traceback(stopped_holder, root, monkeypatch):
    """C-5.13 a pauser's file is another program's; bytes that are not UTF-8 attribute nothing."""
    path = root / "garbled.pids"
    path.write_bytes(b"\xff\xfe" + str(stopped_holder.pid).encode() + b"\n")
    monkeypatch.setattr(client_module, "KNOWN_PAUSERS", (
        {**getattr(client_module, "KNOWN_PAUSERS", ({"name": "clamshell-guard"},))[0], "marker": str(path)},))
    with pytest.raises(client_module.DaemonStopped) as caught:
        Client(root).call("daemon.status", {})
    assert caught.value.fix == f"kill -CONT {stopped_holder.pid}"



# --- daemon status (C-5.13) ---------------------------------------------------

def test_daemon_status_names_a_stopped_holder_and_skips_the_socket(
        stopped_holder, daemon, root, capsys, monkeypatch):
    """C-5.13 `daemon status` says stopped, exits 69, and waits on nothing."""
    daemon({"daemon.status": lambda request: {"version": "test"}})
    monkeypatch.setattr(cli, "DAEMON_STATUS_PING_TIMEOUT_S", 30.0)
    started = time.monotonic()
    assert cli.main(["daemon", "status"]) == int(Exit.DAEMON_UNAVAILABLE)
    elapsed = time.monotonic() - started
    captured = capsys.readouterr()
    assert "holder      stopped" in captured.out
    assert str(stopped_holder.pid) in captured.out
    assert f"fix: kill -CONT {stopped_holder.pid}" in captured.err
    assert elapsed < 5.0, "the 30 s ping budget was not spent"


def test_daemon_status_json_carries_the_state_the_flag_and_the_diagnosis(
        stopped_holder, root, capsys):
    """C-5.13 `--json` reports `holder_state`, `holder_stopped`, and `diagnosis`."""
    assert cli.main(["daemon", "status", "--json"]) == int(Exit.DAEMON_UNAVAILABLE)
    payload = json.loads(capsys.readouterr().out)
    assert payload["holder_stopped"] is True
    assert payload["holder_state"].startswith("T")
    assert str(stopped_holder.pid) in payload["diagnosis"]
    assert payload["ping"] is False and payload["ping_ms"] < 1000


def test_daemon_start_refuses_to_start_a_second_daemon_over_a_stopped_one(
        stopped_holder, root, capsys):
    """C-5.8, C-5.13 the stopped holder still owns the flock, so continue it."""
    assert cli.main(["daemon", "start"]) == int(Exit.DAEMON_UNAVAILABLE)
    captured = capsys.readouterr()
    assert "is stopped" in captured.err
    assert f"fix: kill -CONT {stopped_holder.pid}" in captured.err


# --- doctor (C-5.13) ----------------------------------------------------------

def test_the_doctor_lock_row_fails_on_a_stopped_holder(stopped_holder, root):
    """C-5.13 the offline table must not pass a daemon that cannot answer."""
    (root / "daemon.sock").touch()
    item = doctor.check_daemon_lock(root)
    assert item["status"] == doctor.FAIL
    assert "is stopped" in item["detail"] and str(stopped_holder.pid) in item["detail"]
    assert item["fix"] == f"`kill -CONT {stopped_holder.pid}`"


def test_the_live_ping_row_carries_the_same_diagnosis(stopped_holder, root):
    """C-5.13 `--live` fails with the diagnosis, not with a bare timeout."""
    item = doctor.check_live(root)
    assert item["status"] == doctor.FAIL
    assert "is stopped" in item["detail"]
    assert item["fix"] == f"`kill -CONT {stopped_holder.pid}`"


# --- attribution (C-5.13) -----------------------------------------------------

def test_a_pauser_is_named_only_when_its_marker_file_lists_the_pid(
        stopped_holder, root, marker):
    """C-5.13 attribution comes from the pauser's own record and nowhere else."""
    marker(stopped_holder.pid)
    stopped = Client(root).stopped_holder()
    assert "clamshell-guard paused it" in str(stopped)
    assert "clamshell-guard-resume" in stopped.fix
    assert "kill -CONT" not in stopped.fix, "the guard would stop it again"


def test_a_marker_file_that_lists_other_pids_attributes_nothing(
        stopped_holder, root, marker):
    """C-5.13 a marker file is evidence about the pids it names, and no others."""
    marker(stopped_holder.pid + 1, stopped_holder.pid + 2)
    stopped = Client(root).stopped_holder()
    assert "clamshell-guard" not in str(stopped)
    assert stopped.fix == f"kill -CONT {stopped_holder.pid}"


def test_no_marker_file_at_all_attributes_nothing(stopped_holder, root, monkeypatch):
    """C-5.13 an absent marker file is not an attribution either."""
    monkeypatch.setattr(client_module, "KNOWN_PAUSERS", (
        {**getattr(client_module, "KNOWN_PAUSERS", ({"name": "clamshell-guard"},))[0], "marker": str(root / "absent.pids")},))
    stopped = Client(root).stopped_holder()
    assert "clamshell-guard" not in str(stopped)
    assert stopped.fix == f"kill -CONT {stopped_holder.pid}"


# --- the verbs (C-17.5) -------------------------------------------------------

def test_runs_falls_back_offline_and_says_why(stopped_holder, root, capsys):
    """C-17.5, C-5.13 a stopped daemon is unavailable, and the reader says so."""
    from test_offline import JOB, build_store
    build_store(root)
    assert cli.main(["runs"]) == int(Exit.OK)
    captured = capsys.readouterr()
    assert JOB in captured.out, "the store was read"
    assert "offline" in captured.err
    assert "is stopped" in captured.err
    assert f"fix: kill -CONT {stopped_holder.pid}" in captured.err


def test_status_falls_back_offline_and_says_why(stopped_holder, root, capsys):
    """C-17.5, C-5.13 `status` reads the store and names the stopped holder."""
    from test_offline import build_store
    build_store(root)
    assert cli.main(["status"]) == int(Exit.OK)
    captured = capsys.readouterr()
    assert "offline" in captured.err and "is stopped" in captured.err


def test_no_daemon_and_no_store_still_carries_the_daemon_s_own_fix(
        stopped_holder, root, capsys):
    """C-17.5, C-5.13 the store is what is missing, but the daemon is what to fix."""
    assert cli.main(["runs"]) == int(Exit.DAEMON_UNAVAILABLE)
    captured = capsys.readouterr()
    assert "no store" in captured.err, "the message names what could not be read"
    assert f"fix: kill -CONT {stopped_holder.pid}" in captured.err
    assert "fix: subfleet daemon start" not in captured.err


def test_a_verb_with_no_offline_mode_prints_the_diagnosis_not_daemon_start(
        stopped_holder, root, capsys):
    """C-17.5, C-5.13 `subfleet daemon start` is the wrong fix for this one."""
    assert cli.main(["lanes"]) == int(Exit.DAEMON_UNAVAILABLE)
    captured = capsys.readouterr()
    assert "is stopped" in captured.err
    assert f"fix: kill -CONT {stopped_holder.pid}" in captured.err
    assert "subfleet daemon start" not in captured.err


# Release regressions use only seams available before the port, so their base
# failures demonstrate behavior rather than missing imports (C-5.13).
START = "Sun Sep 20 23:26:42 2026"


def test_fake_stopped_holder_returns_69_before_timeout(root, monkeypatch, capsys, holder_clock):
    """C-5.13 fake ps T must refuse the CLI call before touching its socket."""
    (root / "daemon.lock").write_text(json.dumps({"pid": 12345, "proc_start": START}))
    monkeypatch.setattr(client_module, "_run", lambda *a, **k: (0, "T+ " + START))
    monkeypatch.setattr(client_module, "DEFAULT_TIMEOUT_S", 0.2)
    with backlog(root) as queued:
        assert cli.main(["lanes"]) == int(Exit.DAEMON_UNAVAILABLE)
        assert queued() == 0
        assert holder_clock.now == 0.0
    error = capsys.readouterr().err
    assert "is stopped" in error and "kill -CONT 12345" in error


def test_real_stopped_holder_never_connects(stopped_holder, root):
    """C-5.13 refusal needs one process probe, without a socket timeout budget."""
    with backlog(root) as queued:
        with pytest.raises(DaemonUnavailable) as caught:
            Client(root, timeout=2).call("daemon.status", {})
        assert queued() == 0
    assert caught.value.code == Exit.DAEMON_UNAVAILABLE
    assert "is stopped" in str(caught.value)
    assert caught.value.fix == f"kill -CONT {stopped_holder.pid}"


def test_fake_stopped_doctor_row_fails(root, monkeypatch):
    """C-5.13 doctor must not pass a holder in T state."""
    (root / "daemon.lock").write_text(json.dumps({"pid": 12345, "proc_start": START}))
    monkeypatch.setattr(client_module, "_run", lambda *a, **k: (0, "T " + START))
    item = doctor.check_daemon_lock(root)
    assert item["status"] == doctor.FAIL
    assert "is stopped" in item["detail"] and "kill -CONT 12345" in item["fix"]


@pytest.mark.parametrize("method", ["call", "call_settled"])
@pytest.mark.parametrize("op", ["daemon.status", "ping", "submit", "kill", "wait", "list"])
def test_every_client_call_refuses_a_stopped_holder(root, monkeypatch, holder_clock, method, op):
    """C-5.13 every client entry point refuses before connecting, repeatedly."""
    (root / "daemon.lock").write_text(json.dumps({"pid": 12345, "proc_start": START}))
    monkeypatch.setattr(client_module, "_run", lambda *a, **k: (0, "Ts+< " + START))
    probe = Client(root, timeout=30)
    with backlog(root) as queued:
        for _ in range(3):
            with pytest.raises(DaemonUnavailable) as caught:
                getattr(probe, method)(op, {}, request_id="req-stopped")
            assert "is stopped" in str(caught.value)
        assert queued() == 0


@pytest.fixture
def holder_clock(monkeypatch):
    """Advance only the client's probe/retry clock, leaving real socket I/O alone."""
    from types import SimpleNamespace
    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(client_module, "_clock", lambda: clock.now)
    monkeypatch.setattr(client_module, "_sleep", sleep)
    return clock


def test_cached_client_rechecks_before_wait(root, daemon, monkeypatch, holder_clock):
    """C-5.13 a successful S ping cannot authorize a later send to a T+ holder."""
    (root / "daemon.lock").write_text(json.dumps({"pid": 12345, "proc_start": START}))
    state = ["S"]
    monkeypatch.setattr(client_module, "_run", lambda *a, **k: (0, state[0] + " " + START))
    server = daemon({"ping": lambda request: {"pong": True}})
    probe = Client(root, timeout=2)
    assert probe.call("ping") == {"pong": True}
    server.close()
    state[0] = "T+"
    with backlog(root) as queued:
        with pytest.raises(client_module.DaemonStopped):
            probe.call("wait", {"job_ids": ["job-1"]})
        assert queued() == 0, "the cached client sent to the stopped holder"


@pytest.mark.parametrize("rc,output", [(0, "S " + START), (2, "T+ " + START)])
def test_reused_client_probes_are_paced(root, daemon, monkeypatch, holder_clock, rc, output):
    """C-5.13 four fresh probes are free to burst, then refill one per second."""
    (root / "daemon.lock").write_text(json.dumps({"pid": 12345, "proc_start": START}))
    probes = []

    def observe(*args, **kwargs):
        probes.append(holder_clock.now)
        return rc, output

    monkeypatch.setattr(client_module, "_run", observe)
    server = daemon({op: lambda request: {} for op in ("ping", "wait", "conversation.watch", "kill")})
    probe = Client(root)
    for op in ("ping", "wait", "conversation.watch"):
        probe.call(op)
    probe.call_settled("kill")
    assert holder_clock.sleeps == []
    probe.call('ping')
    assert server.ops() == ["ping", "wait", "conversation.watch", "kill", "ping"]
    assert probes == [0.0, 0.0, 0.0, 0.0, 1.0]
    assert holder_clock.sleeps == [1.0]


def test_stopped_detection_uses_one_probe_budget(root, monkeypatch, holder_clock):
    """C-5.13 a slow probe sets the detection cost; there is no 1.5 s promise."""
    (root / "daemon.lock").write_text(json.dumps({"pid": 12345, "proc_start": START}))
    probes = []

    def observe(*args, **kwargs):
        probes.append(holder_clock.now)
        holder_clock.now += 1.6
        return 0, "T+ " + START

    monkeypatch.setattr(client_module, "_run", observe)
    with backlog(root) as queued:
        with pytest.raises(client_module.DaemonStopped):
            Client(root, timeout=0.1).call("wait")
        assert queued() == 0
    assert probes == [0.0] and holder_clock.now == 1.6


from hypothesis import example, given, settings, strategies as st

settings.register_profile("ci", max_examples=40, derandomize=True, database=None, deadline=None)


@settings(settings.get_profile("ci"))
@given(letter=st.sampled_from(["R", "S", "T", "Z", "U", "I"]),
       flags=st.lists(st.sampled_from(["s", "+", "<"]), unique=True, max_size=3).map("".join),
       rc=st.sampled_from([-1, 0, 1, 2]),
       shape=st.sampled_from(["row", "empty", "blank", "multi", "no-start", "bad-start", "bad-state", "extra"]))
@example(letter="T", flags="+", rc=1, shape="row")
@example(letter="T", flags="+", rc=2, shape="row")
@example(letter="T", flags="+", rc=0, shape="multi")
@example(letter="T", flags="+", rc=0, shape="row")
@example(letter="Z", flags="s<", rc=0, shape="row")
@example(letter="S", flags="+", rc=0, shape="row")
@example(letter="R", flags="", rc=1, shape="empty")
def test_ps_state_classification_is_exact(letter, flags, rc, shape):
    """C-5.3, C-5.13 only successful, single, well-formed rows verify identity."""
    from unittest.mock import patch
    state = letter + flags
    row = state + " " + START
    output = {"row": "  " + row + "\n", "empty": "", "blank": " \n\t",
              "multi": row + "\n" + row, "no-start": state,
              "bad-start": state + " not-a-start", "bad-state": "? " + START,
              "extra": row + " extra"}[shape]
    valid = rc == 0 and shape == "row"
    gone = rc == 1 and shape in ("empty", "blank")
    expected_state = state if valid else "" if gone else None
    expected_start = START if valid else "" if gone else None
    with patch.object(client_module, "_run", return_value=(rc, output)):
        assert client_module.proc_status(12345) == (expected_state, expected_start)
        assert client_module.proc_start(12345) == ("" if valid and letter == "Z" else expected_start)
        assert client_module.is_stopped(expected_state) == (valid and letter == "T")
        alive, _, reported = client_module.identity_status(12345, None, START)
        expected_alive = letter != "Z" if valid else False if gone else None
        assert alive is expected_alive and reported == expected_state


# Round 3: deadlines cover pacing, and pacing covers the actual ps invocations.
class PacedWire:
    """A deterministic socket: one poll per send, bounded by settimeout."""

    def __init__(self, clock, *, completion=0.0, polling=False, late_reply=False):
        self.clock, self.completion = clock, completion
        self.polling, self.late_reply = polling, late_reply
        self.sent = []

    def socket(self, *args):
        wire = self

        class Connection:
            def settimeout(self, seconds):
                self.timeout = seconds

            def connect(self, path):
                pass

            def sendall(self, data):
                from subfleet import protocol
                self.request = protocol.decode_request(data)
                wire.sent.append((wire.clock.now, self.request.op))

            def recv(self, size):
                from subfleet import protocol
                if not wire.polling and wire.completion > wire.clock.now:
                    delay = wire.completion - wire.clock.now
                    if delay > self.timeout and not wire.late_reply:
                        wire.clock.now += self.timeout
                        raise TimeoutError('controlled response deadline')
                    wire.clock.now += delay
                done = wire.clock.now >= wire.completion
                result = ({'jobs': [{'job_id': 'job-1', 'state': 'succeeded' if done else 'running'}]}
                          if wire.polling else {'pong': True})
                return protocol.encode(protocol.ok(self.request.id, result))

            def close(self):
                pass

        return Connection()


def paced_observations(root, monkeypatch, clock, *, boots=(0.0,), probes=(0.0,)):
    """Model real subprocess deadlines, including stages longer than their budget."""
    from types import SimpleNamespace
    boot_uuid = '66355737-51db-46d4-8f31-c928bc955e16'
    (root / 'daemon.lock').write_text(json.dumps(
        {'pid': 12345, 'boot_id': boot_uuid, 'proc_start': START}))
    starts = []
    observations = [-1]

    def run(argv, *, timeout, **kwargs):
        if argv[0] == '/usr/sbin/sysctl':
            observations[0] += 1
        index = observations[0]
        if argv[0] == '/bin/ps':
            starts.append(clock.now)
            cost, output = probes[index % len(probes)], 'S ' + START
        else:
            cost, output = boots[index % len(boots)], boot_uuid
        clock.now += min(cost, timeout)
        if cost > timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        return SimpleNamespace(returncode=0, stdout=output)

    # Exercise every boot read, including when an earlier observation succeeded.
    monkeypatch.setattr(client_module, 'boot_id', client_module._read_boot_id)
    monkeypatch.setattr(client_module.subprocess, 'run', run)
    return starts


def test_wait_probe_slot_respects_deadline(root, monkeypatch, holder_clock, capsys):
    """C-15.4 completion at .75 cannot satisfy wait's .6-second deadline."""
    from types import SimpleNamespace
    starts = paced_observations(root, monkeypatch, holder_clock)
    wire = PacedWire(holder_clock, completion=.75, polling=True)
    monkeypatch.setattr(client_module.socket, 'socket', wire.socket)
    monkeypatch.setattr(cli, 'time', SimpleNamespace(
        monotonic=lambda: holder_clock.now, sleep=client_module._sleep))
    assert cli.main(['wait', 'job-1', '--timeout', '0.6']) == int(Exit.WAIT_TIMEOUT)
    assert holder_clock.now == pytest.approx(.6)
    assert starts == [0.0, .4] and wire.sent == [(0.0, 'wait'), (.4, 'wait')]
    assert 'SUCCEEDED' not in capsys.readouterr().err


@pytest.mark.parametrize('as_json', [False, True])
def test_status_uses_later_stopped_observation(root, monkeypatch, holder_clock, capsys, as_json):
    """C-5.13 S then T+ must render exactly the same diagnosis as initial T+."""
    (root / 'daemon.lock').write_text(json.dumps({'pid': 12345, 'proc_start': START}))
    states = iter(['S', 'T+'])
    monkeypatch.setattr(client_module, '_run', lambda *a, **k: (0, next(states) + ' ' + START))
    args = ['daemon', 'status'] + (['--json'] if as_json else [])
    assert cli.main(args) == int(Exit.DAEMON_UNAVAILABLE)
    captured = capsys.readouterr()
    if as_json:
        payload = json.loads(captured.out)
        assert payload['lock_holder_alive'] is True
        assert payload['holder_state'] == 'T+' and payload['holder_stopped'] is True
        assert 'is stopped' in payload['diagnosis']
        assert payload['fix'] == 'kill -CONT 12345'
        assert payload['ping'] is False
    else:
        assert 'holder      stopped' in captured.out
        assert 'is stopped' in captured.err and 'fix: kill -CONT 12345' in captured.err


@pytest.mark.parametrize('check', [doctor.check_live, doctor.check_descriptors_live])
def test_doctor_live_uses_later_stopped_observation(root, monkeypatch, holder_clock, check):
    """C-5.13 doctor's offline row may pass, but its later live row must fail."""
    (root / 'daemon.lock').write_text(json.dumps({'pid': 12345, 'proc_start': START}))
    states = iter(['S', 'T+'])
    monkeypatch.setattr(client_module, '_run', lambda *a, **k: (0, next(states) + ' ' + START))
    assert doctor.check_daemon_lock(root)['status'] == doctor.PASS
    item = check(root)
    assert item['status'] == doctor.FAIL
    assert 'T+' in item['detail'] and 'is stopped' in item['detail']
    assert item['fix'] == '`kill -CONT 12345`'


def test_slow_boot_lookup_paces_actual_ps_starts(root, monkeypatch, holder_clock):
    """C-5.13 a slow boot read cannot refill a token before ps spends it."""
    starts = paced_observations(root, monkeypatch, holder_clock, boots=(1.6, 0.0, 0.0, 0.0, 0.0))
    wire = PacedWire(holder_clock)
    monkeypatch.setattr(client_module.socket, 'socket', wire.socket)
    probe = Client(root)
    for _ in range(5):
        assert probe.call('ping')['pong']
    assert starts == pytest.approx([1.6] * 4 + [2.6])
    assert holder_clock.sleeps == pytest.approx([1.0])


def test_call_rejects_response_observed_after_deadline(root, monkeypatch, holder_clock):
    """C-5.13 an overdue socket observation cannot turn a timeout into success."""
    paced_observations(root, monkeypatch, holder_clock)
    wire = PacedWire(holder_clock, completion=.75, late_reply=True)
    monkeypatch.setattr(client_module.socket, 'socket', wire.socket)
    with pytest.raises(ResponseLost):
        Client(root).call('ping', timeout=.6)


@settings(settings.get_profile('ci'))
@given(deadlines=st.lists(st.integers(1, 7000), min_size=4, max_size=4),
       boots=st.lists(st.integers(0, 10000), min_size=4, max_size=4),
       probes=st.lists(st.integers(0, 10000), min_size=4, max_size=4),
       completions=st.lists(st.integers(0, 20000), min_size=4, max_size=4),
       verify=st.booleans())
@example(deadlines=[600]*4, boots=[1600]*4, probes=[5000]*4, completions=[10000]*4, verify=True)
@example(deadlines=[600]*4, boots=[1600]*4, probes=[5000]*4, completions=[10000]*4, verify=False)
@example(deadlines=[600]*4, boots=[10000]*4, probes=[10000]*4, completions=[0]*4, verify=True)
@example(deadlines=[600]*4, boots=[10000]*4, probes=[10000]*4, completions=[10000]*4, verify=False)
def test_probe_pacing_deadline_property(deadlines, boots, probes, completions, verify):
    """C-5.13 deadlines, slow observations and completion times compose safely."""
    import tempfile
    from types import SimpleNamespace
    with tempfile.TemporaryDirectory(prefix='sf-pacing-', dir='/tmp') as directory, pytest.MonkeyPatch.context() as patch:
        clock = SimpleNamespace(now=0.0)
        patch.setattr(client_module, '_clock', lambda: clock.now)
        patch.setattr(client_module, '_sleep', lambda seconds: setattr(clock, 'now', clock.now + seconds))
        starts = paced_observations(Path(directory), patch, clock,
                                    boots=tuple(v / 1000 for v in boots),
                                    probes=tuple(v / 1000 for v in probes))
        wire = PacedWire(clock)
        patch.setattr(client_module.socket, 'socket', wire.socket)
        probe = Client(directory, verify_lock=verify)
        for duration, completion in zip(deadlines, completions):
            began = clock.now
            deadline = began + duration / 1000
            wire.completion = began + completion / 1000
            try:
                result = probe.call('ping', timeout=duration / 1000)
            except (DaemonUnavailable, client_module.ProtocolError):
                pass
            else:
                assert result['pong']
                assert wire.completion <= deadline and clock.now <= deadline
            assert clock.now <= deadline + 5.0 + 1e-9


@pytest.mark.parametrize('verify', [True, False], ids=['preflight', 'post-send'])
def test_ping_observation_shares_boot_and_ps_budget(root, monkeypatch, holder_clock, verify):
    """C-5.13 the reviewer's 1.6 s boot + 5 s ps ends within .6 + 5 seconds."""
    paced_observations(root, monkeypatch, holder_clock, boots=(1.6,), probes=(5.0,))
    wire = PacedWire(holder_clock, completion=20)
    monkeypatch.setattr(client_module.socket, 'socket', wire.socket)
    with pytest.raises(client_module.ProtocolError):
        Client(root, verify_lock=verify).call('ping', timeout=.6)
    assert holder_clock.now <= 5.6 + 1e-9
    assert bool(wire.sent) is (not verify)


def test_preflight_and_silence_diagnosis_share_call_ceiling(root, monkeypatch, holder_clock):
    """C-5.13 a sent call gets no new allowance beyond its deadline plus 5 s."""
    paced_observations(root, monkeypatch, holder_clock, boots=(.2, 1.6), probes=(.2, 5.0))
    wire = PacedWire(holder_clock, completion=20)
    monkeypatch.setattr(client_module.socket, 'socket', wire.socket)
    with pytest.raises(ResponseLost):
        Client(root).call('ping', timeout=.6)
    assert holder_clock.now <= 5.6 + 1e-9
    assert wire.sent == [(.4, 'ping')]


@settings(settings.get_profile('ci'))
@given(gaps=st.lists(st.integers(0, 6000), min_size=1, max_size=30),
       boots=st.lists(st.integers(0, 4000), min_size=1, max_size=30))
@example(gaps=[0] * 15, boots=[0])
@example(gaps=[0] * 5, boots=[1600, 0, 0, 0, 0])
@example(gaps=[0, 0, 0, 0, 500, 500, 10000, 0, 0, 0, 0, 0], boots=[0])
def test_probe_bucket_rate_property(gaps, boots):
    """C-5.13 every time window allows at most four plus its refill tokens."""
    import tempfile
    from types import SimpleNamespace
    with tempfile.TemporaryDirectory(prefix='sf-bucket-', dir='/tmp') as directory, pytest.MonkeyPatch.context() as patch:
        clock = SimpleNamespace(now=0.0)
        patch.setattr(client_module, '_clock', lambda: clock.now)
        patch.setattr(client_module, '_sleep', lambda seconds: setattr(clock, 'now', clock.now + seconds))
        starts = paced_observations(Path(directory), patch, clock,
                                    boots=tuple(value / 1000 for value in boots))
        wire = PacedWire(clock)
        patch.setattr(client_module.socket, 'socket', wire.socket)
        probe = Client(directory)
        for gap in gaps:
            clock.now += gap / 1000
            assert probe.call('ping')['pong']
        for i, start in enumerate(starts):
            for j in range(i, len(starts)):
                assert j - i + 1 <= 4 + (starts[j] - start) / client_module.HOLDER_PROBE_INTERVAL_S + 1e-9


def test_start_preserves_later_stopped_observation(root, monkeypatch, holder_clock, capsys):
    """C-5.13 S then T+ refuses start before it launches another daemon."""
    (root / 'daemon.lock').write_text(json.dumps({'pid': 12345, 'proc_start': START}))
    states = iter(['S', 'T+'])
    monkeypatch.setattr(client_module, '_run', lambda *a, **k: (0, next(states) + ' ' + START))
    monkeypatch.setattr(cli, '_daemond_argv', lambda root: pytest.fail('attempted a second launch'))
    assert cli.main(['daemon', 'start']) == int(Exit.DAEMON_UNAVAILABLE)
    assert 'fix: kill -CONT 12345' in capsys.readouterr().err


def test_settlement_probe_wait_keeps_unknown_outcome(root, monkeypatch, holder_clock):
    """C-16.3 an expired re-send slot cannot erase the first sent outcome."""
    paced_observations(root, monkeypatch, holder_clock)
    wire = PacedWire(holder_clock, completion=10)
    monkeypatch.setattr(client_module.socket, 'socket', wire.socket)
    probe = Client(root, timeout=.1)
    # First send and its silence diagnosis spend the two remaining tokens.
    for _ in range(2):
        probe.holder_report()
    with pytest.raises(OutcomeUnknown) as caught:
        probe.call_settled('kill', requery_timeout=.2)
    assert holder_clock.now == pytest.approx(.3)
    assert wire.sent == [(0.0, 'kill')]
    assert 'no response' in str(caught.value)
    assert 'before sending' in str(caught.value)


def test_hook_wait_forwards_remaining_deadline(holder_clock):
    """C-15.2 a hook's final poll gives preflight only its remaining budget."""
    import io
    from types import SimpleNamespace
    from subfleet import hooks

    def call(op, args, **kwargs):
        assert kwargs['timeout'] <= .6 - holder_clock.now
        raise client_module.ProtocolError('controlled timeout', Exit.OPERATIONAL)

    assert hooks._wait_and_deliver(SimpleNamespace(call=call), 'session', 'job-1', .6,
                                   stderr=io.StringIO(), now=lambda: holder_clock.now,
                                   sleep=client_module._sleep) == int(Exit.OK)


def test_wait_refused_connect_probe_slot_respects_deadline(root, monkeypatch, holder_clock, capsys):
    """C-15.4 a refusal's diagnostic slot also ends wait at its deadline, exit 124."""
    from types import SimpleNamespace
    starts = paced_observations(root, monkeypatch, holder_clock)
    wire = PacedWire(holder_clock)
    probe = Client(root)
    # Leave one token for preflight, so the refused-connect diagnosis must
    # wait for a refill. The wait's original .6-second deadline still wins.
    for _ in range(3):
        probe.holder_report()
    starts.clear()
    monkeypatch.setattr(cli, '_client', lambda args, **kwargs: probe)

    def refuse(path):
        raise ConnectionRefusedError('controlled refusal')

    def refused_socket(*args):
        conn = wire.socket(*args)
        conn.connect = refuse
        return conn

    monkeypatch.setattr(client_module.socket, 'socket', refused_socket)
    monkeypatch.setattr(cli, 'time', SimpleNamespace(
        monotonic=lambda: holder_clock.now, sleep=client_module._sleep))
    assert cli.main(['wait', 'job-1', '--timeout', '0.6']) == int(Exit.WAIT_TIMEOUT)
    assert holder_clock.now == pytest.approx(.6)
    assert starts == [0.0] and wire.sent == []
    assert 'timeout' in capsys.readouterr().err


def test_busy_then_refused_diagnoses_share_original_call_ceiling(root, daemon, monkeypatch, holder_clock):
    """C-5.13 a busy/refused call's two diagnoses share deadline + 5 seconds."""
    from subfleet import protocol
    starts = paced_observations(root, monkeypatch, holder_clock,
                                boots=(0.0, 0.0, 1.6, 1.6), probes=(0.0, 0.0, 5.0, 5.0))
    server = daemon({'ping': lambda request: protocol.fail(request.id, Exit.DAEMON_UNAVAILABLE, 'busy')})

    def close_listener(seconds):
        holder_clock.now += seconds
        server._stop.set()
        server._thread.join(timeout=2)
        assert not server._thread.is_alive()
        server._server.close()       # preserve the path: connect must be refused
        assert server.path.exists()

    monkeypatch.setattr(client_module, '_sleep', close_listener)
    monkeypatch.setattr(client_module, 'busy_pause', lambda streak: .006)
    with pytest.raises(client_module.DaemonError, match='busy'):
        Client(root).call('ping', timeout=2)
    assert server.ops() == ['ping']
    assert len(starts) == 4           # two preflights, unreachable(), refused_while_busy()
    assert holder_clock.now <= 7.006
    assert holder_clock.now == pytest.approx(7.0)


def test_late_ps_observation_cannot_verify_stopped_holder(root, monkeypatch, holder_clock):
    """C-5.13 a successful ps returned after its observation deadline proves nothing."""
    from types import SimpleNamespace
    (root / 'daemon.lock').write_text(json.dumps({'pid': 12345, 'proc_start': START}))

    def late(argv, *, timeout, **kwargs):
        holder_clock.now += timeout + .01   # scheduler delay after subprocess completed
        return SimpleNamespace(returncode=0, stdout='T+ ' + START)

    monkeypatch.setattr(client_module.subprocess, 'run', late)
    pid, alive, reason, state = Client(root).holder_report(deadline_at=.1)
    assert pid == 12345 and alive is None and state is None
    assert 'could not report' in reason


def test_expired_probe_slot_starts_no_boot_read(root, monkeypatch, holder_clock):
    """C-5.13 wait for an available slot before boot reads, but spend it only at ps."""
    paced_observations(root, monkeypatch, holder_clock)
    probe = Client(root)
    for _ in range(4):
        probe.holder_report()
    monkeypatch.setattr(client_module, 'boot_id', lambda: pytest.fail('boot read before available slot'))
    with pytest.raises(client_module.RequestTimeout, match='before sending'):
        probe.call('ping', timeout=.2)
    assert holder_clock.now == pytest.approx(.2)
