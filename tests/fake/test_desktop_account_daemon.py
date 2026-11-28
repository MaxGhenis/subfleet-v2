"""C-10.3, C-10.9: the daemon records a desktop session's report and routes by the app's account.

No provider and no process is started: the daemon is the in-process fixture of
`tests/fake/test_state_contract.py`. The usage file and `~/.claude.json` stand-in
live in the test's own folder (`SUBFLEET_DESKTOP_USAGE`, `SUBFLEET_DESKTOP_LOGIN`);
every value is synthetic.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from subfleet import desktop_app, doctor, protocol
from subfleet.contracts import Credential
from tests.fake.test_state_contract import state_daemon  # noqa: F401 - the fixture

EMAILS = ["hint@example.com", "app@example.com", "other@example.com"]


def uuid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


ORG_APP, ORG_OTHER = uuid(0xA1), uuid(0xA2)


def now_iso(seconds: float = 0) -> str:
    return desktop_app.iso(datetime.now(timezone.utc) + timedelta(seconds=seconds))


@pytest.fixture
def desktop(state_daemon, tmp_path, monkeypatch):  # noqa: F811 - the fixture above
    daemon, harness = state_daemon
    daemon.desktop_prober = lambda: None
    login, usage = tmp_path / "claude.json", tmp_path / "claude-app" / "plan-usage-history.json"
    usage.parent.mkdir()
    login.write_text(json.dumps({"oauthAccount": {"emailAddress": EMAILS[0]}}))
    monkeypatch.setenv("SUBFLEET_DESKTOP_LOGIN", str(login))
    monkeypatch.setenv("SUBFLEET_DESKTOP_USAGE", str(usage))
    # The session blanks the default login read (`tests/conftest.py`, no_desktop_login);
    # this test means its own stand-in, so it names that file.
    from subfleet import capacity
    named = capacity.read_desktop_account
    monkeypatch.setattr("subfleet.capacity.read_desktop_account", lambda path=None: named(path or login))
    named_usage = desktop_app.read_app_usage          # conftest blanks only the default path
    monkeypatch.setattr("subfleet.desktop_app.read_app_usage",
                        lambda path=None, **kwargs: named_usage(path or usage, **kwargs))
    capacity.forget_desktop_account()
    desktop_app.forget_app_usage()
    original = daemon.store.get_lane("codex-1")
    for number, email in enumerate(EMAILS, start=1):
        daemon.store.put_lane(replace(original, lane_id=f"claude-{number}", provider="claude",
                                      account_key=f"claude:{email}", label=email,
                                      credential=Credential("claude", f"claude-quota-{email}", "keychain-token")))
    yield daemon, harness, usage
    desktop_app.forget_app_usage()


def write_usage(path, org, *, seconds=0, sd=7):
    stamp = int((datetime.now(timezone.utc) + timedelta(seconds=seconds)).timestamp() * 1000)
    path.write_text(json.dumps({"version": 2, "samples": [{"t": stamp - 60_000, "org": ORG_OTHER, "u": {"sd": 100}},
                                                          {"t": stamp, "org": org, "u": {"sd": sd}}]}))
    desktop_app.forget_app_usage()


def report(email=EMAILS[1], org=ORG_APP, account=uuid(0xC1), session="sess-app", seconds=0):
    return {"email": email, "account_uuid": account, "org_uuid": org, "session_id": session,
            "observed_at": now_iso(seconds)}


def events(daemon):
    return [json.loads(row["data_json"]) for row in daemon.store.query(
        "SELECT data_json FROM events WHERE kind=? AND data_json LIKE '%org_uuid%' ORDER BY event_id",
        (desktop_app.DESKTOP_ACCOUNT_EVENT,))]


def desktop_lanes(daemon):
    view = daemon._capacity_view(daemon._desktop_identity())
    return ({lane["lane_id"] for lane in view["lanes"] if lane.get("desktop")},
            {lane["lane_id"] for lane in view["lanes"] if lane.get("desktop_possible")})


def test_c10_9_a_report_is_recorded_once_and_again_on_a_change(desktop):
    daemon, _, _ = desktop
    assert daemon.dispatch(desktop_app.REPORT_OP, report()) == {"recorded": True}
    assert daemon.dispatch(desktop_app.REPORT_OP, report(session="sess-other")) == {"recorded": False}
    assert daemon.dispatch(desktop_app.REPORT_OP, report(email=EMAILS[2], org=ORG_OTHER)) == {"recorded": True}
    assert daemon.dispatch(desktop_app.REPORT_OP, report()) == {"recorded": True}     # a switch back
    found = events(daemon)
    assert [(row["org_uuid"], row["email"], row["source"]) for row in found] == [
        (ORG_APP, EMAILS[1], "hook"), (ORG_OTHER, EMAILS[2], "hook"), (ORG_APP, EMAILS[1], "hook")]
    assert set(found[0]) == {"email", "account_uuid", "org_uuid", "session_id", "observed_at", "source"}


def test_c10_9_the_same_report_is_written_again_only_after_an_hour(desktop):
    daemon, _, _ = desktop
    daemon.store.add_event(desktop_app.DESKTOP_ACCOUNT_EVENT, data={**report(seconds=-3700), "source": "hook"})
    assert daemon.dispatch(desktop_app.REPORT_OP, report()) == {"recorded": True}
    assert daemon.dispatch(desktop_app.REPORT_OP, report()) == {"recorded": False}
    assert len(events(daemon)) == 2


@pytest.mark.parametrize("broken", [{"email": "no-at"}, {"org_uuid": "x"}, {"account_uuid": None},
                                    {"session_id": ""}, {"observed_at": "soon"}])
def test_c10_9_a_malformed_report_is_refused_and_writes_nothing(desktop, broken):
    daemon, _, _ = desktop
    with pytest.raises(protocol.ProtocolError) as refused:
        daemon.dispatch(desktop_app.REPORT_OP, {**report(), **broken})
    assert refused.value.reason == "invalid-args"
    assert events(daemon) == []


def test_c10_9_without_the_signals_routing_is_the_hints(desktop):
    """Invariant 6 at the daemon: no usage file and no event, no app answer, and
    the `~/.claude.json` hint marks its lane as release/217 did."""
    daemon, _, _ = desktop
    assert daemon._desktop_identity().app is None and daemon._cached_desktop_identity().app is None
    assert desktop_lanes(daemon) == ({"claude-1"}, set())


def test_c10_9_while_the_signals_answer_the_hint_decides_nothing(desktop):
    """Mutant 3's test: the hint names claude-1; the app's file names an org a
    session linked to app@example.com, so claude-2 is the desktop lane."""
    daemon, _, usage = desktop
    daemon.dispatch(desktop_app.REPORT_OP, report())
    assert desktop_lanes(daemon) == ({"claude-2"}, set())        # the hook alone answers
    write_usage(usage, ORG_APP)
    assert desktop_lanes(daemon) == ({"claude-2"}, set())
    assert daemon._cached_desktop_identity().owns({"account_key": f"claude:{EMAILS[1]}"})
    lanes = daemon.dispatch("lanes", {})["lanes"]
    assert {lane["lane_id"] for lane in lanes if lane.get("desktop")} == {"claude-2"}


def test_c10_9_an_org_the_file_names_and_no_event_links_makes_lanes_possible(desktop):
    daemon, _, usage = desktop
    daemon.dispatch(desktop_app.REPORT_OP, report(email=EMAILS[2], org=ORG_OTHER))
    write_usage(usage, ORG_APP)
    assert desktop_lanes(daemon) == (set(), {"claude-1", "claude-2"})
    daemon.dispatch(desktop_app.REPORT_OP, report())
    assert desktop_lanes(daemon) == ({"claude-2"}, set())


def test_c10_9_status_shows_the_apps_account_in_one_line(desktop):
    daemon, _, usage = desktop
    write_usage(usage, ORG_APP, sd=12)
    daemon.dispatch(desktop_app.REPORT_OP, report())
    status = daemon.dispatch("daemon.status", {})
    account = status["desktop_account"]
    assert (account["org_prefix"], account["lanes"], account["linked"]) == (ORG_APP[:8], ["claude-2"], True)
    [line] = [line for line in status["status"].splitlines() if line.startswith("desktop app:")]
    assert f"org {ORG_APP[:8]} (app file) on claude-2" in line and "weekly 12%" in line and "; hook " in line
    from subfleet import cli
    assert line in cli.format_status(status).splitlines()


def test_c10_9_status_without_the_signals_has_no_desktop_line(desktop):
    daemon, _, _ = desktop
    status = daemon.dispatch("daemon.status", {})
    assert "desktop_account" not in status and "desktop app:" not in status["status"]


def test_c10_9_the_timers_snapshot_judges_lanes_by_the_app_and_alerts_on_it(desktop):
    daemon, _, usage = desktop
    write_usage(usage, ORG_APP, seconds=0)
    snapshot = daemon.timers.snapshot()
    assert snapshot["desktop_account"]["linked"] is False
    assert {lane["lane_id"] for lane in snapshot["lanes"] if lane.get("desktop_possible")} == \
           {"claude-1", "claude-2", "claude-3"}
    later = datetime.now(timezone.utc) + timedelta(minutes=11)
    from subfleet import alerts
    keys = {row["key"] for row in alerts.evaluate_conditions({**snapshot, "desktop_account": {
        **snapshot["desktop_account"], "in_use": True}}, now=later)}
    assert f"desktop-app-unlinked:{ORG_APP[:8]}" in keys


def test_c10_9_doctor_passes_when_an_event_links_the_apps_org(desktop):
    daemon, harness, usage = desktop
    row = doctor.check_desktop_app(harness.root)
    assert row["status"] == doctor.UNKNOWN and "daemon install --hooks" in row["fix"]
    write_usage(usage, ORG_APP)
    row = doctor.check_desktop_app(harness.root)
    assert row["status"] == doctor.UNKNOWN and "no desktop session has reported" in row["detail"]
    daemon.dispatch(desktop_app.REPORT_OP, report())
    row = doctor.check_desktop_app(harness.root)
    assert row["status"] == doctor.PASS and EMAILS[1] in row["detail"]
    assert f"~/.claude.json names {EMAILS[0]}, another account" in row["detail"]
    stale = datetime.now(timezone.utc) + timedelta(hours=25)
    assert doctor.check_desktop_app(harness.root, now=stale)["status"] == doctor.UNKNOWN
    assert set(row) == {"check", "status", "detail", "fix"}


# --- fix round 1 of review r1 ------------------------------------------------------

ACCT, ORG = uuid(0xC7), uuid(0xA7)


def write_huge_sd(path, org=ORG_APP):
    stamp = int(datetime.now(timezone.utc).timestamp() * 1000)
    path.write_text('{"version":2,"samples":[{"t":%d,"org":"%s","u":{"sd":1%s}}]}' % (stamp, org, "0" * 400))
    desktop_app.forget_app_usage()


def test_c10_9_a_usage_number_too_large_for_a_float_stops_nothing(desktop):
    """Review r1, P1 1: `u.sd` = 10**400 raised `OverflowError` out of every
    desktop read, so admission, `daemon.status`, `lanes` and `pick` raised until
    the file changed, and the timers' snapshot lost `desktop_account`."""
    daemon, harness, usage = desktop
    write_huge_sd(usage)
    assert daemon._desktop_identity().app.org == ORG_APP
    assert daemon._cached_desktop_identity().app.sample.weekly is None
    assert daemon.dispatch("daemon.status", {})["desktop_account"]["org_prefix"] == ORG_APP[:8]
    assert {lane["lane_id"] for lane in daemon.dispatch("lanes", {})["lanes"]} >= {"claude-1", "claude-2"}
    daemon.dispatch("pick", {"model": "opus"})
    assert daemon.timers.snapshot().get("desktop_account", {}).get("org_prefix") == ORG_APP[:8]
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon._admit()
    huge = daemon._job(job_id)["state"]
    # The job moves as it does with `sd: 7`.
    write_usage(usage, ORG_APP, sd=7)
    other = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon._admit()
    assert huge != "queued" and huge == daemon._job(other)["state"]


def verified_lane(daemon, lane_id="claude-9", account=ACCT, org=ORG, email="verified@example.com"):
    """A lane enrolled with profile scope: its key is its identity (C-1.4)."""
    daemon.store.put_lane(replace(daemon.store.get_lane("claude-1"), lane_id=lane_id,
                                  account_key=f"claude:{account}:{org}", identity=f"{account}:{org}",
                                  label=email, credential=Credential("claude", f"claude-quota-{lane_id}",
                                                                     "keychain-token")))


def test_c10_3_a_verified_lane_stays_desktop_on_the_report_that_names_its_account(desktop):
    """Review r1, P1 2: the report that names claude-9's own account turned it
    from `desktop` to nothing, because `owns` matched `claude:<email>` keys only."""
    from subfleet import daemon as daemon_module
    from subfleet.adapters.claude import PROFILE_OK, ProfileResult
    daemon, harness, usage = desktop
    verified_lane(daemon)
    daemon.desktop_prober = lambda: ProfileResult(PROFILE_OK, email="verified@example.com", account_uuid=ACCT,
                                                  org_uuid=ORG)
    daemon._desktop_cache = (0.0, daemon_module._UNSET)
    assert "claude-9" in desktop_lanes(daemon)[0]                    # the profile's rule, before any signal
    assert daemon.dispatch(desktop_app.REPORT_OP, report(email="verified@example.com", account=ACCT, org=ORG)) == \
           {"recorded": True}
    assert desktop_lanes(daemon) == ({"claude-9"}, set())
    write_usage(usage, ORG)
    assert desktop_lanes(daemon) == ({"claude-9"}, set())
    daemon._desktop_in_use = daemon._desktop_answer = lambda: True
    lanes = {lane["lane_id"]: lane for lane in daemon.dispatch("lanes", {})["lanes"]}
    assert lanes["claude-9"].get("desktop") is True
    decision = daemon._pick(opus_job(daemon, harness), desktop=daemon._desktop_identity())
    assert "desktop" in rejected(decision, "claude-9") and decision.chosen_lane != "claude-9"


def test_c10_3_the_usage_file_alone_makes_a_verified_lane_in_its_org_desktop(desktop):
    daemon, _, usage = desktop
    verified_lane(daemon)
    write_usage(usage, ORG)
    assert desktop_lanes(daemon) == ({"claude-9"}, {"claude-1", "claude-2", "claude-3"})
    status = daemon.dispatch("daemon.status", {})
    assert status["desktop_account"]["lanes"] == ["claude-9"]
    [line] = [line for line in status["status"].splitlines() if line.startswith("desktop app:")]
    assert "on claude-9" in line


def seven_day(daemon, lane_id, utilization):
    from subfleet.contracts import Reading, ReadingLabel
    daemon.store.add_reading(Reading(lane_id, "account", "seven_day", utilization, now_iso(86400),
                                     ReadingLabel.PROVIDER, "fixture", now_iso()))


def rejected(decision, lane_id):
    return {reason for row in decision.evaluations[0]["rejections"] if row["lane_id"] == lane_id
            for reason in row["reasons"]}


def opus_job(daemon, harness):
    job_id = daemon.dispatch("submit", harness.submit_args(pinned_model="opus"))["job_id"]
    return daemon._job(job_id)


def test_c10_3_the_reservation_judges_the_desktop_lane_by_the_newest_report(desktop):
    """Review r1, P2 1: `_route_rows` marked the lanes with the evaluation's
    desktop decision, so a report that moved the desktop lane after the
    evaluation left the old verdict standing."""
    daemon, harness, _ = desktop
    for lane_id in ("claude-1", "claude-2", "claude-3"):
        seven_day(daemon, lane_id, 0.2)
    daemon._desktop_in_use = daemon._desktop_answer = lambda: True
    daemon.dispatch(desktop_app.REPORT_OP, report(email=EMAILS[2], org=ORG_OTHER))    # claude-3 is the desktop lane
    job = opus_job(daemon, harness)
    basis = {}
    early = daemon._pick(job, extra_exclusions=("claude-1",), desktop=daemon._desktop_identity(), basis=basis)
    assert early.chosen_lane == "claude-2" and "desktop" in rejected(early, "claude-3")
    daemon.dispatch(desktop_app.REPORT_OP, report())                                 # claude-2 is, in use
    daemon._desktop_in_use()
    daemon._record_desktop_use()
    status, _, decision = daemon._route_stands(basis, early)
    if status is None:
        assert decision.chosen_lane == "claude-3" and "desktop" in rejected(decision, "claude-2")
    else:
        assert status == "moved"


def test_c10_3_a_report_after_the_refresh_moves_the_route(desktop):
    """Ruling 3's window: a report that lands after the answer the reservation
    reads was refreshed, and before its transaction, is seen there: the route
    is `moved`, never the old verdict."""
    daemon, harness, _ = desktop
    for lane_id in ("claude-1", "claude-2", "claude-3"):
        seven_day(daemon, lane_id, 0.2)
    daemon._desktop_in_use = daemon._desktop_answer = lambda: True
    daemon.dispatch(desktop_app.REPORT_OP, report(email=EMAILS[2], org=ORG_OTHER))
    job = opus_job(daemon, harness)
    basis = {}
    early = daemon._pick(job, extra_exclusions=("claude-1",), desktop=daemon._desktop_identity(), basis=basis)
    assert early.chosen_lane == "claude-2"
    daemon._desktop_in_use()
    daemon._record_desktop_use()
    daemon.dispatch(desktop_app.REPORT_OP, report())                                 # after every refresh
    status, _, decision = daemon._route_stands(basis, early)
    assert not (status is None and decision.chosen_lane == "claude-2")


def test_c10_9_desktop_possible_alone_changing_moves_the_route(desktop):
    """Review r1, mutant 17: only claude-2's `desktop_possible` changes between
    the evaluation and the reservation (a report links its email to another
    org), and the check must judge it again (`desktop_possible` is one of
    `scheduler.LANE_FACTS`)."""
    daemon, harness, usage = desktop
    write_usage(usage, ORG_APP)                                  # unlinked: every Claude lane is possible
    seven_day(daemon, "claude-1", 0.1)
    seven_day(daemon, "claude-2", 0.5)
    seven_day(daemon, "claude-3", 0.9)
    daemon._desktop_in_use = daemon._desktop_answer = lambda: False
    job = opus_job(daemon, harness)
    basis = {}
    early = daemon._pick(job, desktop=daemon._desktop_identity(), basis=basis)
    assert early.chosen_lane == "claude-1"
    daemon.dispatch(desktop_app.REPORT_OP, report(email=EMAILS[1], org=ORG_OTHER))    # claude-2 linked elsewhere
    assert daemon._pick(job, desktop=daemon._desktop_identity()).chosen_lane == "claude-2"
    daemon._desktop_in_use()
    daemon._record_desktop_use()
    status, _, decision = daemon._route_stands(basis, early)
    assert not (status is None and decision.chosen_lane == "claude-1")


def alternate(daemon, calls):
    for n in range(calls):
        found = daemon.dispatch(desktop_app.REPORT_OP, report(email=EMAILS[2], org=ORG_OTHER, session=f"s-{n}")
                                if n % 2 == 0 else report(email="third@example.com", org=uuid(0xA3), session=f"s-{n}"))
        assert found == {"recorded": True}


def test_c10_9_an_orgs_link_outlives_the_row_window(desktop, monkeypatch):
    """Review r1, P2 2: an org's only link fell out of the newest
    `desktop_app.EVENT_ROWS` rows (2,003 alternating reports at 4,000 rows; 5
    at 8 here), and the app's own lane lost `desktop`."""
    daemon, _, usage = desktop
    monkeypatch.setattr(desktop_app, "EVENT_ROWS", 8)
    daemon.dispatch(desktop_app.REPORT_OP, report())
    write_usage(usage, ORG_APP)
    assert desktop_lanes(daemon) == ({"claude-2"}, set())
    alternate(daemon, 5)
    assert daemon._desktop_identity().app.email == EMAILS[1]
    assert desktop_lanes(daemon) == ({"claude-2"}, set())
    # Doctor's offline read follows the same rule.
    _, harness, _ = desktop
    app, _ = doctor.desktop_app_offline(harness.root)
    assert app.email == EMAILS[1]


def test_c10_9_the_hook_alone_names_the_newest_reports_org_past_the_window(desktop, monkeypatch):
    """Review r1, mutant 13 (the window read oldest first): with more rows than
    the window and no usage file, the app's org is the newest report's."""
    daemon, _, _ = desktop
    monkeypatch.setattr(desktop_app, "EVENT_ROWS", 4)                  # two events
    alternate(daemon, 3)
    assert daemon.dispatch(desktop_app.REPORT_OP, report()) == {"recorded": True}
    app = daemon._desktop_identity().app
    assert (app.org, app.email) == (ORG_APP, EMAILS[1])
    assert desktop_lanes(daemon) == ({"claude-2"}, set())


def test_c10_9_disagreement_is_dated_by_the_newest_events_past_the_window(desktop, monkeypatch):
    """Review r1, mutant 13, through a reader that takes the newest rows: when
    the newest reports began to name another org than the usage file
    (`disagree_since`)."""
    daemon, _, usage = desktop
    monkeypatch.setattr(desktop_app, "EVENT_ROWS", 4)                  # two events
    write_usage(usage, ORG_APP, seconds=-3600)
    for email, org, account, seconds in (("third@example.com", uuid(0xA3), uuid(0xC3), -7200),
                                         ("fourth@example.com", uuid(0xA4), uuid(0xC4), -7100),
                                         (EMAILS[1], ORG_APP, uuid(0xC1), -3000),
                                         (EMAILS[2], ORG_OTHER, uuid(0xC2), -2400),
                                         (EMAILS[2], ORG_OTHER, uuid(0xC5), -1800)):
        assert daemon.dispatch(desktop_app.REPORT_OP, report(email=email, org=org, account=account,
                                                             seconds=seconds)) == {"recorded": True}
    found = events(daemon)
    assert daemon._desktop_identity().app.disagree_since == found[3]["observed_at"]


def test_c10_9_concurrent_identical_reports_write_one_event(desktop, monkeypatch):
    """Review r1, mutant 15: the look and the write are one step under
    `_desktop_report_lock`. A slow look (50 ms) makes every unlocked run write more than one."""
    import threading
    import time
    daemon, _, _ = desktop
    look = desktop_app.should_record

    def slow(*args, **kwargs):
        answer = look(*args, **kwargs)
        time.sleep(0.05)
        return answer

    monkeypatch.setattr(desktop_app, "should_record", slow)
    same, errors = report(), []
    start = threading.Barrier(16)

    def send():
        try:
            start.wait()
            daemon.dispatch(desktop_app.REPORT_OP, dict(same))
        except Exception as exc:               # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=send) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == [] and len(events(daemon)) == 1


@pytest.mark.parametrize("broken", [{"observed_at": "9999-12-31T23:59:59-01:00"},
                                    {"observed_at": "0001-01-01T00:00:00+01:00"},
                                    {"email": "a\udcff@example.com"},
                                    {"email": "a​@example.com"}])
def test_c10_9_a_report_past_the_calendar_or_with_a_surrogate_email_is_refused(desktop, broken):
    """Review r1, P3 (malformed reports): these raised `OverflowError` and
    `UnicodeEncodeError` instead of being refused as `invalid-args`."""
    daemon, _, _ = desktop
    with pytest.raises(protocol.ProtocolError) as refused:
        daemon.dispatch(desktop_app.REPORT_OP, {**report(), **broken})
    assert refused.value.reason == "invalid-args"
    assert events(daemon) == []


def test_c10_9_status_and_the_snapshot_carry_no_full_org(desktop):
    """Review r1, P3: `desktop_account.org` put the full org uuid in `daemon.status`,
    the timers snapshot and so `status.json`."""
    daemon, _, usage = desktop
    write_usage(usage, ORG_APP)
    daemon.dispatch(desktop_app.REPORT_OP, report())
    for account in (daemon.dispatch("daemon.status", {})["desktop_account"],
                    daemon.timers.snapshot()["desktop_account"]):
        assert "org" not in account and account["org_prefix"] == ORG_APP[:8]
        assert ORG_APP not in json.dumps(account)


def test_c10_9_doctor_fails_when_no_lane_holds_the_apps_account(desktop):
    """Review r1, P3: doctor passed while the status line read "no lane"."""
    daemon, harness, usage = desktop
    write_usage(usage, ORG_APP)
    daemon.dispatch(desktop_app.REPORT_OP, report(email="nolane@example.com"))
    row = doctor.check_desktop_app(harness.root)
    [line] = [line for line in daemon.dispatch("daemon.status", {})["status"].splitlines()
              if line.startswith("desktop app:")]
    assert "no lane" in line
    assert row["status"] == doctor.FAIL and "lanes enroll" in row["fix"]
    verified_lane(daemon, org=ORG_APP, account=uuid(0xC1))
    assert doctor.check_desktop_app(harness.root)["status"] == doctor.PASS
