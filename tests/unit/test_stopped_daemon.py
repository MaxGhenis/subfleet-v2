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
from subfleet.client import Client, DaemonStopped, DaemonUnavailable, boot_id, proc_start
from subfleet.contracts import Exit
from subfleet.protocol import ProtocolError


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


def test_a_stop_during_the_call_is_re_diagnosed_after_the_timeout(stopped_holder, root):
    """C-5.11 a daemon stopped mid-call is named, not reported as silence."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(root / "daemon.sock"))
    listener.listen(8)                   # accepts into the backlog, answers nothing
    probe = Client(root, timeout=0.2)
    probe._checked = True                # the holder was running when it was checked
    try:
        with pytest.raises(DaemonStopped) as caught:
            probe.call("daemon.status", {})
    finally:
        listener.close()
    assert str(stopped_holder.pid) in str(caught.value)


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


def test_a_silent_daemon_that_is_not_stopped_keeps_its_exit_code(root, capsys):
    """C-17.3 only the diagnosis is new: silence is still an operational error."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(root / "daemon.sock"))
    listener.listen(8)
    try:
        with pytest.raises(ProtocolError) as caught:
            Client(root, timeout=0.2).call("daemon.status", {})
    finally:
        listener.close()
    assert caught.value.code == Exit.OPERATIONAL
    assert "no response from the daemon within 0.2s" in str(caught.value)
    assert "second daemon" in (caught.value.fix or ""), "and never `daemon start`"


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
    assert item["fix"] == f"kill -CONT {stopped_holder.pid}"


def test_the_live_ping_row_carries_the_same_diagnosis(stopped_holder, root):
    """C-5.11 `--live` fails with the diagnosis, not with a bare timeout."""
    item = doctor.check_live(root)
    assert item["status"] == doctor.FAIL
    assert "is stopped" in item["detail"]
    assert item["fix"] == f"kill -CONT {stopped_holder.pid}"


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
