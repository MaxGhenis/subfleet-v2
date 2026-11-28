"""C-3.8 properties over generated admission pass sequences (invariants 1 and 2).

A real daemon core on a temp state root with one Codex lane, driven pass by pass:
the lane closes and opens, another job holds and frees a job's output path, jobs
are submitted and cancelled, and the daemon restarts. After every step:

1. A reservation is real: every `attempt.reserved` event names an attempt of its
   job, on that attempt's lane.
2. Bounded noise: each job's `job.capacity_waiting` events number at most its new
   verdicts, plus its changes of state, wait reason or exclusions, plus restarts.
"""

import os
from pathlib import Path
import tempfile

from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
import pytest

from subfleet.adapters.base import AdapterError
from subfleet.adapters.registry import register
from subfleet.contracts import ClockSource, Closure, ClosureReason, Reading, ReadingLabel
from subfleet.daemon import ADMISSION_EVENT, RESERVATION_EVENT, Daemon, after, utcnow
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter


class AdmissionPasses(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.directory = tempfile.TemporaryDirectory(prefix="sf-rn-props-", dir=os.environ.get("TMPDIR"))
        self.patch = pytest.MonkeyPatch()
        root = Path(self.directory.name) / "state"
        root.mkdir()
        self.harness = Harness(root)
        register("codex", FakeAdapter)
        self.daemon = None
        self.start()
        self.daemon.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                              ReadingLabel.PROVIDER, "fixture", utcnow()))
        self.out = str(self.harness.root / "report.md")
        self.jobs: list[str] = []
        # Per job: the allowance invariant 2 grants, and what its last look left.
        self.allowed: dict[str, int] = {}
        self.last: dict[str, tuple] = {}

    def start(self):
        self.daemon = Daemon(self.harness.root, desktop_prober=lambda: None)
        self.patch.setattr(self.daemon, "_launch", lambda *args: pytest.fail("nothing here may launch a guardian"))

    def teardown(self):
        try:
            self.daemon.close()
        finally:
            self.patch.undo()
            self.directory.cleanup()

    def look(self):
        """What a job's row and verdict are now: (state, wait_reason, exclusions), signature."""
        out = {}
        for job_id in self.jobs:
            row = self.daemon.store.get_job(job_id)
            wait = self.daemon._capacity_waits.get(job_id)
            out[job_id] = ((row["state"], row["wait_reason"], row["exclusions"]), wait and wait["signature"])
        return out

    @precondition(lambda self: len(self.jobs) < 3)
    @rule(with_output=st.booleans())
    def submit(self, with_output):
        args = self.harness.submit_args(**({"out_path": self.out} if with_output else {}))
        try:
            job_id = self.daemon.dispatch("submit", args)["job_id"]
        except AdapterError:
            return          # submit refuses an output path another job holds; not what is under test
        self.jobs.append(job_id)
        self.allowed[job_id] = 0
        self.last[job_id] = self.look()[job_id]

    @rule()
    def close_lane(self):
        self.daemon.store.put_closure(Closure("codex-1", "account", after(3600), ClosureReason.PROVIDER_LIMIT,
                                              ClockSource.REPORTED, "fixture"))

    @rule()
    def open_lane(self):
        with self.daemon.store.transaction("fixture.reopen") as tx:
            tx.execute("DELETE FROM closures")

    @rule()
    def hold_output(self):
        with self.daemon.store.transaction("fixture.lease") as tx:
            tx.execute("INSERT OR IGNORE INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)",
                       (f"out:{self.out}", "some-other-job", utcnow()))

    @rule()
    def free_output(self):
        with self.daemon.store.transaction("fixture.free") as tx:
            tx.execute("DELETE FROM leases WHERE holder='some-other-job'")

    @precondition(lambda self: self.jobs)
    @rule(index=st.integers(0, 2))
    def cancel(self, index):
        job_id = self.jobs[index % len(self.jobs)]
        self.daemon.dispatch("kill", {"job_id": job_id})
        self.last[job_id] = self.look()[job_id]

    @rule()
    def restart(self):
        self.daemon.close()
        self.start()
        for job_id in self.jobs:
            self.allowed[job_id] += 1
            self.last[job_id] = (self.last[job_id][0], None)

    @rule(repeat=st.integers(1, 4))
    def passes(self, repeat):
        for _ in range(repeat):
            self.daemon._admit()
            with self.daemon.store.transaction("test.due") as tx:
                tx.execute("UPDATE jobs SET next_check_at=? WHERE state='waiting'", (after(-1),))
            now = self.look()
            for job_id, (row, signature) in now.items():
                before_row, before_signature = self.last[job_id]
                self.allowed[job_id] += (row != before_row) + (signature is not None and signature != before_signature)
                self.last[job_id] = (row, signature)

    @invariant()
    def a_reservation_is_real(self):
        if self.daemon is None:
            return
        for row in self.daemon.store.query("SELECT * FROM events WHERE kind=?", (RESERVATION_EVENT,)):
            attempt = self.daemon.store.one("SELECT job_id, lane_id FROM attempts WHERE attempt_id=?",
                                            (row["attempt_id"],))
            assert row["attempt_id"] is not None and attempt is not None, dict(row)
            assert (attempt["job_id"], attempt["lane_id"]) == (row["job_id"], row["lane_id"]), dict(row)
        reservations = self.daemon.store.one("SELECT count(*) AS n FROM events WHERE kind=?", (RESERVATION_EVENT,))
        attempts = self.daemon.store.one("SELECT count(*) AS n FROM attempts")
        assert reservations["n"] == attempts["n"]

    @invariant()
    def waits_are_bounded_by_what_changed(self):
        if self.daemon is None:
            return
        for job_id in self.jobs:
            waits = self.daemon.store.one("SELECT count(*) AS n FROM events WHERE job_id=? AND kind=?",
                                          (job_id, ADMISSION_EVENT))["n"]
            assert waits <= self.allowed[job_id], (job_id, waits, self.allowed[job_id])


TestAdmissionPasses = AdmissionPasses.TestCase
TestAdmissionPasses.settings = settings(
    max_examples=12, stateful_step_count=12, deadline=None, derandomize=True, database=None)
