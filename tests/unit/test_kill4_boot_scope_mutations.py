"""D-KILL4 with D-QUAR-REUSE1 (#250 fix round 2a): strict mutation controls for the
combination; each mutant must fail its named regression in `test_kill4_boot_scope.py`."""

from __future__ import annotations

import pytest

from subfleet import procs
from subfleet.daemon import Daemon
from tests.unit.test_census_round1_mutations import replace_function
from tests.unit.test_daemon_settle import daemon  # noqa: F401
from tests.unit.test_kill4_boot_scope import (
    BOOT_A,
    test_case_2_with_the_attempts_boot_over_the_group_is_no_source_and_confirm_dead_releases as leader_order,
    test_case_7_a_chain_ancestor_stored_on_another_boot_is_never_re_added as chain_read,
    test_case_7_a_held_parent_from_another_boot_is_never_held as held_rehold,
    test_case_8_a_marked_process_on_a_later_boot_is_a_target_and_nothing_is_kept as kept_writes,
    world,  # noqa: F401
)

#: Review r2's combination plan, undone one step at a time.
LEADER_FIRST = '''        compared = len(errors)
        if pgid and groups and is_recycled(pgid):
            groups.clear()
        if pgid and groups and pgid in recorded_identities:
            known = recorded_identities[pgid]
            if pgid in table:
                match = None if len(errors) > compared else True
            else:
                match = leader_boot(pgid)
                if match is None:
                    errors.append(f"boot identity unavailable for group {pgid}")
            if match is False:
                recycled.setdefault(pgid, {"recorded": asdict(known), "now": {"pid": pgid},
                                           "reason": "group of another boot"})
                groups.clear()
            elif match is True:
                leader_verified = bool(live(pgid) and known.proc_start and table[pgid][3] == known.proc_start)
        if (attempt_boot and'''
MUTANTS = {
    # The boot filter no longer reads #250's stored keys: chain ancestors and held parents.
    "unscoped-reads": [(procs, "containment", [
        ("        if parent_chain is not None:\n            # D-KILL4's stored keys too",
         "        if False:\n            # D-KILL4's stored keys too")])],
    # #250's keys are written without the boot filter: chain, held parents, held_marked, taint.
    "unscoped-writes": [(Daemon, "_contain", [
        ("if held.get(str(pid)) != dataclasses.asdict(ident) and on_boot(ident)}",
         "if held.get(str(pid)) != dataclasses.asdict(ident)}"),
        ("if pid in census.identities and attempts(pid)", "if pid in census.identities"),   # chain, taint
        ("if pid in census.held_identities and on_boot(census.held_identities[pid])}",
         "if pid in census.held_identities}"),
        ("Daemon._tainted(current, census, boot)", "Daemon._tainted(current, census)")]),
                        (Daemon, "_kill_protocol", [
        ("and not procs.boot_identity.differ(census.held_identities[pid].boot_id, boot)}}", "and True}}")])],
    # A held parent is held again by its start time alone, as #250 compared it.
    "start-only-rehold": [(procs, "_parent_chain", [("\n                and same_boot(known)):", "):")])],
    # #250's leader check runs before #259's "the attempt's boot is over" branch.
    "leader-first": [(procs, "containment", [("        if (attempt_boot and", LEADER_FIRST)])],
}


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the named regression must detect this mutant")
@pytest.mark.parametrize("mutant", sorted(MUTANTS))
def test_kill4_boot_scope_mutant_fails_its_named_regression(mutant, daemon, world, monkeypatch):
    for owner, name, replacements in MUTANTS[mutant]:
        replace_function(monkeypatch, owner, name, replacements)
    if mutant == "unscoped-reads":
        chain_read(daemon, world, monkeypatch)
    elif mutant == "unscoped-writes":
        kept_writes(daemon, world, monkeypatch)
    elif mutant == "start-only-rehold":
        held_rehold(daemon, world, BOOT_A, ("held_marked",))
    else:
        leader_order(daemon, world, monkeypatch)
