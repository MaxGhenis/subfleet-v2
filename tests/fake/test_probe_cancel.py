"""C-7.4: cancellation serializes with real admission reservations and gates.

The state machine uses SQLite, the daemon, FakeAdapter and real gate pipes.
No daemon worker, guardian or provider process is started. Cancellation at a
transaction boundary models a commit overtaking an earlier read, without threads.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

from hypothesis import event, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule
import pytest

from subfleet import daemon as module, scheduler
from subfleet.adapters import registry
from subfleet.contracts import Outcome, OutcomeClass
from subfleet.daemon import Daemon, TERMINAL
from subfleet.store import Store
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter


class AdmissionHistory:
    def __init__(self, root, patch):
        self.root, self.patch = root, patch
        root.mkdir()
        self.harness = Harness(root)
        self.job_id = self.routed = self.holder = None
        self.cancel_at = None
        self.gates, self.reservations, self.guardians = [], [], []
        self.defer_probe = True
        self.real_candidate = Daemon._probe_candidate
        self.real_route = Daemon._route
        self.real_transaction = Store.transaction
        self.real_pipe = module.procs.pipe_above_stdio
        self.real_write = os.write
        self.observer = self.gate_fd = None
        patch.setattr(registry, "_factories", {"codex": FakeAdapter})
        patch.setattr(module.procs, "boot_id", lambda: "cancel-test-boot")
        patch.setattr(module.procs, "proc_start", lambda pid: "cancel-test-start")
        patch.setattr(module.procs, "proc_start_retry", lambda *a, **k: "cancel-test-start")
        patch.setattr(module.procs, "same_process", lambda *a, **k: False)
        patch.setattr(module.procs, "containment", lambda *a, **k: module.procs.Containment())
        patch.setattr(Daemon, "_desktop_identity", lambda self: None)
        patch.setattr(Daemon, "_desktop_in_use", lambda self: False)
        patch.setattr(Daemon, "_record_desktop_use", lambda self: None)
        patch.setattr(Daemon, "_desktop_answer", lambda self: False)
        patch.setattr(Daemon, "_liveness", lambda self, jobs: scheduler.Liveness())
        patch.setattr(module, "git_head", lambda *a, **k: None)
        patch.setattr(Daemon, "_route", self.route_boundary)
        patch.setattr(Daemon, "_probe_candidate", self.probe_boundary)
        patch.setattr(Daemon, "_await_probe", self.await_probe)
        patch.setattr(Store, "transaction", lambda store, *a, **k: self.transaction_boundary(store, *a, **k))
        patch.setattr(module.procs, "pipe_above_stdio", self.pipe)
        patch.setattr(module.os, "write", self.write_gate)
        patch.setattr(module, "subprocess", SimpleNamespace(
            **{**vars(module.subprocess), "Popen": self.guardian}))
        self.service = Daemon(root, desktop_prober=lambda: None, term_grace_s=0)

    def submit(self, **overrides):
        self.job_id = self.service.dispatch("submit", self.harness.submit_args(
            tier="hard", pinned_lane="codex-1", **overrides))["job_id"]
        return self.job_id

    def cancel(self):
        self.service.dispatch("kill", {"job_id": self.job_id})

    def route(self):
        self.routed = self.real_route(self.service, self.service.store.get_job(self.job_id))
        assert self.routed.chosen_lane == "codex-1"
        assert scheduler.probe_required(self.routed, self.service.store.get_job(self.job_id))

    def route_boundary(self, job, **kwargs):
        decision = self.real_route(self.service, job, **kwargs)
        if self.cancel_at == "after-route":
            self.cancel_at = None
            self.cancel()
        return decision

    def reserved_holders(self, store):
        return {json.loads(row["data_json"])["holder"] for row in store.query(
            "SELECT data_json FROM events WHERE kind='probe.state'")
            if json.loads(row["data_json"]).get("state") == "reserved"}

    @contextmanager
    def transaction_boundary(self, store, kind="state.changed", **kwargs):
        if self.cancel_at == kind:
            self.cancel_at = None
            self.cancel()  # Commit BEFORE BEGIN IMMEDIATE, after any outside read.
        with self.real_transaction(store, kind, **kwargs) as tx:
            row = store.get_job(kwargs["job_id"]) if kwargs.get("job_id") else None
            blocked = bool(row and (row["cancel_requested_at"] or row["state"] in TERMINAL))
            if kind in ("probe.reserved", "attempt.reserved"):
                before = (self.reserved_holders(store), {a["attempt_id"] for a in store.list_attempts()})
            yield tx
            if kind in ("probe.reserved", "attempt.reserved"):
                after = (self.reserved_holders(store), {a["attempt_id"] for a in store.list_attempts()})
                added = (after[0] - before[0]) | (after[1] - before[1])
                self.reservations.append((kind, blocked, added))
                assert not (blocked and added), "reserved after cancellation at transaction entry"

    def reserve_probe(self, cancel_at=None):
        self.cancel_at = cancel_at
        job = self.service.store.get_job(self.job_id)
        self.service._prepare_route(job, job, ())
        current = self.service.store.get_job(self.job_id)
        assert self.cancel_at is None or current["cancel_requested_at"] or current["state"] in TERMINAL
        self.cancel_at = None

    def probe_boundary(self, job, decision, holder):
        self.holder = holder
        if self.defer_probe:
            return Outcome(OutcomeClass.UNKNOWN, "deferred by state-machine operation")
        return self.real_candidate(self.service, job, decision, holder)

    def pipe(self):
        read_fd, write_fd = self.real_pipe()
        self.observer, self.gate_fd = os.dup(read_fd), write_fd
        os.set_blocking(self.observer, False)
        return read_fd, write_fd

    def write_gate(self, fd, value):
        if fd == self.gate_fd:
            current = self.service.store.get_job(self.job_id)
            blocked = bool(current["cancel_requested_at"] or current["state"] in TERMINAL)
            self.gates.append((value, blocked))
            assert not (value == b"1" and blocked), "gate opened after cancellation"
            assert self.service.store.conn.in_transaction, "gate decision escaped its transaction"
        return self.real_write(fd, value)

    def guardian(self, command, **kwargs):
        self.guardians.append(command)
        record = self.service._probe_record(self.holder)
        assert record["state"] == "reserved"
        return SimpleNamespace(pid=987654321, poll=lambda: None)

    def await_probe(self, record, child=None):
        # Exercise production containment and finishing, with a verified empty census.
        return self.service._contain_probe(record), None

    def open_gate(self, cancel_at=None, *, contained=True):
        self.cancel_at = cancel_at
        job = self.service.store.get_job(self.job_id)
        try:
            self.real_candidate(self.service, job, self.routed, self.holder)
            byte = os.read(self.observer, 1)
            assert byte == self.gates[-1][0]
        finally:
            if self.observer is not None:
                os.close(self.observer)
                self.observer = self.gate_fd = None
        if contained:
            assert not self.service.store.list_leases(self.holder)
            assert self.service._probe_record(self.holder)["state"] == "completed"
            self.holder = None
        else:
            assert self.service.store.list_leases(self.holder)
            assert self.service._probe_record(self.holder)["state"] == "quarantined"

    def contain(self):
        self.service._recover_probes()
        self.holder = None

    def restart(self):
        self.service.close()
        self.service = Daemon(self.root, desktop_prober=lambda: None, term_grace_s=0)
        self.service._recover_probes()
        self.service._recover_capacity_waits()
        self.holder = None

    def reserve_attempt(self, cancel_at=None):
        self.cancel_at = cancel_at
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(self.service, "_needs_probe", lambda *a: False)
            self.service._admit()
        self.cancel_at = None

    def close(self):
        self.service.close()
        if self.observer is not None:
            os.close(self.observer)


@pytest.fixture
def history(tmp_path, monkeypatch):
    history = AdmissionHistory(tmp_path / "state", monkeypatch)
    try:
        yield history
    finally:
        history.close()


def test_verifier_minimized_cancel_after_route(history):
    """Audit A1: one hard readonly Astra pin, uncapped, unmeasured Codex lane."""
    history.job_id = history.service.dispatch("submit", history.harness.submit_args(
        request_id="candidate", tier="hard"))["job_id"]
    job = history.service.store.get_job(history.job_id)
    assert job["pinned_model"] == "astra" and job["pinned_lane"] is None
    assert job["sandbox"] == "read-only"
    assert history.service.policy["caps"]["max_active_attempts"] is None
    assert history.service.store.list_readings() == []
    history.route()
    history.defer_probe = False
    history.reserve_probe("after-route")
    job = history.service.store.get_job(history.job_id)
    assert job["state"] == "cancelled" and job["cancel_requested_at"]
    assert history.holder is None
    assert history.guardians == history.gates == []
    assert history.service.store.list_leases() == []
    assert history.service.store.list_attempts() == []
    assert history.reserved_holders(history.service.store) == set()
    assert list((history.root / "lanes/codex-1/probes").iterdir()) == []


@pytest.mark.parametrize("change", ["stamp", "succeeded", "failed", "cancelled", "lost"])
def test_probe_reservation_rechecks_terminal_or_stamp(history, change):
    history.submit()
    real = history.real_route

    def changed_route(*args, **kwargs):
        decision = real(*args, **kwargs)
        if change == "stamp":
            history.service.store.update_job(history.job_id, cancel_requested_at=module.utcnow())
        else:
            history.service.store.update_job(history.job_id, state=change)
        return decision

    history.real_route = changed_route
    history.reserve_probe()
    assert history.holder is None
    assert history.reserved_holders(history.service.store) == set()


def test_probe_reservation_cancel_at_transaction_entry(history):
    history.submit()
    history.reserve_probe("probe.reserved")
    assert history.reservations == [("probe.reserved", True, set())]
    assert history.holder is None


def test_pending_cancel_with_running_attempt_refuses_repeated_probes(history):
    history.submit()
    history.route()
    history.reserve_attempt()
    assert len(history.service.store.list_attempts()) == 1
    history.reserve_probe("after-route")
    history.reserve_probe("after-route")
    current = history.service.store.get_job(history.job_id)
    assert current["state"] == "running" and current["cancel_requested_at"]
    assert history.reserved_holders(history.service.store) == set()
    assert history.guardians == history.gates == []


@pytest.mark.parametrize("cancel_at", ["before-gate", "probe.gate"])
def test_probe_gate_refuses_and_releases_after_cancel(history, cancel_at):
    history.submit()
    history.route()
    history.reserve_probe()
    holder = history.holder
    if cancel_at == "before-gate":
        history.cancel()
        cancel_at = None
    history.open_gate(cancel_at)
    assert history.gates == [(b"0", True)]
    assert len(history.guardians) == 1  # Guardian stayed behind the refused gate.
    assert history.service.store.list_leases(holder) == []
    states = [value["state"] for row in history.service.store.query(
        "SELECT data_json FROM events WHERE kind='probe.state' ORDER BY rowid")
        if (value := json.loads(row["data_json"])).get("state")]
    assert states == ["reserved", "starting", "contained", "completed"]


@pytest.mark.parametrize("change", ["stamp", "succeeded", "failed", "cancelled", "lost"])
def test_probe_gate_rechecks_terminal_or_stamp(history, change):
    history.submit()
    history.route()
    history.reserve_probe()
    if change == "stamp":
        history.service.store.update_job(history.job_id, cancel_requested_at=module.utcnow())
    else:
        history.service.store.update_job(history.job_id, state=change)
    history.open_gate()
    assert history.gates == [(b"0", True)]


def test_uncancelled_probe_gate_opens(history):
    history.submit()
    history.route()
    history.reserve_probe()
    history.open_gate()
    assert history.gates == [(b"1", False)]


def test_refused_probe_gate_retains_uncertain_lease(history, monkeypatch):
    history.submit()
    history.route()
    history.reserve_probe()
    history.cancel()
    monkeypatch.setattr(module.procs, "containment", lambda *a, **k: module.procs.Containment(unverifiable=True))
    history.open_gate(contained=False)
    assert history.gates == [(b"0", True)]
    assert history.service.store.get_job(history.job_id)["state"] == "cancelled"


@pytest.mark.parametrize("kind", ["dispatch", "pilot", "resume", "revive", "gate-review", "turn"])
def test_shared_attempt_reservation_rechecks_cancel(history, kind):
    history.submit()
    # The shared reservation guard precedes kind-specific lease/launch work.
    # Native-session and review-bundle validation are covered by their own tests.
    history.service.store.update_job(history.job_id, kind="dispatch" if kind == "pilot" else kind)
    if kind == "pilot":
        history.service.policy["admission"]["prove_idle_s"] = 900
    if kind == "turn":
        history.patch.setattr(history.service.conversations, "admission_hold", lambda job: None)
    if kind == "resume":
        path = history.root / "jobs" / history.job_id / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["resume"] = {"native_session_id": "cancel-test-session"}
        path.write_text(json.dumps(manifest))
    history.reserve_attempt("attempt.reserved")
    assert history.reservations == [("attempt.reserved", True, set())]
    assert history.service.store.list_attempts() == []


class ProbeCancelMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.directory = tempfile.TemporaryDirectory(prefix="sf-cancel-machine-", dir=os.environ["TMPDIR"])
        self.patch = pytest.MonkeyPatch()
        self.history = AdmissionHistory(Path(self.directory.name) / "state", self.patch)

    @initialize(reserved=st.booleans())
    def initial_state(self, reserved):
        # Reach both sides of the reservation boundary through real operations,
        # so cancellation cannot make every generated gate assertion vacuous.
        if reserved:
            self.submit()
            self.route()
            self.reserve_probe(None)

    @precondition(lambda self: self.history.job_id is None)
    @rule()
    def submit(self):
        event("operation=submit")
        self.history.submit()

    @precondition(lambda self: self.history.job_id is not None and self.history.routed is None)
    @rule()
    def route(self):
        event("operation=route")
        self.history.route()

    @precondition(lambda self: self.history.job_id is not None)
    @rule()
    def cancel(self):
        event("operation=cancel")
        self.history.cancel()

    @precondition(lambda self: self.history.routed is not None and self.history.holder is None)
    @rule(cancel_at=st.sampled_from([None, "after-route", "probe.reserved"]))
    def reserve_probe(self, cancel_at):
        event("operation=reserve_probe")
        self.history.reserve_probe(cancel_at)

    @precondition(lambda self: self.history.holder is not None)
    @rule(cancel_at=st.sampled_from([None, "probe.gate"]))
    def open_gate(self, cancel_at):
        event("operation=open_gate")
        self.history.open_gate(cancel_at)

    @precondition(lambda self: self.history.holder is not None)
    @rule()
    def contain(self):
        event("operation=contain")
        self.history.contain()

    @rule()
    def restart(self):
        event("operation=restart")
        self.history.restart()

    @precondition(lambda self: self.history.routed is not None and self.history.holder is None)
    @rule(cancel_at=st.sampled_from([None, "attempt.reserved"]))
    def reserve_attempt(self, cancel_at):
        event("operation=reserve_attempt")
        self.history.reserve_attempt(cancel_at)

    @invariant()
    def cancelled_jobs_reserve_nothing_and_open_no_gate(self):
        assert all(not blocked or value != b"1" for value, blocked in self.history.gates)
        assert all(not blocked or not added for _, blocked, added in self.history.reservations)

    def teardown(self):
        try:
            self.history.close()
        finally:
            self.patch.undo()
            self.directory.cleanup()


TestProbeCancelMachine = ProbeCancelMachine.TestCase
TestProbeCancelMachine.settings = settings(
    max_examples=60, stateful_step_count=25, deadline=None, derandomize=True, database=None)
