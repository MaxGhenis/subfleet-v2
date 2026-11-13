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
