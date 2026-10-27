"""D-QUAR-START1 (b) on this host's real processes: the abandon handshake.

The daemon side is `tests/unit/test_daemon_settle.py`'s core with its process calls
put back (`tests/process/test_kill4_fix1_process.py`'s `real`); the guardian is the
real `subfleet.guardian`, run through `tests/guardian_steps.py`, which pauses it
before each of its handshake steps. The daemon's two steps (writing `abandon.json`,
then its last read of `start.json`) are paused the same way, so every order of the
two sides' steps is forced, not hoped for. Every process here is started by the
test, in a scratch state root, and stopped by its recorded identity on failure too.

1. Invariant 1, over every interleaving: a provider never runs for an attempt the
   daemon ended as unlaunched (`Daemon._abandon_start` said nothing can launch).
2. The guardian sees the abandon: no provider ever spawns, and its receipt says so.
3. The daemon sees `start.json` after its abandon: the provider is contained.
4. An unverifiable census at expiry: nothing is signalled, the abandon is written,
   and the guardian exits without spawning.
"""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

from hypothesis import HealthCheck, example, given, settings, strategies as st
import pytest

from subfleet import procs
from subfleet.contracts import attempt_dir
from tests.process.test_kill4_fix1_process import end_group, real, until  # noqa: F401
from tests.unit.test_daemon_settle import ATTEMPT, JOB, attempt, daemon, with_launch  # noqa: F401

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS process census")
REPO = Path(__file__).resolve().parents[2]
STEP_S = 30.0                       # the longest one side may take to reach its next step


class DaemonSteps:
    """The daemon core's handshake steps, paused until the scheduler releases them."""

    def __init__(self, core, pause: set[str]):
        self.pause, self.pending, self.free = pause, [], threading.Event()
        self.lock, self.go = threading.Lock(), threading.Condition()
        self.released = 0
        publish, read = core._publish, core._read_json
        inside = threading.local()
        handshake = core._abandon_start if hasattr(type(core), "_abandon_start") else None

        def step(name):
            if name not in self.pause or self.free.is_set():
                return
            with self.go:
                self.pending.append(name)
                mine = len(self.pending)
                self.go.notify_all()
                assert self.go.wait_for(lambda: self.released >= mine or self.free.is_set(), STEP_S * 4)

        def published(role, path, contents):
            if role == "abandon":
                step("write-abandon")
            return publish(role, path, contents)

        def read_json(path):
            if getattr(inside, "on", False) and Path(path).name == "start.json":
                step("read-start")
            return read(path)

        def abandon_start(a, detail):
            inside.on = True
            try:
                return handshake(a, detail)
            finally:
                inside.on = False
        self.core, self.saved = core, {name: core.__dict__.get(name) for name in
                                       ("_publish", "_read_json", "_abandon_start")}
        core._publish, core._read_json = published, read_json
        if handshake is not None:
            core._abandon_start = abandon_start

    def restore(self) -> None:
        self.run_free()
        for name, value in self.saved.items():
            if value is None:
                self.core.__dict__.pop(name, None)
            else:
                self.core.__dict__[name] = value

    def waiting(self) -> bool:
        with self.go:
            return len(self.pending) > self.released

    def release(self) -> None:
        with self.go:
            self.released += 1
            self.go.notify_all()

    def run_free(self) -> None:
        self.free.set()
        with self.go:
            self.go.notify_all()


class GuardianSteps:
    """A real guardian under `tests/guardian_steps.py`, its launch gate opened."""

    def __init__(self, core, adir: Path, cwd: Path, provider: list[str], steps: Path, *, marked: bool = True):
        steps.mkdir(parents=True, exist_ok=True)
        self.steps, self.next = steps, 1
        gate_r, gate_w = os.pipe()
        env = {**os.environ, "PYTHONPATH": str(REPO), "GUARDIAN_STEPS_DIR": str(steps)}
        if marked:
            env.update(SUBFLEET_ATTEMPT=ATTEMPT, SUBFLEET_ROOT=str(core.root))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "tests.guardian_steps", "--attempt-dir", str(adir), "--cwd", str(cwd),
             "--stdout-path", str(adir / "stdout"), "--stderr-path", str(adir / "stderr"),
             "--launch-fd", str(gate_r), "--", *provider],
            env=env, cwd=str(REPO), pass_fds=(gate_r,), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(gate_r)
        os.write(gate_w, b"1")
        os.close(gate_w)
        self.identity = None

    def step(self) -> str | None:
        """The step the guardian waits at, or None while it is between steps."""
        found = sorted(self.steps.glob(f"{self.next}.*"))
        found = [path for path in found if not path.name.endswith(".go")]
        return found[0].name.split(".", 1)[1] if found else None

    def finished(self) -> bool:
        return self.proc.poll() is not None

    def release(self) -> None:
        (self.steps / f"{self.next}.go").touch()
        self.next += 1

    def run_free(self) -> None:
        for n in range(self.next, self.next + 8):
            (self.steps / f"{n}.go").touch()

    def wait_for_step(self, name: str | None = None) -> str | None:
        until(lambda: self.step() is not None or self.finished(), timeout=STEP_S)
        step = self.step()
        assert name is None or step == name, (name, step, self.proc.poll())
        return step

    def stop(self) -> None:
        self.run_free()
        if self.proc.poll() is None:
            identity = self.identity or procs.identity(self.proc.pid)
            if identity is not None:
                end_group(identity)
            self.proc.wait(timeout=30)


def recorded(core, guardian: GuardianSteps) -> None:
    """Record the guardian as the starting attempt's, as `_launch` does before its gate opens."""
    until(lambda: guardian.proc.pid in procs.snapshot().group(guardian.proc.pid))       # it has called setsid
    guardian.identity = procs.identity(guardian.proc.pid)
    assert guardian.identity is not None
    core.store.update_attempt(ATTEMPT, state="starting", guardian_pid=guardian.proc.pid, pgid=guardian.proc.pid,
                              child_pid=None, boot_id=guardian.identity.boot_id,
                              proc_start=guardian.identity.proc_start)
    core.start_grace_s, core._starting_deadlines[ATTEMPT] = 10, 0.0     # expired, and past the bound


def receipt(adir: Path) -> dict | None:
    try:
        return json.loads((adir / "exit.json").read_text())
    except FileNotFoundError:
        return None


# 1. Invariant 1 over every interleaving of the handshake's steps.

STEPS = ["daemon", "daemon", "guardian", "guardian", "guardian"]
ORDERS = st.permutations(STEPS)
#: Every distinct order of the two sides' steps (5 choose 2 = 10), each an explicit example,
#: so no generated run can miss one; generation then adds repeats.
EVERY_ORDER = sorted({tuple("daemon" if i in pair else "guardian" for i in range(5))
                      for pair in itertools.combinations(range(5), 2)})


def every_order(test):
    for order in EVERY_ORDER:
        test = example(order=list(order))(test)
    return test


@every_order
@settings(max_examples=24, deadline=None, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(order=ORDERS)
def test_no_provider_runs_for_an_attempt_the_daemon_ended_unlaunched_in_any_interleaving(real, order):
    """The daemon writes `abandon.json` then reads `start.json`; the guardian checks for
    the abandon after its gate, writes `start.json`, then checks again before it spawns.
    Each side writes its own file before reading the other's, so at least one sees the
    other: the daemon's read finds `start.json` (possibly launched), or the guardian's
    check finds the abandon (it spawns nothing). The guardian's three steps and the
    daemon's two are released in `order`; the provider would touch `ran`."""
    with tempfile.TemporaryDirectory(prefix="qs1-") as scratch:
        scratch = Path(scratch).resolve()
        a = {"attempt_id": f"{JOB}-x/a1", "job_id": f"{JOB}-x", "seq": 1, "state": "starting"}
        adir = attempt_dir(real.root, a["job_id"], 1)
        for leftover in ("abandon.json", "start.json", "exit.json"):
            (adir / leftover).unlink(missing_ok=True)
        adir.mkdir(parents=True, exist_ok=True)
        ran = scratch / "ran"
        daemon = DaemonSteps(real, {"write-abandon", "read-start"})
        guardian = GuardianSteps(real, adir, scratch, ["/bin/sh", "-c", f'touch "{ran}"'], scratch / "steps",
                            marked=False)
        verdict: list = []
        worker = threading.Thread(target=lambda: verdict.append(real._abandon_start(a, "starting-no-receipt")),
                                  daemon=True)
        try:
            worker.start()
            for actor in order:
                if actor == "guardian":
                    if guardian.wait_for_step() is not None:
                        guardian.release()
                else:
                    until(lambda: daemon.waiting() or not worker.is_alive(), timeout=STEP_S)
                    if daemon.waiting():
                        daemon.release()
            daemon.run_free()
            guardian.run_free()
            worker.join(timeout=STEP_S)
            assert verdict, "the daemon's handshake raised or never returned"
            assert guardian.proc.wait(timeout=STEP_S) is not None
            unlaunched = verdict[0]
            spawned = ran.exists()
            # Invariant 1: never both.
            assert not (unlaunched and spawned), (order, unlaunched, spawned)
            # The guardian always finishes one way: it spawned, or its receipt says it abandoned.
            ended = receipt(adir) or {}
            assert spawned != bool(ended.get("abandoned")), (order, spawned, ended)
            if ended.get("abandoned"):
                assert ended["child_pid"] is None, ended
        finally:
            daemon.restore()
            guardian.stop()


# 2. and 3. Through start grace (`_process_attempt`), each side's order forced.

def start_grace(real, monkeypatch, pause: set[str]):
    with_launch(real, monkeypatch)
    real.store.update_job(JOB, max_attempts=1)
    real.term_grace_s, real.kill_settle_s = 3.0, 3.0
    daemon = DaemonSteps(real, pause)
    worker = threading.Thread(target=real._process_attempt, args=(ATTEMPT,), daemon=True)
    return daemon, worker


def test_a_guardian_that_sees_the_abandon_spawns_nothing_and_its_receipt_says_so(real, monkeypatch, tmp_path):
    """The daemon writes the abandon; the guardian then writes start.json and checks:
    it spawns nothing and writes an abandoned receipt. The daemon's last read then
    finds start.json, contains what is left (nothing), and the abandoned receipt
    reads as unlaunched, never as a provider result."""
    adir = attempt_dir(real.root, JOB, 1)
    ran = tmp_path / "ran"
    guardian = GuardianSteps(real, adir, tmp_path, ["/bin/sh", "-c", f'touch "{ran}"; exec sleep 60'], tmp_path / "steps")
    daemon, worker = start_grace(real, monkeypatch, {"read-start"})
    try:
        guardian.wait_for_step("read-abandon")                  # after its gate: nothing yet
        guardian.release()
        guardian.wait_for_step("write-start")
        recorded(real, guardian)
        worker.start()
        until(daemon.waiting, timeout=STEP_S)                   # the abandon is written; its last read waits
        assert (adir / "abandon.json").exists() and not (adir / "start.json").exists()
        guardian.release()                                      # start.json
        guardian.wait_for_step("read-abandon")
        guardian.release()                                      # it sees the abandon
        assert guardian.proc.wait(timeout=STEP_S) == 127
        daemon.release()                                        # the daemon's last read finds start.json
        worker.join(timeout=60)
        assert not worker.is_alive()
        time.sleep(.5)
        assert not ran.exists(), "the provider ran after the abandon"
        ended = receipt(adir)
        assert ended["abandoned"]["reason"] == "starting-no-receipt" and ended["child_pid"] is None, ended
        assert ended["rc"] == 127 and ended["spawn_error"].startswith("abandoned before launch"), ended
        a = attempt(real)
        assert (a["state"], a["outcome_detail"]) == ("failed", "starting-no-receipt"), a
        assert not real.store.get_job(JOB)["accepted_attempt_id"]
    finally:
        daemon.run_free()
        guardian.stop()


def test_a_start_json_the_daemon_reads_after_its_abandon_is_contained_as_launched(real, monkeypatch, tmp_path):
    """The guardian checks (no abandon yet), writes start.json and checks again, still
    before the daemon writes its abandon: it spawns its provider. The daemon's last
    read finds start.json, so the full protocol runs against a fresh census, the
    group's TERM first, and the provider is contained; the end is not unlaunched."""
    adir = attempt_dir(real.root, JOB, 1)
    ran = tmp_path / "ran"
    guardian = GuardianSteps(real, adir, tmp_path, ["/bin/sh", "-c", f'touch "{ran}"; exec sleep 60'], tmp_path / "steps")
    daemon, worker = start_grace(real, monkeypatch, {"write-abandon"})
    try:
        guardian.wait_for_step("read-abandon")
        guardian.release()
        guardian.wait_for_step("write-start")
        recorded(real, guardian)
        worker.start()
        until(daemon.waiting, timeout=STEP_S)                   # past its census and re-read; abandon next
        assert not (adir / "abandon.json").exists()
        guardian.release()                                      # start.json
        guardian.wait_for_step("read-abandon")
        guardian.release()                                      # no abandon: it spawns
        until(ran.exists, timeout=STEP_S)
        provider = next(pid for pid, row in procs.snapshot().rows.items()
                        if row[0] == guardian.proc.pid)
        daemon.release()                                        # abandon, then the last read finds start.json
        worker.join(timeout=60)
        assert not worker.is_alive()
        assert guardian.proc.wait(timeout=STEP_S) is not None
        assert procs.identity(provider) is None, "the provider outlived the protocol"
        assert ("group", guardian.proc.pid, int(signal.SIGTERM)) == real.sent[0], real.sent
        ended = receipt(adir)
        assert ended and not ended.get("abandoned") and ended["child_pid"] == provider, ended
        a = attempt(real)
        assert a["outcome_detail"] != "starting-no-receipt" and a["killed_by"] == "recovery", a
        assert a["state"] not in ("starting", "running", "succeeded"), a
    finally:
        daemon.run_free()
        guardian.stop()


# 4. An unverifiable census at expiry.

def test_an_unverifiable_census_at_expiry_signals_nothing_and_the_guardian_never_spawns(real, monkeypatch, tmp_path):
    """No process table and no identity can be read while start grace decides: the
    census is unverifiable and every leader or identity check fails, so the protocol
    delivers nothing. The abandon is written first, and the guardian, released once
    the attempt has ended, writes start.json, finds the abandon and exits."""
    adir = attempt_dir(real.root, JOB, 1)
    ran = tmp_path / "ran"
    guardian = GuardianSteps(real, adir, tmp_path, ["/bin/sh", "-c", f'touch "{ran}"'], tmp_path / "steps", marked=False)
    try:
        guardian.wait_for_step("read-abandon")
        guardian.release()
        guardian.wait_for_step("write-start")
        recorded(real, guardian)
        with_launch(real, monkeypatch)
        real.term_grace_s, real.kill_settle_s = .3, .3

        def unavailable(*args, **kwargs):
            raise procs.InspectionError("process table unavailable")
        with pytest.MonkeyPatch.context() as outage:
            outage.setattr(procs, "snapshot", unavailable)
            outage.setattr(procs, "identity", unavailable)
            real._process_attempt(ATTEMPT)
        assert not real.sent, real.sent
        assert json.loads((adir / "abandon.json").read_text())["reason"] == "starting-no-receipt"
        a = attempt(real)
        assert a["state"] not in ("starting", "running"), a
        guardian.run_free()
        assert guardian.proc.wait(timeout=STEP_S) == 127
        time.sleep(.3)
        assert not ran.exists(), "the guardian spawned its provider for an abandoned attempt"
        assert receipt(adir)["abandoned"]["reason"] == "starting-no-receipt"
    finally:
        guardian.stop()


# Mutation controls: each order mutant must fail the interleaving property.

@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the interleaving property must detect this mutant")
@pytest.mark.parametrize("mutant", ["daemon-reads-first", "guardian-checks-first"])
def test_a_handshake_order_mutant_fails_the_interleaving_property(mutant, real, monkeypatch):
    from subfleet.daemon import Daemon as Core
    from tests.unit.test_census_round1_mutations import replace_function
    from tests.unit.test_kill4_fix2b_mutations import MUTANTS
    if mutant == "daemon-reads-first":
        for owner, name, replacements in MUTANTS[mutant]:
            replace_function(monkeypatch, owner if owner is not Core else Core, name, replacements)
    else:
        monkeypatch.setenv("GUARDIAN_STEPS_MUTANT", "check-first")
    test_no_provider_runs_for_an_attempt_the_daemon_ended_unlaunched_in_any_interleaving(real=real)
