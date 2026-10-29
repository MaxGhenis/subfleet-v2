"""Dropping the key again at a queue ending (4cddd7925's behaviour) must fail the named regression."""

import pytest

from subfleet.daemon import Daemon
from tests.fake.test_census_round3_leases import observed  # noqa: F401
from tests.fake.test_census_round4_endings import (
    test_r4_a_job_ending_from_the_queue_keeps_its_worktree_as_one_reader_row as queue_ending,
)
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.unit.test_census_round4_mutation import mutate_method

#: Only where the job ends: queued again after start grace, it still keeps its own key.
DROPS_THE_KEY = ("_release_ending", [("        keep = observers != frozenset() and bool(",
                                      "        keep = not convert and observers != frozenset() and bool(")])


@pytest.mark.parametrize("ending", ["kill", "fail-queued", "cancelled-before-launch"])
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="no lease is left: the folder has no row")
def test_r4_a_queue_ending_that_drops_the_key_fails_the_regression(observed, monkeypatch, ending):
    mutate_method(monkeypatch, *DROPS_THE_KEY)
    queue_ending(observed, monkeypatch, ending, "observer")


def test_r4_the_queue_ending_mutant_hits_its_decision(monkeypatch):
    original = Daemon._release_ending
    mutate_method(monkeypatch, *DROPS_THE_KEY)
    assert Daemon._release_ending is not original
