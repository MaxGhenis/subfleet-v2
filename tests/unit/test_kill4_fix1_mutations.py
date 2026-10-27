"""D-KILL4 fix round 1: strict mutation controls; each mutant must fail its named regression."""

from __future__ import annotations

import pytest

from subfleet.daemon import Daemon
from tests.unit.test_census_round1_mutations import replace_function
from tests.unit.test_daemon_settle import daemon  # noqa: F401
from tests.unit.test_kill4_fix1 import (
    test_a_chain_ancestors_unmarked_child_is_never_signalled as chain_not_seed,
    test_a_held_parent_with_no_marked_descendant_left_releases as held_release,
    test_every_path_records_the_chain_ancestor_and_its_reason as chain_evidence,
    test_kill_on_a_finished_job_never_signals_a_strangers_leaderless_group as recycled_group,
    test_self_stopping_children_born_after_the_group_term_each_get_a_term_before_any_cont as joiners,
    test_the_guardian_is_never_continued_on_a_cancel as guardian_continued,
)
from tests.unit.test_kill4_terminal_containment import world  # noqa: F401

MUTANTS = {
    # Keep a recycled (leaderless) group's members as signal targets (P1).
    "recycled-group-targets": [(Daemon, "_signal_reasons", [
        ("elif pid in census.group_pids and census.leader_verified:", "elif pid in census.group_pids:")])],
    # Continue the guardian after the group's TERM (P2).
    "continue-guardian": [(Daemon, "_kill_protocol", [("and pid != guardian", "")])],
    # Credit the group's TERM to every current group member, later joiners included (P3-1).
    "credit-joiners": [(Daemon, "_kill_protocol", [
        ("and in_group and mine in reached_by[signal.SIGTERM])", "and in_group and group.get(sig))")])],
    # Hold a recorded parent again by its birth for as long as it lives (P3-2).
    "hold-forever": [(Daemon, "_contain", [
        ('marked = dict(evidence["held_marked"] if "held_marked" in evidence else held)',
         "marked = dict(held)")])],
    # Count parent-chain ancestors as walk seeds again (P3-3).
    "seed-chain": [(Daemon, "_contain", [
        ("census.live_pids - census.responsible_pids - census.chain_pids - unattributed",
         "census.live_pids - census.responsible_pids - unattributed")])],
    # Drop the parent-chain reasons from the durable evidence (P3-4).
    "drop-chain-reasons": [(Daemon, "_contain", [
        ('("chain_identities", "attempt.chain_recorded", kept_chain, added)',
         '("chain_identities", "attempt.chain_recorded", kept_chain, {})')]),
                           (Daemon, "_kill_protocol", [("chain.update(found.chain_reasons)", "pass")])],
}
NAMED = {
    "recycled-group-targets": recycled_group, "continue-guardian": guardian_continued, "credit-joiners": joiners,
    "hold-forever": held_release, "seed-chain": chain_not_seed, "drop-chain-reasons": chain_evidence,
}


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the named regression must detect this mutant")
@pytest.mark.parametrize("mutant", sorted(MUTANTS))
def test_d_kill4_fix1_mutant_fails_its_named_regression(mutant, daemon, world, monkeypatch):
    for owner, name, replacements in MUTANTS[mutant]:
        replace_function(monkeypatch, owner, name, replacements)
    regression = NAMED[mutant]
    if regression is chain_evidence:
        regression(daemon, world, monkeypatch, "exit-receipt")
    elif regression in (recycled_group, held_release, chain_not_seed, joiners):
        regression(daemon, world, monkeypatch)
    else:
        regression(daemon, world)
