"""C-9.6: closure order and legacy precision cannot change the applied explanation."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from itertools import permutations
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from hypothesis import example, given, settings, strategies as st
import pytest

from subfleet import cli, daemon, protocol, render
from subfleet.capacity import from_store
from subfleet.contracts import Credential, Lane, LaneOwner, hold_note_of
from subfleet.conversations import waits
from subfleet.store import Store

NOW = datetime(2026, 10, 26, 16, 55, tzinfo=timezone.utc)
NOTES = st.sampled_from([None, "ramp", "gate", "guard: load 140", "é"])
CLOSURES = st.lists(st.tuples(
    st.sampled_from(["codex-1", "codex-2", "codex-3"]),
    st.sampled_from(["account", "astra"]),
    st.sampled_from(["operator-hold", "provider-limit", "credits"]),
    st.integers(min_value=1, max_value=3), NOTES,
), min_size=1, max_size=5, unique_by=lambda row: row[:3])


def iso(at):
    return at.isoformat(timespec="seconds").replace("+00:00", "Z")


def closure_rows(specs):
    return [{"lane_id": lane, "scope": scope, "reason": reason,
             "until_at": iso(NOW + timedelta(hours=hours)), "clock_source": "reported",
             "source_event": (f"operator: {note}" if note else "operator")
             if reason == "operator-hold" else "provider-event"}
            for lane, scope, reason, hours, note in specs]


@settings(max_examples=100, deadline=None, database=None, derandomize=True)
@example(specs=[("codex-1", "account", "operator-hold", 1, "ramp"),
                ("codex-1", "account", "provider-limit", 2, None)])
@example(specs=[("codex-1", "account", "operator-hold", 1, None),
                ("codex-1", "account", "provider-limit", 1, None),
                ("codex-2", "astra", "operator-hold", 2, "gate"),
                ("codex-2", "account", "credits", 3, None)])
@example(specs=[("codex-1", "account", "operator-hold", 1, "ramp"),
                ("codex-1", "astra", "operator-hold", 2, "gate"),
                ("codex-2", "account", "operator-hold", 1, None)])
@given(specs=CLOSURES)
def test_every_closure_permutation_keeps_the_same_conversation_explanation(specs):
    """Enumerate every order of each generated set, including rejection/lane order.

    Every hold survives, only provider clocks supply the earliest reset, and
    the pinned entry retains each provider clock as well as every hold.
    """
    rows = closure_rows(specs)
    answers = set()
    pin_answers = set()
    for ordered in permutations(rows):
        by_lane = {}
        for row in ordered:
            by_lane.setdefault(row["lane_id"], []).append(row)
        rejections = [{"lane_id": lane, "reasons": [f"closed:{row['scope']}:{row['until_at']}"
                                                    for row in closures]}
                      for lane, closures in by_lane.items()]
        summary = waits.lane_summary({"evaluations": [
            {"provider": "codex", "closures": ordered, "rejections": rejections}]})
        text = waits.hold_reason({"reason": "closed:account", "lanes": summary}, describe=str, who=str)
        answers.add(text)
        limits = [row["until_at"] for row in rows if row["reason"] != "operator-hold"]
        assert summary["reset"] == (min(limits) if limits else None)
        if limits:
            assert text.startswith("closed:") and waits.when(min(limits)) in text
        else:
            assert text.startswith("held:")
        for lane, closures in by_lane.items():
            expected_kind = "closed:" if any(row["reason"] != "operator-hold" for row in closures) else "operator-hold"
            assert any(lane in ids and key.startswith(expected_kind) for key, ids in summary["lanes"].items())
        pinned = []
        for lane in sorted(by_lane):
            refusal = {"reason": "pin-unadmittable", "lane_id": lane, "closures": by_lane[lane],
                       "reasons": next(row["reasons"] for row in rejections if row["lane_id"] == lane)}
            pin_text = waits.hold_reason(refusal, describe=str, who=str)
            pinned.append(pin_text)
            for row in by_lane[lane]:
                assert row["until_at"] in pin_text
                if row["reason"] == "operator-hold":
                    note = hold_note_of(row["source_event"])
                    if note:
                        assert f"held by the operator: {note} (until {row['until_at']}" in pin_text
                else:
                    assert f"{lane} is closed for {row['scope']}" in pin_text
                    assert row["reason"] in pin_text
        pin_answers.add(tuple(pinned))
        for row in rows:
            if row["reason"] == "operator-hold":
                assert render.closure_text(row, home=row["lane_id"]) in text
                note = hold_note_of(row["source_event"])
                if note:
                    assert f"held by the operator: {note} (until {row['until_at']}" in text
    assert len(answers) == 1
    assert len(pin_answers) == 1


@settings(max_examples=100, deadline=None, database=None, derandomize=True)
@example(offset=3600, micros=500000, zone_quarters=0, naive=False,
         existing=None, previous_note=None, note="ramp", json_flag=False)
@example(offset=3600, micros=1, zone_quarters=4, naive=False,
         existing=7200, previous_note="gate", note="ramp", json_flag=True)
@example(offset=3600, micros=1, zone_quarters=-28, naive=False,
         existing=3600, previous_note="gate", note="ramp", json_flag=False)
@example(offset=3600, micros=999999, zone_quarters=0, naive=True,
         existing=3600, previous_note="ramp", note="ramp", json_flag=True)
@given(offset=st.integers(min_value=-86400, max_value=7 * 86400),
       micros=st.integers(min_value=0, max_value=999999),
       zone_quarters=st.integers(min_value=-48, max_value=56), naive=st.booleans(),
       existing=st.one_of(st.none(), st.integers(min_value=1, max_value=7 * 86400)),
       previous_note=NOTES, note=NOTES, json_flag=st.booleans())
def test_legacy_acknowledgement_kept_requires_the_preexisting_applied_row(
        offset, micros, zone_quarters, naive, existing, previous_note, note, json_flag):
    """Use a real daemon/store behind a #263 response in text and JSON.

    Any kept answer must identify the row that existed before this call;
    fractions, offsets and naive UTC requests alone never imply a kept row.
    """
    requested = NOW + timedelta(seconds=offset, microseconds=micros)
    shown = requested if naive else requested.astimezone(timezone(timedelta(minutes=15 * zone_quarters)))
    asked = shown.replace(tzinfo=None).isoformat() if naive else shown.isoformat()
    normalized = iso(requested.replace(microsecond=0))
    with tempfile.TemporaryDirectory(prefix="hold-ack-") as directory, ExitStack() as stack:
        stack.enter_context(patch.object(daemon, "utcnow", lambda: iso(NOW)))
        store = stack.enter_context(Store(Path(directory) / "state.db"))
        store.put_lane(Lane("codex-1", "codex", "codex:one", Credential("codex", "/homes/codex-1", "home"),
                            "/homes/codex-1", LaneOwner.V2, False, True))
        operator = SimpleNamespace(store=store, _notify=lambda: None, _desktop_identity=lambda: None,
                                   _capacity_view=lambda _: from_store(store, now=NOW))
        before = None
        if existing is not None:
            before = (iso(NOW + timedelta(seconds=existing)), previous_note)
            daemon.Daemon._hold_lane(operator, protocol.LanesArgs(
                action="hold", lane_id="codex-1", until=before[0], note=previous_note))

        def legacy_reply(_op, args):
            answer = daemon.Daemon._hold_lane(operator, protocol.LanesArgs(**args))
            answer.pop("kept")
            answer["note"] = args.get("note")
            return answer

        stack.enter_context(patch.object(cli, "_client", lambda _: SimpleNamespace(call=legacy_reply)))
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["lanes", "hold", "codex-1", "--until", asked]
                          + (["--note", note] if note else []) + (["--json"] if json_flag else []))
        if normalized <= iso(NOW):
            assert rc == 2 and not out.getvalue()
            return
        assert rc == 0, err.getvalue()
        [row] = store.list_closures()
        applied = (row["until_at"], hold_note_of(row["source_event"]))
        kept = (json.loads(out.getvalue())["kept"] if json_flag else "were kept" in out.getvalue())
        expected = before is not None and before[0] >= normalized and before != (normalized, note)
        assert kept == expected
        if kept:
            assert applied == before
        else:
            assert applied == (normalized, note)


@pytest.mark.parametrize("source", [None, "Operator: ramp", "operator: sk-SECRET7" + "x" * 30])
def test_a_present_invalid_pin_source_still_withholds_its_note(source):
    row = {"scope": "account", "until_at": iso(NOW + timedelta(hours=1)),
           "reason": "operator-hold", "source_event": source}
    text = render.pin_failure({"lane_id": "codex-1", "reasons": [f"closed:account:{row['until_at']}"],
                               "closures": [row]}, iso(NOW))
    assert "[note withheld: it would carry a credential]" in text
    if source:
        assert source not in text
