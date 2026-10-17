"""C-4.1, C-6.9–C-6.12, C-11.2: stateful property tests of the admission pass.

Hypothesis drives one in-process daemon (the `state_daemon` shape: fake
adapters, stubbed process identity, no guardian, no provider) on a virtual
clock through random sequences of lanes enrolled and disabled, sensor readings
fresh and stale, closures, probe reservations, lane pins (a lane id, a name two
lanes answer to, a name no lane answers to), jobs in every tier, attempts that
end `ok`, `transient` or `limited`, C-6.14's pilot hold on or off, restarts,
and sensors that fail; `TestAdmissionPassProviderCaps` runs the same machine
with C-6.4a's count per provider on as well. After each
admission pass it checks what the pass left against the contract rather than
against the code that produced it:

- P1 totality (C-6.11, C-6.12): no error from one job's route ends the pass,
  and every job the pass leaves pending has a stated reason.
- P2 non-interference (C-6.9, C-11.2, C-11.8): `behind-older-job` names an
  older job of the same tier that competes with the held job, by the models
  routing walks and the lanes the pins name; in a capped pool a younger job
  that competes with a job waiting for a slot, a lane or a probe is not placed
  past it, even when the waiter is itself held behind an older job while its
  capacity clock runs. One placed past such a waiter or a lease waiter leaves the last
  slot free.
- P3 bounded progress (C-6.9, C-11.8): under fair capacity every job some lane
  would take is placed within a bounded number of passes, with no exception (a
  job no lane can admit holds nobody back and keeps no slot), and the fleet
  stays within its caps.
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

Bounded by default: a plain `pytest` run is 40 examples of at most 30 steps
(`ci`, derandomized, when `CI` is set; `dev`, random, otherwise), and writes no
example database. An explicit `--hypothesis-seed` overrides the machines'
derandomization while preserving the selected profile's bounds.
`HYPOTHESIS_PROFILE=deep` is the opt-in long search, 300
examples of at most 60 steps (about one 20-minute hold of the host's shared test
lock); `HYPOTHESIS_MAX_EXAMPLES` resizes it. A failure prints the steps that
reproduce it, and a blob for `@reproduce_failure`.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, Phase, event, given, note, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, precondition, rule

from subfleet import actions as actions_module
from subfleet import ids as ids_module
from subfleet import capacity, protocol, scheduler
from subfleet import daemon as daemon_module
from subfleet import policy as policy_module
from subfleet import store as store_module
from subfleet import timers as timers_module
from subfleet.adapters import registry
from subfleet.adapters.base import AdapterError
from subfleet.contracts import (MODEL_CAPACITY_STEER_S, ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner,
                                Outcome, OutcomeClass, Reading, ReadingLabel, attempt_dir)
from subfleet.daemon import Daemon
from subfleet.policy import PolicyError, resolve_model
from subfleet.procs import Containment
from tests.claude_code import claude_code_active
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter

# Every example builds a daemon and its store on disk, so every profile names its size.
QUIET = [HealthCheck.too_slow, HealthCheck.filter_too_much]
settings.register_profile("ci", max_examples=40, stateful_step_count=30, derandomize=True,
                          database=None, deadline=None, print_blob=True, suppress_health_check=QUIET)
settings.register_profile("dev", max_examples=40, stateful_step_count=30,
                          database=None, deadline=None, print_blob=True, suppress_health_check=QUIET)
settings.register_profile("deep", max_examples=300, stateful_step_count=60,
                          deadline=None, print_blob=True, suppress_health_check=QUIET)
PROFILE = settings.get_profile(os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else "dev"))
if os.environ.get("HYPOTHESIS_MAX_EXAMPLES"):
    # Size a long search to the time a run may hold the host's shared test lock.
    PROFILE = settings(PROFILE, max_examples=int(os.environ["HYPOTHESIS_MAX_EXAMPLES"]))
if os.environ.get("HYPOTHESIS_NO_SHRINK"):
    # A stateful example here takes up to a second, so shrinking one can take minutes:
    # this reports the first failure as found (rerun its seed without it to shrink).
    PROFILE = settings(PROFILE, phases=[Phase.explicit, Phase.reuse, Phase.generate])

START = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
TIERS = (None, "trivial", "easy", "standard", "hard")
#: `fable` is retired onto `opus` (2026-09-27): a pin to it exercises the alias.
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
    # `standard` for contention, `hard` because hard work on an unmeasured lane probes first (C-11.4).
    "tier": st.sampled_from(("standard", "hard") + TIERS),
    "model": st.sampled_from((None,) + MODELS), "task": st.sampled_from((None,) + TASKS),
    "pin": st.none() | st.integers(0, 15), "legacy": st.booleans(), "authorize": st.booleans(),
    # C-6.12: a stored exclusion that is not a lane name (submit refuses one now; an older row may hold one).
    "exclusions": st.sampled_from(("[]", '["a@example.invalid"]', "[1]", "[null]")),
})
#: One change in capacity, as (kind, lane index, details).
CAPACITY = st.one_of(
    st.tuples(st.just("reading"), st.integers(0, 7), st.sampled_from(("account", "reserved", "model")),
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
#: What an admission probe (C-11.4) finds: admitted, limited, inconclusive, not run, or not contained.
PROBE_MODES = ("ok", "limited", "unknown", "adapter-error", "os-error", "quarantined")
#: One failure or repair.
FAILURE = st.one_of(
    st.tuples(st.just("route"), st.sampled_from(("evaluate", "probe_required")), st.sampled_from(sorted(ROUTE_FAULTS))),
    st.tuples(st.just("workspace")),
    st.tuples(st.just("probe"), st.sampled_from(PROBE_MODES)),
    st.tuples(st.just("garbage"), st.integers(0, 7)),
    st.tuples(st.just("repair")),
)
#: C-4.1: why a waiting job waits.
WAIT_REASONS = {"capacity", "dependency", "approval", "uncertain", "workspace", "route"}
#: C-4.1, C-6.9: holds of jobs that hold back nobody else in their tier.
#: C-4.1, C-6.9, C-11.8: holds of jobs that hold back nobody else in their tier: a person's or a retry's
#: wait, a route, an earlier attempt still live, a lease (held back by none, capped or not), a pin its
#: lane can never admit, and the machine and disk guards (C-6.13, C-6.17). A hold with `for_good` (no
#: lane can ever admit the job, C-11.8) holds nobody either.
HOLD_NOBODY = {"route", "approval", "uncertain", "workspace", "attempt-live", "lease-held", "pin-unadmittable",
               "machine-busy", "disk", "conversation-blocked", "message-settled"}
#: C-6.9, C-6.11: holds that exist only in a pool with a count cap (C-6.4a's count per provider included).
CAPPED_ONLY = {"behind-older-job", "fleet-full", "slot-kept", "parent-cap", "provider-cap", "provider-slot-kept"}
#: One fleet: its caps, the reserve on or off, lanes whose names may collide, the capacity it starts
#: with, a queue, what probes find, and C-6.14's pilot hold (`AdmissionMachine.fleet`).
FLEET = dict(cap=st.none() | st.integers(1, 4), per_lane=st.none() | st.integers(1, 2), reserve=st.booleans(),
             claude=st.lists(st.sampled_from((None,) + NAMES), min_size=0, max_size=2),
             codex_email=st.sampled_from((None,) + NAMES), second_codex=st.booleans(),
             capacity=st.lists(CAPACITY, max_size=4), queue=st.lists(JOB, max_size=6),
             probe=st.sampled_from(PROBE_MODES), prove=st.sampled_from((None, 900)))
#: What `AdmissionMachine.provider_state` reports of one look, in order.
PROVIDER_STATE = ("count-full", "reason", "behind", "live")
#: C-6.4a: a count per provider, shared by all of its lanes; up to six lanes share one of 1 or 2.
PROVIDER_CAPS = st.one_of(st.fixed_dictionaries({"codex": st.integers(1, 2)}, optional={"claude": st.integers(1, 2)}),
                          st.fixed_dictionaries({"claude": st.integers(1, 2)}))
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

    #: C-6.4a: provider -> `providers.<provider>.max_active_attempts`; none here, drawn by the variant below.
    provider_caps: dict[str, int] = {}

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
        self.last_provider_state: dict[str, tuple] = {}  # job id -> its providers' counts and peer at that look
        self.quarantine = False                     # probe containment cannot be verified (C-5.5)
        self.passes = 0
        # `ids` too: job ids carry a timestamp, and a derandomized run should name its jobs alike.
        for module in (daemon_module, scheduler, capacity, store_module, timers_module,
                       policy_module, actions_module, ids_module):
            self.patch.setattr(module, "datetime", self.clock.datetime)
        # No process ever runs here, so containment's settle sleeps (C-5.5) wait for nothing.
        fast_time = {name: getattr(time, name) for name in dir(time) if not name.startswith("_")}
        self.patch.setattr(daemon_module, "time", SimpleNamespace(**{**fast_time, "sleep": lambda seconds: None}))
        self.patch.setattr(daemon_module.procs, "boot_id", lambda: "stateful-boot")
        self.patch.setattr(daemon_module.procs, "proc_start", lambda pid: "stateful-start")
        self.patch.setattr(daemon_module.procs, "same_process", lambda *args: False)
        self.patch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment())
        self.patch.setattr(capacity, "read_desktop_account", lambda path=None: None)
        factory = lambda: ControlledAdapter(self)        # noqa: E731
        self.patch.setattr(registry, "_factories", {"codex": factory, "claude": factory})
        # C-6.9, C-10.3: a fixture session registry, never this host's; its one live session is not the
        # submissions' caller, so every job here is `background` and the pass order is tier, then age.
        claude_code_active(self.patch, self.tmp / "claude")
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
            return job.get("worktree") or job["workdir"], None, None, []

        real_pick = service._pick

        def pick(job, **options):
            decision = real_pick(job, **options)
            self.decided[job["job_id"]] = decision
            return decision

        def probe_census(record):
            return Containment(unverifiable=True) if self.quarantine else Containment()

        def execute_probe(job, lane, model, holder):
            event(f"probe: {self.probe_mode}")
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

    # C-6.4: no count cap is the default; a cap brings C-6.9's hold-back and slot-keeping.
    @initialize(**FLEET)
    def fleet(self, cap, per_lane, reserve, claude, codex_email, second_codex, capacity, queue, probe, prove):
        """A fleet (its caps, the reserve (C-11.7) on or off, lanes whose names may collide), the
        capacity it starts with, and a queue submitted before the first pass sees any of it."""
        self.root = self.tmp / "state"
        self.root.mkdir()
        self.harness = Harness(self.root)
        policy = json.loads((self.root / "policy.json").read_text())
        policy["caps"].update(max_active_attempts=cap, max_in_flight_per_lane=per_lane)
        # C-11.7 reserved Fable until its retirement; the rule is the same for any model, so Opus here.
        policy["reserve"]["models"] = ["opus"] if reserve else []
        # C-6.14: every lane starts never having answered, so with the pilot hold on (the Harness turns
        # it off) each takes one detached attempt at a time until one answers.
        policy["admission"]["prove_idle_s"] = prove
        for provider, limit in self.provider_caps.items():
            policy.setdefault("providers", {}).setdefault(provider, {})["max_active_attempts"] = limit
        (self.root / "policy.json").write_text(json.dumps(policy, indent=2) + "\n")
        self.service = self.start()
        self.probe_mode = probe
        for label in claude:
            self.put_lane("claude", label)
        if second_codex:
            self.put_lane("codex", None)
        if codex_email:
            # C-11.2, the 2026-09-22 roster: a Codex lane answers to the email its usage probe read.
            self.report_email("codex-1", codex_email)
        note(f"fleet: cap={cap} per_lane={per_lane} reserve={reserve} lanes="
             f"{[(lane['lane_id'], lane['label']) for lane in self.roster()]} codex-1 email={codex_email} probe={probe} "
             f"prove_idle_s={prove} provider_caps={self.provider_caps}")
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
            scope = {"account": "account", "reserved": self.policy["models"]["opus"]["id"]}.get(scope, ids[0])
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
    @rule(attempt=st.integers(0, 7), outcome=st.sampled_from(("ok", "transient", "limited", "model-capacity")),
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
                                     "limited": OutcomeClass.LIMITED, "model-capacity": OutcomeClass.TRANSIENT}[outcome],
                                    f"fixture {outcome}", closure=closure,
                                    evidence={"model_capacity": True} if outcome == "model-capacity" else {})
        adir = attempt_dir(self.service.root, attempt["job_id"], attempt["seq"])
        adir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name, contents in (("stdout", b"fixture result\n"), ("stderr", b""), ("lane.log", b"")):
            (adir / name).write_bytes(contents)
        # C-9.2: finalization independently verifies a detached Codex success
        # against its frozen stream, exit receipt and nonblank deliverable.
        # Claude's corresponding terminal event is `result`. Failed attempts
        # carry no success proof, even though their partial output is nonblank.
        stream = ""
        if outcome == "ok":
            provider = self.store.get_lane(attempt["lane_id"]).provider
            terminal = ({"type": "turn.completed"} if provider == "codex" else
                        {"type": "result", "subtype": "success", "is_error": False,
                         "result": "fixture result"})
            stream = json.dumps(terminal) + "\n"
        (adir / "stream.jsonl").write_text(stream)
        receipt = {"rc": {"ok": 0, "transient": 1, "limited": 4, "model-capacity": 1}[outcome], "signal": None, "wall_s": .1,
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
        self.last_provider_state.clear()
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
        live_rows = self.live()
        live_before, provider_before = len(live_rows), self.provider_live(live_rows)
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
            self.last_provider_state.pop(row["job_id"], None)
        for hold in self.service._holds.values():
            event(f"hold: {hold['reason'].split(':')[0]}")
        event(f"pass placed {min(len(placed), 3)}+ with {min(len(self.pending(after)), 4)}+ pending")
        self.check_totality(before, after, placed)
        self.check_non_interference(before, after, placed, live_before, provider_before)
        self.check_caps()
        self.check_recheck_cost(before, after, decisions, provider_before)

    def decision_counts(self) -> Counter:
        return Counter(row["job_id"] for row in self.store.query(
            "SELECT job_id FROM decisions WHERE attempt_id IS NULL"))

    def check_totality(self, before, after, placed) -> None:
        """P1 (C-4.1, C-6.11, C-6.12): every job left pending has a stated reason."""
        holds, cap, roster = self.service._holds, self.policy["caps"]["max_active_attempts"], self.roster()
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
                        "lease-held": {"leases"}, "route": {"error_type", "error", "deferrals", "next_check_at"},
                        "pin-unadmittable": {"lane_id", "reasons", "closures", "since", "fail_at"},
                        "route-moved": {"tries"},
                        # C-6.4a: "`why` and status name `providers.<provider>.max_active_attempts` and say
                        # `<provider>: N of N running`, or identify an older peer keeping a slot".
                        "provider-cap": {"provider", "live", "max_active_attempts", "key"},
                        "provider-slot-kept": {"provider", "live", "max_active_attempts", "key", "kept_for", "tier"}}
            missing = required.get(reason, set()) - set(hold)
            assert not missing, f"C-6.11: the {reason} hold on {job_id} lacks {sorted(missing)}: {hold}"
            if reason in ("provider-cap", "provider-slot-kept"):
                assert (hold["max_active_attempts"] == self.provider_caps.get(hold["provider"])
                        and hold["key"] == f"providers.{hold['provider']}.max_active_attempts"), \
                    f"C-6.4a: {job_id}'s {reason} hold names a count the policy does not set: {hold}"
            if not self.capped(job, roster):
                assert reason not in CAPPED_ONLY, (
                    f"C-6.9, C-6.11: {job_id} is held {hold} in a pool with no count cap, where no job waits "
                    f"behind another")
            if job["wait_reason"] in ("approval", "uncertain", "workspace") and not (
                    job["wait_reason"] == "workspace" and reason == "pin-unadmittable"):
                assert reason == job["wait_reason"], (
                    f"C-6.11: {job_id} waits on {job['wait_reason']!r}, which is reported as that "
                    f"whatever else holds it, but its hold reads {hold}")
            if reason in ("fleet-full", "slot-kept"):
                assert hold["max_active_attempts"] == cap
            if reason == "route":
                assert job["wait_reason"] == "route" and hold["next_check_at"] == job["next_check_at"]
            if job["wait_reason"] == "route" and parse(job["next_check_at"]) > self.clock.instant:
                assert reason == "route", f"C-6.12: {job_id} waits on its route but its hold reads {hold}"
            # C-6.11: a pass that does not look at the job repeats its last hold in full, except what it
            # asks again on the job's clock: `next_check_at`, and `for_good`, which "a pass that asks it
            # again on the job's clock replaces ... with its own answer" (C-11.8).
            asked_again = ("next_check_at", "for_good")
            if reason == "probe-pending":
                # C-6.9: "each detached pass builds the line again", so whose turn the probe is (`behind`)
                # is this pass's answer, not the last look's.
                asked_again += ("behind",)
                owner = hold.get("behind")
                if owner is not None:
                    order = lambda job_id: (self.policy["tiers"].index(tier_of(self.policy, after[job_id])),  # noqa: E731
                                            self.seq[job_id])
                    assert owner in self.pending(after) and order(owner) < order(job_id), (
                        f"C-6.9: {job_id} waits for the probe turn of {owner}, which is not pending ahead of it")
                    assert holds.get(owner, {}).get("reason") == "probe-pending" and (
                        not hold.get("model") or not holds[owner].get("model")
                        or holds[owner]["model"] == hold["model"]), (
                        f"C-6.9: {job_id} waits for the probe turn of {owner}, whose hold {holds.get(owner)} "
                        f"waits on no probe of {hold.get('model')}")
            if job_id in self.looks:
                self.last_look[job_id] = {key: value for key, value in hold.items() if key not in asked_again}
            elif (job_id in self.last_look and "next_check_at" in hold and reason not in HOLD_NOBODY
                  and before[job_id]["state"] == "waiting" and before[job_id]["wait_reason"] == "capacity"
                  and job["next_check_at"] == before[job_id]["next_check_at"]):
                # C-6.11: "a pass that does not look at the job repeats the last hold in full".
                repeated = {key: value for key, value in hold.items() if key not in asked_again}
                assert repeated == self.last_look[job_id], \
                    f"C-6.11: {job_id}'s hold degraded between looks: {self.last_look[job_id]} -> {repeated}"
        for job_id in set(self.last_look) - set(self.pending(after)):
            self.last_look.pop(job_id)

    def demands(self, job, roster) -> list[tuple]:
        """What a job could be waiting for: its own demand, or the one retry pinned to its last pair (C-4.5)."""
        models = walked_models(self.policy, job)
        attempts = self.store.list_attempts(job["job_id"])
        # Contract oracle: only this unpinned job's live, recorded transient steers
        # remove model demand; do not use the scheduler's demand implementation.
        if models is not None and not job["pinned_model"] and not job["pinned_lane"] and job["kind"] not in (
                "turn", "resume", "revive"):
            refused = set()
            for attempt in attempts:
                steer = json.loads(attempt["evidence_json"] or "{}").get("capacity_steer")
                if attempt["outcome_class"] == "transient" and steer and parse(steer["until"]) > self.clock.instant:
                    refused.add(steer["model_id"])
            models = frozenset(model for model in models if self.policy["models"][model]["id"] not in refused)
        own = (models, pinned_lanes(roster, self.policy, job))
        last = attempts[-1] if attempts else None
        last_steered = bool(last and json.loads(last["evidence_json"] or "{}").get("capacity_steer"))
        if last and not last_steered and last["outcome_class"] == "transient" and sum(
                1 for row in attempts if row["outcome_class"] == "transient" and row["lane_id"] == last["lane_id"]) == 1:
            try:
                pair = (frozenset({resolve_model(self.policy, last["model_requested"], note=False)}),
                        frozenset({last["lane_id"]}))
            except (PolicyError, ValueError):
                pair = (None, frozenset({last["lane_id"]}))
            return [own, pair]
        return [own]

    def check_non_interference(self, before, after, placed, live_before, provider_before) -> None:
        """P2 (C-4.1, C-6.9, C-11.2, C-6.4a): a hold names an older competitor of the same tier, and a job is
        not placed past an older competitor that cannot be placed; passing a waiter leaves it a slot, and
        with a count on the provider it was placed on, a slot of that provider's count."""
        holds, roster, cap = self.service._holds, self.roster(), self.policy["caps"]["max_active_attempts"]
        lane_provider = {lane["lane_id"]: lane["provider"] for lane in roster}
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
            assert holds[older]["reason"] not in HOLD_NOBODY and "for_good" not in holds[older], \
                f"P2, C-4.1: {job_id} is held behind {older}, whose own hold {holds[older]} holds back nobody"
            assert any(self.competes(mine, theirs, roster) for mine in demand[job_id] for theirs in demand[older]), (
                f"P2, C-6.9: {job_id} is held behind {older} but they cannot compete: "
                f"{job_id} {self.describe(after[job_id], roster)} vs {older} {self.describe(after[older], roster)}")

        def holds_back(older) -> bool:
            """C-6.9: whether an older job waits for a slot, a lane or a probe (including a pilot
            or a moved route), never a lease or for good. Held behind an older job, it still does while its
            capacity clock runs (C-6.9, C-6.12), unless its last look found it waiting for a lease."""
            hold = holds.get(older, {})
            if "for_good" in hold:
                return False
            if hold.get("reason") == "behind-older-job":
                job = after[older]
                return (job["state"] == "waiting" and job["wait_reason"] == "capacity" and bool(job["next_check_at"])
                        and parse(job["next_check_at"]) > self.clock.instant
                        and self.last_look.get(older, {}).get("reason") != "lease-held")
            return hold.get("reason") not in HOLD_NOBODY | {"waiting", None}

        live, provider_live = live_before, Counter(provider_before)
        for row in sorted(placed, key=lambda row: (
                self.policy["tiers"].index(tier_of(self.policy, after[row["job_id"]])), self.seq[row["job_id"]])):
            live += 1
            job_id, provider = row["job_id"], lane_provider[row["lane_id"]]
            provider_live[provider] += 1
            # C-6.9: an older waiter holds back only in a pool with a count cap, and only while it waits for a
            # slot, a lane or a probe (a pilot's lane and a moved route included, C-6.11, C-6.14), never for
            # good (C-11.8) and never for a lease, though a lease waiter keeps the last slot.
            older_jobs = [older for older in pending
                          if self.seq[older] < self.seq[job_id]
                          and tier_of(self.policy, after[older]) == tier_of(self.policy, after[job_id])
                          and "for_good" not in holds.get(older, {})]
            waiting = [older for older in older_jobs if holds_back(older)]
            keepers = waiting + [older for older in older_jobs if holds.get(older, {}).get("reason") == "lease-held"]
            for older in waiting:
                wanted = demand[job_id]
                original = walked_models(self.policy, after[older])
                remaining = demand[older][0][0]
                skipped = original - remaining if original is not None and remaining is not None else frozenset()
                if skipped and any(mine[0] is not None and skipped & mine[0] for mine in wanted):
                    # C-6.9: a job trying a model steered away from the waiter
                    # competes on the model actually selected for its attempt.
                    selected = resolve_model(self.policy, row["model_requested"], note=False)
                    wanted = [(frozenset({selected}), mine[1]) for mine in wanted]
                assert not all(self.holds_back(theirs, mine, provider, roster)
                               for mine in wanted for theirs in demand[older]), (
                    f"P2, C-6.9: {job_id} was placed on {row['lane_id']} past {older}, an older job of its tier "
                    f"that competes with it and could not be placed ({holds[older]}); before the pass {older} was "
                    f"{ {key: before[older][key] for key in ('state', 'wait_reason', 'next_check_at')} } "
                    f"and {job_id} {self.describe(after[job_id], roster)}; looked at: "
                    f"{sorted(self.looks & {older, job_id})}")
            if keepers and cap is not None:
                assert live <= cap - 1, (f"C-6.9: {job_id} passed waiting {keepers} and left no slot free "
                                         f"({live} live, max_active_attempts {cap})")
            limit = self.provider_caps.get(provider)
            # C-6.4a: "the last fleet slot kept for an older waiter" is kept of the provider's count too, for
            # an older waiter that could use the provider.
            sharing = [older for older in keepers
                       if any(provider in self.providers_of(theirs, roster) for theirs in demand[older])]
            if limit is not None and sharing:
                assert provider_live[provider] <= limit - 1, (
                    f"C-6.4a: {job_id} passed waiting {sharing} on {provider} and left no slot of its count free "
                    f"({provider_live[provider]} live, providers.{provider}.max_active_attempts {limit})")

    def describe(self, job, roster) -> str:
        return (f"(tier={job['tier']}, task={job['task']}, model={job['pinned_model']}, pin={job['pinned_lane']}; "
                f"demands {self.demands(job, roster)})")

    def capped(self, job=None, roster=()) -> bool:
        """C-6.9 (`scheduler.hold_scope`): a detached job's pool has a count cap: the fleet's or a lane's,
        or (C-6.4a) a count on a provider the job could use."""
        caps = self.policy["caps"]
        if any(caps.get(key) is not None for key in
               ("max_active_attempts", "max_in_flight_per_lane", "max_in_flight_unmeasured")):
            return True
        return job is not None and any(set(self.provider_caps) & self.providers_of(demand, roster)
                                       for demand in self.demands(job, roster))

    def providers_of(self, demand, roster) -> frozenset[str]:
        """C-6.4a: the providers a demand could use: its models' (every provider's when they cannot be
        told), narrowed to its lanes' when it names lanes of the roster."""
        models, lanes = demand
        providers = {model["provider"] for name, model in self.policy["models"].items()
                     if models is None or name in models}
        named = {lane["provider"] for lane in roster if lanes is not None and lane["lane_id"] in lanes}
        return frozenset(providers & named if named else providers)

    def competes(self, one, other, roster) -> bool:
        """C-6.9 with C-6.4a: two jobs that could use one capped provider compete whatever their models and
        lanes, since that provider's count is theirs to share."""
        return compete(one, other) or bool(set(self.provider_caps) & self.providers_of(one, roster)
                                           & self.providers_of(other, roster))

    def holds_back(self, theirs, mine, provider, roster) -> bool:
        """C-6.9: whether an older waiter's demand holds back a job placed on a lane of `provider`. Without
        counts per provider, when they compete in a capped pool. With them, C-6.4a's order: "fleet/lane/family
        competition using demand narrowed to the selected provider; competition against older peers of that
        selected provider": any waiter that could use it holds back a job placed on it, if it is capped."""
        if not self.provider_caps:
            return self.capped() and compete(mine, theirs)
        models, lanes = mine
        narrowed = (frozenset(name for name, model in self.policy["models"].items()
                              if model["provider"] == provider and (models is None or name in models)), lanes)
        return (self.capped() and compete(narrowed, theirs)
                or provider in self.provider_caps and provider in self.providers_of(theirs, roster))

    def provider_live(self, live=None) -> Counter:
        """C-6.4a: live attempts per provider. None here is quarantined, and probes hold no attempt."""
        lanes = {lane["lane_id"]: lane["provider"] for lane in self.store.lane_rows()}
        return Counter(lanes[row["lane_id"]] for row in (self.live() if live is None else live))

    def check_caps(self) -> None:
        """P3 (C-6.4, C-6.4a): the fleet, every lane and every provider stay within the caps that are set."""
        caps, live = self.policy["caps"], self.live()
        if caps["max_active_attempts"] is not None:
            assert len(live) <= caps["max_active_attempts"], f"C-6.4: {len(live)} live attempts over the cap"
        per_lane = Counter(row["lane_id"] for row in live)
        if caps["max_in_flight_per_lane"] is not None:
            assert all(count <= caps["max_in_flight_per_lane"] for count in per_lane.values()), per_lane
        per_provider = self.provider_live(live)
        for provider, limit in self.provider_caps.items():
            assert per_provider[provider] <= limit, \
                f"C-6.4a: {per_provider[provider]} live {provider} attempts over providers.{provider}.max_active_attempts {limit}"

    def check_recheck_cost(self, before, after, decisions, provider_before) -> None:
        """P4 (C-6.10): a look repeating its verdict adds no row; every look sets a later clock. Whether a
        count was full and which job a hold names are not the verdict, C-6.4a's count per provider included."""
        now, waits, counts = self.clock.instant, self.service._capacity_waits, self.decision_counts()
        pending, roster = self.pending(after), self.roster()
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
                self.last_provider_state.pop(job_id, None)
            else:
                verdict, kind = scheduler.verdict_signature(decision), wait_kind(wait)
                last = self.last_verdict.get(job_id)
                state = self.provider_state(job, roster, provider_before)
                previous = self.last_provider_state.get(job_id)
                if self.provider_caps and last and last[0] == verdict and previous and previous != state:
                    # The case the chain merge of 2026-10-13 settled: #194 keyed the wait on these too.
                    changed = "+".join(name for name, old, new in zip(PROVIDER_STATE, previous, state) if old != new)
                    event(f"C-6.4a: a look repeated its verdict with {changed} changed")
                self.last_provider_state[job_id] = state
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

    def provider_state(self, job, roster, provider_before) -> tuple:
        """C-6.4a: at a look, whether each capped provider the job could use was at its count when the pass
        began, and the hold's reason, older job (`behind`) and provider count (`live`)."""
        providers = set().union(*(self.providers_of(demand, roster) for demand in self.demands(job, roster)))
        hold = self.service._holds.get(job["job_id"], {})
        return (tuple(sorted((provider, provider_before[provider] >= limit)
                             for provider, limit in self.provider_caps.items() if provider in providers)),
                hold.get("reason"), hold.get("behind"), hold.get("live"))

    # --- P3 and P4 over many passes ---------------------------------------------------------------

    def boundaries(self, until: datetime) -> list[datetime]:
        """The distinct instants at which capacity changes by itself: a reading going stale or
        resetting, a closure expiring, a pilot's wait lapsing or a lane's proof going stale. A verdict may change there, and C-6.10 checks a known reset on time."""
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
        # C-6.14: a pilot that has not answered stops holding its lane `prove_wait_s` after its reservation.
        wait_s = (self.policy.get("admission") or {}).get("prove_wait_s", 300)
        if wait_s:
            instants += [parse(row["reserved_at"]) + timedelta(seconds=wait_s) for row in self.live()
                         if row.get("reserved_at")]
        # C-6.14: a lane is proven for `prove_idle_s` after its last answer; then an attempt in flight
        # on it that has not answered is its pilot.
        idle_s = (self.policy.get("admission") or {}).get("prove_idle_s")
        if idle_s:
            instants += [datetime.fromtimestamp(last + idle_s, timezone.utc)
                         for last in self.service._lane_answers.values()]
        return sorted({instant for instant in instants if self.clock.instant < instant <= until})

    @precondition(lambda self: self.service is not None)
    @rule(minutes=st.integers(2, 10))
    def quiet_fleet(self, minutes):
        """P4 (C-6.10): nothing changes but the clock. A waiting job is looked at when it is due, and
        its looks are bounded by the backoff: about six to reach 30 s, then two a minute, plus a few
        after each change the clock itself brings (a reading going stale, a closure expiring) and
        each placement (a verdict carries whether the fleet had room)."""
        end = self.clock.instant + timedelta(minutes=minutes)
        boundaries = len(self.boundaries(end))
        looks, placements, passes = Counter(), 0, 0
        # A step is at least a second, so this many passes always reach the end of the stretch.
        while self.clock.instant < end and passes <= 60 * minutes:
            placed_before = len(self.store.list_attempts())
            self.admission_pass()
            passes += 1
            placements += len(self.store.list_attempts()) - placed_before
            looks.update(self.looks)
            self.clock.advance(self.next_step(end))
        assert self.clock.instant >= end, f"P4: the quiet stretch stopped {end - self.clock.instant} short"
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
        """Every lane enabled, open, measured with room, and the reserve slack; every sensor sound."""
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
                if exclusions:
                    self.service._merged_exclusions(job, exclusions)
                lane = self.service._pick(candidate, extra_exclusions=exclusions).chosen_lane
            except (daemon_module.Unroutable, *daemon_module.ROUTE_ERRORS):
                continue                    # its next look settles it (C-6.12); it is not placeable
            if lane:
                return lane
        return None

    @precondition(lambda self: self.service is not None)
    @rule()
    def fair_capacity_drains_the_queue(self):
        """P3 (C-6.9, C-11.8): with capacity fair and every attempt ending as soon as it starts, each job
        some lane would take is placed within a few passes. A job no lane can admit (a pin that names
        no lane, exclusions that cover every lane) holds nobody back and keeps no slot."""
        # Fair progress is tested after the bounded steer has ended (C-9.5).
        self.clock.advance(MODEL_CAPACITY_STEER_S)
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

        for job_id, lane in placeable.items():
            # C-6.9, C-11.8: no job waits behind, or is kept out of a slot for, a job that no lane can
            # admit, and with every attempt ending as it starts the pass order places every other job.
            assert not lane, (
                f"P3, C-6.9: {job_id} {self.describe(jobs[job_id], roster)} could run on {lane} under fair "
                f"capacity and is still pending after {self.passes} passes: {holds.get(job_id)}")


AdmissionMachine.TestCase.settings = PROFILE
TestAdmissionPass = AdmissionMachine.TestCase


class ProviderCapMachine(AdmissionMachine):
    """The machine with C-6.4a's count per provider on, one provider's or both, with the fleet's caps or
    without. Filling and emptying a provider's count, and a new older peer of the provider ahead of a job,
    change no verdict, so a look that repeats its verdict across them adds no decision row (C-6.10, P4).
    #194 keyed the wait on both; the chain merge of 2026-10-13 kept the verdict alone."""

    @initialize(**FLEET, provider_caps=PROVIDER_CAPS)
    def fleet(self, cap, per_lane, reserve, claude, codex_email, second_codex, capacity, queue, probe, prove,
              provider_caps):
        self.provider_caps = provider_caps
        super().fleet(cap, per_lane, reserve, claude, codex_email, second_codex, capacity, queue, probe, prove)


ProviderCapMachine.TestCase.settings = PROFILE
TestAdmissionPassProviderCaps = ProviderCapMachine.TestCase


@pytest.fixture(autouse=True)
def explicit_stateful_seed(request, monkeypatch):
    """Hypothesis checks derandomization before its CLI seed for stateful tests."""
    if (request.cls in (TestAdmissionPass, TestAdmissionPassProviderCaps)
            and request.config.getoption("--hypothesis-seed") is not None):
        monkeypatch.setattr(request.cls, "settings", settings(PROFILE, derandomize=False))


@settings(PROFILE, max_examples=1)
@given(_context=st.none())
def test_r4_three_standard_reviews_drain_after_probe_os_error(_context):
    """Review r3b: simulated successes must free the only slot for all three reviews."""
    machine = AdmissionMachine()
    try:
        review = {"tier": "standard", "model": None, "task": "review", "pin": None,
                  "legacy": False, "authorize": False, "exclusions": "[]"}
        machine.fleet(cap=1, per_lane=None, reserve=False, claude=[], codex_email=None,
                      second_codex=True, capacity=[], queue=[dict(review) for _ in range(3)],
                      probe="os-error", prove=None)
        machine.fair_capacity_drains_the_queue()
        assert len(machine.jobs()) == 3
        assert {job["state"] for job in machine.jobs().values()} == {"succeeded"}
        assert len(machine.store.list_attempts()) == 3
        assert machine.passes <= 7
    finally:
        machine.teardown()


@pytest.mark.parametrize("provider,model", [("codex", "astra"), ("claude", "sonnet")])
@pytest.mark.parametrize("outcome,expected", [("ok", "ok"), ("transient", "transient"),
                                              ("limited", "limited"), ("model-capacity", "transient")])
@settings(PROFILE, max_examples=1)
@given(_context=st.none())
def test_r4_simulated_finish_preserves_provider_outcome(provider, model, outcome, expected, _context):
    """C-9.2: completion proof admits successes without turning failures into successes."""
    machine = AdmissionMachine()
    try:
        job = {"tier": "standard", "model": model, "task": None, "pin": None,
               "legacy": False, "authorize": False, "exclusions": "[]"}
        machine.fleet(cap=1, per_lane=None, reserve=False,
                      claude=[None] if provider == "claude" else [], codex_email=None,
                      second_codex=False, capacity=[], queue=[job], probe="ok", prove=None)
        machine.admission_pass()
        attempt, = machine.live()
        assert machine.store.get_lane(attempt["lane_id"]).provider == provider
        machine.finish(attempt, outcome)
        finished = machine.store.get_attempt(attempt["attempt_id"])
        assert finished["outcome_class"] == expected
        assert finished["state"] == ("succeeded" if outcome == "ok" else "failed")
        evidence = json.loads(finished["evidence_json"])
        if provider == "codex":
            assert evidence["classification"]["turn_completed"] is (outcome == "ok")
        if outcome == "ok":
            assert machine.store.get_job(attempt["job_id"])["state"] == "succeeded"
            adir = attempt_dir(machine.root, attempt["job_id"], attempt["seq"])
            assert (adir / "deliverable.md").read_bytes().strip()
            terminal = json.loads((adir / "stream.jsonl").read_text())
            assert terminal["type"] == ("turn.completed" if provider == "codex" else "result")
    finally:
        machine.teardown()
