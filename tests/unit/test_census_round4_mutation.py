"""Round 4's mutants, each made to fail a named test (D-KILL3 review r4 of #240).

r4 found four mutants that survived round 3's tests: the export's conversion in a second
transaction, the `_unlaunched` TMPDIR cleanup inside its transaction and before it, and
the loss path without its cwd-clock gate. The others undo this round's fixes. Each
is applied to one `Daemon` method's source text and must fail its oracle (strict xfail);
each oracle passes unmutated in `test_census_round4.py`.
"""

import inspect
import textwrap

import pytest

from subfleet import daemon as daemon_module
from subfleet.daemon import Daemon
from tests.unit import test_census_round4 as oracle
from tests.unit.test_daemon_settle import daemon  # noqa: F401

CLEANUP = "        self._cleanup_attempt_tmpdir(a, census)\n        self._notify()\n"
NOTICE = ("                self._notice(tx, job, (f\"attempt a{a['seq']}: \" if earlier else \"\") + detail + earlier"
          " + retained)\n")
NO_LAUNCH = "        with self.store.transaction(\"attempt.no_launch\""

MUTANTS = {
    # r4: survived. The export's DELETE commits alone; a crash before the conversion
    # leaves a finished job's writer key and its other leases gone.
    "export-second-transaction": ("_export_locked", [(
        "            if keep_worktree:\n"
        "                self._keep_for_cwd(tx, job_id, aid)                                  # C-5.5\n"
        "        self._notify()\n",
        "        if keep_worktree:\n"
        "            with self.store.transaction(\"job.export_kept\", job_id=job_id) as tx:\n"
        "                self._keep_for_cwd(tx, job_id, aid)\n"
        "        self._notify()\n")]),
    # r4: survived. The census would hold the store's write lock for a `ps`.
    "cleanup-inside-transaction": ("_unlaunched", [
        (CLEANUP, "        self._notify()\n"),
        (NOTICE, NOTICE + "            self._cleanup_attempt_tmpdir(a, census)\n")]),
    # r4: survived. The attempt is not terminal yet, so nothing is freed.
    "cleanup-before-transaction": ("_unlaunched", [
        (CLEANUP, "        self._notify()\n"),
        (NO_LAUNCH, "        self._cleanup_attempt_tmpdir(a, census)\n" + NO_LAUNCH)]),
    # r4: survived. Only the gate: inspections alone pace it when one attempt reads its own table.
    "no-pacing-gate": ("_inspect_running", [(
        "        if not self._cwd_census_due(a):\n            return True\n        census = self._contain(a)\n",
        "        census = self._contain(a)\n")]),
    # Found by the test above with a 254 ms census: the clock from the lagging due time alone.
    "clock-from-due-alone": ("_cwd_wait", [(
        "max(began, clocks.get(a['attempt_id'], began)) + self.inspect_interval_s",
        "began + self.inspect_interval_s")]),
    # This round's P3-2, undone: an observer holds the TMPDIR again.
    "tmpdir-from-verified-empty": ("_release_tmpdir", [(
        "            if not census.containment_empty:\n", "            if not census.verified_empty:\n")]),
    # This round's note, undone: the cleanup takes a census of its own.
    "fresh-cleanup-census": ("_unlaunched", [(CLEANUP, "        self._cleanup_attempt_tmpdir(a)\n        self._notify()\n")]),
}


def mutate(monkeypatch, name: str) -> None:
    mutate_method(monkeypatch, *MUTANTS[name])


def mutate_method(monkeypatch, method: str, edits: list[tuple[str, str]]) -> None:
    """Replace one `Daemon` method by its mutated source, compiled in the daemon module."""
    source = inspect.getsource(getattr(Daemon, method))         # indented as in the class
    for old, new in edits:
        assert source.count(old) == 1, (method, old, "the mutant must still hit its production decision")
        source = source.replace(old, new)
    namespace: dict = {}
    exec(compile(textwrap.dedent(source), daemon_module.__file__, "exec"), daemon_module.__dict__, namespace)
    monkeypatch.setattr(Daemon, method, namespace[method])


@pytest.mark.parametrize("name", list(MUTANTS))
def test_r4_each_mutant_compiles_and_hits_its_decision(monkeypatch, name):
    """A mutant whose anchor no longer matched would fail its xfail below for the wrong
    reason (an AssertionError in `mutate`): each anchor hits exactly once, and compiles."""
    original = getattr(Daemon, MUTANTS[name][0])
    mutate(monkeypatch, name)
    assert getattr(Daemon, MUTANTS[name][0]) is not original


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="a crash leaves the export half released")
def test_r4_the_export_conversion_in_a_second_transaction_fails_the_crash_probe(daemon, monkeypatch):
    mutate(monkeypatch, "export-second-transaction")
    oracle.test_r4_a_crash_in_the_export_rolls_it_back_whole_and_the_retry_converts(daemon, "after-conversion")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the census runs under the write lock, before commit")
def test_r4_the_cleanup_inside_the_transaction_fails_the_placement_probe(daemon, monkeypatch, tmp_path):
    mutate(monkeypatch, "cleanup-inside-transaction")
    oracle.test_r4_the_unlaunched_cleanup_runs_after_its_ending_commits_and_outside_the_lock(daemon, tmp_path)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the attempt is still live: nothing is freed")
def test_r4_the_cleanup_before_the_transaction_fails_the_placement_probe(daemon, monkeypatch, tmp_path):
    mutate(monkeypatch, "cleanup-before-transaction")
    oracle.test_r4_the_unlaunched_cleanup_runs_after_its_ending_commits_and_outside_the_lock(daemon, tmp_path)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="a census on every early-expiring table")
def test_r4_the_loss_without_its_gate_fails_the_shared_table_pacing(daemon, monkeypatch):
    mutate(monkeypatch, "no-pacing-gate")
    oracle.test_r4_the_cwd_clock_paces_the_loss_when_the_shared_table_expires_early(daemon, monkeypatch, 0)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="90 censuses in 30 s, against 61")
def test_r4_the_clock_from_the_due_time_alone_fails_the_slow_census_pacing(daemon, monkeypatch):
    mutate(monkeypatch, "clock-from-due-alone")
    oracle.test_r4_the_cwd_clock_paces_the_loss_when_the_shared_table_expires_early(daemon, monkeypatch, .254)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the observer holds the TMPDIR")
def test_r4_tmpdir_release_from_verified_empty_fails_the_verdict_regression(daemon, monkeypatch, tmp_path):
    mutate(monkeypatch, "tmpdir-from-verified-empty")
    oracle.test_r4_an_observer_only_census_removes_the_tmpdir_at_the_verdict(daemon, monkeypatch, tmp_path)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="two censuses for one ending")
def test_r4_a_fresh_cleanup_census_fails_the_census_counter(daemon, monkeypatch, tmp_path):
    mutate(monkeypatch, "fresh-cleanup-census")
    oracle.test_r4_start_grace_hands_its_census_to_the_tmpdir_cleanup(daemon, monkeypatch, tmp_path)
