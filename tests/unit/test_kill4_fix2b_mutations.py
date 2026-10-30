"""D-KILL4 fix round 2b and D-QUAR-START1: strict mutation controls; each mutant must
fail its named regression (an AssertionError; a missing mutation target raises
RuntimeError and fails the test).

Fix round 2c adds review r2's "marker ignored" mutant against the kill4 property, which
round 2b's P3-4 coverage (every ending, the unverifiable censuses) did not detect at its
120 derandomized examples; the property's invariant 6 does."""

from __future__ import annotations

import pytest

from subfleet.daemon import Daemon
from tests.unit.test_census_round1_mutations import replace_function
from tests.unit.test_daemon_settle import daemon  # noqa: F401
from tests.unit.test_kill4_properties import (
    test_no_finalized_attempt_keeps_census_pids_and_nothing_outside_the_census_is_signalled as kill4_property,
)
from tests.unit.test_kill4_fix2b import (
    test_a_pid_held_by_one_census_is_signalled_when_a_later_census_attributes_it as later_attribution,
    test_every_ending_of_a_starting_attempt_writes_the_abandon_before_any_signal_or_end as abandon_first,
    test_kill_force_release_kill_kill_on_a_held_only_census_runs_no_protocol as held_only_kill,
    test_start_grace_extends_for_no_guardian_but_a_live_verified_running_one as extends_only,
    test_unlaunched_writes_the_abandon_for_a_starting_attempt_whatever_its_caller as unlaunched_marker,
)
from tests.unit.test_kill4_terminal_containment import world  # noqa: F401

MUTANTS = {
    # D-QUAR-START1 (a): extend a stopped guardian too.
    "extend-stopped": [(Daemon, "_start_extends", [
        ('or not state or state.startswith(("T", "Z"))):', "or not state):")])],
    # D-QUAR-START1 (a): extend with no bound.
    "extend-unbounded": [(Daemon, "_start_extends", [
        ("if time.monotonic() >= deadline - self.start_grace_s + bound:", "if False:")])],
    # D-QUAR-START1 (b): the daemon reads start.json before it writes the abandon.
    "daemon-reads-first": [(Daemon, "_abandon_start", [
        # (`replace_function` dedents the method: its body is at four spaces.)
        # (The read that finds start.json also records a landing, `_start_landed`.)
        ("""    adir = self._abandon(a, detail)
    start = self._read_json(adir / "start.json")""",
         """    start = self._read_json(attempt_dir(self.root, a["job_id"], a["seq"]) / "start.json")
    adir = self._abandon(a, detail)""")])],
    # D-QUAR-START1 (b): start grace's empty census ends with no abandon written first.
    "skip-abandon-start-grace-empty": [(Daemon, "_process_attempt", [
        ('if census.containment_empty and self._abandon_start(a, "starting-no-receipt"):',
         "if census.containment_empty:")])],
    # D-QUAR-START1 (b): the kill protocol of a starting attempt (start grace, a cancel,
    # the wall) signals and ends it with no abandon written first.
    "skip-abandon-kill-attempt": [(Daemon, "_kill_attempt", [
        ("unlaunched = self._abandon_start(a, unlaunched_detail())", "unlaunched = True")])],
    # D-QUAR-START1 (b): `_unlaunched` ends a starting attempt with no marker.
    "skip-abandon-unlaunched": [(Daemon, "_unlaunched", [
        ("self._abandon(a, detail)\n    observers", "pass\n    observers")])],
    # Decision 2 (P3-2): schedule the protocol for a held-only census again.
    "recontain-no-targets": [(Daemon, "kill", [
        ("contain = self._may_signal(a, census)", "contain = True")])],
    # Decision 3 (P3-3): a hold vetoes every later attribution of the same pid.
    "sticky-hold": [(Daemon, "_signal_reasons", [
        ("never = Daemon._never_signalled(self, a)",
         "never = Daemon._never_signalled(self, a) | {int(pid) for pid in "
         "json.loads(a.get('evidence_json') or '{}').get('held_parents', {})}")])],
}
NAMED = {
    "extend-stopped": (extends_only, "stopped"), "extend-unbounded": (extends_only, "past-bound"),
    "daemon-reads-first": (None, None),
    "skip-abandon-start-grace-empty": (abandon_first, "start-grace-empty"),
    "skip-abandon-kill-attempt": (abandon_first, "cancel"),
    "skip-abandon-unlaunched": (unlaunched_marker, None),
    "recontain-no-targets": (held_only_kill, None), "sticky-hold": (later_attribution, None),
}


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the named regression must detect this mutant")
@pytest.mark.parametrize("mutant", sorted(m for m in MUTANTS if NAMED[m][0] is not None))
def test_fix2b_mutant_fails_its_named_regression(mutant, daemon, world, monkeypatch):
    for owner, name, replacements in MUTANTS[mutant]:
        replace_function(monkeypatch, owner, name, replacements)
    regression, parameter = NAMED[mutant]
    if regression is unlaunched_marker:
        regression(daemon)
    elif regression in (held_only_kill, later_attribution):
        regression(daemon, world, monkeypatch)
    else:
        regression(daemon, world, monkeypatch, parameter)


# Review r2's P3-4 (fix round 2c, decision 4): case 5's workers attributed through the
# parent chain only, the marker ignored. A marked member no other source reaches is never
# signalled, and the attempt is quarantined with it alive.
MARKER_IGNORED = [(Daemon, "_signal_reasons", [
    ("elif pid in census.marker_pids:\n            why = f\"pid {pid}: carries this attempt's markers\"",
     "elif False:\n            why = \"\"")])]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the kill4 property must detect this mutant")
def test_the_marker_ignored_mutant_fails_the_kill4_property(monkeypatch):
    for owner, name, replacements in MARKER_IGNORED:
        replace_function(monkeypatch, owner, name, replacements)
    kill4_property()
