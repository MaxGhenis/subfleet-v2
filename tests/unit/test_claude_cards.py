"""C-9.10: Claude limit-reset cards and promotional credits, read and never redeemed.

Payload fixtures are live reads from 2026-10-05 with identifying values replaced:
an account holding an unused card at its weekly limit, one whose card is used,
the same endpoint answered without Claude Code's User-Agent (`surface`), the
cloud-credit claim status, and an active and a lapsed profile.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import claude_cards as cc

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "claude_cards"
NOW = datetime(2026, 10, 5, 15, 30, tzinfo=timezone.utc)
CARD = "opus55-launch-promax-20260921"
GOOD_URLS = {cc.PROFILE_URL, cc.CARDS_USAGE_URL, cc.CLOUD_CREDIT_STATUS_URL}


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


# --- parsing ------------------------------------------------------------------


def test_unused_card_parsed_from_live_shape():
    """C-9.10: an unused card's id, count, window, what it clears and whether it can be used now."""
    cards = cc.parse_cards(fixture("usage_unused_card_at_limit")["cedar_ember"])
    assert cards["eligible"] is True and cards["at_limit"] is True
    assert cards["next_grant_id"] == CARD
    [grant] = cards["grants"]
    assert grant["id"] == CARD and grant["resets_left"] == 1 and grant["resets_total"] == 1
    assert grant["ends_at"] == "2026-10-22T16:00:00Z" and grant["starts_at"] == "2026-09-22T16:00:00Z"
    assert grant["clears"] == ["five_hour", "seven_day", "seven_day_overage_included"]
    assert grant["usable_now"] is True and grant["paused"] is False and grant["use_requires_limit"] is False


def test_used_card_and_surface_refusal():
    """C-9.10: a used card has nothing left; without Claude Code's User-Agent the server shows no grant."""
    [grant] = cc.parse_cards(fixture("usage_used_card")["cedar_ember"])["grants"]
    assert grant["resets_left"] == 0 and grant["usable_now"] is False
    refused = cc.parse_cards(fixture("usage_surface_ineligible")["cedar_ember"])
    assert refused == {**refused, "eligible": False, "ineligible_reason": "surface", "grants": []}


def test_cloud_credit_found_and_windows_ignored():
    """C-9.10: `iguana_necktie` is the cloud-session credit; usage windows are never credits."""
    [credit] = cc.parse_credits(fixture("usage_used_card"))
    assert credit == {"key": "iguana_necktie", "label": "cloud-session credit", "limit_dollars": 250.0,
                      "used_dollars": 0.0, "remaining_dollars": 250.0,
                      "expires_at": "2026-11-05T07:59:00Z", "locked_reason": None}


def test_unknown_dollar_block_kept_under_its_name():
    """C-9.10: a new promotion is shown before anyone has named it; a dollar window with no limit is not one."""
    payload = {"brass_thimble": {"limit_dollars": 40, "remaining_dollars": 12.5, "used_dollars": 27.5,
                                 "resets_at": "2026-12-01T00:00:00Z"},
               "five_hour": {"limit_dollars": 10, "remaining_dollars": 3},
               "seven_day_cowork": {"limit_dollars": 10, "remaining_dollars": 3},
               "tangelo": {"limit_dollars": None, "remaining_dollars": None}, "nimbus_quill": None}
    assert [credit["key"] for credit in cc.parse_credits(payload)] == ["brass_thimble"]
    assert cc.parse_credits(payload)[0]["label"] == "brass_thimble"


def test_claim_status_and_plan():
    """C-9.10: the claim status as `/claim-credit` reads it; the plan, and what a lapse looks like."""
    assert cc.parse_claim(fixture("promo_cloud_credit_active")) == {
        "eligible": False, "claimed": True, "state": "active",
        "claimed_at": "2026-09-30T19:19:39Z", "expires_at": "2026-11-05T07:59:00Z"}
    assert cc.parse_claim({"error": "nope"}) is None
    assert cc.parse_claim({"eligible": True, "state": "PROMO_STATE_NOT_CLAIMED"})["state"] == "not_claimed"
    assert cc.parse_claim({"eligible": True, "state": "something_new"})["state"] is None
    active, gone = cc.parse_plan(fixture("profile_active")), cc.parse_plan(fixture("profile_lapsed"))
    assert active["identity"] == "00000000-0000-4000-8000-000000000001:00000000-0000-4000-8000-0000000000aa"
    assert active["subscription_status"] == "active" and not cc.lapsed(active)
    assert gone["organization_type"] == "claude_free" and gone["subscription_status"] == "canceled"
    assert cc.lapsed(gone)
    assert cc.parse_plan({"account": {"uuid": "a"}}) is None


JSON = st.recursive(st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True) | st.text(max_size=8),
                    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=12), inner, max_size=5),
                    max_leaves=25)


@given(JSON)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_parsers_are_total(value):
    """C-9.10 invariant: no payload, however malformed, makes a parser raise or invent a card."""
    cards = cc.parse_cards(value)
    if cards is not None:
        assert all(isinstance(g["resets_left"], int) and g["resets_left"] >= 0 for g in cards["grants"])
    if isinstance(value, dict):
        for credit in cc.parse_credits(value):
            assert credit["limit_dollars"] > 0 and credit["remaining_dollars"] >= 0
        cc.parse_claim(value)
        cc.parse_plan(value)
        cc.parse_grant(value)


# --- what is about to be lost ---------------------------------------------------


def account(*, left=1, ends="2026-10-22T16:00:00Z", status="active", org="claude_max", credits=(),
            claim=None, login="max@example.org", lanes=("claude-1",)):
    return {"login": login, "lanes": list(lanes), "status": "ok",
            "plan": {"organization_type": org, "subscription_status": status},
            "cards": {"eligible": True, "grants": [{"id": CARD, "resets_left": left, "resets_total": 1,
                                                     "ends_at": ends, "usable_now": True}]},
            "credits": list(credits), "cloud_credit_claim": claim}


def kinds(rows):
    return sorted(row["kind"] for row in rows)


def test_card_expiring_only_inside_the_horizon():
    """C-9.10: an unused card warns within `warn_days` of its end, not before and not once used or ended."""
    near = account(ends=(NOW + timedelta(days=4)).isoformat())
    assert kinds(cc.warnings([near], NOW, warn_days=5)) == ["card-expiring"]
    assert cc.warnings([account()], NOW, warn_days=5) == []                     # 17 days out
    assert cc.warnings([account(left=0, ends=(NOW + timedelta(days=1)).isoformat())], NOW, warn_days=5) == []
    assert cc.warnings([account(ends=(NOW - timedelta(hours=1)).isoformat())], NOW, warn_days=5) == []


def test_lapse_risk_from_status_and_declared_end():
    """C-9.10: a card on a plan that is lapsing warns; a declared end after the card's own end does not."""
    assert kinds(cc.warnings([account(status="past_due")], NOW, warn_days=5)) == ["card-lapse-risk"]
    [row] = cc.warnings([account(status="canceled")], NOW, warn_days=5)
    assert row["reasons"] == ["plan lapsed"]
    soon = {"max@example.org": NOW + timedelta(days=2)}
    [row] = cc.warnings([account()], NOW, warn_days=5, plan_ends=soon)
    assert row["kind"] == "card-lapse-risk" and row["at"] == "2026-10-07T15:30:00Z"
    by_lane = {"claude-1": NOW + timedelta(days=2)}
    assert kinds(cc.warnings([account()], NOW, warn_days=5, plan_ends=by_lane)) == ["card-lapse-risk"]
    late = {"max@example.org": datetime(2026, 10, 30, tzinfo=timezone.utc)}
    # The card ends first, so only card-expiring speaks: the plan's end cannot take it.
    assert kinds(cc.warnings([account()], NOW, warn_days=60, plan_ends=late)) == ["card-expiring"]
    assert cc.warnings([account(left=0, status="canceled")], NOW, warn_days=5) == []


def test_credit_warnings():
    """C-9.10: money left on an expiring credit warns; an unclaimed eligible credit warns; spent or claimed do not."""
    credit = {"key": "iguana_necktie", "label": "cloud-session credit", "remaining_dollars": 250.0,
              "expires_at": (NOW + timedelta(days=3)).isoformat()}
    assert kinds(cc.warnings([account(left=0, credits=[credit])], NOW, warn_days=5)) == ["credit-expiring"]
    assert cc.warnings([account(left=0, credits=[{**credit, "remaining_dollars": 0.0}])], NOW, warn_days=5) == []
    lapsing = account(left=0, status="canceled", credits=[{**credit, "expires_at": "2026-11-05T07:59:00Z"}])
    [row] = cc.warnings([lapsing], NOW, warn_days=5)
    assert row["kind"] == "credit-lapse-risk" and row["reasons"] == ["plan lapsed"]
    ending = {"max@example.org": NOW + timedelta(days=2)}
    assert kinds(cc.warnings([account(left=0, credits=[{**credit, "expires_at": "2026-11-05T07:59:00Z"}])],
                             NOW, warn_days=5, plan_ends=ending)) == ["credit-lapse-risk"]
    claimable = {"eligible": True, "claimed": False, "state": "not_claimed"}
    assert kinds(cc.warnings([account(left=0, claim=claimable)], NOW, warn_days=5)) == ["credit-claimable"]
    # Claude Code's own test is eligible and not claimed; an unknown state does not hide it.
    assert kinds(cc.warnings([account(left=0, claim={**claimable, "state": None})], NOW, warn_days=5)) == ["credit-claimable"]
    over = {**claimable, "expires_at": (NOW - timedelta(days=1)).isoformat()}
    assert cc.warnings([account(left=0, claim=over)], NOW, warn_days=5) == []
    assert cc.warnings([account(left=0, claim={**claimable, "claimed": True, "state": "active"})], NOW, warn_days=5) == []


def test_plan_ends_file_is_lenient(tmp_path):
    """C-9.10: a bare date is that whole day; a typo is ignored, not fatal."""
    path = tmp_path / cc.PLAN_ENDS_FILE
    path.write_text(json.dumps({"max@example.org": "2026-10-09", "claude-3": "2026-10-09T12:00:00Z",
                                "bad": "Oct 9", "worse": 7}))
    ends = cc.load_plan_ends(path)
    assert ends == {"max@example.org": datetime(2026, 10, 10, tzinfo=timezone.utc),
                    "claude-3": datetime(2026, 10, 9, 12, tzinfo=timezone.utc)}
    path.write_text("{not json")
    assert cc.load_plan_ends(path) == {}


ACCOUNTS = st.builds(
    lambda left, days, status, remaining, credit_days, claimed: account(
        left=left, ends=(NOW + timedelta(days=days)).isoformat(), status=status,
        credits=[{"key": "iguana_necktie", "label": "c", "remaining_dollars": remaining,
                  "expires_at": (NOW + timedelta(days=credit_days)).isoformat()}],
        claim={"eligible": True, "claimed": claimed, "state": None}),
    st.integers(0, 2), st.floats(-3, 40), st.sampled_from(["active", "past_due", "canceled", None]),
    st.floats(0, 300), st.floats(-3, 40), st.booleans())


@given(st.lists(ACCOUNTS, max_size=4), st.floats(0, 30), st.floats(0, 30))
@settings(max_examples=200)
def test_warnings_monotone_in_horizon(rows, a, b):
    """C-9.10 invariant: a longer warning horizon never drops a warning a shorter one raised."""
    for index, row in enumerate(rows):
        row["login"] = f"login-{index}"
    short, long = sorted((a, b))
    keys = lambda days: {w["key"] for w in cc.warnings(rows, NOW, warn_days=days)}
    assert keys(short) <= keys(long)


@given(ACCOUNTS, st.floats(0, 10), st.floats(0, 20))
@settings(max_examples=200)
def test_expiring_card_keeps_warning_until_it_ends(row, warn, step):
    """C-9.10 invariant: once an unused card warns, it warns at every later instant before its end."""
    later = NOW + timedelta(days=step)
    ends = cc.parse_time(row["cards"]["grants"][0]["ends_at"])
    first = {w["key"] for w in cc.warnings([row], NOW, warn_days=warn) if w["kind"] == "card-expiring"}
    second = {w["key"] for w in cc.warnings([row], later, warn_days=warn) if w["kind"] == "card-expiring"}
    if later < ends:
        assert first <= second


# --- the sensor --------------------------------------------------------------------


class Wire:
    """A fake opener: one queue of answers per URL; every request kept for inspection."""

    def __init__(self, answers=None):
        self.answers = {url: list(rows) for url, rows in (answers or {}).items()}
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        queue = self.answers.get(request.full_url) or [(200, {}, None)]
        status, body, retry = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(status, Exception):
            raise status
        return status, json.dumps(body).encode(), retry


def healthy():
    return {cc.PROFILE_URL: [(200, fixture("profile_active"), None)],
            cc.CARDS_USAGE_URL: [(200, fixture("usage_unused_card_at_limit"), None)],
            cc.CLOUD_CREDIT_STATUS_URL: [(200, fixture("promo_cloud_credit_active"), None)]}


class Login:
    """The CLI's credential store for one folder: a heal renews it when `renews`."""

    def __init__(self, *, expires_in_s=3600, renews=True, present=True, clock=lambda: NOW):
        self.expires_ms = (clock().timestamp() + expires_in_s) * 1000
        self.renews, self.present, self.heals, self.clock = renews, present, [], clock

    def read(self, home):
        if not self.present:
            return None
        return {"accessToken": "tok-secret", "expiresAt": self.expires_ms}

    def heal(self, home):
        self.heals.append(home.name)
        if self.renews:
            self.expires_ms = (self.clock().timestamp() + 8 * 3600) * 1000
        # The CLI's own words when a login's refresh token is dead (observed 2026-10-05).
        return (0, "", "") if self.renews else (
            1, "", "Failed to authenticate: OAuth session expired and could not be refreshed")


def sensor(wire, login, *, now=lambda: NOW):
    return cc.Sensor(login_reader=login.read, heal=login.heal, version=lambda: "2.1.286", opener=wire, now=now)


def test_read_ok_sends_three_gets_with_claude_codes_agent(tmp_path):
    """C-9.10: profile, cards and claim, each a GET with Claude Code's own User-Agent; nothing else."""
    wire, login = Wire(healthy()), Login()
    row = sensor(wire, login).read(tmp_path / "max@example.org", allow_heal=True)
    assert row["status"] == "ok" and row["identity"].endswith("0000000000aa")
    assert [r.full_url for r in wire.requests] == [cc.PROFILE_URL, cc.CARDS_USAGE_URL, cc.CLOUD_CREDIT_STATUS_URL]
    assert all(r.get_method() == "GET" for r in wire.requests)
    assert {r.get_header("User-agent") for r in wire.requests} == {"claude-cli/2.1.286 (external, cli)"}
    assert wire.requests[2].get_header("X-organization-uuid") == "00000000-0000-4000-8000-0000000000aa"
    assert row["cards"]["grants"][0]["resets_left"] == 1
    assert row["credits"][0]["remaining_dollars"] == 250.0 and row["cloud_credit_claim"]["state"] == "active"
    assert login.heals == [] and "tok-secret" not in json.dumps(row)


def test_expired_login_heals_once_then_reads(tmp_path):
    """C-9.10, C-23.47: one turn renews an expired login; the read follows; the heal is recorded."""
    wire, login = Wire(healthy()), Login(expires_in_s=-60)
    row = sensor(wire, login).read(tmp_path / "a", allow_heal=True)
    assert login.heals == ["a"] and row["status"] == "ok" and row["healed"] is True
    assert row["heal"]["refreshed"] is True and row["login_expires_ms"] == login.expires_ms


def test_dead_login_is_not_healed_again_until_it_changes(tmp_path):
    """C-9.10: a login the CLI says it cannot renew costs one turn, not one per cycle or per day;
    a new sign-in (a changed expiresAt) reopens it."""
    wire, login = Wire(healthy()), Login(expires_in_s=-60, renews=False)
    first = sensor(wire, login).read(tmp_path / "a", allow_heal=True)
    assert first["status"] == "login-dead" and wire.requests == [] and first["heal"]["transient"] is False
    again = sensor(wire, login, now=lambda: NOW + timedelta(days=3)).read(tmp_path / "a", allow_heal=True, previous=first)
    assert again["status"] == "login-dead" and login.heals == ["a"]
    login.expires_ms -= 1000          # signed in again: the stored login changed
    sensor(wire, login, now=lambda: NOW + timedelta(days=3, hours=1)).read(tmp_path / "a", allow_heal=True, previous=again)
    assert login.heals == ["a", "a"]


@pytest.mark.parametrize("rc,out", [(124, ""), (127, ""), (130, ""), (1, "network unreachable")])
def test_heal_that_never_reached_the_login_is_retried(tmp_path, rc, out):
    """C-9.10: a timeout, a missing CLI, a stop or an unexplained failure is not a dead login:
    it is `unavailable`, says no "sign in again", and is tried again after the heal interval."""
    login = Login(expires_in_s=-60, renews=False)
    login.heal = lambda home: (login.heals.append(home.name), (rc, out, ""))[1]
    wire = Wire(healthy())
    first = sensor(wire, login).read(tmp_path / "a", allow_heal=True, heal_after_s=3600)
    assert first["status"] == "unavailable" and first["heal"]["transient"] is True
    soon = sensor(wire, login, now=lambda: NOW + timedelta(minutes=30)).read(
        tmp_path / "a", allow_heal=True, previous=first, heal_after_s=3600)
    assert soon["status"] == "unavailable" and "next heal after 2026-10-05T16:30:00Z" in soon["detail"]
    later = sensor(wire, login, now=lambda: NOW + timedelta(minutes=61)).read(
        tmp_path / "a", allow_heal=True, previous=soon, heal_after_s=3600)
    assert len(login.heals) == 2 and later["status"] == "unavailable"


def test_renewed_login_that_expires_again_is_healed_again(tmp_path):
    """C-9.10: after a heal that worked, the next expiry is healed again once the interval has
    passed; inside it the detail says so instead of claiming the heal failed."""
    clock = {"now": NOW}
    login = Login(expires_in_s=-60, clock=lambda: clock["now"])
    wire = Wire(healthy())
    read = lambda prev: sensor(wire, login, now=lambda: clock["now"]).read(
        tmp_path / "a", allow_heal=True, previous=prev, heal_after_s=3600)
    first = read(None)
    assert first["status"] == "ok" and login.heals == ["a"]
    clock["now"] = NOW + timedelta(hours=9)                       # the renewed token has expired
    second = read(first)
    assert second["status"] == "ok" and login.heals == ["a", "a"]
    login.expires_ms = (clock["now"].timestamp() - 60) * 1000    # expires again at once
    clock["now"] += timedelta(minutes=10)
    third = read(second)
    assert third["status"] == "login-expired" and "did not renew" not in third["detail"]
    assert "expired again since the heal" in third["detail"] and login.heals == ["a", "a"]


def test_clock_stepping_back_withholds_a_heal(tmp_path):
    """C-9.10: a clock that steps back before the last heal opens no guard."""
    login = Login(expires_in_s=-60, renews=False)
    login.heal = lambda home: (login.heals.append(home.name), (124, "", ""))[1]
    first = sensor(Wire(healthy()), login).read(tmp_path / "a", allow_heal=True, heal_after_s=3600)
    login.expires_ms = (NOW - timedelta(hours=3)).timestamp() * 1000     # expired on the stepped-back clock too
    back = sensor(Wire(healthy()), login, now=lambda: NOW - timedelta(hours=2)).read(
        tmp_path / "a", allow_heal=True, previous=first, heal_after_s=3600)
    assert back["status"] == "unavailable" and login.heals == ["a"]


def test_no_turn_when_heal_not_allowed(tmp_path):
    """C-9.10: an expired login on an account that may not be spent on is reported, not healed, not read."""
    wire, login = Wire(healthy()), Login(expires_in_s=-60)
    row = sensor(wire, login).read(tmp_path / "a", allow_heal=False)
    assert row["status"] == "login-expired" and login.heals == [] and wire.requests == []


def test_rate_limit_waits_out_retry_after(tmp_path):
    """C-9.9, C-9.10: a 429 is waited out (at least an hour) before that login is asked again."""
    answers = healthy()
    answers[cc.CARDS_USAGE_URL] = [(urllib.error.HTTPError(cc.CARDS_USAGE_URL, 429, "busy", {"Retry-After": "120"}, None), None, None)]
    wire, login = Wire(answers), Login()
    first = sensor(wire, login).read(tmp_path / "a", allow_heal=True)
    assert first["status"] == "rate-limited" and first["retry_after_until"] == "2026-10-05T16:30:00Z"
    count = len(wire.requests)
    second = sensor(wire, login, now=lambda: NOW + timedelta(minutes=30)).read(tmp_path / "a", allow_heal=True, previous=first)
    assert second["status"] == "rate-limited" and len(wire.requests) == count


def test_lapsed_account_reads_no_usage(tmp_path):
    """C-9.10: a free, canceled account has no cards to read; the profile says so and nothing more is asked."""
    wire, login = Wire({cc.PROFILE_URL: [(200, fixture("profile_lapsed"), None)]}), Login()
    row = sensor(wire, login).read(tmp_path / "a", allow_heal=True)
    assert row["status"] == "lapsed" and row["cards"] is None
    assert [r.full_url for r in wire.requests] == [cc.PROFILE_URL]


def test_failed_read_keeps_last_good_cards(tmp_path):
    """C-9.10: a usage read that fails keeps what the last good read saw, with its time, and says why."""
    good = sensor(Wire(healthy()), Login()).read(tmp_path / "a", allow_heal=True)
    answers = healthy()
    answers[cc.CARDS_USAGE_URL] = [(OSError("down"), None, None)]
    later = sensor(Wire(answers), Login(), now=lambda: NOW + timedelta(hours=6)).read(tmp_path / "a", allow_heal=True, previous=good)
    assert later["status"] == "unavailable" and later["cards"] == good["cards"] and later["read_at"] == good["read_at"]


def test_failed_claim_read_keeps_last_claim(tmp_path):
    """C-9.10: a claim-status read that fails keeps the claim status the last read saw."""
    good = sensor(Wire(healthy()), Login()).read(tmp_path / "a", allow_heal=True)
    answers = healthy()
    answers[cc.CLOUD_CREDIT_STATUS_URL] = [(urllib.error.HTTPError(cc.CLOUD_CREDIT_STATUS_URL, 503, "x", {}, None), None, None)]
    later = sensor(Wire(answers), Login(), now=lambda: NOW + timedelta(hours=6)).read(tmp_path / "a", allow_heal=True, previous=good)
    assert later["status"] == "ok" and later["cloud_credit_claim"] == good["cloud_credit_claim"]


def test_http_client_errors_are_a_failed_read(tmp_path):
    """C-9.10: a truncated or garbled response (`http.client` errors are not OSError) is a failed
    read that keeps the last good snapshot, never an exception that loses a heal record."""
    import http.client
    good = sensor(Wire(healthy()), Login()).read(tmp_path / "a", allow_heal=True)
    answers = healthy()
    answers[cc.CARDS_USAGE_URL] = [(http.client.IncompleteRead(b"{"), None, None)]
    login = Login(expires_in_s=-60)
    later = sensor(Wire(answers), login, now=lambda: NOW + timedelta(hours=6)).read(tmp_path / "a", allow_heal=True, previous=good)
    assert later["status"] == "unavailable" and later["cards"] == good["cards"] and later["healed"] is True


def test_no_login_and_no_scope(tmp_path):
    """C-9.10: a folder with no login, and a token the profile refuses, each say so."""
    wire = Wire(healthy())
    assert sensor(wire, Login(present=False)).read(tmp_path / "a", allow_heal=True)["status"] == "no-login"
    wire = Wire({cc.PROFILE_URL: [(urllib.error.HTTPError(cc.PROFILE_URL, 403, "no", {}, None), None, None)]})
    assert sensor(wire, Login()).read(tmp_path / "a", allow_heal=True)["status"] == "no-scope"


STATUSES = st.sampled_from([200, 401, 403, 404, 429, 500, "neterr"])


@given(st.tuples(STATUSES, STATUSES, STATUSES), st.booleans(), st.integers(-7200, 7200), st.booleans())
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_sensor_is_read_only(tmp_path, codes, allow_heal, expires_in_s, renews):
    """C-9.10 invariant: whatever the server answers, every request is a GET to a known read
    endpoint, no claim or reset URL is ever asked, and at most one heal turn is spent per read."""
    bodies = {cc.PROFILE_URL: fixture("profile_active"), cc.CARDS_USAGE_URL: fixture("usage_used_card"),
              cc.CLOUD_CREDIT_STATUS_URL: fixture("promo_cloud_credit_active")}
    answers = {}
    for url, code in zip((cc.PROFILE_URL, cc.CARDS_USAGE_URL, cc.CLOUD_CREDIT_STATUS_URL), codes):
        if code == "neterr":
            answers[url] = [(OSError("x"), None, None)]
        elif code == 200:
            answers[url] = [(200, bodies[url], None)]
        else:
            answers[url] = [(urllib.error.HTTPError(url, code, "x", {"Retry-After": "5"}, None), None, None)]
    wire, login = Wire(answers), Login(expires_in_s=expires_in_s, renews=renews)
    row = sensor(wire, login).read(tmp_path / "a", allow_heal=allow_heal)
    assert row["status"] in cc.STATUSES
    assert all(r.get_method() == "GET" and r.full_url in GOOD_URLS for r in wire.requests)
    assert not any("reset_rate_limits" in r.full_url or "claim" in r.full_url for r in wire.requests)
    assert len(login.heals) <= (1 if allow_heal else 0)
    assert "tok-secret" not in json.dumps(row)


def test_module_never_names_a_claim_endpoint_as_a_url():
    """C-9.10: the only URLs the module can reach are the three reads."""
    urls = {value for name, value in vars(cc).items() if name.endswith("_URL")}
    assert urls == GOOD_URLS


# --- one pass over every login -------------------------------------------------------


LANES = [
    {"lane_id": "claude-2", "identity": None, "label": "max@pe.example", "account_key": "claude:max@pe.example", "held": True},
    {"lane_id": "claude-11", "identity": None, "label": "max@ax.example", "account_key": "claude:max@ax.example", "held": False},
    {"lane_id": "claude-18", "identity": None, "label": "max@ax.example", "account_key": "claude:max@ax.example", "held": False},
    {"lane_id": "claude-30", "identity": "00000000-0000-4000-8000-000000000001:00000000-0000-4000-8000-0000000000aa",
     "label": "renamed@example", "account_key": "claude:renamed@example", "held": False},
]


class Many:
    """Logins by folder name, each with its own `Login`."""

    def __init__(self, logins):
        self.logins = logins

    def read(self, home):
        return self.logins[home.name].read(home)

    def heal(self, home):
        return self.logins[home.name].heal(home)


def test_refresh_binds_heals_and_holds(tmp_path):
    """C-9.10, C-10.6: bind by identity when a lane recorded one, else by exact name; never spend a turn
    under an operator hold or on a login that backs no lane."""
    names = ["max@pe.example", "max@ax.example", "max@pe.example+team", "fresh@example"]
    logins = {name: Login(expires_in_s=-60) for name in names[:3]}
    logins["fresh@example"] = Login()
    many = Many(logins)
    sense = cc.Sensor(login_reader=many.read, heal=many.heal, version=lambda: "2.1.286", opener=Wire(healthy()), now=lambda: NOW)
    snap = cc.refresh(sense, logins=[tmp_path / name for name in names], lanes=LANES, previous=None,
                      heal=True, heal_after_s=43200)
    rows = {row["login"]: row for row in snap["accounts"]}
    assert rows["max@pe.example"]["status"] == "held" and logins["max@pe.example"].heals == []
    assert rows["max@pe.example"]["lanes"] == ["claude-2"] and rows["max@pe.example"]["lanes_by"] == "label"
    assert logins["max@ax.example"].heals == ["max@ax.example"]
    assert rows["max@pe.example+team"]["status"] == "login-expired" and logins["max@pe.example+team"].heals == []
    assert rows["max@pe.example+team"]["lanes"] == []
    # `fresh@example` reads the fixture profile, whose identity claude-30 recorded.
    assert rows["fresh@example"]["lanes"] == ["claude-30"] and rows["fresh@example"]["lanes_by"] == "identity"
    # After its read, max@ax.example's identity is claude-30's; the lanes that recorded no identity
    # stay bound by name beside it, so their holds and declared ends still apply.
    assert rows["max@ax.example"]["lanes"] == ["claude-11", "claude-18", "claude-30"]
    assert rows["max@ax.example"]["lanes_by"] == "identity+label"


def test_a_held_same_named_lane_still_holds_once_identity_is_known(tmp_path):
    """C-9.10: a lane with no recorded identity, bound by name and held, keeps its hold over the
    login after the login's identity is known and matches another lane."""
    lanes = [{"lane_id": "claude-30", "identity": "00000000-0000-4000-8000-000000000001:00000000-0000-4000-8000-0000000000aa",
              "label": "x", "account_key": "claude:x", "held": False},
             {"lane_id": "claude-31", "identity": None, "label": "who@example", "account_key": "claude:who@example",
              "held": True}]
    previous = {"version": 1, "accounts": [{"login": "who@example", "status": "ok", "lanes": [],
                                            "identity": lanes[0]["identity"]}]}
    login = Login(expires_in_s=-60)
    snap = cc.refresh(cc.Sensor(login_reader=login.read, heal=login.heal, version=lambda: "2", opener=Wire(healthy()),
                                now=lambda: NOW),
                      logins=[tmp_path / "who@example"], lanes=lanes, previous=previous, heal=True, heal_after_s=1)
    [row] = snap["accounts"]
    assert row["status"] == "held" and login.heals == [] and row["lanes"] == ["claude-30", "claude-31"]


def test_identity_lane_login_first_seen_expired_can_heal_and_bind(tmp_path):
    """C-9.10: a login first seen with an expired token, whose lane recorded an identity, is
    matched by name for the heal, then bound by the identity its profile returns."""
    lanes = [{"lane_id": "claude-30", "identity": "00000000-0000-4000-8000-000000000001:00000000-0000-4000-8000-0000000000aa",
              "label": "who@example", "account_key": "claude:x", "held": False}]
    login = Login(expires_in_s=-60)
    snap = cc.refresh(cc.Sensor(login_reader=login.read, heal=login.heal, version=lambda: "2", opener=Wire(healthy()),
                                now=lambda: NOW),
                      logins=[tmp_path / "who@example"], lanes=lanes, previous=None, heal=True, heal_after_s=1)
    [row] = snap["accounts"]
    assert login.heals == ["who@example"] and row["lanes"] == ["claude-30"] and row["lanes_by"] == "identity"


def test_a_login_that_raises_does_not_stop_the_pass(tmp_path):
    """C-9.10: a login whose read raises keeps its last snapshot; the others are read."""
    class Bad(Login):
        def read(self, home):
            if home.name == "bad":
                raise RuntimeError("boom")
            return super().read(home)
    previous = {"version": 1, "accounts": [{"login": "bad", "status": "ok", "cards": {"grants": []}, "lanes": []}]}
    snap = cc.refresh(cc.Sensor(login_reader=Bad().read, heal=None, version=lambda: "2", opener=Wire(healthy()),
                                now=lambda: NOW),
                      logins=[tmp_path / "bad", tmp_path / "good"], lanes=[], previous=previous, heal=False, heal_after_s=1)
    rows = {row["login"]: row for row in snap["accounts"]}
    assert rows["bad"]["status"] == "unavailable" and rows["bad"]["cards"] == {"grants": []}
    assert rows["good"]["status"] == "ok"


def test_settled_login_is_not_read_again_within_a_day(tmp_path):
    """C-9.10: a lapsed or dead login is read at most daily unless its stored login changes."""
    wire = Wire({cc.PROFILE_URL: [(200, fixture("profile_lapsed"), None)]})
    login = Login(expires_in_s=30 * 86400)
    sense = lambda when: cc.Sensor(login_reader=login.read, heal=login.heal, version=lambda: "2", opener=wire, now=lambda: when)
    first = cc.refresh(sense(NOW), logins=[tmp_path / "a"], lanes=[], previous=None, heal=True, heal_after_s=1)
    count = len(wire.requests)
    cc.refresh(sense(NOW + timedelta(hours=6)), logins=[tmp_path / "a"], lanes=[], previous=first, heal=True, heal_after_s=1)
    assert len(wire.requests) == count
    cc.refresh(sense(NOW + timedelta(hours=25)), logins=[tmp_path / "a"], lanes=[], previous=first, heal=True, heal_after_s=1)
    assert len(wire.requests) == count + 1


def test_stopped_pass_keeps_unread_logins(tmp_path):
    """C-9.10: a pass stopped part way keeps the last snapshot of every login it did not reach."""
    previous = {"version": 1, "accounts": [{"login": "b", "status": "ok", "cards": {"grants": []}}]}
    wire = Wire(healthy())
    sense = cc.Sensor(login_reader=Login().read, heal=None, version=lambda: "2", opener=wire, now=lambda: NOW)
    snap = cc.refresh(sense, logins=[tmp_path / "a", tmp_path / "b"], lanes=[], previous=previous,
                      heal=False, heal_after_s=1, stop=lambda: len(wire.requests) >= 3)   # after a's read
    assert [row["login"] for row in snap["accounts"]] == ["a", "b"] and snap["accounts"][1]["status"] == "ok"


def test_lapse_records_lost_cards_and_credits_once(tmp_path):
    """C-9.10: the read that first sees a lapse records what the last good read held (an unused card,
    money on a credit), one entry each, alerts once per item, and records nothing again."""
    login = Login(expires_in_s=30 * 86400)
    good = cc.refresh(cc.Sensor(login_reader=login.read, heal=None, version=lambda: "2", opener=Wire(healthy()), now=lambda: NOW),
                      logins=[tmp_path / "a"], lanes=[], previous=None, heal=False, heal_after_s=1)
    lapsed_wire = Wire({cc.PROFILE_URL: [(200, fixture("profile_lapsed"), None)]})
    later = cc.refresh(cc.Sensor(login_reader=login.read, heal=None, version=lambda: "2", opener=lapsed_wire,
                                 now=lambda: NOW + timedelta(days=1)),
                       logins=[tmp_path / "a"], lanes=[], previous=good, heal=False, heal_after_s=1)
    lost = later["accounts"][0]["lost"]
    assert [(item.get("grant") or item.get("credit"), item["reason"]) for item in lost] == [
        (CARD, "lapse"), ("iguana_necktie", "lapse")]
    shown = cc.view(later, NOW + timedelta(days=1), warn_days=5)
    assert kinds(shown["warnings"]) == ["card-lost", "credit-lost"]
    again = cc.refresh(cc.Sensor(login_reader=login.read, heal=None, version=lambda: "2", opener=lapsed_wire,
                                 now=lambda: NOW + timedelta(days=3)),
                       logins=[tmp_path / "a"], lanes=[], previous=later, heal=False, heal_after_s=1)
    assert again["accounts"][0]["lost"] == lost
    assert cc.view(again, NOW + timedelta(days=7), warn_days=5)["warnings"] == []


def test_card_unused_at_its_end_is_lost_and_a_used_one_is_not(tmp_path):
    """C-9.10: an `ok` read after a card's end that still shows it unused records it lost; one that
    shows it used does not."""
    login = Login(expires_in_s=60 * 86400)
    good = cc.refresh(cc.Sensor(login_reader=login.read, heal=None, version=lambda: "2", opener=Wire(healthy()), now=lambda: NOW),
                      logins=[tmp_path / "a"], lanes=[], previous=None, heal=False, heal_after_s=1)
    after_end = NOW + timedelta(days=18)                            # the card ends 2026-10-22T16:00Z
    expired = cc.refresh(cc.Sensor(login_reader=login.read, heal=None, version=lambda: "2", opener=Wire(healthy()),
                                   now=lambda: after_end),
                         logins=[tmp_path / "a"], lanes=[], previous=good, heal=False, heal_after_s=1)
    assert expired["accounts"][0]["lost"] == [{"grant": CARD, "at": cc.iso_utc(after_end), "reason": "expired"}]
    used = healthy()
    used[cc.CARDS_USAGE_URL] = [(200, fixture("usage_used_card"), None)]
    spent = cc.refresh(cc.Sensor(login_reader=login.read, heal=None, version=lambda: "2", opener=Wire(used), now=lambda: after_end),
                       logins=[tmp_path / "a"], lanes=[], previous=good, heal=False, heal_after_s=1)
    assert "lost" not in spent["accounts"][0]


def test_a_stale_snapshot_never_asserts_a_loss_or_repeats_one(tmp_path):
    """C-9.10: an account no read can see (here: expired, no lane, no heal) is never said to have lost
    a card or credit at its end, however long it stays stale; and once a loss is recorded, two
    things ending on different days are each told once, never again."""
    login = Login(expires_in_s=3600)
    good = cc.refresh(cc.Sensor(login_reader=login.read, heal=None, version=lambda: "2", opener=Wire(healthy()), now=lambda: NOW),
                      logins=[tmp_path / "a"], lanes=[], previous=None, heal=False, heal_after_s=1)
    snap, alerts = good, set()
    for step in range(0, 45 * 4):                                   # 45 days of 6-hourly passes
        when = NOW + timedelta(hours=6 * (step + 1))
        snap = cc.refresh(cc.Sensor(login_reader=login.read, heal=None, version=lambda: "2", opener=Wire(healthy()),
                                    now=lambda: when),
                          logins=[tmp_path / "a"], lanes=[], previous=snap, heal=False, heal_after_s=1)
        assert snap["accounts"][0]["status"] == "login-expired"
        alerts |= {row["key"] for row in cc.view(snap, when, warn_days=5)["warnings"] if row["kind"].endswith("-lost")}
    assert "lost" not in snap["accounts"][0] and alerts == set()
    # Fresh reads after each end: one card-lost and one credit-lost, each told once.
    login.expires_ms = (NOW + timedelta(days=90)).timestamp() * 1000
    gone = healthy()
    gone[cc.CARDS_USAGE_URL] = [(200, {**fixture("usage_unused_card_at_limit"), "iguana_necktie": None}, None)]
    snap, keys = good, []
    for when in (NOW + timedelta(days=18), NOW + timedelta(days=32), NOW + timedelta(days=33), NOW + timedelta(days=40)):
        opener = Wire(gone if when > NOW + timedelta(days=30) else healthy())
        snap = cc.refresh(cc.Sensor(login_reader=login.read, heal=None, version=lambda: "2", opener=opener, now=lambda: when),
                          logins=[tmp_path / "a"], lanes=[], previous=snap, heal=False, heal_after_s=1)
        keys += [row["key"] for row in cc.view(snap, when, warn_days=5)["warnings"] if row["kind"].endswith("-lost")]
    assert sorted(set(keys)) == ["a:credit-lost:iguana_necktie", "a:lost:" + CARD]
    assert [item.get("grant") or item.get("credit") for item in snap["accounts"][0]["lost"]] == [CARD, "iguana_necktie"]


def test_stopped_pass_reports_no_heal(tmp_path):
    """C-9.10: a login a stopped pass did not reach is not reported as healed in that pass."""
    previous = {"version": 1, "accounts": [{"login": "a", "status": "ok", "healed": True, "lanes": []}]}
    snap = cc.refresh(cc.Sensor(login_reader=Login().read, heal=None, version=lambda: "2", opener=Wire(healthy()),
                                now=lambda: NOW),
                      logins=[tmp_path / "a"], lanes=[], previous=previous, heal=False, heal_after_s=1, stop=lambda: True)
    assert snap["accounts"][0]["healed"] is False


def test_heal_is_checkpointed_before_the_next_login(tmp_path):
    """C-9.10: after a heal the snapshot is written before the next login is read, so a pass that dies
    later cannot spend the same turn again."""
    logins = {"a": Login(expires_in_s=-60), "b": Login()}
    many = Many(logins)
    written = []
    cc.refresh(cc.Sensor(login_reader=many.read, heal=many.heal, version=lambda: "2", opener=Wire(healthy()), now=lambda: NOW),
               logins=[tmp_path / "a", tmp_path / "b"],
               lanes=[{"lane_id": "claude-1", "identity": None, "label": "a", "account_key": "claude:a", "held": False}],
               previous=None, heal=True, heal_after_s=1, checkpoint=written.append)
    assert len(written) == 1 and [row["login"] for row in written[0]["accounts"]] == ["a"]
    assert written[0]["accounts"][0]["heal"]["refreshed"] is True


def test_claim_status_goes_stale(tmp_path):
    """C-9.10: a claim status not read for two days no longer says claimable."""
    claim = {"eligible": True, "claimed": False, "state": "not_claimed", "checked_at": cc.iso_utc(NOW)}
    assert cc.claimable(claim, NOW + timedelta(days=1))
    assert not cc.claimable(claim, NOW + timedelta(days=3))
    assert not cc.claimable({**claim, "state": "expired"}, NOW)


def test_snapshot_is_private(tmp_path):
    """C-8.1, C-9.10: the snapshot is published mode 0600 by temp, fsync and rename."""
    path = tmp_path / cc.SNAPSHOT_FILE
    cc.write_snapshot(path, {"version": 1, "accounts": []})
    assert (path.stat().st_mode & 0o777) == 0o600 and cc.read_snapshot(path) == {"version": 1, "accounts": []}


def test_snapshot_round_trip_and_view(tmp_path):
    """C-9.10: what is written is what is read back; the view counts unused cards and names warnings."""
    snap = cc.refresh(cc.Sensor(login_reader=Login().read, heal=None, version=lambda: "2", opener=Wire(healthy()),
                                now=lambda: NOW),
                      logins=[tmp_path / "max@ax.example"], lanes=LANES, previous=None, heal=False, heal_after_s=1)
    path = tmp_path / cc.SNAPSHOT_FILE
    cc.write_snapshot(path, snap)
    assert cc.read_snapshot(path) == json.loads(json.dumps(snap))
    shown = cc.view(cc.read_snapshot(path), datetime(2026, 10, 18, tzinfo=timezone.utc), warn_days=5)
    assert shown["accounts"][0]["unused_cards"] == 1
    assert kinds(shown["warnings"]) == ["card-expiring"]
    path.write_text('{"version": 99}')
    assert cc.read_snapshot(path) == {}


def test_cli_version_parsed_and_fallback():
    """C-9.10: the User-Agent carries the installed CLI's version, or a known one."""
    done = type("Done", (), {"stdout": "2.1.284 (Claude Code)\n"})
    assert cc.cli_version("claude", runner=lambda *a, **k: done) == "2.1.284"
    assert cc.cli_version("claude", runner=lambda *a, **k: (_ for _ in ()).throw(OSError())) == cc.FALLBACK_CLI_VERSION


def test_default_opener_is_the_adapters_network_seam(tmp_path, monkeypatch):
    """C-9.10: with no opener injected the sensor goes through `adapters.claude._urlopen`, the one
    seam the suite's `no_network` fixture replaces, so no test can reach Anthropic by accident."""
    from subfleet.adapters import claude as adapter
    seen = []

    def opener(request, timeout):
        seen.append(request.full_url)
        return 200, json.dumps(fixture("profile_lapsed")).encode()
    monkeypatch.setattr(adapter, "_urlopen", opener)
    row = cc.Sensor(login_reader=Login().read, heal=None, version=lambda: "2", now=lambda: NOW).read(tmp_path / "a", allow_heal=False)
    assert seen == [cc.PROFILE_URL] and row["status"] == "lapsed"


# --- the heal turn (C-23.47) -----------------------------------------------------------


def test_heal_turn_argv_environment_and_session(tmp_path, monkeypatch):
    """C-9.10, C-23.47: one Haiku turn under the login's folder, no MCP server, its own process
    group, and no inherited credential that could answer instead of the login."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "leaked-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leaked-key")
    seen = {}

    class Child:
        pid, returncode = 4242, 0

        def __init__(self, argv, **kwargs):
            seen.update(argv=argv, **kwargs)

        def communicate(self, timeout=None):
            return "ok", ""
    import signal
    kills = []           # the fake pid is no process of the test's; nothing is signalled for real
    monkeypatch.setattr(cc, "_kill_group", lambda pid, sig=signal.SIGKILL: kills.append((pid, sig)))
    rc, out, _err = cc.heal_turn(tmp_path / "max@example.org", claude_bin="claude", model="claude-haiku-4-5-20251001",
                                 prompt="Reply with exactly: ok", popen=Child)
    assert (rc, out) == (0, "ok") and kills == [(4242, signal.SIGKILL)]
    assert seen["argv"] == ["claude", "-p", "Reply with exactly: ok", "--model", "claude-haiku-4-5-20251001",
                            "--max-turns", "1", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    assert seen["start_new_session"] is True and seen["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "max@example.org")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen["env"] and "ANTHROPIC_API_KEY" not in seen["env"]
    assert seen["cwd"] != str(tmp_path) and seen["stdin"] is not None
    assert (seen["text"], seen["encoding"], seen["errors"]) == (True, "utf-8", "replace")


def test_heal_turn_missing_cli_is_127(tmp_path):
    """C-23.47: a CLI that cannot start is reported, not raised."""
    def refuse(*args, **kwargs):
        raise FileNotFoundError("claude")
    assert cc.heal_turn(tmp_path, model="m", prompt="p", popen=refuse)[0] == 127


def test_heal_turn_timeout_kills_the_whole_group(tmp_path):
    """C-23.47: a heal that outlives its deadline is killed with every process it started."""
    import os
    import time as clock
    marker = tmp_path / "grandchild.pid"
    fake = tmp_path / "claude"
    fake.write_text(f"#!/bin/sh\nsleep 60 &\necho $! > {marker}\nsleep 60\n")
    fake.chmod(0o755)
    started = clock.monotonic()
    rc, _out, err = cc.heal_turn(tmp_path / "home", claude_bin=str(fake), model="m", prompt="p", timeout=1)
    assert rc == 124 and "timed out" in err and clock.monotonic() - started < 30
    grandchild = int(marker.read_text())
    for _ in range(50):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        clock.sleep(.1)
    else:
        pytest.fail("the heal's background child outlived the group kill")


def test_heal_turn_stops_when_the_daemon_stops(tmp_path):
    """C-9.10, C-5.8a: a heal in flight when the daemon stops is killed within a second or two,
    so a stop never waits out the heal's 120 s."""
    import threading
    import time as clock
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nsleep 60\n")
    fake.chmod(0o755)
    cancel = threading.Event()
    threading.Timer(.5, cancel.set).start()
    started = clock.monotonic()
    rc, _out, err = cc.heal_turn(tmp_path / "home", claude_bin=str(fake), model="m", prompt="p", cancel=cancel)
    assert rc == 130 and "stopping" in err and clock.monotonic() - started < 10


def test_heal_turn_leaves_nothing_running_after_a_normal_exit(tmp_path):
    """C-9.10: whatever the turn started in its group is killed when it exits."""
    import os
    import time as clock
    marker = tmp_path / "child.pid"
    fake = tmp_path / "claude"
    fake.write_text(f"#!/bin/sh\nsleep 60 >/dev/null 2>&1 &\necho $! > {marker}\necho ok\n")
    fake.chmod(0o755)
    rc, out, _err = cc.heal_turn(tmp_path / "home", claude_bin=str(fake), model="m", prompt="p", timeout=30)
    assert rc == 0 and out.strip() == "ok"
    child = int(marker.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        clock.sleep(.1)
    else:
        pytest.fail("the heal's background child outlived the turn")


# --- review round 3 ------------------------------------------------------------------


def _usage_with(ends=None, credit_resets=None, cedar=None):
    payload = json.loads(json.dumps(fixture("usage_unused_card_at_limit")))
    if ends:
        payload["cedar_ember"]["grants"][0]["ends_at"] = ends
    if credit_resets:
        payload["iguana_necktie"]["resets_at"] = credit_resets
    if cedar is not None:
        payload["cedar_ember"] = cedar
    return payload


def _pass(login, opener, when, previous, tmp_path, **kwargs):
    return cc.refresh(cc.Sensor(login_reader=login.read, heal=None, version=lambda: "2", opener=opener, now=lambda: when),
                      logins=[tmp_path / "a"], lanes=[], previous=previous, heal=False, heal_after_s=1, **kwargs)


def test_a_card_whose_end_moved_later_is_not_lost_until_its_new_end(tmp_path):
    """C-9.10: a card judged by the end the read now gives: still live at its new end is not lost;
    unused past it is."""
    login = Login(expires_in_s=90 * 86400)
    good = _pass(login, Wire(healthy()), NOW, None, tmp_path)
    moved = healthy()
    moved[cc.CARDS_USAGE_URL] = [(200, _usage_with(ends="2026-10-29T16:00:00Z"), None)]
    later = _pass(login, Wire(moved), NOW + timedelta(days=18), good, tmp_path)
    assert "lost" not in later["accounts"][0]
    past = _pass(login, Wire(moved), NOW + timedelta(days=25), later, tmp_path)
    assert [item["grant"] for item in past["accounts"][0]["lost"]] == [CARD]


def test_a_read_that_lists_no_cards_proves_no_loss(tmp_path):
    """C-9.10: an `ok` read whose cards block is missing or ineligible says nothing about a card."""
    login = Login(expires_in_s=90 * 86400)
    good = _pass(login, Wire(healthy()), NOW, None, tmp_path)
    for cedar in (None, {"eligible": False, "ineligible_reason": "surface", "grants": []}):
        blind = healthy()
        payload = _usage_with()
        payload.pop("cedar_ember")
        if cedar is not None:
            payload["cedar_ember"] = cedar
        blind[cc.CARDS_USAGE_URL] = [(200, payload, None)]
        later = _pass(login, Wire(blind), NOW + timedelta(days=18), good, tmp_path)
        assert later["accounts"][0]["status"] == "ok" and "lost" not in later["accounts"][0]


def test_another_account_in_the_folder_records_no_loss(tmp_path):
    """C-9.10: a login folder signed into another account starts afresh; the old account's cards and
    credits are neither shown for it nor recorded lost."""
    login = Login(expires_in_s=90 * 86400)
    good = _pass(login, Wire(healthy()), NOW, None, tmp_path)
    other = json.loads(json.dumps(fixture("profile_lapsed")))
    other["account"]["uuid"], other["organization"]["uuid"] = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    later = _pass(login, Wire({cc.PROFILE_URL: [(200, other, None)]}), NOW + timedelta(days=1), good, tmp_path)
    row = later["accounts"][0]
    assert row["status"] == "lapsed" and "lost" not in row and row["cards"] is None and row["credits"] == []


def test_holds_are_read_again_for_every_login(tmp_path):
    """C-9.10: a hold placed while a pass runs covers the logins read after it."""
    logins = {"a": Login(expires_in_s=-60), "b": Login(expires_in_s=-60)}
    many = Many(logins)
    lanes = [{"lane_id": "claude-1", "identity": None, "label": "a", "account_key": "claude:a", "held": False},
             {"lane_id": "claude-2", "identity": None, "label": "b", "account_key": "claude:b", "held": False}]
    def current():
        if logins["a"].heals:                      # the operator holds b once a has been healed
            lanes[1]["held"] = True
        return lanes
    cc.refresh(cc.Sensor(login_reader=many.read, heal=many.heal, version=lambda: "2", opener=Wire(healthy()), now=lambda: NOW),
               logins=[tmp_path / "a", tmp_path / "b"], lanes=current, previous=None, heal=True, heal_after_s=1)
    assert logins["a"].heals == ["a"] and logins["b"].heals == []


def test_no_heal_starts_once_the_daemon_is_stopping(tmp_path):
    """C-9.10: a heal asked for after a stop began starts no process."""
    import threading
    stopping = threading.Event()
    stopping.set()
    def refuse(*args, **kwargs):
        raise AssertionError("a heal was started during a stop")
    assert cc.heal_turn(tmp_path, model="m", prompt="p", cancel=stopping, popen=refuse)[0] == 130


def test_a_stop_ends_a_read_between_requests(tmp_path):
    """C-9.10: a stop that lands during a read sends no further request."""
    wire = Wire(healthy())
    row = sensor(wire, Login()).read(tmp_path / "a", allow_heal=False, stop=lambda: len(wire.requests) >= 1)
    assert row["status"] == "unavailable" and [r.full_url for r in wire.requests] == [cc.PROFILE_URL]


def test_generic_auth_failure_is_not_a_dead_login(tmp_path):
    """C-9.10: only the CLI saying the login cannot be renewed makes it dead; a bare auth failure is retried."""
    login = Login(expires_in_s=-60, renews=False)
    login.heal = lambda home: (login.heals.append(home.name), (1, "", "Failed to authenticate. API Error: 401"))[1]
    row = sensor(Wire(healthy()), login).read(tmp_path / "a", allow_heal=True)
    assert row["status"] == "unavailable" and row["heal"]["transient"] is True


def test_cloud_credit_expiry_comes_from_the_claim_first(tmp_path):
    """C-9.10: as the claude.ai frontend does, the cloud credit's expiry is the claim status's
    `expires_at`, and the usage block's `resets_at` only when there is none."""
    answers = healthy()
    answers[cc.CARDS_USAGE_URL] = [(200, _usage_with(credit_resets="2026-12-31T00:00:00Z"), None)]
    row = sensor(Wire(answers), Login()).read(tmp_path / "a", allow_heal=False)
    [credit] = row["credits"]
    assert credit["expires_at"] == "2026-11-05T07:59:00Z" and credit["expires_from"] == "claim"


def test_ended_card_is_shown_as_ended_not_usable(tmp_path):
    """C-9.10: a card past its end with a reset left reads 'ended unused', never 'usable now'."""
    from subfleet import render
    login = Login(expires_in_s=90 * 86400)
    good = _pass(login, Wire(healthy()), NOW, None, tmp_path)
    shown = cc.view(good, NOW + timedelta(days=18), warn_days=5)
    text = "\n".join(render.card_lines(shown))
    assert "reset card ended unused (opus55-launch-promax-20260921" in text and "usable now" not in text
    compact = "\n".join(render.card_lines(shown, compact=True))
    assert "card used" not in compact


# --- the #132 review's follow-ups --------------------------------------------------


def _blind(cedar):
    """Healthy answers whose usage read carries `cedar` as its cards block, or none at all."""
    answers, payload = healthy(), _usage_with()
    payload.pop("cedar_ember")
    if cedar is not None:
        payload["cedar_ember"] = cedar
    answers[cc.CARDS_USAGE_URL] = [(200, payload, None)]
    return answers


def test_a_read_that_lists_no_cards_keeps_the_last_listed_grants(tmp_path):
    """C-9.10: an `ok` read whose cards block is missing or ineligible keeps the cards the last
    listing read saw, records that read beside them, and goes on warning on what they hold."""
    from subfleet import render, status_json
    login = Login(expires_in_s=90 * 86400)
    good = _pass(login, Wire(healthy()), NOW, None, tmp_path)
    listed = good["accounts"][0]
    when = NOW + timedelta(days=13)          # the card ends 2026-10-22T16:00Z, inside five days
    for cedar, reason, missing in ((None, None, True),
                                   ({"eligible": False, "ineligible_reason": "surface", "grants": []}, "surface", False)):
        later = _pass(login, Wire(_blind(cedar)), when, good, tmp_path)
        row = later["accounts"][0]
        assert row["status"] == "ok" and row["cards"] == listed["cards"]
        assert row["cards_unlisted"] == {"at": cc.iso_utc(when), "ineligible_reason": reason,
                                         "missing": missing, "listed_at": listed["read_at"]}
        shown = cc.view(later, when, warn_days=5)
        assert shown["accounts"][0]["unused_cards"] == 1 and kinds(shown["warnings"]) == ["card-expiring"]
        text = "\n".join(render.card_lines(shown))
        assert "1 unused reset card (opus55-launch-promax-20260921" in text
        assert f"cards as listed {listed['read_at']}; the read at {cc.iso_utc(when)} listed none" in text
        assert "nothing held" not in "\n".join(render.card_lines(shown, compact=True))
        [menu] = status_json._cards({"claude_cards": shown})["accounts"]
        assert menu["cards_unlisted"] == row["cards_unlisted"] and menu["cards"][0]["id"] == CARD
        # Another read that lists nothing still knows when the cards were last listed ...
        again = _pass(login, Wire(_blind(cedar)), when + timedelta(hours=6), later, tmp_path)
        assert again["accounts"][0]["cards_unlisted"]["listed_at"] == listed["read_at"]
        # ... and a read that lists cards again ends the record.
        back = _pass(login, Wire(healthy()), when + timedelta(hours=12), again, tmp_path)
        assert back["accounts"][0]["cards_unlisted"] is None and back["accounts"][0]["cards"] == listed["cards"]


def test_an_ineligible_first_read_is_shown_as_ineligible(tmp_path):
    """C-9.10: with no listing read before it, an ineligible block is what is shown, with its reason."""
    from subfleet import render
    later = _pass(Login(expires_in_s=90 * 86400), Wire(_blind({"eligible": False, "ineligible_reason": "surface",
                                                               "grants": []})), NOW, None, tmp_path)
    row = later["accounts"][0]
    assert row["cards"]["eligible"] is False and row["cards_unlisted"] is None
    assert "no reset card (ineligible: surface)" in "\n".join(render.card_lines(cc.view(later, NOW, warn_days=5)))


def test_another_account_never_inherits_the_old_claims_credit_expiry(tmp_path):
    """C-9.10: a folder signed into another account, whose claim-status read fails, dates its cloud
    credit from its own usage block, never from the old account's claim; and keeps none of the old
    account's cards when its own read lists none."""
    login = Login(expires_in_s=90 * 86400)
    good = _pass(login, Wire(healthy()), NOW, None, tmp_path)
    assert good["accounts"][0]["credits"][0]["expires_at"] == "2026-11-05T07:59:00Z"
    other = json.loads(json.dumps(fixture("profile_active")))
    other["account"]["uuid"], other["organization"]["uuid"] = ("11111111-1111-4111-8111-111111111111",
                                                               "22222222-2222-4222-8222-222222222222")
    answers = {cc.PROFILE_URL: [(200, other, None)],
               cc.CARDS_USAGE_URL: [(200, _usage_with(credit_resets="2026-12-31T00:00:00Z",
                                                       cedar={"eligible": False, "ineligible_reason": "surface"}), None)],
               cc.CLOUD_CREDIT_STATUS_URL: [(urllib.error.HTTPError(cc.CLOUD_CREDIT_STATUS_URL, 503, "x", {}, None),
                                             None, None)]}
    for row in (_pass(login, Wire(answers), NOW + timedelta(hours=6), good, tmp_path)["accounts"][0],
                sensor(Wire(answers), login, now=lambda: NOW + timedelta(hours=6)).read(
                    tmp_path / "a", allow_heal=False, previous=good["accounts"][0])):
        assert row["status"] == "ok" and row["identity"].startswith("11111111")
        [credit] = row["credits"]
        assert credit["expires_at"] == "2026-12-31T00:00:00Z" and "expires_from" not in credit
        assert row["cloud_credit_claim"] is None
        assert row["cards"]["eligible"] is False and row["cards"]["grants"] == [] and not row.get("cards_unlisted")


def test_another_account_whose_usage_read_fails_shows_nothing_of_the_old_one(tmp_path):
    """C-9.10: the folder's new account, its profile read and its usage read failed, shows none of
    the old account's cards, credits or claim status, and no time they were read."""
    login = Login(expires_in_s=90 * 86400)
    good = _pass(login, Wire(healthy()), NOW, None, tmp_path)
    other = json.loads(json.dumps(fixture("profile_active")))
    other["account"]["uuid"] = "11111111-1111-4111-8111-111111111111"
    row = sensor(Wire({cc.PROFILE_URL: [(200, other, None)], cc.CARDS_USAGE_URL: [(OSError("down"), None, None)]}),
                 login).read(tmp_path / "a", allow_heal=False, previous=good["accounts"][0])
    assert row["status"] == "unavailable" and row["identity"].startswith("11111111")
    assert (row["cards"], row["credits"], row["cloud_credit_claim"], row["read_at"]) == (None, [], None, None)


def test_heal_turn_output_that_is_not_utf8_is_replaced_not_raised(tmp_path):
    """C-9.10: a byte the CLI writes that is not UTF-8 is replaced: the turn's rc and output come
    back, and what the turn left running is killed."""
    import os
    import time as clock
    marker = tmp_path / "child.pid"
    fake = tmp_path / "claude"
    fake.write_text(f"#!/bin/sh\nsleep 60 >/dev/null 2>&1 &\necho $! > {marker}\n"
                    "printf 'ok \\377\\n'\nprintf 'note \\376\\n' >&2\n")
    fake.chmod(0o755)
    rc, out, err = cc.heal_turn(tmp_path / "home", claude_bin=str(fake), model="m", prompt="p", timeout=30)
    assert (rc, out, err) == (0, "ok �\n", "note �\n")
    child = int(marker.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        clock.sleep(.1)
    else:
        pytest.fail("the heal's background child outlived the turn")


def test_heal_turn_kills_its_group_however_the_turn_ends(tmp_path, monkeypatch):
    """C-9.10: an error while the turn's output is read still kills the turn's process group."""
    import signal
    kills = []
    monkeypatch.setattr(cc, "_kill_group", lambda pid, sig=signal.SIGKILL: kills.append((pid, sig)))

    class Child:
        pid, returncode = 4243, None

        def __init__(self, argv, **kwargs):
            pass

        def communicate(self, timeout=None):
            raise RuntimeError("the pipe broke")
    with pytest.raises(RuntimeError):
        cc.heal_turn(tmp_path / "home", model="m", prompt="p", popen=Child)
    assert kills == [(4243, signal.SIGKILL)]


@pytest.mark.parametrize(("org", "status", "gone"), [
    ("claude_free", "canceled", True),       # what the five lapsed accounts read (2026-10-05)
    ("claude_free", "active", True),
    ("claude_max", "canceled", True),        # C-9.10: `canceled` alone is `lapsed`
    ("claude_max", "active", False),
    ("claude_max", "past_due", False),       # lapsing, not lapsed: its cards are read and warned on
    ("claude_max", None, False),
    (None, None, False)])
def test_lapsed_is_the_contracts_rule(org, status, gone):
    """C-9.10: "`claude_free` or `canceled` is `lapsed`": either one, each on its own."""
    assert cc.lapsed({"organization_type": org, "subscription_status": status}) is gone
