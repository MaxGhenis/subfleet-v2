"""C-31: chip cards, replayable person choices, and nested child sessions."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import uuid

import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import ServiceHarness
from tests.frontend.test_core_protocol import assert_lossless

pytestmark = needs_swift


@pytest.fixture
def harness():
    fixture = ServiceHarness(Path(tempfile.mkdtemp(prefix="sf-chip-app-", dir="/tmp")))
    yield fixture
    fixture.close()


def suggestion(harness, **changes):
    parent = harness.create(title="Parent")
    message = harness.submit(parent["conversation_id"], "Find related work")
    host = harness.service.chips.host_credentials(parent["conversation_id"], message["message_id"])
    args = dict(conversation_id=parent["conversation_id"], message_id=message["message_id"],
                host_token=host["token"], request_id=str(uuid.uuid4()), title="Add regression coverage",
                tldr="Exercise the edge case in a separate session.", prompt="Read the parser.\nAdd regression coverage.\n")
    args.update(changes)
    chip = harness.call("chip.spawn", **args)["chip"]
    return parent, chip


def fold(core_probe, tmp_path, chip, steps):
    return run_probe(core_probe, "fold", write_json(tmp_path / "fold.json", {
        "conversation_id": chip["parent_conversation_id"], "steps": steps}))


def cards(result):
    return [row["chip"] for row in result["items"] if row["type"] == "chip"]


def events(harness, parent):
    return harness.call("conversation.events", conversation_id=parent["conversation_id"], after=0)


def test_c31_chip_operations_preserve_the_daemons_wire_shapes(core_probe, tmp_path, harness):
    parent, chip = suggestion(harness)
    assert_lossless(core_probe, tmp_path, "chip.spawn", {"chip": chip})
    assert_lossless(core_probe, tmp_path, "chip.list", harness.call("chip.list", conversation_id=parent["conversation_id"]))
    assert_lossless(core_probe, tmp_path, "conversation.open", harness.call("conversation.open", conversation_id=parent["conversation_id"]))
    started = harness.call("chip.start", chip_id=chip["chip_id"])
    assert_lossless(core_probe, tmp_path, "chip.start", started)
    _, withdrawn = suggestion(harness)
    assert_lossless(core_probe, tmp_path, "chip.dismiss", harness.call("chip.dismiss", chip_id=withdrawn["chip_id"], reason="Already covered"))
    assert_lossless(core_probe, tmp_path, "conversation.list", harness.call("conversation.list"))


@pytest.mark.parametrize("choice", ["start", "dismiss"])
def test_c31_snapshot_terminal_state_survives_replayed_creation_and_reset(core_probe, tmp_path, harness, choice):
    parent, chip = suggestion(harness)
    original = events(harness, parent)
    terminal = harness.call("chip." + choice, chip_id=chip["chip_id"])["chip"]
    result = fold(core_probe, tmp_path, chip, [{"chips": [terminal]}, {"page": original},
        {"page": {"events": [], "next": 0, "reset": True, "floor": 999}},
        {"page": {**original, "reset": True, "floor": 999}}])
    assert len(cards(result)) == 1
    assert cards(result)[0] == {key: value for key, value in terminal.items() if value is not None}
    assert cards(result)[0]["prompt"] == chip["prompt"]
    assert result["resets"] == 1


def test_c31_event_card_updates_in_place_without_full_prompt(core_probe, tmp_path, harness):
    parent, chip = suggestion(harness)
    pending = events(harness, parent)
    assert all("prompt" not in event["data"]["chip"] for event in pending["events"] if event["kind"].startswith("chip."))
    harness.call("chip.dismiss", chip_id=chip["chip_id"], reason="Resolved here")
    result = fold(core_probe, tmp_path, chip, [{"page": pending, "snapshot": True}, {"page": events(harness, parent)}])
    assert cards(result["snapshots"][0])[0]["state"] == "pending"
    assert len(cards(result)) == 1 and cards(result)[0]["state"] == "dismissed"
    assert cards(result)[0]["dismissal_reason"] == "Resolved here"
    assert "prompt" not in cards(result)[0]
    assert result["unknown_kinds"] == {}


def test_c31_open_recovers_cards_with_no_retained_events(core_probe, tmp_path, harness):
    parent, chip = suggestion(harness)
    opened = harness.call("conversation.open", conversation_id=parent["conversation_id"])
    result = run_probe(core_probe, "store", write_json(tmp_path / "store.json", {"steps": [{"open": opened}]}))
    assert result["task_chips"][parent["conversation_id"]][0]["prompt"] == chip["prompt"]


def test_c31_children_follow_parent_across_groups_and_filters(core_probe, tmp_path, harness):
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    parent, chip = suggestion(harness, cwd=str(elsewhere))
    child = harness.call("chip.start", chip_id=chip["chip_id"])["conversation"]
    listed = harness.call("conversation.list")
    # The child is new but its root belongs to an older bucket/workspace.
    next(c for c in listed["conversations"] if c["conversation_id"] == parent["conversation_id"])["updated_at"] = "2020-01-01T00:00:00Z"
    def store(*steps):
        return run_probe(core_probe, "store", write_json(tmp_path / "store.json", {
            "now": datetime(2026, 9, 28, tzinfo=timezone.utc).timestamp(), "steps": [{"list": listed}, *steps]}))
    for group in ("recency", "workspace"):
        result = store({"grouping": group})
        assert len(result["sidebar"]) == 1
        rows = result["sidebar"][0]["entries"]
        assert [(r["target"]["conversation"], r["depth"]) for r in rows] == [(parent["conversation_id"], 0), (child["conversation_id"], 1)]
    filtered = store({"search": child["title"]})
    assert filtered["sidebar"][0]["entries"][0]["depth"] == 0


def test_c31_sidebar_cycles_show_each_conversation_once(core_probe, tmp_path, harness):
    parent, chip = suggestion(harness)
    child = harness.call("chip.start", chip_id=chip["chip_id"])["conversation"]
    listed = harness.call("conversation.list")
    next(c for c in listed["conversations"] if c["conversation_id"] == parent["conversation_id"])["parent_conversation_id"] = child["conversation_id"]
    result = run_probe(core_probe, "store", write_json(tmp_path / "store.json", {"steps": [{"list": listed}]}))
    rows = [entry for section in result["sidebar"] for entry in section["entries"]]
    assert len(rows) == 2
    assert sorted(row["depth"] for row in rows) == [0, 1]


@pytest.mark.parametrize("choice", ["start", "dismiss"])
def test_c31_chip_choice_is_journaled_and_replayed_after_restart(core_probe, tmp_path, harness, choice):
    _, chip = suggestion(harness)
    key = "chip." + choice + ":" + chip["chip_id"]
    result = harness.call("chip." + choice, chip_id=chip["chip_id"])
    # Scripted wire replies keep this probe portable in environments that block
    # AF_UNIX bind. The real service above has already committed the lost answer.
    exchanges = write_json(tmp_path / "exchange.json", [{"op": "chip." + choice, "answer": {"v": 1, "id": "fixture", "ok": True, "result": result}}])
    steps = write_json(tmp_path / "steps.json", [{"do": "chip-" + choice, "chip": chip},
        {"do": "begin", "key": key}, {"do": "reload"}, {"do": "pump"}])
    out = run_probe(core_probe, "outbox", "script:" + str(exchanges), tmp_path / "journal.json", steps)
    assert out["entries"][0]["state"] == "acknowledged"
    assert out["entries"][0]["attempts"] == 2
    assert out["results"][2]["states"] == ["queued"]
    assert out["results"][3]["report"]["chips"][0]["state"] == result["chip"]["state"]
    assert out["journal_mode"] == "600"
    if choice == "start":
        assert out["results"][3]["report"]["conversations"] == [result["conversation"]["conversation_id"]]
        assert out["results"][3]["report"]["receipts"][0]["text"] == chip["prompt"]


def test_c31_pending_start_cannot_queue_conflicting_dismiss(core_probe, tmp_path, harness):
    _, chip = suggestion(harness)
    steps = write_json(tmp_path / "steps.json", [{"do": "chip-start", "chip": chip}, {"do": "chip-dismiss", "chip": chip}])
    script = write_json(tmp_path / "exchange.json", [])
    out = run_probe(core_probe, "outbox", "script:" + str(script), tmp_path / "journal.json", steps)
    assert len(out["entries"]) == 1
    assert "error" in out["results"][1]


def test_c31_failed_start_can_retry_the_same_journaled_choice(core_probe, tmp_path, harness):
    _, chip = suggestion(harness)
    started = harness.call("chip.start", chip_id=chip["chip_id"])
    key = "chip.start:" + chip["chip_id"]
    script = write_json(tmp_path / "exchange.json", [
        {"op": "chip.start", "answer": {"v": 1, "id": "", "ok": False,
            "error": {"code": 2, "message": "bad-workspace: task directory no longer exists"}}},
        {"op": "chip.start", "answer": {"v": 1, "id": "fixture", "ok": True, "result": started}},
    ])
    steps = write_json(tmp_path / "steps.json", [
        {"do": "chip-start", "chip": chip}, {"do": "pump"}, {"do": "reload"},
        {"do": "retry", "key": key}, {"do": "pump"},
    ])
    out = run_probe(core_probe, "outbox", "script:" + str(script), tmp_path / "journal.json", steps)
    assert out["results"][1]["report"]["failed"] == [key]
    assert out["results"][2]["states"] == ["failed"]
    assert out["entries"][0]["state"] == "acknowledged"
    assert out["entries"][0]["attempts"] == 2
    assert out["results"][4]["report"]["conversations"] == [started["conversation"]["conversation_id"]]
