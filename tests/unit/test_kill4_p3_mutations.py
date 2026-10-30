"""Review r3 of #250's three P3s and the start-bound record: strict mutation controls.
Each mutant must fail its named regression (an AssertionError; a missing mutation target
raises RuntimeError and fails the test)."""

from __future__ import annotations

import pytest

from subfleet.daemon import Daemon
from tests.kill4_world import World
from tests.unit.test_census_round1_mutations import replace_function
from tests.unit.test_daemon_settle import ATTEMPT, daemon, with_launch  # noqa: F401
from tests.unit.test_kill4_boot_scope import (
    test_an_unverifiable_census_whose_walk_members_are_tainted_runs_no_protocol as tainted_walk,
    test_case_3_kill_force_release_kill_kill_on_an_unverifiable_census_runs_no_protocol as case_3_sequence,
    world,  # noqa: F401
)
from tests.unit.test_kill4_fix2b import (
    test_every_ending_of_a_starting_attempt_writes_the_abandon_before_any_signal_or_end as every_ending,
    test_extension_is_granted_exactly_while_every_condition_holds_and_never_past_the_bound as extension_property,
)
from tests.unit.test_quar_start1_landed import (
    clock,  # noqa: F401
    test_a_start_that_lands_within_start_grace_records_nothing as unextended_landing,
)

BOUND = "if time.monotonic() >= deadline - self.start_grace_s + bound:"
MUTANTS = {
    # P3-1: every unverifiable census runs the protocol again (review r3's input).
    "p3-1-unverifiable-always": (Daemon, "_may_signal", [
        ("if not census.live_pids and not census.held_pids:", "if True:")]),
    # P3-1: a tainted walk member qualifies, though no read attributes it.
    "p3-1-taint-ignored": (Daemon, "_may_signal", [(" - never - tainted)", " - never)")]),
    # P3-2 (C-5.6): a cancel or wall stop while starting TERMs first, as a launched attempt's.
    "p3-2-starting-stop-terms-first": (Daemon, "_kill_attempt", [
        ("unlaunched = self._abandon_start(a, unlaunched_detail())",
         "unlaunched = self._abandon_start(a, unlaunched_detail()) and lost")]),
    # P3-3: an extension at the bound itself.
    "p3-3-extend-at-the-bound": (Daemon, "_start_extends", [(BOUND, BOUND.replace(">=", ">"))]),
    # P3-3: the bound counted from the deadline instead of its start (10 s late).
    "p3-3-bound-from-the-deadline": (Daemon, "_start_extends", [
        (BOUND, BOUND.replace(" - self.start_grace_s", ""))]),
    # Check 5: a landing recorded for an attempt start grace never extended.
    "check-5-landed-unextended": (Daemon, "_start_landed", [("if (not self.store.one(", "if (False and not self.store.one(")]),
}


@pytest.fixture
def plain_world(daemon, monkeypatch):
    """`test_kill4_terminal_containment.py`'s `world`: the simulated host on one boot."""
    w = World(ATTEMPT, str(daemon.root)).install(monkeypatch)
    daemon.exit_settle_s = 0
    with_launch(daemon, monkeypatch)
    return w


def mutate(monkeypatch, mutant: str) -> None:
    owner, name, replacements = MUTANTS[mutant]
    replace_function(monkeypatch, owner, name, replacements)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the named regression must detect this mutant")
@pytest.mark.parametrize("mutant", ["p3-1-unverifiable-always", "p3-1-taint-ignored"])
def test_a_p3_1_mutant_fails_its_named_regression(mutant, daemon, world, monkeypatch):
    mutate(monkeypatch, mutant)
    if mutant == "p3-1-unverifiable-always":
        case_3_sequence(daemon, world, monkeypatch, "99", world.boot)
    else:
        tainted_walk(daemon, world, monkeypatch)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the named regression must detect this mutant")
@pytest.mark.parametrize("path", ["cancel", "wall"])
def test_the_p3_2_mutant_fails_every_ending_of_a_starting_stop(daemon, plain_world, monkeypatch, path):
    mutate(monkeypatch, "p3-2-starting-stop-terms-first")
    every_ending(daemon, plain_world, monkeypatch, path)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the extension property must detect this mutant")
@pytest.mark.parametrize("mutant", ["p3-3-extend-at-the-bound", "p3-3-bound-from-the-deadline"])
def test_a_p3_3_mutant_fails_the_extension_property(mutant, daemon, monkeypatch):
    mutate(monkeypatch, mutant)
    extension_property(daemon, monkeypatch)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the named regression must detect this mutant")
def test_the_check_5_mutant_fails_the_unextended_landing(daemon, plain_world, clock, monkeypatch):
    mutate(monkeypatch, "check-5-landed-unextended")
    unextended_landing(daemon, plain_world, clock)
