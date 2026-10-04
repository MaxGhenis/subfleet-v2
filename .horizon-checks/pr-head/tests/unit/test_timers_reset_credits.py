"""Offline action-state and gift-policy checks (C-19, C-23.7, C-23.13, C-23.16–18)."""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from subfleet.actions import Actions, ResetCredits, fleet_credits_remaining
from subfleet.adapters.codex import (
    CodexAdapter, RESET_CREDIT_URLS, WHAM_RESET_CREDITS_CONSUME_URL, WHAM_RESET_CREDITS_URL,
)
from subfleet.capacity import from_store
from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, Reading, ReadingLabel
from subfleet.store import Store

NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
STAMP = "2026-09-05T12:00:00Z"
GIFT = {"id": "gift-1", "reset_type": "codex_rate_limits", "status": "available"}


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "state.db") as value:
        yield value


def lane(store, tmp_path, number=1, *, utilization=1., days=3, shadowed=False):
    home = tmp_path / f"codex-{number}"
    home.mkdir(exist_ok=True)
    (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "test-only", "account_id": str(number)}}))
    value = Lane(f"codex-{number}", "codex", f"codex:account-{number}", Credential("codex", str(home), "home"),
                 str(home), LaneOwner.V2, False)
    store.put_lane(value)
    reset = (NOW + timedelta(days=days)).isoformat().replace("+00:00", "Z")
    store.add_reading(Reading(value.lane_id, "account", "seven_day", utilization, reset,
                              ReadingLabel.PROVIDER, "wham", STAMP))
    if utilization == 1:
        store.put_closure(Closure(value.lane_id, "account", reset, ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, None))
    return value


def snapshot(store, **overrides):
    view = from_store(store, now=NOW)
    for row in view["lanes"]:
        row["dispatchable"] = not bool(row["closures"])
        row["probe"] = {"status": "limited" if row["closures"] else "ok",
                        "limit_reached": bool(row["closures"]), "checked_at": STAMP,
                        "reset_credits": {"available": 2, "applicable": 2}}
        row.update(overrides.get(row["lane_id"], {}))
    return view


class HTTP:
    def __init__(self, *, store=None, response=None, credits=None):
        self.store, self.calls = store, []
        self.response = {"code": "reset", "windows_reset": 2} if response is None else response
        self.credits = [GIFT] if credits is None else credits

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        assert timeout > 0
        if request.full_url == WHAM_RESET_CREDITS_URL:
            return 200, json.dumps({"credits": self.credits}).encode()
        assert request.full_url == WHAM_RESET_CREDITS_CONSUME_URL
        assert request.get_method() == "POST"
        if self.store is not None:
            row = self.store.query("SELECT * FROM actions")[0]
            assert row["state"] == "executing"
            persisted = json.loads(row["request_json"])
            body = json.loads(request.data)
            assert body["redeem_request_id"] == persisted["redeem_request_id"]
            assert uuid.UUID(body["redeem_request_id"]).version == 4
            assert persisted["holder"]
            events = [row["kind"] for row in self.store.list_events()]
            assert events.index("action.pending") < events.index("action.executing")
        if isinstance(self.response, Exception):
            raise self.response
        return 200, json.dumps(self.response).encode()


def component(store, opener, **policy):
    return ResetCredits(store, {"reset_credits": policy}, lambda lane: CodexAdapter(opener=opener, now=lambda: NOW))


def test_pending_precedes_call_and_confirmed_reopens_without_inventing_windows(store, tmp_path):
    """C-19.1 C-23.13 C-23.16 C-23.17: durable intent, holder, confirmation and numeric-free reopening."""
    target = lane(store, tmp_path)
    store.put_closure(Closure(target.lane_id, "gpt-6-astra", "2026-09-06T12:00:00Z", ClosureReason.COOLDOWN,
                              ClockSource.GUESSED, None))
    before = store.list_readings()
    http = HTTP(store=store)
    resets = component(store, http)
    result = resets.evaluate(snapshot(store), now=NOW)
    assert result["status"] == "confirmed"
    assert result["fleet_credits_remaining"] == 1
    assert not store.list_closures(active_at=STAMP)
    assert store.list_readings() == before
    override = resets.confirmed_override(target.lane_id, now=NOW)
    assert override["weekly_reset_at"] == "2026-09-12T12:00:00Z"
    assert override["clock_source"] == "guessed"
    assert "utilization" not in override


def test_confirmed_action_and_closure_release_commit_atomically(store, tmp_path, monkeypatch):
    """C-3.2 C-23.17: publication failure cannot leave a confirmed action with its old closure."""
    lane(store, tmp_path)
    resets = component(store, HTTP())
    def fail_release(*args, **kwargs):
        raise OSError("injected publication failure")
    monkeypatch.setattr(resets, "_release", fail_release)
    with pytest.raises(OSError):
        resets.evaluate(snapshot(store), now=NOW)
    action = store.query("SELECT * FROM actions")[0]
    assert action["state"] == "executing" and action["result_json"] is None
    assert store.list_closures(active_at=STAMP)
    resets.recover(now=NOW)
    assert store.get_action(action["action_id"])["state"] == "unknown"


@pytest.mark.parametrize("response", [{"code": "reset", "windows_reset": 0}, {"code": "success", "windows_reset": 2},
                                     {"code": "reset", "windows_reset": True}, {"code": "reset", "windows_reset": "2"}])
def test_only_exact_provider_success_confirms(store, tmp_path, response):
    """C-23.16: only reset with a positive integer windows_reset confirms consumption."""
    lane(store, tmp_path)
    result = component(store, HTTP(response=response)).evaluate(snapshot(store), now=NOW)
    assert result["status"] == "failed"
    assert store.list_closures(active_at=STAMP)


def test_timeout_is_unknown_until_usage_read_without_overwriting_result(store, tmp_path):
    """C-19.1 C-23.13 C-9.6: unknown is reconciled by read evidence, never blindly retried or overwritten."""
    target = lane(store, tmp_path)
    http = HTTP(response=TimeoutError())
    resets = component(store, http, min_interval_min=0)
    view = snapshot(store)
    result = resets.evaluate(view, now=NOW)
    original = store.get_action(result["action_id"])
    assert result["status"] == "unknown"
    assert result["error_type"] == "TimeoutError"
    assert resets.evaluate(view, now=NOW + timedelta(hours=1))["status"] == "unsettled-action"
    assert resets.settle_by_usage(target.lane_id, {"status": "limited", "limit_reached": True}, now=NOW) is None
    settled = resets.settle_by_usage(target.lane_id, {"status": "ok", "limit_reached": False,
                                    "checked_at": "2026-09-05T12:05:00Z"}, now=NOW + timedelta(minutes=5))
    assert settled["effective_state"] == "settled"
    assert settled["outcome"] == "usage-open"
    assert store.get_action(result["action_id"]) == original
    assert not store.list_closures(active_at=STAMP)
    assert resets.settle_by_usage(target.lane_id, {"status": "ok", "limit_reached": False}, now=NOW) is None
    assert resets.evaluate(view, now=NOW + timedelta(hours=1))["status"] == "no-concrete-credit"
    assert sum(request.get_method() == "POST" for request, _ in http.calls) == 1


def test_deadline_fences_late_success_and_returns_promptly(store, tmp_path):
    """C-16.4 C-23.13: late HTTP success cannot overwrite the bounded worker's unknown result."""
    lane(store, tmp_path)
    entered, release = threading.Event(), threading.Event()
    http = HTTP()
    def opener(request, timeout):
        if request.get_method() == "POST":
            entered.set()
            release.wait(2)
        return http(request, timeout)
    resets = component(store, opener)
    started = time.monotonic()
    result = resets.evaluate(snapshot(store), now=NOW, deadline=started + .1)
    assert time.monotonic() - started < .5
    assert entered.is_set() and result["status"] == "unknown"
    release.set()
    assert store.get_action(result["action_id"])["state"] == "unknown"


def test_one_credit_per_evaluation_and_minimum_interval_including_imported_actions(store, tmp_path):
    """C-18.1 C-23.16: one credit per cycle and imported redemption history obey the interval."""
    lane(store, tmp_path, 1)
    lane(store, tmp_path, 2)
    http = HTTP()
    resets = component(store, http)
    view = snapshot(store)
    assert resets.evaluate(view, now=NOW)["status"] == "confirmed"
    assert len(store.query("SELECT * FROM actions")) == 1
    assert sum(request.get_method() == "POST" for request, _ in http.calls) == 1
    assert resets.evaluate(view, now=NOW + timedelta(minutes=29))["status"] == "interval-blocked"
    for row in view["lanes"]:
        row["probe"]["checked_at"] = "2026-09-05T12:30:00Z"
    assert resets.evaluate(view, now=NOW + timedelta(minutes=30))["status"] == "confirmed"
    store.add_action(action_id="imported", kind="reset-credit", op_key="imported-account:gift-old", subject="old-home",
                     state="confirmed", created_at="2026-09-05T13:00:00Z", updated_at="2026-09-05T13:00:00Z")
    assert resets.evaluate(view, now=NOW + timedelta(minutes=61))["status"] == "interval-blocked"


def test_order_furthest_reset_then_fewest_inflight_then_lane_number(store, tmp_path):
    """C-23.38: redemption orders distant weekly resets, occupancy, and numeric lane order."""
    for number, days in [(1, 2), (2, 4), (3, 4), (10, 4)]:
        lane(store, tmp_path, number, days=days)
    view = snapshot(store, **{"codex-2": {"in_flight": 2}})
    result = component(store, HTTP()).evaluate(view, now=NOW)
    assert result["lane_id"] == "codex-3"


def test_shadowed_lane_waits_for_all_concrete_unshadowed_gifts(store, tmp_path):
    """C-23.46: a shadowed distant reset loses to a nearer unshadowed concrete gift."""
    lane(store, tmp_path, 1, days=6)
    lane(store, tmp_path, 2, days=2)
    result = component(store, HTTP()).evaluate(snapshot(store, **{"codex-1": {"app_shadowed": True}}), now=NOW)
    assert result["lane_id"] == "codex-2"


def test_shadowed_lane_can_redeem_when_unshadowed_has_no_concrete_gift(store, tmp_path):
    """C-23.46 C-23.16: unreadable or empty unshadowed entitlements do not manufacture a gift."""
    lane(store, tmp_path, 1, days=6)
    lane(store, tmp_path, 2, days=2)
    def opener(request, timeout):
        account = dict((key.lower(), value) for key, value in request.header_items())["chatgpt-account-id"]
        if account == "2":
            return 200, b'{"credits":[]}'
        return HTTP()(request, timeout)
    result = component(store, opener).evaluate(snapshot(store, **{"codex-1": {"app_shadowed": True}}), now=NOW)
    assert result["lane_id"] == "codex-1"


def test_shadowed_lane_waits_for_gift_on_an_unlimited_unshadowed_lane(store, tmp_path):
    """C-23.46: even a currently unlimited unshadowed gift holder excludes shadowed redemption."""
    lane(store, tmp_path, 1, days=6)
    lane(store, tmp_path, 2, utilization=.99)
    http = HTTP()
    result = component(store, http).evaluate(snapshot(store, **{"codex-1": {"app_shadowed": True}}), now=NOW)
    assert result["status"] == "shadow-excluded"
    assert not store.query("SELECT * FROM actions")
    assert all(request.get_method() == "GET" for request, _ in http.calls)


@pytest.mark.parametrize("invalid", [dict(GIFT, reset_type="paid_credits"), dict(GIFT, status="used"),
                                      dict(GIFT, source="purchased"), dict(GIFT, gifted=False), dict(GIFT, id="")])
def test_gifted_only_allowlist_and_concrete_entitlement_gate(store, tmp_path, invalid):
    """C-23.7 C-23.16: purchase, used, wrong-type and missing-id credits never reach POST."""
    target = lane(store, tmp_path)
    http = HTTP(credits=[invalid])
    assert component(store, http).evaluate(snapshot(store), now=NOW)["status"] == "no-concrete-credit"
    assert all(request.get_method() == "GET" for request, _ in http.calls)
    assert RESET_CREDIT_URLS == {WHAM_RESET_CREDITS_URL, WHAM_RESET_CREDITS_CONSUME_URL}
    with pytest.raises(ValueError):
        CodexAdapter(opener=http).consume_reset_credit(target, invalid, str(uuid.uuid4()))


def test_limit_reached_and_fleet_trigger_are_both_required(store, tmp_path):
    """C-23.16 C-18.1: neither entitlement possession nor a local closure alone admits redemption."""
    lane(store, tmp_path, 1)
    lane(store, tmp_path, 2, utilization=.2)
    http = HTTP()
    resets = component(store, http)
    assert resets.evaluate(snapshot(store), now=NOW)["status"] == "not-triggered"
    view = snapshot(store)
    for row in view["lanes"]:
        row.update(dispatchable=False, probe={"status": "ok", "limit_reached": False})
    assert resets.evaluate(view, now=NOW)["status"] == "no-concrete-credit"
    assert not http.calls


def test_headroom_is_summed_across_dispatchable_lanes(store, tmp_path):
    """C-18.1: policy sums weekly headroom across dispatchable lanes before spending a gift."""
    lane(store, tmp_path, 1)
    lane(store, tmp_path, 2, utilization=.92)
    lane(store, tmp_path, 3, utilization=.92)
    http = HTTP()
    view = snapshot(store)
    assert component(store, http).evaluate(view, now=NOW)["status"] == "not-triggered"
    assert component(store, http, headroom_floor_pct=17).evaluate(view, now=NOW)["status"] == "confirmed"


def test_op_key_is_unique_across_components_and_attempted_credits_never_retry(store, tmp_path):
    """C-19.1 C-23.13: account-plus-credit uniqueness survives evaluator recreation and failed calls."""
    target = lane(store, tmp_path)
    http = HTTP(response={"code": "already_consumed", "windows_reset": 0})
    view = snapshot(store)
    assert component(store, http).evaluate(view, now=NOW)["status"] == "failed"
    assert component(store, http).evaluate(view, now=NOW)["status"] == "no-concrete-credit"
    row = store.query("SELECT * FROM actions")[0]
    assert row["op_key"] == target.account_key + ":" + GIFT["id"]
    assert sum(request.get_method() == "POST" for request, _ in http.calls) == 1


@pytest.mark.parametrize("prefix", ["", "reset-credit:"])
def test_imported_credit_keys_never_retry_after_eight_days(store, tmp_path, prefix):
    target = lane(store, tmp_path)
    old = (NOW - timedelta(days=8)).isoformat()
    store.add_action(action_id="imported", kind="reset-credit",
                     op_key=prefix + target.account_key + ":" + GIFT["id"],
                     subject=target.account_key, state="confirmed", created_at=old, updated_at=old,
                     request_json=json.dumps({"source": "v1-reset-policy", "credit_id": GIFT["id"]}))
    before = store.get_action("imported")
    http = HTTP()
    resets = component(store, http)
    assert resets.confirmed_override(target.lane_id, now=NOW) is None
    assert resets.evaluate(snapshot(store), now=NOW)["status"] == "no-concrete-credit"
    assert [request.get_method() for request, _ in http.calls] == ["GET"]
    assert store.query("SELECT * FROM actions") == [before]


def test_credit_claim_rechecks_legacy_import_added_after_listing(store, tmp_path, monkeypatch):
    from contextlib import contextmanager

    target = lane(store, tmp_path)
    old = (NOW - timedelta(days=8)).isoformat()
    transaction = store.transaction

    @contextmanager
    def import_before_claim(kind, **kwargs):
        if kind == "action.pending":
            store.add_action(action_id="imported", kind="reset-credit", state="confirmed",
                             op_key="reset-credit:" + target.account_key + ":" + GIFT["id"],
                             subject=target.account_key, created_at=old, updated_at=old, request_json="{}")
        with transaction(kind, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(store, "transaction", import_before_claim)
    http = HTTP()
    assert component(store, http).evaluate(snapshot(store), now=NOW)["status"] == "already-attempted"
    assert [request.get_method() for request, _ in http.calls] == ["GET"]
    assert len(store.query("SELECT * FROM actions")) == 1


def test_only_holder_publishes_and_terminal_result_is_immutable(store):
    """C-23.13: stale holders and late successes are discarded with explicit audit evidence."""
    store.add_action(action_id="action", kind="reset-credit", op_key="account:credit", subject="lane", request_json="{}")
    actions = Actions(store)
    assert actions.claim("action", "holder", now=STAMP)
    assert not actions.claim("action", "other", now=STAMP)
    assert not actions.publish("action", "other", "confirmed", {}, now=STAMP)
    assert actions.publish("action", "holder", "unknown", {"status": "timeout"}, now=STAMP)
    before = store.get_action("action")
    assert not actions.publish("action", "holder", "confirmed", {"code": "reset", "windows_reset": 2}, now=STAMP)
    assert store.get_action("action") == before
    discarded = store.query("SELECT * FROM events WHERE kind='action.result-discarded'")
    assert len([row for row in discarded if json.loads(row["data_json"]).get("action_id")]) == 2


def test_unknown_credit_count_makes_entire_fleet_total_null():
    """C-23.18: one unreadable count makes the fleet unknown rather than a partial sum."""
    rows = [{"provider": "codex", "lane_id": "a", "account_key": "a", "reset_credits": {"available": 3}},
            {"provider": "codex", "lane_id": "b", "account_key": "b", "reset_credits": {"available": None}}]
    assert fleet_credits_remaining(rows) is None
    assert fleet_credits_remaining(rows, spent_lane="a") is None
    rows[1]["reset_credits"]["available"] = 2
    assert fleet_credits_remaining(rows, spent_lane="a") == 4


def test_disabled_unreadable_lane_keeps_fleet_total_null_after_a_confirmed_spend(store, tmp_path):
    """C-23.18: action eligibility cannot remove an unreadable lane from the fleet credit total."""
    lane(store, tmp_path, 1)
    other = lane(store, tmp_path, 2)
    store.update_lane(other.lane_id, enabled=False)
    view = snapshot(store, **{"codex-2": {"probe": {"status": "auth-dead"}}})
    result = component(store, HTTP()).evaluate(view, now=NOW)
    assert result["status"] == "confirmed"
    assert result["fleet_credits_remaining"] is None


def test_confirmed_override_ends_after_provider_propagation(store, tmp_path):
    """C-23.17 C-9.6: actual usage settles the temporary authority without synthetic window data."""
    target = lane(store, tmp_path)
    resets = component(store, HTTP())
    resets.evaluate(snapshot(store), now=NOW)
    assert resets.confirmed_override(target.lane_id, now=NOW)
    resets.settle_by_usage(target.lane_id, {"status": "ok", "limit_reached": False,
                          "checked_at": "2026-09-05T12:05:00Z"}, now=NOW + timedelta(minutes=5))
    assert resets.confirmed_override(target.lane_id, now=NOW + timedelta(minutes=5)) is None


def test_lagging_usage_cannot_spend_another_gift_on_a_confirmed_open_lane(store, tmp_path):
    """C-23.17: an authoritative confirmed consume protects the lane while old usage propagates."""
    lane(store, tmp_path)
    http = HTTP(credits=[GIFT, dict(GIFT, id="gift-2")])
    resets = component(store, http)
    view = snapshot(store)
    assert resets.evaluate(view, now=NOW)["status"] == "confirmed"
    assert resets.evaluate(view, now=NOW + timedelta(minutes=31))["status"] == "no-concrete-credit"
    assert sum(request.get_method() == "POST" for request, _ in http.calls) == 1


@pytest.mark.parametrize("binding", ["account", "home", "request-account", "request-lane"])
def test_imported_account_or_home_subject_reopens_and_reconciles_the_correct_lane(store, tmp_path, binding):
    """C-19.1 C-23.17: imported confirmed actions retain account authority across subject formats."""
    target = lane(store, tmp_path)
    subject = target.account_key if binding == "account" else target.home if binding == "home" else "imported"
    request = {"account_key": target.account_key} if binding == "request-account" else (
        {"lane_id": target.lane_id} if binding == "request-lane" else {})
    store.add_action(action_id="imported", kind="reset-credit", op_key=target.account_key + ":gift-old",
                     subject=subject, request_json=json.dumps(request), state="confirmed",
                     created_at=STAMP, updated_at=STAMP)
    resets = component(store, HTTP())
    assert resets.confirmed_override(target.lane_id, now=NOW)["action_id"] == "imported"
    settled = resets.settle_by_usage(target.lane_id, {"status": "ok", "limit_reached": False, "checked_at": STAMP}, now=NOW)
    assert settled["action_id"] == "imported"
    assert resets.confirmed_override(target.lane_id, now=NOW) is None
    assert store.get_action("imported")["state"] == "confirmed"


def test_imported_home_cannot_reopen_another_account_after_home_rebinding(store, tmp_path):
    """C-1.4 C-23.17: a home path alone cannot transfer a prior account's reset authority."""
    target = lane(store, tmp_path)
    store.add_action(action_id="imported", kind="reset-credit", op_key="codex:another:gift-old",
                     subject=target.home, state="confirmed", created_at=STAMP, updated_at=STAMP)
    assert component(store, HTTP()).confirmed_override(target.lane_id, now=NOW) is None


@pytest.mark.parametrize("metadata", [
    {"checked_at": "2026-09-05T11:00:00Z"},
    {"checked_at": "2026-09-05T13:00:00Z"},
    {"account_key": "codex:another-account"},
])
def test_stale_future_or_wrong_account_usage_cannot_admit_a_reset(store, tmp_path, metadata):
    """C-23.16 C-1.4: redemption requires current usage from the bound account."""
    target = lane(store, tmp_path)
    http = HTTP()
    view = snapshot(store)
    view["lanes"][0]["probe"].update(metadata)
    assert component(store, http).evaluate(view, now=NOW)["status"] == "no-concrete-credit"
    assert not http.calls


@pytest.mark.parametrize("metadata", [
    {"checked_at": "2026-09-05T11:00:00Z"},
    {"checked_at": STAMP, "account_key": "codex:another-account"},
    {"probed_at": STAMP, "readings": [{"label": "provider", "observed_at": "2026-09-05T11:00:00Z"}]},
    {},
])
def test_unknown_reconciliation_requires_current_same_account_usage(store, tmp_path, metadata):
    """C-19.1 C-23.13 C-1.4: completion timestamps and wrong-account reads cannot settle unknown actions."""
    target = lane(store, tmp_path)
    resets = component(store, HTTP(response=TimeoutError()))
    result = resets.evaluate(snapshot(store), now=NOW)
    assert result["status"] == "unknown"
    probe = {"status": "ok", "limit_reached": False, **metadata}
    assert resets.settle_by_usage(target.lane_id, probe, now=NOW) is None
    assert store.list_closures(active_at=STAMP)
    assert not resets._reconciled() - {None}


def test_cancelled_evaluation_makes_no_request(store, tmp_path):
    """C-16.4: a cancelled timer creates no new remote action."""
    lane(store, tmp_path)
    cancel, http = threading.Event(), HTTP()
    cancel.set()
    assert component(store, http).evaluate(snapshot(store), now=NOW, cancel=cancel)["status"] == "cancelled"
    assert not http.calls and not store.query("SELECT * FROM actions")


def test_recovery_preserves_lost_holder_result_and_aborts_uncalled_pending_intent(store):
    """C-19.1 C-23.13: daemon recovery never retries an orphan call or invents its holder's result."""
    store.add_action(action_id="pending", kind="reset-credit", op_key="a:1", subject="a", request_json="{}")
    store.add_action(action_id="executing", kind="reset-credit", op_key="b:2", subject="b", request_json="{}")
    Actions(store).claim("executing", "lost-holder", now=STAMP)
    result = ResetCredits(store, {}).recover(now=NOW)
    assert set(result["recovered"]) == {"pending", "executing"}
    assert store.get_action("pending")["state"] == "failed"
    orphan = store.get_action("executing")
    assert orphan["state"] == "unknown" and orphan["result_json"] is None
    assert json.loads(orphan["request_json"])["holder"] == "lost-holder"


def test_a_stuck_list_cannot_accumulate_one_http_worker_per_cycle(store, tmp_path):
    """C-16.4: a timed-out lane retains its HTTP slot until its real worker finishes."""
    lane(store, tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def opener(request, timeout):
        calls.append(request)
        entered.set()
        release.wait(2)
        return 200, b'{"credits":[]}'
    resets = component(store, opener)
    try:
        view = snapshot(store)
        resets.evaluate(view, now=NOW, deadline=time.monotonic() + .05)
        assert entered.is_set()
        resets.evaluate(view, now=NOW, deadline=time.monotonic() + .05)
        resets.evaluate(view, now=NOW, deadline=time.monotonic() + .05)
        assert len(calls) == 1
    finally:
        release.set()
