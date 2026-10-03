"""Alerts reach a person, and no notice is parked for no one (C-15.8, C-18.4, C-23.26).

Incident (2026-10-03): timer alerts went to `alerts.operator_session`, which was
never configured, so `ping` addressed them to the literal session `operator`.
No session has that id, nothing listed the inbox, and C-23.26 never prunes a
`pending` notice: 1,637 alerts in 14 days, none read, among them every
"credentials ... auth-dead. Run: ..." line C-23.52 exists to put in front of the
operator. Every `doctor --live` parked one more.

Invariants, for every input (Hypothesis), beside the examples:

- A `ping` without text writes nothing, whatever session it names or omits.
- With no `alerts.operator_session`, no sequence of alert cycles writes a
  service notice.
- After a cycle that is not offline, with delivery succeeding, the alerts
  `status` shows are exactly that cycle's conditions; an offline cycle changes
  none of them (C-23.27).
- A withdrawal deletes exactly the named, unresolved service notices of its
  session; its one event names exactly those; nothing else in either notice
  table changes; a named job notice refuses the whole withdrawal.
- `notice.list` agrees with `notice.pending` on a session's unresolved notices,
  and the offline reader agrees with `notice.list` (differential).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from subfleet import cli, protocol
from subfleet import daemon as daemon_module
from subfleet.alerts import Alerts, active_alerts, evaluate_conditions, load_latches, operator_session
from subfleet.contracts import Exit
from subfleet.daemon import Daemon
from subfleet.offline import Offline
from subfleet.status_json import build_status
from subfleet.store import Store

NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
FIXTURE_HEALTH = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow]
JOB = "20261003-120000-notices"
STATES = ("pending", "offered", "surfaced", "acknowledged")


@pytest.fixture
def core(tmp_path, monkeypatch):
    """A daemon core, not serving: its ops are called through `dispatch`."""
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fake-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fake-start")
    service = Daemon(tmp_path / "state")
    service.policy.setdefault("alerts", {}).pop("operator_session", None)
    yield service
    service.close()


def service_rows(store):
    return store.query("SELECT notice_id, session_id, text, state FROM service_notices ORDER BY notice_id")


def lane(identity="one", provider="codex", **extra):
    return {"lane_id": identity, "provider": provider, "account_key": f"{provider}:{identity}",
            "email": f"{identity}@example.invalid", "home": f"/homes/{identity}",
            "credential_ref": f"/homes/{identity}", "enabled": True, "owner": "v2", "verdict": "ok",
            "readings": [], "closures": [], **extra}


def fleet(*lanes):
    """A view with two healthy Codex lanes beside `lanes`, so no fleet alert fires."""
    return {"lanes": [lane("healthy-1"), lane("healthy-2"), *lanes]}


# --- doctor and ping (C-15.8, C-16.2) -----------------------------------------

@settings(max_examples=40, deadline=None, suppress_health_check=FIXTURE_HEALTH)
@given(session=st.sampled_from([None, "", "s-1", "operator"]),
       configured=st.sampled_from([None, "", "  ", "ops-1"]),
       text=st.sampled_from([None, ""]))
def test_c15_8_a_ping_without_text_writes_nothing(core, session, configured, text):
    """C-15.8, C-16.2 a liveness ping is read-only for every session it names or omits."""
    core.policy["alerts"]["operator_session"] = configured
    args = {key: value for key, value in (("session_id", session), ("text", text)) if value is not None}
    reply = core.dispatch("ping", args)
    assert reply["pong"] is True and reply["notice_id"] is None
    assert service_rows(core.store) == []


def test_c15_8_a_ping_with_text_and_no_session_is_refused_without_an_operator(core):
    """C-15.8 there is no default inbox: text addressed to no session is refused, exit 2."""
    with pytest.raises(protocol.ProtocolError) as refused:
        core.dispatch("ping", {"text": "hello"})
    assert refused.value.code == Exit.INVALID_INPUT and "operator_session" in str(refused.value)
    assert service_rows(core.store) == []


def test_c15_8_a_ping_goes_to_the_session_named_else_the_configured_operator(core):
    """C-15.8 a named session wins; with none named, the configured operator session."""
    core.policy["alerts"]["operator_session"] = "ops-1"
    named = core.dispatch("ping", {"text": "to s-1", "session_id": "s-1"})
    unnamed = core.dispatch("ping", {"text": "to the operator"})
    assert (named["session_id"], unnamed["session_id"]) == ("s-1", "ops-1")
    assert [(row["session_id"], row["text"]) for row in service_rows(core.store)] == [
        ("s-1", "to s-1"), ("ops-1", "to the operator")]


@pytest.mark.parametrize("value,expected", [(None, None), ("", None), ("  ", None), (7, None),
                                            ("ops-1", "ops-1"), (" ops-1 ", "ops-1")])
def test_c15_8_operator_session_is_only_a_named_session(value, expected):
    """C-15.8 an unset, blank or non-string `alerts.operator_session` names no one."""
    assert operator_session({"alerts": {"operator_session": value}}) == expected
    assert operator_session({}) is None and operator_session({"alerts": None}) is None


# --- the timer's delivery (C-15.8, C-18.1) ------------------------------------

NOTICE = {"key": "codex-fleet-empty", "severity": "critical", "subject": "codex: no dispatchable lanes",
          "body": "All enrolled lanes are exhausted, unavailable, or unknown. Run: subfleet status",
          "home": "fleet:codex"}


def test_c15_8_a_timer_alert_with_no_operator_session_is_shown_not_parked(core):
    """C-15.8, C-18.1 with no operator session an alert is delivered by `status` alone."""
    assert core._timer_notice(dict(NOTICE)) is True
    assert service_rows(core.store) == []


def test_c15_8_a_timer_alert_goes_to_the_configured_operator_session(core):
    """C-18.1 a configured operator session is still sent every alert as a notice."""
    core.policy["alerts"]["operator_session"] = "ops-1"
    assert core._timer_notice(dict(NOTICE)) is True
    assert [(row["session_id"], row["text"]) for row in service_rows(core.store)] == [
        ("ops-1", NOTICE["subject"] + "\n" + NOTICE["body"])]


VERDICTS = ("ok", "auth-dead", "no-auth", "expired-token", "limited")
LANES = st.lists(st.tuples(st.sampled_from(["one", "two", "three"]), st.sampled_from(["codex", "claude"]),
                           st.sampled_from(VERDICTS), st.booleans(), st.booleans()),
                 max_size=4, unique_by=lambda row: row[0])
CYCLES = st.lists(st.tuples(LANES, st.booleans(), st.integers(min_value=1, max_value=60 * 9)),
                  min_size=1, max_size=8)


def view_of(rows):
    lanes = []
    for identity, provider, verdict, closed, enabled in rows:
        closures = ([{"scope": "account", "reason": "provider-limit", "until_at": "2026-10-04T00:00:00Z"}]
                    if closed else [])
        lanes.append(lane(identity, provider, verdict=verdict, enabled=enabled, closures=closures))
    return {"lanes": lanes}


@settings(max_examples=60, deadline=None, suppress_health_check=FIXTURE_HEALTH)
@given(cycles=CYCLES)
def test_c15_8_c18_4_no_cycle_parks_a_notice_and_status_shows_each_cycles_conditions(core, cycles):
    """C-15.8, C-18.4, C-23.27 for any cycle sequence with no operator session: no
    service notice is written; after a cycle that is not offline the alerts shown
    are exactly its conditions, each in that cycle's words; an offline cycle
    changes none of them; and a restarted monitor shows the same alerts."""
    with core.store.transaction() as tx:
        tx.execute("DELETE FROM events WHERE kind='alert-latch'")
    monitor = Alerts(core.store, core.policy, core._timer_notice)
    at = NOW
    for rows, offline, minutes in cycles:
        at += timedelta(minutes=minutes)
        view = view_of(rows)
        before = monitor.active()
        monitor.evaluate(view, now=at, offline=offline)
        shown = monitor.active()
        if offline:
            assert shown == before
        else:
            current = {row["key"]: row for row in evaluate_conditions(view, now=at)}
            assert {row["key"] for row in shown} == set(current)
            for row in shown:
                assert (row["severity"], row["subject"], row["body"]) == tuple(
                    current[row["key"]][field] for field in ("severity", "subject", "body"))
                assert row["since"] is not None and row["since"] <= at.isoformat().replace("+00:00", "Z")
        assert service_rows(core.store) == []
        restarted = Alerts(core.store, core.policy, core._timer_notice)
        assert {row["key"] for row in restarted.active()} == {row["key"] for row in shown}


def test_c18_4_since_is_kept_while_active_and_restarts_when_the_alert_returns(tmp_path):
    """C-18.4 `since` is when the alert last came into force, not its last re-alert."""
    with Store(tmp_path / "store.db") as store:
        monitor = Alerts(store, {}, lambda notice: True)
        dead = fleet(lane(verdict="auth-dead"))
        stamp = lambda moment: moment.isoformat().replace("+00:00", "Z")
        monitor.evaluate(dead, now=NOW)
        monitor.evaluate(dead, now=NOW + timedelta(hours=7))             # a re-alert
        (row,) = monitor.active()
        assert (row["since"], row["last_sent"]) == (stamp(NOW), stamp(NOW + timedelta(hours=7)))
        assert "Run: CODEX_HOME=/homes/one codex login" in row["body"]
        monitor.evaluate(fleet(lane()), now=NOW + timedelta(hours=8))
        assert monitor.active() == []
        monitor.evaluate(dead, now=NOW + timedelta(hours=9))
        assert monitor.active()[0]["since"] == stamp(NOW + timedelta(hours=9))


def test_c18_4_a_failed_delivery_is_not_shown_as_delivered(tmp_path):
    """C-18.1, C-18.4 an alert whose notice could not be written does not latch, so
    `status` does not show it as raised; it is retried next cycle as before."""
    with Store(tmp_path / "store.db") as store:
        monitor = Alerts(store, {}, lambda notice: False)
        monitor.evaluate({"lanes": [lane(verdict="auth-dead")]}, now=NOW)
        assert monitor.active() == []


def test_c18_4_most_severe_first_and_a_wordless_latch_shows_its_key():
    """C-18.4 critical before warn before info; a latch from before alerts kept
    their words shows its key, with no severity and no `since`."""
    latches = {"old-v1-key": {"active": True, "last_sent": "2026-09-01T00:00:00Z"},
               "b": {"active": True, "severity": "warn", "subject": "B", "body": "b", "since": "2026-10-02T00:00:00Z"},
               "a": {"active": True, "severity": "critical", "subject": "A", "body": "a", "since": "2026-10-03T00:00:00Z"},
               "gone": {"active": False, "severity": "critical", "subject": "G", "body": "g"},
               "i": {"active": True, "severity": "info", "subject": "I", "body": "i", "since": "2026-09-30T00:00:00Z"}}
    rows = active_alerts(latches)
    assert [row["key"] for row in rows] == ["a", "old-v1-key", "b", "i"]
    assert rows[1] == {"key": "old-v1-key", "severity": None, "subject": "old-v1-key", "body": "",
                       "home": None, "since": None, "last_sent": "2026-09-01T00:00:00Z"}


# --- where alerts are shown (C-18.4) ------------------------------------------

def test_c18_4_status_json_carries_the_alerts_in_force(tmp_path):
    """C-18.4 `status.json` always has `alerts`; each row is what the menu shows."""
    assert build_status({"lanes": []}, now=NOW)["alerts"] == []
    with Store(tmp_path / "store.db") as store:
        monitor = Alerts(store, {}, lambda notice: True)
        monitor.evaluate(fleet(lane(verdict="auth-dead")), now=NOW)
        (row,) = build_status({"lanes": [], "alerts": monitor.active()}, now=NOW)["alerts"]
    assert set(row) == {"key", "severity", "subject", "body", "since", "last_sent"}
    assert row["severity"] == "critical" and row["subject"] == "codex credentials: /homes/one is auth-dead"


def test_c18_4_status_leads_with_the_alerts_and_what_to_run():
    """C-18.4, C-23.52 `subfleet status` prints the alerts in force first, each with its command."""
    text = cli.format_status({"lanes": [], "alerts": [
        {"key": "k", "severity": "critical", "subject": "claude credentials: c-1 is auth-dead",
         "body": "c-1 (a@example.invalid): auth-dead. Run: claude setup-token; subfleet lanes enroll c-1",
         "since": "2026-10-03T12:00:00Z"}]})
    lines = text.splitlines()
    assert lines[0] == "alerts: 1 in force"
    assert "critical" in lines[1] and "auth-dead" in lines[1] and "since 2026-10-03T12:00:00Z" in lines[1]
    assert lines[2].strip().endswith("subfleet lanes enroll c-1")
    assert "alerts" not in cli.format_status({"lanes": []})


def test_c18_4_daemon_status_lists_the_alerts_in_force(core):
    """C-18.4 `daemon.status`, which `subfleet status` prints, carries the alerts."""
    core.timers.alerts.evaluate(fleet(lane(verdict="auth-dead")), now=NOW)
    status = core.dispatch("daemon.status", {})
    assert [row["key"] for row in status["alerts"]] == ["codex-revoked:/homes/one"]
    assert {"active_attempts", "admission", "timers", "lanes"} <= set(status)   # nothing displaced


def test_c18_4_offline_status_reads_the_alerts_from_their_latches(root):
    """C-18.4, C-17.5 with the daemon down, `status` shows the alerts in force in
    the words they last fired with."""
    with Store(root / "state.sqlite3") as store:
        monitor = Alerts(store, {}, lambda notice: True)
        monitor.evaluate(fleet(lane(verdict="auth-dead"), lane("two", verdict="no-auth")), now=NOW)
        expected = monitor.active()
    shown = Offline(root).status()["alerts"]
    assert shown == expected
    assert {row["key"] for row in shown} == {"codex-revoked:/homes/one", "codex-noauth:/homes/two"}
    with Store(root / "state.sqlite3") as store:
        assert load_latches(store.query("SELECT data_json FROM events WHERE kind='alert-latch' ORDER BY event_id"))


# --- notice.list and notice.withdraw (C-15.8, C-23.26) ------------------------

ROWS = st.lists(st.tuples(st.sampled_from(["job", "service"]), st.sampled_from(["a", "b", None]),
                          st.sampled_from(STATES), st.sampled_from(["first", "second\nbody"])),
                max_size=14)


def seed(store, rows):
    """Insert job and service notices; returns every (id as listed, table, session, state, text)."""
    if store.one("SELECT 1 FROM jobs WHERE job_id=?", (JOB,)) is None:
        store.add_job(job_id=JOB, request_id="r-1", payload_digest="d", kind="run", state="succeeded",
                      workdir="/w", prompt_path="/w/p.md", sandbox="read-only")
    made = []
    with store.transaction() as tx:
        for number, (table, session, state, text) in enumerate(rows):
            stamp = f"2026-10-03T12:{number:02d}:00Z"
            if table == "job":
                cursor = tx.execute("INSERT INTO notices(job_id,session_id,text,state,created_at) "
                                    "VALUES(?,?,?,?,?)", (JOB, session, text, state, stamp))
                made.append((cursor.lastrowid, "job", session, state, text))
            else:
                cursor = tx.execute("INSERT INTO service_notices(session_id,text,state,created_at) "
                                    "VALUES(?,?,?,?)", (session or "c", text, state, stamp))
                made.append((-cursor.lastrowid, "service", session or "c", state, text))
    return made


def everything(store):
    jobs = store.query("SELECT notice_id, session_id, state, text FROM notices ORDER BY notice_id")
    service = store.query("SELECT notice_id, session_id, state, text FROM service_notices ORDER BY notice_id")
    return ({row["notice_id"]: row for row in jobs}, {-row["notice_id"]: row for row in service})


def reset(store):
    with store.transaction() as tx:
        tx.execute("DELETE FROM notices")
        tx.execute("DELETE FROM service_notices")
        tx.execute("DELETE FROM events WHERE kind='notice.withdrawn'")


@settings(max_examples=80, deadline=None, suppress_health_check=FIXTURE_HEALTH)
@given(rows=ROWS, data=st.data())
def test_c15_8_a_withdrawal_deletes_exactly_the_named_undelivered_service_notices(core, rows, data):
    """C-15.8, C-23.26 only the named rows, only the session's, only unresolved;
    one event names exactly them; a named job notice refuses the whole call."""
    reset(core.store)
    made = seed(core.store, rows)
    named = data.draw(st.lists(st.sampled_from([row[0] for row in made] or [-99]), unique=True))
    session = data.draw(st.sampled_from(["a", "b", "c"]))
    before = everything(core.store)
    if any(notice_id >= 0 for notice_id in named):
        with pytest.raises(protocol.ProtocolError) as refused:
            core.dispatch("notice.withdraw", {"session_id": session, "notice_ids": named})
        assert refused.value.code == Exit.INVALID_INPUT and "--ack" in (refused.value.fix or "")
        assert everything(core.store) == before
        return
    reply = core.dispatch("notice.withdraw", {"session_id": session, "notice_ids": named, "reason": "test"})
    gone = {notice_id for notice_id, table, owner, state, _ in made
            if table == "service" and notice_id in named and owner == session and state in ("pending", "offered")}
    jobs_after, service_after = everything(core.store)
    assert jobs_after == before[0]
    assert service_after == {key: row for key, row in before[1].items() if key not in gone}
    assert set(reply["withdrawn"]) == gone and set(reply["kept"]) == set(named) - gone
    events = [json.loads(row["data_json"]) for row in
              core.store.query("SELECT data_json FROM events WHERE kind='notice.withdrawn'")]
    if not gone:
        assert events == []
        return
    (event,) = events
    assert sorted(event["service_notice_ids"]) == sorted(-notice_id for notice_id in gone)
    assert event["count"] == len(gone) == sum(event["subjects"].values())
    assert set(event["subjects"]) <= {"first", "second"}
    assert (event["session_id"], event["reason"]) == (session, "test")
    assert event["first_created_at"] <= event["last_created_at"]


@settings(max_examples=60, deadline=None, suppress_health_check=FIXTURE_HEALTH)
@given(rows=ROWS, session=st.sampled_from(["a", "b", "c"]))
def test_c15_8_notice_list_agrees_with_notice_pending_and_with_the_offline_reader(core, rows, session):
    """C-15.8 differential: `notice.list` for one session's unresolved notices is
    `notice.pending`'s answer, and the offline reader returns `notice.list`'s rows."""
    reset(core.store)
    seed(core.store, rows)
    listed = core.dispatch("notice.list", {"session_id": session})["notices"]
    pending = core.dispatch("notice.pending", {"session_id": session})["notices"]
    key = lambda row: (row["notice_id"], row["session_id"], row["state"], row["text"], row["job_id"])
    assert sorted(map(key, listed)) == sorted(map(key, pending))
    offline = Offline(core.root)
    for session_id in (session, None):
        for resolved in (False, True):
            assert offline.notices(session_id, resolved=resolved) == core.dispatch(
                "notice.list", {"session_id": session_id, "resolved": resolved})["notices"]
    everyone = core.dispatch("notice.list", {"resolved": True})["notices"]
    assert len(everyone) == len(rows)


def test_c15_8_listing_marks_nothing(core):
    """C-15.8 listing an inbox is read-only: the session's own hooks still surface it."""
    seed(core.store, [("service", "a", "pending", "x"), ("job", "a", "offered", "y")])
    before = everything(core.store)
    core.dispatch("notice.list", {})
    core.dispatch("notice.list", {"session_id": "a", "resolved": True})
    assert everything(core.store) == before


# --- the `notices` verb (C-15.8, C-17.1) --------------------------------------

LISTED = [{"notice_id": -7, "session_id": "operator", "state": "pending", "job_id": None,
           "text": "codex: no dispatchable lanes\nAll enrolled lanes ...", "created_at": "2026-09-19T14:50:51Z"},
          {"notice_id": 12, "session_id": "operator", "state": "offered", "job_id": JOB,
           "text": f"{JOB}: succeeded; rc=0", "created_at": "2026-09-20T00:00:00Z"},
          {"notice_id": -8, "session_id": "s-2", "state": "pending", "job_id": None,
           "text": "hello", "created_at": "2026-09-21T00:00:00Z"}]


def test_c17_1_notices_lists_every_inbox_and_marks_nothing(daemon, capsys):
    """C-15.8, C-17.1 v1's `notices` spelling: every session's unresolved notices by default."""
    server = daemon({"notice.list": lambda request: {"notices": LISTED}})
    assert cli.main(["notices"]) == int(Exit.OK)
    text = capsys.readouterr().out
    assert "operator: 2 notice(s), 1 job, 1 service" in text and "s-2: 1 notice(s)" in text
    assert "codex: no dispatchable lanes" in text and "All enrolled" not in text
    assert server.ops() == ["notice.list"]
    assert server.requests[0].args == {"session_id": None, "resolved": False}


def test_c17_1_notices_all_includes_resolved_and_json_is_one_object(daemon, capsys):
    """C-17.1, C-17.4 `--all` asks for resolved rows too; `--json` is one object."""
    server = daemon({"notice.list": lambda request: {"notices": LISTED}})
    assert cli.main(["notices", "--session", "operator", "--all", "--json"]) == int(Exit.OK)
    assert json.loads(capsys.readouterr().out) == {"notices": LISTED}
    assert server.requests[0].args == {"session_id": "operator", "resolved": True}


def test_c15_8_notices_ack_acknowledges_exactly_the_unresolved_rows_listed(daemon, capsys):
    """C-15.8 `--ack` names every unresolved notice the listing returned, job and service."""
    operator = [row for row in LISTED if row["session_id"] == "operator"]
    resolved = {**operator[0], "notice_id": -9, "state": "surfaced"}
    server = daemon({"notice.list": lambda request: {"notices": [*operator, resolved]},
                     "notice.ack": lambda request: {"notices": []}})
    assert cli.main(["notices", "--session", "operator", "--ack"]) == int(Exit.OK)
    assert server.ops() == ["notice.list", "notice.ack"]
    assert server.requests[0].args["resolved"] is False
    assert server.requests[1].args == {"session_id": "operator", "notice_ids": [-7, 12]}
    assert "acknowledged 2 notice(s) for operator" in capsys.readouterr().out


def test_c15_8_notices_withdraw_sends_only_service_notices_and_names_the_rest(daemon, capsys):
    """C-15.8 `--withdraw` withdraws service notices only; job notices are named, left."""
    operator = [row for row in LISTED if row["session_id"] == "operator"]
    server = daemon({"notice.list": lambda request: {"notices": operator},
                     "notice.withdraw": lambda request: {"session_id": "operator", "withdrawn": [-7], "kept": []}})
    assert cli.main(["notices", "--session", "operator", "--withdraw", "--reason", "superseded"]) == int(Exit.OK)
    assert server.requests[1].op == "notice.withdraw"
    assert server.requests[1].args == {"session_id": "operator", "notice_ids": [-7], "reason": "superseded"}
    captured = capsys.readouterr()
    assert "withdrew 1 service notice(s) for operator" in captured.out
    assert "1 job notice(s) left" in captured.err and "--ack" in captured.err


@pytest.mark.parametrize("argv,needs", [(["notices", "--ack"], "--session"),
                                        (["notices", "--withdraw"], "--session"),
                                        (["notices", "--session", "s", "--reason", "x"], "--withdraw")])
def test_c15_8_notices_refuses_an_action_without_its_inbox(daemon, capsys, argv, needs):
    """C-15.8, C-17.3 an action names one session; `--reason` belongs to `--withdraw`; exit 2."""
    server = daemon({})
    assert cli.main(argv) == int(Exit.INVALID_INPUT)
    assert needs in capsys.readouterr().err and server.ops() == []


def test_c15_8_notices_ack_and_withdraw_are_exclusive(daemon, capsys):
    """C-15.8, C-17.3 one action per call: both is a usage error, exit 2."""
    server = daemon({})
    assert cli.main(["notices", "--session", "s", "--ack", "--withdraw"]) == int(Exit.INVALID_INPUT)
    assert "not allowed with" in capsys.readouterr().err and server.ops() == []


def test_c17_5_notices_lists_from_the_store_when_the_daemon_is_down(root, capsys):
    """C-17.5 listing needs no daemon; an action does (exit 69)."""
    with Store(root / "state.sqlite3") as store:
        seed(store, [("service", "operator", "pending", "codex: no dispatchable lanes\nbody")])
    assert cli.main(["notices", "--session", "operator"]) == int(Exit.OK)
    captured = capsys.readouterr()
    assert "operator: 1 notice(s), 0 job, 1 service" in captured.out and "offline" in captured.err
    assert cli.main(["notices", "--session", "operator", "--ack"]) == int(Exit.DAEMON_UNAVAILABLE)


def test_c15_8_withdrawal_through_the_cli_against_a_real_store(core, root, monkeypatch, capsys):
    """C-15.8 end to end on a real daemon core: the operator inbox is listed, its
    service notices withdrawn with a reason, its job notice left and named."""
    seed(core.store, [("service", "operator", "pending", "codex: no dispatchable lanes\nbody"),
                      ("service", "operator", "offered", "recovered: fleet:codex\nbody"),
                      ("service", "operator", "surfaced", "doctor"),
                      ("job", "operator", "pending", f"{JOB}: succeeded; rc=0")])

    class Direct:
        def __init__(self, *_args, **_kwargs):
            pass

        def call(self, op, args=None):
            return core.dispatch(op, args or {})

    monkeypatch.setattr(cli, "Client", Direct)
    assert cli.main(["notices", "--session", "operator", "--withdraw", "--reason", "never delivered"]) == 0
    assert "withdrew 2 service notice(s) for operator" in capsys.readouterr().out
    assert [row["state"] for row in service_rows(core.store)] == ["surfaced"]
    assert core.store.query("SELECT state FROM notices") == [{"state": "pending"}]
    (event,) = [json.loads(row["data_json"]) for row in
                core.store.query("SELECT data_json FROM events WHERE kind='notice.withdrawn'")]
    assert event["subjects"] == {"codex: no dispatchable lanes": 1, "recovered: fleet:codex": 1}
    assert event["reason"] == "never delivered" and event["count"] == 2
