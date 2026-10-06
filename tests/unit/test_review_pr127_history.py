"""Synthetic user rows must not divide a Subfleet-owned native turn."""
import json

import pytest
from subfleet.conversations import history


MARKERS = [
    ({}, "[Request interrupted by user]"),
    ({"isCompactSummary": True}, "This session is being continued from a previous conversation that ran out of context."),
    ({}, "<task-notification><task-id>child</task-id></task-notification>"),
]


def transcript(path, extra, marker):
    rows = [
        {"type": "user", "uuid": "owned", "timestamp": "2026-10-04T00:00:00Z", "message": {"content": "Subfleet question"}},
        {"type": "assistant", "uuid": "first", "timestamp": "2026-10-04T00:00:01Z", "message": {"content": "first half"}},
        {"type": "user", "uuid": "synthetic", "timestamp": "2026-10-04T00:00:02Z", **extra, "message": {"content": marker}},
        {"type": "assistant", "uuid": "second", "timestamp": "2026-10-04T00:00:03Z", "message": {"content": "Subfleet answer"}},
        {"type": "user", "uuid": "outside", "timestamp": "2026-10-04T00:00:04Z", "message": {"content": "Outside question"}},
        {"type": "assistant", "uuid": "third", "timestamp": "2026-10-04T00:00:05Z", "message": {"content": "Outside answer"}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


@pytest.mark.parametrize("extra,marker", MARKERS)
def test_synthetic_user_row_preserves_owned_turn(tmp_path, extra, marker):
    path = transcript(tmp_path / "native.jsonl", extra, marker)
    items, _ = history._claude_items(path, None, 50, owned={"owned"})
    assert {i["source"] for i in items if i["id"] in {"first", "synthetic", "second"}} == {"subfleet"}
    assert {i["source"] for i in items if i["id"] in {"outside", "third"}} == {"other-app"}


@pytest.mark.parametrize("extra,marker", MARKERS)
def test_known_prompt_uuid_wins_even_when_its_text_resembles_a_marker(tmp_path, extra, marker):
    path = tmp_path / "native.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in [
        {"type": "user", "uuid": "outside", "message": {"content": "Outside prompt"}},
        {"type": "user", "uuid": "owned", "message": {"content": marker}},
        {"type": "assistant", "uuid": "answer", "message": {"content": "Subfleet answer"}},
    ]) + "\n")
    items, _ = history._claude_items(path, None, 50, owned={"owned"})
    assert items[0]["source"] == items[1]["source"] == "subfleet"
    assert items[2]["source"] == "other-app"
