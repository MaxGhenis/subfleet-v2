"""C-9.6, D-RAMP-HOLD-LABEL: a six-hour safety bound is an operator hold."""

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from hypothesis import example, given, settings, strategies as st

from subfleet import cli, daemon, protocol, render, scheduler
from subfleet.alerts import Alerts, evaluate_conditions
from subfleet.capacity import build_view, from_store
from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner
from subfleet.conversations import waits
from subfleet.operations import brief, error_report
from subfleet.offline import Offline
from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
from subfleet.scheduler import evaluate
from subfleet.status_json import build_status
from subfleet.store import Store
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
            "clock_source": "reported", "source_event": "operator" if note is None else f"operator: {note}", "note": note, **changes}


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
                {"scope": "account", "reason": "operator-hold", "until_at": "2026-10-26T22:55:00Z",
                 "source_event": "operator"}]}
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
    assert from_store(operator.store, now=NOW)["lanes"][0]["closures"][0]["source_event"] == f"operator: {NOTE}"
    assert cli.main(["lanes", "release", "codex-1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["closures"] == []
    [released] = operator.store.list_closures()
    assert released["source_event"] == f"operator: {NOTE}" and released["released_at"] == iso(NOW)


@pytest.mark.parametrize("note", [None, "new operator decision"])
def test_replacing_hold_takes_new_calls_note_or_none(operator, note):
    """C-9.6: an extending call replaces the row's metadata; it never inherits an omitted note."""
    hold(operator, note=NOTE)
    result = hold(operator, note=note, until="2026-10-26T23:55:00Z")
    assert len(result["closures"]) == 1
    assert result["closures"][0]["note"] == note
    daemon.Daemon._hold_lane(operator, protocol.LanesArgs(action="release", lane_id="codex-1"))
    hold(operator, note=note)
    assert operator.store.list_closures()[1]["source_event"] == ("operator" if note is None else f"operator: {note}")


@pytest.mark.parametrize("note", [None, NOTE])
@pytest.mark.parametrize("site", ["alert", "notice", "status", "capacity", "lanes", "why", "runs",
                                  "pin-notice", "pin-failure", "brief", "conversation", "why-rejected", "why-queue", "why-retry"])
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
        text = format_status({**view, "alerts": evaluate_conditions(view, now=NOW)})
    elif site == "capacity":
        text = render.status(view)
    elif site == "lanes":
        text = cli._format_lanes(view)
    elif site == "why":
        text = render.why({"evaluations": [{"model": "astra", "closures": [closure]}]})
    elif site == "why-rejected":
        text = render.why({"evaluations": [{"model": "astra", "closures": [closure],
            "rejections": [{"lane_id": "codex-1", "reasons": stuck["reasons"]}]}]})
        text = next(line for line in text.splitlines() if "rejected codex-1:" in line)
        assert f"closed:account:{UNTIL}" not in text
    elif site in ("why-queue", "why-retry"):
        text = "\n".join(render.why_queue({"job_id": "job", "state": "waiting", "hold": {
            **stuck, "reason": "pin-unadmittable" if site == "why-queue" else "lanes-unadmittable"}}))
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
        assert text.startswith("held:")
    else:
        text = brief(view)
    expected = "held by the operator" + (f": {note}" if note else "") + f" (until {UNTIL} at the latest)"
    assert expected in text
    if site == "status":
        before, separator, after = text.partition("\nclosures\n")
        assert separator and "/homes/codex-1: " + expected in before
        assert "/homes/codex-1: " + expected in after
    if site not in ("why", "why-rejected", "conversation"):
        assert "/homes/codex-1: " + expected in text
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
    assert "DO NOT DISPLAY" not in json.dumps(build_status({"lanes": [lane]}, now=NOW))


@pytest.mark.parametrize("note", [None, NOTE])
def test_pin_explanation_keeps_the_note_from_real_admission(operator, note):
    """C-9.6, C-11.8: scheduler explanations must carry the note through to notices."""
    from subfleet.scheduler import pin_unadmittable
    hold(operator, note=note, until="2027-01-01T00:00:00Z")
    stuck = pin_unadmittable(POLICY, from_store(operator.store, now=NOW),
                             {**JOB, "pinned_lane": "codex-1"})
    assert stuck is not None
    expected = "/homes/codex-1: held by the operator" + (f": {note}" if note else "")
    assert expected + " (until 2027-01-01T00:00:00Z at the latest)" in render.pin_notice("job", stuck, None)


@pytest.mark.parametrize("note", [None, NOTE])
def test_hold_acknowledgement_uses_operator_wording(operator, note, monkeypatch, capsys):
    """C-9.6: the hold command's own answer uses its stored note and latest bound."""
    monkeypatch.setattr(cli, "_client", lambda _: SimpleNamespace(
        call=lambda _op, args: daemon.Daemon._hold_lane(operator, protocol.LanesArgs(**args))))
    args = ["lanes", "hold", "codex-1", "--until", UNTIL]
    assert cli.main(args + (["--note", note] if note else [])) == 0
    assert capsys.readouterr().out.strip() == render.closure_text(held(note), home="/homes/codex-1")




POLICY = capped(load_policy(DEFAULT_POLICY_PATH))
POLICY["reserve"] = {**POLICY.get("reserve", {}), "models": []}
JOB = {"job_id": "job", "task": None, "tier": "hard", "pinned_model": "astra", "sandbox": "read-only",
       "exclusions": []}


@pytest.mark.parametrize("note", [None, NOTE])
def test_pin_hold_and_limit_are_separate_and_limit_sentence_is_unchanged(note):
    """C-11.8: a shared end must not merge the operator's hold with a provider's limit."""
    stuck = {"lane_id": "codex-1", "home": "/homes/codex-1",
             "reasons": [f"closed:account:{UNTIL}", f"closed:account:{UNTIL}"],
             "closures": [held(note), held(reason="provider-limit", source_event="provider-event")]}
    expected = "/homes/codex-1: held by the operator" + (f": {note}" if note else "")
    assert render.pin_refusals(stuck) == (expected + f" (until {UNTIL} at the latest); "
        f"codex-1 is closed for account (provider-limit) until {UNTIL}")


@pytest.mark.parametrize("until", ["2020-01-01T00:00:00Z", iso(NOW)])
def test_past_or_present_until_is_refused_without_writes(operator, until):
    """C-9.6: the daemon refuses an end that cannot put a hold in force."""
    before = operator.store.list_events()
    with pytest.raises(protocol.ProtocolError) as error:
        hold(operator, note="past", until=until)
    assert error.value.code == 2 and error.value.reason == "hold-until-past"
    assert "future" in protocol.refusal_text(error.value)[0]
    assert operator.store.list_closures() == []
    assert operator.store.list_events() == before


@pytest.mark.parametrize("refresh_until", [UNTIL, "2026-10-26T21:55:00Z"])
@pytest.mark.parametrize("note", [None, "asked note"])
def test_kept_refresh_acknowledges_stored_until_and_note(operator, monkeypatch, capsys, refresh_until, note):
    """C-9.6: a non-extending call prints what remains in force and explains what was kept."""
    hold(operator, note=NOTE)
    monkeypatch.setattr(cli, "_client", lambda _: SimpleNamespace(
        call=lambda _op, args: daemon.Daemon._hold_lane(operator, protocol.LanesArgs(**args))))
    argv = ["lanes", "hold", "codex-1", "--until", refresh_until] + (["--note", note] if note else [])
    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == (f"/homes/codex-1: held by the operator: {NOTE} "
                                           f"(until {UNTIL} at the latest)")
    assert "stored until and note were kept" in captured.out
    assert "not applied" in captured.out
    assert "older" not in captured.err


@pytest.mark.parametrize("note", [None, "asked note"])
def test_kept_refresh_reply_and_event_record_applied_values(operator, note):
    """C-9.6: audit data and reply describe the row kept by Store.put_closure."""
    hold(operator, note=NOTE)
    result = hold(operator, note=note, until="2026-10-26T21:55:00Z")
    assert result["note"] == NOTE
    event = operator.store.one("SELECT data_json FROM events WHERE kind='lane.held' ORDER BY event_id DESC LIMIT 1")
    assert json.loads(event["data_json"]) == {"until": UNTIL, "note": NOTE}


@pytest.mark.parametrize("as_json", [False, True])
def test_old_daemon_missing_note_field_warns(operator, monkeypatch, capsys, as_json):
    """C-16.2: an older daemon can apply the hold while silently dropping its note."""
    response = {"held": "codex-1", "lanes": [lane_row()], "closures": [held()]}
    response["closures"][0].pop("note")
    monkeypatch.setattr(cli, "_client", lambda _: SimpleNamespace(call=lambda *_: response))
    assert cli.main(["lanes", "hold", "codex-1", "--until", UNTIL, "--note", NOTE] +
                    (["--json"] if as_json else [])) == 69
    assert "without its note" in capsys.readouterr().err


@pytest.mark.parametrize("as_json", [False, True])
def test_missing_stored_row_never_acknowledges_arguments(operator, monkeypatch, capsys, as_json):
    """C-9.6: a missing returned closure cannot be replaced by the CLI's requested hold."""
    monkeypatch.setattr(cli, "_client", lambda _: SimpleNamespace(
        call=lambda *_: {"held": "codex-1", "note": NOTE, "lanes": [lane_row()], "closures": []}))
    assert cli.main(["lanes", "hold", "codex-1", "--until", UNTIL, "--note", NOTE] +
                    (["--json"] if as_json else [])) != 0
    result = capsys.readouterr()
    assert "held by the operator" not in result.out
    assert "not in force" in result.err


R1_BAD_NOTES = ["password=SECRET7-left", "Bearer SECRET7abc…xyz0123",
                "see https://u:SECRET7pw@example.invalid/x", "   ", ""]
R1_CONTROLS = ["\n", "\r", "\t", "\0", "\x1b", "\x7f", "\x85", "\u2028", "\u2029",
               "\u202e", "\u202d", "\u2067", "\u2069", "\u200e", "\u061c", "\u200b", "\u200d",
               "\ufeff", "\xa0", "\ud800"]


@pytest.mark.parametrize("note", R1_BAD_NOTES + [f"first{char}last" for char in R1_CONTROLS])
def test_r1_notes_are_refused_by_cli_and_daemon_without_writes(operator, monkeypatch, capsys, note):
    """C-9.6: #263 already rejects r1's credentials, blanks and nonprintable inputs."""
    monkeypatch.setattr(cli, "_client", lambda _: pytest.fail("invalid note reached transport"))
    assert cli.main(["lanes", "hold", "codex-1", "--until", UNTIL, "--note", note]) == 2
    capsys.readouterr()
    before = operator.store.list_events()
    with pytest.raises(protocol.ProtocolError) as error:
        hold(operator, note=note)
    assert error.value.reason == "hold-note"
    assert operator.store.list_closures() == []
    assert operator.store.list_events() == before


def test_real_pin_explanation_withholds_an_external_credential_source(operator):
    """C-9.6: carrying hold metadata into pin notices preserves #263's strict reader."""
    hold(operator, note=NOTE, until="2027-01-01T00:00:00Z")
    with operator.store.transaction() as tx:
        tx.execute("UPDATE closures SET source_event=?", ("operator: password=SECRET7-left",))
    stuck = scheduler.pin_unadmittable(POLICY, from_store(operator.store, now=NOW),
                                       {**JOB, "pinned_lane": "codex-1"})
    assert "SECRET7" not in json.dumps(stuck)
    assert "/homes/codex-1: held by the operator: [note withheld: it would carry a credential]" in (
        render.pin_notice("job", stuck, None))
