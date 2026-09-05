"""Golden routing and anti-starvation cases for milestone 3 (C-21)."""

import copy
import json
from dataclasses import asdict

import pytest

from subfleet.capacity import build_view
from subfleet.contracts import Exit
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from subfleet.scheduler import evaluate, exit_code, ordered_jobs, probe_required, waiting_metadata


NOW = "2026-09-05T10:33:00Z"
TOMORROW = "2026-09-06T10:33:00Z"
SIX_DAYS = "2026-09-11T10:33:00Z"


@pytest.fixture
def policy():
    return load_policy(DEFAULT_POLICY_PATH)


def lane(identity="claude-1", **changes):
    provider = identity.split("-")[0]
    return {"lane_id": identity, "provider": provider, "account_key": f"{provider}:{identity}@example.com",
            "owner": "v2", "enabled": True, "desktop": False, "home": f"/lanes/{identity}", **changes}


def reading(identity, utilization=.3, reset=TOMORROW, **changes):
    return {"lane_id": identity, "scope": "account", "window": "seven_day", "utilization": utilization,
            "resets_at": reset, "observed_at": NOW, "label": "provider", "source": "probe", **changes}


def closure(identity, scope="account", until=TOMORROW, **changes):
    return {"lane_id": identity, "scope": scope, "until_at": until,
            "reason": "provider-limit", "clock_source": "reported", "source_event": "limit-1", **changes}


def attempt(identity, job_id="running-job", state="running"):
    return {"attempt_id": job_id + "/a1", "job_id": job_id, "lane_id": identity, "state": state}


def job(**changes):
    return {"task": "research", "tier": "standard", "sandbox": "read-only", **changes}


def view(lanes, readings=(), closures=(), attempts=(), jobs=()):
    return build_view(lanes, readings, closures, attempts, jobs, now=NOW)


@pytest.fixture
def observed_0633():
    return view(
        [lane("claude-1", desktop=True, account_key="claude:max@rulesfoundation.org"),
         lane("claude-2", account_key="claude:max@rules.foundation"), lane("claude-3"), lane("codex-1")],
        [reading("claude-1", .2), reading("codex-1", .6)],
        attempts=[attempt("claude-2", "blind-1"), attempt("claude-3", "blind-2")],
    )


def test_c_11_6_observed_0633_promotes_exhausted_opus_chain_to_astra(policy, observed_0633):
    """C-11.6, C-11.2, C-6.4: the excluded desktop and busy blind lanes promote to Astra."""
    decision = evaluate(policy, observed_0633, job(exclusions=["max@rulesfoundation.org"]))
    assert decision.chain == ("opus", "astra")
    assert decision.chosen_model == "astra"
    assert decision.chosen_lane == "codex-1"
    assert "opus: no candidate lanes after exclusions; promoted" in decision.reason
    reasons = {row["lane_id"]: row["reasons"] for row in decision.evaluations[0]["rejections"]}
    assert reasons == {"claude-1": ["excluded", "desktop"], "claude-2": ["no-slot"], "claude-3": ["no-slot"]}


def test_pinned_opus_same_0633_state_returns_no_lane_and_earliest_reset(policy, observed_0633):
    """C-11.2, C-17.3: an Opus pin never promotes and names the earliest known reset."""
    decision = evaluate(policy, observed_0633, job(pinned_model="opus", exclusions=["max@rulesfoundation.org"]))
    assert decision.chain == ("opus",)
    assert exit_code(decision) == Exit.NO_LANE
    assert TOMORROW in decision.reason
    assert "promoted" not in decision.reason


def test_pinned_rules_foundation_lane_never_uses_another_lane(policy, observed_0633):
    """C-11.2: -a max@rules.foundation uses that lane alone or returns NO_LANE."""
    spec = job(pinned_lane="max@rules.foundation")
    assert exit_code(evaluate(policy, observed_0633, spec)) == Exit.NO_LANE
    free = {**observed_0633, "in_flight": {"claude-2": 0, "claude-3": 1}}
    assert evaluate(policy, free, spec).chosen_lane == "claude-2"


@pytest.mark.parametrize("pin", ["claude-2", "claude:max@rules.foundation", "/lanes/claude-2"])
def test_lane_pin_resolves_id_account_and_home(policy, observed_0633, pin):
    """C-11.2, C-17.2: lane, account, and home pins select exactly one identity."""
    result = evaluate(policy, {**observed_0633, "in_flight": {}}, job(pinned_lane=pin))
    assert result.chosen_lane == "claude-2"
    assert result.evaluations[0]["candidates"] == ["claude-2"]


def test_unknown_lane_pin_returns_no_lane(policy):
    """C-11.2, C-17.3: a missing pinned lane is NO_LANE without a substitute."""
    result = evaluate(policy, view([lane()]), job(pinned_lane="missing@example.com"))
    assert exit_code(result) == Exit.NO_LANE
    assert "missing@example.com" in result.reason


def test_codex_weekly_waterfall_fuller_lane_resetting_tomorrow_wins(policy):
    """C-11.3: 60% used resetting tomorrow wins over 40% used resetting in six days."""
    snapshot = view([lane("codex-2"), lane("codex-1")],
                    [reading("codex-1", .6), reading("codex-2", .4, SIX_DAYS)],
                    attempts=[attempt("codex-1")])
    decision = evaluate(policy, snapshot, job(pinned_model="astra"))
    assert decision.evaluations[0]["candidates"] == ["codex-1", "codex-2"]


def test_codex_in_flight_never_reorders_equal_weekly_resets(policy):
    """C-11.3: the lane id breaks tied weekly resets even when that lane is busier."""
    snapshot = view([lane("codex-2"), lane("codex-1")],
                    [reading("codex-1", .8), reading("codex-2", .2)], attempts=[attempt("codex-1")])
    assert evaluate(policy, snapshot, job(pinned_model="astra")).chosen_lane == "codex-1"


@pytest.mark.parametrize("utilization", [.85, .9, 1, 1.47])
def test_codex_any_window_at_or_above_floor_is_ineligible_even_with_soon_reset(policy, utilization):
    """C-11.3: all provider windows must be strictly below the utilization threshold."""
    snapshot = view([lane("codex-1"), lane("codex-2")],
                    [reading("codex-1", .2), reading("codex-1", utilization, window="five_hour"),
                     reading("codex-2", .4, SIX_DAYS)])
    decision = evaluate(policy, snapshot, job(pinned_model="astra"))
    assert decision.chosen_lane == "codex-2"
    assert decision.evaluations[0]["rejections"][0]["reason"] == "below-floor"


def test_claude_worst_window_headroom_then_in_flight_then_id(policy):
    """C-11.3: Claude uses the worst window, then fewer attempts, then lane id."""
    snapshot = view([lane("claude-4"), lane("claude-3"), lane("claude-2"), lane("claude-1")],
                    [reading("claude-1", .1), reading("claude-1", .7, window="five_hour"),
                     reading("claude-2", .3), reading("claude-3", .3), reading("claude-4", .3)],
                    attempts=[attempt("claude-2")])
    decision = evaluate(policy, snapshot, job(pinned_model="opus"))
    assert decision.evaluations[0]["candidates"] == ["claude-3", "claude-4", "claude-2", "claude-1"]


def test_fable_model_scoped_closure_leaves_opus_eligible(policy):
    """C-11.2, C-9.6: claude-fable-5-1 admission closure preserves Opus on the account."""
    snapshot = view([lane()], closures=[closure("claude-1", "claude-fable-5-1")])
    assert evaluate(policy, snapshot, job(pinned_model="opus")).chosen_lane == "claude-1"
    denied = evaluate(policy, snapshot, job(pinned_model="fable"))
    assert exit_code(denied) == Exit.NO_LANE
    assert denied.evaluations[0]["rejections"][0]["reason"] == f"closed:claude-fable-5-1:{TOMORROW}"


@pytest.mark.parametrize("model", ["opus", "fable"])
def test_account_scoped_closure_removes_both_claude_models(policy, model):
    """C-11.2, C-9.6: an account closure applies to both Fable and Opus."""
    assert exit_code(evaluate(policy, view([lane()], closures=[closure("claude-1")]),
                              job(pinned_model=model))) == Exit.NO_LANE


def test_v1_never_candidate_and_desktop_requires_allow_desktop(policy):
    """C-10.3, C-10.4, C-11.2: desktop permission does not override v1 ownership."""
    snapshot = view([lane("claude-1", owner="v1"), lane("claude-2", desktop=True)])
    assert exit_code(evaluate(policy, snapshot, job(pinned_model="opus"))) == Exit.NO_LANE
    allowed = evaluate(policy, snapshot, job(pinned_model="opus", allow_desktop=True))
    assert allowed.chosen_lane == "claude-2"
    assert allowed.evaluations[0]["rejections"][0]["reasons"] == ["owner-v1"]


def test_all_rejection_reasons_recorded_for_one_lane(policy):
    """C-11.5: a rejected lane retains every exclusion and capacity failure."""
    snapshot = view([lane(owner="v1", desktop=True, enabled=False)], [reading("claude-1", .9)],
                    [closure("claude-1")], [attempt("claude-1", "a"), attempt("claude-1", "b")])
    result = evaluate(policy, snapshot, job(pinned_model="opus", exclusions=["claude-1"]))
    assert result.evaluations[0]["rejections"][0]["reasons"] == [
        "excluded", "desktop", "owner-v1", "disabled", f"closed:account:{TOMORROW}", "no-slot", "below-floor"]


def test_unmeasured_lane_takes_one_slot_second_job_waits(policy):
    """C-6.4, C-4.1: a blind lane admits once; the next job waits with capacity and a clock."""
    first = evaluate(policy, view([lane()]), job(pinned_model="opus"))
    assert first.chosen_lane == "claude-1"
    assert "eligible but unmeasured" in first.reason
    second = evaluate(policy, view([lane()], attempts=[attempt("claude-1")]), job(pinned_model="opus"))
    assert exit_code(second) == Exit.NO_LANE
    assert waiting_metadata(second, NOW) == {"wait_reason": "capacity", "next_check_at": "2026-09-05T10:33:01Z"}


@pytest.mark.parametrize("provider,model", [("codex", "astra"), ("claude", "opus")])
def test_unmeasured_and_stale_provider_lanes_rank_after_measured(policy, provider, model):
    """C-11.3, C-9.1: stale and unknown evidence cannot outrank measured headroom."""
    ids = [f"{provider}-{n}" for n in (1, 2, 3)]
    snapshot = view([lane(identity) for identity in ids],
                    [reading(ids[1], .8, SIX_DAYS), reading(ids[2], .01, observed_at="2026-09-05T10:30:00Z")])
    result = evaluate(policy, snapshot, job(pinned_model=model))
    assert result.evaluations[0]["candidates"] == [ids[1], ids[0], ids[2]]
    assert result.evaluations[0]["candidate_details"][ids[2]]["status"] == "eligible but unmeasured"


@pytest.mark.parametrize("sandbox,tier,expected", [("read-only", "standard", False),
    ("workspace-write", "standard", True), ("read-only", "hard", True)])
def test_probe_required_only_for_expensive_unmeasured_work(policy, sandbox, tier, expected):
    """C-11.4: writable or hard work probes its requested model while ordinary work starts."""
    spec = job(pinned_model="opus", sandbox=sandbox, tier=tier)
    assert probe_required(evaluate(policy, view([lane()]), spec), spec) is expected
    assert not probe_required(evaluate(policy, view([lane()], [reading("claude-1")]), spec), spec)
    assert not probe_required(evaluate(policy, view([]), spec), spec)


def test_admission_observed_does_not_grant_measured_capacity(policy):
    """C-9.1, C-11.4: admission success is not a measured window for expensive work."""
    snapshot = view([lane()], [reading("claude-1", None, label="admission-observed")])
    spec = job(pinned_model="opus", sandbox="workspace-write")
    assert probe_required(evaluate(policy, snapshot, spec), spec)


def test_provider_label_without_numeric_window_does_not_grant_second_slot(policy):
    """C-6.4, C-9.1: an incomplete provider reading cannot grant measured concurrency."""
    snapshot = view([lane()], [reading("claude-1", None)], attempts=[attempt("claude-1")])
    result = evaluate(policy, snapshot, job(pinned_model="opus"))
    assert exit_code(result) == Exit.NO_LANE
    assert result.evaluations[0]["rejections"][0]["reasons"] == ["no-slot"]


def test_retired_sol_alias_resolves_with_note(policy, capsys):
    """C-11.1, C-17.2: the retired Sol pin resolves to Astra with a stderr note."""
    decision = evaluate(policy, view([lane("codex-1")]), job(pinned_model="sol"))
    assert decision.chosen_model == "astra"
    assert "sol" in capsys.readouterr().err


def test_unknown_model_names_input_key(policy):
    """C-11.1, C-17.3: unknown model validation names pinned_model for exit 2 mapping."""
    with pytest.raises(ValueError, match="pinned_model"):
        evaluate(policy, view([lane()]), job(pinned_model="unknown"))


@pytest.mark.parametrize("task", ["authored-prose", "strategy", "adjudication"])
def test_fable_only_chains_never_leave_claude_provider(policy, task):
    """C-11.1–2, C-21: authored prose, strategy, and adjudication remain Fable-only."""
    snapshot = view([lane(), lane("codex-1")], [reading("codex-1")],
                    [closure("claude-1", "claude-fable-5-1")])
    decision = evaluate(policy, snapshot, job(task=task, tier="trivial"))
    assert decision.chain == ("fable",)
    assert exit_code(decision) == Exit.NO_LANE
    assert all(row["provider"] == "claude" for row in decision.evaluations)


def test_chain_never_falls_below_requested_tier(policy):
    """C-11.2: a hard task with Claude capacity and closed Astra does not downgrade."""
    snapshot = view([lane(), lane("codex-1")], [reading("claude-1")], [closure("codex-1")])
    decision = evaluate(policy, snapshot, job(tier="hard"))
    assert decision.chain == ("astra",)
    assert exit_code(decision) == Exit.NO_LANE


def test_fleet_cap_prevents_launch_on_otherwise_empty_lane(policy):
    """C-6.4: the fleet concurrency bound applies before model promotion or lane choice."""
    snapshot = view([lane(), lane("codex-1"), lane("codex-2")],
                    attempts=[attempt("codex-1", str(n)) for n in range(4)])
    decision = evaluate(policy, snapshot, job())
    assert exit_code(decision) == Exit.NO_LANE
    assert all(row["capacity_blocks"] == ["fleet"] for row in decision.evaluations)


def test_fifo_within_tier_preserves_same_second_submission_order(policy):
    """C-4.1, C-6.4; plan amendment 11: queued and waiting jobs remain FIFO within tier."""
    jobs = [job(job_id="z-first", created_at=NOW, state="waiting"),
            job(job_id="a-second", created_at=NOW, state="queued"),
            job(job_id="old-hard", tier="hard", created_at="2026-09-05T09:00:00Z"),
            job(job_id="old-standard", created_at="2026-09-05T08:00:00Z")]
    result = ordered_jobs(policy, jobs)
    assert [row["job_id"] for row in result] == ["old-standard", "z-first", "a-second", "old-hard"]


def test_parent_descendants_share_one_concurrency_bound(policy):
    """C-6.4; plan amendment 11: grandchildren cannot evade their root parent's cap."""
    jobs = [job(job_id="parent"), job(job_id="child-a", parent_job_id="parent"),
            job(job_id="child-b", parent_job_id="parent"),
            job(job_id="grandchild-a", parent_job_id="child-a")]
    snapshot = view([lane("claude-1"), lane("claude-2")], attempts=[attempt("claude-1", "grandchild-a")], jobs=jobs)
    result = evaluate(policy, snapshot, job(job_id="grandchild-b", parent_job_id="child-b"))
    assert exit_code(result) == Exit.NO_LANE
    assert result.evaluations[0]["capacity_blocks"] == ["parent:parent"]
    assert evaluate(policy, snapshot, job(job_id="other-root")).chosen_lane == "claude-2"
    policy["caps"]["max_active_attempts_per_parent"] = 2
    assert evaluate(policy, snapshot, job(parent_job_id="child-b")).chosen_lane == "claude-2"


def test_decision_records_consulted_evidence_and_policy_hash_without_mutation(policy):
    """C-11.5: decisions serialize consulted scoped evidence and the job's policy hash."""
    snapshot = view([lane()], [reading("claude-1")], [closure("claude-1", "claude-fable-5-1")])
    original = copy.deepcopy(snapshot)
    result = evaluate(policy, snapshot, job(pinned_model="opus", policy_hash="job-policy-sha"))
    data = json.loads(json.dumps(asdict(result)))
    assert data["policy_hash"] == "job-policy-sha"
    assert data["evaluations"][0]["readings"] == snapshot["readings"]
    assert data["evaluations"][0]["closures"] == []
    assert snapshot == original
