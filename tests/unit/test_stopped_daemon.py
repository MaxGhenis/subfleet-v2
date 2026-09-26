"""A SIGSTOPped lock holder: the diagnosis, where it appears, and its fix.

Every test names the clause it proves (C-20.5). The holder is a real process
that is really stopped, because the whole question is what `ps` says about it;
it is continued, killed, and reaped however the test ends.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from subfleet import cli, client as client_module, doctor
from subfleet.client import (Client, DaemonStopped, DaemonUnavailable, OutcomeUnknown,
                             ResponseLost, boot_id, proc_start)
from subfleet.contracts import Exit


@pytest.fixture(autouse=True)
def no_real_marker(root, monkeypatch):
    """C-5.11 attribution reads a file on this machine; tests read their own or none."""
    monkeypatch.setattr(client_module, "KNOWN_PAUSERS", (
        {**client_module.KNOWN_PAUSERS[0], "marker": str(root / "no-such-marker")},))


@pytest.fixture
def running_holder(root):
    """A live process recorded in `daemon.lock` and left running (C-5.8)."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    try:
        started = client_module.proc_start(child.pid)
        for _ in range(20):
            if started:
                break
            time.sleep(.05)
            started = client_module.proc_start(child.pid)
        assert started, "ps could not read the child's start time"
        (root / "daemon.lock").write_text(json.dumps(
            {"pid": child.pid, "boot_id": boot_id(), "proc_start": started, "version": "stub"}))
        yield child
    finally:
        with contextlib.suppress(OSError):
            child.kill()
        child.wait(timeout=5)


@pytest.fixture
def stopped_holder(root):
    """A live process recorded in `daemon.lock` and then stopped (C-5.8)."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    try:
        started = proc_start(child.pid)
        assert started, "ps could not read the child's start time"
        (root / "daemon.lock").write_text(json.dumps(
            {"pid": child.pid, "boot_id": boot_id(), "proc_start": started,
             "version": "stub"}))
        os.kill(child.pid, signal.SIGSTOP)
        yield child
    finally:
        # SIGKILL lands on a stopped process too, so the teardown holds even if
        # the SIGCONT is missed; the wait keeps it from lingering as a zombie,
        # which `proc_start` would then read as a dead holder (C-5.5).
        with contextlib.suppress(OSError):
            os.kill(child.pid, signal.SIGCONT)
        with contextlib.suppress(OSError):
            child.kill()
        child.wait(timeout=5)


@pytest.fixture
def marker(root, monkeypatch):
    """Factory: a known pauser whose marker file lists the pids given (C-5.11)."""
    path = root / "paused.pids"

    def write(*pids: int) -> Path:
        path.write_text("".join(f"{pid}\n" for pid in pids))
        monkeypatch.setattr(client_module, "KNOWN_PAUSERS", (
            {**client_module.KNOWN_PAUSERS[0], "marker": str(path)},))
        return path

    return write


# --- before the socket (C-5.11) -----------------------------------------------

def test_the_call_is_refused_before_anything_connects(stopped_holder, root):
    """C-5.11 a stopped holder is diagnosed from the lock, not from the wire."""
    assert not (root / "daemon.sock").exists(), "there is no socket to connect to"
    with pytest.raises(DaemonStopped) as caught:
        Client(root).call("daemon.status", {})
    assert isinstance(caught.value, DaemonUnavailable), "stopped is unavailable"
    assert caught.value.code == Exit.DAEMON_UNAVAILABLE
    assert str(stopped_holder.pid) in str(caught.value)
    assert "is stopped" in str(caught.value)
    assert caught.value.fix == f"kill -CONT {stopped_holder.pid}"


def test_a_live_socket_does_not_override_the_stopped_lock(stopped_holder, daemon, root):
    """C-5.11 the state of the recorded holder decides, not a socket that answers."""
    daemon({"daemon.status": lambda request: {"version": "test"}})
    with pytest.raises(DaemonStopped):
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


def test_a_stop_during_the_call_is_named_and_its_outcome_stays_unknown(stopped_holder, root):
    """C-5.11, C-16.3 a daemon stopped after the request was sent is named, not
    reported as silence, but the request is in its buffer, so the answer is lost
    rather than "not sent"; the next call asks the lock before it sends."""
    probe = Client(root, timeout=0.2)
    probe._checked = True                # the holder was running when it was checked
    with backlog(root) as queued:
        with pytest.raises(ResponseLost) as caught:
            probe.call("daemon.status", {})
        assert not isinstance(caught.value, DaemonUnavailable), "the request was sent"
        assert caught.value.code == Exit.OPERATIONAL
        assert "no response from the daemon within 0.2s" in str(caught.value)
        assert "is stopped" in str(caught.value) and str(stopped_holder.pid) in str(caught.value)
        assert caught.value.fix == f"kill -CONT {stopped_holder.pid}"
        with pytest.raises(DaemonStopped):
            probe.call("daemon.status", {})
        assert queued() == 1, "the second call wrote into the paused buffer"


def test_a_submit_lost_to_a_stop_is_unknown_and_not_re_sent_into_the_buffer(stopped_holder, root):
    """C-5.11, C-16.3 the re-send asks the lock first: the stopped holder refuses
    it before it is written, so the outcome is unknown at once, not after the
    re-send's own deadline."""
    probe = Client(root, timeout=0.2)
    probe._checked = True
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


def test_a_kill_lost_to_a_stop_goes_to_offline_mode(stopped_holder, root):
    """C-5.11, C-16.3, C-17.5 a daemon gone before the re-send sends a kill to
    offline mode, and a stopped one is gone in exactly that sense."""
    probe = Client(root, timeout=0.2)
    probe._checked = True
    with backlog(root) as queued:
        with pytest.raises(DaemonStopped) as caught:
            probe.call_settled("kill", {"job_id": "job-1"})
        assert queued() == 1
    assert caught.value.fix == f"kill -CONT {stopped_holder.pid}"


def test_a_connect_that_times_out_is_re_diagnosed_too(stopped_holder, root, monkeypatch):
    """C-5.11 a listen backlog nobody accepts from times out in `connect`, and
    that path asks the holder as well."""
    def timed_out(self, address):
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket.socket, "connect", timed_out)
    probe = Client(root, timeout=0.2)
    probe._checked = True                # the holder was running when it was checked
    with pytest.raises(DaemonStopped) as caught:
        probe.call("daemon.status", {})
    assert str(stopped_holder.pid) in str(caught.value)


def silent_call(root):
    with backlog(root):
        with pytest.raises(ResponseLost) as caught:
            Client(root, timeout=0.2).call("daemon.status", {})
    return caught.value


def test_a_silent_daemon_that_is_not_stopped_keeps_its_exit_code(running_holder, root):
    """C-17.3, C-5.11 silence is still exit 1; with a verified running holder the fix is never a second daemon."""
    error = silent_call(root)
    assert error.code == Exit.OPERATIONAL
    assert "no response from the daemon within 0.2s" in str(error)
    assert "second daemon" in error.fix and str(running_holder.pid) in error.fix


def test_silence_with_no_lock_to_verify_says_start_one(root):
    """C-5.11 "do not start a second daemon" is said only when a holder was verified; without one, start it."""
    assert not (root / "daemon.lock").exists()
    error = silent_call(root)
    assert error.code == Exit.OPERATIONAL and error.fix == "subfleet daemon start"


def test_a_refused_connect_asks_the_holder_too(running_holder, root, monkeypatch):
    """C-5.11 a full backlog refuses rather than queues, so a refused connect is not "nobody is there"."""
    (root / "daemon.sock").touch()                      # a path nothing listens on: connect is refused
    with pytest.raises(DaemonUnavailable) as caught:
        Client(root, timeout=1).call("daemon.status", {})
    assert not isinstance(caught.value, DaemonStopped)
    assert str(running_holder.pid) in str(caught.value) and "second daemon" in caught.value.fix
    os.kill(running_holder.pid, signal.SIGSTOP)
    try:
        probe = Client(root, timeout=1)
        probe._checked = True                           # it was running when it was checked
        with pytest.raises(DaemonStopped):
            probe.call("daemon.status", {})
    finally:
        os.kill(running_holder.pid, signal.SIGCONT)


def test_a_refused_connect_with_no_holder_is_the_plain_absence(root):
    """C-5.11 with no lock at all, a refused connect is the ordinary "no daemon" and its fix is to start one."""
    (root / "daemon.sock").touch()
    with pytest.raises(DaemonUnavailable) as caught:
        Client(root, timeout=1).call("daemon.status", {})
    assert "no daemon at" in str(caught.value) and caught.value.fix == "subfleet daemon start"


@pytest.mark.parametrize("state,stopped", [("T", True), ("T+", True), ("TN", True), ("S", False),
                                           ("Ss", False), ("R+", False), ("Z", False), (None, False), ("", False)])
def test_stopped_is_any_state_that_begins_with_t(state, stopped):
    """C-5.11 BSD ps prints flags after the letter; only the letter decides."""
    assert client_module.is_stopped(state) is stopped


def test_the_socket_is_named_only_when_it_exists(stopped_holder, root):
    """C-5.11 the message says what the holder holds, and a socket that is not there is not claimed."""
    message, _fix, _command = client_module.stopped_report(stopped_holder.pid, "T", root / "daemon.sock")
    assert "daemon.lock but" in message and "daemon.sock" not in message
    (root / "daemon.sock").touch()
    message, _fix, _command = client_module.stopped_report(stopped_holder.pid, "T", root / "daemon.sock")
    assert "daemon.lock and daemon.sock" in message


def test_an_undecodable_marker_is_no_evidence_not_a_traceback(stopped_holder, root, monkeypatch):
    """C-5.11 a pauser's file is another program's; bytes that are not UTF-8 attribute nothing."""
    path = root / "garbled.pids"
    path.write_bytes(b"\xff\xfe" + str(stopped_holder.pid).encode() + b"\n")
    monkeypatch.setattr(client_module, "KNOWN_PAUSERS", (
        {**client_module.KNOWN_PAUSERS[0], "marker": str(path)},))
    with pytest.raises(DaemonStopped) as caught:
        Client(root).call("daemon.status", {})
    assert caught.value.fix == f"kill -CONT {stopped_holder.pid}"


def test_daemon_stop_continues_a_stopped_holder_so_it_can_exit(stopped_holder, root, capsys):
    """C-5.11 a stopped process runs no SIGTERM handler; `daemon stop` continues it after signalling."""
    assert cli.main(["daemon", "stop"]) == int(Exit.OK)
    assert "SIGCONT" in capsys.readouterr().err
    stopped_holder.wait(timeout=5)                      # the sleeper takes SIGTERM's default action once continued
    assert stopped_holder.returncode == -signal.SIGTERM


# --- daemon status (C-5.11) ---------------------------------------------------

def test_daemon_status_names_a_stopped_holder_and_skips_the_socket(
        stopped_holder, daemon, root, capsys, monkeypatch):
    """C-5.11 `daemon status` says stopped, exits 69, and waits on nothing."""
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
    """C-5.11 `--json` reports `holder_state`, `holder_stopped`, and `diagnosis`."""
    assert cli.main(["daemon", "status", "--json"]) == int(Exit.DAEMON_UNAVAILABLE)
    payload = json.loads(capsys.readouterr().out)
    assert payload["holder_stopped"] is True
    assert payload["holder_state"].startswith("T")
    assert str(stopped_holder.pid) in payload["diagnosis"]
    assert payload["ping"] is False and payload["ping_ms"] < 1000


def test_daemon_start_refuses_to_start_a_second_daemon_over_a_stopped_one(
        stopped_holder, root, capsys):
    """C-5.8, C-5.11 the stopped holder still owns the flock, so continue it."""
    assert cli.main(["daemon", "start"]) == int(Exit.DAEMON_UNAVAILABLE)
    captured = capsys.readouterr()
    assert "is stopped" in captured.err
    assert f"fix: kill -CONT {stopped_holder.pid}" in captured.err


# --- doctor (C-5.11) ----------------------------------------------------------

def test_the_doctor_lock_row_fails_on_a_stopped_holder(stopped_holder, root):
    """C-5.11 the offline table must not pass a daemon that cannot answer."""
    (root / "daemon.sock").touch()
    item = doctor.check_daemon_lock(root)
    assert item["status"] == doctor.FAIL
    assert "is stopped" in item["detail"] and str(stopped_holder.pid) in item["detail"]
    assert item["fix"] == f"`kill -CONT {stopped_holder.pid}`"


def test_the_live_ping_row_carries_the_same_diagnosis(stopped_holder, root):
    """C-5.11 `--live` fails with the diagnosis, not with a bare timeout."""
    item = doctor.check_live(root)
    assert item["status"] == doctor.FAIL
    assert "is stopped" in item["detail"]
    assert item["fix"] == f"`kill -CONT {stopped_holder.pid}`"


# --- attribution (C-5.11) -----------------------------------------------------

def test_a_pauser_is_named_only_when_its_marker_file_lists_the_pid(
        stopped_holder, root, marker):
    """C-5.11 attribution comes from the pauser's own record and nowhere else."""
    marker(stopped_holder.pid)
    stopped = Client(root).stopped_holder()
    assert "clamshell-guard paused it" in str(stopped)
    assert "clamshell-guard-resume" in stopped.fix
    assert "kill -CONT" not in stopped.fix, "the guard would stop it again"


def test_a_marker_file_that_lists_other_pids_attributes_nothing(
        stopped_holder, root, marker):
    """C-5.11 a marker file is evidence about the pids it names, and no others."""
    marker(stopped_holder.pid + 1, stopped_holder.pid + 2)
    stopped = Client(root).stopped_holder()
    assert "clamshell-guard" not in str(stopped)
    assert stopped.fix == f"kill -CONT {stopped_holder.pid}"


def test_no_marker_file_at_all_attributes_nothing(stopped_holder, root, monkeypatch):
    """C-5.11 an absent marker file is not an attribution either."""
    monkeypatch.setattr(client_module, "KNOWN_PAUSERS", (
        {**client_module.KNOWN_PAUSERS[0], "marker": str(root / "absent.pids")},))
    stopped = Client(root).stopped_holder()
    assert "clamshell-guard" not in str(stopped)
    assert stopped.fix == f"kill -CONT {stopped_holder.pid}"


# --- the verbs (C-17.5) -------------------------------------------------------

def test_runs_falls_back_offline_and_says_why(stopped_holder, root, capsys):
    """C-17.5, C-5.11 a stopped daemon is unavailable, and the reader says so."""
    from test_offline import JOB, build_store
    build_store(root)
    assert cli.main(["runs"]) == int(Exit.OK)
    captured = capsys.readouterr()
    assert JOB in captured.out, "the store was read"
    assert "offline" in captured.err
    assert "is stopped" in captured.err
    assert f"fix: kill -CONT {stopped_holder.pid}" in captured.err


def test_status_falls_back_offline_and_says_why(stopped_holder, root, capsys):
    """C-17.5, C-5.11 `status` reads the store and names the stopped holder."""
    from test_offline import build_store
    build_store(root)
    assert cli.main(["status"]) == int(Exit.OK)
    captured = capsys.readouterr()
    assert "offline" in captured.err and "is stopped" in captured.err


def test_no_daemon_and_no_store_still_carries_the_daemon_s_own_fix(
        stopped_holder, root, capsys):
    """C-17.5, C-5.11 the store is what is missing, but the daemon is what to fix."""
    assert cli.main(["runs"]) == int(Exit.DAEMON_UNAVAILABLE)
    captured = capsys.readouterr()
    assert "no store" in captured.err, "the message names what could not be read"
    assert f"fix: kill -CONT {stopped_holder.pid}" in captured.err
    assert "fix: subfleet daemon start" not in captured.err


def test_a_verb_with_no_offline_mode_prints_the_diagnosis_not_daemon_start(
        stopped_holder, root, capsys):
    """C-17.5, C-5.11 `subfleet daemon start` is the wrong fix for this one."""
    assert cli.main(["lanes"]) == int(Exit.DAEMON_UNAVAILABLE)
    captured = capsys.readouterr()
    assert "is stopped" in captured.err
    assert f"fix: kill -CONT {stopped_holder.pid}" in captured.err
    assert "subfleet daemon start" not in captured.err
