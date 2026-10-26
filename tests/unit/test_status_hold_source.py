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
  `dispatchable`, the cycle's verdict and the readings; and, over malformed
  evidence, it answers or raises the same exception class (review r1 of #263).
- I2: the verdict is `limited` iff an open account-scope closure that is not a
  hold (`operator-hold`, `auth-dead`) exists, the lane is enabled and it has no
  credential fault (an `auth-dead` closure, or a cycle verdict naming a
  credential fault). It is `held` iff an open `operator-hold` exists, no such
  limit does, and the lane is enabled with no credential fault.
- I3: the fleet-empty alert's per-cause counts sum to the provider's
  non-dispatchable enrolled lanes, every count its body renders is the
  condition's, and outside the operators' own notes its body says "limited" or
  "exhausted" only when a lane is provider-limited.
- I4: a note `hold_note` accepts round-trips exactly into the closure row, the
  `lane.held` event, `status --json` (daemon and offline), the status text, the
  fleet-empty body and the session notice it becomes; a note it refuses (exit 2,
  the rule it broke) leaves the store unchanged: no closure, no event.
- I5: no text the C-23.14 scrubber would change reaches the store, status, the
  alerts or a notice through a hold, as written or as JSON carries it (`status
  --json`, `status.json`; review r2 of #263): a note with a dummy credential
  spliced in (a URL password among the shapes, a letter outside ASCII just
  before it among the prefixes) is refused with the store unchanged, or
  accepted only when the scrubber leaves every surface it reaches alone; an
  ordinary note is always accepted. Outside I5, pinned as it stands: a URL
  password just after a digit, which no scrubber mode masks (D-SCR-URL1).

A hold's stored `source_event` reads strictly (review r2 of #263): exactly
`operator`, or exactly `operator: <note>` with a note the rule accepts as it
stands; anything else, which only a writer outside `lanes hold` leaves, is
withheld by every reader of closure rows.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import json
import re
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import alerts, cli, contracts, ids, operations, protocol, status_json
from subfleet import daemon as daemon_module
from subfleet import refusals as refusals_module
from subfleet import render as render_module
from subfleet.contracts import Credential, Exit, Lane, LaneOwner
from subfleet.daemon import Daemon
from subfleet.ids import holds_credential
from subfleet.offline import Offline
from subfleet.sessions.handoff import scrub_secrets

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


#: Review r1 of #263, P3-1: evidence as a malformed snapshot can carry it. Readings
#: may be no list, a list holding None or another non-mapping, or mappings missing
#: their keys; a closure row may miss its scope or reason, or not be a mapping.
MALFORMED_READINGS = st.one_of(
    st.none(), st.just([None]), st.lists(st.sampled_from([None, 1, "provider", ("label", "provider")]),
                                         min_size=1, max_size=2),
    st.lists(st.sampled_from([{}, {"scope": "account"}, {"window": "seven_day"}, {"label": "provider"}]),
             min_size=1, max_size=3))


@st.composite
def closures_malformed(draw, identity: str = "lane-1"):
    row = draw(closures(identity))
    for key in ("scope", "reason"):
        if draw(st.booleans()):
            row.pop(key)
    return row


@st.composite
def lanes_malformed(draw, identity: str = "lane-1") -> dict:
    """`lanes_any`, with malformed evidence spliced in: the readings, the closures, or both."""
    row = draw(lanes_any(identity))
    if draw(st.booleans()):
        row["readings"] = draw(MALFORMED_READINGS)
    if draw(st.booleans()):
        extra = st.one_of(closures_malformed(identity), st.sampled_from([None, 1, "account"]))
        row["closures"] = draw(st.lists(st.one_of(closures(identity), extra), min_size=1, max_size=4))
    return row


def outcome(function, row: dict):
    """What `function(row)` gives: its answer, or the class of what it raised."""
    try:
        return function(row)
    except Exception as error:                              # noqa: BLE001 - compared, never hidden
        return type(error)


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


ACCOUNT_HOLD = {"scope": "account", "reason": "operator-hold", "until_at": "2099-01-01T00:00:00Z"}


@settings(max_examples=3000, deadline=None, database=None, derandomize=True)
@given(row=lanes_malformed())
@example(row={"lane_id": "codex-1", "provider": "codex", "readings": None, "closures": [ACCOUNT_HOLD]})
@example(row={"lane_id": "codex-1", "provider": "codex", "readings": [None], "closures": [ACCOUNT_HOLD]})
@example(row={"lane_id": "codex-1", "provider": "codex", "readings": None, "closures": [ACCOUNT_HOLD, None]})
@example(row={"lane_id": "codex-1", "provider": "codex", "readings": [{}], "closures": [{"until_at": "x"}]})
def test_i1_with_malformed_evidence_dispatchable_answers_or_raises_as_release_did(row):
    """I1 over malformed evidence (review r1 of #263, P3-1): the same answer, or the
    same exception class, as the frozen release/217 function."""
    assert outcome(status_json.dispatchable, row) == outcome(legacy_dispatchable, row)


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
    check_fleet_empty(rows)


#: C-18.4: what the fleet-empty body calls each cause, in the order it names them.
CAUSE_WORDS = {"held": "held by operator", "limited": "provider-limited", "credential": "credential-blocked",
               "disabled": "disabled", "other": "otherwise unavailable"}
RENDERED_CAUSE = re.compile(r"([0-9]+) (" + "|".join(map(re.escape, CAUSE_WORDS.values())) + r")(?: \(([^()]*)\))?\Z")


def rendered_causes(body: str, provider: str, notes: list[str]) -> list[tuple[str, int]]:
    """Each cause the fleet-empty body names, with the count it renders, in its order.
    The operators' notes (and a withheld one's text) are masked first, so a note
    that reads like a cause or holds a comma is no part of its own."""
    prefix, suffix = f"No {provider} lane is dispatchable: ", ". Run: subfleet status"
    assert body.startswith(prefix) and body.endswith(suffix), body
    middle = body[len(prefix):len(body) - len(suffix)]
    for note in sorted({*notes, WITHHELD}, key=len, reverse=True):
        middle = middle.replace(note, "\x00")
    parts = [RENDERED_CAUSE.match(part) for part in middle.split(", ")]
    assert all(parts), (body, middle)
    return [(part[2], int(part[1])) for part in parts]


def check_fleet_empty(rows: list[dict]) -> None:
    """I3 over one fleet with nothing dispatchable, through the real alert builder."""
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
    # Review r1 of #263, P3-4: every count the body renders is the condition's count
    # for that cause, and a cause with no lane is not named at all.
    assert rendered_causes(body, provider, notes_of(rows)) == [
        (words, causes[cause]) for cause, words in CAUSE_WORDS.items() if causes[cause]]
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


#: The spec's texts (review r1, decision 1): the credential refusal, and what a reader shows instead
#: of a stored note that would carry one.
CREDENTIAL_RULE = "--note must not carry a credential (the C-23.14 scrubber would change it)"
WITHHELD = "[note withheld: it would carry a credential]"
#: A 160-character note an operator might write: the longest the rule accepts.
LONGEST = ("codex-open: disk brake; load 140 > 120; " * 4)[:159] + "!"


def note_rule(text: str) -> str | None:
    """The spec: the rule `text` breaks, or None when it is a note. After stripping it
    must be one line of 1 to 160 printable characters, and the C-23.14 scrubber must
    leave it whole, read from its start or from just after any `-` or `_` in it, both
    as written and as JSON writes it (review r2 of #263, P3-1: `status --json` and
    `status.json` carry `\\u00fc` for `ü`)."""
    stripped = text.strip()
    if not (0 < len(stripped) <= 160 and stripped.isprintable()):
        return "--note must be one line of 1 to 160 printable characters"
    return CREDENTIAL_RULE if holds_credential(stripped) or holds_credential(json.dumps(stripped)[1:-1]) else None


def valid_note(text: str) -> bool:
    return note_rule(text) is None


def stored_note(source) -> str | None:
    """The spec (review r2 of #263, P3-3): the note a hold's stored `source_event`
    shows. None for exactly `operator`; the note for exactly `operator: <note>` with
    a note the rule accepts as it stands; the withheld text for anything else."""
    if source == "operator":
        return None
    note = source[len("operator: "):] if isinstance(source, str) and source.startswith("operator: ") else None
    return note if note is not None and valid_note(note) and note == note.strip() else WITHHELD


NOTES = st.one_of(
    st.text(max_size=170),
    st.text(st.characters(codec="utf-8", exclude_categories=("Cs",)), min_size=150, max_size=175),
    st.builds(lambda head, cut, tail: head + cut + tail, st.text(max_size=20),
              st.sampled_from(["\n", "\r", "\t", "\x00", "\x07", "\x1b", "\x7f", "\x85", " ", "​", "\xa0"]),
              st.text(max_size=20)),
    st.sampled_from([NOTE, " " + NOTE + " \n", LONGEST, "x" * 160, "x" * 161, "   ", ""]))


@settings(max_examples=60, deadline=None, database=None, derandomize=True, suppress_health_check=FIXTURE_HEALTH)
@given(note=NOTES)
@example(note=NOTE)
@example(note="disk\nbrake")
@example(note="disk\x07brake")
@example(note="x" * 161)
@example(note=LONGEST)
@example(note="x" * 160)                                  # a 160-character base64 run to the scrubber,
@example(note="api_key=r1_dummy_01234567890123456789")     # and a named value: each refused, never masked
def test_i4_a_note_round_trips_or_is_refused_with_the_store_unchanged(core, note):
    until = future()
    before = store_counts(core)
    if not valid_note(note):
        with pytest.raises(protocol.ProtocolError) as refused:
            core.dispatch("lanes", {"action": "hold", "lane_id": "codex-1", "until": until, "note": note})
        assert refused.value.code == Exit.INVALID_INPUT and refused.value.reason == "hold-note"
        assert protocol.refusal_text(refused.value) == (f"lanes hold: {note_rule(note)}", None)
        assert store_counts(core) == before                  # no closure, no event
        return
    expected = note.strip()
    core.policy.setdefault("alerts", {})["operator_session"] = "r1-session"
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
        alert = conditions(*core.timers.snapshot()["lanes"])["codex-fleet-empty"]
        assert alert["body"].startswith(f"No codex lane is dispatchable: 1 held by operator ({expected}; earliest end ")
        assert core._timer_notice(alert)                     # the session notice the alert becomes
        notice = core.store.one("SELECT text FROM service_notices ORDER BY notice_id DESC LIMIT 1")["text"]
        assert notice == f"codex: no dispatchable lanes\n{alert['body']}"
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


REPOSITORY = Path(__file__).resolve().parents[2]


#: SQL that compares the column with `operator` (review r2 of #263, P3-4): `=`, `==`,
#: `!=`, `<>`, `IN`, `IS` or `LIKE`, then the word in either quote, or a `?` (the
#: group), which counts only when the call the SQL is passed to has the word among
#: its other arguments.
SQL_COMPARISON = re.compile(r"""source_event\s*(?:==?|!=|<>|\bIN\b|\bIS\b|\bLIKE\b)\s*\(?\s*(?:'operator'|"operator"|(\?))""",
                            re.IGNORECASE)
COMPARISONS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn, ast.Is, ast.IsNot)
COLLECTIONS = ("frozenset", "set", "tuple", "list")


def _is_operator(node: ast.AST) -> bool:
    """The literal `"operator"`, or a name or an attribute `HOLD_SOURCE` (the contract's constant)."""
    return (isinstance(node, ast.Constant) and node.value == "operator"
            or isinstance(node, ast.Name) and node.id == "HOLD_SOURCE"
            or isinstance(node, ast.Attribute) and node.attr == "HOLD_SOURCE")


def _holds_operator(node: ast.AST) -> bool:
    """`_is_operator`, or a set, tuple or list, or a `frozenset(…)`, `set(…)`, `tuple(…)`
    or `list(…)` of one, holding it."""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_holds_operator(item) for item in node.elts)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in COLLECTIONS
            and len(node.args) == 1 and not node.keywords):
        return _holds_operator(node.args[0])
    return _is_operator(node)


def _sql_text(node: ast.AST) -> str | None:
    """A string constant's text, or an f-string's with each `operator` it formats in
    written out (and `{}` for anything else it formats); None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) else
                       "operator" if _is_operator(part.value) else "{}" for part in node.values)
    return None


def _sql_compares(node: ast.AST, others: list[ast.AST]) -> bool:
    """Whether SQL in `node` compares `source_event` with the word: written in, or a
    `?` for which `others`, the rest of the call it is passed to, give the word."""
    text = _sql_text(node)
    return text is not None and any(
        found.group(1) is None or any(_is_operator(inner) for other in others for inner in ast.walk(other))
        for found in SQL_COMPARISON.finditer(text))


def _comparisons(text: str, filename: str) -> list[int]:
    """The lines of `text` that compare with `operator`, as far as its syntax shows
    (C-9.6, review r2 of #263, P3-4). Caught: a comparison (`==`, `!=`, `in`,
    `not in`, `is`, `is not`) with the literal `"operator"` or a name or
    attribute `HOLD_SOURCE` on either side, or a set, tuple, list or
    `frozenset(…)` holding one, whatever the other side is, so a wrapped column
    (`str(…).strip()`, `(x or "")`), an alias of it, or a helper's parameter;
    a `match` case whose value pattern is one; and SQL in a string or f-string
    comparing `source_event` with the word in either quote, or with a `?` its
    call passes the word for. Not seen: a comparison through `getattr`,
    `operator.eq` or a lookup, or with the word or the SQL held in a variable
    first. A prefix test (`startswith`, `hold_note_of`) is no comparison."""
    lines = []
    for node in ast.walk(ast.parse(text, filename=filename)):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(isinstance(op, COMPARISONS) and (_holds_operator(left) or _holds_operator(right))
                   for op, left, right in zip(node.ops, operands, operands[1:])):
                lines.append(node.lineno)
        elif isinstance(node, ast.Match):
            lines += [pattern.lineno for case in node.cases for pattern in ast.walk(case.pattern)
                      if isinstance(pattern, ast.MatchValue) and _is_operator(pattern.value)]
        elif isinstance(node, ast.Call):
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            lines += [argument.lineno for index, argument in enumerate(arguments)
                      if _sql_compares(argument, arguments[:index] + arguments[index + 1:])]
        elif _sql_compares(node, []):
            lines.append(node.lineno)
    return sorted(set(lines))


def operator_comparisons(*roots: Path) -> list[str]:
    """`path:line` for each place under `roots` that compares with `operator`, as far as syntax shows (`_comparisons`)."""
    return [f"{path}:{number}"
            for path in sorted(path for root in roots for path in root.rglob("*.py"))
            for number in _comparisons(path.read_text(), str(path))]


def test_nothing_reads_a_closure_source_for_equality_with_operator():
    """D-STATUS-SRC1: the note rides in `source_event`, so no reader may compare it with `operator`.
    The check is syntactic (C-9.6); `_comparisons` names what it does not see."""
    assert operator_comparisons(REPOSITORY / "subfleet", REPOSITORY / "tools") == []


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


@pytest.mark.parametrize("json_flag", ([], ["--json"]))
def test_lanes_hold_with_a_note_refuses_an_older_daemon_that_dropped_it(daemon, capsys, json_flag):
    daemon({"lanes": lambda request: {"held": "codex-1", "released": None, "lanes": [], "closures": []}})
    assert cli.main(["lanes", "hold", "codex-1", "--until", "2026-10-25T03:51:22Z", "--note", NOTE, *json_flag]) == 69
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
@example(text="x" * 160)
@example(text=LONGEST)
@example(text="run-sk-r1dummy0123456789abcdef")
def test_hold_note_accepts_exactly_the_spec_and_round_trips_through_source_event(text):
    if valid_note(text):
        note = contracts.hold_note(text)
        assert note == text.strip()
        assert contracts.hold_source(note) == f"operator: {note}"
        assert contracts.hold_note_of(contracts.hold_source(note)) == note
    else:
        with pytest.raises(ValueError) as refused:
            contracts.hold_note(text)
        assert str(refused.value) == note_rule(text)          # each refusal names its own rule
    assert contracts.hold_note(None) is None and contracts.hold_source(None) == "operator"
    assert contracts.hold_note_of("operator") is None
    # Review r2 of #263, P3-3: a hold's source reads strictly. It is `operator`, or
    # `operator: ` and a note `hold_note` takes as it stands; anything else, the
    # text itself included, is withheld.
    for source in (text, f"operator: {text}"):
        assert contracts.hold_note_of(source) == stored_note(source)
    for other in ("wham", None, "", "operator: ", "Operator", f"operator:{NOTE}", f"operator:  {NOTE}",
                  f"operator: {NOTE} ", f"Operator: {NOTE}", f" operator: {NOTE}"):
        assert contracts.hold_note_of(other) == WITHHELD
        assert contracts.hold_source_shown(other) == f"operator: {WITHHELD}"


# --- review r1 (P2, P3-1 to P3-4) ---------------------------------------------------
#
# P2: a note the C-23.14 scrubber would change is refused, never masked, as a
# request id (C-1.5) and a correlation id (C-25.7) are, and no refusal names it.
# Every credential below is a dummy made up for these tests.

DUMMY_NOTE = "api_key=review_dummy_01234567890123456789"
DUMMY_NOTES = [DUMMY_NOTE, "sk-r1dummyABCDEFGHIJ0123456789", "run-sk-r1dummyABCDEFGHIJ0123456789",
               "codex-open: Bearer r1dummyabcdefghij0123", "x_glpat-r1dummyABCDEFGHIJ012345"]


def store_text(service) -> str:
    """Every value of every row the store holds, as one text."""
    tables = [row["name"] for row in service.store.query("SELECT name FROM sqlite_master WHERE type='table'")]
    return "\n".join(repr(tuple(row)) for table in tables for row in service.store.query(f'SELECT * FROM "{table}"'))


def files_holding(root: Path, text: str) -> list[str]:
    """The files under `root` (the store, its WAL, the daemon's log) whose bytes hold `text`."""
    return sorted(str(path.relative_to(root)) for path in root.rglob("*")
                  if path.is_file() and text.encode() in path.read_bytes())


def test_r1_p2_a_credential_note_reaches_no_status_alert_or_notice(core):
    """Review r1 P2, as executed: the shared scrubber masks this dummy, and status, the
    alert and the session notice must not carry it. A plain hold first keeps the lane
    held whether or not the later one is refused, so the alert exists either way."""
    clean, count = scrub_secrets(DUMMY_NOTE, strip_reminders=True)
    assert count and clean != DUMMY_NOTE
    core.policy.setdefault("alerts", {})["operator_session"] = "review-session"
    core.dispatch("lanes", {"action": "hold", "lane_id": "codex-1", "until": future(10), "note": "load 140"})
    with contextlib.suppress(protocol.ProtocolError):     # a later hold would give the row its note
        core.dispatch("lanes", {"action": "hold", "lane_id": "codex-1", "until": future(20), "note": DUMMY_NOTE})
    status = core.dispatch("daemon.status", {})
    alert = next(row for row in alerts.evaluate_conditions(core.timers.snapshot()) if row["key"] == "codex-fleet-empty")
    assert core._timer_notice(alert)
    text = core.store.one("SELECT text FROM service_notices")["text"]
    assert all(DUMMY_NOTE not in value for value in (cli.format_status(status), str(status), alert["body"], text))


def test_r1_p2_the_credential_rule_is_a_contract_text_of_its_own():
    assert contracts.HOLD_NOTE_CREDENTIAL_RULE == CREDENTIAL_RULE != contracts.HOLD_NOTE_RULE
    assert contracts.HOLD_NOTE_WITHHELD == WITHHELD
    for note in DUMMY_NOTES:
        with pytest.raises(ValueError) as refused:
            contracts.hold_note(f"  {note} ")
        assert str(refused.value) == CREDENTIAL_RULE and refused.value.args == (CREDENTIAL_RULE,)


@pytest.mark.parametrize("note", DUMMY_NOTES)
def test_r1_p2_the_daemon_refuses_a_credential_note_and_names_it_nowhere(core, note):
    """Exit 2, reason `hold-note`, its own rule on the wire; the note in no error,
    log line, row or file of the store."""
    before = store_counts(core)
    with pytest.raises(protocol.ProtocolError) as refused:
        core.dispatch("lanes", {"action": "hold", "lane_id": "codex-1", "until": future(), "note": note})
    error = refused.value
    assert (error.code, error.reason) == (Exit.INVALID_INPUT, "hold-note")
    reply = protocol.encode(protocol.refuse("r1-request", error)).decode()
    assert json.loads(reply)["error"] == {"code": 2, "message": f"lanes hold: {CREDENTIAL_RULE}", "fix": None}
    line = refusals_module.detail_line("lanes", error)
    refusals_module.log_detail(core.log, "lanes", error)
    for handler in core.log.handlers:
        handler.flush()
    dummy = note.rsplit(" ", 1)[-1].split("=")[-1]          # the value alone, whatever its shape
    for shown in (str(error), repr(error.args), repr(error.details), str(error.fix), reply, str(line)):
        assert note not in shown and dummy not in shown
    assert store_counts(core) == before                      # no closure, no event
    assert dummy not in store_text(core)
    assert files_holding(core.root, dummy) == []


@pytest.mark.parametrize("note", DUMMY_NOTES)
def test_r1_p2_lanes_hold_refuses_a_credential_note_without_naming_it(daemon, capsys, note):
    server = daemon({"lanes": held_answer})
    assert cli.main(["lanes", "hold", "codex-1", "--until", "2026-10-25T03:51:22Z", "--note", note]) == 2
    out, err = capsys.readouterr()
    assert (out, err) == ("", f"subfleet: lanes hold: {CREDENTIAL_RULE}\n")
    dummy = note.rsplit(" ", 1)[-1].split("=")[-1]
    assert dummy not in out and dummy not in err
    assert server.ops() == []                                # refused before the daemon is asked


def test_r1_p2_a_stored_credential_note_is_withheld_by_every_reader(core):
    """The reader's backstop: a hold row no writer here would make (written to the
    store directly) never shows its note in status, JSON, the alert or the notice."""
    core.policy.setdefault("alerts", {})["operator_session"] = "review-session"
    until = future()
    core.store.add_closure(contracts.Closure("codex-1", "account", until, contracts.ClosureReason.OPERATOR_HOLD,
                                             contracts.ClockSource.REPORTED, f"operator: {DUMMY_NOTE}"))
    dummy = DUMMY_NOTE.split("=")[-1]
    status = core.dispatch("daemon.status", {})
    rows = [*status["closures"], *status["lanes"][0]["closures"]]
    assert [(row["kind"], row["note"], row["source_event"]) for row in rows] == [
        ("hold", WITHHELD, f"operator: {WITHHELD}")] * 2
    assert f"  codex-1 held by operator until {until}: {WITHHELD}" in cli.format_status(status).splitlines()
    offline = Offline(core.root).status()
    assert [(row["note"], row["source_event"]) for row in offline["closures"]] == [(WITHHELD, f"operator: {WITHHELD}")]
    alert = conditions(*core.timers.snapshot()["lanes"])["codex-fleet-empty"]
    assert f"1 held by operator ({WITHHELD}; earliest end " in alert["body"]
    assert core._timer_notice(alert)
    notice = core.store.one("SELECT text FROM service_notices")["text"]
    for shown in (cli.format_status(status), str(status), str(offline["closures"]), cli.format_status(offline),
                  alert["body"], notice, render_module.closure_text(core.store.list_closures()[0])):
        assert dummy not in shown
    assert contracts.hold_note_of(f"operator: {DUMMY_NOTE}") == WITHHELD
    assert contracts.hold_note_of(f"operator: {NOTE}") == NOTE


# P3-1: an account closure refuses the lane before its evidence is read, as release/217 did.

@pytest.mark.parametrize("readings", [None, [None]])
def test_r1_p3_1_a_closed_lane_keeps_the_release_answer_whatever_its_readings(readings):
    row = {"lane_id": "codex-1", "provider": "codex", "enabled": True, "owner": "v2", "readings": readings,
           "closures": [{"scope": "account", "reason": "operator-hold", "until_at": "2099-01-01T00:00:00Z"}]}
    assert legacy_dispatchable(row) is False
    assert status_json.dispatchable(row) is False


# P3-2: the hold answer's nested closure rows carry `kind` and `note`, as `daemon.status`'s do.

def test_r1_p3_2_the_hold_answer_enriches_nested_closure_rows(core):
    answer = core.dispatch("lanes", {"action": "hold", "lane_id": "codex-1", "until": future(), "note": "operator"})
    assert (answer["closures"][0]["kind"], answer["closures"][0]["note"]) == ("hold", "operator")
    nested = answer["lanes"][0]["closures"][0]
    assert (nested.get("kind"), nested.get("note")) == ("hold", "operator")
    status = core.dispatch("daemon.status", {})
    assert answer["closures"] == status["closures"] and nested == status["lanes"][0]["closures"][0]


# P3-3: the source guard reads each module's syntax tree, so a comparison split over
# lines is caught. Each case is a module under pytest's temporary folder.

GUARD_CASES = {
    "sql": 'def compares(conn):\n    return conn.execute("SELECT 1 FROM closures WHERE source_event = \'operator\'")\n',
    "one line": 'def compares(row):\n    return row["source_event"] == "operator"\n',
    "two lines": 'def compares(row):\n    return (row["source_event"]\n            == "operator")\n',
    "get, not equal": 'def compares(row):\n    return row.get("source_event") != "operator"\n',
    "in a tuple": 'def compares(row):\n    return row["source_event"] in ("operator",)\n',
    "attribute, set": 'def compares(closure):\n    return closure.source_event not in {"wham", "operator"}\n',
    "operator first": 'def compares(row):\n    return "operator" == row.get("source_event")\n',
}


@pytest.mark.parametrize("case", sorted(GUARD_CASES))
def test_r1_p3_3_the_source_guard_catches_each_comparison(tmp_path, case):
    module = tmp_path / "scratch_pkg" / "compares.py"
    module.parent.mkdir()
    module.write_text(GUARD_CASES[case])
    assert operator_comparisons(module.parent) == [f"{module}:2"]


def test_r1_p3_3_the_source_guard_passes_prefix_reads(tmp_path):
    module = tmp_path / "scratch_pkg" / "reads.py"
    module.parent.mkdir()
    module.write_text('PREFIX = "operator: "\n'
                      'def note(row):\n'
                      '    source = row.get("source_event")\n'
                      '    held = isinstance(source, str) and source.startswith("operator")\n'
                      '    return source[len(PREFIX):] if held and row["source_event"].startswith(PREFIX) else None\n'
                      'def other(row):\n'
                      '    return row["reason"] == "operator-hold" and row["source_event"] != "wham"\n')
    assert operator_comparisons(module.parent) == []


# Review r2 of #263, P3-4: the guard is syntactic, and catches the comparisons it
# can see as syntax. Review r2 kept none of its 16 scratch modules, so each is
# rebuilt from its list, `name: (module, the lines the guard must name)`. Its
# getattr and its alias are each read both ways: on the column, where the word
# is an operand and the guard sees it, and on the constant, a known miss below.

GUARD_CASES_R2 = {
    "01 literal ==": ('def compares(row):\n    return row["source_event"] == "operator"\n', [2]),
    "02 match, the literal": ('def compares(row):\n    match row["source_event"]:\n        case "operator":\n'
                              '            return True\n    return False\n', [3]),
    "02 match, the constant": ('from subfleet import contracts\ndef compares(row):\n    match row.get("source_event"):\n'
                               '        case "wham" | contracts.HOLD_SOURCE:\n            return True\n    return False\n',
                               [4]),
    "03 == HOLD_SOURCE": ('from subfleet.contracts import HOLD_SOURCE\ndef compares(row):\n'
                          '    return row["source_event"] == HOLD_SOURCE\n', [3]),
    "04 == contracts.HOLD_SOURCE": ('from subfleet import contracts\ndef compares(row):\n'
                                    '    return row.get("source_event") == contracts.HOLD_SOURCE\n', [3]),
    "05 an alias of the column": ('def compares(row):\n    source = row["source_event"]\n    return source == "operator"\n',
                                  [3]),
    "06 a helper function": ('def is_operator(value):\n    return value == "operator"\ndef compares(row):\n'
                             '    return is_operator(row["source_event"])\n', [2]),
    "07 is": ('def compares(row):\n    return row["source_event"] is "operator"\n', [2]),
    "07 is not, the constant": ('from subfleet.contracts import HOLD_SOURCE\ndef compares(closure):\n'
                                '    return closure.source_event is not HOLD_SOURCE\n', [3]),
    "08 str(...).strip() ==": ('def compares(row):\n    return str(row.get("source_event")).strip() == "operator"\n', [2]),
    "09 (x or '') ==": ('def compares(row):\n    return (row["source_event"] or "") == "operator"\n', [2]),
    "10 getattr on the column": ('def compares(closure):\n    return getattr(closure, "source_event") == "operator"\n',
                                 [2]),
    "12 in frozenset(...)": ('def compares(row):\n    return row["source_event"] in frozenset({"wham", "operator"})\n',
                             [2]),
    "12 not in a set of the constant": ('from subfleet.contracts import HOLD_SOURCE\ndef compares(row):\n'
                                        '    return row["source_event"] not in {HOLD_SOURCE}\n', [3]),
    "12 in frozenset((...)) and a list": ('def compares(row, other):\n'
                                          '    return row["source_event"] in frozenset(("operator",)) or other in ["operator"]\n',
                                          [2]),
    "14 SQL ?, the literal its parameter": ('def compares(conn):\n    return conn.execute(\n'
                                            '        "SELECT 1 FROM closures WHERE source_event=?", ("operator",))\n', [3]),
    "14 SQL IN (?), the constant its parameter": ('from subfleet.contracts import HOLD_SOURCE\ndef compares(conn):\n'
                                                  '    return conn.execute("SELECT 1 FROM closures WHERE source_event IN (?)",\n'
                                                  '                        params=[HOLD_SOURCE])\n', [3]),
    "15 SQL, double quotes": ("def compares(conn):\n"
                              "    return conn.execute('SELECT 1 FROM closures WHERE source_event = \"operator\"')\n", [2]),
    "15 SQL LIKE": ("def compares(conn):\n    return conn.execute(\"SELECT 1 FROM closures WHERE source_event LIKE 'operator'\")\n",
                    [2]),
    "16 SQL in an f-string": ('def compares(conn, lane):\n    return conn.execute(f"SELECT 1 FROM closures '
                              'WHERE lane_id=\'{lane}\' AND source_event=\'operator\'")\n', [2]),
    "16 SQL in an f-string, the constant": ('from subfleet.contracts import HOLD_SOURCE\ndef compares(conn):\n'
                                            '    return conn.execute(f"SELECT 1 FROM closures WHERE source_event=\'{HOLD_SOURCE}\'")\n',
                                            [3]),
    # Syntactic: any comparison with the word, whatever the other side, though no code today has one.
    "any comparison with the word": ('def compares(action):\n    return action["trigger"] == "operator"\n', [2]),
}

#: The known misses (C-9.6: the guard is syntactic only). Each compares a closure's
#: `source_event` with `operator`, and the guard does not see it.
GUARD_MISSES = {
    "11 operator.eq": 'import operator\ndef compares(row):\n    return operator.eq(row["source_event"], "operator")\n',
    "13 a dict lookup": 'def compares(row):\n    return {"operator": True}.get(row["source_event"], False)\n',
    "getattr for the constant": ('from subfleet import contracts\ndef compares(row):\n'
                                 '    return row["source_event"] == getattr(contracts, "HOLD_SOURCE")\n'),
    "the constant aliased through a variable": 'MARK = "operator"\ndef compares(row):\n    return row["source_event"] == MARK\n',
    "the SQL aliased through a variable": ('SQL = "SELECT 1 FROM closures WHERE source_event=?"\ndef compares(conn):\n'
                                           '    return conn.execute(SQL, ("operator",))\n'),
}


def scratch_module(tmp_path: Path, text: str) -> Path:
    module = tmp_path / "scratch_pkg" / "compares.py"
    module.parent.mkdir()
    module.write_text(text)
    return module


@pytest.mark.parametrize("case", sorted(GUARD_CASES_R2))
def test_r2_p3_4_the_source_guard_catches_each_comparison_it_can_see(tmp_path, case):
    text, lines = GUARD_CASES_R2[case]
    module = scratch_module(tmp_path, text)
    assert operator_comparisons(module.parent) == [f"{module}:{line}" for line in lines]


@pytest.mark.parametrize("case", sorted(GUARD_MISSES))
def test_r2_p3_4_the_source_guard_known_misses_stay_listed(tmp_path, case):
    """Each is a miss the contract names. One the guard comes to catch fails here, to
    be moved to the cases above and dropped from C-9.6's list."""
    module = scratch_module(tmp_path, GUARD_MISSES[case])
    assert operator_comparisons(module.parent) == []


def test_r2_p3_4_the_source_guard_passes_what_is_no_comparison_with_the_word(tmp_path):
    module = scratch_module(tmp_path, 'from subfleet.contracts import HOLD_SOURCE, hold_note, hold_source\n'
                                      'PREFIX = f"{HOLD_SOURCE}: "\n'
                                      'def parsed(source_event, note):\n'
                                      '    return hold_source(hold_note(note)) == source_event\n'
                                      'def reads(conn, source):\n'
                                      '    conn.execute("SELECT 1 FROM closures WHERE source_event=?", (source,))\n'
                                      '    conn.execute("SELECT 1 FROM closures WHERE reason=?", ("operator",))\n'
                                      '    return conn.execute("UPDATE attempts SET killed_by=\'operator\'")\n'
                                      'def kinds(row):\n'
                                      '    return row["reason"] in ("operator-hold", "auth-dead") or "operator" + "s"\n')
    assert operator_comparisons(module.parent) == []


# Review r3 of #263, P3-1: the SQL check reads more of SQL's comparisons. Each case
# but the last is a form review r3 executed that the guard at `b11661169` missed;
# the last is one it caught, which review r3's regex as written would miss (a space
# after the `(`). `name: (module, the lines the guard must name)`.

GUARD_CASES_R3 = {
    "IN, the word a later item": ("def compares(conn):\n    return conn.execute("
                                  "\"SELECT 1 FROM closures WHERE source_event IN ('wham','operator')\")\n", [2]),
    "NOT IN": ("def compares(conn):\n    return conn.execute(\"SELECT 1 FROM closures WHERE source_event NOT IN ('operator')\")\n",
               [2]),
    "IS NOT": ("def compares(conn):\n    return conn.execute(\"SELECT 1 FROM closures WHERE source_event IS NOT 'operator'\")\n",
               [2]),
    "NOT LIKE": ("def compares(conn):\n"
                 "    return conn.execute(\"SELECT 1 FROM closures WHERE source_event NOT LIKE 'operator'\")\n", [2]),
    "GLOB": ("def compares(conn):\n    return conn.execute(\"SELECT 1 FROM closures WHERE source_event GLOB 'operator'\")\n",
             [2]),
    "the word first": ("def compares(conn):\n    return conn.execute(\"SELECT 1 FROM closures WHERE 'operator' = source_event\")\n",
                       [2]),
    "lower(source_event)": ("def compares(conn):\n"
                            "    return conn.execute(\"SELECT 1 FROM closures WHERE lower(source_event)='operator'\")\n", [2]),
    "a space after the (, caught at b11661169": ("def compares(conn):\n    return conn.execute("
                                                 "\"SELECT 1 FROM closures WHERE source_event IN ( 'operator')\")\n", [2]),
}


@pytest.mark.parametrize("case", sorted(GUARD_CASES_R3))
def test_r3_p3_1_the_source_guard_catches_each_sql_form_it_reads(tmp_path, case):
    text, lines = GUARD_CASES_R3[case]
    module = scratch_module(tmp_path, text)
    assert operator_comparisons(module.parent) == [f"{module}:{line}" for line in lines]


# P3-4: I3 checks every count the fleet-empty body renders.

def test_r1_p3_4_i3_catches_a_rendered_count_dropped(monkeypatch):
    original = alerts.fleet_empty_body
    monkeypatch.setattr(alerts, "fleet_empty_body",
                        lambda provider, causes: original(provider, causes).replace("1 disabled", "disabled"))
    rows = [lane("codex-1", enabled=False, dispatchable=False)]
    assert alerts.evaluate_conditions({"lanes": rows})[0]["causes"]["disabled"] == 1
    with pytest.raises(AssertionError):
        check_fleet_empty(rows)
    monkeypatch.setattr(alerts, "fleet_empty_body", original)
    check_fleet_empty(rows)


# I5: no text the C-23.14 scrubber would change reaches the store, status, the alerts
# or a notice through a hold. Notes are printable text with a dummy credential spliced
# in, in the shapes the scrubber's own tests use, alone or after a `-` or `_` joiner.

DUMMY_VALUES = st.text(string.ascii_letters + string.digits, min_size=20, max_size=28)
#: Review r2 of #263, P3-1: URL passwords too, in the scrubber's URL rule's shape.
CREDENTIAL_SHAPES = ("api_key={}", "sk-{}", "glpat-{}", "xoxb-{}", "Bearer {}", "https://u:{}@host",
                     "ftp://operator:{}@example.invalid/x")
CREDENTIALS = st.builds(lambda shape, value: (shape.format(value), value), st.sampled_from(CREDENTIAL_SHAPES),
                        DUMMY_VALUES)
JOINED = st.one_of(CREDENTIALS, st.builds(lambda word, joiner, credential: (word + joiner + credential[0], credential[1]),
                                          st.text(string.ascii_letters, min_size=1, max_size=8),
                                          st.sampled_from("-_"), CREDENTIALS))
PRINTABLE = st.text(st.characters(codec="utf-8", categories=("L", "N", "P", "S", "Zs")).filter(str.isprintable),
                    max_size=30)
#: Review r2 of #263, P3-1: a letter outside ASCII just before the credential, where the
#: URL rule's leading `\b` finds no boundary and JSON writes a `\uXXXX` escape.
NON_ASCII = st.sampled_from(["", "ü", "é", "中"])
SPLICED = st.builds(lambda head, letter, joined, tail: (head + letter + joined[0] + tail, joined[1]),
                    PRINTABLE, NON_ASCII, JOINED, PRINTABLE)
#: Review r2 of #263, P3-1, as executed: each refused only because its JSON form is read.
URL_SECRET = "SECRET7dummy0123456789abcdef"
URL_NOTES = [f"{letter}https://u:{URL_SECRET}@host" for letter in ("ü", "é", "中")]


def scrubbed_alike(text: str) -> bool:
    """Whether the C-23.14 scrubber leaves `text` as it is, read whole and from each joiner."""
    return scrub_secrets(text, strip_reminders=True, whole=True) == (text, 0) and not holds_credential(text)


def survives_scrubbing(document: str, note: str) -> bool:
    """Whether `note`, as JSON writes it, is in `document` (a JSON text) and every copy
    of it comes through the C-23.14 scrubber over the whole of `document` unchanged.
    (The whole document need not: `daemon.status` carries `"lane_credentials": []`.)"""
    written = json.dumps(note)[1:-1]
    clean, _ = scrub_secrets(document, strip_reminders=True, whole=True)
    return document.count(written) > 0 and clean.count(written) == document.count(written)


@settings(max_examples=150, deadline=None, database=None, derandomize=True, suppress_health_check=FIXTURE_HEALTH)
@given(spliced=SPLICED)
@example(spliced=(DUMMY_NOTE, "review_dummy_01234567890123456789"))
@example(spliced=("xsk-r1dummyABCDEFGHIJ0123456789 done", "r1dummyABCDEFGHIJ0123456789"))
@example(spliced=("hold: run-sk-r1dummyABCDEFGHIJ0123456789", "r1dummyABCDEFGHIJ0123456789"))
@example(spliced=(URL_NOTES[0], URL_SECRET))
@example(spliced=(URL_NOTES[1], URL_SECRET))
@example(spliced=(f"load 140: {URL_NOTES[2]} now", URL_SECRET))
def test_i5_no_credential_reaches_the_store_status_alerts_or_a_notice_through_a_hold(core, spliced):
    note, value = spliced
    core.policy.setdefault("alerts", {})["operator_session"] = "r1-session"
    before = store_counts(core)
    until = future()
    try:
        held = core.dispatch("lanes", {"action": "hold", "lane_id": "codex-1", "until": until, "note": note})
    except protocol.ProtocolError as refused:
        assert (refused.code, refused.reason) == (Exit.INVALID_INPUT, "hold-note")
        assert store_counts(core) == before and value not in store_text(core)
        return
    try:
        accepted = held["note"]
        assert accepted == note.strip() and not holds_credential(accepted)
        status = core.dispatch("daemon.status", {})
        shown = [row for row in (*status["closures"], *status["lanes"][0]["closures"]) if row["kind"] == "hold"]
        line = next(text for text in cli.format_status(status).splitlines() if " held by operator " in text)
        alert = conditions(*core.timers.snapshot()["lanes"])["codex-fleet-empty"]
        assert core._timer_notice(alert)
        notice = core.store.one("SELECT text FROM service_notices ORDER BY notice_id DESC LIMIT 1")["text"]
        event = json.loads(core.store.one("SELECT data_json FROM events WHERE kind='lane.held' "
                                          "ORDER BY event_id DESC LIMIT 1")["data_json"])
        texts = [event["note"], line, alert["body"], notice,
                 *(row[key] for row in shown for key in ("note", "source_event"))]
        assert all(scrubbed_alike(text) for text in texts)
        # Review r2 of #263, P3-1: and as JSON carries them, in `status --json` (the
        # CLI's `emit`) and in `status.json` as the timers publish it, its alerts included.
        assert all(scrubbed_alike(json.dumps(text)[1:-1]) for text in texts)
        snapshot = core.timers.snapshot()
        core.timers.alerts.evaluate(snapshot, now=core.timers.now(), offline=False)
        core.timers.publish_status(snapshot)
        for document in (json.dumps(status, sort_keys=True, default=str), (core.root / "status.json").read_text()):
            assert survives_scrubbing(document, accepted)
    finally:
        core.dispatch("lanes", {"action": "release", "lane_id": "codex-1"})


#: An ordinary note: words of 1 to 18 characters of letters, digits and `:;,./()-`,
#: joined by spaces. 18 is under the shortest token shape the scrubber reads (`sk-`
#: and 16 characters), and no word spells a key word it reads (`api_key`, `token`,
#: `passwd`, `cookie`, `Bearer`, `base64` in a data URI, and the rest).
KEY_STEMS = ("key", "token", "secret", "pass", "cookie", "authoriz", "credential", "bearer", "base64")
WORDS = st.text(string.ascii_letters + string.digits + ":;,./()-", min_size=1, max_size=18).filter(
    lambda word: not any(stem in word.lower() for stem in KEY_STEMS))
ORDINARY = st.lists(WORDS, min_size=1, max_size=40).map(" ".join).filter(lambda text: len(text) <= 160)


@settings(max_examples=400, deadline=None, database=None, derandomize=True)
@given(note=ORDINARY)
@example(note=NOTE)
@example(note="load 140 > 120")
@example(note=LONGEST)
def test_i5_an_ordinary_note_is_always_accepted(note):
    assert contracts.hold_note(note) == note
    assert contracts.hold_note_of(contracts.hold_source(note)) == note


@pytest.mark.parametrize("note", ["x" * 160, "abc/" * 40, "sk-" + "a" * 16, "token: x", "cookie: x",
                                  "Bearer abcdefgh", "--token abc", "AKIA" + "A" * 16, "passwd:x",
                                  "eyJabcdefgh.abcdefgh.abcdefgh"])
def test_i5_the_plain_alphabet_can_spell_a_credential_shape_and_that_is_refused(note):
    """Why the ordinary-note property bounds its words: over letters, digits, spaces and
    `:;,./()-` alone, these are each a shape the scrubber masks."""
    assert holds_credential(note)
    with pytest.raises(ValueError, match=re.escape(CREDENTIAL_RULE)):
        contracts.hold_note(note)


# --- review r2 (P3-1 to P3-5) -------------------------------------------------------
#
# Every credential below is a dummy made up for these tests (`SECRET7…`).

# P3-1: a URL password just after a letter outside ASCII. The raw note passes the
# scrubber (its URL rule opens with `\b`, and there is no boundary between `ü` and
# `h`); the JSON form `status --json` and `status.json` carry (`ühttps://…`)
# does not. `hold_note` reads both.

@pytest.mark.parametrize("note", URL_NOTES)
def test_r2_p3_1_a_url_password_after_a_non_ascii_letter_is_refused(core, note):
    assert not holds_credential(note) and holds_credential(json.dumps(note)[1:-1])     # why the JSON form is read
    with pytest.raises(ValueError) as rule:
        contracts.hold_note(note)
    assert str(rule.value) == CREDENTIAL_RULE
    before = store_counts(core)
    with pytest.raises(protocol.ProtocolError) as refused:
        core.dispatch("lanes", {"action": "hold", "lane_id": "codex-1", "until": future(), "note": note})
    assert (refused.value.code, refused.value.reason) == (Exit.INVALID_INPUT, "hold-note")
    assert protocol.refusal_text(refused.value) == (f"lanes hold: {CREDENTIAL_RULE}", None)
    assert store_counts(core) == before and URL_SECRET not in store_text(core)


@pytest.mark.parametrize("note", URL_NOTES)
def test_r2_p3_1_lanes_hold_refuses_it_before_asking_the_daemon(daemon, capsys, note):
    server = daemon({"lanes": held_answer})
    assert cli.main(["lanes", "hold", "codex-1", "--until", "2026-10-25T03:51:22Z", "--note", note]) == 2
    assert tuple(capsys.readouterr()) == ("", f"subfleet: lanes hold: {CREDENTIAL_RULE}\n")
    assert server.ops() == []


def test_d_scr_url1_a_url_password_after_a_digit_is_accepted_today():
    """D-SCR-URL1, outside #263, pinned as it stands: no scrubber mode masks a URL
    password just after a digit, raw or as JSON writes it (the URL rule's `\\b`
    finds no boundary, and its scheme must open with a letter). The hold rule reads
    the scrubber, so it accepts this note. When D-SCR-URL1 changes the URL rule,
    this note is refused and this test is to be turned round."""
    note = f"1https://u:{URL_SECRET}@host"
    for text in (note, json.dumps(note)[1:-1]):
        for whole in (False, True):
            assert scrub_secrets(text, strip_reminders=True, whole=whole) == (text, 0)
        assert not holds_credential(text)
    assert contracts.hold_note(note) == note


# P3-2, P3-5: notes no writer here accepts, stored outside `lanes hold`: a credential,
# and an ordinary note one character over the rule's 160.
OVER_LONG = LONGEST + "!"
STORED_NOTES = {"credential": f"api_key={URL_SECRET}", "161 characters": OVER_LONG}
SHOWN_WITHHELD = (WITHHELD, f"operator: {WITHHELD}")


def stored_hold(core, source: str) -> str:
    """A hold row with `source` as its `source_event`, written to the store directly."""
    until = future()
    core.store.add_closure(contracts.Closure("codex-1", "account", until, contracts.ClosureReason.OPERATOR_HOLD,
                                             contracts.ClockSource.REPORTED, source))
    return until


def shown_holds(rows) -> list[tuple]:
    """Each hold row's note and `source_event`, as a reader shows them."""
    return [(row.get("note"), row["source_event"]) for row in rows if row.get("reason") == "operator-hold"]


def decision_holds(decision: dict) -> list:
    """Each hold row's `source_event` in a route decision's evaluations."""
    return [row["source_event"] for evaluation in decision["evaluations"]
            for row in (*evaluation["closures"], *evaluation["stranding_closures"])
            if row.get("reason") == "operator-hold"]


@pytest.mark.parametrize("case", sorted(STORED_NOTES))
def test_r2_p3_2_every_reader_withholds_a_stored_note(core, tmp_path, case):
    """Each reader of closure rows outside `status` (review r2 of #263, P3-2, and the
    route decisions the grep found): the `lanes` list, `readings`, `errors`, the
    `release --limits` answer, `why` (a shape, and a job's recorded decision), the
    `run --dry-run` answer and the offline `runs show`."""
    note = STORED_NOTES[case]
    assert case != "161 characters" or (len(note) == 161 and contracts.hold_note(note[:160]) == note[:160])
    stored_hold(core, f"operator: {note}")
    core.store.add_closure(contracts.Closure("codex-1", "account", future(), contracts.ClosureReason.PROVIDER_LIMIT,
                                             contracts.ClockSource.GUESSED, "attempt"))      # for `release --limits`
    answers = {}
    answers["lanes"] = core.dispatch("lanes", {})
    assert shown_holds(answers["lanes"]["lanes"][0]["closures"]) == [SHOWN_WITHHELD]
    answers["readings"] = core.dispatch("readings", {})
    assert shown_holds(answers["readings"]["closures"]) == [SHOWN_WITHHELD]
    answers["errors"] = operations.error_report(core.store.query, 1)
    assert shown_holds(answers["errors"]["closures"]) == [SHOWN_WITHHELD]
    # The route decision as the scheduler makes it carries the stored row, as stored.
    shape = protocol.coerce_args(protocol.WhyArgs, {"task": "build", "tier": "standard"})
    recorded = dataclasses.asdict(core._pick(dataclasses.asdict(shape), desktop=core._desktop_identity()))
    assert decision_holds(recorded) == [f"operator: {note}"]
    answers["why"] = core.dispatch("why", {"task": "build", "tier": "standard"})
    assert decision_holds(answers["why"]["decision"]) == [SHOWN_WITHHELD[1]]
    work, prompt = tmp_path / "work", tmp_path / "prompt.md"
    work.mkdir()
    prompt.write_text("r2 probe")
    core.store.add_job(job_id="r2-job", request_id="r2-request", payload_digest="digest", kind="dispatch",
                       workdir=str(work), prompt_path=str(prompt), sandbox="read-only", state="succeeded")
    core.store.add_decision("r2-job", recorded)
    answers["why job"] = core.dispatch("why", {"job_id": "r2-job"})
    assert decision_holds(answers["why job"]["decision"]) == [SHOWN_WITHHELD[1]]
    answers["runs show (offline)"] = Offline(core.root).show_job("r2-job")
    assert decision_holds(answers["runs show (offline)"]["decision"]) == [SHOWN_WITHHELD[1]]
    answers["run --dry-run"] = core.dispatch("submit", {
        "request_id": "r2-dry-run", "kind": "dispatch", "workdir": str(work), "prompt_path": str(prompt),
        "sandbox": "read-only", "task": "build", "tier": "standard", "allow_tmp": True, "dry_run": True})
    assert decision_holds(answers["run --dry-run"]["decision"]) == [SHOWN_WITHHELD[1]]
    answers["release --limits"] = core.dispatch("lanes", {"action": "release-limits", "lane_id": "codex-1",
                                                          "note": "false positive"})
    assert shown_holds(answers["release --limits"]["closures"]) == [SHOWN_WITHHELD]
    assert shown_holds(answers["release --limits"]["lanes"][0]["closures"]) == [SHOWN_WITHHELD]
    for name, answer in answers.items():                     # as JSON (`--json`, `errors`) and as text
        for shown in (json.dumps(answer, sort_keys=True, default=str), str(answer)):
            assert note not in shown and json.dumps(note)[1:-1] not in shown, name


# P3-3: the reader's backstop parses a hold's source strictly: hold rows a writer
# outside `lanes hold` could leave, which the backstop let through raw unless they
# began exactly `operator: `.
MALFORMED_SOURCES = {"no space": f"operator:api_key={URL_SECRET}", "capital": f"Operator: api_key={URL_SECRET}",
                     "no prefix": f"sk-{URL_SECRET}", "url after ü": f"operator: {URL_NOTES[0]}"}


@pytest.mark.parametrize("case", sorted(MALFORMED_SOURCES))
def test_r2_p3_3_a_hold_source_lanes_hold_would_not_write_is_withheld(core, case):
    core.policy.setdefault("alerts", {})["operator_session"] = "r2-session"
    until = stored_hold(core, MALFORMED_SOURCES[case])
    status = core.dispatch("daemon.status", {})
    assert shown_holds([*status["closures"], *status["lanes"][0]["closures"]]) == [SHOWN_WITHHELD] * 2
    offline = Offline(core.root).status()
    assert shown_holds(offline["closures"]) == [SHOWN_WITHHELD]
    text = cli.format_status(status)
    assert f"  codex-1 held by operator until {until}: {WITHHELD}" in text.splitlines()
    alert = conditions(*core.timers.snapshot()["lanes"])["codex-fleet-empty"]
    assert f"1 held by operator ({WITHHELD}; earliest end " in alert["body"]
    assert core._timer_notice(alert)
    notice = core.store.one("SELECT text FROM service_notices ORDER BY notice_id DESC LIMIT 1")["text"]
    for shown in (json.dumps(status, sort_keys=True, default=str), json.dumps(offline, sort_keys=True, default=str),
                  text, cli.format_status(offline), alert["body"], notice):
        assert URL_SECRET not in shown


# P3-5: a stored note longer than 160 characters is withheld, and unread.

def test_r2_p3_5_a_stored_note_over_160_characters_is_withheld_unread(core, monkeypatch):
    assert len(OVER_LONG) == 161 and not holds_credential(OVER_LONG)          # nothing else to withhold it for
    read = []
    monkeypatch.setattr(ids, "holds_credential", lambda text, real=ids.holds_credential: read.append(text) or real(text))
    core.policy.setdefault("alerts", {})["operator_session"] = "r2-session"
    until = stored_hold(core, f"operator: {OVER_LONG}")
    status = core.dispatch("daemon.status", {})
    assert shown_holds([*status["closures"], *status["lanes"][0]["closures"]]) == [SHOWN_WITHHELD] * 2
    text = cli.format_status(status)
    assert f"  codex-1 held by operator until {until}: {WITHHELD}" in text.splitlines()
    alert = conditions(*core.timers.snapshot()["lanes"])["codex-fleet-empty"]
    assert f"1 held by operator ({WITHHELD}; earliest end " in alert["body"]
    assert core._timer_notice(alert)
    notice = core.store.one("SELECT text FROM service_notices ORDER BY notice_id DESC LIMIT 1")["text"]
    assert notice == f"codex: no dispatchable lanes\n{alert['body']}"
    for shown in (json.dumps(status, sort_keys=True, default=str), text, alert["body"], notice):
        assert OVER_LONG not in shown and OVER_LONG[:160] not in shown
    assert not [item for item in read if OVER_LONG[:160] in item]            # withheld unread
