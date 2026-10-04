"""No-follow and content integrity for conversation state (C-24.3, C-25.3)."""

from __future__ import annotations

import json
import hashlib
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet.conversations.launch import TURN_MANIFEST_KEY
from subfleet.conversations.service import ConversationService
from subfleet.conversations.store import ConversationStore

SETTINGS = {"model": "opus", "effort": "high", "fast": False, "permission": "ask"}


@pytest.mark.parametrize("replacement", ["symlink-same", "symlink-other", "changed", "hardlink", "public"])
def test_accepted_message_refuses_replaced_or_changed_text(tmp_path, replacement):
    """C-24.3: the accepted digest is checked against bytes from a private fd;
    neither a link nor another regular file can silently change accepted text.
    """
    store = ConversationStore(tmp_path / "state")
    try:
        conversation, _ = store.create_conversation(
            provider="claude", workspace=str(tmp_path), workspace_kind="in-place", settings=SETTINGS, origin="new")
        message, _ = store.submit_message(
            conversation_id=conversation["conversation_id"], message_id=str(uuid.uuid4()), after_message_id=None,
            text="accepted", attachments=[], settings=SETTINGS)
        path = Path(message["text_path"])
        if replacement.startswith("symlink"):
            target = tmp_path / "private.md"
            target.write_text("accepted" if replacement == "symlink-same" else "unrelated private content")
            target.chmod(0o600)
            path.unlink()
            path.symlink_to(target)
        elif replacement == "changed":
            path.write_text("different contents")
        elif replacement == "hardlink":
            os.link(path, tmp_path / "other-name.md")
        else:
            path.chmod(0o644)
        with pytest.raises(OSError):
            store.message_text(store.message(message["message_id"]))
        assert store.message(message["message_id"])["digest"] == message["digest"]
        assert store.message(message["message_id"])["state"] == "queued"
    finally:
        store.close()


@pytest.mark.parametrize("replacement", ["symlink", "oversized"])
def test_admission_holds_unreadable_manifest_and_can_inspect_the_next_job(tmp_path, replacement):
    """C-25.3: an invalid state file follows the held-job path, leaving admission
    able to inspect another queued job, without following a manifest symlink.
    """
    manifest = {TURN_MANIFEST_KEY: {"conversation_id": "c", "message_id": "m", "provider": "claude"}}
    good = tmp_path / "jobs" / "good" / "manifest.json"
    bad = tmp_path / "jobs" / "bad" / "manifest.json"
    for path in (good, bad):
        path.parent.mkdir(parents=True)
    good.write_text(json.dumps(manifest))
    if replacement == "symlink":
        bad.symlink_to(good)
    else:
        with bad.open("wb") as stream:
            stream.write(json.dumps(manifest).encode())
            stream.write(b" " * (16 * 1024 * 1024))
    service = SimpleNamespace(root=tmp_path, store=SimpleNamespace(one=lambda *args: None, turn_hold=lambda *args: None))
    held = ConversationService.admission_hold(service, {"job_id": "bad"})
    assert held is not None
    assert held["reason"] == "conversation-blocked"
    assert held["conversation_id"] is None
    assert held["error"] == "its turn manifest cannot be read"
    assert ConversationService.admission_hold(service, {"job_id": "good"}) is None


@pytest.mark.parametrize("reader", ["service-json", "models", "runner-json", "reconcile-json", "turn",
                                    "catalog", "relay", "delivery", "codex-stdout"])
def test_state_reader_refuses_symlink_without_changing_native_transcript_readers(tmp_path, reader):
    """C-25.3: state JSON and logs never adopt bytes from a symlink target;
    native transcript symlinks remain a separate explicitly permissive reader.
    """
    from subfleet import relay
    from subfleet.conversations import catalog, classify, reconcile, runner, service
    from subfleet.sessions import transcripts

    content = {"trusted": True}
    data = json.dumps(content).encode()
    if reader in ("relay", "delivery"):
        data = (json.dumps({"kind": "intent", "seq": 1, "tag": "user-message"}) + "\n"
                + json.dumps({"kind": "written", "seq": 1}) + "\n").encode()
    elif reader == "codex-stdout":
        data = json.dumps({"method": "account/rateLimits/updated", "params": {
            "rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 35}}}}).encode()
    target = tmp_path / "other-data"
    target.write_bytes(data)
    paths = {"models": tmp_path / "conversations" / "models.json", "turn": tmp_path / "turn.json",
             "delivery": tmp_path / "stdin.jsonl", "catalog": tmp_path / "catalog.json"}
    path = paths.get(reader, tmp_path / "state-record")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    if reader == "service-json":
        assert service._read_json(path) is None
    elif reader == "models":
        assert ConversationService._catalog_cache(SimpleNamespace(root=tmp_path)) == {}
    elif reader == "runner-json":
        assert runner._read_json(path) is None
    elif reader == "reconcile-json":
        assert reconcile._read_json(path) is None
    elif reader == "turn":
        assert classify.read_turn(tmp_path) is None
    elif reader == "catalog":
        assert catalog.read_catalog(tmp_path)["state"] == "unreadable"
    elif reader == "relay":
        with pytest.raises(OSError):
            relay.read_log(path)
    elif reader == "delivery":
        assert reconcile.frame_status(tmp_path) == "unreadable"
    else:
        assert classify.codex_readings(path, lane_id="codex-1", attempt_id=None) == ([], None)
    assert transcripts.read_regular(path) == data


@pytest.mark.parametrize("replacement", ["symlink", "changed"])
def test_approval_read_refuses_changed_request_instead_of_returning_accepted_digest(tmp_path, replacement):
    """C-25.3, C-27.1: the request a person reviews belongs to the recorded
    approval digest; changing its file cannot present other bytes under it.
    """
    path = tmp_path / "request.json"
    original = b'{"command":"accepted"}'
    path.write_bytes(original)
    path.chmod(0o600)
    approval = {"request_path": str(path), "request_sha256": hashlib.sha256(original).hexdigest(), "nonce": "n"}
    if replacement == "symlink":
        target = tmp_path / "other.json"
        target.write_bytes(original)
        target.chmod(0o600)
        path.unlink()
        path.symlink_to(target)
    else:
        path.write_bytes(b'{"command":"changed"}')
    service = SimpleNamespace(store=SimpleNamespace(approval=lambda _: approval),
                              _person=lambda *args: None, _approval_view=lambda _: {})
    with pytest.raises(OSError):
        ConversationService.op_approval_get(service, {"approval_id": "ap", "reveal": True}, None)
