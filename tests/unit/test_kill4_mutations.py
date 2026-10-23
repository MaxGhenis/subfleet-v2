"""D-KILL4 strict mutation controls: each mutant must fail its named regression."""

from __future__ import annotations

import pytest

from subfleet.daemon import Daemon
from tests.unit.test_census_round1_mutations import replace_function
from tests.unit.test_daemon_settle import daemon  # noqa: F401
from tests.unit.test_kill4_terminal_containment import (
    test_a_cancel_contains_a_hidden_driver_in_the_worktree_with_its_respawning_child as driver_contained,
    test_a_dead_guardians_escaped_marked_runner_is_signalled_not_left as dead_guardian,
    test_a_stopped_escaped_member_is_continued_and_contained_and_nothing_else_is as stopped_escaped,
    test_an_in_place_hidden_driver_is_held_never_signalled_and_the_attempt_quarantines as held_driver,
    test_an_uncounted_operator_process_in_the_worktree_is_never_signalled_and_the_verdict_stands as cwd_negative,
    test_start_grace_contains_a_stopped_guardian_before_any_retry as no_early_retry,
    test_writers_left_after_the_exit_receipt_are_contained_then_the_receipt_decides as receipt_contained,
    world,
)  # noqa: F401
from subfleet import procs


MUTANTS = {
    # Finalize `lost` (and after a receipt) without the kill protocol, as before D-KILL4.
    "lost-uncontained": [(Daemon, "_finalize", [("if not census.containment_empty:", "if False:")]),
                         (Daemon, "_inspect_running", [("self._kill_attempt(a, lost=True)", "self._lost(a)")])],
    # Quarantine writers after the receipt without containing them first.
    "receipt-uncontained": [(Daemon, "_finalize", [
        ('census = self._contain_writers(a, census, "writers remain after exit receipt")',
         'self._quarantine(a, census, "writers remain after exit receipt"); census = None')])],
    # Release a lost-at-start attempt to its retry before its census is empty.
    "retry-first": [(Daemon, "_process_attempt", [
        ('self._kill_attempt(a, lost=True, reason="start grace expired without a receipt",',
         'self._unlaunched(a, "starting-no-receipt", census=census); (lambda *a, **k: None)('
         'a, lost=True, reason="start grace expired without a receipt",')])],
    # Drop the parent-chain rule.
    "no-parent-chain": [(Daemon, "_contain", [
        ('**({"parent_chain": chain} if chain is not None else {})', '**{}')])],
    # Signal an uncounted process because its cwd is in the worktree.
    "signal-cwd": [(Daemon, "_signal_reasons", [
        ("for pid in sorted(census.live_pids):", "for pid in sorted(census.live_pids | census.cwd_pids):"),
        ("ident = census.identities.get(pid)", "ident = census.identities.get(pid) or procs.identity(pid)"),
        ("elif pid in census.responsible_pids:", "elif pid in census.responsible_pids | census.cwd_pids:")])],
    # Let a cwd-only census change a verdict again (#240's rule).
    "cwd-verdict": [(Daemon, "_finalize", [("if not census.containment_empty:", "if not census.verified_empty:")]),
                    (Daemon, "_contain_writers", [("if not census.containment_empty:",
                                                   "if not census.verified_empty:")])],
    # TERM a stopped member without continuing it.
    "no-continue": [(Daemon, "_kill_protocol", [("if (sig == signal.SIGTERM and reached and stopped(found, pid)",
                                                 "if (False and reached and stopped(found, pid)")])],
    # Forget held parents: a respawning driver no census may signal stops holding the attempt.
    "no-hold": [(procs.Containment, "containment_empty", None)],
}
NAMED = {
    "lost-uncontained": dead_guardian, "receipt-uncontained": receipt_contained, "retry-first": no_early_retry,
    "no-parent-chain": driver_contained, "signal-cwd": cwd_negative, "cwd-verdict": cwd_negative,
    "no-continue": stopped_escaped, "no-hold": held_driver,
}


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the named regression must detect this mutant")
@pytest.mark.parametrize("mutant", sorted(MUTANTS))
def test_d_kill4_mutant_fails_its_named_regression(mutant, daemon, world, monkeypatch):
    for owner, name, replacements in MUTANTS[mutant]:
        if replacements is None:
            monkeypatch.setattr(owner, name, property(
                lambda self: not self.unverifiable and not self.live_pids))
        else:
            replace_function(monkeypatch, owner, name, replacements)
    regression = NAMED[mutant]
    if regression is driver_contained:
        regression(daemon, world, monkeypatch, "operator")
    elif regression in (no_early_retry, cwd_negative):
        regression(daemon, world, monkeypatch)
    else:
        regression(daemon, world)
