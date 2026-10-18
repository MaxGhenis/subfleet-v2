"""Review of #120: differential against the base's real code, not a reference copy.

`tests/reference_scheduler.py` was edited in the same PR, so the PR's own
"no previously eligible lane becomes ineligible" law compares the head with a
model of the head. Here the release base (f3bffcea, whose scheduler, capacity,
picker and policy are byte-identical to f832e6b8) and the PR's pre-horizon-fix
head (64b4175f) are loaded from `git archive` beside this tree, and each
generated fleet is judged by both.

What may change is order only (PR body: "Eligibility, headroom_floor,
lane_spread and the live policy are unchanged: nothing new waits"). So:
1. every lane's verdict (candidate or the exact rejection reasons) is the same;
2. no lane the base would launch on without a pre-launch probe (C-11.4) needs
   one now: a probe is a wait;
3. `pick` excludes no lane the base recommended.

The fleets add what the PR's generators never produce: a lane's windows read at
different times, including a model-scoped weekly window the provider stopped
reporting days ago, which stays the newest row of its key in every view
(`capacity.latest_readings`, `Store.latest_reading_candidates`).
"""

from __future__ import annotations

import copy
import importlib
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import capacity as head_capacity, picker as head_picker, scheduler as head_scheduler
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
TTL = 120
LAWS = settings(max_examples=int(__import__("os").environ.get("REVIEW_EXAMPLES", "300")), deadline=None, derandomize=True,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


def _load(commit: str) -> dict:
    """The commit's `subfleet` package under another name, or skip if git cannot supply it."""
    name = f"subfleet_{commit}"
    if name not in sys.modules:
        try:
            blob = subprocess.run(["git", "archive", commit, "subfleet"], cwd=REPO, check=True,
                                  capture_output=True).stdout
        except (OSError, subprocess.CalledProcessError) as error:
            pytest.skip(f"git archive {commit}: {error}")
        root = Path(tempfile.mkdtemp(prefix=f"sf-{commit}-"))
        with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
            archive.extractall(root, filter="data")
        (root / "subfleet").rename(root / name)
        sys.path.insert(0, str(root))
    return {part: importlib.import_module(f"{name}.{part}") for part in ("capacity", "scheduler", "picker")}


HEAD = {"capacity": head_capacity, "scheduler": head_scheduler, "picker": head_picker}


def iso(seconds: float) -> str:
    return (NOW + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def policy(floor=0.0, reserve_models=()):
    rules = copy.deepcopy(load_policy(DEFAULT_POLICY_PATH))
    rules["headroom_floor"] = floor
    rules["reserve"]["models"] = list(reserve_models)
    return rules


AGES = st.sampled_from([0, 30, 119, 121, 600, 86_400, 6 * 86_400])
RESETS = st.one_of(st.none(), st.sampled_from([-86_400, -1, 30, 3_600, 4 * 86_400]))


@st.composite
def fleets(draw, provider: str, realistic: bool = False):
    """`realistic` draws every reset after its own observation, as providers report them."""
    rules = policy(draw(st.sampled_from([0.0, 0.15])),
                   draw(st.sampled_from([(), ("fable",)])) if provider == "claude" else ())
    model = draw(st.sampled_from(["opus", "fable", "sonnet"] if provider == "claude" else ["astra", "terra"]))
    model_id = rules["models"][model]["id"]
    other_ids = [m["id"] for m in rules["models"].values() if m["provider"] == provider]
    lanes, readings, closures = [], [], []
    for n in range(draw(st.integers(1, 5))):
        identity = f"{provider}-{n}"
        lanes.append({"lane_id": identity, "provider": provider, "owner": "v2", "enabled": True,
                      "desktop": False, "account_key": identity, "home": "/lanes/" + identity,
                      "email": f"{identity}@example.test", "identity_status": "verified"})
        windows = [("account", "seven_day"), ("account", "five_hour")]
        windows += [(scope, "seven_day") for scope in draw(st.lists(st.sampled_from(other_ids),
                                                                    unique=True, max_size=2))]
        for scope, window in windows:
            if draw(st.integers(0, 4)) == 0:
                continue
            age = draw(AGES)
            reset = draw(RESETS)
            if realistic and reset is not None:
                window_s = 5 * 3_600 if window == "five_hour" else 7 * 86_400
                reset = -age + draw(st.integers(1, window_s))
            readings.append({"lane_id": identity, "scope": scope, "window": window,
                             "utilization": draw(st.sampled_from([0.0, .3, .85, .95, .99])),
                             "resets_at": iso(reset) if reset is not None else None,
                             "observed_at": iso(-age), "label": "provider",
                             "source": "rate_limit_event"})
        if draw(st.integers(0, 5)) == 0:
            closures.append({"lane_id": identity, "scope": draw(st.sampled_from(["account", model_id])),
                             "until_at": iso(3_600), "reason": "provider-limit", "clock_source": "reported",
                             "created_at": iso(-60), "released_at": None})
    view = head_capacity.build_view(lanes, readings, closures, [], [], now=iso(0), reading_ttl_s=TTL)
    view["in_flight"] = {lane["lane_id"]: draw(st.integers(0, 3)) for lane in lanes}
    job = {"pinned_model": model, "sandbox": draw(st.sampled_from(["read-only", "workspace-write"])),
           "tier": draw(st.sampled_from(["standard", "hard"])), "kind": "dispatch"}
    return rules, view, job


def judged(modules, rules, view, job):
    evaluation = modules["scheduler"].evaluate(rules, copy.deepcopy(view), job).evaluations[0]
    verdicts = {row["lane_id"]: tuple(row["reasons"]) for row in evaluation["rejections"]}
    verdicts.update({identity: () for identity in evaluation["candidates"]})
    return verdicts


def needs_probe(modules, rules, view, job, identity):
    pinned = {**job, "pinned_lane": identity}
    decision = modules["scheduler"].evaluate(rules, copy.deepcopy(view), pinned)
    assert decision.chosen_lane == identity
    return modules["scheduler"].probe_required(decision, pinned)


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("commit", ["64b4175f"])
@LAWS
@given(data=st.data())
def test_every_lane_is_judged_as_the_base_judged_it(provider, commit, data):
    """Eligibility sets on f3bffcea and on the PR (64b4175f and this head) are identical."""
    rules, view, job = data.draw(fleets(provider))
    base = judged(_load("f3bffcea"), rules, view, job)
    assert judged(HEAD, rules, view, job) == base
    assert judged(_load(commit), rules, view, job) == base


@pytest.mark.parametrize("realistic", [False, True])
@pytest.mark.parametrize("provider", ["claude", "codex"])
@LAWS
@given(data=st.data())
def test_no_lane_newly_needs_a_pre_launch_probe(provider, realistic, data):
    """A lane the base launches on directly is launched on directly now (C-11.4)."""
    rules, view, job = data.draw(fleets(provider, realistic))
    base = _load("f3bffcea")
    for identity, reasons in judged(base, rules, view, job).items():
        if not reasons and not needs_probe(base, rules, view, job, identity):
            assert not needs_probe(HEAD, rules, view, job, identity), identity


@pytest.mark.parametrize("realistic", [False, True])
@pytest.mark.parametrize("provider", ["claude", "codex"])
@LAWS
@given(data=st.data())
def test_pick_excludes_no_lane_the_base_recommended(provider, realistic, data):
    rules, view, _ = data.draw(fleets(provider, realistic))
    base = _load("f3bffcea")
    for model in (None, *(name for name, m in rules["models"].items() if m["provider"] == provider)):
        old = base["picker"].rank(rules, copy.deepcopy(view), family=provider, model=model)
        new = head_picker.rank(rules, copy.deepcopy(view), family=provider, model=model)
        excluded = {row["lane_id"]: row["reasons"] for row in new["excluded"]}
        lost = {row["lane_id"]: excluded.get(row["lane_id"]) for row in old["ranked"]}
        lost = {k: v for k, v in lost.items() if k not in {row["lane_id"] for row in new["ranked"]}}
        assert not lost, (model, lost)


def live_claude_9_view(rules):
    """claude-9 at 2026-10-03T13:05:30Z, from a read-only .backup of the live store.

    Its account windows were read 29 s earlier by an attempt's rate_limit_event.
    Its newest Fable-scoped weekly row is from 2026-09-27 (the CLI has not
    reported that bucket since), with a reset of 2026-10-03T13:00:00Z."""
    t = lambda s: s
    readings = [
        {"lane_id": "claude-9", "scope": "account", "window": "five_hour", "utilization": .02,
         "resets_at": t("2026-10-03T18:00:00Z"), "observed_at": t("2026-10-03T13:05:01Z"),
         "label": "provider", "source": "rate_limit_event"},
        {"lane_id": "claude-9", "scope": "account", "window": "seven_day", "utilization": 0.0,
         "resets_at": t("2026-10-10T13:00:00Z"), "observed_at": t("2026-10-03T13:05:01Z"),
         "label": "provider", "source": "rate_limit_event"},
        {"lane_id": "claude-9", "scope": rules["models"]["fable"]["id"], "window": "seven_day",
         "utilization": .21, "resets_at": t("2026-10-03T13:00:00Z"), "observed_at": t("2026-09-27T23:53:11Z"),
         "label": "provider", "source": "rate_limit_event"}]
    lanes = [{"lane_id": "claude-9", "provider": "claude", "owner": "v2", "enabled": True, "desktop": False,
              "account_key": "claude-9", "home": "/lanes/claude-9", "email": "claude-9@example.test",
              "identity_status": "verified"}]
    return head_capacity.build_view(lanes, readings, [], [], [], now="2026-10-03T13:05:30Z", reading_ttl_s=TTL)


def test_a_bucket_the_provider_stopped_reporting_does_not_unmeasure_a_freshly_read_lane():
    rules = policy()
    view = live_claude_9_view(rules)
    job = {"pinned_model": "fable", "pinned_lane": "claude-9", "sandbox": "workspace-write", "tier": "hard"}
    base = _load("f3bffcea")["scheduler"]
    assert base.probe_required(base.evaluate(rules, copy.deepcopy(view), job), job) is False
    decision = head_scheduler.evaluate(rules, copy.deepcopy(view), job)
    assert decision.chosen_lane == "claude-9"
    assert head_scheduler.probe_required(decision, job) is False, decision.evaluations[0]["candidate_details"]


def test_pick_keeps_a_freshly_read_lane_beside_a_bucket_the_provider_stopped_reporting():
    rules = policy()
    view = live_claude_9_view(rules)
    old = _load("f3bffcea")["picker"].rank(rules, copy.deepcopy(view), family="claude")
    assert [row["lane_id"] for row in old["ranked"]] == ["claude-9"]
    new = head_picker.rank(rules, copy.deepcopy(view), family="claude")
    assert [row["lane_id"] for row in new["ranked"]] == ["claude-9"], new["excluded"]


@pytest.mark.parametrize("provider,model", [("claude", "opus"), ("codex", "astra")])
@pytest.mark.parametrize("scoped_first", [True, False])
def test_equal_weekly_headroom_binds_the_earliest_reset(provider, model, scoped_first):
    """C-11.3: "Equal weekly headrooms bind the earliest known reset, then scope."

    The reviewer's mutation `binding-tie-latest-reset` (bind the latest reset on
    a tie) survived every scheduler, weekly, picker, horizon, route-check,
    split and uncapped test of 7bab0816."""
    rules = policy()
    early, late = iso(3_600), iso(4 * 86_400)
    scoped_id = rules["models"][model]["id"]
    identity = f"{provider}-0"
    rows = [{"lane_id": identity, "scope": "account", "window": "seven_day", "utilization": .4,
             "resets_at": late if scoped_first else early, "observed_at": iso(0), "label": "provider",
             "source": "rate_limit_event"},
            {"lane_id": identity, "scope": scoped_id, "window": "seven_day", "utilization": .4,
             "resets_at": early if scoped_first else late, "observed_at": iso(0), "label": "provider",
             "source": "rate_limit_event"}]
    lanes = [{"lane_id": identity, "provider": provider, "owner": "v2", "enabled": True, "desktop": False,
              "account_key": identity, "home": "/lanes/" + identity}]
    view = head_capacity.build_view(lanes, rows, [], [], [], now=iso(0), reading_ttl_s=TTL)
    detail = head_scheduler.evaluate(rules, view, {"pinned_model": model}).evaluations[0]["candidate_details"][identity]
    assert detail["seven_day_reset"] == early
    assert detail["weekly_scope"] == (scoped_id if scoped_first else "account")


@pytest.mark.parametrize('provider,model', [('claude', 'fable'), ('codex', 'astra')])
@pytest.mark.parametrize('scope_kind', ['account', 'model'])
def test_stopped_windows_do_not_demote_a_lane_forever(provider, model, scope_kind):
    rules = policy()
    scope = 'account' if scope_kind == 'account' else rules['models'][model]['id']
    identity = f'{provider}-0'
    lanes = [{'lane_id': identity, 'provider': provider, 'owner': 'v2', 'enabled': True,
              'desktop': False, 'account_key': identity, 'home': '/lanes/' + identity,
              'email': identity + '@example.test'}]
    rows = [{'lane_id': identity, 'scope': 'account', 'window': 'seven_day', 'utilization': .2,
             'observed_at': iso(0), 'resets_at': iso(3600), 'label': 'provider'},
            {'lane_id': identity, 'scope': scope, 'window': 'five_hour', 'utilization': .9,
             'observed_at': iso(-6 * 86400), 'resets_at': iso(-86400), 'label': 'provider'}]
    view = head_capacity.build_view(lanes, rows, [], [], [], now=iso(0), reading_ttl_s=TTL)
    detail = head_scheduler.evaluate(rules, view, {'pinned_model': model}).evaluations[0]['candidate_details'][identity]
    assert detail.get('ranking_measured', detail['measured']) is True
    assert detail['reserve_class'] == 'clear'
    assert head_scheduler.ranking_reading_age(detail, iso(0)) == 0


@pytest.mark.parametrize('age', [0, 30, 119])
def test_why_distinguishes_a_recent_renewal_from_admission_freshness(age):
    from subfleet.cli import _format_decision
    rules = policy()
    view = live_claude_9_view(rules)
    view['now'] = '2026-10-03T13:05:30Z'
    # Make the stopped window recent instead: ranking waits for it, admission
    # still has 29-second-old account evidence.
    row = next(r for r in view['readings'] if r['scope'] != 'account')
    row.update(observed_at=(datetime.fromisoformat(view['now'].replace('Z', '+00:00'))
                            - timedelta(seconds=age)).isoformat(), label='provider')
    decision = head_scheduler.evaluate(rules, view, {'pinned_model': 'fable'})
    detail = decision.evaluations[0]['candidate_details']['claude-9']
    assert detail['measured'] is True
    assert detail['ranking_measured'] is False
    assert detail['reading_renewed'] is True
    from dataclasses import asdict
    rendered = _format_decision({**asdict(decision), "evaluations": list(decision.evaluations)})
    assert 'usage=measured ranking=unmeasured renewal=pending' in rendered
    assert 'admission-headroom=98.0%' in rendered
    assert f'reading-age={max(29, age):.1f}s' in rendered


def test_recent_renewal_uncertainty_ends_at_its_ttl_with_a_stable_horizon():
    rules = policy()
    view = live_claude_9_view(rules)
    renewed = next(r for r in view['readings'] if r['scope'] != 'account')
    now = datetime.fromisoformat(view['now'].replace('Z', '+00:00'))
    renewed.update(observed_at=(now - timedelta(seconds=119)).isoformat(), label='provider')
    first = head_scheduler.evaluate(rules, copy.deepcopy(view), {'pinned_model': 'fable'})
    detail = first.evaluations[0]['candidate_details']['claude-9']
    assert not detail.get('ranking_measured', detail['measured'])
    horizon = head_capacity.lane_horizons(view, reading_ttl_s=TTL)['claude-9']
    assert horizon == now + timedelta(seconds=1)
    later = {**copy.deepcopy(view), 'now': (now + timedelta(seconds=2)).isoformat()}
    next_detail = head_scheduler.evaluate(rules, later, {'pinned_model': 'fable'}).evaluations[0]['candidate_details']['claude-9']
    assert next_detail.get('ranking_measured', next_detail['measured'])
    assert next_detail['reading_renewed'] is False
    assert next_detail['weekly_headroom'] == 1


@pytest.mark.parametrize('observed,expected_horizon', [(2, 2), (-119, 1)])
def test_stale_labeled_reading_recency_has_a_judgement_horizon(observed, expected_horizon):
    """Explanatory evidence can enter/leave the TTL set without a reset clock."""
    rules = policy()
    identity = 'codex-0'
    lanes = [{'lane_id': identity, 'provider': 'codex', 'owner': 'v2', 'enabled': True,
              'desktop': False, 'account_key': identity, 'home': '/lanes/' + identity}]
    rows = [{'lane_id': identity, 'scope': scope, 'window': 'seven_day', 'utilization': .2,
             'observed_at': iso(age), 'resets_at': None, 'label': 'stale-provider'}
            for scope, age in [('account', -7 * 86400), (rules['models']['astra']['id'], observed)]]
    def detail(at):
        view = head_capacity.build_view(lanes, rows, [], [], [], now=iso(at), reading_ttl_s=TTL)
        return view, head_scheduler.evaluate(rules, view, {'pinned_model': 'astra'}).evaluations[0]['candidate_details'][identity]
    view, first = detail(0)
    assert head_capacity.lane_horizons(view, reading_ttl_s=TTL) == {identity: NOW + timedelta(seconds=expected_horizon)}
    _, before = detail(expected_horizon - .5)
    assert before == first
    _, after = detail(expected_horizon + 1)
    assert after['reading_observed_at'] != first['reading_observed_at']
    assert after['measured'] is False and after['ranking_measured'] is False
