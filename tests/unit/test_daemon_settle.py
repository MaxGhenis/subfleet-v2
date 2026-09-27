"""C-4.2, C-5.6, C-5.9: what the daemon decides when the process table is still moving.

A census the kernel is still draining is re-read, not quarantined; a guardian that
cannot be inspected decides nothing; a receipt on disk always beats a stale lost verdict."""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from types import SimpleNamespace

import pytest

from subfleet import daemon as daemon_module
from subfleet.contracts import (
    Attestation, AttestationResult, Credential, Lane, LaneOwner, Launch, Outcome, OutcomeClass,
    attempt_dir,
)
from subfleet.daemon import Daemon
from subfleet.guardian import atomic_publish
from subfleet.procs import Containment, ProcessIdentity
from subfleet.store import Store

JOB = "20260905-100000-settle"
ATTEMPT = JOB + "/a1"
STARTED = "Sat Sep  5 10:00:00 2026"
BUSY = Containment(frozenset({4243}), frozenset({4243}), frozenset(), False,
                   {4243: ProcessIdentity(4243, "boot", STARTED)}, (),
                   {4243: {"ppid": 4242, "pgid": 4242, "stat": "R"}})
GUARDIAN_ONLY = Containment(frozenset({4242}), frozenset({4242}), frozenset(), False,
                            {4242: ProcessIdentity(4242, "boot", STARTED)}, (),
                            {4242: {"ppid": 1, "pgid": 4242, "stat": "S"}})
UNVERIFIABLE = Containment(unverifiable=True, errors=("group enumeration unavailable",
                                                      "descendant enumeration unavailable"))
EMPTY = Containment()


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    """A daemon core with a running attempt and no processes: the census is injected."""
    root = tmp_path / "state"
    attempt_dir(root, JOB, 1).mkdir(parents=True)
    core = object.__new__(Daemon)
    core.root, core.store = root, Store(root / "state.sqlite3")
    core.stopping = threading.Event()
    core.term_grace_s, core.kill_settle_s, core.exit_settle_s = .05, .3, .3
    core._exit_settle = {}
    core._children, core._pending_launches, core._starting_deadlines, core._census_next = {}, set(), {}, {}
    core._liveness_next, core.liveness_interval_s = {}, 0   # every tick inspects, as these cases expect
    core._launches, core._export_locks = {}, {}
    core.log = logging.getLogger("subfleet.test")
    core._salvage = lambda job, a: ([], None)
    core._record_identity = lambda *args: None
    core._export = lambda job_id: None
    core.timers = SimpleNamespace(record_auth_dead=lambda *args: None, metadata={})   # C-11.2: the pin roster reads it
    core._notify = lambda: None
    core._boundary = lambda *args: None
    core._publish = lambda role, path, contents: atomic_publish(path, contents)
    home = root / "home"
    core.store.put_lane(Lane("codex-1", "codex", "codex:test", Credential("codex", str(home), "home"),
                             str(home), LaneOwner.V2, False))
    core.store.add_job(job_id=JOB, request_id="settle-1", payload_digest="digest", kind="run",
                       state="running", workdir=str(root), prompt_path=str(root / "prompt.md"),
                       sandbox="read-only")
    core.store.add_attempt(attempt_id=ATTEMPT, job_id=JOB, seq=1, lane_id="codex-1",
                           model_requested="astra", state="running", guardian_pid=4242,
                           child_pid=4243, pgid=4242, boot_id="boot", proc_start=STARTED,
                           started_at="2026-09-05T14:00:00Z", evidence_json="{}")
    # No real process is signalled or inspected: the leader is gone, signals succeed.
    monkeypatch.setattr(daemon_module.procs, "same_process", lambda *args, **kwargs: False)
    monkeypatch.setattr(daemon_module.procs, "signal_group", lambda *args, **kwargs: True)
    monkeypatch.setattr(daemon_module.procs, "signal_process", lambda *args, **kwargs: True)
    yield core
    core.store.close()


def attempt(core) -> dict:
    return dict(core.store.get_attempt(ATTEMPT))


def draining(core, busy_for_s: float, busy=BUSY) -> list[float]:
    """Inject a census that stays busy for `busy_for_s` after its first read, then empties."""
    calls: list[float] = []
    first: list[float] = []

    def contain(a):
        now = time.monotonic()
        first.append(now) if not first else None
        calls.append(now - first[0])
        return busy if now - first[0] < busy_for_s else EMPTY
    core._contain = contain
    return calls


def test_c5_6_kill_re_reads_a_draining_census_within_the_settle_window(daemon, monkeypatch):
    """C-5.6 pids still being torn down after SIGKILL are re-read for kill_settle_s, not quarantined."""
    now = 0.0
    killed_at = None
    signals = []
    calls = []

    def wait(timeout):
        nonlocal now
        now += timeout
        return False

    def signal_group(pgid, sig, **identity):
        nonlocal killed_at
        signals.append(sig)
        if sig == signal.SIGKILL:
            killed_at = now
        return True

    def contain(a):
        # The process ignores TERM and starts draining only after KILL. An
        # overloaded host must not make it disappear before escalation occurs.
        elapsed = None if killed_at is None else now - killed_at
        census = EMPTY if elapsed is not None and elapsed >= .15 else BUSY
        calls.append((elapsed, census.verified_empty))
        return census

    # Replace only this daemon module's clock, not the shared time module or
    # real threading primitives used by pytest and filesystem publication.
    monkeypatch.setattr(daemon_module, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(daemon_module.procs, "signal_group", signal_group)
    daemon.stopping = SimpleNamespace(wait=wait)
    daemon._contain = contain
    daemon._kill_attempt(attempt(daemon))
    a = attempt(daemon)
    assert a["state"] == "finalizing", a
    assert a["quarantine_reason"] is None
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    after_kill = [(elapsed, empty) for elapsed, empty in calls if elapsed is not None]
    assert len(after_kill) >= 3
    assert all(not empty for _, empty in after_kill[:-1])
    assert after_kill[-1][1] is True
    assert .15 <= after_kill[-1][0] <= daemon.kill_settle_s
    receipt = json.loads((attempt_dir(daemon.root, JOB, 1) / "exit.json").read_text())
    assert receipt["signal"] == 9 and receipt["killed_by"] == "operator"


def test_c5_6_kill_quarantines_only_after_the_settle_window(daemon):
    """C-5.6 a census that never empties quarantines once kill_settle_s has elapsed, with its evidence."""
    draining(daemon, 60)
    started = time.monotonic()
    daemon._kill_attempt(attempt(daemon))
    assert time.monotonic() - started >= .3
    a = attempt(daemon)
    assert a["state"] == "quarantined"
    detail = json.loads(a["quarantine_reason"])
    assert detail["reason"] == "termination could not verify containment"
    assert detail["live_pids"] == [4243]
    assert detail["shapes"]["4243"] == {"ppid": 4242, "pgid": 4242, "stat": "R"}
    assert daemon.store.get_job(JOB)["state"] == "lost"
    kinds = [row["kind"] for row in daemon.store.list_events(JOB)]
    assert "attempt.quarantined" in kinds


@pytest.mark.parametrize("census", [BUSY, GUARDIAN_ONLY, UNVERIFIABLE],
                         ids=["survivor", "guardian-only", "unverifiable"])
def test_c5_9_exit_receipt_census_waits_out_exit_settle_before_quarantining(daemon, census):
    """C-5.9 after exit.json the census may drain for exit_settle_s; past it, writers remain."""
    daemon.store.update_attempt(ATTEMPT, state="finalizing", rc=0)
    atomic_publish(attempt_dir(daemon.root, JOB, 1) / "exit.json",
                   json.dumps({"rc": 0, "finished_at": "2026-09-05T14:01:00Z", "wall_s": 60,
                               "child_pid": 4243}).encode())
    daemon._contain = lambda a: census
    daemon._finalize(attempt(daemon))
    assert attempt(daemon)["state"] == "finalizing"
    assert ATTEMPT in daemon._exit_settle
    daemon._finalize(attempt(daemon))
    assert attempt(daemon)["state"] == "finalizing"
    daemon._exit_settle[ATTEMPT] -= 1.0
    daemon._finalize(attempt(daemon))
    a = attempt(daemon)
    assert a["state"] == "quarantined"
    assert json.loads(a["quarantine_reason"])["reason"] == "writers remain after exit receipt"
    assert ATTEMPT not in daemon._exit_settle


class StubAdapter:
    """Classifies every exit as ok; the daemon's own logic is what is under test."""

    def classify(self, adir, launch, exit_info):
        return Outcome(OutcomeClass.OK, "ok")

    def attest(self, adir, launch, outcome, model):
        return AttestationResult(Attestation.UNATTESTED, None, "stub")

    def deliverable(self, adir, launch, outcome):
        return b"done\n"


def receipt_path(core):
    return attempt_dir(core.root, JOB, 1) / "exit.json"


def publish_receipt(core, rc=0):
    atomic_publish(receipt_path(core), json.dumps({"rc": rc, "signal": None, "finished_at": "2026-09-05T14:01:00Z",
                                                   "wall_s": .026, "child_pid": 4243}).encode())


def with_launch(core, monkeypatch):
    adir = attempt_dir(core.root, JOB, 1)
    (adir / "stdout").write_text("hello\n")
    core._launches[ATTEMPT] = Launch(argv=("fake",), env_add={}, env_remove=(), cwd=str(core.root),
                                     stdin_path=None, stdout_path=str(adir / "stdout"),
                                     stderr_path=str(adir / "stderr"), raw_stream_path=None,
                                     native_session_id=None, lane_id="codex-1")
    monkeypatch.setattr(daemon_module, "get_adapter", lambda provider: StubAdapter())


def never_census(a):
    raise AssertionError("the census must not run on this path")


def test_c4_2_receipt_on_disk_wins_over_a_stale_lost_verdict(daemon, monkeypatch):
    """C-4.2 a guardian found dead right after publishing exit.json completed its attempt, not lost it."""
    with_launch(daemon, monkeypatch)
    publish_receipt(daemon, rc=0)
    daemon._contain = lambda a: EMPTY
    daemon._lost(attempt(daemon))      # what the tick calls for a dead guardian and an empty census
    a = attempt(daemon)
    assert a["state"] == "succeeded", a
    assert a["rc"] == 0 and a["outcome_class"] == "ok"
    assert a["outcome_detail"] != "guardian lost without exit receipt"
    job = daemon.store.get_job(JOB)
    assert job["state"] == "succeeded" and job["accepted_attempt_id"] == ATTEMPT
    assert (attempt_dir(daemon.root, JOB, 1) / "deliverable.md").read_bytes() == b"done\n"


def test_c4_2_missing_receipt_is_still_a_loss(daemon, monkeypatch):
    """C-4.2 without a receipt the dead-guardian path records the loss as before."""
    with_launch(daemon, monkeypatch)
    daemon._contain = lambda a: EMPTY
    daemon._lost(attempt(daemon))
    a = attempt(daemon)
    assert a["state"] == "lost" and a["outcome_detail"] == "guardian lost without exit receipt"
    # A lost read-only attempt is retried (C-4.2), so the job waits for its next attempt.
    assert daemon.store.get_job(JOB)["state"] == "waiting"


def test_c4_2_dead_guardian_with_a_late_receipt_finalizes_instead_of_lost(daemon, monkeypatch):
    """C-4.2 the tick re-reads exit.json after finding the guardian dead: the receipt landed in between."""
    def dead_after_publishing(pid, boot_id, proc_start):
        publish_receipt(daemon, rc=0)     # the guardian wrote and exited during the liveness check
        return "dead"
    monkeypatch.setattr(daemon_module.procs, "liveness", dead_after_publishing)
    daemon._contain = never_census
    daemon._process_attempt(ATTEMPT)
    a = attempt(daemon)
    assert a["state"] == "finalizing" and a["rc"] == 0
    assert "attempt.finalizing" in [row["kind"] for row in daemon.store.list_events(JOB)]


def test_c4_2_uninspectable_guardian_decides_nothing_this_tick(daemon, monkeypatch):
    """C-4.2, C-5.5 a failed liveness inspection is not death: no census, no loss, no kill."""
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: "unknown")
    daemon._contain = never_census
    before = len(daemon.store.list_events(JOB))
    daemon._process_attempt(ATTEMPT)
    assert attempt(daemon)["state"] == "running"
    assert len(daemon.store.list_events(JOB)) == before


def test_c4_2_dead_guardian_without_receipt_runs_containment(daemon, monkeypatch):
    """C-4.2 only a dead guardian with no receipt reaches containment; an empty census is a loss."""
    with_launch(daemon, monkeypatch)
    monkeypatch.setattr(daemon_module.procs, "liveness", lambda *args: "dead")
    calls = []
    daemon._contain = lambda a: calls.append(1) or EMPTY
    daemon._process_attempt(ATTEMPT)
    assert calls and attempt(daemon)["state"] == "lost"
