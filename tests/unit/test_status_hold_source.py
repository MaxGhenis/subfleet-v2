"""Status and alerts name the source of every lane closure (D-STATUS-SRC1; C-9.6, C-17.1, C-18.4).

Incident (2026-10-24): the Claude lanes started no jobs for nearly 8 hours and
the Codex lanes were held from 21:51Z. Every hold was a hub controller's
`operator-hold` (load, running caps, disk), not a provider limit, yet
`subfleet status` and the notices said the accounts were out: `lane_verdict`
called any open account-scope closure `limited`, every closure but `auth-dead`
raised "<home> account limited", and the fleet-empty alert said "All enrolled
lanes are exhausted, unavailable, or unknown" whatever the cause.

Invariants, for every input (Hypothesis), beside the examples:

- I1: `dispatchable(lane)` equals the function release/217 (1c3bfc4f2) shipped,
  frozen below, for every lane: closures (reasons, scopes, ends, sources),
  enabled, owner, canonical, duplicate, desktop, probe state, an explicit
  `dispatchable`, the cycle's verdict and the readings.
- I2: the verdict is `limited` iff an open account-scope closure that is not a
  hold (`operator-hold`, `auth-dead`) exists, the lane is enabled and it has no
  credential fault (an `auth-dead` closure, or a cycle verdict naming a
  credential fault). It is `held` iff an open `operator-hold` exists, no such
  limit does, and the lane is enabled with no credential fault.
- I3: the fleet-empty alert's per-cause counts sum to the provider's
  non-dispatchable enrolled lanes, and outside the operators' own notes its
  body says "limited" or "exhausted" only when a lane is provider-limited.
- I4: a valid `lanes hold --note` round-trips exactly into the closure row, the
  `lane.held` event, `status --json` (daemon and offline), the status text and
  the fleet-empty body; an invalid one is refused (exit 2) and the store is
  unchanged: no closure, no event.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import alerts, cli, contracts, protocol, status_json
from subfleet import daemon as daemon_module
from subfleet.contracts import Credential, Exit, Lane, LaneOwner
from subfleet.daemon import Daemon
from subfleet.offline import Offline

NOW = datetime(2026, 10, 24, 22, tzinfo=timezone.utc)
NOTE = "codex-open: disk brake"
FIXTURE_HEALTH = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow]
#: C-23.52: the cycle verdicts that name a credential fault, as alerts.py has named them.
CREDENTIAL_FAULTS = {"auth-dead", "auth-revoked", "revoked", "no-auth", "auth-suspect", "token-invalid",
                     "secret-missing", "no-token", "expired-token", "free-plan"}
HOLD_REASONS = {"operator-hold", "auth-dead"}


def iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def lane(identity: str, provider: str = "codex", **extra) -> dict:
    return {"lane_id": identity, "provider": provider, "account_key": f"{provider}:{identity}@example.invalid",
            "email": f"{identity}@example.invalid", "home": f"/homes/{identity}",
            "credential_ref": f"/homes/{identity}", "enabled": True, "owner": "v2",
            "readings": [], "closures": [], **extra}


def hold(identity: str, until: str, note: str | None = NOTE, scope: str = "account") -> dict:
    """An operator hold as the daemon writes it: `operator`, or `operator: <note>`."""
    return {"lane_id": identity, "scope": scope, "until_at": until, "reason": "operator-hold",
            "clock_source": "reported", "source_event": "operator" if note is None else f"operator: {note}"}


def limit(identity: str, until: str, scope: str = "account", reason: str = "provider-limit",
          clock: str = "reported") -> dict:
    return {"lane_id": identity, "scope": scope, "until_at": until, "reason": reason,
            "clock_source": clock, "source_event": "wham"}


def held_lane(n: int, until: str, note: str | None = NOTE, provider: str = "codex", **extra) -> dict:
    identity = f"{provider}-{n}"
    return lane(identity, provider, closures=[hold(identity, until, note)], **extra)


def limited_lane(n: int, until: str, provider: str = "codex", **extra) -> dict:
    identity = f"{provider}-{n}"
    return lane(identity, provider, closures=[limit(identity, until)], **extra)


def conditions(*lanes: dict) -> dict[str, dict]:
    return {row["key"]: row for row in alerts.evaluate_conditions({"lanes": list(lanes)}, now=NOW)}


def status_text(lanes: list[dict]) -> str:
    """`subfleet status` over a daemon's answer for `lanes`: its lanes and their closures."""
    return cli.format_status({"now": iso(NOW), "lanes": lanes, "readings": [], "running": [],
                              "closures": [row for item in lanes for row in item["closures"]]})


def lane_row(text: str, identity: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(identity + " "))


def closure_lines(text: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index("closures") + 1
    return [line for line in lines[start:] if line.startswith("  ")]


# --- the frozen release/217 functions (1c3bfc4f2), for I1 --------------------

def legacy_desktop_excluded(lane: dict) -> bool:
    return bool(lane.get("desktop")) and lane.get("desktop_in_use", True) is not False


def legacy_lane_verdict(lane: dict) -> str:
    explicit = lane.get("verdict") or lane.get("outcome")
    if isinstance(explicit, str):
        return explicit
    if any(row.get("reason") == "auth-dead" for row in lane.get("closures", ())):
        return "auth-dead"
    if not lane.get("enabled", True):
        return "disabled"
    if any(row.get("scope") == "account" for row in lane.get("closures", ())):
        return "limited"
    readings = lane.get("readings", ())
    if any(row.get("label") == "provider" for row in readings):
        return "ok"
    for label in ("stale-provider", "admission-observed", "local-backoff"):
        if any(row.get("label") == label for row in readings):
            return label
    return "unknown"


def legacy_dispatchable(lane: dict) -> bool:
    if (not lane.get("enabled", True) or lane.get("owner", "v2") != "v2"
            or lane.get("canonical") is False or lane.get("duplicate_of")
            or lane.get("provider") == "claude" and legacy_desktop_excluded(lane)):
        return False
    if lane.get("probe_state") is not None:
        return False
    if "dispatchable" in lane:
        return bool(lane["dispatchable"])
    return (legacy_lane_verdict(lane) in {"ok", "ready", "provider", "stale-provider", "admission-observed"}
            and not any(row.get("scope") == "account" for row in lane.get("closures", ())))


# --- strategies ---------------------------------------------------------------

REASONS = ("provider-limit", "credits", "cooldown", "operator-hold", "auth-dead")
SCOPES = ("account", "gpt-6-astra", "claude-fable-5-1")
UNTILS = ("2026-10-25T03:09:22Z", "2026-10-25T03:51:22Z", "2026-10-27T04:00:00Z", "2026-10-24T23:00:00Z")
VERDICTS = ("ok", "ready", "provider", "stale-provider", "admission-observed", "local-backoff", "unknown",
            "limited", "held", "disabled", "duplicate", "identity-mismatch", "network-error", "", *sorted(CREDENTIAL_FAULTS))
LABELS = ("provider", "stale-provider", "admission-observed", "local-backoff", "unknown")
#: Notes an operator might write, including the words I3 watches for.
NOTE_WORDS = st.sampled_from([None, NOTE, "load 140", "running cap 6", "rate limited by hub", "exhausted? no",
                              "held; earliest end soon", "ünïcödé ✓ 🚦"])


@st.composite
def closures(draw, identity: str = "lane-1") -> dict:
    reason = draw(st.sampled_from(REASONS))
    note = draw(NOTE_WORDS) if reason == "operator-hold" else None
    return {"lane_id": identity, "scope": draw(st.sampled_from(SCOPES)), "until_at": draw(st.sampled_from(UNTILS)),
            "reason": reason, "clock_source": draw(st.sampled_from(("reported", "guessed"))),
            "source_event": draw(st.sampled_from(("operator", "wham", None))) if note is None else f"operator: {note}"}


@st.composite
def lanes_any(draw, identity: str = "lane-1", provider: str | None = None) -> dict:
    """A lane as a snapshot can carry it, every field `dispatchable` and `lane_verdict` read drawn."""
    row = {"lane_id": identity, "provider": provider or draw(st.sampled_from(("codex", "claude"))),
           "home": f"/homes/{identity}", "account_key": f"x:{identity}",
           "closures": draw(st.lists(closures(identity), max_size=4)),
           "readings": [{"label": label, "scope": "account", "window": "seven_day"}
                        for label in draw(st.lists(st.sampled_from(LABELS), max_size=3))]}
    optional = {"enabled": st.sampled_from((True, False, None, 0, 1)), "owner": st.sampled_from(("v2", "v1")),
                "canonical": st.sampled_from((True, False, None)),
                "duplicate_of": st.sampled_from((None, "", "/homes/first")),
                "desktop": st.booleans(), "desktop_in_use": st.sampled_from((True, False, None)),
                "probe_state": st.sampled_from((None, "reserved", "running", "uncertain")),
                "dispatchable": st.booleans(),
                "verdict": st.one_of(st.sampled_from(VERDICTS), st.none(), st.integers(0, 2)),
                "outcome": st.one_of(st.sampled_from(VERDICTS), st.none())}
    for key, strategy in optional.items():
        if draw(st.booleans()):
            row[key] = draw(strategy)
    return row


def explicit_of(lane: dict) -> str | None:
    value = lane.get("verdict") or lane.get("outcome")
    return value if isinstance(value, str) else None


def credential_fault(lane: dict) -> bool:
    return (explicit_of(lane) in CREDENTIAL_FAULTS
            or any(row.get("reason") == "auth-dead" for row in lane.get("closures", ())))


def account_limit(lane: dict) -> bool:
    return any(row.get("scope") == "account" and row.get("reason") not in HOLD_REASONS
               for row in lane.get("closures", ()))


def operator_hold(lane: dict) -> bool:
    return any(row.get("reason") == "operator-hold" for row in lane.get("closures", ()))


# --- I1: dispatchable is unchanged --------------------------------------------

@settings(max_examples=3000, deadline=None, database=None, derandomize=True)
@given(row=lanes_any())
@example(row={"lane_id": "lane-1", "provider": "codex", "closures": [hold("lane-1", UNTILS[0])], "readings": []})
@example(row={"lane_id": "lane-1", "provider": "codex", "verdict": "ok",
              "closures": [hold("lane-1", UNTILS[0])], "readings": [{"label": "provider"}]})
@example(row={"lane_id": "lane-1", "provider": "codex", "verdict": "limited", "closures": [], "readings": []})
@example(row={"lane_id": "lane-1", "provider": "codex", "verdict": "ok",
              "closures": [{**hold("lane-1", UNTILS[0]), "reason": "auth-dead", "scope": "gpt-6-astra"}],
              "readings": [{"label": "provider"}]})
def test_i1_dispatchable_equals_the_frozen_release_217_function(row):
    """I1 (D-STATUS-SRC1): a held lane stays non-dispatchable, and no lane's answer moves."""
    assert status_json.dispatchable(row) is legacy_dispatchable(row)


def test_i1_a_held_lane_is_not_dispatchable_whatever_its_cycle_said():
    for explicit in (None, "ok", "admission-observed", "limited"):
        row = held_lane(1, UNTILS[1], verdict=explicit, readings=[{"label": "provider"}])
        assert status_json.lane_verdict(row) == "held"
        assert status_json.dispatchable(row) is False


# --- I2: limited and held ------------------------------------------------------

@settings(max_examples=3000, deadline=None, database=None, derandomize=True)
@given(row=lanes_any())
def test_i2_limited_iff_an_account_limit_and_held_iff_only_a_hold(row):
    """I2: `limited` and `held` come from closures alone, under the credential and disabled verdicts."""
    verdict = status_json.lane_verdict(row)
    usable = bool(row.get("enabled", True)) and not credential_fault(row)
    assert (verdict == "limited") is (account_limit(row) and usable)
    assert (verdict == "held") is (operator_hold(row) and not account_limit(row) and usable)
    if explicit_of(row) in CREDENTIAL_FAULTS:
        assert verdict == explicit_of(row)                  # the alert's login command and the menu's sign-in
    elif any(item.get("reason") == "auth-dead" for item in row["closures"]) and explicit_of(row) in (None, "limited", "held"):
        assert verdict == "auth-dead"
    elif not row.get("enabled", True) and explicit_of(row) in (None, "limited", "held"):
        assert verdict == "disabled"


def test_i2_precedence_auth_dead_disabled_limited_held():
    until = UNTILS[1]
    both = lane("codex-1", closures=[hold("codex-1", until), limit("codex-1", UNTILS[2])])
    assert status_json.lane_verdict(both) == "limited"
    assert status_json.lane_verdict({**both, "enabled": False}) == "disabled"
    dead = [*both["closures"], {**limit("codex-1", until), "reason": "auth-dead"}]
    assert status_json.lane_verdict({**both, "closures": dead}) == "auth-dead"
    assert status_json.lane_verdict({**both, "verdict": "auth-revoked"}) == "auth-revoked"
    assert status_json.lane_verdict(lane("codex-1", closures=[hold("codex-1", until)])) == "held"
    # A model-scoped limit is no account limit: the hold names the lane.
    scoped = lane("codex-1", closures=[hold("codex-1", until), limit("codex-1", until, scope="gpt-6-astra")])
    assert status_json.lane_verdict(scoped) == "held"
    # A cycle's `limited` stands only while a limit closure does.
    assert status_json.lane_verdict(lane("codex-1", verdict="limited", readings=[{"label": "provider"}])) == "ok"
    assert status_json.lane_verdict(lane("codex-1", verdict="limited")) == "unknown"


# --- examples: today's case and its neighbours ---------------------------------

def six_held(explicit: str | None = None) -> list[dict]:
    ends = ["2026-10-25T03:51:22Z", "2026-10-25T03:09:22Z", "2026-10-25T04:30:00Z",
            "2026-10-25T03:51:22Z", "2026-10-25T05:00:00Z", "2026-10-25T03:20:00Z"]
    extra = {} if explicit is None else {"verdict": explicit, "dispatchable": False}
    return [held_lane(n, until, **extra) for n, until in enumerate(ends, start=1)]


@pytest.mark.parametrize("explicit", [None, "ok", "limited"])
def test_all_six_codex_lanes_held_by_an_operator_read_held_not_limited(explicit):
    """2026-10-24: six Codex lanes under hub holds, with or without a stale cycle verdict."""
    lanes = six_held(explicit)
    payload = status_json.build_status({"lanes": lanes}, now=NOW)
    assert [row["verdict"] for row in payload["codex"]["homes"]] == ["held"] * 6
    assert payload["codex"]["fleet"]["dispatchable_now"] == 0
    found = conditions(*lanes)
    assert not [key for key in found if key.startswith("codex-scoped-limit:")]       # no per-lane "limited"
    empty = found["codex-fleet-empty"]
    assert empty["subject"] == "codex: no dispatchable lanes"
    assert empty["body"] == ("No codex lane is dispatchable: 6 held by operator "
                             f"({NOTE}; earliest end 2026-10-25T03:09:22Z). Run: subfleet status")
    assert empty["severity"] == "warn"
    assert empty["causes"] == {"held": 6, "limited": 0, "credential": 0, "disabled": 0, "other": 0}
    assert "limited" not in empty["body"] and "exhausted" not in empty["body"]


def test_all_six_held_status_text_says_held_by_operator_with_the_note():
    text = status_text(six_held())
    assert closure_lines(text)[0] == f"  codex-1 held by operator until 2026-10-25T03:51:22Z: {NOTE}"
    assert all(" held by operator until " in line for line in closure_lines(text))
    assert lane_row(text, "codex-1").endswith("[held]")
    assert "limited" not in text


def test_a_mix_of_holds_and_provider_limits_names_each_with_counts():
    lanes = [*(held_lane(n, f"2026-10-25T0{n}:00:00Z") for n in range(1, 5)),
             limited_lane(5, "2026-10-27T04:00:00Z"), limited_lane(6, "2026-10-26T04:00:00Z")]
    found = conditions(*lanes)
    empty = found["codex-fleet-empty"]
    assert empty["body"] == ("No codex lane is dispatchable: 4 held by operator "
                             f"({NOTE}; earliest end 2026-10-25T01:00:00Z), "
                             "2 provider-limited (earliest reset 2026-10-26T04:00:00Z). Run: subfleet status")
    assert empty["severity"] == "critical"
    assert empty["causes"] == {"held": 4, "limited": 2, "credential": 0, "disabled": 0, "other": 0}
    # Provider limits keep today's alert, key and wording; holds raise none.
    limits = sorted(key for key in found if key.startswith("codex-scoped-limit:"))
    assert limits == ["codex-scoped-limit:codex-5@example.invalid:account",
                      "codex-scoped-limit:codex-6@example.invalid:account"]
    five = found["codex-scoped-limit:codex-5@example.invalid:account"]
    assert (five["severity"], five["subject"], five["body"]) == (
        "warn", "codex: /homes/codex-5 account limited",
        "/homes/codex-5: account is closed (provider-limit); resets 2026-10-27T04:00:00Z.")
    verdicts = [row["verdict"] for row in status_json.build_status({"lanes": lanes}, now=NOW)["codex"]["homes"]]
    assert verdicts == ["held"] * 4 + ["limited"] * 2


def test_all_lanes_provider_limited_keep_todays_alerts():
    lanes = [limited_lane(n, f"2026-10-2{n}T04:00:00Z") for n in range(5, 9)]
    found = conditions(*lanes)
    empty = found["codex-fleet-empty"]
    assert empty["severity"] == "critical"
    assert empty["body"] == ("No codex lane is dispatchable: 4 provider-limited "
                             "(earliest reset 2026-10-25T04:00:00Z). Run: subfleet status")
    assert len([key for key in found if key.startswith("codex-scoped-limit:")]) == 4


def test_a_hold_and_a_limit_on_one_lane_reads_limited_and_shows_both():
    row = lane("claude-10", "claude", closures=[hold("claude-10", "2026-10-25T03:51:22Z"),
                                                limit("claude-10", "2026-10-27T04:00:00Z")])
    assert status_json.lane_verdict(row) == "limited"
    text = status_text([row])
    assert closure_lines(text) == [f"  claude-10 held by operator until 2026-10-25T03:51:22Z: {NOTE}",
                                   "  claude-10 provider limit on account until 2026-10-27T04:00:00Z (reported)"]
    assert lane_row(text, "claude-10").endswith("[limited]")
    shown = [status_json.closure_row(item) for item in row["closures"]]
    assert [(item["kind"], item["note"]) for item in shown] == [("hold", NOTE), ("limit", None)]
    assert all(item[key] == source[key] for item, source in zip(shown, row["closures"]) for key in source)
    found = conditions(row)
    assert list(found) == ["claude-limit:2026-10-27T04:00:00Z", "claude-lanes-empty"]
    assert found["claude-lanes-empty"]["body"] == ("No claude lane is dispatchable: 1 provider-limited "
                                                   "(earliest reset 2026-10-27T04:00:00Z). Run: subfleet status")


def test_auth_dead_outranks_a_hold_and_keeps_its_credential_alert():
    dead = held_lane(1, UNTILS[1], verdict="auth-dead", enabled=False)
    closed = lane("codex-2", closures=[hold("codex-2", UNTILS[1]), {**limit("codex-2", UNTILS[2]), "reason": "auth-dead"}])
    assert status_json.lane_verdict(dead) == "auth-dead"
    assert status_json.lane_verdict(closed) == "auth-dead"
    homes = status_json.build_status({"lanes": [dead]}, now=NOW)["codex"]["homes"]
    assert (homes[0]["verdict"], homes[0]["outcome"]) == ("auth-revoked", "auth-dead")
    found = conditions(dead, closed)
    assert "codex-revoked:/homes/codex-1" in found and "codex-revoked:/homes/codex-2" in found
    assert not [key for key in found if "scoped-limit" in key]
    assert found["codex-fleet-empty"]["causes"] == {"held": 0, "limited": 0, "credential": 2, "disabled": 0,
                                                    "other": 0}
    assert found["codex-fleet-empty"]["body"] == ("No codex lane is dispatchable: 2 credential-blocked. "
                                                  "Run: subfleet status")
    assert found["codex-fleet-empty"]["severity"] == "critical"


def test_a_disabled_lane_reads_disabled_and_the_fleet_is_not_all_held():
    lanes = [held_lane(1, UNTILS[1]), held_lane(2, UNTILS[0], enabled=False)]
    assert status_json.lane_verdict(lanes[1]) == "disabled"
    empty = conditions(*lanes)["codex-fleet-empty"]
    assert empty["causes"] == {"held": 1, "limited": 0, "credential": 0, "disabled": 1, "other": 0}
    assert empty["body"] == ("No codex lane is dispatchable: 1 held by operator "
                             f"({NOTE}; earliest end {UNTILS[1]}), 1 disabled. Run: subfleet status")
    assert empty["severity"] == "critical"


def test_claude_lanes_all_held_raise_no_per_lane_limit_and_stay_warn():
    lanes = [held_lane(n, UNTILS[1], provider="claude", note="claude-open: running cap") for n in (1, 2, 3)]
    found = conditions(*lanes)
    assert not [key for key in found if key.startswith("claude-limit:")]
    empty = found["claude-lanes-empty"]
    assert (empty["severity"], empty["subject"]) == ("warn", "claude: no dispatchable lanes")
    assert empty["body"] == ("No claude lane is dispatchable: 3 held by operator "
                             f"(claude-open: running cap; earliest end {UNTILS[1]}). Run: subfleet status")
    accounts = status_json.build_status({"lanes": lanes}, now=NOW)["claude"]["accounts"]
    assert {row["verdict"] for row in accounts} == {"held"}


def test_holds_without_notes_and_with_several_notes():
    lanes = [held_lane(1, UNTILS[1], note=None), held_lane(2, UNTILS[0], note="load 140"),
             held_lane(3, UNTILS[1], note=NOTE), held_lane(4, UNTILS[1], note="load 140")]
    empty = conditions(*lanes)["codex-fleet-empty"]
    assert empty["body"] == ("No codex lane is dispatchable: 4 held by operator "
                             f"(load 140; {NOTE}; earliest end {UNTILS[0]}). Run: subfleet status")
    bare = conditions(held_lane(1, UNTILS[1], note=None))["codex-fleet-empty"]
    assert bare["body"] == f"No codex lane is dispatchable: 1 held by operator (earliest end {UNTILS[1]}). Run: subfleet status"


def test_a_lane_held_twice_is_held_until_its_last_hold_ends():
    row = lane("codex-1", closures=[hold("codex-1", "2026-10-25T01:00:00Z"),
                                    hold("codex-1", "2026-10-25T06:00:00Z", note="load 140", scope="gpt-6-astra")])
    later = held_lane(2, "2026-10-25T05:00:00Z")
    body = conditions(row, later)["codex-fleet-empty"]["body"]
    assert f"({NOTE}; load 140; earliest end 2026-10-25T05:00:00Z)" in body


def test_a_hold_without_a_note_shows_held_by_operator_and_no_note():
    row = held_lane(1, UNTILS[1], note=None)
    assert closure_lines(status_text([row])) == [f"  codex-1 held by operator until {UNTILS[1]}"]
    shown = status_json.closure_row(row["closures"][0])
    assert (shown["kind"], shown["note"]) == ("hold", None)


def test_other_limits_and_auth_closures_name_what_they_are():
    rows = [limit("codex-1", UNTILS[2], reason="credits", clock="guessed"),
            limit("codex-1", UNTILS[2], reason="cooldown", scope="gpt-6-astra"),
            {**limit("codex-1", UNTILS[2]), "reason": "auth-dead"}]
    text = status_text([lane("codex-1", closures=rows)])
    assert closure_lines(text) == [f"  codex-1 credits limit on account until {UNTILS[2]} (guessed)",
                                   f"  codex-1 cooldown on gpt-6-astra until {UNTILS[2]} (reported)",
                                   f"  codex-1 auth-dead on account until {UNTILS[2]} (reported)"]
    assert [status_json.closure_row(row)["kind"] for row in rows] == ["limit", "limit", "auth"]


def test_offline_status_text_flags_only_closures_still_in_force():
    """With the daemon down, the store's unreleased rows include ended ones: they flag no lane."""
    row = lane("codex-1", closures=[])
    data = {"now": iso(NOW), "lanes": [row], "readings": [], "running": [], "offline": True,
            "closures": [hold("codex-1", "2026-10-24T21:00:00Z")]}
    text = cli.format_status(data)
    assert "[held]" not in lane_row(text, "codex-1")
    data["closures"] = [hold("codex-1", "2026-10-25T21:00:00Z")]
    assert lane_row(cli.format_status(data), "codex-1").endswith("[held]")


# --- I3: the fleet-empty body ---------------------------------------------------

@st.composite
def fleets(draw) -> list[dict]:
    provider = draw(st.sampled_from(("codex", "claude")))
    count = draw(st.integers(1, 7))
    rows = []
    for n in range(1, count + 1):
        row = draw(lanes_any(f"{provider}-{n}", provider))
        row.update(owner="v2", dispatchable=False)          # a fleet with nothing dispatchable
        row.pop("superseded_by", None)
        rows.append(row)
    return rows


def notes_of(rows: list[dict]) -> list[str]:
    """Every note the fleet's holds carry, as the operators wrote them."""
    return [source[len("operator: "):] for row in rows for item in row["closures"]
            if item["reason"] == "operator-hold" and isinstance(source := item.get("source_event"), str)
            and source.startswith("operator: ") and len(source) > len("operator: ")]


@settings(max_examples=1500, deadline=None, database=None, derandomize=True)
@given(rows=fleets())
def test_i3_fleet_empty_counts_sum_and_limited_words_need_a_limited_lane(rows):
    provider = rows[0]["provider"]
    key = "codex-fleet-empty" if provider == "codex" else "claude-lanes-empty"
    empty = conditions(*rows)[key]
    causes = empty["causes"]
    assert sum(causes.values()) == len(rows)                 # every non-dispatchable enrolled lane, once
    limited = sum(status_json.lane_verdict(row) == "limited" for row in rows)
    held = sum(status_json.lane_verdict(row) == "held" for row in rows)
    assert (causes["limited"], causes["held"]) == (limited, held)
    body = empty["body"]
    assert empty["subject"] == f"{provider}: no dispatchable lanes"
    words = body
    for note in sorted(set(notes_of(rows)), key=len, reverse=True):
        words = words.replace(note, "\x00")
    if not limited:
        assert "limited" not in words and "exhausted" not in words
    else:
        assert f"{limited} provider-limited (earliest reset " in body
    if held:
        assert f"{held} held by operator (" in body
    assert not re.search(r"\b0 ", words)                     # a cause with no lane is not named
    all_held = held == len(rows)
    assert empty["severity"] == ("warn" if all_held or provider == "claude" else "critical")


# --- I4: the note, end to end ---------------------------------------------------

@pytest.fixture
def core(tmp_path, monkeypatch):
    """A daemon core, not serving, with one Codex lane: its ops are called through `dispatch`."""
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fake-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fake-start")
    service = Daemon(tmp_path / "state")
    service.policy.setdefault("alerts", {}).pop("operator_session", None)
    home = tmp_path / "codex-1"
    home.mkdir()
    service.store.put_lane(Lane("codex-1", "codex", "codex:codex-1", Credential("codex", str(home), "home"),
                                str(home), LaneOwner.V2, False, True))
    yield service
    service.close()


def future(hours: float = 48) -> str:
    return iso(datetime.now(timezone.utc) + timedelta(hours=hours))


def store_counts(service) -> tuple[int, int]:
    return (service.store.one("SELECT count(*) n FROM closures")["n"],
            service.store.one("SELECT count(*) n FROM events")["n"])


def valid_note(text: str) -> bool:
    """The spec: after stripping, one line of 1 to 160 printable characters."""
    stripped = text.strip()
    return 0 < len(stripped) <= 160 and stripped.isprintable()


NOTES = st.one_of(
    st.text(max_size=170),
    st.text(st.characters(codec="utf-8", exclude_categories=("Cs",)), min_size=150, max_size=175),
    st.builds(lambda head, cut, tail: head + cut + tail, st.text(max_size=20),
              st.sampled_from(["\n", "\r", "\t", "\x00", "\x07", "\x1b", "\x7f", "\x85", " ", "​", "\xa0"]),
              st.text(max_size=20)),
    st.sampled_from([NOTE, " " + NOTE + " \n", "x" * 160, "x" * 161, "   ", ""]))


@settings(max_examples=60, deadline=None, database=None, derandomize=True, suppress_health_check=FIXTURE_HEALTH)
@given(note=NOTES)
@example(note=NOTE)
@example(note="disk\nbrake")
@example(note="disk\x07brake")
@example(note="x" * 161)
@example(note="x" * 160)
def test_i4_a_note_round_trips_or_is_refused_with_the_store_unchanged(core, note):
    until = future()
    before = store_counts(core)
    if not valid_note(note):
        with pytest.raises(protocol.ProtocolError) as refused:
            core.dispatch("lanes", {"action": "hold", "lane_id": "codex-1", "until": until, "note": note})
        assert refused.value.code == Exit.INVALID_INPUT and refused.value.reason == "hold-note"
        assert store_counts(core) == before                  # no closure, no event
        return
    expected = note.strip()
    held = core.dispatch("lanes", {"action": "hold", "lane_id": "codex-1", "until": until, "note": note})
    try:
        assert held["held"] == "codex-1" and held["note"] == expected
        row = core.store.one("SELECT * FROM closures WHERE lane_id='codex-1' AND reason='operator-hold' "
                             "AND released_at IS NULL")
        assert row["source_event"] == f"operator: {expected}" and row["until_at"] == until
        event = core.store.one("SELECT data_json FROM events WHERE kind='lane.held' ORDER BY event_id DESC LIMIT 1")
        assert json.loads(event["data_json"]) == {"until": until, "note": expected}
        status = core.dispatch("daemon.status", {})
        shown = [item for item in status["closures"] if item["reason"] == "operator-hold"]
        assert [(item["kind"], item["note"], item["source_event"]) for item in shown] == [
            ("hold", expected, f"operator: {expected}")]
        assert [item["note"] for item in status["lanes"][0]["closures"]] == [expected]
        offline = [item for item in Offline(core.root).status()["closures"] if item["reason"] == "operator-hold"]
        assert [(item["kind"], item["note"]) for item in offline] == [("hold", expected)]
        assert f"  codex-1 held by operator until {until}: {expected}" in cli.format_status(status).splitlines()
        body = conditions(*core.timers.snapshot()["lanes"])["codex-fleet-empty"]["body"]
        assert body.startswith(f"No codex lane is dispatchable: 1 held by operator ({expected}; earliest end ")
    finally:
        core.dispatch("lanes", {"action": "release", "lane_id": "codex-1"})


def test_i4_a_hold_without_a_note_records_operator_as_before(core):
    until = future()
    held = core.dispatch("lanes", {"action": "hold", "lane_id": "codex-1", "until": until})
    assert held["note"] is None
    row = core.store.one("SELECT * FROM closures WHERE reason='operator-hold'")
    assert row["source_event"] == "operator"
    event = core.store.one("SELECT data_json FROM events WHERE kind='lane.held'")
    assert json.loads(event["data_json"]) == {"until": until}
    status = core.dispatch("daemon.status", {})
    assert [(item["kind"], item["note"]) for item in status["closures"]] == [("hold", None)]
    assert f"  codex-1 held by operator until {until}" in cli.format_status(status).splitlines()


def test_i4_a_later_hold_takes_the_row_and_its_note_a_shorter_one_leaves_both(core):
    """C-9.6: holds share one row per lane and scope; the hold that sets its end gives its note."""
    first, later, shorter = future(10), future(20), future(5)
    core.dispatch("lanes", {"action": "hold", "lane_id": "codex-1", "until": first, "note": "load 140"})
    core.dispatch("lanes", {"action": "hold", "lane_id": "codex-1", "until": later, "note": NOTE})
    core.dispatch("lanes", {"action": "hold", "lane_id": "codex-1", "until": shorter, "note": "running cap 6"})
    rows = core.store.query("SELECT until_at,source_event FROM closures WHERE reason='operator-hold'")
    assert [(row["until_at"], row["source_event"]) for row in rows] == [(later, f"operator: {NOTE}")]
    notes = [json.loads(row["data_json"]).get("note") for row in
             core.store.query("SELECT data_json FROM events WHERE kind='lane.held' ORDER BY event_id")]
    assert notes == ["load 140", NOTE, "running cap 6"]


def test_nothing_reads_a_closure_source_for_equality_with_operator():
    """D-STATUS-SRC1: the note rides in `source_event`, so no reader may compare it with `operator`."""
    repository = Path(__file__).resolve().parents[2]
    pattern = re.compile(r"""source_event\W{0,3}\s*(==|!=)|['"]operator['"]|source_event\s*(=|IN)\s*\(?'operator""",
                         re.IGNORECASE)
    found = [f"{path.relative_to(repository)}:{number}"
             for path in sorted([*(repository / "subfleet").rglob("*.py"), *(repository / "tools").rglob("*.py")])
             for number, text in enumerate(path.read_text().splitlines(), start=1)
             if "source_event" in text and pattern.search(text)]
    assert found == []


# --- the CLI ----------------------------------------------------------------------

def held_answer(request: protocol.Request) -> dict:
    return {"held": request.args.get("lane_id"), "released": None, "note": request.args.get("note"),
            "lanes": [], "closures": []}


def test_lanes_hold_sends_the_stripped_note(daemon, capsys):
    server = daemon({"lanes": held_answer})
    assert cli.main(["lanes", "hold", "codex-1", "--until", "2026-10-25T03:51:22Z", "--note", f"  {NOTE} "]) == 0
    sent = server.args("lanes")
    assert (sent["action"], sent["lane_id"], sent["note"]) == ("hold", "codex-1", NOTE)
    assert capsys.readouterr().out.strip() == "hold: codex-1"


@pytest.mark.parametrize("note", ["disk\nbrake", "disk\x07brake", "x" * 161, "   ", "a\tb"])
def test_lanes_hold_refuses_an_invalid_note_before_asking_the_daemon(daemon, capsys, note):
    server = daemon({"lanes": held_answer})
    assert cli.main(["lanes", "hold", "codex-1", "--until", "2026-10-25T03:51:22Z", "--note", note]) == 2
    assert "--note must be one line of 1 to 160 printable characters" in capsys.readouterr().err
    assert server.ops() == []


def test_lanes_hold_with_a_note_refuses_an_older_daemon_that_dropped_it(daemon, capsys):
    daemon({"lanes": lambda request: {"held": "codex-1", "released": None, "lanes": [], "closures": []}})
    assert cli.main(["lanes", "hold", "codex-1", "--until", "2026-10-25T03:51:22Z", "--note", NOTE]) == 69
    assert "without its note" in capsys.readouterr().err


def test_lanes_hold_without_a_note_is_unchanged(daemon, capsys):
    server = daemon({"lanes": lambda request: {"held": "codex-1", "lanes": [], "closures": []}})
    assert cli.main(["lanes", "hold", "codex-1", "--until", "2026-10-25T03:51:22Z"]) == 0
    assert server.args("lanes").get("note") is None
    assert cli.main(["lanes", "release", "codex-1", "--note", "reason"]) == 2   # release keeps its rule


def test_lanes_hold_help_names_the_note(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["lanes", "hold", "--help"])
    output = " ".join(capsys.readouterr().out.split())
    assert "--note" in output and "160" in output


# --- the note's rule itself ---------------------------------------------------------

@settings(max_examples=2000, deadline=None, database=None, derandomize=True)
@given(text=NOTES)
def test_hold_note_accepts_exactly_the_spec_and_round_trips_through_source_event(text):
    if valid_note(text):
        note = contracts.hold_note(text)
        assert note == text.strip()
        assert contracts.hold_source(note) == f"operator: {note}"
        assert contracts.hold_note_of(contracts.hold_source(note)) == note
    else:
        with pytest.raises(ValueError):
            contracts.hold_note(text)
    assert contracts.hold_note(None) is None and contracts.hold_source(None) == "operator"
    assert contracts.hold_note_of("operator") is None and contracts.hold_note_of("wham") is None
