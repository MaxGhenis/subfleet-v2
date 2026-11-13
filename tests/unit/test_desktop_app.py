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
from hypothesis import given, settings, strategies as st

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
    assert desktop_app.read_app_usage().org == ORGS[2]
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
    assert {lane_id for lane_id, lane in lanes.items() if lane.get("desktop_possible")} == {"claude-1", "claude-4"}


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
    lanes = []
    for n in range(draw(st.integers(1, 7))):
        kind = draw(st.sampled_from(["email", "email", "stranger", "pair", "codex"]))
        recorded = draw(st.booleans())
        if kind == "email":
            lanes.append(claude(f"claude-{n}", draw(st.sampled_from(EMAILS).flatmap(email_variants)), desktop=recorded))
        elif kind == "stranger":
            lanes.append(claude(f"claude-{n}", f"stranger{n}@example.com", desktop=recorded))
        elif kind == "pair":
            lanes.append(claude(f"claude-{n}", None, desktop=recorded))
        else:
            lanes.append(codex(f"codex-{n}", draw(st.sampled_from(EMAILS))))
    return app_sample, events, lanes


def expected_org(app_sample, events):
    """Independent of `desktop_app`: the file's org, else the newest event's by (time, id)."""
    if app_sample is not None:
        return app_sample.org
    return max(events, key=lambda e: (datetime.fromisoformat(e.observed_at.replace("Z", "+00:00")), e.event_id)).org


def expected_email(org, events):
    named = [e for e in events if e.org == org]
    if not named:
        return None
    return max(named, key=lambda e: (datetime.fromisoformat(e.observed_at.replace("Z", "+00:00")), e.event_id)).email


@LAWS
@given(histories())
def test_c10_3_invariant_1_and_2_the_apps_org_decides_and_never_guesses(history):
    """Invariant 1: with email E for the app's org, exactly the Claude lanes whose
    account is `claude:E` are `desktop`. Invariant 2: with no email, none is, and
    `desktop_possible` holds for exactly the Claude lanes not linked elsewhere."""
    app_sample, events, lanes = history
    app = resolve(app_sample, events)
    if app_sample is None and not events:
        assert app is None
        return
    org = expected_org(app_sample, events)
    email = expected_email(org, events)
    assert (app.org, app.email) == (org, email)
    rows = marked(lanes, app)
    claude_lanes = [lane for lane in lanes if lane["provider"] == "claude"]
    desktop = {lane_id for lane_id, lane in rows.items() if lane.get("desktop")}
    possible = {lane_id for lane_id, lane in rows.items() if lane.get("desktop_possible")}
    if email is not None:
        assert desktop == {lane["lane_id"] for lane in claude_lanes
                           if lane["account_key"].casefold() == f"claude:{email}".casefold()}
        assert possible == set()
    else:
        mine = {e.email.casefold() for e in events if e.org == org}
        other = {e.email.casefold() for e in events if e.org != org}
        assert desktop == set()
        assert possible == {lane["lane_id"] for lane in claude_lanes
                            if lane["account_key"].partition(":")[2].casefold() not in other - mine}
    assert not any(rows[lane["lane_id"]].get("desktop") or rows[lane["lane_id"]].get("desktop_possible")
                   for lane in lanes if lane["provider"] != "claude")


@LAWS
@given(histories(), st.sampled_from([*ORGS, uuid(0xEE)]), st.sampled_from(EMAILS), st.integers(-10, 100),
       st.integers(10_001, 20_000))
def test_c10_9_invariant_3_while_the_file_answers_no_event_moves_the_org(history, org, email, minutes, event_id):
    """Invariant 3: events only add links; the file's org stands whatever their time."""
    app_sample, events, _ = history
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
    app_sample, events, lanes = history
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
