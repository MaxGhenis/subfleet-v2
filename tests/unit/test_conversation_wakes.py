"""C-24.10: durable wakes and races against personal input and ownership."""
import json
import time
import uuid
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet.conversations import catalog, wakes
from subfleet.conversations.store import ConversationError
from tests.unit.test_conversation_service import SETTINGS, conversation, submit, svc  # noqa: F401


def bound(svc):
    return conversation(svc, native_session_id=str(uuid.uuid4()))


def register(svc, cid, **args):
    return svc.wakes.register(cid, str(uuid.uuid4()), wakes.normalize(**args, now=svc.wakes.now()))


def rows(svc, cid):
    return svc.store.query("SELECT * FROM messages WHERE conversation_id=? AND origin='wake' ORDER BY seq", (cid,))


def finish(svc, cid, ids=("run-one",)):
    sid = svc.store.conversation(cid)["native_session_id"]
    for job_id in ids:
        svc.daemon.store.add_job(job_id=job_id, request_id=job_id, payload_digest="fixture", kind="dispatch",
                                caller_session=sid, state="succeeded", out_path=f"/work/{job_id}.md",
                                workdir=svc.test_workspace, prompt_path="fixture.md", sandbox="read-only")
    svc.daemon.store.add_job(job_id=f"parent-{cid}", request_id=f"parent-{cid}", payload_digest="fixture", kind="turn",
                            name=f"turn-{cid}", state="succeeded", created_at=svc.store.conversation(cid)["created_at"],
                            workdir=svc.test_workspace, prompt_path="fixture.md", sandbox="read-only")


def settle_all(svc, cid):
    for m in rows(svc, cid):
        svc.store.set_state(m["message_id"], "complete")


def test_completion_batches_once_and_restarts(svc):
    cid = bound(svc)
    finish(svc, cid, ("r1", "r2"))
    svc.wakes.tick()
    assert len(rows(svc, cid)) == 1
    message = svc.store.message(rows(svc, cid)[0]["message_id"])
    assert svc.store.message_text(message) == "[Subfleet]\nr1 finished: succeeded; deliverable /work/r1.md\nr2 finished: succeeded; deliverable /work/r2.md"
    settle_all(svc, cid)
    wakes.WakeEngine(svc).tick()
    assert len(rows(svc, cid)) == 1


def test_final_grammar_and_completion_are_one_message(svc):
    cid = bound(svc)
    finish(svc, cid)
    text = 'Work dispatched.\nWAKE-ME: runs=run-one note="Inspect the result"'
    svc.wakes.from_final(cid, "final-message", text)
    svc.wakes.from_final(cid, "final-message", text)
    svc.wakes.tick()
    assert len(rows(svc, cid)) == 1
    assert "Inspect the result" in svc.store.message_text(svc.store.message(rows(svc, cid)[0]["message_id"]))
    assert svc.store.one("SELECT state FROM wake_requests")["state"] == "fired"


@pytest.mark.parametrize("text", ["WAKE-ME: x=no", "WAKE-ME: runs=a runs=b", "WAKE-ME: runs=", 'WAKE-ME: note="broken'])
def test_invalid_final_grammar_is_refused(text):
    with pytest.raises((ConversationError, ValueError)):
        wakes.trailing_requests(text)


def test_only_trailing_lines_and_timer_floor():
    assert wakes.trailing_requests("WAKE-ME: runs=a\nmore prose") == []
    assert len(wakes.trailing_requests('Done\nWAKE-ME: runs=a,b prs=x/y#12\nWAKE-ME: at=2026-12-01T00:00:00Z')) == 2
    with pytest.raises(ConversationError):
        wakes.normalize(at="2026-10-04T00:04:59Z", now=datetime(2026, 10, 4, tzinfo=UTC).timestamp())
    with pytest.raises(ConversationError):
        wakes.normalize(at="2026-10-04T00:05:00", now=0)


def test_time_replacement_and_restart(svc):
    cid = bound(svc)
    svc.wakes.now = lambda: 1000
    at = lambda seconds: datetime.fromtimestamp(seconds, UTC).isoformat()
    first = register(svc, cid, at=at(1300))
    second = register(svc, cid, at=at(1600))
    assert first != second
    svc.wakes.now = lambda: 1300
    svc.wakes.tick()
    assert rows(svc, cid) == []
    engine = wakes.WakeEngine(svc)
    engine.now = lambda: 1600
    engine.tick()
    engine.tick()
    assert len(rows(svc, cid)) == 1
    assert svc.store.query("SELECT state FROM wake_requests ORDER BY created_at") == [{"state": "superseded"}, {"state": "fired"}]


def test_prs_one_query_per_minute_for_all_conversations_and_hold_latches_event(svc, monkeypatch):
    cids = [bound(svc), bound(svc)]
    svc.wakes.now = lambda: 1000
    for cid in cids:
        register(svc, cid, prs=["o/r#1", "o/r#2"])
    query = Mock(return_value={p: {"state": "OPEN", "checks": [], "reviews": []} for p in ["o/r#1", "o/r#2"]})
    monkeypatch.setattr(wakes, "query_prs", query)
    svc.wakes.tick()
    svc.wakes.tick()
    assert query.call_count == 1 and query.call_args.args[0] == ["o/r#1", "o/r#2"]
    svc.store.update_conversation(cids[0], blocked_by="unfinished-turn")
    svc.wakes.now = lambda: 1060
    query.return_value["o/r#1"] = {"state": "MERGED", "checks": [], "reviews": []}
    svc.wakes.tick()
    assert len(rows(svc, cids[0])) == 0 and len(rows(svc, cids[1])) == 1
    svc.store.update_conversation(cids[0], blocked_by=None)
    svc.wakes.tick()
    assert len(rows(svc, cids[0])) == 1 and query.call_count == 2


@pytest.mark.parametrize("after", [
    {"state": "MERGED", "checks": [], "reviews": []},
    {"state": "CLOSED", "checks": [], "reviews": []},
    {"state": "OPEN", "checks": [["COMPLETED", "test", "SUCCESS", "now", "head"]], "reviews": []},
    {"state": "OPEN", "checks": [], "reviews": ["review-1"]},
])
def test_pr_event_kinds(after):
    assert wakes.pr_changed({"state": "OPEN", "checks": [], "reviews": []}, after)
    assert not wakes.pr_changed(after, after)


def test_person_races_publication_and_wake_yields_before_attempt(svc, monkeypatch):
    cid = bound(svc)
    finish(svc, cid)
    publish = svc.store._publish
    accepted = []
    def race(path, data):
        publish(path, data)
        if data.startswith(b"[Subfleet]"):
            accepted.append(submit(svc, cid, "human first"))
    monkeypatch.setattr(svc.store, "_publish", race)
    svc.wakes.tick()
    assert rows(svc, cid) == []
    assert svc.store.next_dispatchable(cid)[0]["message_id"] == accepted[0]
    monkeypatch.setattr(svc.store, "_publish", publish)
    svc.store.set_state(accepted[0], "complete")
    svc.wakes.tick()
    svc._dispatch()
    wake = svc.store.message(rows(svc, cid)[0]["message_id"])
    assert wake["state"] == "waiting"
    human = svc.op_message_submit({"conversation_id": cid, "message_id": str(uuid.uuid4()),
                                   "after_message_id": accepted[0], "text": "human next"}, None)
    assert svc.store.message(wake["message_id"])["state"] == "queued"
    assert svc.store.next_dispatchable(cid)[0]["message_id"] == human["message_id"]
    svc.store.set_state(human["message_id"], "complete")
    assert svc.store.next_dispatchable(cid)[0]["message_id"] == wake["message_id"]


def test_one_request_with_multiple_kinds_fires_once(svc):
    cid = bound(svc)
    svc.wakes.now = lambda: 1000
    register(svc, cid, prs=["o/r#1"], at=datetime.fromtimestamp(1300, UTC).isoformat())
    with svc.store.transaction() as tx:
        tx.execute("UPDATE wake_requests SET ready_json='[\"o/r#1\"]' WHERE conversation_id=? AND kind='pr'", (cid,))
    svc.wakes.tick()
    settle_all(svc, cid)
    svc.wakes.now = lambda: 1400
    svc.wakes.tick()
    assert len(rows(svc, cid)) == 1
    assert all(r["state"] == "fired" for r in svc.store.query("SELECT state FROM wake_requests WHERE conversation_id=?", (cid,)))


def test_throttle_eight_and_cooldown(svc):
    cid = bound(svc)
    svc.wakes.now = lambda: 1000
    register(svc, cid, prs=["o/r#1"])
    with svc.store.transaction() as tx:
        tx.execute("UPDATE conversations SET wake_streak=8,last_wake_at=1000 WHERE conversation_id=?", (cid,))
        tx.execute("UPDATE wake_requests SET ready_json='[\"o/r#1\"]' WHERE conversation_id=?", (cid,))
    svc.wakes.tick()
    assert rows(svc, cid) == []
    svc.wakes.now = lambda: 1000 + wakes.COOLDOWN_S
    svc.wakes.tick()
    assert len(rows(svc, cid)) == 1


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(held=st.sampled_from(["blocked", "archived", "person", "legacy", "live"]), cycles=st.integers(1, 12))
def test_property_no_wake_into_held_conversation(svc, held, cycles):
    cid = bound(svc)
    register(svc, cid, prs=["o/r#9"])
    with svc.store.transaction() as tx:
        tx.execute("UPDATE wake_requests SET ready_json='[\"o/r#9\"]' WHERE conversation_id=?", (cid,))
    if held == "blocked":
        svc.store.update_conversation(cid, blocked_by="unfinished-turn")
    elif held == "archived":
        svc.store.update_conversation(cid, archived_at="now")
    elif held == "legacy":
        svc.store.set_legacy_hold(cid, "live-cockpit")
    else:
        mid = submit(svc, cid)
        if held == "live":
            svc.store.set_state(mid, "running")
    for _ in range(cycles):
        svc.wakes.tick()
    assert rows(svc, cid) == []


@settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(count=st.integers(9, 12), repeats=st.integers(1, 3))
def test_property_at_most_once_and_throttle_and_person_fifo(svc, count, repeats):
    cid = bound(svc)
    svc.wakes.now = lambda: 1000
    previous = None
    for index in range(count):
        register(svc, cid, prs=[f"o/r#{index+1}"])
        with svc.store.transaction() as tx:
            tx.execute("UPDATE wake_requests SET ready_json='[\"o/r#1\"]' WHERE conversation_id=? AND state='pending'", (cid,))
        for _ in range(repeats):
            svc.wakes.tick()
        settle_all(svc, cid)
        for _ in range(repeats):
            svc.wakes.tick()
    assert len(rows(svc, cid)) == wakes.MAX_STREAK
    svc.wakes.now = lambda: 1000 + wakes.COOLDOWN_S
    svc.wakes.tick()
    assert len(rows(svc, cid)) == wakes.MAX_STREAK + 1
    settle_all(svc, cid)
    humans = []
    for index in range(3):
        previous = submit(svc, cid, f"person {index}", after=previous)
        humans.append(previous)
        svc.wakes.tick()
    assert svc.store.conversation(cid)["wake_streak"] == 0
    for mid in humans:
        assert svc.store.next_dispatchable(cid)[0]["message_id"] == mid
        svc.store.set_state(mid, "complete")


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(delta=st.integers(-10, 10), live=st.booleans())
def test_moot_block_differential_against_dispatch_live_writer_check(svc, monkeypatch, delta, live):
    cid = bound(svc)
    svc.store.update_conversation(cid, blocked_by="unfinished-turn")
    c = svc.store.conversation(cid)
    timestamp = datetime.fromisoformat(c["blocked_at"].replace("Z", "+00:00")).timestamp()
    path = svc.root / "native.jsonl"
    path.write_text('{}\n')
    (svc.root / "catalog.json").write_text(json.dumps({"native_records": {
        f"claude:{c['native_session_id']}": {"path": str(path), "mtime": timestamp + delta}}}))
    holder = Mock(return_value=[123] if live else [])
    monkeypatch.setattr(catalog, "external_writers", holder)
    svc._moot_blocks()
    expected = delta > 0 and not live
    assert (svc.store.conversation(cid)["blocked_by"] is None) == expected
    event = svc.store.query("SELECT kind,data_json FROM events WHERE conversation_id=?", (cid,))
    assert len(event) == int(expected)
    if expected:
        assert json.loads(event[0]["data_json"])["reason"] == "continued-elsewhere"
    if delta > 0:
        holder.assert_called_with(c["native_session_id"])
