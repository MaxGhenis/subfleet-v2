"""Independent C-5.3/C-5.7 process-world extensions and oracle challenges.

Only read visibility changes: the kernel's start identity stays intact. No real
processes are started or signalled. The original model remains unmodified.
"""
import json
import os

from hypothesis import Phase, settings, strategies as st
from hypothesis.stateful import precondition, rule
import pytest

from subfleet import procs
from tests.fake.test_quarantine_process_world import PIDS, ProcessWorldMachine, World
from tests.fake.test_review_pr131_probes import BOOT


class PartialIdentityWorld(World):
    def __init__(self):
        super().__init__()
        self.missing_starts = set()

    def snapshot(self):
        table = super().snapshot()
        rows = {pid: (*row[:3], "" if ("table", pid) in self.missing_starts else row[3])
                for pid, row in table.rows.items()}
        return procs.ProcessTable(rows, table.boot_id, table.taken_at)

    def identity(self, pid):
        phase = "confirm" if pid in self.confirming else "identity"
        process = self.processes.get(pid)
        if process and not process.zombie and (phase, pid) in self.missing_starts:
            self.confirming.discard(pid)
            self.after(phase)
            raise procs.InspectionError("world missing process start identity")
        return super().identity(pid)


class PartialIdentityMachine(ProcessWorldMachine):
    def __init__(self):
        super().__init__()
        self.world = PartialIdentityWorld()

    @precondition(lambda self: self.active())
    @rule(pid=PIDS, phase=st.sampled_from(["table", "identity", "confirm"]), missing=st.booleans())
    def start_column_visibility(self, pid, phase, missing):
        (self.world.missing_starts.add if missing else self.world.missing_starts.discard)((phase, pid))


TestPartialIdentityWorld = PartialIdentityMachine.TestCase
TestPartialIdentityWorld.settings = settings(
    max_examples=int(os.environ.get("SF_REVIEW_WORLD_EXAMPLES", "50")),
    stateful_step_count=25, deadline=None, database=None,
    phases=[phase for phase in Phase if phase != Phase.explain])


def test_missing_start_descendant_changes_group_after_parent_exit():
    """C-5.7 missing table starts retain a writer across escape and reparenting."""
    machine = PartialIdentityMachine()
    try:
        machine.world.fork(100, 200)
        machine.world.missing_starts.add(("table", 200))
        machine.pair(resolve=True)
        assert not any(machine.last_verdicts)
        machine.world.change("setsid", 200)
        machine.world.exit(100)
        machine.pair(resolve=True)
        assert not any(machine.last_verdicts)
        machine.world.missing_starts.clear()
        machine.pair(resolve=True)
        assert not any(machine.last_verdicts)
        machine.world.exit(200)
        machine.pair(resolve=True)
        assert all(machine.last_verdicts)
    finally:
        machine.teardown()


@pytest.mark.parametrize("zombies", [False, True])
def test_liveness_with_unrelated_survivor_or_zombie_only(zombies):
    """C-5.7 release requires empty sources, rather than an empty process table."""
    machine = ProcessWorldMachine()
    try:
        machine.world.fork(100, 200)
        machine.pair(resolve=True)
        machine.world.exit(100)
        if zombies:
            machine.world.change("zombie", 200)
        else:
            machine.world.exit(200)
            machine.world.spawn(900, marked=False, writer=False)
        machine.pair(resolve=True)
        assert all(machine.last_verdicts)
    finally:
        machine.teardown()


def test_s1_oracle_rejects_complete_lease_loss():
    """C-5.7 fault-inject store state to challenge the model's S1 oracle."""
    machine = ProcessWorldMachine()
    try:
        machine.world.spawn(99)
        a = machine.attempts[0]
        machine.daemon.store.release_leases(a["attempt_id"])
        machine.daemon.store.release_leases(a["job_id"])
        with pytest.raises(AssertionError, match="S1 premature release"):
            machine.safety()
    finally:
        machine.teardown()


@pytest.mark.xfail(strict=True, reason="Round-8 item 3: S1 still checks only one lease survives")
def test_s1_oracle_rejects_loss_of_one_protected_lease():
    """C-5.7 loss of a protected lease is unsafe even when another remains."""
    machine = ProcessWorldMachine()
    try:
        machine.world.spawn(99)
        a = machine.attempts[0]
        # The base model uses read-only attempts and a single synthetic native
        # lease. Add a required worktree lease before injecting partial loss.
        machine.daemon.store.acquire_lease("worktree:" + str(machine.harness.workdir), a["attempt_id"])
        machine.safety()
        leases = [l for l in machine.daemon.store.list_leases()
                  if l["holder"] in {a["attempt_id"], a["job_id"]}]
        protected = [l for l in leases if not l["lease_key"].startswith("native:")]
        assert protected, leases
        with machine.daemon.store.transaction() as tx:
            tx.execute("DELETE FROM leases WHERE lease_key=?", (protected[0]["lease_key"],))
        assert any(l["lease_key"].startswith("native:") for l in machine.daemon.store.list_leases())
        with pytest.raises(AssertionError, match="S1 premature release"):
            machine.safety()
    finally:
        machine.teardown()


def test_s2_oracle_rejects_foreign_group_signal():
    """C-5.3 identity confirmation alone cannot authorize a foreign group."""
    world = World()
    world.spawn(200, pgid=700, marked=False, writer=False)
    assert world.same_process(200, BOOT, world.processes[200].start)
    with pytest.raises(AssertionError, match="S2 stray signal"):
        world.signal(200, 9)


@pytest.mark.parametrize("consumer", ["attempt", "probe"])
def test_previously_owned_escape_is_a_valid_signal_target(consumer):
    """C-5.6 a confirmed formerly owned member may be signalled after escape."""
    machine = ProcessWorldMachine()
    try:
        machine.world.fork(100, 200)
        # Record ownership during ordinary inspection, before any signal.
        machine.ownership_pace()
        for a in machine.attempts:
            evidence = json.loads(machine.daemon.store.get_attempt(a["attempt_id"])["evidence_json"])
            assert evidence["owned_identities"]["200"]["proc_start"] == machine.world.processes[200].start
        machine.world.change("setsid", 200)
        assert machine.world.processes[200].pgid == 200
        machine.kill(consumer)
        assert any(pid == 200 for pid, _, _ in machine.world.signals)
        machine.safety()
    finally:
        machine.teardown()


@pytest.mark.parametrize("consumer", ["attempt", "probe"])
def test_escape_without_confirmed_ownership_is_not_signalled(consumer):
    machine = ProcessWorldMachine()
    try:
        machine.world.fork(100, 200)
        # The same child escapes before the pace can confirm group ownership.
        machine.world.change("setsid", 200)
        machine.ownership_pace()
        assert (200, machine.world.processes[200].start) not in machine.world.owned
        machine.kill(consumer)
        assert all(pid != 200 for pid, _, _ in machine.world.signals)
        machine.safety()
        assert machine.world.same_process(200, BOOT, machine.world.processes[200].start)
        with pytest.raises(AssertionError, match="S2 stray signal"):
            machine.world.signal(200, 9)
    finally:
        machine.teardown()


def test_paced_missing_start_observation_survives_parent_death():
    """C-5.5 paced ownership capture preserves unknown descendant identities."""
    machine = PartialIdentityMachine()
    try:
        machine.world.fork(100, 200)
        machine.world.change("setsid", 200)
        machine.world.missing_starts.add(("table", 200))
        table = machine.world.snapshot()
        for a in machine.attempts:
            machine.daemon._record_owned(a, table)
        machine.world.exit(100)
        machine.pair(resolve=True)
        assert not any(machine.last_verdicts)
        machine.world.missing_starts.clear()
        machine.pair(resolve=True)
        assert not any(machine.last_verdicts)
        machine.world.exit(200)
        machine.pair(resolve=True)
        assert all(machine.last_verdicts)
    finally:
        machine.teardown()
