"""C-4.1, C-6.9–C-6.12, C-11.2: stateful property tests of the admission pass.

Hypothesis drives one in-process daemon (the `state_daemon` shape: fake
adapters, stubbed process identity, no guardian, no provider) on a virtual
clock through random sequences of lanes enrolled and disabled, sensor readings
fresh and stale, closures, probe reservations, lane pins (a lane id, a name two
lanes answer to, a name no lane answers to), jobs in every tier, attempts that
end `ok`, `transient` or `limited`, restarts, and sensors that fail. After each
admission pass it checks what the pass left against the contract rather than
against the code that produced it:

- P1 totality (C-6.11, C-6.12): no error from one job's route ends the pass,
  and every job the pass leaves pending has a stated reason.
- P2 non-interference (C-6.9, C-11.2): `behind-older-job` names an older job of
  the same tier that competes with the held job, by the models routing walks
  and the lanes the pins name; a younger job that competes with a waiting job
  is not placed past it.
- P3 bounded progress (C-6.9): under fair capacity every job some lane would
  take is placed within a bounded number of passes unless an older job that
  competes with it can never be placed, and the fleet stays within its caps.
- P4 recheck cost (C-6.10): a look that repeats its verdict adds no decision
  row and sets a later clock, so a quiet fleet looks at a waiting job a bounded
  number of times however long it waits.

Incidents: 2026-09-20 (034d3d3: the `standard` tier held behind an Opus head on
`reserve:fable:unmeasured` beside eleven free Fable lanes for three hours;
cb83e1b: capacity waits rechecked every second, 98,014 decision rows),
2026-09-22 (de95879: three Fable gate reviews pinned to three lanes held each
other for six hours; b841d0d: one unroutable pin raised inside the pass and
stalled the fleet for 198 and 26 minutes), and 2026-09-24 (`admission: 5 jobs
pending, none placed for 4863 s; 14 lanes open`).

Settings profiles, chosen with `HYPOTHESIS_PROFILE`: `ci` (the default when
`CI` is set: derandomized and modest), `dev` (the default otherwise), and
`deep` for a long local search. A failure prints the steps that reproduce it.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, Phase, event, note, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, precondition, rule

from subfleet import actions as actions_module
from subfleet import capacity, protocol, scheduler
from subfleet import daemon as daemon_module
from subfleet import policy as policy_module
from subfleet import store as store_module
from subfleet import timers as timers_module
from subfleet.adapters import registry
from subfleet.adapters.base import AdapterError
from subfleet.contracts import (ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner,
                                Outcome, OutcomeClass, Reading, ReadingLabel, attempt_dir)
from subfleet.daemon import Daemon
from subfleet.policy import PolicyError, resolve_model
from subfleet.procs import Containment
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter

settings.register_profile("ci", max_examples=40, stateful_step_count=30, derandomize=True,
                          database=None, deadline=None, print_blob=True,
                          suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])
settings.register_profile("dev", max_examples=60, stateful_step_count=40, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])
settings.register_profile("deep", max_examples=1000, stateful_step_count=80, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])
PROFILE = settings.get_profile(os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else "dev"))
if os.environ.get("HYPOTHESIS_NO_SHRINK"):
    # A stateful example here takes up to a second, so shrinking one can take minutes:
    # this reports the first failure as found (rerun its seed without it to shrink).
    PROFILE = settings(PROFILE, phases=[Phase.explicit, Phase.reuse, Phase.generate])

START = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
TIERS = (None, "trivial", "easy", "standard", "hard")
MODELS = ("fable", "opus", "sonnet", "haiku", "astra", "terra")
TASKS = ("review", "sweep", "authored-prose")
NAMES = ("a@example.invalid", "b@example.invalid", "shared@example.invalid")
UNKNOWN_PIN = "nobody@example.invalid"
#: C-6.12: what one job's route may raise without ending the pass, and the job's own error.
ROUTE_FAULTS = {
    "ValueError": lambda: ValueError("injected: bad reset clock"),
    "KeyError": lambda: KeyError("utilization"),
    "TypeError": lambda: TypeError("injected: unorderable reading"),
    "AttributeError": lambda: AttributeError("injected: 'list' object has no attribute 'get'"),
    "IndexError": lambda: IndexError("injected: chain index out of range"),
    "RouteError": lambda: scheduler.RouteError("pinned_lane: injected names 2 lanes"),
}
#: One job as a caller submits it: every tier (none is `standard`), a model, a task, or both, and a
#: pin by index into the names lanes answer to (lane ids, labels, probe emails, one nobody answers to).
JOB = st.fixed_dictionaries({
    "tier": st.one_of(st.just("standard"), st.sampled_from(TIERS)),
    "model": st.sampled_from((None,) + MODELS), "task": st.sampled_from((None,) + TASKS),
    "pin": st.none() | st.integers(0, 15), "legacy": st.booleans(), "authorize": st.booleans(),
    # C-6.12: a stored exclusion that is not a lane name (submit refuses one now; an older row may hold one).
    "exclusions": st.sampled_from(("[]", '["a@example.invalid"]', "[1]", "[null]")),
})
#: One change in capacity, as (kind, lane index, details).
CAPACITY = st.one_of(
    st.tuples(st.just("reading"), st.integers(0, 7), st.sampled_from(("account", "fable", "model")),
              st.sampled_from(("seven_day", "five_hour")), st.sampled_from((0.0, 0.2, 0.6, 0.9, 1.0)),
              st.sampled_from((0, 30, 119, 121, 900)), st.none() | st.sampled_from((20, 3600, 86400)),
              st.sampled_from(("oauth-usage", "wham"))),
    st.tuples(st.just("closure"), st.integers(0, 7), st.sampled_from(("account", "model")),
              st.sampled_from((5, 60, 600, 3600, 86400)), st.integers(0, 5)),
    st.tuples(st.just("lift"), st.integers(0, 7)),
    st.tuples(st.just("probe"), st.integers(0, 7)),
    st.tuples(st.just("probes-end"), st.integers(0, 7)),
    st.tuples(st.just("toggle"), st.integers(0, 7), st.booleans()),
    st.tuples(st.just("enroll"), st.integers(0, 7), st.sampled_from(("claude", "codex")),
              st.sampled_from((None,) + NAMES)),
)
#: One failure or repair.
FAILURE = st.one_of(
    st.tuples(st.just("route"), st.sampled_from(("evaluate", "probe_required")), st.sampled_from(sorted(ROUTE_FAULTS))),
    st.tuples(st.just("workspace")),
    st.tuples(st.just("probe"), st.sampled_from(("ok", "limited", "unknown", "adapter-error", "os-error",
                                                  "quarantined"))),
    st.tuples(st.just("garbage"), st.integers(0, 7)),
    st.tuples(st.just("repair")),
)
#: C-4.1: why a waiting job waits.
WAIT_REASONS = {"capacity", "dependency", "approval", "uncertain", "workspace", "route"}
#: C-4.1, C-6.9: holds of jobs that hold back nobody else in their tier.
HOLD_NOBODY = {"route", "approval", "uncertain", "workspace", "attempt-live"}
LIVE = ("reserved", "starting", "running", "finalizing")


def iso(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


class Clock:
    """The one clock every admission module reads, so a step can let an hour pass.

    C-6.10's backoff, C-6.12's deferrals, reading freshness and closure expiry are
    all whole-second stamps against `datetime.now`; the existing tests race the
    wall clock across a second boundary, and this one never does.
    """

    def __init__(self):
        self.instant = START
        clock = self

        class VirtualDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.instant.astimezone(tz) if tz else clock.instant.replace(tzinfo=None)

        self.datetime = VirtualDatetime

    def iso(self, seconds: float = 0) -> str:
        return iso(self.instant + timedelta(seconds=seconds))

    def advance(self, seconds: float) -> None:
        self.instant += timedelta(seconds=seconds)


class ControlledAdapter(FakeAdapter):
    """The fake provider, classifying each finished attempt as the machine says."""

    def __init__(self, machine: "AdmissionMachine"):
        self.machine = machine

    def classify(self, attempt_dir, launch, exit_info):
        return self.machine.next_outcome


# --- the contract's own definitions, computed from the job rows (C-6.9, C-11.2) ------------------

def tier_of(policy, job) -> str:
    return job["tier"] or ("standard" if "standard" in policy["tiers"] else policy["tiers"][0])


def walked_models(policy, job) -> frozenset[str] | None:
    """C-6.9, C-11.2: the models routing walks for this job; None when they cannot be told.

    A `-m` pin is one model. A task is its chain from its tier upward, except that
    a lane-pinned job "evaluates one model, its -m model or else the first model of
    its task's chain from its tier" (C-11.2), and C-6.9 says a task job's models are
    "exactly the chain routing walks". A lane pin with neither cannot be told.
    """
    try:
        if job["pinned_model"]:
            return frozenset({resolve_model(policy, job["pinned_model"], note=False)})
        if job["task"] in policy["chains"]:
            chain = policy["chains"][job["task"]][policy["tiers"].index(tier_of(policy, job)):]
            return frozenset(chain[:1] if job["pinned_lane"] else chain)
    except (PolicyError, ValueError, KeyError):
        return None
    return None


def job_provider(policy, job) -> str | None:
    models = walked_models(policy, {**job, "pinned_lane": job["pinned_lane"] or "pinned"})
    return policy["models"][next(iter(models))]["provider"] if models else None


def lane_names(lane) -> set[str]:
    """C-11.2: every name a lane answers to (a Codex lane also to its probe's email)."""
    account = str(lane.get("account_key") or "")
    return {str(value) for value in (lane.get("lane_id"), account, account.partition(":")[2],
                                     lane.get("label"), lane.get("email"), lane.get("home")) if value}


def pinned_lanes(roster, policy, job) -> frozenset[str] | None:
    """C-6.9, C-11.2: the one lane a pin names, or None for no pin or a pin that names no one lane.

    The roster here never re-enrols a credential or latches a mismatch, so a lane
    id names itself and the only narrowing is by the job's provider.
    """
    pin = job["pinned_lane"]
    if not pin:
        return None
    if any(lane["lane_id"] == pin for lane in roster):
        return frozenset({pin})
    matches = [lane for lane in roster if pin in lane_names(lane)]
    provider = job_provider(policy, job)
    if len(matches) > 1 and provider:
        matches = [lane for lane in matches if lane["provider"] == provider] or matches
    return frozenset({matches[0]["lane_id"]}) if len(matches) == 1 else None


def wait_kind(wait: dict | None) -> str | None:
    """What kind of wait admission keyed a look's record by (`Daemon._capacity_wait`)."""
    signature = (wait or {}).get("signature") or ""
    for kind in ("probe-wait:", "probe-pending", "lease-held:", "retry-let-go:"):
        if signature.startswith(kind):
            return kind.rstrip(":")
    return "verdict" if signature else None


def compete(one, other) -> bool:
    """C-6.9: some model could serve both and some lane could serve both; unknown overlaps."""
    (models, lanes), (other_models, other_lanes) = one, other
    if models is not None and other_models is not None and not models & other_models:
        return False
    return lanes is None or other_lanes is None or bool(lanes & other_lanes)


class AdmissionMachine(RuleBasedStateMachine):
    """One fleet, one daemon, one virtual clock; the rules change the world and run passes."""

    def __init__(self):
        super().__init__()
        self.patch = pytest.MonkeyPatch()
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-admit-"))
        self.clock = Clock()
        self.service: Daemon | None = None
        self.seq: dict[str, int] = {}               # job id -> submission order (C-6.9 "older")
        self.metadata: dict[str, dict] = {}         # what probes reported, merged into the view (C-11.2)
        self.faults: dict[tuple[str, str], str] = {}     # (seam, job id) -> exception name (C-6.12)
        self.workspace_faults: set[str] = set()
        self.probe_mode = "ok"
        self.next_outcome: Outcome | None = None
        self.looks: set[str] = set()                # jobs whose workspace this pass prepared: a look
        self.last_look: dict[str, dict] = {}        # job id -> the hold its last look reported
        self.decided: dict[str, object] = {}        # job id -> the decision this pass's look reached
        self.last_verdict: dict[str, str] = {}      # job id -> C-6.10's verdict at its last look
        self.quarantine = False                     # probe containment cannot be verified (C-5.5)
        self.passes = 0
        for module in (daemon_module, scheduler, capacity, store_module, timers_module,
                       policy_module, actions_module):
            self.patch.setattr(module, "datetime", self.clock.datetime)
        self.patch.setattr(daemon_module.procs, "boot_id", lambda: "stateful-boot")
        self.patch.setattr(daemon_module.procs, "proc_start", lambda pid: "stateful-start")
        self.patch.setattr(daemon_module.procs, "same_process", lambda *args: False)
        self.patch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment())
        self.patch.setattr(capacity, "read_desktop_account", lambda path=None: None)
        factory = lambda: ControlledAdapter(self)        # noqa: E731
        self.patch.setattr(registry, "_factories", {"codex": factory, "claude": factory})
        real_evaluate, real_probe_required = scheduler.evaluate, scheduler.probe_required

        def evaluate(policy, view, job):
            fault = self.faults.get(("evaluate", dict(job).get("job_id")))
            if fault:
                raise ROUTE_FAULTS[fault]()
            return real_evaluate(policy, view, job)

        def probe_required(decision, job):
            fault = self.faults.get(("probe_required", dict(job).get("job_id")))
            if fault:
                raise ROUTE_FAULTS[fault]()
            return real_probe_required(decision, job)

        self.patch.setattr(scheduler, "evaluate", evaluate)
        self.patch.setattr(scheduler, "probe_required", probe_required)

    def teardown(self):
        try:
            if self.service is not None:
                self.service.close()
                self.harness.check_notices()             # C-15.1: every notice agrees with its job row
        finally:
            self.patch.undo()
            shutil.rmtree(self.tmp, ignore_errors=True)

    # --- the daemon -----------------------------------------------------------------------------

    def start(self) -> Daemon:
        service = Daemon(self.root)

        def refuse_launch(*args):
            raise AssertionError("the admission machine never launches a guardian")

        def workspace(job):
            # A look starts here: C-6.10's cost is this preparation and the scoring after it.
            self.looks.add(job["job_id"])
            if job["job_id"] in self.workspace_faults:
                raise OSError(errno.EAGAIN, "injected: workspace temporarily unavailable")
            assert job["sandbox"] == "read-only", "the machine submits read-only jobs only"
            return job.get("worktree") or job["workdir"], None, None

        real_pick = service._pick

        def pick(job, **options):
            decision = real_pick(job, **options)
            self.decided[job["job_id"]] = decision
            return decision

        def probe_census(record):
            return Containment(unverifiable=True) if self.quarantine else Containment()

        def execute_probe(job, lane, model, holder):
            if self.probe_mode == "quarantined":
                # What `_execute_probe` returns when `_await_probe` finds the probe's
                # processes cannot be accounted for: the record and the job are quarantined.
                self.quarantine = True
                assert not service._contain_probe(service._probe_record(holder))
                return Outcome(OutcomeClass.UNKNOWN, "probe containment is quarantined",
                               evidence={"probe_quarantined": True})
            if self.probe_mode == "adapter-error":
                raise AdapterError("injected: probe could not run")
            if self.probe_mode == "os-error":
                raise OSError(errno.EIO, "injected: probe I/O error")
            cls = {"ok": OutcomeClass.OK, "limited": OutcomeClass.LIMITED,
                   "unknown": OutcomeClass.UNKNOWN}[self.probe_mode]
            return Outcome(cls, f"injected probe {self.probe_mode}")

        service._launch = refuse_launch
        service._workspace = workspace
        service._execute_probe = execute_probe
        service._pick = pick
        service._probe_census = probe_census
        service.term_grace_s = 0
        for lane_id, meta in self.metadata.items():
            service.timers.metadata[lane_id] = dict(meta)
        return service

    @property
    def store(self):
        return self.service.store

    @property
    def policy(self):
        return self.service.policy

    def roster(self) -> list[dict]:
        return [{**dict(row), **self.metadata.get(row["lane_id"], {})} for row in self.store.lane_rows()]

    def jobs(self) -> dict[str, dict]:
        return {row["job_id"]: dict(row) for row in self.store.query("SELECT * FROM jobs")}

    def pending(self, jobs) -> dict[str, dict]:
        return {job_id: job for job_id, job in jobs.items()
                if job["state"] in ("queued", "waiting") and not job["cancel_requested_at"]}

    def live(self) -> list[dict]:
        return [dict(row) for row in self.store.query(
            "SELECT * FROM attempts WHERE state IN ('reserved','starting','running','finalizing')")]

    def put_lane(self, provider: str, label: str | None) -> str:
        number = 1 + sum(1 for lane in self.store.lane_rows() if lane["provider"] == provider)
        lane_id = f"{provider}-{number}"
        self.store.put_lane(Lane(lane_id, provider, f"{provider}:{lane_id}@lanes.invalid",
                                 Credential(provider, f"/fake/{lane_id}", "home"), f"/fake/{lane_id}",
                                 LaneOwner.V2, False, True, None, label))
        return lane_id

    @initialize(cap=st.integers(1, 4), per_lane=st.integers(1, 2), reserve=st.booleans(),
                claude=st.lists(st.sampled_from((None,) + NAMES), min_size=0, max_size=2),
                codex_email=st.sampled_from((None,) + NAMES), second_codex=st.booleans(),
                capacity=st.lists(CAPACITY, max_size=4), queue=st.lists(JOB, max_size=6))
    def fleet(self, cap, per_lane, reserve, claude, codex_email, second_codex, capacity, queue):
        """A fleet (its caps, the Fable reserve on or off, lanes whose names may collide), the
        capacity it starts with, and a queue submitted before the first pass sees any of it."""
        self.root = self.tmp / "state"
        self.root.mkdir()
        self.harness = Harness(self.root)
        policy = json.loads((self.root / "policy.json").read_text())
        policy["caps"].update(max_active_attempts=cap, max_in_flight_per_lane=per_lane)
        policy["reserve"]["models"] = ["fable"] if reserve else []
        (self.root / "policy.json").write_text(json.dumps(policy, indent=2) + "\n")
        self.service = self.start()
        for label in claude:
            self.put_lane("claude", label)
        if second_codex:
            self.put_lane("codex", None)
        if codex_email:
            # C-11.2, the 2026-09-22 roster: a Codex lane answers to the email its usage probe read.
            self.report_email("codex-1", codex_email)
        note(f"fleet: cap={cap} per_lane={per_lane} reserve={reserve} lanes="
             f"{[(lane['lane_id'], lane['label']) for lane in self.roster()]} codex-1 email={codex_email}")
        for change in capacity:
            self.change_capacity(change)
        for job in queue:
            self.submit_one(job)

    def report_email(self, lane_id: str, email: str) -> None:
        self.metadata[lane_id] = {**self.metadata.get(lane_id, {}), "email": email}
        self.service.timers.metadata[lane_id] = dict(self.metadata[lane_id])

    # --- the world ------------------------------------------------------------------------------

    def lane_at(self, index: int) -> dict:
        lanes = sorted(self.roster(), key=lambda lane: lane["lane_id"])
        return lanes[index % len(lanes)]

    def pin_names(self) -> list[str]:
        names = sorted(lane["lane_id"] for lane in self.roster())
        names += sorted({name for lane in self.roster() for name in (lane.get("label"), lane.get("email")) if name})
        return names + [UNKNOWN_PIN]

    def pending_at(self, index: int) -> str | None:
        pending = sorted(self.pending(self.jobs()), key=self.seq.get)
        return pending[index % len(pending)] if pending else None

    def submit_one(self, spec: dict) -> None:
        """Submit one job. A legacy pin is written as it was typed, as a job accepted before C-11.2's
        canonical pins carries it: a name several lanes answer to, or one no lane answers to."""
        names = self.pin_names()
        name = names[spec["pin"] % len(names)] if spec["pin"] is not None else None
        model, task, legacy = spec["model"], spec["task"], spec["legacy"]
        if model is None and task is None and name is None:
            task = "review"
        changes = {"pinned_model": model, "task": task, "tier": spec["tier"]}
        if spec["authorize"] and name and model and not legacy:
            changes["unmeasured_reserve_reason"] = "stateful fixture: operator authorizes; quota unverified"
        submitted = changes if not legacy else {**changes, "pinned_model": model or (None if task else "astra")}
        exclusions = json.loads(spec["exclusions"])
        try:
            job_id = self.service.dispatch("submit", self.harness.submit_args(
                **submitted, pinned_lane=None if legacy else name, exclusions=[] if legacy else exclusions))["job_id"]
        except (protocol.ProtocolError, AdapterError) as exc:
            event(f"submit refused: {type(exc).__name__}")
            note(f"submit {changes} pin={name!r} refused: {exc}")
            return
        assert all(isinstance(value, str) for value in exclusions) or legacy, \
            f"C-6.12: submit accepted exclusions {exclusions} that are not lane names"
        if legacy:
            self.store.update_job(job_id, pinned_model=model, pinned_lane=name, exclusions=spec["exclusions"])
        self.seq[job_id] = len(self.seq)
        note(f"submitted {job_id}: {changes} pin={name!r} exclusions={spec['exclusions']}"
             f"{' (legacy)' if legacy else ''}")

    @rule(jobs=st.lists(JOB, min_size=1, max_size=3), tick=st.booleans())
    def submit(self, jobs, tick):
        """Jobs arrive together (a batch, or submissions inside one tick)."""
        for job in jobs:
            self.submit_one(job)
        if tick:
            self.admission_pass()

    def change_capacity(self, change: tuple) -> None:
        kind, lane_index, *rest = change
        lane = self.lane_at(lane_index)
        lane_id, provider = lane["lane_id"], lane["provider"]
        ids = [model["id"] for model in self.policy["models"].values() if model["provider"] == provider]
        if kind == "reading":
            # A usage sensor reports (C-9.1), perhaps already stale (reading_ttl_s is 120 s).
            scope, window, utilization, age, resets_in, source = rest
            scope = {"account": "account", "fable": self.policy["models"]["fable"]["id"]}.get(scope, ids[0])
            self.store.add_reading(Reading(lane_id, scope, window, utilization,
                                           self.clock.iso(resets_in) if resets_in else None,
                                           ReadingLabel.PROVIDER, source, self.clock.iso(-age)))
        elif kind == "closure":
            # A provider limit closes a lane for its account or for one model (C-9.6).
            scope, seconds, model = rest
            scope = "account" if scope == "account" else ids[model % len(ids)]
            self.store.add_closure(Closure(lane_id, scope, self.clock.iso(seconds), ClosureReason.PROVIDER_LIMIT,
                                           ClockSource.REPORTED, "fixture"))
        elif kind == "lift":
            # An operator lifts a lane's closures (C-6.10: seen at the next recheck).
            with self.store.transaction("fixture.closures_lifted") as tx:
                tx.execute("UPDATE closures SET released_at=? WHERE lane_id=? AND released_at IS NULL",
                           (self.clock.iso(), lane_id))
        elif kind == "probe":
            # A probe cycle (C-18.1) holds a lane's first slot; its reservation counts toward the fleet cap.
            self.store.acquire_lease(f"lane:{lane_id}:slot:0", f"probe:stateful-{self.passes}-{lane_id}")
        elif kind == "probes-end":
            with self.store.transaction("fixture.probes_released") as tx:
                tx.execute("DELETE FROM leases WHERE holder LIKE 'probe:stateful-%'")
        elif kind == "toggle":
            self.store.update_lane(lane_id, enabled=int(rest[0]))
        elif kind == "enroll":
            # A lane appears (C-6.10: capacity that appears is seen at the next recheck).
            new_provider, label = rest
            if len(self.store.lane_rows()) < 6:
                new = self.put_lane(new_provider, None if new_provider == "codex" else label)
                if new_provider == "codex" and label:
                    self.report_email(new, label)
        note(f"capacity: {change} on {lane_id}")

    @rule(change=CAPACITY, tick=st.booleans())
    def capacity_changes(self, change, tick):
        self.change_capacity(change)
        if tick:
            self.admission_pass()

    @rule(failure=FAILURE, job=st.integers(0, 15), tick=st.booleans())
    def sensors_fail(self, failure, job, tick):
        """A sensor or a seam fails, or is repaired: C-6.12's route faults (bad data, a policy edit, a
        defect) at `evaluate` or `probe_required`, C-6.8's workspace, an admission probe (C-11.4) that
        is limited, inconclusive or cannot run, and a usage sensor that stores a reset clock that does
        not parse (bad capacity data is nobody's job's fault, so every job it touches waits on `route`)."""
        kind, *rest = failure
        job_id = self.pending_at(job)
        if kind == "route" and job_id:
            self.faults[(rest[0], job_id)] = rest[1]
        elif kind == "workspace" and job_id:
            self.workspace_faults.add(job_id)
        elif kind == "probe":
            self.probe_mode = rest[0]
        elif kind == "garbage":
            lane = self.lane_at(rest[0])
            self.store.add_reading(Reading(lane["lane_id"], "account", "seven_day", 0.2, "not-a-timestamp",
                                           ReadingLabel.PROVIDER, "wham", self.clock.iso()))
        elif kind == "repair":
            self.faults.clear()
            self.workspace_faults.clear()
            self.probe_mode, self.quarantine = "ok", False
            with self.store.transaction("fixture.readings_repaired") as tx:
                tx.execute("DELETE FROM readings WHERE resets_at='not-a-timestamp'")
        note(f"failure: {failure} ({job_id})")
        if tick:
            self.admission_pass()

    @precondition(lambda self: self.service is not None and bool(self.live()))
    @rule(attempt=st.integers(0, 7), outcome=st.sampled_from(("ok", "transient", "limited")),
          scope=st.sampled_from(("account", "model")), tick=st.booleans())
    def attempt_ends(self, attempt, outcome, scope, tick):
        """A live attempt ends and is finalized as the daemon finalizes one (C-4.3, C-4.5)."""
        attempts = sorted(self.live(), key=lambda row: row["attempt_id"])
        self.finish(attempts[attempt % len(attempts)], outcome, scope)
        if tick:
            self.admission_pass()

    def finish(self, attempt: dict, outcome: str, scope: str = "account") -> None:
        closure = None
        if outcome == "limited":
            closure = Closure(attempt["lane_id"], "account" if scope == "account" else attempt["model_requested"],
                              self.clock.iso(3600), ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "fixture")
        self.next_outcome = Outcome({"ok": OutcomeClass.OK, "transient": OutcomeClass.TRANSIENT,
                                     "limited": OutcomeClass.LIMITED}[outcome], f"fixture {outcome}", closure=closure)
        adir = attempt_dir(self.service.root, attempt["job_id"], attempt["seq"])
        adir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name, contents in (("stdout", b"fixture result\n"), ("stderr", b""), ("lane.log", b"")):
            (adir / name).write_bytes(contents)
        receipt = {"rc": {"ok": 0, "transient": 1, "limited": 4}[outcome], "signal": None, "wall_s": .1,
                   "child_pid": None, "finished_at": self.clock.iso()}
        (adir / "exit.json").write_text(json.dumps(receipt))
        self.service._pending_launches.discard(attempt["attempt_id"])
        self.service._begin_finalizing(attempt, receipt)
        self.service._finalize(self.store.get_attempt(attempt["attempt_id"]))
        self.last_look.pop(attempt["job_id"], None)
        note(f"attempt {attempt['attempt_id']} on {attempt['lane_id']} ended {outcome}")

    @rule(job=st.integers(0, 15), tick=st.booleans())
    def cancel(self, job, tick):
        job_id = self.pending_at(job)
        if job_id:
            self.service.dispatch("kill", {"job_id": job_id})
        if tick:
            self.admission_pass()

    @rule()
    def restart(self):
        """The daemon restarts: C-6.10's records and C-6.12's counts are forgotten, recovery makes
        every capacity and route wait due, and C-11.2 canonicalizes the pins it can."""
        self.service.close()
        self.service = self.start()
        self.service._canonicalize_pins_once()
        self.service._recover_capacity_waits()
        self.last_look.clear()
        self.last_verdict.clear()           # C-6.10: "a restart therefore forgets it"
        note("restarted")

    @rule(seconds=st.sampled_from((1, 2, 5, 16, 31, 61, 121, 301, 3601)))
    def time_passes(self, seconds):
        """Time passes and the next tick runs a pass."""
        self.clock.advance(seconds)
        self.admission_pass()

    # --- the pass and what it must leave ---------------------------------------------------------

    @precondition(lambda self: self.service is not None)
    @rule()
    def admission_pass(self) -> None:
        before = self.jobs()
        decisions = self.decision_counts()
        attempts = {row["attempt_id"] for row in self.store.list_attempts()}
        live_before = len(self.live())
        self.looks, self.decided = set(), {}
        try:
            self.service._admit()
        except Exception as exc:
            raise AssertionError(f"P1, C-6.12: the admission pass raised {type(exc).__name__}: {exc}") from exc
        self.passes += 1
        after = self.jobs()
        placed = [row for row in self.store.list_attempts() if row["attempt_id"] not in attempts]
        for row in placed:
            self.last_verdict.pop(row["job_id"], None)   # C-6.10: the record is dropped when the job is placed
        for hold in self.service._holds.values():
            event(f"hold: {hold['reason'].split(':')[0]}")
        event(f"pass placed {min(len(placed), 3)}+ with {min(len(self.pending(after)), 4)}+ pending")
        self.check_totality(before, after, placed)
        self.check_non_interference(before, after, placed, live_before)
        self.check_caps()
        self.check_recheck_cost(before, after, decisions)

    def decision_counts(self) -> Counter:
        return Counter(row["job_id"] for row in self.store.query(
            "SELECT job_id FROM decisions WHERE attempt_id IS NULL"))

    def check_totality(self, before, after, placed) -> None:
        """P1 (C-4.1, C-6.11, C-6.12): every job left pending has a stated reason."""
        holds, cap = self.service._holds, self.policy["caps"]["max_active_attempts"]
        placed_jobs = {row["job_id"] for row in placed}
        for job_id, job in self.pending(after).items():
            assert job_id not in placed_jobs
            if job["state"] == "waiting":
                assert job["wait_reason"] in WAIT_REASONS and job["next_check_at"], \
                    f"C-4.1: waiting job {job_id} has wait_reason {job['wait_reason']!r}, next_check_at {job['next_check_at']!r}"
            hold = holds.get(job_id)
            assert hold and isinstance(hold.get("reason"), str) and hold["reason"], \
                f"P1, C-6.11: {job_id} ({job['state']}, {job['wait_reason']}) was left pending with no stated reason: {hold!r}"
            reason = hold["reason"]
            required = {"behind-older-job": {"behind", "tier"}, "fleet-full": {"max_active_attempts"},
                        "slot-kept": {"kept_for", "live", "max_active_attempts", "tier"},
                        "lease-held": {"leases"}, "route": {"error_type", "error", "deferrals", "next_check_at"}}
            missing = required.get(reason, set()) - set(hold)
            assert not missing, f"C-6.11: the {reason} hold on {job_id} lacks {sorted(missing)}: {hold}"
            if job["wait_reason"] in ("approval", "uncertain", "workspace"):
                assert reason == job["wait_reason"], (
                    f"C-6.11: {job_id} waits on {job['wait_reason']!r}, which is reported as that "
                    f"whatever else holds it, but its hold reads {hold}")
            if reason in ("fleet-full", "slot-kept"):
                assert hold["max_active_attempts"] == cap
            if reason == "route":
                assert job["wait_reason"] == "route" and hold["next_check_at"] == job["next_check_at"]
            if job["wait_reason"] == "route" and parse(job["next_check_at"]) > self.clock.instant:
                assert reason == "route", f"C-6.12: {job_id} waits on its route but its hold reads {hold}"
            if job_id in self.looks:
                self.last_look[job_id] = {key: value for key, value in hold.items() if key != "next_check_at"}
            elif (job_id in self.last_look and "next_check_at" in hold and reason not in HOLD_NOBODY
                  and before[job_id]["state"] == "waiting" and before[job_id]["wait_reason"] == "capacity"
                  and job["next_check_at"] == before[job_id]["next_check_at"]):
                # C-6.11: "a pass that does not look at the job repeats the last hold in full".
                repeated = {key: value for key, value in hold.items() if key != "next_check_at"}
                assert repeated == self.last_look[job_id], \
                    f"C-6.11: {job_id}'s hold degraded between looks: {self.last_look[job_id]} -> {repeated}"
        for job_id in set(self.last_look) - set(self.pending(after)):
            self.last_look.pop(job_id)

    def demands(self, job, roster) -> list[tuple]:
        """What a job could be waiting for: its own demand, or the one retry pinned to its last pair (C-4.5)."""
        own = (walked_models(self.policy, job), pinned_lanes(roster, self.policy, job))
        attempts = self.store.list_attempts(job["job_id"])
        last = attempts[-1] if attempts else None
        if last and last["outcome_class"] == "transient" and sum(
                1 for row in attempts if row["outcome_class"] == "transient" and row["lane_id"] == last["lane_id"]) == 1:
            try:
                pair = (frozenset({resolve_model(self.policy, last["model_requested"], note=False)}),
                        frozenset({last["lane_id"]}))
            except (PolicyError, ValueError):
                pair = (None, frozenset({last["lane_id"]}))
            return [own, pair]
        return [own]

    def check_non_interference(self, before, after, placed, live_before) -> None:
        """P2 (C-4.1, C-6.9, C-11.2): a hold names an older competitor of the same tier, and a job is not
        placed past an older competitor that cannot be placed; passing a waiter leaves it a slot."""
        holds, roster, cap = self.service._holds, self.roster(), self.policy["caps"]["max_active_attempts"]
        pending = self.pending(after)
        demand = {job_id: self.demands(after[job_id], roster) for job_id in set(pending) | {row["job_id"] for row in placed}}
        for job_id, hold in holds.items():
            if hold.get("reason") != "behind-older-job" or job_id not in pending:
                continue
            older = hold["behind"]
            assert older in pending, f"P2: {job_id} is held behind {older}, which is no longer pending"
            assert tier_of(self.policy, after[older]) == tier_of(self.policy, after[job_id]) == hold["tier"], \
                f"P2, C-6.9: {job_id} ({after[job_id]['tier']}) is held behind {older} ({after[older]['tier']}) of another tier"
            assert self.seq[older] < self.seq[job_id], f"P2, C-6.9: {job_id} is held behind {older}, which is younger"
            assert holds[older]["reason"] not in HOLD_NOBODY, \
                f"P2, C-4.1: {job_id} is held behind {older}, whose own hold {holds[older]} holds back nobody"
            assert any(compete(mine, theirs) for mine in demand[job_id] for theirs in demand[older]), (
                f"P2, C-6.9: {job_id} is held behind {older} but they cannot compete: "
                f"{job_id} {self.describe(after[job_id], roster)} vs {older} {self.describe(after[older], roster)}")
        live = live_before
        for row in sorted(placed, key=lambda row: (
                self.policy["tiers"].index(tier_of(self.policy, after[row["job_id"]])), self.seq[row["job_id"]])):
            live += 1
            job_id = row["job_id"]
            waiting = [older for older in pending
                       if self.seq[older] < self.seq[job_id]
                       and tier_of(self.policy, after[older]) == tier_of(self.policy, after[job_id])
                       and holds.get(older, {}).get("reason") not in HOLD_NOBODY | {"behind-older-job", "waiting", None}]
            for older in waiting:
                assert not all(compete(mine, theirs) for mine in demand[job_id] for theirs in demand[older]), (
                    f"P2, C-6.9: {job_id} was placed past {older}, an older job of its tier that competes with it "
                    f"and could not be placed ({holds[older]})")
            if waiting:
                assert live <= cap - 1, (f"C-6.9: {job_id} passed waiting {waiting} and left no slot free "
                                         f"({live} live, max_active_attempts {cap})")

    def describe(self, job, roster) -> str:
        return (f"(tier={job['tier']}, task={job['task']}, model={job['pinned_model']}, pin={job['pinned_lane']}; "
                f"demands {self.demands(job, roster)})")

    def check_caps(self) -> None:
        """P3 (C-6.4): the fleet and every lane stay within their caps."""
        caps, live = self.policy["caps"], self.live()
        assert len(live) <= caps["max_active_attempts"], f"C-6.4: {len(live)} live attempts over the cap"
        per_lane = Counter(row["lane_id"] for row in live)
        assert all(count <= caps["max_in_flight_per_lane"] for count in per_lane.values()), per_lane

    def check_recheck_cost(self, before, after, decisions) -> None:
        """P4 (C-6.10): a look repeating its verdict adds no row; every look sets a later clock."""
        now, waits, counts = self.clock.instant, self.service._capacity_waits, self.decision_counts()
        pending = self.pending(after)
        for job_id in set(pending) - self.looks:
            assert counts[job_id] == decisions[job_id], f"C-6.10: {job_id} gained a decision row without a look"
        for job_id in self.looks & set(pending):
            job = after[job_id]
            assert job["state"] == "waiting" and job["next_check_at"] and parse(job["next_check_at"]) > now, \
                f"C-6.10: {job_id} was looked at and left {job['state']} with next_check_at {job['next_check_at']!r}"
            decision, wait = self.decided.get(job_id), waits.get(job_id)
            if decision is None or self.service._holds.get(job_id, {}).get("reason") in ("route", "workspace"):
                # A look that ended on its route or its workspace reached no capacity verdict (C-6.12, C-6.8).
                self.last_verdict.pop(job_id, None)
            else:
                verdict, kind = scheduler.verdict_signature(decision), wait_kind(wait)
                last = self.last_verdict.get(job_id)
                if counts[job_id] > decisions[job_id] and last and last[0] == verdict:
                    if last[1] != kind:
                        # KNOWN_WAIT_KIND_RESETS: xfailed in test_admission_properties.py.
                        event("known C-6.10 violation: a wait of another kind re-records the verdict")
                    else:
                        raise AssertionError(
                            f"C-6.10: {job_id}'s look reached the verdict its last look reached "
                            f"({decision.reason!r}) and added a decision row (hold {self.service._holds.get(job_id)})")
                self.last_verdict[job_id] = (verdict, kind)
            if job["wait_reason"] != "capacity" or not wait or parse(wait["checked_at"]) != now:
                continue
            if wait["rechecks"]:
                assert counts[job_id] == decisions[job_id], \
                    f"C-6.10: {job_id} repeated its verdict ({wait['rechecks']} rechecks) and added a decision row"
            ceiling = 60 if wait["signature"].startswith("probe-wait:") else scheduler.capacity_recheck_delay(wait["rechecks"])
            assert (parse(job["next_check_at"]) - now).total_seconds() <= ceiling, \
                f"C-6.10: {job_id}'s recheck is {job['next_check_at']}, later than {ceiling} s after its look"

    # --- P3 and P4 over many passes ---------------------------------------------------------------

    def boundaries(self, until: datetime) -> list[datetime]:
        """The instants at which capacity changes by itself: a reading going stale or resetting, a
        closure expiring. A verdict may change there, and C-6.10 checks a known reset on time."""
        ttl = self.policy["caps"]["reading_ttl_s"]
        instants = []
        for row in self.store.list_readings():
            try:
                instants.append(parse(row["observed_at"]) + timedelta(seconds=ttl))
                if row["resets_at"]:
                    instants.append(parse(row["resets_at"]))
            except ValueError:
                continue
        for row in self.store.list_closures():
            if not row["released_at"]:
                instants.append(parse(row["until_at"]))
        return [instant for instant in instants if self.clock.instant < instant <= until]

    @precondition(lambda self: self.service is not None)
    @rule(minutes=st.integers(2, 12))
    def quiet_fleet(self, minutes):
        """P4 (C-6.10): nothing changes but the clock. A waiting job is looked at when it is due, and
        its looks are bounded by the backoff: about six to reach 30 s, then two a minute, plus a few
        after each change the clock itself brings (a reading going stale, a closure expiring) and
        each placement (a verdict carries whether the fleet had room)."""
        end = self.clock.instant + timedelta(minutes=minutes)
        boundaries = len(self.boundaries(end))
        looks, placements, passes = Counter(), 0, 0
        while self.clock.instant < end and passes < 4 * 60 * minutes:
            placed_before = len(self.store.list_attempts())
            self.admission_pass()
            passes += 1
            placements += len(self.store.list_attempts()) - placed_before
            looks.update(self.looks)
            self.clock.advance(self.next_step(end))
        for job_id, count in looks.items():
            if job_id not in self.pending(self.jobs()):
                continue
            bound = 6 * (1 + boundaries + placements) + 2 * minutes + boundaries + 6
            assert count <= bound, (f"P4, C-6.10: {job_id} was looked at {count} times in {minutes} quiet minutes "
                                    f"(bound {bound}: {boundaries} clock boundaries, {placements} placements)")

    def next_step(self, end: datetime) -> float:
        """Seconds to the next instant a pass could do something: a job left due by its last look is
        looked at again on the next tick (1 s here), otherwise the next clock, boundary or 30 s."""
        now = self.clock.instant
        pending = self.pending(self.jobs())
        if any(job_id in pending and (not pending[job_id]["next_check_at"] or parse(pending[job_id]["next_check_at"]) <= now)
               for job_id in self.looks):
            return 1
        instants = [parse(job["next_check_at"]) for job in pending.values() if job["next_check_at"]]
        instants += self.boundaries(end)
        later = [instant for instant in instants if instant > now]
        step = (min(later) - now).total_seconds() if later else 30
        return max(1, min(30, step, (end - now).total_seconds()))

    def fair(self) -> None:
        """Every lane enabled, open, measured with room, and the Fable reserve slack; every sensor sound."""
        self.faults.clear()
        self.workspace_faults.clear()
        self.probe_mode, self.quarantine = "ok", False
        with self.store.transaction("fixture.fair") as tx:
            tx.execute("UPDATE closures SET released_at=? WHERE released_at IS NULL", (self.clock.iso(),))
            tx.execute("DELETE FROM leases WHERE holder LIKE 'probe:stateful-%'")
            tx.execute("DELETE FROM readings WHERE resets_at='not-a-timestamp'")
            tx.execute("UPDATE lanes SET enabled=1")
        for lane in self.roster():
            # A complete usage read with no reserved window: all of it is slack (C-11.7).
            self.store.add_reading(Reading(lane["lane_id"], "account", "seven_day", 0.1, self.clock.iso(86400 * 3),
                                           ReadingLabel.PROVIDER, "oauth-usage", self.clock.iso()))

    def placeable(self, job) -> str | None:
        """The lane admission would give this job now, with its retry exclusions and pin (C-4.5)."""
        _, exclusions, retry = self.service._retry_pin(job)
        for candidate in ([retry] if retry else []) + [job]:
            try:
                lane = self.service._pick(candidate, extra_exclusions=exclusions).chosen_lane
            except daemon_module.ROUTE_ERRORS:
                continue                    # its next look settles it (C-6.12); it is not placeable
            if lane:
                return lane
        return None

    @precondition(lambda self: self.service is not None)
    @rule()
    def fair_capacity_drains_the_queue(self):
        """P3 (C-6.9): with capacity fair and every attempt ending as soon as it starts, each job some
        lane would take is placed within a few passes, unless an older job that competes with it can
        never be placed (a pin that names no lane, exclusions that cover every lane)."""
        for _ in range(len(self.pending(self.jobs())) + 4):
            for attempt in self.live():
                self.finish(attempt, "ok")
            self.fair()
            pending = self.pending(self.jobs())
            if not pending:
                return
            # Past every clock, and past reading_ttl_s so no earlier reading is still fresh.
            clocks = [parse(job["next_check_at"]) for job in pending.values() if job["next_check_at"]]
            self.clock.advance(max([self.policy["caps"]["reading_ttl_s"] + 1]
                                   + [(clock - self.clock.instant).total_seconds() + 1 for clock in clocks]))
            self.fair()
            self.admission_pass()
        for attempt in self.live():
            self.finish(attempt, "ok")
        self.fair()
        jobs, roster, holds = self.jobs(), self.roster(), self.service._holds
        pending = self.pending(jobs)
        placeable = {job_id: self.placeable(job) for job_id, job in pending.items()}

        def stuck(job_id, seen=()) -> bool:
            """Left pending for a reason fair capacity cannot end: the job can never be placed, or the
            job its hold names is stuck (C-6.9 keeps a competitor behind it, or a slot for it)."""
            if job_id not in pending or job_id in seen:
                return False
            if not placeable[job_id]:
                return True
            hold = holds.get(job_id, {})
            older = hold.get("behind") or hold.get("kept_for")
            return hold.get("reason") in ("behind-older-job", "slot-kept") and stuck(older, (*seen, job_id))

        for job_id, lane in placeable.items():
            if not lane:
                continue
            hold = holds.get(job_id, {})
            older = hold.get("behind") or hold.get("kept_for")
            assert hold.get("reason") in ("behind-older-job", "slot-kept") and stuck(older, (job_id,)), (
                f"P3, C-6.9: {job_id} {self.describe(jobs[job_id], roster)} could run on {lane} under fair "
                f"capacity and is still pending after {self.passes} passes: {hold}")
            if hold["reason"] == "slot-kept" and not any(
                    compete(mine, theirs) for mine in self.demands(jobs[job_id], roster)
                    for theirs in self.demands(jobs[older], roster)):
                # KNOWN_SLOT_KEPT_STARVATION: xfailed in test_admission_properties.py. At
                # max_active_attempts 1 the slot C-6.9 keeps for an older waiter that can never be
                # placed starves every job of its tier, including those that do not compete with it.
                event("known C-6.9 conflict: a slot kept for a job that can never use it")


AdmissionMachine.TestCase.settings = PROFILE
TestAdmissionPass = AdmissionMachine.TestCase
