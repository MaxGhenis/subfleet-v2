"""C-4.5, C-6.9, C-6.11, C-6.13, C-10.3: the daemon's own admission pass, uncapped and
placed by priority (Max, 2026-09-27: "we should uncap everything and instead use
prioritization").

`tests/unit/test_admission_uncapped.py` proves the decisions over a pure model of
a pass; these cases show the daemon takes them: its store, its liveness reads,
its machine reading and its registry reads, with fake providers and no process.
"""

from __future__ import annotations

import copy
import json
import os
import time

import pytest

from subfleet import daemon as daemon_module
from subfleet.contracts import Outcome, OutcomeClass
from subfleet.policy import MACHINE_GUARD_PROPOSAL
from tests.caps import capped
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401 (fixture)
from tests.fake_adapter import FakeAdapter

BUSY = {"load1": 150.0, "load5": 140.0, "cpus": 18, "memory_pressure": 1, "observed_at": 0.0}
QUIET = {"load1": 0.5, "load5": 0.5, "cpus": 18, "memory_pressure": 1, "observed_at": 0.0}


LIVE_SESSION = "0a1b2c3d-0000-4000-8000-00000000abcd"


def live_session(tmp_path, monkeypatch, *, pid=None, start=None):
    """A simulated live Claude Code session (C-6.9). A different recorded
    `start` models a reused pid; registry validation still checks the table."""
    directory = tmp_path / "claude" / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(daemon_module, "REGISTRY_READ_TTL_S", 0)
    pid = pid or os.getpid()
    actual_start = "fixture-session-start"
    table = daemon_module.procs.ProcessTable({pid: (1, pid, "S", actual_start)}, boot_id="unit-test-boot")
    monkeypatch.setattr(daemon_module.procs, "snapshot", lambda: table)
    (directory / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "sessionId": LIVE_SESSION, "entrypoint": "sdk-cli", "status": "idle",
        "statusUpdatedAt": 0, "procStart": start or actual_start}))


def submit(daemon, harness, name, **overrides):
    return daemon.dispatch("submit", harness.submit_args(request_id=name, **overrides))["job_id"]


def reservation_order(daemon) -> list[str]:
    return [row["job_id"] for row in daemon.store.query("SELECT job_id FROM attempts ORDER BY rowid")]


def test_c6_4_c6_9_one_pass_places_every_job_on_one_lane_by_class(state_daemon, tmp_path, monkeypatch):
    """Uncapped (the shipped policy), one unmeasured lane takes all five jobs in one
    pass, where the caps of before took one; jobs whose caller is live go first,
    then background work, oldest first within each."""
    daemon, harness = state_daemon
    background = [submit(daemon, harness, f"bg-{n}", caller_session="gone-session") for n in range(3)]
    live_session(tmp_path, monkeypatch)
    session = [submit(daemon, harness, f"live-{n}", caller_session=LIVE_SESSION.upper()) for n in range(2)]
    daemon._admit()
    assert [daemon.store.get_job(job)["state"] for job in background + session] == ["running"] * 5
    assert reservation_order(daemon) == session + background
    assert daemon._admission["pending"] == 0


def test_c6_9_caps_set_in_policy_still_hold_and_the_order_decides_who_waits(state_daemon, tmp_path, monkeypatch):
    """With the caps of before 2026-09-27, the lane takes one job: a live caller's,
    though a background job is older, and the others wait as they did."""
    daemon, harness = state_daemon
    capped(daemon.policy)
    live_session(tmp_path, monkeypatch)
    background = submit(daemon, harness, "bg", caller_session="gone-session")
    live = submit(daemon, harness, "live", caller_session=LIVE_SESSION)
    daemon._admit()
    assert reservation_order(daemon) == [live]
    assert daemon.store.get_job(background)["state"] in ("queued", "waiting")
    assert daemon._holds[background]["reason"] in ("no-slot", "behind-older-job")


def test_c6_13_the_guard_holds_background_work_at_the_door_and_lets_it_go(state_daemon, tmp_path, monkeypatch):
    """With the proposed guard on (off by default), at 8.3 load per CPU it holds
    background jobs (6.0) and places session jobs (10.0); held, a job records no
    decision and prepares no workspace; `why` says what held it; a quiet machine
    lets it go."""
    daemon, harness = state_daemon
    daemon.policy["admission"]["machine_guard"] = copy.deepcopy(MACHINE_GUARD_PROPOSAL)
    reading = dict(BUSY)
    monkeypatch.setattr("subfleet.machine.read", lambda: dict(reading))
    live_session(tmp_path, monkeypatch)
    background = submit(daemon, harness, "bg", caller_session="gone-session")
    live = submit(daemon, harness, "live", caller_session=LIVE_SESSION)
    daemon._admit()
    assert reservation_order(daemon) == [live]
    hold = daemon._holds[background]
    assert hold == {"reason": "machine-busy", "class": "background", "load_per_cpu": 8.33, "load_threshold": 6.0}
    assert not daemon.store.query("SELECT 1 FROM decisions WHERE job_id=?", (background,))
    assert daemon._admission["reasons"] == {"machine-busy": 1}
    why = daemon.dispatch("why", {"job_id": background})
    assert "the machine is saturated (load 8.33 per CPU, threshold 6.0)" in json.dumps(why)
    reading.update(QUIET)
    daemon._admit()
    assert reservation_order(daemon) == [live, background]


def test_c6_13_the_shipped_policy_holds_nothing_for_the_machine(state_daemon, monkeypatch):
    """Max, 2026-09-28: "remove *all* caps"; the guard is off by default."""
    daemon, harness = state_daemon
    monkeypatch.setattr("subfleet.machine.read", lambda: dict(BUSY, memory_pressure=4))
    assert daemon.policy["admission"]["machine_guard"] is None
    job = submit(daemon, harness, "bg", caller_session="gone-session")
    daemon._admit()
    assert reservation_order(daemon) == [job]


def _registry(tmp_path, monkeypatch, **fields):
    directory = tmp_path / "claude" / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(tmp_path / "claude"))
    now = time.time() * 1000
    (directory / f"{os.getpid()}.json").write_text(json.dumps({
        "pid": os.getpid(), "sessionId": "0a1b2c3d-0000-4000-8000-000000000001", "entrypoint": "claude-desktop",
        "status": "busy", "statusUpdatedAt": now, "updatedAt": now, "startedAt": now - 60_000, **fields}))


def _uses(daemon):
    """The `desktop.in_use` events, without the audit row every store mutation adds (C-3.2)."""
    rows = [json.loads(row["data_json"] or "{}") for row in daemon.store.query(
        "SELECT data_json FROM events WHERE kind='desktop.in_use' ORDER BY event_id")]
    return [row for row in rows if "in_use" in row]


def test_c10_3_the_desktop_signal_is_read_by_reads_and_recorded_only_by_admission(state_daemon, tmp_path,
                                                                                    monkeypatch):
    """A busy Claude app session makes the login in use; `lanes` and `status` read
    it and write nothing; the admission pass records the first answer and each
    change, once."""
    daemon, harness = state_daemon
    monkeypatch.setattr(daemon_module, "REGISTRY_READ_TTL_S", 0)
    _registry(tmp_path, monkeypatch)
    daemon.dispatch("lanes", {})
    daemon.dispatch("daemon.status", {})
    assert _uses(daemon) == []
    daemon._admit()
    daemon._admit()
    first, = _uses(daemon)
    assert first["in_use"] is True and first["was"] is None and first["busy"] == 1
    _registry(tmp_path, monkeypatch, status="idle", statusUpdatedAt=time.time() * 1000 - 40 * 60_000)
    daemon._admit()
    daemon._admit()
    assert [use["in_use"] for use in _uses(daemon)] == [True, False]
    assert _uses(daemon)[-1]["was"] is True
    # A headless run nobody owns may be on the desktop login: busy, it is use.
    _registry(tmp_path, monkeypatch, entrypoint="sdk-cli")
    daemon._admit()
    assert [use["in_use"] for use in _uses(daemon)] == [True, False, True]
    # The same run as a live attempt's (its session is the attempt's): never the login.
    job_id, attempt, _ = reserve(daemon, harness)
    daemon.store.query("SELECT 1")                  # the attempt is in flight
    with daemon.store.transaction("test.native_session", job_id=job_id) as tx:
        tx.execute("UPDATE attempts SET native_session_id=? WHERE attempt_id=?",
                   ("0A1B2C3D-0000-4000-8000-000000000001", attempt["attempt_id"]))
    daemon._admit()
    assert [use["in_use"] for use in _uses(daemon)] == [True, False, True, False]
    assert _uses(daemon)[-1]["subfleet"] == 1


def test_c10_3_an_unreadable_registry_is_use(state_daemon, tmp_path, monkeypatch):
    daemon, harness = state_daemon
    monkeypatch.setattr(daemon_module, "REGISTRY_READ_TTL_S", 0)
    monkeypatch.setattr(daemon_module.registry, "listing", lambda directory=None: None)
    assert daemon._desktop_in_use() is True


def test_c6_9_a_reused_pid_is_not_the_caller(state_daemon, tmp_path, monkeypatch):
    """A registry row whose pid now belongs to another process (a start that does
    not match) makes no caller live: its job is background, and the guard holds it."""
    daemon, harness = state_daemon
    live_session(tmp_path, monkeypatch, start="Thu Jan  1 00:00:00 1970")
    monkeypatch.setattr("subfleet.machine.read", lambda: dict(BUSY))
    daemon.policy["admission"]["machine_guard"] = copy.deepcopy(MACHINE_GUARD_PROPOSAL)
    job = submit(daemon, harness, "stale", caller_session=LIVE_SESSION)
    daemon._admit()
    assert daemon._holds[job]["class"] == "background"


class Limited(FakeAdapter):
    def classify(self, *args):
        return Outcome(OutcomeClass.LIMITED, "provider limit fixture")


def test_c4_5_c17_3_a_job_limited_on_every_lane_still_fails_with_rc_4(state_daemon, monkeypatch):
    """Uncapped, a limit still spends an attempt (C-4.5 unchanged; the uncap plan's
    revision 4 dropped the change that stopped counting it): a job limited on its
    two lanes with `max_attempts` 2 fails at once with rc 4 (C-17.3), rather than
    waiting out `max_wall_s` with every lane excluded (review of PR #72)."""
    from dataclasses import replace

    daemon, harness = state_daemon
    monkeypatch.setattr(daemon_module, "get_adapter", lambda _: Limited())
    lane = daemon.store.get_lane("codex-1")
    daemon.store.put_lane(replace(lane, lane_id="codex-2", account_key="codex:fake-2"))
    job_id, attempt, adir = reserve(daemon, harness, max_attempts=2)
    daemon._finalize(receipt_fixture(daemon, attempt, adir, rc=1))
    assert daemon.store.get_job(job_id)["state"] == "waiting"
    daemon._admit()
    second = daemon.store.list_attempts(job_id)[-1]
    assert second["seq"] == 2 and second["lane_id"] != attempt["lane_id"]
    daemon._pending_launches.discard(second["attempt_id"])
    second_dir = daemon.root / "jobs" / job_id / "a2"
    second_dir.mkdir(mode=0o700)
    daemon._finalize(receipt_fixture(daemon, second, second_dir, rc=1))
    job = daemon.store.get_job(job_id)
    assert job["state"] == "failed" and job["finished_at"]


def test_c10_3_c6_3_the_reservation_sees_claude_code_become_active(state_daemon, tmp_path, monkeypatch):
    """The review of the uncap plan: the check inside the reservation reused the early
    view's answer, so a job evaluated while the desktop login was idle was reserved on
    it after Claude Code had become active. The answer is read again, off the lock,
    before each reservation try, and a change judges the desktop lane again."""
    from subfleet.adapters.registry import register
    from subfleet.contracts import Credential, Lane, LaneOwner

    daemon, harness = state_daemon
    register("claude", FakeAdapter)
    monkeypatch.setattr(daemon_module, "REGISTRY_READ_TTL_S", 0)
    daemon.store.put_lane(Lane("claude-4", "claude", "claude:desk@example.invalid",
                               Credential("claude", "desk", "keychain-token"), None, LaneOwner.V2, True))
    idle = {"status": "idle", "statusUpdatedAt": time.time() * 1000 - 40 * 60_000}
    _registry(tmp_path, monkeypatch, **idle)
    first = submit(daemon, harness, "idle-desktop", pinned_model="opus")
    daemon._admit()
    assert [row["lane_id"] for row in daemon.store.list_attempts(first)] == ["claude-4"]   # idle: last, but a lane

    pick = daemon._pick

    def then_active(job, **options):
        decision = pick(job, **options)
        _registry(tmp_path, monkeypatch)                          # busy, from now on
        return decision
    monkeypatch.setattr(daemon, "_pick", then_active)
    second = submit(daemon, harness, "then-active", pinned_model="opus")
    daemon._admit()
    assert daemon.store.list_attempts(second) == []
    assert daemon._holds[second]["reason"] == "desktop"
    monkeypatch.setattr(daemon, "_pick", pick)
    _registry(tmp_path, monkeypatch, **idle)
    with daemon.store.transaction("test.clock", job_id=second) as tx:  # its recheck is due (C-6.10)
        tx.execute("UPDATE jobs SET next_check_at=? WHERE job_id=?", ("2000-01-01T00:00:00Z", second))
    daemon._admit()                                               # and idle again: placed
    assert [row["lane_id"] for row in daemon.store.list_attempts(second)] == ["claude-4"]


def test_c10_3_c11_4_a_probe_never_starts_on_a_desktop_login_that_became_busy(state_daemon, tmp_path,
                                                                            monkeypatch):
    """Review of PR #72: the admission-probe reservation read the early evaluation's
    desktop answer, so a `hard` job's probe could run a model turn on the desktop
    login that Claude Code began using after the evaluation. The answer is read
    again, off the lock, just before the probe's transaction."""
    from subfleet.adapters.registry import register
    from subfleet.contracts import Credential, Lane, LaneOwner

    daemon, harness = state_daemon
    register("claude", FakeAdapter)
    monkeypatch.setattr(daemon_module, "REGISTRY_READ_TTL_S", 0)
    daemon.store.put_lane(Lane("claude-4", "claude", "claude:desk@example.invalid",
                               Credential("claude", "desk", "keychain-token"), None, LaneOwner.V2, True))
    _registry(tmp_path, monkeypatch, status="idle", statusUpdatedAt=time.time() * 1000 - 40 * 60_000)
    probes = []
    monkeypatch.setattr(daemon, "_execute_probe", lambda job, lane, model, holder: probes.append(lane.lane_id)
                        or Outcome(OutcomeClass.OK, "admitted", {"rc": 0, "signal": None}))
    route = daemon._route

    def then_active(job, **options):
        decision = route(job, **options)
        _registry(tmp_path, monkeypatch)                          # busy, from now on
        return decision
    monkeypatch.setattr(daemon, "_route", then_active)
    job = submit(daemon, harness, "hard", pinned_model="opus", tier="hard")
    daemon._admit()
    assert probes == [] and daemon.store.list_attempts(job) == []
    assert not daemon.store.one("SELECT 1 FROM leases WHERE lease_key='lane:claude-4:slot:0'")
    # The answer the probe was refused by is on record (review of PR #72, round 2).
    assert [row["in_use"] for row in _uses(daemon)] == [False, True]


def test_c23_44_a_probe_never_starts_on_a_credential_found_revoked_after_the_evaluation(state_daemon,
                                                                                      monkeypatch):
    """Review of PR #72's plan: a timer's read that latches a lane's credential after
    the evaluation did not stop an admission probe from starting a model turn on it.
    The probe reservation reads the latch inside its transaction."""
    daemon, harness = state_daemon
    probes = []
    monkeypatch.setattr(daemon, "_execute_probe", lambda job, lane, model, holder: probes.append(lane.lane_id)
                        or Outcome(OutcomeClass.OK, "admitted", {"rc": 0, "signal": None}))
    route = daemon._route

    def then_revoked(job, **options):
        decision = route(job, **options)
        daemon.timers.metadata["codex-1"] = {"probe_status": "revoked", "verdict": "auth-revoked"}
        return decision
    monkeypatch.setattr(daemon, "_route", then_revoked)
    job = submit(daemon, harness, "hard", pinned_model="astra", tier="hard")
    daemon._admit()
    assert probes == [] and daemon.store.list_attempts(job) == []
    assert not daemon.store.one("SELECT 1 FROM leases WHERE lease_key='lane:codex-1:slot:0'")


def test_c10_3_a_slow_idle_read_never_replaces_a_newer_busy_one(state_daemon, monkeypatch):
    """Review of PR #72's plan (round 4): two reads of the registry at once, with the
    production cache lifetimes. The older one, idle, is held inside its read while a
    newer one reads busy and is kept; when the older one finishes it is not kept,
    and the answer a reservation reads stays busy, aged from the newer read."""
    import threading
    from subfleet.sessions import registry as registry_module

    daemon, harness = state_daemon
    now = time.time() * 1000
    idle = registry_module.SessionRow(session_id="s", pid=os.getpid(), socket=None, name=None, cwd=None,
                                      started_at=now, alive=True, socket_present=False, registry_path="x",
                                      entrypoint="claude-desktop", status="idle",
                                      status_updated_at=now - 40 * 60_000)
    busy = registry_module.SessionRow(**{**idle.__dict__, "status": "busy", "status_updated_at": now})
    entered, release = threading.Event(), threading.Event()
    calls = []

    def listing(directory=None):
        calls.append(None)
        if len(calls) == 1:                                        # the older read: idle, and slow
            entered.set()
            assert release.wait(10)
            return registry_module.Listing((idle,), ())
        return registry_module.Listing((busy,), ())
    monkeypatch.setattr(daemon_module.registry, "listing", listing)
    answers = []
    older = threading.Thread(target=lambda: answers.append(daemon._desktop_in_use()))
    older.start()
    assert entered.wait(10)
    assert daemon._desktop_in_use() is True                       # the newer read, kept
    kept = daemon._registry[0]
    release.set()
    older.join(10)
    assert answers == [True]                                       # the older read yields to the newer
    assert daemon._registry[0] == kept and daemon._registry[1]["in_use"] is True
    # The rows the priority classes read (C-6.9) are the newer read's too.
    assert [row.status for row in daemon._registry[1]["found"]["rows"]] == ["busy"]
    assert daemon._desktop_answer() is True
    # An answer older than the bound is use, whatever it said.
    aged = time.monotonic() - daemon_module.DESKTOP_IN_USE_MAX_AGE_S - 1
    daemon._registry = (aged, {"found": {"rows": [], "unreadable": [], "groups": {}}, "in_use": False,
                               "evidence": {}, "finished": aged})
    assert daemon._desktop_answer() is True


def test_c10_3_a_slow_answer_never_replaces_a_newer_one_after_the_registry_read(state_daemon, monkeypatch):
    """The same race one step later: the older refresh has its rows (idle) and is
    held while it judges them; a newer refresh reads busy rows and is kept. The
    older answer, observed earlier, is not kept."""
    import threading
    from subfleet.sessions import registry as registry_module

    daemon, harness = state_daemon
    monkeypatch.setattr(daemon_module, "REGISTRY_READ_TTL_S", 0)        # each refresh reads the registry
    now = time.time() * 1000
    idle = registry_module.SessionRow(session_id="s", pid=os.getpid(), socket=None, name=None, cwd=None,
                                      started_at=now, alive=True, socket_present=False, registry_path="x",
                                      entrypoint="claude-desktop", status="idle",
                                      status_updated_at=now - 40 * 60_000)
    busy = registry_module.SessionRow(**{**idle.__dict__, "status": "busy", "status_updated_at": now})
    reads = []
    monkeypatch.setattr(daemon_module.registry, "listing",
                        lambda directory=None: reads.append(None) or registry_module.Listing(
                            (idle,) if len(reads) == 1 else (busy,), ()))
    entered, release = threading.Event(), threading.Event()
    judged = daemon._subfleet_processes

    def slow_first(found):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return judged(found)
    monkeypatch.setattr(daemon, "_subfleet_processes", slow_first)
    answers = []
    older = threading.Thread(target=lambda: answers.append(daemon._desktop_in_use()))
    older.start()
    assert entered.wait(10)
    assert daemon._desktop_in_use() is True                          # TTL 0: the newer read, kept
    release.set()
    older.join(10)
    assert answers == [True] and daemon._registry[1]["in_use"] is True


@pytest.mark.parametrize("clock", ["due", "running"])
def test_c6_13_a_workspace_retry_is_held_by_the_guard_before_its_git(state_daemon, monkeypatch, clock):
    """Review of PR #72: a background job waiting on a transient workspace failure
    (C-6.8) skipped the guard when its retry came due and prepared its workspace
    at load 150. Due, it is held `machine-busy` before any git; while its clock
    runs it still reports `workspace` (C-6.11)."""
    daemon, harness = state_daemon
    daemon.policy["admission"]["machine_guard"] = copy.deepcopy(MACHINE_GUARD_PROPOSAL)
    monkeypatch.setattr("subfleet.machine.read", lambda: dict(BUSY))
    job = submit(daemon, harness, "retry", caller_session="gone-session")
    when = "2000-01-01T00:00:00Z" if clock == "due" else daemon_module.after(300)
    with daemon.store.transaction("test.workspace_wait", job_id=job) as tx:
        tx.execute("UPDATE jobs SET state='waiting',wait_reason='workspace',next_check_at=? WHERE job_id=?",
                   (when, job))
    prepared = []
    workspace = daemon._workspace
    monkeypatch.setattr(daemon, "_workspace", lambda row: prepared.append(row["job_id"]) or workspace(row))
    daemon._admit()
    assert prepared == [] and daemon.store.list_attempts(job) == []
    assert daemon._holds[job]["reason"] == ("machine-busy" if clock == "due" else "workspace")


def test_c10_3_a_newer_unreadable_registry_supersedes_an_older_idle_read(state_daemon, monkeypatch):
    """Review of PR #72 (PR gate, round 2): an older refresh reads an empty registry
    and is held; a newer read (priority discovery) finds the registry unreadable,
    which is use. The older refresh then yields to it: the answer is use."""
    import threading
    from subfleet.sessions import registry as registry_module

    daemon, harness = state_daemon
    entered, release, calls = threading.Event(), threading.Event(), []

    def listing(directory=None):
        calls.append(None)
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
            return registry_module.Listing()                        # empty: idle
        return None                                                   # unreadable
    monkeypatch.setattr(daemon_module.registry, "listing", listing)
    answers = []
    older = threading.Thread(target=lambda: answers.append(daemon._desktop_in_use()))
    older.start()
    assert entered.wait(10)
    assert daemon._session_rows() is None                            # the newer read, kept
    release.set()
    older.join(10)
    assert answers == [True] and daemon._desktop_answer() is True



def test_c10_3_a_newer_unreadable_read_supersedes_an_older_answer_being_judged(state_daemon, monkeypatch):
    """Review of PR #72 (fresh PR gate, round 1): an older read of an empty registry
    is held while it judges its rows; a newer read (priority discovery) finds the
    registry unreadable. With one cache, the older read yields to the newer one,
    rows and answer together: the answer is use."""
    import threading
    from subfleet.sessions import registry as registry_module

    daemon, harness = state_daemon
    monkeypatch.setattr(daemon_module, "REGISTRY_READ_TTL_S", 0)
    now = time.time() * 1000
    idle = registry_module.SessionRow(session_id="s", pid=os.getpid(), socket=None, name=None, cwd=None,
                                      started_at=now, alive=True, socket_present=False, registry_path="x",
                                      entrypoint="claude-desktop", status="idle",
                                      status_updated_at=now - 40 * 60_000)
    reads = []
    monkeypatch.setattr(daemon_module.registry, "listing", lambda directory=None: reads.append(None) or (
        registry_module.Listing((idle,), ()) if len(reads) == 1 else None))
    entered, release = threading.Event(), threading.Event()
    judged = daemon._subfleet_processes

    def slow_first(found):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return judged(found)
    monkeypatch.setattr(daemon, "_subfleet_processes", slow_first)
    answers = []
    older = threading.Thread(target=lambda: answers.append(daemon._desktop_in_use()))
    older.start()
    assert entered.wait(10)
    assert daemon._session_rows() is None                             # the newer read: unreadable
    release.set()
    older.join(10)
    assert answers == [True] and daemon._desktop_answer() is True and daemon._session_rows() is None



def _resume(daemon, harness, source_id, caller):
    from subfleet import protocol
    return daemon.submit(protocol.SubmitArgs(**harness.submit_args(
        kind="resume", parent_job_id=source_id, caller_session=caller)))["job_id"]


@pytest.mark.parametrize("policy", ["uncapped", "capped", "cap-1"])
@pytest.mark.parametrize("waiter_due", [True, False])
def test_c6_9_a_retry_never_waits_behind_a_job_waiting_for_its_own_lease(state_daemon, monkeypatch,
                                                                        policy, waiter_due):
    """Review of PR #72 (Opus peer, two lenses): two read-only resumes of one native
    session. The first is placed and holds the session's leases; its attempt ends
    transient, so it waits to retry and keeps them. The second, whose caller is
    live, is ordered first (`session` before `background`) and waits for those
    leases. The first is placed on the next pass: it is never queued (lease FIFO)
    or held behind (C-6.9, capped) a job waiting for a lease it holds itself."""
    from subfleet import scheduler
    from subfleet.daemon import native_session_lease_key
    from tests.fake.test_resume_contract import finish_reserved, finished_source, measured_lane

    daemon, harness = state_daemon
    if policy != "uncapped":
        capped(daemon.policy)
    measured_lane(daemon)
    source_id, source_attempt = finished_source(daemon, harness)
    first = _resume(daemon, harness, source_id, "gone-session")
    daemon._admit()
    assert daemon.store.get_job(first)["state"] == "running"
    second = _resume(daemon, harness, source_id, "live-session")
    real = daemon._liveness
    monkeypatch.setattr(daemon, "_liveness", lambda jobs: scheduler.Liveness(
        sessions=(real(jobs) or scheduler.Liveness(frozenset(), frozenset())).sessions | {"live-session"},
        jobs=frozenset()))
    daemon._admit()
    assert daemon._holds[second]["reason"] == "lease-held"                 # waits for the first's leases
    monkeypatch.setattr(FakeAdapter, "classify", lambda self, adir, launch, exit_info: Outcome(
        OutcomeClass.TRANSIENT, "fixture transient", evidence={"rc": exit_info.rc}))
    finish_reserved(daemon, first)
    key = native_session_lease_key(source_attempt["lane_id"], "native-source-session")
    assert daemon.store.get_job(first)["state"] == "waiting"
    assert daemon.store.one("SELECT holder FROM leases WHERE lease_key=?", (key,))["holder"] == first
    if policy == "cap-1":
        # One slot, none in use: the last slot C-6.9 keeps for an older waiter is
        # never kept for one waiting for this job's own lease (review of PR #72,
        # round 2).
        daemon.policy["caps"]["max_active_attempts"] = 1
    daemon.store.update_job(first, next_check_at=None)
    if waiter_due:
        daemon.store.update_job(second, next_check_at=None)
    daemon._admit()
    assert [a["seq"] for a in daemon.store.list_attempts(first)] == [1, 2], daemon._holds.get(first)
    assert daemon.store.list_attempts(second) == []                        # still waiting its turn


def test_c6_9_an_unlistable_registry_is_unknown_liveness(state_daemon, monkeypatch):
    """Review of PR #72: a registry that exists and cannot be listed says nothing
    about who is live. Every detached job is then `session` (C-6.9), not each live
    caller's job `background`."""
    from subfleet import scheduler

    daemon, harness = state_daemon
    monkeypatch.setattr(daemon_module, "REGISTRY_READ_TTL_S", 0)
    monkeypatch.setattr(daemon_module.registry, "listing", lambda directory=None: None)
    job = {"job_id": "j", "kind": "dispatch", "caller_session": LIVE_SESSION, "parent_job_id": None}
    assert daemon._liveness([job]) is None
    assert scheduler.priority_class(job, daemon._liveness([job])) == "session"


def test_c3_7_a_read_op_never_waits_for_the_desktop_event_write(state_daemon, monkeypatch):
    """Review of PR #72: recording a changed answer writes an event, which waits for
    any store writer. A read op publishing its own registry read meanwhile does not
    wait for that write."""
    import threading

    daemon, harness = state_daemon
    monkeypatch.setattr(daemon_module, "REGISTRY_READ_TTL_S", 0)
    entered, release = threading.Event(), threading.Event()
    real_add = daemon.store.add_event

    def slow_add(kind, *args, **kwargs):
        if kind == "desktop.in_use":
            entered.set()
            assert release.wait(10)
        return real_add(kind, *args, **kwargs)
    monkeypatch.setattr(daemon.store, "add_event", slow_add)
    from subfleet.sessions import registry as registry_module
    monkeypatch.setattr(daemon_module.registry, "listing", lambda directory=None: registry_module.Listing())
    daemon._desktop_in_use()
    recorder = threading.Thread(target=daemon._record_desktop_use)
    recorder.start()
    try:
        assert entered.wait(10)
        answered = []
        reader = threading.Thread(target=lambda: answered.append(daemon._desktop_in_use()))
        reader.start()
        reader.join(5)
        assert answered, "a read op waited for the desktop.in_use event write"
    finally:
        release.set()
        recorder.join(10)


def test_c10_3_a_slow_registry_read_is_reused(state_daemon, monkeypatch):
    """Review of PR #72: a read that takes longer than the cache lifetime is reused
    for that lifetime after it finished, not read again at every call; it is still
    aged from when it began."""
    from subfleet.sessions import registry as registry_module

    daemon, harness = state_daemon
    monkeypatch.setattr(daemon_module, "REGISTRY_READ_TTL_S", 0.2)
    calls = []

    def slow_listing(directory=None):
        calls.append(None)
        time.sleep(0.3)
        return registry_module.Listing()
    monkeypatch.setattr(daemon_module.registry, "listing", slow_listing)
    began = time.monotonic()
    daemon._desktop_in_use()
    daemon._desktop_in_use()
    daemon._session_rows()
    assert len(calls) == 1
    assert daemon._registry[0] <= began + 0.05                               # aged from its start


def test_c10_3_the_answer_a_reservation_places_by_is_recorded(state_daemon, monkeypatch):
    """Review of PR #72: the answer changes between the pass's first read and the
    reservation's refresh. The change the reservation places by is recorded as a
    `desktop.in_use` event in that pass, not left to a later pass that may read it
    changed back."""
    from subfleet.sessions import registry as registry_module

    daemon, harness = state_daemon
    monkeypatch.setattr(daemon_module, "REGISTRY_READ_TTL_S", 0)
    now = time.time() * 1000
    busy = registry_module.SessionRow(session_id="s", pid=os.getpid(), socket=None, name=None, cwd=None,
                                      started_at=now, alive=True, socket_present=False, registry_path="x",
                                      entrypoint="claude-desktop", status="busy", status_updated_at=now)
    reads = []
    monkeypatch.setattr(daemon_module.registry, "listing", lambda directory=None: reads.append(None) or (
        registry_module.Listing() if len(reads) == 1 else registry_module.Listing((busy,), ())))
    monkeypatch.setattr(daemon_module.registry, "validated", lambda found, starts: list(found))
    submit(daemon, harness, "one")
    daemon._admit()
    assert [row["in_use"] for row in _uses(daemon)] == [False, True]


def test_c10_3_a_record_is_never_older_than_the_one_before_it(state_daemon):
    """Review of PR #72 (round 2): the turn pass and the detached pass both record.
    A record that waited for the other is made from the reading current when it
    runs, never from one read before it waited: no stale flip is written."""
    import threading

    daemon, harness = state_daemon
    reading = lambda in_use: {"found": {"rows": [], "unreadable": [], "groups": {}}, "in_use": in_use,
                              "evidence": {}, "finished": time.monotonic()}
    daemon._registry = (time.monotonic(), reading(True))
    daemon._desktop_use_recorded = True
    daemon._desktop_record_lock.acquire()
    recorder = threading.Thread(target=daemon._record_desktop_use)       # the turn pass
    try:
        recorder.start()
        time.sleep(0.3)                                                    # waiting for the lock
        daemon._registry = (time.monotonic(), reading(False))              # the detached pass: newer,
        daemon._desktop_use_recorded = False                               # already recorded
    finally:
        daemon._desktop_record_lock.release()
    recorder.join(10)
    assert [row["in_use"] for row in _uses(daemon)] == []
