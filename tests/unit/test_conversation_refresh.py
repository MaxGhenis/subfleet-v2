"""The cheap focused-conversation refresh never reads the native transcript."""
from tests.frontend.daemon_harness import ServiceHarness
from subfleet import protocol


def test_status_returns_state_and_every_message_id_without_history(tmp_path, monkeypatch):
    h = ServiceHarness(tmp_path)
    try:
        cid = h.create()["conversation_id"]
        first = h.submit(cid, "first")["message_id"]
        second = h.submit(cid, "second", after=first)["message_id"]
        h.store.set_state(first, "complete")
        h.store.update_conversation(cid, blocked_by="unfinished-turn")
        def forbidden(*args, **kwargs):
            raise AssertionError("status must not read transcript or message text")
        monkeypatch.setattr(h.service, "op_conversation_history", forbidden)
        monkeypatch.setattr(h.store, "message_text", forbidden)
        result = h.call("conversation.status", conversation_id=cid)
        assert result["conversation"]["blocked_by"] == "unfinished-turn"
        assert result["message_ids"] == [first, second]
        assert "history" not in result and "messages" not in result
        assert "conversation.status" in protocol.CONVERSATION_OPS
    finally:
        h.close()


def test_receipt_exposes_the_job_binding_not_the_dispatch_claim(tmp_path):
    h = ServiceHarness(tmp_path)
    try:
        cid = h.create()["conversation_id"]
        mid = h.submit(cid, "message")["message_id"]
        h.store.set_state(mid, "waiting", reason="dispatching")
        claim = h.call("message.status", message_ids=[mid])["messages"][0]
        assert claim["job_id"] is None
        h.store.set_state(mid, "waiting", reason="admission: waiting", job_id="test-job")
        bound = h.call("message.status", message_ids=[mid])["messages"][0]
        assert bound["job_id"] == "test-job"
    finally:
        h.close()
