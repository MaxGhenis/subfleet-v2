"""The real Swift timeline must not duplicate the second half of a native turn."""
import pytest

from subfleet.conversations import history
from tests.unit.test_review_pr127_history import MARKERS, transcript
from tests.frontend.conftest import needs_swift, run_probe, write_json

pytestmark = needs_swift


@pytest.mark.parametrize("extra,marker", MARKERS)
def test_mid_turn_markers_do_not_duplicate_or_relabel_the_answer(core_probe, tmp_path, extra, marker):
    path = transcript(tmp_path / "native.jsonl", extra, marker)
    items, _ = history._claude_items(path, None, 50, owned={"owned"})
    receipt = {"message_id": "owned", "conversation_id": "cv", "seq": 1, "origin": "person", "state": "complete",
               "text": "Subfleet question", "created_at": "2026-10-04T00:00:00Z"}
    events = {"events": [{"seq": 1, "conversation_id": "cv", "message_id": "owned", "kind": "text",
                          "ts": "2026-10-04T00:00:03Z", "data": {"block": "answer", "text": "Subfleet answer"}}],
              "next": 1, "reset": False}
    result = run_probe(core_probe, "fold", write_json(tmp_path / "fold.json", {"conversation_id": "cv", "steps": [
        {"receipts": [receipt]}, {"history": {"items": items, "next_before": None}}, {"page": events}]}))
    text = [i.get("text") for i in result["items"]]
    assert text.count("Subfleet answer") == 1, text
    assert text.count("Made in another app") == 1, text  # the genuine outside turn
    assert "Outside answer" in text


def test_invalid_wake_request_is_visible_in_the_timeline(core_probe, tmp_path):
    event = {"seq": 1, "conversation_id": "cv", "message_id": "owned", "kind": "status",
             "data": {"phase": "wake-refused", "detail": "Wake request refused: bad field"}}
    result = run_probe(core_probe, "fold", write_json(tmp_path / "refusal.json", {"conversation_id": "cv", "steps": [
        {"page": {"events": [event], "next": 1, "reset": False}}]}))
    assert any(i.get("text") == "Wake request refused: bad field" for i in result["items"])
