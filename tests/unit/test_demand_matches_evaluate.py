"""C-6.9, C-11.2: admission's demand is exactly what `evaluate` walks (a differential test).

`demand_models` and `demand_lanes` decide which jobs compete (C-6.9); `evaluate`
decides where a job may run. They are two implementations of one semantics, so
over generated rosters and jobs: the models demanded are the chain evaluate
walks, a pin's lanes are the lanes evaluate looks at, a pin naming no lane is the
empty set and evaluate looks at none, a pin naming several lanes is None and
evaluate refuses it, and `demand_lanes` never raises.
"""

from hypothesis import given, settings, HealthCheck, event
from hypothesis import strategies as st

from subfleet import scheduler
from subfleet.policy import load_policy, DEFAULT_POLICY_PATH

POLICY = load_policy(DEFAULT_POLICY_PATH)
NOW = "2026-09-25T00:00:00Z"
MODEL_NAMES = list(POLICY["models"]) + [POLICY["models"]["opus"]["id"], "nope"]
TASKS = list(POLICY["chains"]) + [None, "nope"]
TIERS = list(POLICY["tiers"]) + [None]
SHARED = ["max@x.org", "other@x.org", "/h/1"]


@st.composite
def rosters(draw):
    n = draw(st.integers(0, 5))
    lanes = []
    for i in range(n):
        provider = draw(st.sampled_from(["claude", "codex"]))
        lanes.append({
            "lane_id": f"{provider}-{i}", "provider": provider,
            "account_key": draw(st.sampled_from([None, f"{provider}:acct{i}", f"{provider}:max@x.org"])),
            "label": draw(st.sampled_from([None] + SHARED)),
            "email": draw(st.sampled_from([None] + SHARED)),
            "home": draw(st.sampled_from([None, "/h/1", f"/h/{i}"])),
            "credential_ref": None,
            "enabled": draw(st.booleans()), "owner": "v2",
            # every lane refuses the job, so evaluate walks the whole chain
            "desktop": True,
            "identity_status": draw(st.sampled_from([None, "verified", "mismatch"])),
        })
    return lanes


@st.composite
def jobs(draw, lanes):
    pins = [None, "nope"] + [lane["lane_id"] for lane in lanes] + SHARED
    job = {"job_id": "j", "task": draw(st.sampled_from(TASKS)), "tier": draw(st.sampled_from(TIERS)),
           "pinned_model": draw(st.sampled_from([None] + MODEL_NAMES)),
           "pinned_lane": draw(st.sampled_from(pins)),
           "unmeasured_reserve_reason": draw(st.sampled_from([None, None, "because"])),
           "exclusions": (), "allow_desktop": False}
    return job


@settings(max_examples=1000, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(st.data())
def test_c6_9_demand_is_what_evaluate_walks(data):
    lanes = data.draw(rosters())
    job = data.draw(jobs(lanes))
    models = scheduler.demand_models(POLICY, job)
    demand_lanes_err = None
    try:
        lanes_d = scheduler.demand_lanes(lanes, job, POLICY)
    except Exception as exc:   # demand_lanes must never raise
        demand_lanes_err = exc
    assert demand_lanes_err is None, demand_lanes_err
    try:
        decision = scheduler.evaluate(POLICY, {"lanes": lanes, "now": NOW}, job)
    except scheduler.RouteError as exc:
        event("routeerror:" + str(exc)[:30])
        # a pin naming several lanes is refused; demand_lanes said None for it
        if "names" in str(exc):
            assert lanes_d is None, (job, lanes_d, exc)
        return
    except ValueError as exc:
        event("valueerror")
        return
    event(f"ok models={'none' if models is None else len(models)} pinned={bool(job['pinned_lane'])} lanes_d={'none' if lanes_d is None else len(lanes_d)}")
    walked = frozenset(decision.chain)
    if models is not None:
        assert walked == models, (job, walked, models)
    seen = {row["lane_id"] for ev in decision.evaluations for row in ev["rejections"]} | \
           {c for ev in decision.evaluations for c in ev["candidates"]}
    if job["pinned_lane"]:
        assert lanes_d is not None, (job, "evaluate resolved a pin demand_lanes called several")
        assert seen <= lanes_d, (job, seen, lanes_d)
        if lanes_d == frozenset():
            assert seen == set()
    else:
        assert lanes_d is None
