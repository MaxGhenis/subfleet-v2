"""C-10.3, C-10.9: the desktop app's account, from its usage file and the hook's reports.

Every value is synthetic: uuids built from small numbers and `example.com`
emails. No test reads the real usage file: `SUBFLEET_DESKTOP_USAGE` points into
the session's own folder (`home_paths.SAFE_HOME_PATHS`), and these tests pass
paths in their own temporary folder.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import alerts, capacity, desktop_app, home_paths, render
from subfleet.capacity import DesktopIdentity, build_view
from subfleet.desktop_app import AccountEvent, AppSample, resolve

LAWS = settings(max_examples=300, deadline=None, derandomize=True, database=None)
NOW = datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc)


def uuid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


ORGS = [uuid(0xA0 + n) for n in range(4)]
EMAILS = [f"user{n}@example.com" for n in range(4)]


def at(seconds: float) -> str:
    return desktop_app.iso(NOW + timedelta(seconds=seconds))


def event(event_id: int, org: str, email: str, seconds: float = 0, account: str | None = None) -> AccountEvent:
    return AccountEvent(event_id, org, email, account or uuid(0xC0 + event_id), "sess-1", at(seconds))


def sample(org: str, seconds: float = 0, weekly: float | None = 6.0) -> AppSample:
    return AppSample(org, at(seconds), weekly, at(seconds))


def claude(lane_id: str, email: str | None = None, **extra) -> dict:
    return {"lane_id": lane_id, "provider": "claude", "owner": "v2", "enabled": True, "desktop": False,
            "account_key": f"claude:{email}" if email else f"claude:{uuid(1)}:{uuid(2)}", **extra}


def codex(lane_id: str, email: str) -> dict:
    return {"lane_id": lane_id, "provider": "codex", "owner": "v2", "enabled": True, "desktop": False,
            "account_key": f"codex:{email}"}


def marked(lanes, app) -> dict[str, dict]:
    view = build_view([dict(lane) for lane in lanes], now=NOW,
                      desktop=DesktopIdentity("unverified", app=app))
    return {lane["lane_id"]: lane for lane in view["lanes"]}


# --- validation ----------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (uuid(1), uuid(1)), (uuid(1).upper(), uuid(1)), (" " + uuid(1), None), (uuid(1) + "\n", None),
    (uuid(1)[:-1], None), (uuid(1).replace("-", ""), None), ("{" + uuid(1) + "}", None), (None, None), (7, None),
])
def test_c10_9_only_a_canonical_uuid_is_accepted(value, expected):
    assert desktop_app.canonical_uuid(value) == expected


@pytest.mark.parametrize("value,ok", [
    ("user@example.com", True), ("a@b", True), ("x" * 242 + "@example.com", True),
    ("x" * 243 + "@example.com", False), ("user@@example.com", False), ("user@ex@ample.com", False),
    ("userexample.com", False), ("@example.com", False), ("user@", False), ("us er@example.com", False),
    ("user@example.com\n", False), ("user\x00@example.com", False), ("", False), (None, False), (3, False),
])
def test_c10_9_an_email_has_one_at_and_at_most_254_characters(value, ok):
    assert (desktop_app.valid_email(value) is not None) is ok


def test_c10_9_a_report_needs_every_value_and_normalises_them():
    report = desktop_app.account_report("User@Example.com", uuid(3).upper(), uuid(4), "sess-9", at(0))
    assert report == {"email": "User@Example.com", "account_uuid": uuid(3), "org_uuid": uuid(4),
                      "session_id": "sess-9", "observed_at": at(0)}
    for broken in ({"email": "nope"}, {"account_uuid": "x"}, {"org_uuid": None}, {"session_id": ""},
                   {"observed_at": "yesterday"}, {"observed_at": "2026-11-08T00:00:00"}):
        values = {"email": "u@example.com", "account_uuid": uuid(3), "org_uuid": uuid(4),
                  "session_id": "sess-9", "observed_at": at(0), **broken}
        assert desktop_app.account_report(**values) is None, broken


def test_c10_9_a_stamp_later_than_the_daemons_clock_is_taken_as_now():
    report = desktop_app.account_report("u@example.com", uuid(3), uuid(4), "s", at(3600), now=NOW)
    assert report["observed_at"] == desktop_app.iso(NOW)


# --- the usage file --------------------------------------------------------------

def usage(samples, version=2) -> bytes:
    return json.dumps({"version": version, "samples": samples}).encode()


def raw(org: str, t_ms: int, sd=None) -> dict:
    return {"t": t_ms, "org": org, "u": {} if sd is None else {"sd": sd}}


T0 = int(NOW.timestamp() * 1000)


def test_c10_9_the_newest_sample_names_the_org_its_time_and_weekly_percent():
    found = desktop_app.parse_app_usage(usage([
        raw(ORGS[0], T0 - 7_200_000, 100), raw(ORGS[1], T0 - 3_600_000, 4), raw(ORGS[1], T0, 6)]))
    assert found == AppSample(ORGS[1], at(0), 6.0, at(-3600))


def test_c10_9_a_sample_with_no_weekly_percent_still_names_the_org():
    found = desktop_app.parse_app_usage(usage([raw(ORGS[0], T0 - 1000, 50), raw(ORGS[2], T0)]))
    assert (found.org, found.weekly) == (ORGS[2], None)


@pytest.mark.parametrize("data", [
    usage([raw(ORGS[0], T0)], version=1), usage([raw(ORGS[0], T0)], version=3), usage([raw(ORGS[0], T0)], version="2"),
    usage([raw(ORGS[0], T0)], version=True), b"{", b"[]", b'{"version": 2}', b'{"version": 2, "samples": {}}',
])
def test_c10_9_another_version_or_a_parse_error_is_no_answer(data):
    with pytest.raises(ValueError):
        desktop_app.parse_app_usage(data)


def test_c10_9_malformed_samples_are_left_out():
    assert desktop_app.parse_app_usage(usage([{"t": "x", "org": ORGS[0]}, {"t": T0, "org": "nope"}, 5])) is None
    found = desktop_app.parse_app_usage(usage([{"t": True, "org": ORGS[3]}, raw(ORGS[0], T0 - 5), {"org": ORGS[1]}]))
    assert found.org == ORGS[0]


@LAWS
@given(st.lists(st.tuples(st.sampled_from(ORGS), st.integers(0, 20), st.one_of(st.none(), st.integers(0, 100))),
                min_size=1, max_size=12), st.randoms(use_true_random=False))
def test_c10_9_invariant_8_the_samples_order_never_matters(rows, rng):
    """Invariant 8, file half: shuffle the samples, and the answer is the same."""
    samples = [raw(org, T0 + step * 1000, sd) for org, step, sd in rows]
    first = desktop_app.parse_app_usage(usage(samples))
    rng.shuffle(samples)
    assert desktop_app.parse_app_usage(usage(samples)) == first


def test_c10_9_the_reader_logs_once_per_change_of_the_files_state(tmp_path):
    desktop_app.forget_app_usage()
    path, logs = tmp_path / "plan-usage-history.json", []
    assert desktop_app.read_app_usage(path, log=logs.append) is None           # missing
    assert desktop_app.read_app_usage(path, log=logs.append) is None
    assert len(logs) == 1 and "missing" in logs[0]
    path.write_bytes(usage([raw(ORGS[0], T0)], version=3))
    assert desktop_app.read_app_usage(path, log=logs.append) is None
    assert desktop_app.read_app_usage(path, log=logs.append) is None
    assert len(logs) == 2 and "ValueError" in logs[1]
    path.write_bytes(usage([raw(ORGS[0], T0, 9)]))
    assert desktop_app.read_app_usage(path, log=logs.append).org == ORGS[0]
    assert len(logs) == 2
    path.write_bytes(b"not json at all, and longer")
    assert desktop_app.read_app_usage(path, log=logs.append) is None
    assert len(logs) == 3


def test_c10_9_the_reader_caps_the_file_at_4_mib(tmp_path):
    desktop_app.forget_app_usage()
    path, logs = tmp_path / "big.json", []
    body = usage([raw(ORGS[0], T0)])
    path.write_bytes(body[:-1] + b" " * (desktop_app.APP_USAGE_LIMIT - len(body) + 1) + b"}")
    assert desktop_app.read_app_usage(path, log=logs.append) is None
    assert logs and "TooLarge" in logs[0]
    path.write_bytes(body[:-1] + b" " * (desktop_app.APP_USAGE_LIMIT - len(body)) + b"}")
    assert os.path.getsize(path) == desktop_app.APP_USAGE_LIMIT
    assert desktop_app.read_app_usage(path, log=logs.append).org == ORGS[0]


def test_c10_9_the_reader_never_opens_a_fifo(tmp_path):
    desktop_app.forget_app_usage()
    path, logs = tmp_path / "fifo.json", []
    os.mkfifo(path)
    assert desktop_app.read_app_usage(path, log=logs.append) is None
    assert logs


def test_c10_9_the_path_is_the_tests_own_and_injectable(tmp_path, monkeypatch):
    """Tests never read the real file: the session points it into its own folder."""
    assert home_paths.desktop_usage_path() == home_paths.Path(os.environ["SUBFLEET_DESKTOP_USAGE"])
    assert "SUBFLEET_DESKTOP_USAGE" in home_paths.SAFE_HOME_PATHS
    target = tmp_path / "usage.json"
    monkeypatch.setenv("SUBFLEET_DESKTOP_USAGE", str(target))
    target.write_bytes(usage([raw(ORGS[2], T0)]))
    desktop_app.forget_app_usage()
    # conftest blanks the default-path read; the resolved path is the session's own
    assert desktop_app.read_app_usage(desktop_app.app_usage_path()).org == ORGS[2]
    monkeypatch.delenv("SUBFLEET_DESKTOP_USAGE")
    with pytest.raises(RuntimeError, match="SUBFLEET_DESKTOP_USAGE"):
        home_paths.desktop_usage_path()


# --- C-10.3's rule ----------------------------------------------------------------

def test_c10_3_the_apps_org_email_decides_exactly_its_lanes():
    app = resolve(sample(ORGS[0]), [event(1, ORGS[0], "User0@Example.com"), event(2, ORGS[1], EMAILS[1])])
    lanes = marked([claude("claude-1", EMAILS[0]), claude("claude-2", EMAILS[1], desktop=True),
                    claude("claude-3", "USER0@example.com"), codex("codex-1", EMAILS[0]), claude("claude-4")], app)
    assert {lane_id for lane_id, lane in lanes.items() if lane.get("desktop")} == {"claude-1", "claude-3"}
    assert not any(lane.get("desktop_possible") for lane in lanes.values())


def test_c10_3_an_email_no_lane_has_marks_no_lane():
    app = resolve(sample(ORGS[0]), [event(1, ORGS[0], "stranger@example.com")])
    lanes = marked([claude("claude-1", EMAILS[0], desktop=True), claude("claude-2", EMAILS[1])], app)
    assert not any(lane.get("desktop") or lane.get("desktop_possible") for lane in lanes.values())


def test_c10_3_an_unlinked_org_marks_no_lane_desktop_and_every_lane_not_linked_elsewhere_possible():
    """Mutant 4's test: an unlinked org's lanes are `desktop_possible`, never `desktop`."""
    app = resolve(sample(ORGS[0]), [event(1, ORGS[1], EMAILS[1]), event(2, ORGS[2], EMAILS[2])])
    lanes = marked([claude("claude-1", EMAILS[0], desktop=True), claude("claude-2", EMAILS[1]),
                    claude("claude-3", EMAILS[2].upper()), claude("claude-4"), codex("codex-1", EMAILS[0])], app)
    assert not any(lane.get("desktop") for lane in lanes.values())
    # claude-4's key names its account and org (`claude:<account uuid>:<org uuid>`), and that
    # org is not the app's: it is linked elsewhere, so it is not possible (review r1, P1 2).
    assert {lane_id for lane_id, lane in lanes.items() if lane.get("desktop_possible")} == {"claude-1"}


def test_c10_3_an_org_is_linked_through_its_newest_event_not_its_oldest():
    """Mutant 6's test: the newest event naming the org gives its email."""
    events = [event(1, ORGS[0], EMAILS[0], seconds=-7200), event(2, ORGS[0], EMAILS[1], seconds=-60),
              event(3, ORGS[1], EMAILS[2], seconds=-30)]
    app = resolve(sample(ORGS[0]), events)
    assert app.email == EMAILS[1]
    lanes = marked([claude("claude-1", EMAILS[0]), claude("claude-2", EMAILS[1])], app)
    assert [lane_id for lane_id, lane in lanes.items() if lane.get("desktop")] == ["claude-2"]


def test_c10_3_an_email_linked_to_the_apps_org_too_is_not_elsewhere():
    app = resolve(sample(ORGS[0]), [event(1, ORGS[0], EMAILS[0], seconds=-90), event(2, ORGS[1], EMAILS[0])])
    assert app.email == EMAILS[0] and app.elsewhere == frozenset()


def test_c10_3_without_the_file_the_newest_event_names_the_org():
    app = resolve(None, [event(5, ORGS[0], EMAILS[0], seconds=-10), event(4, ORGS[1], EMAILS[1], seconds=-5)])
    assert (app.org, app.source, app.email) == (ORGS[1], "hook", EMAILS[1])


def test_c10_3_equal_times_break_by_event_id():
    events = [event(9, ORGS[1], EMAILS[1]), event(3, ORGS[0], EMAILS[0])]
    assert resolve(None, events).org == ORGS[1]
    assert resolve(None, list(reversed(events))).org == ORGS[1]


def test_c10_3_with_neither_signal_there_is_no_answer():
    assert resolve(None, []) is None


def test_c10_9_the_file_sets_the_org_whatever_a_newer_event_says():
    """Mutant 7's test: an event newer than the file never moves the app's org."""
    app = resolve(sample(ORGS[0], seconds=-3600), [event(1, ORGS[1], EMAILS[1], seconds=0)])
    assert (app.org, app.source, app.email) == (ORGS[0], "app-file", None)
    lanes = marked([claude("claude-1", EMAILS[0]), claude("claude-2", EMAILS[1])], app)
    assert not any(lane.get("desktop") for lane in lanes.values())
    assert {lane_id for lane_id, lane in lanes.items() if lane.get("desktop_possible")} == {"claude-1"}


# --- the generated laws -----------------------------------------------------------

def email_variants(email: str):
    return st.sampled_from([email, email.upper(), email.capitalize()])


@st.composite
def histories(draw):
    count = draw(st.integers(0, 8))
    ids = draw(st.lists(st.integers(1, 10_000), min_size=count, max_size=count, unique=True))
    events = []
    for event_id in ids:
        email = draw(st.sampled_from(EMAILS).flatmap(email_variants))
        events.append(event(event_id, draw(st.sampled_from(ORGS)), email,
                            seconds=draw(st.integers(-5, 5)) * 600, account=uuid(draw(st.integers(0, 3)))))
    file_org = draw(st.one_of(st.none(), st.sampled_from([*ORGS, uuid(0xEE)])))
    app_sample = sample(file_org, seconds=draw(st.integers(-5, 5)) * 600) if file_org else None
    lanes, accounts = [], {}
    for n in range(draw(st.integers(1, 7))):
        kind = draw(st.sampled_from(["email", "email", "stranger", "pair", "pair", "codex"]))
        recorded = draw(st.booleans())
        if kind == "email":
            email = draw(st.sampled_from(EMAILS).flatmap(email_variants))
            lanes.append(claude(f"claude-{n}", email, desktop=recorded))
            accounts[f"claude-{n}"] = ("email", email)
        elif kind == "stranger":
            lanes.append(claude(f"claude-{n}", f"stranger{n}@example.com", desktop=recorded))
            accounts[f"claude-{n}"] = ("email", f"stranger{n}@example.com")
        elif kind == "pair":
            # A lane enrolled with profile scope: its key is `claude:<account uuid>:<org uuid>` (C-1.4).
            account, org = uuid(draw(st.integers(0, 3))), draw(st.sampled_from([*ORGS, uuid(0xEE)]))
            if draw(st.booleans()):
                account, org = account.upper(), org.upper()
            lanes.append(claude(f"claude-{n}", None, desktop=recorded, account_key=f"claude:{account}:{org}"))
            accounts[f"claude-{n}"] = ("pair", account.lower(), org.lower())
        else:
            lanes.append(codex(f"codex-{n}", draw(st.sampled_from(EMAILS))))
    return app_sample, events, lanes, accounts


def expected_org(app_sample, events):
    """Independent of `desktop_app`: the file's org, else the newest event's by (time, id)."""
    if app_sample is not None:
        return app_sample.org
    return max(events, key=lambda e: (datetime.fromisoformat(e.observed_at.replace("Z", "+00:00")), e.event_id)).org


def expected_link(org, events):
    named = [e for e in events if e.org == org]
    if not named:
        return None
    return max(named, key=lambda e: (datetime.fromisoformat(e.observed_at.replace("Z", "+00:00")), e.event_id))


def expected_email(org, events):
    link = expected_link(org, events)
    return link.email if link else None


@LAWS
@given(histories())
def test_c10_3_invariant_1_and_2_the_apps_org_decides_and_never_guesses(history):
    """Invariant 1: with email E for the app's org, exactly the Claude lanes whose
    account is `claude:E` are `desktop`. Invariant 2: with no email, none is, and
    `desktop_possible` holds for exactly the Claude lanes not linked elsewhere."""
    app_sample, events, lanes, accounts = history
    app = resolve(app_sample, events)
    if app_sample is None and not events:
        assert app is None
        return
    org = expected_org(app_sample, events)
    link = expected_link(org, events)
    email, account = (link.email, link.account_uuid) if link else (None, None)
    assert (app.org, app.email) == (org, email)
    rows = marked(lanes, app)
    desktop = {lane_id for lane_id, lane in rows.items() if lane.get("desktop")}
    possible = {lane_id for lane_id, lane in rows.items() if lane.get("desktop_possible")}
    # From each lane's recorded account, never from `owns` (review r1, P1 2): an
    # email-keyed lane by its email, case aside; a verified lane by its org and,
    # when the newest event naming that org gives one, its account.
    by_email = {lane_id for lane_id, found in accounts.items()
                if found[0] == "email" and email is not None and found[1].lower() == email.lower()}
    by_key = {lane_id for lane_id, found in accounts.items()
              if found[0] == "pair" and found[2] == org and (account is None or found[1] == account)}
    assert desktop == by_email | by_key
    if email is not None:
        assert possible == set()
    else:
        mine = {e.email.lower() for e in events if e.org == org}
        other = {e.email.lower() for e in events if e.org != org}
        assert possible == {lane_id for lane_id, found in accounts.items()
                            if found[0] == "email" and found[1].lower() not in other - mine}
    assert not any(rows[lane["lane_id"]].get("desktop") or rows[lane["lane_id"]].get("desktop_possible")
                   for lane in lanes if lane["provider"] != "claude")


@LAWS
@given(histories(), st.sampled_from([*ORGS, uuid(0xEE)]), st.sampled_from(EMAILS), st.integers(-10, 100),
       st.integers(10_001, 20_000))
def test_c10_9_invariant_3_while_the_file_answers_no_event_moves_the_org(history, org, email, minutes, event_id):
    """Invariant 3: events only add links; the file's org stands whatever their time."""
    app_sample, events, _, _ = history
    if app_sample is None:
        app_sample = sample(ORGS[0])
    before = resolve(app_sample, events)
    after = resolve(app_sample, [*events, event(event_id, org, email, seconds=minutes * 60)])
    assert before.org == after.org == app_sample.org
    if org == app_sample.org:
        assert after.email is not None


@LAWS
@given(histories(), st.randoms(use_true_random=False))
def test_c10_9_invariant_8_shuffled_events_decide_the_same(history, rng):
    """Invariant 8: shuffle the events (and the lanes); the decision does not change."""
    app_sample, events, lanes, _ = history
    first = resolve(app_sample, events)
    shuffled = list(events)
    rng.shuffle(shuffled)
    again = resolve(app_sample, shuffled)
    assert again == first
    if first is not None:
        rows = marked(lanes, first)
        reordered = list(lanes)
        rng.shuffle(reordered)
        rows_again = marked(reordered, again)
        assert {k: (v.get("desktop"), v.get("desktop_possible")) for k, v in rows.items()} == \
               {k: (v.get("desktop"), v.get("desktop_possible")) for k, v in rows_again.items()}


# --- recording, alerts and the status line ------------------------------------------

def test_c10_9_a_report_is_recorded_when_it_differs_or_the_newest_is_an_hour_old():
    report = desktop_app.account_report(EMAILS[0], uuid(1), ORGS[0], "s", at(0))
    assert desktop_app.should_record(report, [], now=NOW)
    same = AccountEvent(1, ORGS[0], EMAILS[0].upper(), uuid(1), "s", at(-60))
    assert not desktop_app.should_record(report, [same], now=NOW)
    old = AccountEvent(1, ORGS[0], EMAILS[0], uuid(1), "s", at(-3601))
    assert desktop_app.should_record(report, [old], now=NOW)
    for other in (AccountEvent(1, ORGS[1], EMAILS[0], uuid(1), "s", at(-60)),
                  AccountEvent(1, ORGS[0], EMAILS[1], uuid(1), "s", at(-60)),
                  AccountEvent(1, ORGS[0], EMAILS[0], uuid(2), "s", at(-60))):
        assert desktop_app.should_record(report, [other], now=NOW)
    # Against the newest only: an older same-account event does not hold back a switch back.
    assert desktop_app.should_record(report, [same, AccountEvent(2, ORGS[1], EMAILS[1], uuid(2), "s", at(-30))], now=NOW)


def test_c10_9_audit_rows_and_malformed_rows_are_not_events():
    rows = [{"event_id": 1, "data_json": "{}"}, {"event_id": 2, "data_json": "not json"},
            {"event_id": 3, "data_json": json.dumps({"email": "x", "org_uuid": ORGS[0]})},
            {"event_id": 4, "data_json": json.dumps({"email": EMAILS[0], "account_uuid": uuid(1), "org_uuid": ORGS[0],
                                                     "session_id": "s", "observed_at": at(0), "source": "hook"})}]
    assert [e.event_id for e in desktop_app.account_events(rows)] == [4]


def summary_of(app, lanes=(), in_use=True):
    return {**app.summary(lanes), "in_use": in_use}


def test_c10_9_alert_the_apps_org_unlinked_for_ten_minutes_while_in_use():
    app = resolve(AppSample(ORGS[0], at(-30), 5.0, at(-601)), [])
    found = desktop_app.alert_conditions(summary_of(app), in_use=True, now=NOW)
    assert [row["key"] for row in found] == [f"desktop-app-unlinked:{ORGS[0][:8]}"]
    assert all(row["severity"] == "warn" for row in found)
    assert desktop_app.alert_conditions(summary_of(app), in_use=None, now=NOW)           # unknown is use
    assert desktop_app.alert_conditions(summary_of(app), in_use=False, now=NOW) == []
    recent = resolve(AppSample(ORGS[0], at(-30), 5.0, at(-599)), [])
    assert desktop_app.alert_conditions(summary_of(recent), in_use=True, now=NOW) == []
    linked = resolve(AppSample(ORGS[0], at(-30), 5.0, at(-601)), [event(1, ORGS[0], EMAILS[0], seconds=-900)])
    assert desktop_app.alert_conditions(summary_of(linked), in_use=True, now=NOW) == []


def test_c10_9_no_unlinked_alert_while_a_lane_key_names_the_apps_org():
    """Review r1 of #318 (P3): on the file alone a uuid-keyed lane in the app's org is
    `desktop`, so the org is not unlinked from a lane and the alert stays quiet; a
    uuid-keyed lane in another org does not quiet it. Written after the fix (fix round 1)."""
    app = resolve(AppSample(ORGS[0], at(-30), 5.0, at(-601)), [])
    mine = [verified("claude-9", uuid(9), ORGS[0]), claude("claude-1", EMAILS[0])]
    assert app.summary(mine)["lanes"] == ["claude-9"]
    assert desktop_app.alert_conditions(summary_of(app, mine), in_use=True, now=NOW) == []
    other = [verified("claude-9", uuid(9), ORGS[1]), claude("claude-1", EMAILS[0])]
    keys = [row["key"] for row in desktop_app.alert_conditions(summary_of(app, other), in_use=True, now=NOW)]
    assert keys == [f"desktop-app-unlinked:{ORGS[0][:8]}"]


def test_c10_9_alert_a_newer_event_naming_another_org_for_ten_minutes():
    events = [event(1, ORGS[1], EMAILS[1], seconds=-700), event(2, ORGS[1], EMAILS[1], seconds=-100)]
    app = resolve(AppSample(ORGS[0], at(-800), 5.0, at(-800)), [event(0, ORGS[0], EMAILS[0], seconds=-900), *events])
    assert app.disagree_since == at(-700)
    keys = [row["key"] for row in desktop_app.alert_conditions(summary_of(app, in_use=False), in_use=False, now=NOW)]
    assert keys == [f"desktop-app-disagree:{ORGS[1][:8]}"]
    # Not yet ten minutes, not newer than the file, or agreeing with it: no alert.
    for sample_at, onset in ((-800, -500), (-50, -700)):
        later = resolve(AppSample(ORGS[0], at(sample_at), 5.0, at(sample_at)),
                        [event(1, ORGS[1], EMAILS[1], seconds=onset), event(2, ORGS[1], EMAILS[1], seconds=-10)])
        assert desktop_app.alert_conditions(summary_of(later, in_use=False), in_use=False, now=NOW) == []
    agree = resolve(AppSample(ORGS[0], at(-800), 5.0, at(-800)), [event(1, ORGS[1], EMAILS[1], seconds=-700),
                                                               event(2, ORGS[0], EMAILS[0], seconds=-100)])
    assert agree.disagree_since is None


def test_c10_9_the_alerts_reach_the_conditions_the_timers_judge():
    app = resolve(AppSample(ORGS[0], at(-30), 5.0, at(-900)), [])
    snapshot = {"now": desktop_app.iso(NOW), "lanes": [], "desktop_account": summary_of(app, in_use=True)}
    keys = {row["key"] for row in alerts.evaluate_conditions(snapshot, now=NOW)}
    assert f"desktop-app-unlinked:{ORGS[0][:8]}" in keys
    assert not any(key.startswith("desktop-app") for key in
                   (row["key"] for row in alerts.evaluate_conditions({"now": desktop_app.iso(NOW), "lanes": []}, now=NOW)))


def test_c10_9_the_status_line_names_the_org_its_lanes_and_each_signal():
    lanes = [claude("claude-1", EMAILS[0]), claude("claude-9", EMAILS[0].upper()), claude("claude-2", EMAILS[1])]
    app = resolve(AppSample(ORGS[0], at(-60), 6.0, at(-60)), [event(1, ORGS[0], EMAILS[0], seconds=-30)])
    line = render.desktop_account_line(app.summary(lanes))
    assert line.startswith(f"desktop app: org {ORGS[0][:8]} (app file) on claude-1, claude-9;")
    assert f"app file {at(-60)} (weekly 6%)" in line and f"hook {at(-30)}" in line
    assert EMAILS[0] not in line and ORGS[0] not in line
    nolane = resolve(None, [event(1, ORGS[2], "stranger@example.com")])
    assert "(hook) no lane; hook" in render.desktop_account_line(nolane.summary(lanes))
    unlinked = resolve(AppSample(ORGS[3], at(-60), None, at(-60)), [event(1, ORGS[0], EMAILS[0])])
    text = render.desktop_account_line(unlinked.summary(lanes))
    assert "not linked yet" in text and f"org {ORGS[0][:8]}" in text and "weekly" not in text


def test_c10_9_desktop_identity_keeps_its_profile_rule_without_the_app():
    """With no app answer the identity is the one C-10.3 always built."""
    plain = capacity.desktop_identity(None, cached_label=EMAILS[0])
    assert plain.app is None and plain.decisive and plain.owns(claude("claude-1", EMAILS[0]))
    assert not plain.possible(claude("claude-1", EMAILS[0]))


def test_c10_9_while_the_app_answers_the_hint_decides_nothing():
    """Mutant 3's unit test: the `~/.claude.json` hint names claude-1, the app claude-2."""
    app = resolve(sample(ORGS[0]), [event(1, ORGS[0], EMAILS[1])])
    identity = capacity.desktop_identity(None, cached_label=EMAILS[0], last_label=EMAILS[0], app=app)
    lanes = [claude("claude-1", EMAILS[0], label=EMAILS[0]), claude("claude-2", EMAILS[1])]
    view = build_view(lanes, now=NOW, desktop=identity)
    assert {lane["lane_id"]: lane["desktop"] for lane in view["lanes"]} == {"claude-1": False, "claude-2": True}


# --- fix round 1 of review r1: no number in the usage file raises (ruling 1) ----------

#: Each number the parser reads (`version`, a sample's `t`, `u.sd`), written as
#: JSON text: an integer too large for a float, what Python's `json` reads as
#: NaN and the infinities, a bool and a string.
ODD_NUMBERS = {"10**400": "1" + "0" * 400, "NaN": "NaN", "Infinity": "Infinity", "-Infinity": "-Infinity",
               "true": "true", "string": '"7"'}


def usage_text(*, version: str = "2", t: str = str(T0), sd: str = "7") -> bytes:
    return ('{"version": %s, "samples": [{"t": %s, "org": "%s", "u": {"sd": %s}}]}'
            % (version, t, ORGS[0], sd)).encode()


def counting_parser(monkeypatch) -> list:
    parses, real = [], desktop_app.parse_app_usage
    monkeypatch.setattr(desktop_app, "parse_app_usage",
                        lambda data, **kwargs: parses.append(1) or real(data, **kwargs))
    return parses


@pytest.mark.parametrize("place", ["u.sd", "t", "version"])
@pytest.mark.parametrize("name", sorted(ODD_NUMBERS))
def test_c10_9_no_number_in_the_usage_file_raises_out_of_the_reader(tmp_path, monkeypatch, place, name):
    """Review r1, P1 1: `u.sd` = 10**400 raised `OverflowError` out of `read_app_usage`.
    A `u.sd` that is no finite number reads as weekly None; a sample without a
    usable `t` is left out; a `version` other than 2 is no answer. A second
    call on the unchanged file neither parses it nor logs again."""
    desktop_app.forget_app_usage()
    value = ODD_NUMBERS[name]
    path, logs = tmp_path / "plan-usage-history.json", []
    path.write_bytes(usage_text(**{{"u.sd": "sd"}.get(place, place): value}))
    parses = counting_parser(monkeypatch)
    found = desktop_app.read_app_usage(path, log=logs.append)
    if place == "u.sd":
        assert found == AppSample(ORGS[0], at(0), None, at(0))
        assert logs == []
    else:
        assert found is None
        assert len(logs) == 1
    assert desktop_app.read_app_usage(path, log=logs.append) == found
    assert len(parses) == 1 and len(logs) == (0 if place == "u.sd" else 1)
    desktop_app.forget_app_usage()


@pytest.mark.parametrize("error", [TypeError("x"), ArithmeticError("x"), KeyError("x"), MemoryError()])
def test_c10_9_any_failure_in_the_parse_is_no_answer_recorded_and_logged_once(tmp_path, monkeypatch, error):
    """Ruling 1: anything else that fails in the parse makes the file no answer,
    recorded for that state of the file and logged once."""
    desktop_app.forget_app_usage()
    path, logs, calls = tmp_path / "plan-usage-history.json", [], []
    path.write_bytes(usage([raw(ORGS[0], T0, 7)]))

    def broken(data, **kwargs):
        calls.append(1)
        raise error

    monkeypatch.setattr(desktop_app, "parse_app_usage", broken)
    assert desktop_app.read_app_usage(path, log=logs.append) is None
    assert desktop_app.read_app_usage(path, log=logs.append) is None
    assert len(calls) == 1 and len(logs) == 1 and type(error).__name__ in logs[0]
    desktop_app.forget_app_usage()


JSON_VALUES = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.sampled_from([10**400, -10**400]),
              st.floats(allow_nan=True, allow_infinity=True), st.text(max_size=6)),
    lambda inner: st.one_of(st.lists(inner, max_size=3), st.dictionaries(st.text(max_size=3), inner, max_size=3)),
    max_leaves=8)
PLACES = ["version", "samples", "t", "org", "u", "u.sd"]


@settings(max_examples=400, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(place=st.sampled_from(PLACES), value=JSON_VALUES)
@example(place="u.sd", value=10**400)
@example(place="u.sd", value=-10**400)
@example(place="t", value=10**400)
@example(place="version", value=10**400)
@example(place="u.sd", value=float("nan"))
def test_c10_9_any_json_value_anywhere_gives_a_sample_or_none(tmp_path, place, value):
    """Ruling 1's property: any JSON value at `version`, at `samples`, at a
    sample's `t`, `org` or `u`, or at `u.sd`, written with `json.dumps`, gives
    an `AppSample` or None from `read_app_usage`, never an exception."""
    document = {"version": 2, "samples": [{"t": T0, "org": ORGS[0], "u": {"sd": 7}}]}
    first = document["samples"][0]
    if place in ("version", "samples"):
        document[place] = value
    elif place == "u.sd":
        first["u"]["sd"] = value
    else:
        first[place] = value
    path = tmp_path / "plan-usage-history.json"
    path.write_text(json.dumps(document))
    desktop_app.forget_app_usage()
    found = desktop_app.read_app_usage(path)
    assert found is None or isinstance(found, AppSample)
    desktop_app.forget_app_usage()


# --- fix round 1: a lane matches the app's account by what its kind records (ruling 2) --

def verified(lane_id: str, account: str, org: str, **extra) -> dict:
    """A lane enrolled with profile scope (`adapters/claude.py`, C-1.4): its key is the identity."""
    return {"lane_id": lane_id, "provider": "claude", "owner": "v2", "enabled": True, "desktop": False,
            "account_key": f"claude:{account}:{org}", "identity": f"{account}:{org}", **extra}


def flags(lanes) -> tuple[set, set]:
    return ({lane_id for lane_id, lane in lanes.items() if lane.get("desktop")},
            {lane_id for lane_id, lane in lanes.items() if lane.get("desktop_possible")})


def test_c10_3_a_verified_lane_is_the_apps_by_its_account_and_org():
    """Review r1, P1 2: a lane keyed `claude:<account uuid>:<org uuid>` is the app's
    when its org is the app's and its account is the newest event's for that org.
    One account under both key forms is `desktop` under both; another account in
    the org is not, nor is the same account in another org."""
    acct = uuid(0xC1)
    app = resolve(sample(ORGS[0]), [event(1, ORGS[0], EMAILS[0], account=acct)])
    lanes = marked([verified("claude-9", acct, ORGS[0]), verified("claude-8", uuid(0xC8), ORGS[0]),
                    claude("claude-1", EMAILS[0]), verified("claude-7", acct, ORGS[1]),
                    verified("claude-6", acct.upper(), ORGS[0].upper())], app)
    assert flags(lanes) == ({"claude-9", "claude-1", "claude-6"}, set())


def test_c10_3_with_the_file_alone_a_verified_lane_in_the_apps_org_is_desktop():
    """No event names the app's org, so its account is unknown: a verified lane
    whose key names that org is `desktop`; an email-keyed lane not linked
    elsewhere stays `desktop_possible`; a verified lane in another org is linked
    elsewhere and is neither."""
    app = resolve(sample(ORGS[0]), [event(1, ORGS[1], EMAILS[1])])
    lanes = marked([verified("claude-9", uuid(0xC1), ORGS[0]), verified("claude-8", uuid(0xC8), ORGS[1]),
                    claude("claude-1", EMAILS[0]), claude("claude-2", EMAILS[1])], app)
    assert flags(lanes) == ({"claude-9"}, {"claude-1"})


def test_c10_3_a_verified_lane_in_another_org_is_never_possible():
    """Review r1, P1 2's second effect: `lane_email` read no email from a uuid key,
    so a verified lane in another org got `desktop_possible`."""
    app_org, other_org, elsewhere_acct = uuid(0xAC), uuid(0xAA), uuid(0xE1)
    lane = {"provider": "claude", "account_key": f"claude:{elsewhere_acct}:{other_org}",
            "label": "elsewhere@example.com", "identity": f"{elsewhere_acct}:{other_org}"}
    desktop = capacity.DesktopIdentity("unverified", app=resolve(sample(app_org), [
        AccountEvent(1, other_org, "elsewhere@example.com", elsewhere_acct, "s1", at(0))]))
    row = capacity.mark_desktop(dict(lane), desktop=desktop)
    assert row["desktop"] is False and "desktop_possible" not in row


def test_c10_3_a_codex_lane_is_never_desktop_or_possible():
    """Only a Claude lane is the desktop app's, whatever its key holds."""
    acct = uuid(0xC1)
    for app in (resolve(sample(ORGS[0]), [event(1, ORGS[0], "app@example.com", account=acct)]),
                resolve(sample(ORGS[0]), [])):
        for lane in ({"lane_id": "codex-1", "provider": "codex", "account_key": "codex:app@example.com",
                      "desktop": False},
                     {"lane_id": "codex-2", "provider": "codex", "account_key": f"codex:{acct}:{ORGS[0]}",
                      "identity": f"{acct}:{ORGS[0]}", "desktop": False}):
            row = capacity.mark_desktop(dict(lane), desktop=DesktopIdentity("unverified", app=app))
            assert not row.get("desktop") and not row.get("desktop_possible")
            assert not app.owns(lane) and not app.possible(lane)


def test_c10_3_owns_and_elsewhere_read_one_key_parser():
    """Review r1, P3 (valid_email): `owns` kept a trailing space that `lane_email`
    stripped, so `claude:app@example.com ` was matched for `elsewhere` but never `desktop`."""
    app = resolve(sample(ORGS[0]), [event(1, ORGS[0], EMAILS[0])])
    assert flags(marked([claude("claude-1", EMAILS[0] + " ")], app)) == ({"claude-1"}, set())
    unlinked = resolve(sample(ORGS[0]), [event(1, ORGS[1], EMAILS[0])])
    assert flags(marked([claude("claude-1", EMAILS[0] + " ")], unlinked)) == (set(), set())


def test_c10_3_case_aside_is_lower_case_not_casefold():
    """Review r1, P3 (casefold): `casefold` joined distinct addresses, so an event
    for `Straße@example.com` made `claude:strasse@example.com` the desktop lane."""
    app = resolve(sample(ORGS[0]), [event(1, ORGS[0], "Straße@example.com")])
    lanes = marked([claude("claude-1", "strasse@example.com"), claude("claude-2", "STRASSE@example.com"),
                    claude("claude-3", "STRAßE@example.com")], app)
    assert flags(lanes) == ({"claude-3"}, set())
    report = desktop_app.account_report("Straße@example.com", uuid(1), ORGS[0], "s", at(0))
    assert desktop_app.should_record(report, [AccountEvent(1, ORGS[0], "strasse@example.com", uuid(1), "s", at(-60))],
                                     now=NOW)


# --- fix round 1: the P3s ---------------------------------------------------------

@pytest.mark.parametrize("stamp", ["9999-12-31T23:59:59-01:00", "0001-01-01T00:00:00+01:00"])
def test_c10_9_a_stamp_past_the_calendar_is_malformed(stamp):
    """Review r1, P3 (malformed reports): `astimezone` sat outside the `try`."""
    assert desktop_app.account_report(EMAILS[0], uuid(1), ORGS[0], "s", stamp, now=NOW) is None


@pytest.mark.parametrize("value", [
    "a\u009bb@example.com", "a‮b@example.com", "a​b@example.com", "a﻿b@example.com",
    "a＠b@example.com", "a﹫b@example.com", "Kate@example.com", "a\udcff@example.com",
    "a b@example.com", "a\u0085b@example.com",
])
def test_c10_9_an_email_is_refused_when_nfkc_changes_it_or_it_holds_a_control_format_or_space(value):
    """Review r1, P3 (valid_email): C-10.9's excluded characters, with NFKC. A
    lone surrogate (`os.environ` for a non-UTF-8 byte) is refused too, so the
    store never meets it."""
    assert desktop_app.valid_email(value) is None


@pytest.mark.parametrize("value", ["user@example.com", "First.Last+tag@example.org", "Straße@example.com"])
def test_c10_9_a_plain_address_still_passes(value):
    assert desktop_app.valid_email(value) == value


def test_c10_9_the_hook_drops_a_surrogate_email():
    env = {desktop_app.ENTRYPOINT_NAME: desktop_app.DESKTOP_ENTRYPOINT,
           "CLAUDE_CODE_USER_EMAIL": "a\udcff@example.com", "CLAUDE_CODE_ACCOUNT_UUID": uuid(1),
           "CLAUDE_CODE_ORGANIZATION_UUID": uuid(2)}
    assert desktop_app.report_from_environment(env, "sess-1", now=NOW) is None
    env["CLAUDE_CODE_USER_EMAIL"] = "a@example.com"
    assert desktop_app.report_from_environment(env, "sess-1", now=NOW)["email"] == "a@example.com"


def test_c10_9_status_carries_the_org_prefix_only():
    """Review r1, P3 (full org uuid): `daemon.status`, the timers snapshot and
    `status.json` carried `desktop_account.org`."""
    app = resolve(sample(ORGS[0]), [event(1, ORGS[0], EMAILS[0])])
    summary = app.summary([claude("claude-1", EMAILS[0])])
    assert "org" not in summary and summary["org_prefix"] == ORGS[0][:8]
    assert ORGS[0] not in json.dumps(summary)


def test_c10_9_only_an_integer_version_2_is_read():
    """Review r1, P3 (parser looseness): `"version": 2.0` was read."""
    with pytest.raises(ValueError):
        desktop_app.parse_app_usage(b'{"version": 2.0, "samples": [{"t": %d, "org": "%s"}]}' % (T0, ORGS[0].encode()))


@pytest.mark.parametrize("sd", [-5, 250, 100.5])
def test_c10_9_a_weekly_percent_outside_0_to_100_is_none(sd):
    found = desktop_app.parse_app_usage(usage([raw(ORGS[0], T0, sd)]))
    assert (found.org, found.weekly) == (ORGS[0], None)


def test_c10_9_a_sample_from_the_future_is_left_out(tmp_path):
    """Review r1, P3 (parser looseness): a sample ten years ahead pinned the app's
    org and silenced both alerts."""
    desktop_app.forget_app_usage()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    path = tmp_path / "plan-usage-history.json"
    path.write_bytes(usage([raw(ORGS[1], now_ms + 10 * 365 * 86_400_000), raw(ORGS[0], now_ms - 60_000)]))
    found = desktop_app.read_app_usage(path)
    assert found.org == ORGS[0]
    later = desktop_app.iso(datetime.now(timezone.utc) + timedelta(seconds=-1))
    app = resolve(found, [AccountEvent(1, ORGS[2], EMAILS[2], uuid(1), "s", later)])
    assert app.disagree_since == later
    desktop_app.forget_app_usage()
