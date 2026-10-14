"""Checked refusal context keeps the reason and repair without exception text."""
import errno
import json
from types import SimpleNamespace

import pytest

from subfleet import host_push, refusals
from subfleet.adapters.base import AdapterError
from subfleet.conversations import suggestions
from subfleet.conversations.store import ConversationError
from subfleet.daemon import Daemon
from subfleet.contracts import Credential, Lane, LaneOwner
from tests.fake.guard import install_guard
from tests.unit.test_protocol_secrecy import wire_reply


@pytest.mark.parametrize("kind,words", [
    ("checkout-missing", "committed Git checkout"),
    ("origin-missing", "origin URL"),
    ("baseline-missing", "HEAD naming a committed baseline"),
    ("shallow", "the checkout is shallow"),
    ("alternates", "objects/info/alternates"),
    ("partial", "partial clone"),
    ("metadata-symlink", "is a symlink"),
    ("metadata-special", "is not a regular file"),
    ("sparse", "sparse objects"),
    ("object-budget", "object copy exceeds"),
])
def test_push_refusal_keeps_its_checked_reason_and_fix(kind, words):
    error = host_push._workspace_refused(host_push.PushError("password=SECRET7", kind=kind))
    response = wire_reply(op="submit", error=error)
    assert response["error"]["code"] == 7
    assert words in response["error"]["message"]
    assert response["error"]["fix"]
    assert "SECRET7" not in json.dumps(response)


@pytest.mark.parametrize("reason", ["push-location", "push-checkout"])
@pytest.mark.parametrize("details", [None, {}, {
    "checkout_problem": "password=SECRET7", "checkout_fix": "password=SECRET7", "errno_name": "SECRET7",
}])
def test_unknown_push_context_uses_the_fixed_fallback(reason, details):
    error = AdapterError("password=SECRET7", reason=reason, details=details, fix="password=SECRET7")
    message, fix = refusals.handler_refusal(error)
    assert "workspace refused" in message and fix
    assert "SECRET7" not in message + fix


def test_metadata_refusal_keeps_a_checked_errno_without_the_filename(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "password=SECRET7", str(tmp_path / "SECRET7"))
    monkeypatch.setattr(host_push.rfs, "read_regular", denied)
    with pytest.raises(host_push.PushError) as raised:
        host_push._read_metadata(tmp_path / "SECRET7", 100)
    message, fix = refusals.handler_refusal(host_push._workspace_refused(raised.value))
    assert "EACCES" in message and "permissions" in fix
    assert "SECRET7" not in message + fix


@pytest.mark.parametrize("name", [".claude", ".codex", ".subfleet", "state root", "lane home"])
def test_protected_folder_context_is_an_allowlisted_name(name):
    error = ConversationError("protected-workspace", "password=SECRET7", code=7)
    error.suggestion_details = {"protected_folder": name}
    message = suggestions.scrub_error(error)
    assert name in message and suggestions.error_fix(error)
    assert "SECRET7" not in message
    assert suggestions.response_reason(message) == "protected-workspace"
    assert suggestions.response_details(message) == {"protected_folder": name}
    assert suggestions.stored_error(message) == message


@pytest.mark.parametrize("name", ["/tmp/password=SECRET7", ".claude\nSECRET7", "SECRET7", None, True, {}])
def test_unchecked_protected_folder_names_are_withheld(name):
    error = ConversationError("protected-workspace", "password=SECRET7", code=7)
    error.suggestion_details = {"protected_folder": name}
    assert suggestions.scrub_error(error) == "protected-workspace: the folder is protected from writable conversations"
    raw = f"protected-workspace: the folder contains protected {name}; writable conversations cannot start here"
    assert suggestions.response_reason(raw) is None
    assert suggestions.stored_error(raw) == "Exception: the suggestion operation failed"


def test_guard_trust_refusal_keeps_the_checked_failure_kind():
    error = AdapterError("password=SECRET7", reason="guard-preflight", details={"guard_kind": "trust"})
    message, fix = refusals.handler_refusal(error)
    assert "SHA-256" in message and "subfleet doctor" in fix
    assert "SECRET7" not in message + fix


def test_actual_guard_hash_mismatch_keeps_the_public_reason_and_fix(tmp_path):
    _, trust = install_guard(tmp_path)
    data = json.loads(trust.read_text())
    data["hook_sha256"] = "0" * 64
    trust.write_text(json.dumps(data))
    lane = Lane("codex-1", "codex", "codex:fixture", Credential("codex", str(tmp_path), "home"),
                str(tmp_path), LaneOwner.V2, False)
    with pytest.raises(AdapterError) as raised:
        Daemon._guard_override(SimpleNamespace(codex_bin="unused"), lane, str(tmp_path), state_root=tmp_path)
    response = wire_reply(op="submit", error=raised.value)
    assert "SHA-256" in response["error"]["message"]
    assert "subfleet doctor" in response["error"]["fix"]


@pytest.mark.parametrize("verb", ["resume", "revive"])
def test_conversation_session_refusal_keeps_a_checked_verb(verb):
    message, fix = refusals.render("conversation-session", {"session_verb": verb})
    assert message.startswith(verb + " refused:") and "Subfleet app" in fix
    message, fix = refusals.render("conversation-session", {"session_verb": "password=SECRET7"})
    assert "SECRET7" not in message + fix
