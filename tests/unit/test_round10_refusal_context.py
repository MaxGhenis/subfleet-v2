"""Checked refusal context keeps the reason and repair without exception text."""
import errno
import json
import logging
import os
import sys
import uuid
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
from tests.unit.test_conversation_service import svc  # noqa: F401


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


@pytest.mark.parametrize("reason,status,fix", [
    ("bad-workspace", "secret-shaped", "choose another folder or use a new scratch folder"),
    ("bad-workspace", "gone", "dismiss it or restore its folder"),
    ("not-a-repository", "no-longer-repository", "dismiss it or restore its checkout"),
    ("protected-workspace", "unreadable-spelling", "make every directory above it searchable, or choose Ask or read-only"),
])
def test_recorded_workspace_refusal_keeps_its_reason_and_repair(reason, status, fix):
    error = suggestions.SuggestionError(reason, "password=SECRET7", fix="password=SECRET7",
                                        details={"workspace_status": status})
    message = suggestions.scrub_error(error)
    assert "SECRET7" not in message
    assert suggestions.error_fix(error) == fix
    assert suggestions.response_reason(message) == reason
    assert suggestions.response_details(message) == {"workspace_status": status}
    assert suggestions.stored_error(message) == message
    assert suggestions.response_reason(message + " SECRET7") is None
    assert suggestions.stored_error(message + " SECRET7") == "Exception: the suggestion operation failed"
    error.details = {"workspace_status": "/tmp/password=SECRET7"}
    assert suggestions.scrub_error(error) == reason + ": " + suggestions.ERROR_MESSAGES[reason]
    assert suggestions.error_fix(error) == suggestions.ERROR_FIXES[reason]


@pytest.mark.parametrize("status", [None, True, [], {}, "gone", "gone\nSECRET7", "no-longer-repository SECRET7"])
def test_unchecked_recorded_workspace_status_is_withheld(status):
    error = suggestions.SuggestionError("not-a-repository", "password=SECRET7",
                                        details={"workspace_status": status})
    assert suggestions.error_details(error) == {}
    assert suggestions.error_fix(error) == suggestions.ERROR_FIXES["not-a-repository"]
    assert suggestions.scrub_error(error) == "not-a-repository: " + suggestions.ERROR_MESSAGES["not-a-repository"]


def test_existing_secret_shaped_workspace_explains_refusal_on_wire_and_cli(svc, tmp_path, monkeypatch, capsys):
    from subfleet import cli
    from subfleet.client import DaemonError
    from tests.unit.test_suggest_cli import run

    workspace = tmp_path / "work password=SECRET7folder"
    workspace.mkdir()
    args = {"request_id": str(uuid.uuid4()), "title": "Fixture", "tldr": "Fixture", "brief": "Fixture",
            "workspace": str(workspace)}
    response = wire_reply(svc, args=args)
    expected = "bad-workspace: the folder's path cannot be used because it looks like it holds a secret"
    assert response["ok"] is False and response["error"]["code"] == 2
    assert response["error"]["message"] == expected
    assert response["error"]["fix"] == "choose another folder or use a new scratch folder"
    assert suggestions.response_details(expected) == {"workspace_status": "secret-shaped"}
    assert str(workspace) not in json.dumps(response) and "SECRET7" not in json.dumps(response)
    assert svc.store.query("SELECT suggestion_id FROM suggestions") == []

    def refuse(*args, **kwargs):
        error = response["error"]
        raise DaemonError(error["code"], error["message"], error["fix"])

    monkeypatch.setattr(cli.Client, "call", refuse)
    brief = tmp_path / "brief.md"
    brief.write_text("Fixture")
    assert run("--title", "Fixture", "--tldr", "Fixture", "--brief-file", str(brief),
               "--workspace", str(workspace)) == 2
    output = capsys.readouterr()
    assert expected in output.err and response["error"]["fix"] in output.err
    assert "SECRET7" not in output.out + output.err


@pytest.mark.parametrize("kind,repair", [("environment", "binaries"), ("config", "configuration")])
def test_real_guard_nontrust_verdict_gives_its_own_repair(tmp_path, monkeypatch, kind, repair):
    from subfleet.guard import preflight as guard

    install_guard(tmp_path)
    monkeypatch.setattr(guard, "_jq_available", lambda: True)
    if kind == "config":
        monkeypatch.setenv(guard.TIMEOUT_ENV, "not-a-number")
    else:
        monkeypatch.delenv(guard.TIMEOUT_ENV, raising=False)
    lane = Lane("codex-1", "codex", "codex:fixture", Credential("codex", str(tmp_path), "home"),
                str(tmp_path), LaneOwner.V2, False)
    verdicts = []
    with pytest.raises(AdapterError) as raised:
        Daemon._guard_override(SimpleNamespace(codex_bin=str(tmp_path / "missing-codex")), lane,
                               str(tmp_path), state_root=tmp_path, recorder=verdicts.append)
    assert verdicts[0].kind == kind
    response = wire_reply(op="submit", error=raised.value)
    message, fix = response["error"]["message"], response["error"]["fix"]
    assert "(" + kind + ")" in message
    assert repair in fix and "subfleet doctor" in fix
    assert all(word not in message + fix for word in ("SHA-256", "TRUST", "pinned Codex version"))


def test_workspace_check_and_create_share_unsearchable_protected_path_repair(svc, tmp_path, monkeypatch):
    from subfleet import folders

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    real_spelling = folders.spelling

    def spelling(path):
        if os.fspath(path) == str(home / ".claude"):
            return str(path), "password=SECRET7unsearchable"
        return real_spelling(path)

    monkeypatch.setattr(folders, "spelling", spelling)
    monkeypatch.setattr(svc, "_person", lambda *args: None)
    args = {"workspace": svc.test_workspace, "permission": "accept-edits"}
    checked = svc.op_workspace_check(args, None)
    with pytest.raises(ConversationError) as raised:
        svc.op_conversation_create({"request_id": str(uuid.uuid4()), "provider": "claude",
                                    "workspace": svc.test_workspace, "confirm_widen": True,
                                    "settings": {"model": "opus", "permission": "accept-edits"}}, None)
    assert raised.value.reason == "protected-workspace"
    assert checked["ok"] is False
    assert checked["fix"] == raised.value.fix == (
        "make every directory above it searchable, or choose Ask or read-only")
    assert "SECRET7" not in json.dumps(checked)


UNREADABLE = "protected-workspace: the folder cannot be checked because a protected path's spelling cannot be read"
SEARCHABLE = "make every directory above it searchable, or choose Ask or read-only"


def test_create_answers_an_unreadable_protected_spelling_with_the_checked_template(svc, tmp_path, monkeypatch):
    """Review r1 of #225, P3 (C-26.10): `conversation.create` put the unreadable path and
    its OS error on the wire. It now answers with `workspace.check`'s checked template;
    its own words go to daemon.log only."""
    from subfleet import folders

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    real_spelling = folders.spelling

    def spelling(path):
        if os.fspath(path) == str(home / ".claude"):
            return str(path), "EACCES"
        return real_spelling(path)

    monkeypatch.setattr(folders, "spelling", spelling)
    monkeypatch.setattr(svc, "_person", lambda *args: None)
    check = wire_reply(svc, op="workspace.check", args={"workspace": svc.test_workspace, "permission": "accept-edits"})
    handler = logging.FileHandler(tmp_path / "daemon.log")
    previous_level = svc.log.level
    svc.log.addHandler(handler)
    svc.log.setLevel(logging.INFO)
    try:
        create = wire_reply(svc, op="conversation.create", args={
            "request_id": str(uuid.uuid4()), "provider": "claude", "workspace": svc.test_workspace,
            "confirm_widen": True, "settings": {"model": "opus", "permission": "accept-edits"}})
    finally:
        svc.log.setLevel(previous_level)
        svc.log.removeHandler(handler)
        handler.close()
    assert check["ok"] is True and check["result"]["ok"] is False
    assert check["result"]["reason"] == UNREADABLE and check["result"]["fix"] == SEARCHABLE
    assert create["ok"] is False and create["error"]["code"] == 7
    assert create["error"]["message"] == UNREADABLE and create["error"]["fix"] == SEARCHABLE
    for reply in (check, create):
        shown = json.dumps(reply)
        assert str(home) not in shown and ".claude" not in shown and "EACCES" not in shown
    logged = (tmp_path / "daemon.log").read_text()
    assert "conversation.create refused" in logged and "cannot tell whether" in logged and "EACCES" in logged


def test_unreadable_spelling_status_round_trips_through_the_cli_decoders():
    """Review r1 of #225, P3: the wire template decodes back to its reason and checked
    status, is what a suggestion stores, and a CLI `DaemonError` carrying it keeps both."""
    from subfleet.client import DaemonError

    error = ConversationError("protected-workspace", "cannot tell what /x/password=SECRET7 contains (EACCES)",
                              code=7, fix="password=SECRET7")
    error.suggestion_details = {"workspace_status": "unreadable-spelling"}
    message = suggestions.scrub_error(error)
    assert message == UNREADABLE and suggestions.error_fix(error) == SEARCHABLE
    assert suggestions.response_reason(message) == "protected-workspace"
    assert suggestions.response_details(message) == {"workspace_status": "unreadable-spelling"}
    assert suggestions.stored_error(message) == message
    daemon_error = DaemonError(7, message, "password=SECRET7")
    assert suggestions.scrub_error(daemon_error) == message
    assert suggestions.error_fix(daemon_error) == SEARCHABLE
    assert suggestions.error_code(daemon_error) == 7
    # The status is checked against its reason: another reason never borrows it.
    forged = suggestions.SuggestionError("bad-workspace", "x", details={"workspace_status": "unreadable-spelling"})
    assert suggestions.scrub_error(forged) == "bad-workspace: " + suggestions.ERROR_MESSAGES["bad-workspace"]


@pytest.mark.parametrize("by_policy", [False, True])
@pytest.mark.parametrize("kind", refusals._choices()["guard_kind"])
def test_every_guard_kind_the_checks_admit_renders_its_repair(kind, by_policy):
    """Review r1 of #225, P3: every kind the checks admit has its repair."""
    details = refusals.by_policy({"guard_kind": kind}, "build") if by_policy else {"guard_kind": kind}
    message, fix = refusals.render("guard-preflight", details)
    assert message.startswith(f"Codex guard preflight refused the writable job ({kind})")
    assert "subfleet doctor" in fix and fix.endswith("pass -s read-only") == by_policy
    assert ("SHA-256" in message) == (kind == "trust")


def test_a_guard_kind_without_a_repair_renders_the_generic_one(monkeypatch):
    """Review r1 of #225, P3: a fifth kind admitted by `_choices` alone must not raise
    inside the renderer; it gets the generic repair and no trust advice."""
    choices = refusals._choices
    monkeypatch.setattr(refusals, "_choices",
                        lambda: {**choices(), "guard_kind": (*choices()["guard_kind"], "fifth")})
    assert refusals.render("guard-preflight", {"guard_kind": "fifth"}) == (
        "Codex guard preflight refused the writable job (fifth)", "rerun subfleet doctor")


def test_real_guard_version_verdict_asks_for_the_supported_version(tmp_path, monkeypatch):
    """Review r1 of #225, P3: a Codex whose `--version` differs from TRUST's pin gets the
    `version` verdict and its own repair through the real preflight, with no trust advice."""
    from subfleet.guard import preflight as guard

    install_guard(tmp_path)
    monkeypatch.setattr(guard, "_jq_available", lambda: True)
    monkeypatch.delenv(guard.TIMEOUT_ENV, raising=False)
    codex = tmp_path / "codex"
    codex.write_text("#!" + sys.executable + "\nprint('codex-cli 0.0.1-fixture')\n")
    codex.chmod(0o755)
    lane = Lane("codex-1", "codex", "codex:fixture", Credential("codex", str(tmp_path), "home"),
                str(tmp_path), LaneOwner.V2, False)
    verdicts = []
    with pytest.raises(AdapterError) as raised:
        Daemon._guard_override(SimpleNamespace(codex_bin=str(codex)), lane, str(tmp_path),
                               state_root=tmp_path, recorder=verdicts.append)
    assert verdicts[0].kind == "version"
    response = wire_reply(op="submit", error=raised.value)
    message, fix = response["error"]["message"], response["error"]["fix"]
    assert message == "Codex guard preflight refused the writable job (version)"
    assert fix == "install the supported Codex version, then rerun subfleet doctor"
