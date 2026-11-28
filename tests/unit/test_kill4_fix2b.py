"""D-KILL4 fix round 2b (review r2 of #250, P3-1 to P3-3) and D-QUAR-START1 on the
simulated host (`tests/kill4_world.py`).

The daemon's census, kill protocol, start grace, finalization and kill request run
unchanged; only the host is simulated. No real process is inspected or signalled.

- P3-1 (decision 1): a contained start grace has one end, unlaunched, whether or not a
  process sits in the job's worktree.
- P3-2 (decision 2): `kill` on a finished job whose census holds only members no source
  may signal runs no protocol.
- P3-3 (decision 3): a later attribution supersedes an earlier hold.
- D-QUAR-START1 (a): a live, running, verified guardian gets more time, within
  `START_GRACE_MAX_S`; (b): the abandon handshake on every path that ends a `starting`
  attempt without a `start.json`.
"""

from __future__ import annotations

import dataclasses
import json
import signal
import time

from hypothesis import HealthCheck, example, given, settings, strategies as st
import pytest

from subfleet import daemon as daemon_module, folders, procs, protocol
from subfleet.contracts import START_GRACE_S, attempt_dir
from subfleet.guardian import atomic_publish
from tests.kill4_world import World  # noqa: F401
from tests.unit.test_daemon_settle import ATTEMPT, JOB, STARTED, attempt, daemon  # noqa: F401
from tests.unit.test_kill4_terminal_containment import kinds, owned_worktree, world  # noqa: F401

TERM, KILL, CONT = int(signal.SIGTERM), int(signal.SIGKILL), int(signal.SIGCONT)
MAX_S = 6 * START_GRACE_S          # START_GRACE_MAX_S, spelled out so the red run can import this file


def adir(daemon):
    return attempt_dir(daemon.root, JOB, 1)


def inline(daemon, monkeypatch) -> None:
    monkeypatch.setattr(daemon, "_schedule", lambda key, fn, *args, **kw: fn(*args))


def starting(daemon, *, expired_s: float = 1e6) -> None:
    """A `starting` attempt (no start.json) whose start grace expired `expired_s` ago."""
    daemon.store.update_attempt(ATTEMPT, state="starting", child_pid=None)
    daemon.start_grace_s = START_GRACE_S
    daemon._starting_deadlines[ATTEMPT] = time.monotonic() - expired_s


def settle(daemon, passes: int = 6) -> dict:
    """Pass the attempt until it ends; each pass is due, and a cwd wait has run out."""
    for _ in range(passes):
        a = attempt(daemon)
        if a["state"] not in ("starting", "running", "finalizing"):
            break
        daemon._inspect_next.pop(ATTEMPT, None)
        daemon.__dict__.setdefault("_cwd_wait_next", {}).pop(ATTEMPT, None)
        settle_clock = daemon.__dict__.setdefault("_cwd_settle", {})
        if ATTEMPT in settle_clock:
            settle_clock[ATTEMPT] -= daemon_module.CWD_SETTLE_S + 1
        daemon._process_attempt(ATTEMPT) if a["state"] != "finalizing" else daemon._finalize(a)
    return attempt(daemon)


def abandon_seen_at_each_signal(daemon, world, monkeypatch) -> list[bool]:
    """Whether `abandon.json` was in the attempt folder when each signal was sent."""
    seen: list[bool] = []
    single, group = world.signal_process, world.signal_group

    def one(recorded, sig, **kwargs):
        seen.append((adir(daemon) / "abandon.json").exists())
        return single(recorded, sig, **kwargs)

    def many(pgid, sig, **kwargs):
        seen.append((adir(daemon) / "abandon.json").exists())
        return group(pgid, sig, **kwargs)
    monkeypatch.setattr(procs, "signal_process", one)
    monkeypatch.setattr(procs, "signal_group", many)
    return seen


def publish(daemon, name: str, value) -> None:
    atomic_publish(adir(daemon) / name, json.dumps(value).encode())


def abandoned_receipt(reason: str = "starting-no-receipt") -> dict:
    return {"rc": 127, "signal": None, "finished_at": "2026-09-05T14:00:01Z", "wall_s": 0, "child_pid": None,
            "spawn_error": f"abandoned before launch: {reason}",
            "abandoned": {"at": "2026-09-05T14:00:01Z", "reason": reason, "stage": "spawn"}}


START = {"guardian_pid": 4242, "pgid": 4242, "boot_id": "boot", "proc_start": STARTED,
         "started_at": "2026-09-05T14:00:00Z"}


# Decision 1 (P3-1): one end for a contained start grace.

@pytest.mark.parametrize("operator", [False, True], ids=["no-cwd-holder", "operator-shell"])
def test_a_contained_start_grace_has_one_end_whether_or_not_a_cwd_holder_is_present(
        daemon, world, monkeypatch, operator):
    """Review r2's P3-1 input: a writable owned worktree, `max_attempts` 2, the guardian
    stopped before its start.json. With an operator's shell in the worktree it ended
    `failed: starting-no-receipt` and queued a retry; without one, `lost` and no retry."""
    tree = owned_worktree(daemon, monkeypatch, world)
    key = folders.exclusive_key(tree)
    daemon.store.acquire_lease(key, JOB)
    guardian = world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, term_handler=True,
                         stopped=True)
    shell = world.add(6000, 1, 6000, cwd=str(tree)) if operator else None
    starting(daemon)
    daemon.store.update_job(JOB, max_attempts=2)
    a = settle(daemon)
    job = daemon.store.get_job(JOB)
    assert not guardian.alive and guardian.ended_by == KILL
    assert (a["state"], a["outcome_detail"], a["outcome_class"]) == ("failed", "starting-no-receipt", "unknown"), a
    assert job["state"] == "queued" and job["rc"] is None, job          # nothing launched: it retries (C-4.5)
    assert a["killed_by"] == "recovery" and not a["quarantine_reason"]
    kept = daemon.store.one("SELECT holder FROM leases WHERE lease_key=?", (key,))
    if shell is not None:
        # Queued again, the job is still the writer there and keeps its own key while a
        # process sits in it (C-5.9); with none, the key goes and its retry takes it again.
        assert kept and kept["holder"] in (JOB, ATTEMPT), kept
        assert shell.alive and shell.pid not in world.signalled()
    else:
        assert kept is None, kept
    if shell is not None:
        assert json.loads(a["evidence_json"])["cwd_holders"] == [f"pid {shell.pid}: cwd in worktree"]
    assert world.signals[0] == ("group", 4242, KILL), world.signals


# Decision 2 (P3-2): a kill with nothing it may signal schedules no protocol.

def test_kill_force_release_kill_kill_on_a_held_only_census_runs_no_protocol(daemon, world, monkeypatch):
    """Review r2's executed sequence on a succeeded job: a stranger's leaderless group
    at the recorded pgid (its leader long gone). Each `kill` ran a full protocol
    (a TERM grace plus the settle window) that could signal nothing."""
    member = world.add(5600, 1, 4242)                           # the leaderless group's member
    daemon.store.update_attempt(ATTEMPT, state="succeeded")
    daemon.store.update_job(JOB, state="succeeded")
    daemon.term_grace_s, daemon.kill_settle_s = 2.0, 1.0
    scheduled = []
    monkeypatch.setattr(daemon, "_schedule", lambda key, fn, *args, **kw: (scheduled.append(key), fn(*args)))
    began = time.monotonic()
    answers = [daemon.kill(protocol.KillArgs(job_id=JOB)),
               daemon.kill(protocol.KillArgs(job_id=JOB, force_release=True)),
               daemon.kill(protocol.KillArgs(job_id=JOB)),
               daemon.kill(protocol.KillArgs(job_id=JOB))]
    assert time.monotonic() - began < 1.0, "a TERM grace was spent"
    assert [answer["status"] for answer in answers] == ["already finished"] * 4, answers
    for answer in (answers[0], answers[2], answers[3]):
        assert f"pid {member.pid}" in answer["detail"] and "held, never signalled" in answer["detail"], answer
    assert not scheduled, scheduled
    assert not world.signals and member.alive
    evidence = json.loads(attempt(daemon)["evidence_json"] or "{}")
    assert "terminal_containment" not in evidence and "recontainment" not in evidence, evidence
    assert "attempt.recontainment_started" not in kinds(daemon)
    assert attempt(daemon)["state"] == "succeeded"


def test_kill_on_a_finished_job_with_a_signallable_member_still_contains_it(daemon, world, monkeypatch):
    """The other side of decision 2: a member a source attributes is still contained."""
    lockf = world.add(4401, 1, 4401, marked=True, hidden=True)
    daemon.store.update_attempt(ATTEMPT, state="succeeded", evidence_json=json.dumps({"counted_identities": {
        "4401": {"pid": 4401, "boot_id": "boot", "proc_start": lockf.start}}}))
    daemon.store.update_job(JOB, state="succeeded")
    inline(daemon, monkeypatch)
    answer = daemon.kill(protocol.KillArgs(job_id=JOB))
    assert answer["status"] == "containment requested" and not lockf.alive


# Decision 3 (P3-3): a later attribution supersedes an earlier hold.

def test_a_pid_held_by_one_census_is_signalled_when_a_later_census_attributes_it(daemon, world, monkeypatch):
    """Review r2's counterexample: pid 5000 is another user's, in the guardian's group,
    with its cwd in the worktree; its child 5001 has its own pgid, no marker, its cwd
    elsewhere, and respawns marked children. Census 1 holds 5001 (the parent of a
    marked child, outside the worktree) and counts 5000; the group's TERM cannot reach
    5000 (EPERM); census 2's walk from the counted 5000 reaches 5001, which is TERMed
    by lineage. A hold never authorizes a signal, and it vetoes none either (C-5.5)."""
    tree = owned_worktree(daemon, monkeypatch, world)
    world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, term_handler=True, exits_childless=True)
    world.add(4243, 4242, 4242, start=STARTED, marked=True)
    other = world.add(5000, 1, 4242, cwd=str(tree), uid=0)         # reparented: no walk reaches it
    child = world.add(5001, 5000, 5001, cwd="/elsewhere", respawns=True)
    publish(daemon, "exit.json", {"rc": 0, "signal": None, "finished_at": "2026-09-05T14:01:00Z", "wall_s": 1,
                                  "child_pid": 4243})
    # Inspection recorded the provider as owned while the guardian led its group, as the
    # property's world does; without it the child pid is an identity-less seed, census 1
    # is unverifiable, and its taint (C-5.4), not a hold, keeps 5001 from the walk.
    daemon.store.update_attempt(ATTEMPT, state="finalizing", rc=0, evidence_json=json.dumps(
        {"owned_identities": {"4243": {"pid": 4243, "boot_id": "boot", "proc_start": STARTED}}}))
    censuses: list[procs.Containment] = []
    contain = daemon._contain

    def recorded(a):
        censuses.append(contain(a))
        return censuses[-1]
    daemon._contain = recorded
    sent: list[tuple[int, int]] = []
    single = world.signal_process

    def one(recorded_identity, sig, **kwargs):
        sent.append((recorded_identity.pid, len(censuses) - 1))
        return single(recorded_identity, sig, **kwargs)
    monkeypatch.setattr(procs, "signal_process", one)
    daemon._finalize(attempt(daemon))
    held_by = [i for i, census in enumerate(censuses) if child.pid in census.held_pids]
    signalled_from = [i for pid, i in sent if pid == child.pid]
    assert held_by, [c.to_dict() for c in censuses]
    assert signalled_from, (sent, world.signals)
    # Never signalled from a census that held it.
    for pid, i in sent:
        assert pid not in censuses[i].held_pids, (pid, i, censuses[i].to_dict())
    assert other.alive                                            # another user's: EPERM, never reached


# D-QUAR-START1 (a): a live, running guardian gets more time, within the bound.

def test_a_running_guardian_alone_past_start_grace_is_given_more_time_and_then_runs(daemon, world, monkeypatch):
    guardian = world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, term_handler=True)
    starting(daemon, expired_s=1)                       # 1 s past the grace, 49 s before the bound
    daemon._process_attempt(ATTEMPT)
    assert not world.signals and guardian.alive
    assert attempt(daemon)["state"] == "starting"
    assert not (adir(daemon) / "abandon.json").exists()
    assert "attempt.start_extended" in kinds(daemon)
    publish(daemon, "start.json", START)                 # its start.json lands within the bound
    daemon._inspect_next.pop(ATTEMPT, None)
    daemon._process_attempt(ATTEMPT)
    assert attempt(daemon)["state"] == "running" and not world.signals


@pytest.mark.parametrize("why", ["stopped", "past-bound", "unverifiable", "another-member", "reborn"])
def test_start_grace_extends_for_no_guardian_but_a_live_verified_running_one(daemon, world, monkeypatch, why):
    # "reborn": pid 4242 is a stranger's now, born later and without the attempt's markers.
    guardian = world.add(4242, 1, 4242, start=STARTED if why != "reborn" else None, marked=why != "reborn",
                         ignores_term=True, term_handler=True, stopped=why == "stopped")
    if why == "another-member":
        world.add(4250, 4242, 4242, marked=True)
    starting(daemon, expired_s=MAX_S - START_GRACE_S + 1 if why == "past-bound" else 1)
    if why == "unverifiable":
        contain = daemon._contain
        first = []

        def unverifiable(a):
            census = contain(a)
            if not first:
                first.append(census)
                return dataclasses.replace(census, unverifiable=True, errors=("injected",))
            return census
        daemon._contain = unverifiable
    seen = abandon_seen_at_each_signal(daemon, world, monkeypatch)
    a = settle(daemon)
    assert "attempt.start_extended" not in kinds(daemon)
    assert (adir(daemon) / "abandon.json").exists()
    if why == "reborn":
        # The pid is a stranger's now: not the attempt's guardian, so never signalled.
        assert guardian.alive and 4242 not in world.signalled()
        assert (a["state"], a["outcome_detail"]) == ("failed", "starting-no-receipt"), a
        return
    assert seen and all(seen), seen                      # the abandon precedes every signal
    assert not guardian.alive and guardian.ended_by == KILL
    assert world.signals[0] == ("group", 4242, KILL), world.signals
    assert (a["state"], a["outcome_detail"]) == ("failed", "starting-no-receipt"), a


@settings(max_examples=60, deadline=None, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(stopped=st.booleans(), unverifiable=st.booleans(), member=st.booleans(),
       elapsed=st.floats(0, 2 * MAX_S, allow_nan=False))
# Review r3 of #250, P3-3: the bound itself, just inside it, at it and just past it.
@example(stopped=False, unverifiable=False, member=False, elapsed=MAX_S - 1e-6)
@example(stopped=False, unverifiable=False, member=False, elapsed=MAX_S)
@example(stopped=False, unverifiable=False, member=False, elapsed=MAX_S + 1e-6)
def test_extension_is_granted_exactly_while_every_condition_holds_and_never_past_the_bound(
        daemon, monkeypatch, stopped, unverifiable, member, elapsed):
    """Invariant 3: no extension passes START_GRACE_MAX_S, and a stopped guardian or an
    unverifiable census never extends. `elapsed` is the time since the deadline's start
    (here the launch), on a fake clock, so an example can land on the bound exactly: on a
    real clock, which never reads 60.0, a `>` for the `>=` and a bound counted from the
    deadline instead of its start (70 s) both survived (review r3 of #250, P3-3)."""
    world = World(ATTEMPT, str(daemon.root))
    with pytest.MonkeyPatch.context() as patch:
        world.install(patch)
        world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, term_handler=True,
                  stopped=stopped)
        if member:
            world.add(4250, 4242, 4242, marked=True)
        daemon.store.update_attempt(ATTEMPT, state="starting", child_pid=None)
        daemon.start_grace_s = START_GRACE_S
        launch = 1000.0                          # the deadline's start
        daemon._starting_deadlines[ATTEMPT] = launch + START_GRACE_S
        daemon._inspect_next.pop(ATTEMPT, None)
        census = procs.Containment(group_pids=frozenset({4242}), identities={4242: world.identity(4242)},
                                   shapes={4242: {"ppid": 1, "pgid": 4242, "stat": "T" if stopped else "S"}})
        if member:
            census = dataclasses.replace(census, group_pids=frozenset({4242, 4250}))
        if unverifiable:
            census = dataclasses.replace(census, unverifiable=True, errors=("injected",))
        now = launch + elapsed
        patch.setattr(daemon_module.time, "monotonic", lambda: now)
        extends = daemon._start_extends(attempt(daemon), census, launch + START_GRACE_S)
        # The clock's own reading decides, so a sum that rounds onto the bound is at it.
        expected = not stopped and not unverifiable and not member and now < launch + MAX_S
        assert extends == expected, (stopped, unverifiable, member, elapsed)


# D-QUAR-START1 (b): the abandon handshake.

@pytest.mark.parametrize("path", ["start-grace", "start-grace-empty", "cancel", "wall"])
def test_every_ending_of_a_starting_attempt_writes_the_abandon_before_any_signal_or_end(
        daemon, world, monkeypatch, path):
    if path != "start-grace-empty":
        world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, term_handler=True,
                  stopped=path == "start-grace")
    starting(daemon, expired_s=1e6 if path.startswith("start-grace") else -1e6)
    if path in ("cancel", "wall"):
        daemon.store.update_job(JOB, cancel_requested_at=daemon_module.utcnow())
        if path == "wall":
            daemon.store.update_attempt(ATTEMPT, killed_by="max_wall_s")
            monkeypatch.setattr(daemon, "_cancel_info", lambda job_id: {"by": "daemon", "reason": "max_wall_s"})
    ended = []
    unlaunched = daemon._unlaunched

    def watched(a, detail, **kwargs):
        ended.append((adir(daemon) / "abandon.json").exists())
        return unlaunched(a, detail, **kwargs)
    monkeypatch.setattr(daemon, "_unlaunched", watched)
    seen = abandon_seen_at_each_signal(daemon, world, monkeypatch)
    a = settle(daemon)
    marker = json.loads((adir(daemon) / "abandon.json").read_text())
    assert set(marker) == {"at", "reason"} and marker["at"].endswith("Z"), marker
    assert ended == [True], ended
    assert all(seen), seen
    if path in ("cancel", "wall"):
        # Nothing launched: the group is KILLed at once, never TERMed, and the attempt ends unlaunched.
        assert world.signals[0] == ("group", 4242, KILL), world.signals
        assert not [s for s in world.signals if s[2] in (TERM, CONT)], world.signals
        assert (a["state"], a["outcome_detail"]) == ("interrupted", "cancelled-before-launch"), a
        assert marker["reason"] == "cancelled-before-launch"
        assert daemon.store.get_job(JOB)["state"] == "cancelled"
    else:
        assert (a["state"], a["outcome_detail"]) == ("failed", "starting-no-receipt"), a
        assert marker["reason"] == "starting-no-receipt"


def test_a_start_json_found_after_the_abandon_is_contained_as_possibly_launched(daemon, world, monkeypatch):
    """The guardian wrote start.json, and spawned, between start grace's census and its
    abandon: the last read finds it, so the attempt is treated as launched. The full
    protocol runs (the group's TERM first, never the unlaunched KILL), and with no exit
    receipt it is a loss, never an unlaunched end that would release a retry."""
    guardian = world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, term_handler=True)
    starting(daemon)
    publishing = daemon._publish

    def racing(role, path, contents):
        if role == "abandon":
            publish(daemon, "start.json", START)
            world.add(4243, 4242, 4242, start=STARTED, marked=True)       # its provider
        return publishing(role, path, contents)
    daemon._publish = racing
    daemon.store.update_job(JOB, max_attempts=1)
    a = settle(daemon)
    assert world.signals[0] == ("group", 4242, TERM), world.signals
    assert world.procs[4243].ended_by == TERM and guardian.ended_by == KILL       # it ignores TERM by design
    assert a["state"] == "lost" and a["outcome_detail"] != "starting-no-receipt", a


@pytest.mark.parametrize("where", ["tick", "finalize", "kill-protocol"])
def test_an_abandoned_receipt_reads_as_unlaunched_never_as_a_provider_result(daemon, world, monkeypatch, where):
    with_receipt = abandoned_receipt()
    if where == "tick":
        starting(daemon)
        publish(daemon, "abandon.json", {"at": "2026-09-05T14:00:00Z", "reason": "starting-no-receipt"})
        publish(daemon, "start.json", START)
        publish(daemon, "exit.json", with_receipt)
        daemon._process_attempt(ATTEMPT)
    elif where == "finalize":
        publish(daemon, "exit.json", with_receipt)
        daemon.store.update_attempt(ATTEMPT, state="finalizing", rc=127)
        daemon._finalize(attempt(daemon))
    else:
        world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, term_handler=True)
        starting(daemon)
        publishing = daemon._publish

        def racing(role, path, contents):
            result = publishing(role, path, contents)
            if role == "abandon":                    # the guardian saw it after writing start.json, and exits
                publish(daemon, "start.json", START)
                publish(daemon, "exit.json", with_receipt)
                world.procs[4242].alive = False
            return result
        daemon._publish = racing
        daemon._process_attempt(ATTEMPT)
    a = settle(daemon)
    job = daemon.store.get_job(JOB)
    assert (a["state"], a["outcome_detail"]) == ("failed", "starting-no-receipt"), a
    assert not job["accepted_attempt_id"] and job["state"] in ("queued", "failed"), job


def test_unlaunched_writes_the_abandon_for_a_starting_attempt_whatever_its_caller(daemon):
    """The launch gate's own failure (`guardian-identity-unavailable`) ends a `starting`
    attempt through `_unlaunched` directly; its gate is closed, and the marker is
    written anyway, so no ending of a starting attempt lacks it."""
    daemon.store.update_attempt(ATTEMPT, state="starting", child_pid=None)
    daemon._unlaunched(attempt(daemon), "guardian-identity-unavailable: OSError(EBADF)")
    assert (adir(daemon) / "abandon.json").exists(), "a starting attempt ended with no abandon marker"
    marker = json.loads((adir(daemon) / "abandon.json").read_text())
    assert marker["reason"].startswith("guardian-identity-unavailable")
