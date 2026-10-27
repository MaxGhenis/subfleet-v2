"""D-KILL4 fix round 1 on this host's real processes, through the in-process daemon core.

The core is `tests/unit/test_daemon_settle.py`'s, with its process calls put back:
the census reads the real process table and the kill protocol signals real
processes. Every process here is started by the test, in a scratch state root, and
stopped by its recorded identity on failure too; nothing else is ever signalled.

1. Review r1's P2: the real `subfleet.guardian`, its launch gate opened, stopped
   before its start.json while start grace expires. The protocol must never start
   its provider: no continue, the group KILLed at once.
2. Review r1's P1: a leaderless process group at an attempt's recorded pgid, left by
   a process that called setsid, forked a sleeping member and exited. `kill` on the
   finished job must never signal the member.
3. Review r1's P3-1: a TERM-ignoring provider starts a child that stops itself every
   0.25 s; the attempt is cancelled. Every child born after the group's TERM gets a
   TERM of its own before its continue.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from subfleet import daemon as daemon_module, procs, protocol
from subfleet.contracts import attempt_dir
from tests.unit.test_daemon_settle import ATTEMPT, JOB, attempt, daemon, with_launch  # noqa: F401

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS process census")
REPO = Path(__file__).resolve().parents[2]
REAL = {name: getattr(procs, name) for name in ("same_process", "signal_group", "signal_process")}


@pytest.fixture
def real(daemon, monkeypatch):
    """The settle core with real process calls, and a record of every signal it sends."""
    try:
        if procs.identity(os.getpid()) is None:
            pytest.skip("process identity unavailable")
    except procs.InspectionError:
        pytest.skip("this sandbox denies ps/sysctl")
    sent: list[tuple[str, int, int]] = []

    def group(pgid, sig, **kwargs):
        delivered = REAL["signal_group"](pgid, sig, **kwargs)
        if delivered:
            sent.append(("group", pgid, int(sig)))
        return delivered

    def single(recorded, sig, **kwargs):
        delivered = REAL["signal_process"](recorded, sig, **kwargs)
        if delivered:
            sent.append(("pid", recorded.pid, int(sig)))
        return delivered
    monkeypatch.setattr(procs, "same_process", REAL["same_process"])
    monkeypatch.setattr(procs, "signal_group", group)
    monkeypatch.setattr(procs, "signal_process", single)
    monkeypatch.setattr(daemon, "_schedule", lambda key, fn, *args, **kw: fn(*args))
    daemon.sent = sent
    return daemon


def until(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(.02)


def end_group(identity: procs.ProcessIdentity | None) -> None:
    if identity is not None:
        REAL["signal_group"](identity.pid, signal.SIGKILL, boot_id=identity.boot_id, proc_start=identity.proc_start)


def test_start_grace_never_continues_a_stopped_guardian_so_its_provider_never_starts(real, monkeypatch, tmp_path):
    with_launch(real, monkeypatch)
    adir = attempt_dir(real.root, JOB, 1)
    started = tmp_path / "provider-started"
    beats = tmp_path / "beats"
    provider = ["/bin/sh", "-c", f'echo started > "{started}"; while :; do echo beat >> "{beats}"; sleep .2; done']
    gate_r, gate_w = os.pipe()
    env = {**os.environ, "PYTHONPATH": str(REPO), "SUBFLEET_ATTEMPT": ATTEMPT, "SUBFLEET_ROOT": str(real.root)}
    guardian = subprocess.Popen(
        [sys.executable, "-m", "subfleet.guardian", "--attempt-dir", str(adir), "--cwd", str(tmp_path),
         "--stdout-path", str(adir / "stdout"), "--stderr-path", str(adir / "stderr"),
         "--launch-fd", str(gate_r), "--", *provider],
        env=env, pass_fds=(gate_r,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(gate_r)
    identity = None
    try:
        until(lambda: guardian.pid in procs.snapshot().group(guardian.pid))      # it has called setsid
        os.kill(guardian.pid, signal.SIGSTOP)                                    # stopped before start.json
        os.write(gate_w, b"1")                                                   # its launch gate is open
        os.close(gate_w)
        gate_w = -1
        identity = procs.identity(guardian.pid)
        assert identity is not None and not (adir / "start.json").exists()
        real.store.update_attempt(ATTEMPT, state="starting", guardian_pid=guardian.pid, pgid=guardian.pid,
                                  child_pid=None, boot_id=identity.boot_id, proc_start=identity.proc_start)
        real.store.update_job(JOB, max_attempts=1)
        real.start_grace_s, real._starting_deadlines[ATTEMPT] = 10, 0.0
        real.term_grace_s, real.kill_settle_s = 3.0, 3.0
        real._process_attempt(ATTEMPT)
        guardian.wait(timeout=10)
        time.sleep(.5)                                  # a provider, had it started, would have written by now
        assert not started.exists() and not beats.exists(), "the protocol started the provider it was stopping"
        assert not (adir / "start.json").exists()
        assert guardian.returncode == -signal.SIGKILL
        assert real.sent[0] == ("group", guardian.pid, int(signal.SIGKILL)), real.sent
        assert not [s for s in real.sent if s[2] in (signal.SIGTERM, signal.SIGCONT)], real.sent
        a = attempt(real)
        # Fix round 2b, decision 1: a contained start grace ends unlaunched (its abandon written first).
        assert (a["state"], a["outcome_detail"]) == ("failed", "starting-no-receipt"), a
        assert json.loads(a["evidence_json"])["terminal_containment"][-1]["contained"]
        assert (adir / "abandon.json").exists()
    finally:
        if gate_w >= 0:
            os.close(gate_w)
        if guardian.poll() is None:
            end_group(identity)
            guardian.wait(timeout=10)


def test_kill_on_a_finished_job_never_signals_a_real_leaderless_group_at_its_recorded_pgid(real, tmp_path):
    pidfile = tmp_path / "member.pid"
    leader = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys; m = subprocess.Popen(['/bin/sleep', '60']); "
         "open(sys.argv[1], 'w').write(str(m.pid))", str(pidfile)],
        start_new_session=True, stdin=subprocess.DEVNULL)
    member = None
    try:
        assert leader.wait(timeout=30) == 0                  # setsid, a sleeping member, then gone
        member = procs.identity(int(pidfile.read_text()))
        assert member is not None
        table = procs.snapshot()
        assert table.rows[member.pid][1] == leader.pid and leader.pid not in table.rows   # leaderless
        # A finished attempt recorded that pgid for its guardian, born long before.
        real.store.update_attempt(ATTEMPT, state="succeeded", guardian_pid=leader.pid, pgid=leader.pid,
                                  child_pid=None, boot_id=member.boot_id, proc_start="Sat Sep  5 10:00:00 2026")
        real.store.update_job(JOB, state="succeeded")
        real.kill(protocol.KillArgs(job_id=JOB))
        real.kill(protocol.KillArgs(job_id=JOB, confirm_dead=True))
        assert procs.same_process(member.pid, member.boot_id, member.proc_start), "the member was signalled"
        assert not [s for s in real.sent if s[1] in (member.pid, leader.pid)], real.sent
        assert attempt(real)["state"] == "succeeded"
    finally:
        if member is not None:
            REAL["signal_process"](member, signal.SIGKILL)


GUARDIAN = """
import os, signal, subprocess, sys, time
os.setsid()
signal.signal(signal.SIGTERM, lambda *_: None)          # as subfleet.guardian: its TERM is a no-op
open(sys.argv[1], "w").write(str(os.getpid()))
subprocess.Popen([sys.executable, "-c", sys.argv[2]]).wait()
time.sleep(60)
"""
PROVIDER = """
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)            # a provider that ignores TERM
child = ("import os, signal, time; signal.signal(signal.SIGTERM, lambda *_: os._exit(0)); "
         "os.kill(os.getpid(), signal.SIGSTOP); time.sleep(60)")
end = time.monotonic() + 60
while time.monotonic() < end:
    subprocess.Popen([sys.executable, "-c", child], preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_DFL))
    time.sleep(.25)
"""


def test_self_stopping_children_born_after_the_group_term_each_get_their_own_term_before_a_continue(
        real, tmp_path):
    pidfile = tmp_path / "guardian.pid"
    env = {**os.environ, "SUBFLEET_ATTEMPT": ATTEMPT, "SUBFLEET_ROOT": str(real.root)}
    launcher = subprocess.Popen([sys.executable, "-c", GUARDIAN, str(pidfile), PROVIDER], env=env,
                                stdin=subprocess.DEVNULL)
    guardian = None
    try:
        until(lambda: pidfile.exists() and pidfile.read_text().strip())
        guardian = procs.identity(int(pidfile.read_text()))
        assert guardian is not None and guardian.pid == launcher.pid
        until(lambda: len(procs.snapshot().group(guardian.pid)) >= 4)       # guardian, provider, children
        real.store.update_attempt(ATTEMPT, state="running", guardian_pid=guardian.pid, pgid=guardian.pid,
                                  child_pid=None, boot_id=guardian.boot_id, proc_start=guardian.proc_start)
        real.store.update_job(JOB, cancel_requested_at=daemon_module.utcnow())
        real.term_grace_s, real.kill_settle_s = 3.0, 3.0
        real._process_attempt(ATTEMPT)
        first = json.loads(real.store.one("SELECT data_json FROM events WHERE kind='attempt.containment' "
                                          "ORDER BY event_id")["data_json"])["census"]
        before = set(first["group_pids"])                     # the members the group's TERM reached
        assert ("group", guardian.pid, int(signal.SIGTERM)) in real.sent, real.sent
        continued = [pid for kind, pid, sig in real.sent if kind == "pid" and sig == int(signal.SIGCONT)]
        joiners = [pid for pid in continued if pid not in before]
        assert len(joiners) >= 5, (joiners, real.sent)
        for pid in joiners:
            cont = real.sent.index(("pid", pid, int(signal.SIGCONT)))
            assert ("pid", pid, int(signal.SIGTERM)) in real.sent[:cont], (pid, real.sent)
        assert guardian.pid not in continued
    finally:
        end_group(guardian)
        launcher.wait(timeout=10)
