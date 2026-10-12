"""C-6.9, C-6.12: the daemon's detached pass against the pass model around jobs on their capacity clock.

Stateful admission properties, findings 5 and 6 (2026-10-12): a job held behind an
older one while its capacity clock ran registered as no waiter, so in a pool with a
count cap a younger job that competed with it and not with the older one was placed
past it. The pure model (`tests/admission_model.py`, `capacity_look="clock"`) states
the rule, and its properties are in `tests/unit/test_held_waiter_holdback.py`. Here
the daemon's own `_admit_pass`, in-process on the fake providers, takes the model's
decisions on generated queues: Opus, Astra and Terra jobs and Opus-or-Astra reviews,
some pinned to a lane, some on their clocks, beside lanes some of which are closed,
under a fleet cap or none.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone

import pytest
from hypothesis import HealthCheck, event, given, settings, strategies as st

from subfleet import daemon as daemon_module
from subfleet.adapters import registry
from subfleet.daemon import Daemon, after
from tests.admission_model import run_pass
from tests.claude_code import claude_code_active
from tests.fake.conftest import Harness
from tests.fake.test_admission_properties import closed, lane, measured, placed_on
from tests.fake_adapter import FakeAdapter

LANES = (("codex-1", "codex"), ("codex-2", "codex"), ("claude-a", "claude"), ("claude-b", "claude"))
#: What a job asks for: a model pin, or a review (standard: Opus, then Astra).
ASKS = {"astra": dict(pinned_model="astra"), "opus": dict(pinned_model="opus"), "terra": dict(pinned_model="terra"),
        "review": dict(pinned_model=None, task="review", tier="standard")}
JOBS = st.fixed_dictionaries({"ask": st.sampled_from(sorted(ASKS)), "pin": st.sampled_from([None, 0, 1]),
                              "clocked": st.booleans()})
DIFFERENTIAL = settings(max_examples=40, deadline=None, derandomize=True, database=None,
                        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture,
                                               HealthCheck.data_too_large])


@contextlib.contextmanager
def fleet(root, cap):
    """`routing_state` with four measured lanes, for one Hypothesis example."""
    root.mkdir(parents=True)
    harness = Harness(root)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
        patch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
        patch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
        patch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
        claude_code_active(patch, root / "claude")
        service = Daemon(root)
        try:
            service.policy["caps"]["max_active_attempts"] = cap
            for lane_id, provider in LANES[1:]:
                service.store.put_lane(lane(lane_id, provider))
            for lane_id, _ in LANES:
                measured(service, lane_id)
            patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None, []))
            yield service, harness
        finally:
            service.close()


def pinned_lane(spec) -> str | None:
    """The spec's pin, a lane of its model's provider: Astra and Terra on Codex, Opus on Claude."""
    if spec["pin"] is None or spec["ask"] == "review":
        return None
    provider = "claude" if spec["ask"] == "opus" else "codex"
    return [lane_id for lane_id, owner in LANES if owner == provider][spec["pin"]]


@DIFFERENTIAL
@given(cap=st.sampled_from([None, 2, 3, 10]),
       shut=st.lists(st.sampled_from([lane_id for lane_id, _ in LANES]), max_size=3, unique=True),
       specs=st.lists(JOBS, min_size=2, max_size=6))
def test_c6_9_the_detached_pass_takes_the_pass_model_s_decisions_around_waiters_on_their_clock(
        tmp_path_factory, cap, shut, specs):
    """Differential, the daemon's detached pass against `tests/admission_model.run_pass` on the view the
    daemon evaluates on. Each job's outcome agrees: placed, or held for the same reason, behind or
    keeping a slot for the same job."""
    with fleet(tmp_path_factory.mktemp("differential") / "state", cap) as (service, harness):
        for lane_id in shut:
            closed(service, lane_id)
        submitted = []
        for spec in specs:
            job_id = service.dispatch("submit", harness.submit_args(**ASKS[spec["ask"]],
                                                                    pinned_lane=pinned_lane(spec)))["job_id"]
            if spec["clocked"]:
                service.store.update_job(job_id, state="waiting", wait_reason="capacity", next_check_at=after(600))
            submitted.append((job_id, spec["clocked"]))
        clocked = {job_id for job_id, on_clock in submitted if on_clock}
        queued = service.store.query("SELECT * FROM jobs WHERE state IN ('queued','waiting') ORDER BY created_at,rowid")
        view = service._capacity_view(service._desktop_identity(), service._capacity_rows(route=True),
                                      now=datetime.now(timezone.utc))
        live = service._liveness(queued)
        rows = [dict(row, capacity_look="clock") if row["job_id"] in clocked else dict(row) for row in queued]
        model = run_pass(service.policy, view, rows, live=live).by_job()
        service._admit()
        for row in queued:
            job_id = row["job_id"]
            hold = service._holds.get(job_id) or {}
            daemon_said = ("placed" if placed_on(service, job_id) else hold.get("reason"),
                           hold.get("behind") or hold.get("kept_for"))
            outcome = model[job_id]
            model_said = ("placed" if outcome.placed else outcome.hold, outcome.waits_for)
            assert daemon_said == model_said, (job_id, daemon_said, model_said,
                                               {j: service._holds.get(j) for j in model}, shut, cap)
        through = sum(1 for row in model.values() if row.hold == "behind-older-job"
                      and model[row.waits_for].hold == "behind-older-job")
        event(f"holds through a job held on its clock: {min(through, 2)}{'+' if through >= 2 else ''}")
