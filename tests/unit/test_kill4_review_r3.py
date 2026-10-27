"""Review r3 of #250 (D-KILL4 fix round 2b, D-QUAR-START1): inputs the round's own tests
did not execute, on the simulated host (`tests/kill4_world.py`).

1. Start grace's extension ends exactly at `START_GRACE_MAX_S`: on a fake clock, a
   guardian is extended just before the bound and not at it or after it. The derandomized
   property and the past-bound example both run a real clock, which can never land on the
   bound, so a `>` for the `>=` survived them.
2. A daemon restart between the abandon handshake's two steps (`abandon.json` written,
   then the daemon gone before its last read of `start.json`): the restarted daemon never
   ends a launched attempt unlaunched, and a guardian that then finds the marker launches
   nothing, even while the restarted daemon extends its start grace afresh.
3. Review r2's P3-1 input with each kind of guardian (stopped, alive and running past the
   bound, dead): one state and one retry decision with and without an operator's shell
   in the worktree.
"""

from __future__ import annotations

import time

import pytest

from subfleet import daemon as daemon_module, folders
from subfleet.contracts import START_GRACE_MAX_S, START_GRACE_S
from subfleet.daemon import Daemon
from tests.unit.test_census_round1_mutations import replace_function
from tests.unit.test_daemon_settle import ATTEMPT, JOB, STARTED, attempt, daemon  # noqa: F401
from tests.unit.test_kill4_fix2b import START, abandoned_receipt, adir, publish, settle, starting
from tests.unit.test_kill4_terminal_containment import kinds, owned_worktree, world  # noqa: F401


def running_guardian(world):
    return world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, term_handler=True)


def due(daemon) -> None:
    daemon._inspect_next.pop(ATTEMPT, None)
    daemon.__dict__.setdefault("_cwd_wait_next", {}).pop(ATTEMPT, None)


# 1. The bound, exactly.

@pytest.mark.parametrize(("offset", "extends"), [(-1e-6, True), (0.0, False), (1e-6, False)],
                         ids=["just-before", "exactly-at", "just-after"])
def test_start_grace_extension_ends_exactly_at_the_bound(daemon, world, monkeypatch, offset, extends):
    running_guardian(world)
    daemon.store.update_attempt(ATTEMPT, state="starting", child_pid=None)
    daemon.start_grace_s = START_GRACE_S
    census = daemon._contain(attempt(daemon))
    began = 1000.0                                   # the deadline began at the launch
    monkeypatch.setattr(daemon_module.time, "monotonic", lambda: began + START_GRACE_MAX_S + offset)
    assert daemon._start_extends(attempt(daemon), census, began + START_GRACE_S) is extends


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the exact bound must detect this mutant")
def test_an_extension_at_the_bound_itself_fails_the_exact_bound_test(daemon, world, monkeypatch):
    replace_function(monkeypatch, Daemon, "_start_extends", [
        ("if time.monotonic() >= deadline - self.start_grace_s + bound:",
         "if time.monotonic() > deadline - self.start_grace_s + bound:")])
    test_start_grace_extension_ends_exactly_at_the_bound(daemon, world, monkeypatch, 0.0, False)


# 2. A daemon restart between the handshake's two steps.

def restart(daemon) -> None:
    """What a restart loses: every in-memory clock and scope. The store and the files stay."""
    for name in ("_starting_deadlines", "_inspect_next", "_children", "_exit_settle"):
        getattr(daemon, name).clear()
    daemon._inspect_retry.clear()
    for name in ("_held_scope", "_cwd_settle", "_cwd_wait_next", "_containment_rounds", "_counted_tables"):
        daemon.__dict__.pop(name, None)


@pytest.mark.parametrize("guardian", ["spawned-before-the-marker", "found-the-marker", "slow-then-found-the-marker"])
def test_a_restart_between_the_abandon_and_the_last_read_never_ends_a_launched_attempt_unlaunched(
        daemon, world, monkeypatch, guardian):
    g = running_guardian(world)
    starting(daemon)
    daemon.store.update_job(JOB, max_attempts=2)
    daemon._abandon(attempt(daemon), "starting-no-receipt")       # step one; the daemon dies before its read
    provider = None
    if guardian == "spawned-before-the-marker":
        # Its look before spawning came before the marker: start.json, then its provider.
        publish(daemon, "start.json", START)
        provider = world.add(4243, 4242, 4242, start=STARTED, marked=True)
    elif guardian == "found-the-marker":
        publish(daemon, "start.json", START)
        publish(daemon, "exit.json", abandoned_receipt())
        g.alive = False
    restart(daemon)
    if guardian == "slow-then-found-the-marker":
        # The restarted daemon's deadline starts at its first look; once it expires, the guardian,
        # alive and running, is extended afresh. The marker the old daemon wrote still stands.
        due(daemon)
        daemon._process_attempt(ATTEMPT)
        daemon._starting_deadlines[ATTEMPT] = time.monotonic() - 1
        due(daemon)
        daemon._process_attempt(ATTEMPT)
        assert attempt(daemon)["state"] == "starting" and "attempt.start_extended" in kinds(daemon)
        assert not world.signals
        publish(daemon, "start.json", START)
        publish(daemon, "exit.json", abandoned_receipt())
        g.alive = False
    a = settle(daemon)
    if provider is not None:
        assert a["state"] == "running" and provider.alive and not world.signals, a
    else:
        assert (a["state"], a["outcome_detail"]) == ("failed", "starting-no-receipt"), a
        assert not daemon.store.get_job(JOB)["accepted_attempt_id"] and 4243 not in world.procs
    assert (adir(daemon) / "abandon.json").exists()


# 3. Review r2's P3-1 input, every kind of guardian.

@pytest.mark.parametrize("operator", [False, True], ids=["no-cwd-holder", "operator-shell"])
@pytest.mark.parametrize("guardian", ["stopped", "running-past-the-bound", "dead"])
def test_a_contained_start_grace_has_one_end_for_every_kind_of_guardian(daemon, world, monkeypatch, guardian,
                                                                         operator):
    tree = owned_worktree(daemon, monkeypatch, world)
    daemon.store.acquire_lease(folders.exclusive_key(tree), JOB)
    if guardian != "dead":
        world.add(4242, 1, 4242, start=STARTED, marked=True, ignores_term=True, term_handler=True,
                  stopped=guardian == "stopped")
    shell = world.add(6000, 1, 6000, cwd=str(tree)) if operator else None
    starting(daemon, expired_s=START_GRACE_MAX_S if guardian == "running-past-the-bound" else 1e6)
    daemon.store.update_job(JOB, max_attempts=2)
    a = settle(daemon, passes=10)
    job = daemon.store.get_job(JOB)
    assert (a["state"], a["outcome_detail"], job["state"]) == ("failed", "starting-no-receipt", "queued"), (a, job)
    assert world.signals == ([] if guardian == "dead" else [("group", 4242, 9)]), world.signals
    assert "attempt.start_extended" not in kinds(daemon)
    if shell is not None:
        assert shell.alive and shell.pid not in world.signalled()
