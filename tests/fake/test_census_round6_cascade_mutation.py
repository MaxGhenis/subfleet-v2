"""Round 6's mutants (D-KILL3 review r5 of #240), each made to fail a named test.

Each is applied to `Daemon._request_cancel`'s source text and must fail its oracle (strict
xfail); each oracle passes unmutated in `test_census_round6_cascade.py`, and I6 in
`test_census_round4_endings.py`. The third is r5's survivor: it passed every round-4 test.
"""

from hypothesis import example, given, strategies as st
import pytest

from subfleet.daemon import Daemon
from tests.fake import test_census_round6_cascade as oracle
from tests.fake.test_census_round3_leases import observed  # noqa: F401
from tests.fake.test_census_round4_endings import (
    BOUNDED,
    ENDINGS,
    test_r4_i6_every_ending_with_an_observer_leaves_one_reader_row_and_no_writer_key as i6,
)
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.unit.test_census_round4_mutation import mutate_method

MUTANTS = {
    # P3-1 undone: the cascade scans inside the transaction that settles its parent's attempt.
    "no-transaction-check": ("_request_cancel", [(
        "        observers = {} if self.store._holds_writer() else {\n", "        observers = {\n")]),
    # P3-1's second half undone: a finished family member is scanned, and then left alone.
    "no-state-filter": ("_request_cancel", [(
        "\"j.state NOT IN ('succeeded','failed','cancelled','lost') AND EXISTS", "\"EXISTS")]),
    # P3-2, r5's survivor: the scan of the root job only, the family ignored.
    "root-only-scan": ("_request_cancel", [(
        "family_sql + \"SELECT f.job_id FROM family f JOIN jobs j",
        "\"WITH family(job_id) AS (SELECT ?) SELECT f.job_id FROM family f JOIN jobs j")]),
}

CASCADE_REGRESSIONS = {
    "quarantine": oracle.test_r6_a_wall_cascade_from_quarantine_resolution_scans_nothing_under_the_write_lock,
    "finalize": oracle.test_r6_a_wall_cascade_from_finalization_scans_nothing_under_the_write_lock,
}


def mutate(monkeypatch, name: str) -> None:
    mutate_method(monkeypatch, *MUTANTS[name])


@pytest.mark.parametrize("name", list(MUTANTS))
def test_r6_each_mutant_compiles_and_hits_its_decision(monkeypatch, name):
    """Each anchor hits exactly once, and the mutant compiles (`mutate_method` asserts it)."""
    original = getattr(Daemon, MUTANTS[name][0])
    mutate(monkeypatch, name)
    assert getattr(Daemon, MUTANTS[name][0]) is not original


@pytest.mark.parametrize("path", list(CASCADE_REGRESSIONS))
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the child's scan runs under the write lock")
def test_r6_a_cascade_without_the_transaction_check_fails_its_regression(observed, monkeypatch, path):
    mutate(monkeypatch, "no-transaction-check")
    CASCADE_REGRESSIONS[path](observed, monkeypatch)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the finished child is scanned")
def test_r6_the_scan_without_its_state_filter_fails_the_pending_export_regression(observed, monkeypatch):
    mutate(monkeypatch, "no-state-filter")
    oracle.test_r6_a_finished_family_member_with_its_export_pending_is_not_scanned(observed, monkeypatch)


@pytest.mark.parametrize("scan", ["observer", "gone"])
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the queued child is not scanned")
def test_r6_the_root_only_scan_fails_the_family_kill_regression(observed, monkeypatch, scan):
    mutate(monkeypatch, "root-only-scan")
    oracle.test_r6_a_family_kill_scans_the_queued_child_that_holds_its_key(observed, monkeypatch, scan)


def failures_only_at(failure: pytest.ExceptionInfo, ending: str) -> None:
    """Every failure Hypothesis reports is an assertion error at `ending`'s example."""
    found = failure.value.exceptions if isinstance(failure.value, BaseExceptionGroup) else (failure.value,)
    assert found and all(isinstance(error, AssertionError) for error in found), found
    examples = ["\n".join(getattr(error, "__notes__", [])) for error in found]
    assert all(f"ending={ending!r}" in example for example in examples), examples


def test_r6_the_root_only_scan_fails_the_i6_property_at_its_family_ending_alone(tmp_path_factory, monkeypatch):
    """I6 kills it too, and only at the family kill: the cascade takes no scan, and every other
    ending is the root's own. Hypothesis groups failures from distinct lines (here the missing
    pid line and the row kept with nobody there), so this asserts each one, not a strict xfail."""
    mutate(monkeypatch, "root-only-scan")
    with pytest.raises((AssertionError, BaseExceptionGroup)) as failure:
        i6(tmp_path_factory=tmp_path_factory)
    failures_only_at(failure, "family-cancel")


def i6_shaped(failing: frozenset[str]):
    """Review r6's synthetic property (P3-2): I6's settings and its three no-observer examples,
    each ending in `failing` failing at one assertion, as a cascade would at I6's
    `("no observer, no row", ...)` line."""
    @BOUNDED
    @given(ending=st.sampled_from(ENDINGS), pids=st.just(frozenset()))
    @example(ending="family-cancel", pids=frozenset())
    @example(ending="cascade-quarantine", pids=frozenset())
    @example(ending="cascade-finalize", pids=frozenset())
    def prop(ending, pids):
        assert ending not in failing, ("no observer, no row", ending)
    return prop


def test_r7_the_i6_kill_check_sees_a_cascade_failing_at_family_cancels_line():
    """Review r6 P3-2: Hypothesis reports one failing explicit example per line, the shortest, so a
    cascade failing where family-cancel fails is hidden behind it, named only in a note. The kill
    check must fail on that note."""
    with pytest.raises(AssertionError) as failure:
        i6_shaped(frozenset({"family-cancel", "cascade-finalize"}))()
    notes = "\n".join(getattr(failure.value, "__notes__", []))
    assert "ending='family-cancel'" in notes and "ending='cascade-finalize'" not in notes, notes
    with pytest.raises(AssertionError, match="also failed with this error"):
        failures_only_at(failure, "family-cancel")


def test_r7_the_i6_kill_check_passes_a_failure_at_family_cancel_alone():
    """The control: a property failing only at family-cancel passes the check."""
    with pytest.raises(AssertionError) as failure:
        i6_shaped(frozenset({"family-cancel"}))()
    failures_only_at(failure, "family-cancel")
