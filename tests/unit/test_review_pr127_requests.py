"""Final-text requests are tolerant at the boundary and isolate invalid lines."""
from datetime import UTC, datetime

import pytest

from tests.unit.test_conversation_service import svc, submit  # noqa: F401
from tests.unit.test_conversation_wakes import bound
from subfleet.conversations import wakes


@pytest.mark.parametrize("line", [
    "WAKE-ME: runs= prs=o/r#121", "- WAKE-ME: prs=o/r#121",
    "**WAKE-ME:** prs=o/r#121", "**WAKE-ME: prs=o/r#121**",
    "WAKE-ME: prs=o/r#121\nDONE", "WAKE-ME: prs=o/r#121\nWAITING ON MAX (d001)",
    "WAKE-ME: prs=o/r#121\nHANDED TO hub",
])
def test_final_request_forms_are_accepted(svc, line):
    cid = bound(svc)
    svc.wakes.from_final(cid, "final", line)
    pending = svc.store.query("SELECT * FROM wake_requests WHERE state='pending'")
    assert len(pending) == 1 and pending[0]["kind"] == "pr"


def test_bad_final_line_does_not_discard_the_valid_line(svc):
    cid = bound(svc)
    mid = submit(svc, cid)
    svc.wakes.from_final(cid, mid, 'WAKE-ME: unknown=bad\nWAKE-ME: prs=o/r#121')
    assert svc.store.one("SELECT kind FROM wake_requests")["kind"] == "pr"
    events = svc.store.query("SELECT kind,data_json FROM events WHERE message_id=?", (mid,))
    assert any('wake-refused' in r["data_json"] for r in events)


def test_timer_floor_is_checked_at_turn_start(svc):
    cid = bound(svc)
    mid = submit(svc, cid)
    svc.store._db.execute("UPDATE messages SET created_at=? WHERE message_id=?", (datetime.fromtimestamp(1000, UTC).isoformat(), mid))
    svc.wakes.now = lambda: 1100
    svc.wakes.from_final(cid, mid, "WAKE-ME: at=" + datetime.fromtimestamp(1300, UTC).isoformat())
    assert svc.store.one("SELECT kind FROM wake_requests")["kind"] == "time"


@pytest.mark.parametrize("text", [
    "```text\nWAKE-ME: prs=o/r#1\n```", "~~~\nWAKE-ME: prs=o/r#1\n~~~",
    "```\nWAKE-ME: prs=o/r#1", "> WAKE-ME: prs=o/r#1", "    WAKE-ME: prs=o/r#1",
    "WAKE-ME: prs=o/r#1\nordinary prose", "```\nWAKE-ME: prs=o/r#1\nDONE",
])
def test_only_top_level_final_requests_are_accepted(svc, text):
    cid = bound(svc)
    svc.wakes.from_final(cid, "final", text)
    assert svc.store.query("SELECT * FROM wake_requests") == []


def test_expanded_grammar_replay_preserves_legacy_timer_identity(svc):
    cid = bound(svc)
    mid = submit(svc, cid)
    svc.store._db.execute("UPDATE messages SET created_at=? WHERE message_id=?", (datetime.fromtimestamp(1000, UTC).isoformat(), mid))
    svc.wakes.now = lambda: 1000
    instant = datetime.fromtimestamp(1300, UTC).isoformat()
    svc.wakes.register(cid, f"final:{mid}:0", wakes.normalize(at=instant, now=1000))
    with svc.store.transaction() as tx:
        tx.execute("UPDATE wake_requests SET state='fired'")
    svc.wakes.now = lambda: 1400
    svc.wakes.from_final(cid, mid, "- WAKE-ME: prs=o/r#2\nWAKE-ME: at=" + instant)
    assert svc.store.query("SELECT * FROM wake_requests WHERE state='pending' AND kind='time'") == []
    assert len(svc.store.query("SELECT * FROM wake_requests WHERE state='pending' AND kind='pr'")) == 1
