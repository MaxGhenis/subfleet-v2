"""C-9.6, D-RAMP-HOLD-LABEL: a six-hour safety bound is an operator hold."""

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import example, given, settings, strategies as st

from subfleet import cli, daemon, protocol, render
from subfleet.alerts import Alerts, evaluate_conditions
from subfleet.capacity import build_view, from_store
from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner
from subfleet.conversations import waits
from subfleet.operations import brief, error_report
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from subfleet.scheduler import evaluate
from subfleet.status_json import build_status
from subfleet.store import HOLD_NOTE_MIGRATION, SCHEMA_VERSION, Store
from tests.caps import capped

format_status = cli.format_status
NOW = datetime(2026, 10, 26, 16, 55, tzinfo=timezone.utc)
UNTIL = "2026-10-26T22:55:00Z"
NOTE = "ramp: batch or running-cap hold; releases lanes in rotation"


def iso(at):
    return at.isoformat(timespec="seconds").replace("+00:00", "Z")


def lane_row():
    return {"lane_id": "codex-1", "provider": "codex", "home": "/homes/codex-1",
            "account_key": "codex:one", "owner": "v2", "enabled": True, "desktop": False}


def held(note=None, **changes):
    return {"lane_id": "codex-1", "scope": "account", "until_at": UNTIL, "reason": "operator-hold",
            "clock_source": "reported", "source_event": "operator", "note": note, **changes}


@pytest.fixture
def operator(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "utcnow", lambda: iso(NOW))
    with Store(tmp_path / "state.db") as store:
        store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/homes/codex-1", "home"),
                            "/homes/codex-1", LaneOwner.V2, False, True))
        yield SimpleNamespace(store=store, _notify=lambda: None, _desktop_identity=lambda: None,
                              _capacity_view=lambda _: from_store(store, now=NOW))


def hold(operator, *, note=None, until=UNTIL):
    return daemon.Daemon._hold_lane(operator, protocol.LanesArgs(
        action="hold", lane_id="codex-1", until=until, note=note))


def test_six_hour_hold_status_says_held_with_latest_bound():
    """C-9.6: status must not describe the ramp's safety bound as a provider reset."""
    lane = {"lane_id": "codex-1", "provider": "codex", "home": "/homes/codex-1",
            "owner": "v2", "enabled": True, "closures": [
                {"scope": "account", "reason": "operator-hold", "until_at": "2026-10-26T22:55:00Z"}]}
    alerts = evaluate_conditions({"lanes": [lane]}, now="2026-10-26T16:55:00Z")
    text = format_status({"now": "2026-10-26T16:55:00Z", "lanes": [lane], "alerts": alerts})
    assert "/homes/codex-1: held by the operator (until 2026-10-26T22:55:00Z at the latest)" in text
    assert "closed (operator-hold); resets" not in text


@pytest.mark.parametrize("refresh_until", [UNTIL, "2026-10-26T21:55:00Z"])
def test_hold_note_round_trips_through_hold_refresh_and_release(operator, monkeypatch, capsys, refresh_until):
    """C-9.6: a refresh that keeps the row keeps its note, including after release."""
    monkeypatch.setattr(cli, "_client", lambda _: SimpleNamespace(
        call=lambda _op, args: daemon.Daemon._hold_lane(operator, protocol.LanesArgs(**args))))
    assert cli.main(["lanes", "hold", "codex-1", "--until", UNTIL, "--note", NOTE, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["closures"][0]["note"] == NOTE
    assert result["closures"][0]["reason"] == "operator-hold"
    assert result["closures"][0]["until_at"] == UNTIL
    identity = result["closures"][0]["closure_id"]
    refreshed = hold(operator, until=refresh_until)
    assert refreshed["closures"][0]["closure_id"] == identity
    assert refreshed["closures"][0]["note"] == NOTE
    assert from_store(operator.store, now=NOW)["lanes"][0]["closures"][0]["note"] == NOTE
    assert cli.main(["lanes", "release", "codex-1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["closures"] == []
    [released] = operator.store.list_closures()
    assert released["note"] == NOTE and released["released_at"] == iso(NOW)


@pytest.mark.parametrize("note", [None, "new operator decision"])
def test_replacing_hold_takes_new_calls_note_or_none(operator, note):
    """C-9.6: an extending call replaces the row's metadata; it never inherits an omitted note."""
    hold(operator, note=NOTE)
    result = hold(operator, note=note, until="2026-10-26T23:55:00Z")
    assert len(result["closures"]) == 1
    assert result["closures"][0]["note"] == note
    daemon.Daemon._hold_lane(operator, protocol.LanesArgs(action="release", lane_id="codex-1"))
    hold(operator, note=note)
    assert operator.store.list_closures()[1]["note"] == note


BAD_NOTES = ["x" * 121, "first\nsecond", "first\rsecond", "tab\there", "nul\0here", "escape\x1bhere",
             "delete\x7fhere", "line\u2028here", "hidden\u200bhere"]


@pytest.mark.parametrize("note", BAD_NOTES)
def test_cli_refuses_invalid_note_before_transport(note, monkeypatch, capsys):
    """C-9.6: reject length, line breaks and nonprintable characters without contacting a daemon."""
    monkeypatch.setattr(cli, "_client", lambda _: pytest.fail("invalid note reached the transport"))
    assert cli.main(["lanes", "hold", "codex-1", "--until", UNTIL, "--note", note]) == 2
    assert "one line of printable text, at most 120 characters" in capsys.readouterr().err


@pytest.mark.parametrize("note", [*BAD_NOTES, 7, ["not text"]])
def test_daemon_refuses_invalid_note_without_writes(operator, note):
    """C-9.6: direct protocol callers meet the same validation before any hold is recorded."""
    before = operator.store.list_events()
    with pytest.raises(protocol.ProtocolError, match="one line of printable text, at most 120 characters"):
        hold(operator, note=note)
    assert operator.store.list_closures() == []
    assert operator.store.list_events() == before


@pytest.mark.parametrize("note", [None, "", "é" * 120, "  spaced label  "])
def test_valid_note_is_preserved_and_absence_is_json_null(operator, note):
    """C-9.6: count characters, preserve printable text exactly and represent absence as null."""
    result = json.loads(json.dumps(hold(operator, note=note)))
    assert result["closures"][0]["note"] == (note or None)
    assert operator.store.list_closures()[0]["source_event"] == "operator"
    projected = build_status({"lanes": result["lanes"]}, now=NOW)
    assert projected["codex"]["homes"][0]["closures"][0]["note"] == (note or None)


@pytest.mark.parametrize("note", [None, NOTE])
@pytest.mark.parametrize("site", ["alert", "notice", "status", "capacity", "lanes", "why", "runs",
                                  "pin-notice", "pin-failure", "brief", "conversation"])
def test_each_print_site_calls_an_operator_hold_held(operator, note, site):
    """C-9.6: every human closure display uses the operator wording and the latest bound."""
    closure = held(note)
    lane = {**lane_row(), "closures": [closure]}
    view = {"now": iso(NOW), "lanes": [lane], "closures": [closure]}
    stuck = {"lane_id": "codex-1", "credential_ref": "/homes/codex-1", "closures": [closure],
             "reasons": [f"closed:account:{UNTIL}"]}
    if site == "alert":
        text = "\n".join(row["body"] for row in evaluate_conditions(view, now=NOW))
    elif site == "notice":
        delivered = []
        Alerts(operator.store, {}, delivered.append).evaluate(view, now=NOW)
        text = json.dumps(delivered)
    elif site == "status":
        text = format_status(view)
    elif site == "capacity":
        text = render.status(view)
    elif site == "lanes":
        text = cli._format_lanes(view)
    elif site == "why":
        text = render.why({"evaluations": [{"model": "astra", "closures": [closure]}]})
    elif site == "runs":
        text = cli._format_job({"job_id": "job", "state": "waiting", "notices": [
            {"state": "pending", "text": render.pin_notice("job", stuck, None)}]})
    elif site == "pin-notice":
        text = render.pin_notice("job", stuck, None)
    elif site == "pin-failure":
        text = render.pin_failure(stuck, iso(NOW))
    elif site == "conversation":
        summary = waits.lane_summary({"evaluations": [{"provider": "codex", "closures": [closure],
            "rejections": [{"lane_id": "codex-1", "reasons": stuck["reasons"]}]}]})
        text = waits.lanes_reason(summary, "closed:account")
    else:
        text = brief(view)
    expected = "held by the operator" + (f": {note}" if note else "") + f" (until {UNTIL} at the latest)"
    assert expected in text
    assert "closed (operator-hold)" not in text
    assert "operator-hold, provider-limit" not in text


@pytest.mark.parametrize("reason", ["provider-limit", "credits", "cooldown", "auth-dead"])
def test_other_closures_never_show_a_note_and_keep_their_wording(operator, reason):
    """C-9.6: a stray note cannot change any non-operator closure display."""
    closure = held("DO NOT DISPLAY", reason=reason, source_event="provider-event")
    without = {**closure, "note": None}
    assert render.closure_text(closure) == render.closure_text(without)
    assert render.closure_text(closure) == f"account until {UNTIL} ({reason}; reported clock; event=provider-event)"
    lane = {**lane_row(), "closures": [closure]}
    alerts = evaluate_conditions({"lanes": [lane]}, now=NOW)
    assert "DO NOT DISPLAY" not in json.dumps(alerts)
    if reason != "auth-dead":
        assert any(row["body"] == f"/homes/codex-1: account is closed ({reason}); resets {UNTIL}." for row in alerts)
    operator.store.add_closure(Closure("codex-1", "account", UNTIL, ClosureReason(reason),
                                       ClockSource.REPORTED, "provider-event", "DO NOT DISPLAY"))
    assert operator.store.list_closures()[0]["note"] is None
    assert build_status({"lanes": [lane]}, now=NOW)["codex"]["homes"][0]["closures"][0]["note"] is None


def test_a_hold_and_provider_limit_have_independent_alerts():
    """C-9.6: same scope and end must preserve both kinds of explanation."""
    lane = {**lane_row(), "provider": "claude", "closures": [held(NOTE), held(reason="provider-limit")]}
    alerts = evaluate_conditions({"lanes": [lane]}, now=NOW)
    assert any("held by the operator: " + NOTE in row["body"] for row in alerts)
    assert any("account is closed (provider-limit); resets" in row["body"] for row in alerts)


def test_pin_explanation_keeps_the_note_from_real_admission(operator):
    """C-9.6, C-11.8: scheduler explanations must carry the note through to notices."""
    from subfleet.scheduler import pin_unadmittable
    hold(operator, note=NOTE, until="2027-01-01T00:00:00Z")
    stuck = pin_unadmittable(POLICY, from_store(operator.store, now=NOW),
                             {**JOB, "pinned_lane": "codex-1"})
    assert stuck is not None
    assert f"/homes/codex-1: held by the operator: {NOTE}" in render.pin_notice("job", stuck, None)


def test_note_survives_reopening_and_error_json(operator):
    """C-3.1, C-9.6: the closure row, independent readers and error JSON retain the note."""
    hold(operator, note=NOTE)
    with Store(operator.store.path, read_only=True) as reader:
        assert reader.list_closures()[0]["note"] == NOTE
    result = error_report(operator.store.query, 24, now=NOW + timedelta(hours=1))
    assert result["closures"][0]["note"] == NOTE


@pytest.mark.parametrize("note", [None, NOTE])
def test_hold_acknowledgement_uses_operator_wording(operator, note, monkeypatch, capsys):
    """C-9.6: the hold command's own answer uses its stored note and latest bound."""
    monkeypatch.setattr(cli, "_client", lambda _: SimpleNamespace(
        call=lambda _op, args: daemon.Daemon._hold_lane(operator, protocol.LanesArgs(**args))))
    args = ["lanes", "hold", "codex-1", "--until", UNTIL]
    assert cli.main(args + (["--note", note] if note else [])) == 0
    assert capsys.readouterr().out.strip() == render.closure_text(held(note), home="/homes/codex-1")


def test_note_migrates_an_old_store_without_rewriting_its_hold(tmp_path):
    """C-3.1, C-9.6: version 9 rows keep their evidence and gain a nullable note, once."""
    path = tmp_path / "old.db"
    schema = (Path(__file__).parents[2] / "subfleet/store_schema.sql").read_text().replace("  note TEXT,\n", "")
    with sqlite3.connect(path) as conn:
        conn.executescript(schema)
        conn.execute("INSERT INTO schema_version VALUES (?,?)", (HOLD_NOTE_MIGRATION - 1, iso(NOW)))
        conn.execute("INSERT INTO closures(lane_id,scope,until_at,reason,clock_source,source_event,created_at) "
                     "VALUES ('codex-1','account',?,'operator-hold','reported','operator',?)", (UNTIL, iso(NOW)))
        before = dict(zip([row[1] for row in conn.execute("PRAGMA table_info(closures)")],
                          conn.execute("SELECT * FROM closures").fetchone()))
    for _ in range(2):
        with Store(path) as store:
            [row] = store.list_closures()
            assert row == {**before, "note": None}
            assert store.one("SELECT MAX(version) AS v FROM schema_version")["v"] == SCHEMA_VERSION
            migrations = [json.loads(row["data_json"]) for row in store.list_events() if row["kind"] == "schema.migrated"]
            assert migrations == [{"version": HOLD_NOTE_MIGRATION}]


POLICY = capped(load_policy(DEFAULT_POLICY_PATH))
POLICY["reserve"] = {**POLICY.get("reserve", {}), "models": []}
JOB = {"job_id": "job", "task": None, "tier": "hard", "pinned_model": "astra", "sandbox": "read-only",
       "exclusions": []}


def without_notes(value):
    """Compare admission and its evidence, allowing only the explanatory note to differ."""
    if isinstance(value, dict):
        return {key: without_notes(item) for key, item in value.items() if key != "note"}
    if isinstance(value, (list, tuple)):
        return [without_notes(item) for item in value]
    return value


@settings(max_examples=80, deadline=None)
@example(note="admit immediately", seconds=21600, scope="account", released=False)
@given(note=st.text(st.characters().filter(str.isprintable), max_size=120),
       seconds=st.integers(-3600, 30 * 86400), scope=st.sampled_from(["account", "gpt-6-astra"]),
       released=st.booleans())
def test_any_note_and_hold_leave_admission_equal_to_no_note(note, seconds, scope, released):
    """C-9.6, C-11.2: notes never affect admission, for expired, released and live scoped holds."""
    closure = held(scope=scope, until_at=iso(NOW + timedelta(seconds=seconds)),
                   released_at=iso(NOW) if released else None)
    readings = [{"lane_id": "codex-1", "scope": "account", "window": "seven_day", "utilization": .2,
                 "resets_at": iso(NOW + timedelta(days=2)), "label": "provider", "source": "wham",
                 "observed_at": iso(NOW)}]
    def decision(label):
        view = build_view([lane_row()], readings, [{**closure, "note": label}], now=NOW)
        return evaluate(POLICY, view, JOB)
    original, annotated = decision(None), decision(note)
    assert without_notes(asdict(annotated)) == without_notes(asdict(original))
    if not released and seconds > 0 and scope == "account":
        assert annotated.chosen_lane is None
