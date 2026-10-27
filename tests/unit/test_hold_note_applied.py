"""C-9.6, D-RAMP-HOLD-LABEL (review r1 of #268): what a hold call applied, and hold text beside limits.

The acknowledgement and the `lane.held` event describe the stored row on every
path the daemon takes (insert, extend, kept refresh, refusal), the acknowledgement
names its own lane among several held ones, and a hold sharing an end with a
provider limit never hides the limit in `why` or a conversation's wait.
"""

import json
import tempfile
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from hypothesis import example, given, settings, strategies as st

from subfleet import cli, daemon, protocol, render
from subfleet.capacity import from_store
from subfleet.contracts import Credential, Lane, LaneOwner, hold_note_of
from subfleet.conversations import waits
from subfleet.store import Store

NOW = datetime(2026, 10, 26, 16, 55, tzinfo=timezone.utc)
UNTIL = "2026-10-26T22:55:00Z"


def iso(at):
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def lane(lane_id):
    return Lane(lane_id, "codex", f"codex:{lane_id}", Credential("codex", f"/homes/{lane_id}", "home"),
                f"/homes/{lane_id}", LaneOwner.V2, False, True)


def operator_for(store, clock):
    return SimpleNamespace(store=store, _notify=lambda: None, _desktop_identity=lambda: None,
                           _capacity_view=lambda _: from_store(store, now=clock()))


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "utcnow", lambda: iso(NOW))
    with Store(tmp_path / "state.db") as store:
        for lane_id in ("codex-1", "codex-2", "codex-3"):
            store.put_lane(lane(lane_id))
        yield operator_for(store, lambda: NOW)


def hold(operator, lane_id, until, note=None):
    return daemon.Daemon._hold_lane(operator, protocol.LanesArgs(action="hold", lane_id=lane_id, until=until, note=note))


def stored_state(store):
    """Include metadata and event sequence tables when a refused call must write nothing."""
    names = [row["name"] for row in store.query("SELECT name FROM sqlite_master WHERE type='table'")]
    return {name: store.query(f'SELECT * FROM "{name}"') for name in names}


@pytest.mark.parametrize("asked", ["2026-10-26T21:00:00Z", "2026-10-26T23:30:00Z"])
@pytest.mark.parametrize("target", ["codex-1", "codex-2", "codex-3"])
def test_the_acknowledgement_names_its_own_lane_among_several_held(fleet, monkeypatch, capsys, target, asked):
    """Three lanes held with their own ends and notes: the answer carries all three
    rows, and the CLI prints the one for the lane it held, kept or extended."""
    ends = {"codex-1": "2026-10-26T20:00:00Z", "codex-2": "2026-10-26T22:00:00Z", "codex-3": "2026-10-27T01:00:00Z"}
    for lane_id, end in ends.items():
        hold(fleet, lane_id, end, note=f"note of {lane_id}")
    monkeypatch.setattr(cli, "_client", lambda _: SimpleNamespace(
        call=lambda _op, args: json.loads(json.dumps(daemon.Daemon._hold_lane(fleet, protocol.LanesArgs(**args))))))
    assert cli.main(["lanes", "hold", target, "--until", asked, "--note", "asked"]) == 0
    lines = capsys.readouterr().out.splitlines()
    [row] = [item for item in fleet.store.list_closures() if item["lane_id"] == target]
    stored_note = hold_note_of(row["source_event"])
    assert lines[0] == f"/homes/{target}: held by the operator: {stored_note} (until {row['until_at']} at the latest)"
    kept = asked <= ends[target]
    assert (row["until_at"], stored_note) == ((ends[target], f"note of {target}") if kept else (asked, "asked"))
    assert lines[1:] == (["the stored until and note were kept; this call's until and note were not applied"]
                         if kept else [])


def test_why_keeps_the_limit_that_shares_a_holds_end(fleet):
    """`why` words the hold through `_held_text` and still names the provider limit
    ending at the same instant, which the raw label alone carried before."""
    closures = [{"lane_id": "codex-1", "scope": "account", "until_at": UNTIL, "reason": "operator-hold",
                 "clock_source": "reported", "source_event": "operator: ramp bound"},
                {"lane_id": "codex-1", "scope": "account", "until_at": UNTIL, "reason": "provider-limit",
                 "clock_source": "reported", "source_event": "provider-event"}]
    label = f"closed:account:{UNTIL}"
    text = render.why({"evaluations": [{"model": "astra", "closures": closures,
                                        "rejections": [{"lane_id": "codex-1", "reasons": [label, label]}]}]})
    [line] = [line for line in text.splitlines() if line.startswith("  rejected ")]
    assert line == (f"  rejected codex-1: codex-1: held by the operator: ramp bound (until {UNTIL} at the latest), "
                    f"{label}")


def wait_evaluation(*lanes):
    closures = [row for _, rows in lanes for row in rows]
    return {"evaluations": [{"provider": "codex", "closures": closures, "rejections": [
        {"lane_id": lane_id, "reasons": [f"closed:{row['scope']}:{row['until_at']}" for row in rows][:1]}
        for lane_id, rows in lanes]}]}


def closure(lane_id, reason, until, note=None):
    return {"lane_id": lane_id, "scope": "account", "until_at": until, "reason": reason, "clock_source": "reported",
            "source_event": ("operator" + (f": {note}" if note else "")) if reason == "operator-hold" else "provider"}


def test_a_lane_held_and_limited_at_one_end_waits_as_closed():
    """`held` is the kind only when every closure behind the lane's reason is a hold;
    a limit sharing the end keeps the lane `closed`, with its reset."""
    decision = wait_evaluation(("codex-1", [closure("codex-1", "operator-hold", UNTIL, "bound"),
                                            closure("codex-1", "provider-limit", UNTIL)]))
    summary = waits.lane_summary(decision)
    assert summary["lanes"] == {"closed:account": ["codex-1"]} and summary["reset"] == UNTIL
    text = waits.lanes_reason(summary, f"closed:account:{UNTIL}")
    assert text.startswith("closed: every Codex lane that could take it is closed until 2026-10-26 22:55 UTC")
    assert f"codex-1: held by the operator: bound (until {UNTIL} at the latest)" in text


def test_a_holds_end_is_never_a_closed_lanes_reset():
    """A hold's end is a latest bound, not a reset: with one lane held to 20:00 and
    another limited to 23:00, the closed lanes' earliest reset is 23:00."""
    decision = wait_evaluation(("codex-1", [closure("codex-1", "operator-hold", "2026-10-26T20:00:00Z", "bound")]),
                               ("codex-2", [closure("codex-2", "provider-limit", "2026-10-26T23:00:00Z")]))
    summary = waits.lane_summary(decision)
    assert summary["lanes"] == {"operator-hold": ["codex-1"], "closed:account": ["codex-2"]}
    assert summary["reset"] == "2026-10-26T23:00:00Z"
    text = waits.lanes_reason(summary, "closed:account")
    assert "closed until 2026-10-26 23:00 UTC at the earliest" in text and "20:00 UTC" not in text
    assert "1 held by the operator" in text


# --- every hold call, as a model: applied row, event and answer agree --------------------------------------

#: Seconds from now: past, present (a fraction truncates to now), and future ends.
ENDS = st.sampled_from([-3600, -1, 0, 0.4, 1, 3600, 3 * 3600, 6 * 3600, 40 * 86400])
NOTES = st.one_of(st.none(), st.sampled_from(["ramp", "gate", "guard: load 140", "é"]))
STEPS = st.lists(st.one_of(st.tuples(st.just("hold"), ENDS, NOTES),
                           st.tuples(st.just("release"), st.just(0), st.none()),
                           st.tuples(st.just("tick"), st.sampled_from([60, 3 * 3600, 7 * 3600]), st.none())),
                 min_size=1, max_size=8)


def asked_until(now, offset):
    at = now + timedelta(seconds=offset)
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z") if offset % 1 else iso(at)


@settings(max_examples=200, deadline=None, database=None, derandomize=True)
@example(steps=[("hold", 6 * 3600, "ramp"), ("hold", 3600, "gate"), ("hold", 6 * 3600, None),
                ("hold", 0, "now"), ("hold", -1, "past"), ("tick", 7 * 3600, None), ("hold", 3600, "again")])
@example(steps=[("hold", 0.4, "fraction"), ("hold", 1, None), ("release", 0, None), ("hold", 1, "x")])
@given(steps=STEPS)
def test_every_hold_call_applies_records_and_answers_one_row(steps):
    """For any sequence of holds (past, present and future ends, with and without
    notes), releases and clock advances, on a real `Store` through `_hold_lane`:
    a refused end writes nothing; otherwise the open row is the model's (an end
    never shortens, a later end brings its own note), the newest `lane.held`
    event is exactly that row, and the answer's `note` and `kept` describe it."""
    now = NOW
    with tempfile.TemporaryDirectory(prefix="hold-applied-") as directory, ExitStack() as stack:
        stack.enter_context(patch("subfleet.store.utc_now", lambda: iso(now)))
        stack.enter_context(patch.object(daemon, "utcnow", lambda: iso(now)))
        store = stack.enter_context(Store(Path(directory) / "state.db"))
        store.put_lane(lane("codex-1"))
        operator = operator_for(store, lambda: now)
        model = None                                    # the open row: (until, note), or None
        for kind, value, note in steps:
            if kind == "tick":
                now += timedelta(seconds=value)
                continue
            if kind == "release":
                daemon.Daemon._hold_lane(operator, protocol.LanesArgs(action="release", lane_id="codex-1"))
                model = None
                continue
            asked = asked_until(now, value)
            until = iso(datetime.fromisoformat(asked.replace("Z", "+00:00")).replace(microsecond=0))
            before = stored_state(store)
            if until <= iso(now):
                with pytest.raises(protocol.ProtocolError) as refused:
                    hold(operator, "codex-1", asked, note)
                assert (refused.value.code, refused.value.reason) == (2, "hold-until-past")
                assert stored_state(store) == before
                continue
            answer = hold(operator, "codex-1", asked, note)
            if model is None or until > model[0]:
                model, kept = (until, note), False
            else:
                kept = model != (until, note)
            [row] = store.query("SELECT * FROM closures WHERE released_at IS NULL")
            applied = (row["until_at"], hold_note_of(row["source_event"]))
            assert applied == model
            event = store.one("SELECT data_json FROM events WHERE kind='lane.held' ORDER BY event_id DESC LIMIT 1")
            assert json.loads(event["data_json"]) == {"until": model[0], **({"note": model[1]} if model[1] else {})}
            assert (answer["note"], answer["kept"]) == (model[1], kept)
            shown = [item for item in answer["closures"] if item["reason"] == "operator-hold"]
            assert [(item["until_at"], item["note"]) for item in shown] == ([model] if model[0] > iso(now) else [])
