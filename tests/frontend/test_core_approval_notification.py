"""An Allow once notification cannot approve a replacement or hidden request."""

from copy import deepcopy

import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json

pytestmark = needs_swift


def detail():
    return {
        "approval": {
            "approval_id": "ap-one", "message_id": "msg-one", "conversation_id": "cv-one",
            "kind": "tool", "display": {"tool": "Bash", "command": "pwd"},
            "options": ["allow", "deny"], "created_at": "2026-09-28T12:00:00Z", "state": "pending",
        },
        "request": {"name": "Bash", "input": {"command": "pwd"}},
        "masked": [], "request_sha256": "original-sha", "nonce": "fresh-nonce",
    }


def probe(core_probe, tmp_path, original, *, checks=(), candidates=()):
    return run_probe(core_probe, "approval-notification", write_json(tmp_path / "notification.json", {
        "detail": original, "checks": list(checks), "candidates": list(candidates),
    }))


def test_allow_once_uses_fresh_nonce_for_the_same_request(core_probe, tmp_path):
    original = detail()
    fresh = deepcopy(original)
    fresh["nonce"] = "newly-fetched-nonce"
    result = probe(core_probe, tmp_path, original, checks=[fresh], candidates=[fresh["approval"]])
    assert result["target"] == {
        "conversationID": "cv-one", "messageID": "msg-one", "approvalID": "ap-one",
        "requestSHA256": "original-sha",
    }
    assert result["roundtrip"]
    assert result["can_allow"] == [True]
    assert result["candidate"] == "ap-one"


@pytest.mark.parametrize("field,value", [
    ("state", "answered"),
    ("kind", "question"),
    ("options", ["allow-session", "allow-turn", "deny"]),
    ("masked", [{"path": "input.token", "rule": "token", "length": 24, "sha256": "masked-sha"}]),
])
def test_question_masked_finished_and_broad_permissions_have_no_allow_once(core_probe, tmp_path, field, value):
    original = detail()
    changed = deepcopy(original)
    if field == "masked":
        changed[field] = value
    else:
        changed["approval"][field] = value
    assert probe(core_probe, tmp_path, changed)["target"] is None
    assert probe(core_probe, tmp_path, original, checks=[changed])["can_allow"] == [False]


@pytest.mark.parametrize("field", ["approval_id", "message_id", "conversation_id", "request_sha256"])
def test_notification_action_does_not_follow_a_replacement_request(core_probe, tmp_path, field):
    original = detail()
    replacement = deepcopy(original)
    if field == "request_sha256":
        replacement[field] = "replacement"
    else:
        replacement["approval"][field] = "replacement"
    assert probe(core_probe, tmp_path, original, checks=[replacement])["can_allow"] == [False]


def test_ambiguous_pending_requests_do_not_offer_a_notification_action(core_probe, tmp_path):
    original = detail()
    other = deepcopy(original["approval"])
    other["approval_id"] = "ap-two"
    assert probe(core_probe, tmp_path, original, candidates=[original["approval"], other])["candidate"] is None
    other["message_id"] = "older-message"
    assert probe(core_probe, tmp_path, original, candidates=[original["approval"], other])["candidate"] == "ap-one"
    assert probe(core_probe, tmp_path, original, candidates=[other])["candidate"] is None
