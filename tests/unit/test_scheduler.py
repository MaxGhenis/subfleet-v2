"""Golden routing and anti-starvation cases for milestone 3 (C-21)."""

import copy
import json
from dataclasses import asdict, replace

import pytest

from subfleet.capacity import build_view
from subfleet.contracts import Exit
from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError, load_policy
from tests.caps import capped
from subfleet.scheduler import (evaluate, exit_code, ordered_jobs, probe_required,
                                resolve_lane, waiting_metadata)


NOW = "2026-09-05T10:33:00Z"
TOMORROW = "2026-09-06T10:33:00Z"
SIX_DAYS = "2026-09-11T10:33:00Z"


@pytest.fixture
def policy():
    """The shipped policy with the reserve rule (C-11.7) switched off: these cases
    describe admission mechanics that the rule sits on top of. `reserve_policy`
    below is the policy as shipped, for the reserve cases. Both keep the count
    caps of before 2026-09-27 (`tests/caps.py`): most of these cases, the 06:33
    one of C-11.6 among them, were observed or written under them, and a policy
    may still set them. `tests/unit/test_scheduler_uncapped.py` covers the
    shipped default, which has none."""
    loaded = capped(load_policy(DEFAULT_POLICY_PATH))
    loaded["reserve"] = {**loaded.get("reserve", {}), "models": []}
    return loaded


@pytest.fixture
def reserve_policy():
    return capped(load_policy(DEFAULT_POLICY_PATH))


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
    # C-10.3 (2026-09-30): the desktop login is refused only by the exclusion here, not for being the desktop's.
    assert reasons == {"claude-1": ["excluded"], "claude-2": ["no-slot"], "claude-3": ["no-slot"]}


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


def test_v1_never_candidate_and_the_desktop_login_is_a_candidate_by_default(policy):
    """C-10.3, C-10.4, C-11.2: the desktop login is a lane for every job (2026-09-30), `--no-desktop` keeps
    a job off it, and nothing about the desktop overrides v1 ownership."""
    snapshot = view([lane("claude-1", owner="v1"), lane("claude-2", desktop=True)])
    for spec in (job(pinned_model="opus"), job(pinned_model="opus", allow_desktop=True)):
        decision = evaluate(policy, snapshot, spec)
        assert decision.chosen_lane == "claude-2"
        assert decision.evaluations[0]["rejections"][0]["reasons"] == ["owner-v1"]
    kept_off = evaluate(policy, snapshot, job(pinned_model="opus", exclusions=["@desktop"]))
    assert exit_code(kept_off) == Exit.NO_LANE
    assert rejection(kept_off, "opus", "claude-2")["reasons"] == ["excluded"]


def test_all_rejection_reasons_recorded_for_one_lane(policy):
    """C-11.5: a rejected lane retains every exclusion and capacity failure."""
    snapshot = view([lane(owner="v1", desktop=True, enabled=False)], [reading("claude-1", .9)],
                    [closure("claude-1")], [attempt("claude-1", "a"), attempt("claude-1", "b")])
    result = evaluate(policy, snapshot, job(pinned_model="opus", exclusions=["claude-1"]))
    assert result.evaluations[0]["rejections"][0]["reasons"] == [
        "excluded", "owner-v1", "disabled", f"closed:account:{TOMORROW}", "no-slot", "below-floor",
        "desktop-reserve:seven_day", "desktop-reserve:in-flight"]


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
    """C-6.4; plan amendment 11: grandchildren cannot evade their root parent's cap,
    when the policy sets one. By default there is none (2026-09-27)."""
    jobs = [job(job_id="parent"), job(job_id="child-a", parent_job_id="parent"),
            job(job_id="child-b", parent_job_id="parent"),
            job(job_id="grandchild-a", parent_job_id="child-a")]
    snapshot = view([lane("claude-1"), lane("claude-2")], attempts=[attempt("claude-1", "grandchild-a")], jobs=jobs)
    uncapped = evaluate(policy, snapshot, job(job_id="grandchild-b", parent_job_id="child-b"))
    assert uncapped.chosen_lane is not None and uncapped.evaluations[0]["capacity_blocks"] == []
    policy["caps"]["max_active_attempts_per_parent"] = 1
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


def test_lane_pin_provider_absent_from_valid_policy_is_key_named_invalid_input(policy, tmp_path):
    """C-11.1–2, C-17.3: a lane with no policy model returns exit 2 instead of crashing."""
    policy["models"] = {name: model for name, model in policy["models"].items() if model["provider"] == "codex"}
    policy["chains"] = {"research": ["terra", "terra", "terra", "astra"]}
    policy["permissions"] = {"*": "read-only"}
    policy["retired"] = {"sol": "astra"}
    path = tmp_path / "codex-only-policy.json"
    path.write_text(json.dumps(policy))
    policy = load_policy(path)
    with pytest.raises(PolicyError) as caught:
        evaluate(policy, view([lane()]), {"pinned_lane": "claude-1"})
    assert caught.value.code == Exit.INVALID_INPUT
    assert caught.value.key == "pinned_lane"
    assert "claude" in str(caught.value) and str(path) in str(caught.value)


def test_decision_records_other_model_reading_that_grants_second_lane_slot(policy):
    """C-6.4, C-11.5: cross-model slot evidence is recorded without becoming requested quota."""
    snapshot = view([lane()], [reading("claude-1", .95, scope="claude-fable-5-1")],
                    attempts=[attempt("claude-1")])
    decision = evaluate(policy, snapshot, job(pinned_model="opus"))
    assert decision.chosen_lane == "claude-1"
    evaluation = decision.evaluations[0]
    assert evaluation["candidate_details"]["claude-1"]["measured"] is False
    assert evaluation["candidate_details"]["claude-1"]["headroom"] is None
    assert evaluation["readings"] == []
    assert evaluation["capacity_readings"] == snapshot["readings"]


def test_probe_reservation_blocks_lane_without_becoming_an_in_flight_attempt(policy):
    """C-6.4, C-11.4: an active probe reserves admission without inventing an attempt count."""
    snapshot = view([lane()])
    snapshot["unavailable_lanes"] = {"claude-1": "probe:running"}
    decision = evaluate(policy, snapshot, job(pinned_model="opus"))
    assert exit_code(decision) == Exit.NO_LANE
    assert snapshot["in_flight"] == {"claude-1": 0}
    assert decision.evaluations[0]["rejections"][0]["reasons"] == ["no-slot"]
    assert decision.evaluations[0]["rejections"][0]["slot_block"] == "probe:running"


def test_probe_reservations_count_toward_fleet_admission_bound(policy):
    """C-6.4, C-11.4: concurrent probes consume the same fleet budget as admitted work."""
    snapshot = view([lane()])
    snapshot["reserved_probes"] = policy["caps"]["max_active_attempts"]
    decision = evaluate(policy, snapshot, job(pinned_model="opus"))
    assert exit_code(decision) == Exit.NO_LANE
    assert decision.evaluations[0]["capacity_blocks"] == ["fleet"]
    assert snapshot["in_flight"] == {"claude-1": 0}


def test_no_lane_with_admission_evidence_reports_unknown_reset(policy):
    """C-9.1, C-11.4, C-17.3: admission success supplies no invented reset clock."""
    snapshot = view([lane(desktop=True)], [reading("claude-1", utilization=None,
                    reset=None, label="admission-observed", window="admission",
                    scope=OPUS)])
    decision = evaluate(policy, snapshot, job(pinned_model="opus", exclusions=["@desktop"]))
    assert exit_code(decision) == Exit.NO_LANE
    assert "earliest reset: unknown" in decision.reason


def test_c17_2_an_email_pin_still_resolves_when_the_key_is_two_uuids(policy):
    """C-1.4, C-11.2, C-17.2 a verified Claude lane is keyed by identity, so the
    email an operator types for `-a` and `-x` has to resolve through the label
    C-1.4 calls the display name."""
    lane = {"lane_id": "claude-9", "provider": "claude", "owner": "v2",
            "account_key": "claude:acct-uuid:org-uuid", "desktop": False,
            "identity": "acct-uuid:org-uuid", "label": "max@axiom.org"}
    view = {"lanes": [lane], "readings": [], "closures": [], "attempts": [], "jobs": [],
            "in_flight": {}, "now": "2026-09-05T11:30:00Z"}
    assert resolve_lane([lane], "max@axiom.org")["lane_id"] == "claude-9"
    pinned = evaluate(policy, view, job(pinned_model="haiku", pinned_lane="max@axiom.org"))
    assert pinned.chosen_lane == "claude-9"
    excluded = evaluate(policy, view, job(pinned_model="haiku",
                                          exclusions=["max@axiom.org"]))
    assert excluded.chosen_lane is None


# --- C-11.7: the reserve rule ------------------------------------------------

FABLE = "claude-fable-5-1"
OPUS = "claude-opus-5-5"


def usage(identity, shared, fable=None, **changes):
    """Fresh readings as the usage endpoint reports them (C-9.9): the shared week and,
    when the account has one, the Fable week."""
    rows = [reading(identity, shared, source="oauth-usage", **changes)]
    if fable is not None:
        rows.append(reading(identity, fable, source="oauth-usage", scope=FABLE, **changes))
    return rows


def decision_for(policy, lanes, readings, **job_changes):
    return evaluate(policy, view(lanes, readings), job(**job_changes))


def rejection(decision, model, identity):
    evaluation = next(e for e in decision.evaluations if e["model"] == model)
    return next((r for r in evaluation["rejections"] if r["lane_id"] == identity), None)


def test_c11_7_opus_is_refused_on_an_unmeasured_claude_lane_and_fable_is_not(reserve_policy):
    """Until the usage endpoint has been read, a non-reserved model gets no Claude lane."""
    lanes = [lane("claude-1"), lane("codex-1")]
    opus = decision_for(reserve_policy, lanes, [], pinned_model="opus", task=None, tier=None)
    assert opus.chosen_lane is None
    assert rejection(opus, "opus", "claude-1")["reasons"] == ["reserve:fable:unmeasured"]
    assert rejection(opus, "opus", "claude-1")["reserve"]["state"] == "unmeasured"
    fable = decision_for(reserve_policy, lanes, [], pinned_model="fable", task=None, tier=None)
    assert fable.chosen_lane == "claude-1"        # C-11.4 still probes it before launch


def test_c11_7_slack_decides_and_is_recorded(reserve_policy):
    """slack = (1 - shared) - cap_ratio * (1 - fable); below min_slack the lane is reserved."""
    lanes = [lane("claude-1"), lane("claude-2")]
    # claude-1: shared 94, Fable 49 (the incident's account): slack = .06 - 2 * .51 < 0
    # claude-2: shared 60, Fable 97: slack = .40 - 2 * .03 = .34
    rows = usage("claude-1", .94, .49) + usage("claude-2", .60, .97)
    decision = decision_for(reserve_policy, lanes, rows, pinned_model="opus", task=None, tier=None)
    assert decision.chosen_lane == "claude-2"
    reserved = rejection(decision, "opus", "claude-1")
    assert "reserve:fable:reserved" in reserved["reasons"]      # beside below-floor at 94
    assert reserved["reserve"] == {"model": "fable", "state": "reserved", "all_remaining": .06,
                                   "reserved_remaining": .51, "slack": -.96, "cap_ratio": 2.0, "min_slack": .05}
    chosen = next(e for e in decision.evaluations if e["model"] == "opus")["candidate_details"]["claude-2"]
    assert chosen["reserve"]["state"] == "slack" and chosen["reserve"]["slack"] == .34


def test_c11_7_fable_jobs_see_both_windows_and_ignore_the_reserve(reserve_policy):
    """The reserved model itself is bounded by min(shared, Fable) headroom, never by slack."""
    lanes = [lane("claude-1"), lane("claude-2")]
    rows = usage("claude-1", .80, .49) + usage("claude-2", .60, .82)      # floor is 15 percent
    decision = decision_for(reserve_policy, lanes, rows, pinned_model="fable", task=None, tier=None)
    assert decision.chosen_lane == "claude-1"      # headroom .20 beats claude-2's Fable headroom .18
    details = next(e for e in decision.evaluations if e["model"] == "fable")["candidate_details"]
    assert "reserve" not in details["claude-1"] and details["claude-1"]["headroom"] == pytest.approx(.20)
    assert details["claude-2"]["headroom"] == pytest.approx(.18)


def test_c11_7_an_account_without_a_reserved_window_is_free(reserve_policy):
    lanes = [lane("claude-1")]
    decision = decision_for(reserve_policy, lanes, usage("claude-1", .30), pinned_model="opus", task=None, tier=None)
    assert decision.chosen_lane == "claude-1"
    detail = next(e for e in decision.evaluations if e["model"] == "opus")["candidate_details"]["claude-1"]["reserve"]
    assert detail["state"] == "slack" and detail["slack"] == .7 and detail["reserved_remaining"] is None


def test_c11_7_only_the_usage_sensor_measures_the_shared_window(reserve_policy):
    """A seven_day reading from a rate_limit_event carries no scoped window, so it cannot
    prove the lane free: without a usage read the lane stays unmeasured for Opus."""
    lanes = [lane("claude-1")]
    rows = [reading("claude-1", .30, source="rate_limit_event")]
    decision = decision_for(reserve_policy, lanes, rows, pinned_model="opus", task=None, tier=None)
    assert decision.chosen_lane is None
    assert rejection(decision, "opus", "claude-1")["reasons"] == ["reserve:fable:unmeasured"]


def test_c11_7_non_reserved_work_orders_lanes_by_slack(reserve_policy):
    lanes = [lane("claude-1"), lane("claude-2"), lane("claude-3")]
    rows = (usage("claude-1", .50, .95) + usage("claude-2", .20, .95) + usage("claude-3", .10, .60))
    decision = decision_for(reserve_policy, lanes, rows, pinned_model="sonnet", task=None, tier=None)
    evaluation = next(e for e in decision.evaluations if e["model"] == "sonnet")
    # slack: claude-1 .50-.10=.40; claude-2 .80-.10=.70; claude-3 .90-.80=.10
    assert evaluation["candidates"] == ["claude-2", "claude-1", "claude-3"]
    assert decision.chosen_lane == "claude-2"


def test_c11_7_a_stale_usage_read_does_not_count(reserve_policy):
    lanes = [lane("claude-1")]
    rows = usage("claude-1", .30, .90, observed_at="2026-09-05T10:00:00Z")   # 33 minutes old, ttl 120 s
    decision = decision_for(reserve_policy, lanes, rows, pinned_model="opus", task=None, tier=None)
    assert rejection(decision, "opus", "claude-1")["reasons"] == ["reserve:fable:unmeasured"]


def test_c11_7_a_pinned_lane_is_still_reserved(reserve_policy):
    """An operator's pin does not spend Fable either; the job waits for the usage read."""
    lanes = [lane("claude-1")]
    decision = evaluate(reserve_policy, view(lanes, usage("claude-1", .94, .49)),
                        job(pinned_lane="claude-1", pinned_model="opus", task=None, tier=None))
    assert decision.chosen_lane is None
    assert "reserve:fable:reserved" in rejection(decision, "opus", "claude-1")["reasons"]


AUTHORIZATION = "Operator approved Opus on this lane despite unavailable reserve telemetry."


def authorized_job(**changes):
    return job(**{"pinned_lane": "claude-1", "pinned_model": "opus",
                  "unmeasured_reserve_reason": AUTHORIZATION, **changes})


@pytest.mark.parametrize("model", ["opus", OPUS])
def test_unmeasured_reserve_authorization_is_exact_and_does_not_invent_quota(reserve_policy, model):
    snapshot = view([lane("claude-1"), lane("claude-2"), lane("codex-1")])
    spec = authorized_job(pinned_model=model)
    decision = evaluate(reserve_policy, snapshot, spec)
    assert decision.chain == ("opus",)
    assert (decision.chosen_lane, decision.chosen_model) == ("claude-1", "opus")
    evaluation, = decision.evaluations
    assert evaluation["candidates"] == ["claude-1"]
    details = evaluation["candidate_details"]["claude-1"]
    assert details["reserve"] == {
        "model": "fable", "state": "unmeasured", "cap_ratio": 2.0, "min_slack": .05,
        "authorization": {"reason": AUTHORIZATION, "lane_id": "claude-1", "model_id": OPUS}}
    assert details["measured"] is False and details["headroom"] is None
    assert evaluation["readings"] == []
    assert probe_required(decision, spec)


@pytest.mark.parametrize("reason", ["", " \n ", False, 1, {}, "x" * 2001])
def test_unmeasured_reserve_authorization_rejects_invalid_reason(reserve_policy, reason):
    with pytest.raises(ValueError, match="unmeasured_reserve_reason"):
        evaluate(reserve_policy, view([lane()]), authorized_job(unmeasured_reserve_reason=reason))


@pytest.mark.parametrize("changes", [
    {"pinned_lane": None}, {"pinned_model": None}, {"pinned_lane": ""},
    {"pinned_model": " "}, {"pinned_lane": []}, {"pinned_model": True},
    {"pinned_lane": "claude-1@example.com"},
])
def test_unmeasured_reserve_authorization_requires_explicit_canonical_pair(reserve_policy, changes):
    with pytest.raises(ValueError, match="unmeasured_reserve_reason"):
        evaluate(reserve_policy, view([lane()]), authorized_job(**changes))


@pytest.mark.parametrize("guard", [
    "owner", "enabled", "desktop", "identity", "excluded", "account-closure", "model-closure",
    "floor", "measured-reserve", "lane-slot", "fleet-cap", "probe-cap", "parent-cap", "quarantine",
])
def test_unmeasured_reserve_authorization_preserves_other_rejections(reserve_policy, guard):
    target, rows, closures, attempts, jobs, changes = lane(), [], [], [], [], {}
    expected = {"owner": "owner-v1", "enabled": "disabled", "identity": "identity-mismatch",
                "floor": "below-floor", "measured-reserve": "reserve:fable:reserved"}.get(guard, "no-slot")
    if guard == "owner": target["owner"] = "v1"
    elif guard == "enabled": target["enabled"] = False
    elif guard == "desktop":             # C-10.3: the desktop login's reserve, as any other guard
        target["desktop"], expected = True, "desktop-reserve:five_hour"
        rows = [reading("claude-1", .75, window="five_hour")]
    elif guard == "identity": target["identity_status"] = "mismatch"
    elif guard == "excluded": changes["exclusions"], expected = ["claude-1"], "excluded"
    elif guard in ("account-closure", "model-closure"):
        scope = "account" if guard == "account-closure" else OPUS
        closures = [closure("claude-1", scope)]
        expected = f"closed:{scope}:{TOMORROW}"
    elif guard == "floor": rows = [reading("claude-1", .9)]
    elif guard == "measured-reserve": rows = usage("claude-1", .3, .2)
    elif guard == "lane-slot": attempts = [attempt("claude-1")]
    elif guard == "fleet-cap": attempts = [attempt("codex-1", f"active-{i}") for i in range(4)]
    elif guard == "parent-cap":
        reserve_policy["caps"]["max_active_attempts_per_parent"] = 1
        changes["parent_job_id"] = "parent"
        attempts = [attempt("codex-1")]
        jobs = [{"job_id": "parent"}, {"job_id": "running-job", "parent_job_id": "parent"}]
    snapshot = view([target, lane("claude-2"), lane("codex-1")], rows, closures, attempts, jobs)
    if guard == "quarantine": snapshot["unavailable_lanes"] = {"claude-1": "probe:quarantined"}
    if guard == "probe-cap": snapshot["reserved_probes"] = 4
    spec = authorized_job(**changes)
    decision = evaluate(reserve_policy, snapshot, spec)
    assert decision.chosen_lane is None and decision.chain == ("opus",)
    assert expected in rejection(decision, "opus", "claude-1")["reasons"]
    assert not probe_required(decision, spec)


@pytest.mark.parametrize("rows", [
    [], [reading("claude-1", None, window="admission", label="admission-observed")],
    [reading("claude-1", .3, source="rate_limit_event")], usage("claude-1", .3, .99),
])
def test_unmeasured_reserve_authorization_always_requires_same_model_probe(reserve_policy, rows):
    spec = authorized_job()
    decision = evaluate(reserve_policy, view([lane()], rows), spec)
    assert decision.chosen_lane == "claude-1"
    assert probe_required(decision, spec)
    for mismatch in ({"chosen_lane": "claude-2"}, {"chosen_model": "fable"}):
        with pytest.raises(ValueError, match="unmeasured_reserve_reason"):
            probe_required(replace(decision, **mismatch), spec)


def test_unmeasured_reserve_reason_bound_and_absence_preserve_default(reserve_policy):
    snapshot = view([lane()])
    assert evaluate(reserve_policy, snapshot, authorized_job(unmeasured_reserve_reason="x" * 2000)).chosen_lane
    denied = evaluate(reserve_policy, snapshot, authorized_job(unmeasured_reserve_reason=None))
    assert denied.chosen_lane is None
    assert rejection(denied, "opus", "claude-1")["reasons"] == ["reserve:fable:unmeasured"]
    missing = evaluate(reserve_policy, snapshot, authorized_job(pinned_lane="claude-missing"))
    assert missing.chosen_lane is None and missing.chain == ("opus",)


def test_c23_37_stranded_claude_capacity_precedes_otherwise_better_lane(policy):
    """C-23.37: a Fable-limited lane spends its remaining Opus capacity first."""
    blocked = closure("claude-2", FABLE)
    snapshot = view([lane("claude-1"), lane("claude-2")],
                    [reading("claude-1", .1), reading("claude-2", .7)], [blocked])
    result = evaluate(policy, snapshot, job(pinned_model="opus"))
    assert result.chosen_lane == "claude-2"
    details = result.evaluations[0]["candidate_details"]["claude-2"]
    assert details["stranded_scopes"] == [FABLE]
    assert result.evaluations[0]["stranding_closures"] == [blocked]


@pytest.mark.parametrize("scope,changes", [
    ("claude-haiku-4-5-20251001", {}),
    (FABLE, {"until_at": "2026-09-05T10:32:00Z"}),
    (FABLE, {"released_at": NOW}),
    ("unknown-model", {}),
])
def test_c23_37_only_live_higher_model_closures_strand(policy, scope, changes):
    """C-23.37: lower, unknown, expired, and released limits confer no preference."""
    snapshot = view([lane("claude-1"), lane("claude-2")],
                    [reading("claude-1", .1), reading("claude-2", .7)],
                    [closure("claude-2", scope, **changes)])
    assert evaluate(policy, snapshot, job(pinned_model="opus")).chosen_lane == "claude-1"


def test_c23_37_stranding_cannot_bypass_reserve_or_account_closure(reserve_policy):
    """C-23.37, C-11.7: ordering changes neither eligibility nor reserved headroom."""
    for extra in ([], [closure("claude-2", "account")]):
        snapshot = view([lane("claude-1"), lane("claude-2")],
                        usage("claude-1", .3, .99) + usage("claude-2", .3, .2),
                        [closure("claude-2", FABLE), *extra])
        assert evaluate(reserve_policy, snapshot, job(pinned_model="opus")).chosen_lane == "claude-1"


def test_c23_37_older_policy_uses_its_upward_chain(policy):
    """C-11.1, C-23.37: old policy files derive relative strength from tier chains."""
    for model in policy["models"].values():
        model.pop("priority", None)
    snapshot = view([lane("claude-1"), lane("claude-2")],
                    [reading("claude-1", .1), reading("claude-2", .7)],
                    [closure("claude-2", OPUS)])
    assert evaluate(policy, snapshot, job(pinned_model="sonnet")).chosen_lane == "claude-2"


@pytest.mark.parametrize("changes", [
    {"observed_at": "2026-09-05T10:34:00Z"},
    {"label": "unknown", "utilization": None},
    {"source": "rate_limit_event"},
])
def test_c11_7_unknown_reserved_window_is_not_an_absent_window(reserve_policy, changes):
    """C-11.7: fresh shared usage cannot erase uncertain reserved-model evidence."""
    rows = usage("claude-1", .3) + [{**reading("claude-1", .1, scope=FABLE,
        source="oauth-usage"), **changes}]
    result = decision_for(reserve_policy, [lane()], rows, pinned_model="opus")
    assert result.chosen_lane is None
    assert "reserve:fable:unmeasured" in rejection(result, "opus", "claude-1")["reasons"]


def test_c11_2_compat_picker_uses_reserve_and_upward_routing(reserve_policy):
    """C-11.2, C-11.7: every public picker evaluates the daemon's routing rules."""
    from subfleet.policy import pick
    lanes = [lane("claude-1"), lane("codex-1")]
    result = pick(reserve_policy, lanes, task="research", tier="standard", now=NOW)
    assert result.chosen_lane == "codex-1"
    assert "reserve:fable:unmeasured" in rejection(result, "opus", "claude-1")["reasons"]
    assert pick(reserve_policy, lanes, pinned_model="opus", now=NOW).chosen_lane is None


def test_c11_7_new_usage_snapshot_can_remove_a_reserved_window(reserve_policy):
    """C-11.7: a newer complete endpoint response supersedes a removed scoped bucket."""
    rows = usage("claude-1", .3) + [reading("claude-1", .1, scope=FABLE,
        source="oauth-usage", observed_at="2026-09-05T10:30:00Z")]
    result = decision_for(reserve_policy, [lane()], rows, pinned_model="opus")
    assert result.chosen_lane == "claude-1"
    reserve = result.evaluations[0]["candidate_details"]["claude-1"]["reserve"]
    assert reserve["reserved_remaining"] is None
