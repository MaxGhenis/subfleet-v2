"""Apply the deliverable CoS patch to a pinned poller; never use the live gateway."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/phone/cos"
PATCH = REPO / "docs/desktop/phone/cos-tg-poller.patch"


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def tap(data="sf:opaque:a0", *, owner=42, update_id=10):
    return {"update_id": update_id, "callback_query": {
        "id": "callback-1", "data": data,
        "message": {"chat": {"id": owner}, "message_id": 701}}}


def message(text="go ahead", *, owner=42, card=701, edited=False, update_id=11):
    payload = {"chat": {"id": owner}, "date": 1, "text": text}
    if card is not None:
        payload["reply_to_message"] = {"message_id": card}
    return {"update_id": update_id, "edited_message" if edited else "message": payload}


@pytest.fixture
def gateway(tmp_path):
    home = tmp_path / "cos"
    shutil.copytree(FIXTURE, home)
    applied = subprocess.run(["patch", "--batch", "-p1", "-i", str(PATCH)],
                             cwd=home, text=True, capture_output=True, timeout=10)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert "fuzz" not in applied.stdout.lower()
    cli = home / "bin/subfleet"
    cli.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["PHONE_CALLS"], "a") as stream:
    stream.write(json.dumps(args) + "\\n")
assert args[0] == "phone"
if args[1] == "owns":
    if os.environ.get("PHONE_OWNS_ERROR"):
        print("daemon unavailable", file=sys.stderr)
        sys.exit(69)
    sys.exit(0 if args[2] == "701" else 1)
if os.environ.get("PHONE_ACTION_ERROR"):
    print("approval is no longer pending", file=sys.stderr)
    sys.exit(2)
print("answered" if args[1] == "tap" else "queued")
''')
    cli.chmod(0o755)
    decisions = home / "bin/decisions"
    decisions.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
with open(os.environ["DECISION_CALLS"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
print("recorded")
''')
    decisions.chmod(0o755)
    environment = {**os.environ, "COS_HOME": str(home),
                   "SAY_TRANSPORT": f"file:{home}/api.jsonl", "SAY_CHAT_ID": "42",
                   "TG_INBOUND_DIR": str(home / "inbound"), "SUBFLEET_BIN": str(cli),
                   "PHONE_CALLS": str(home / "phone.jsonl"),
                   "DECISION_CALLS": str(home / "decisions.jsonl")}
    for key in ("PHONE_OWNS_ERROR", "PHONE_ACTION_ERROR", "SAY_RESULT", "SAY_ARGV_FILE"):
        environment.pop(key, None)

    def poll(updates, **extra):
        incoming = home / "incoming.json"
        incoming.write_text(json.dumps(updates))
        result = subprocess.run([sys.executable, str(home / "bin/tg-poller"),
                                 "--once", "--updates", str(incoming)],
                                cwd=home, env={**environment, **extra}, text=True,
                                capture_output=True, timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
        return [json.loads(line)["result"] for line in result.stdout.splitlines()]

    return home, environment, poll


def test_foreign_chats_never_reach_subfleet_or_the_update_log(gateway):
    home, _, poll = gateway
    assert poll([tap(owner=999), message(owner=999)]) == ["ignored (not owner)"] * 2
    assert records(home / "phone.jsonl") == []
    assert records(home / "api.jsonl") == []
    assert records(home / "state/telegram/updates.jsonl") == []


def test_sf_callback_forwards_one_literal_argument_and_acknowledges(gateway):
    home, _, poll = gateway
    data = "sf:token:a0;$(touch injected)"
    assert poll([tap(data)]) == ["Subfleet: answered"]
    assert records(home / "phone.jsonl") == [["phone", "tap", data]]
    calls = records(home / "api.jsonl")
    assert [call["method"] for call in calls] == ["answerCallbackQuery"]
    assert calls[0]["params"]["callback_query_id"] == "callback-1"
    assert not (home / "injected").exists()
    assert records(home / "state/telegram/updates.jsonl")[0]["data"] == data


def test_failed_tap_is_visible_and_still_acknowledges_callback(gateway):
    home, _, poll = gateway
    result = poll([tap()], PHONE_ACTION_ERROR="1")
    assert "could not confirm this action" in result[0]
    calls = records(home / "api.jsonl")
    assert [call["method"] for call in calls] == ["sendMessage", "answerCallbackQuery"]
    assert "no longer pending" in calls[0]["params"]["text"]
    assert calls[0]["params"]["disable_notification"] is True


def test_missing_cli_reports_error_without_crashing_poller(gateway):
    home, _, poll = gateway
    result = poll([tap()], SUBFLEET_BIN=str(home / "missing"))
    assert "could not confirm" in result[0]
    assert records(home / "api.jsonl")[-1]["method"] == "answerCallbackQuery"


@pytest.mark.parametrize("text", ["/brief", "d011 send it", "--cancel $(touch injected)\n`touch injected`"])
def test_owned_reply_precedes_commands_and_rulings_and_preserves_argv(gateway, text):
    home, _, poll = gateway
    assert poll([message(text)]) == ["Subfleet: queued"]
    assert records(home / "phone.jsonl") == [
        ["phone", "owns", "701"],
        ["phone", "reply", "701", "--update-id", "11", "--", text]]
    assert records(home / "decisions.jsonl") == []
    assert not (home / "injected").exists()
    assert records(home / "api.jsonl")[0]["params"]["text"] == "Subfleet: queued"


def test_failed_reply_does_not_acknowledge_success(gateway):
    home, _, poll = gateway
    result = poll([message()], PHONE_ACTION_ERROR="1")
    assert "could not confirm this reply" in result[0]
    assert "no longer pending" in records(home / "api.jsonl")[0]["params"]["text"]


def test_unknown_card_keeps_cos_reply_hint_and_ruling_paths(gateway):
    home, _, poll = gateway
    assert poll([message(card=222), message("d011 yes", card=222, update_id=12)]) == [
        "hint sent", "ruling d011 ok"]
    assert records(home / "phone.jsonl") == [["phone", "owns", "222"]] * 2
    assert records(home / "decisions.jsonl") == [
        ["decide", "d011", "yes"], ["card", "--refresh"]]


def test_ownership_error_cannot_fall_through_to_a_cos_decision(gateway):
    home, _, poll = gateway
    result = poll([message("d011 yes")], PHONE_OWNS_ERROR="1")
    assert "could not check this reply" in result[0]
    assert records(home / "decisions.jsonl") == []
    assert "daemon unavailable" in records(home / "api.jsonl")[0]["params"]["text"]


def test_editing_owned_reply_does_not_dispatch_another_message(gateway):
    home, _, poll = gateway
    assert poll([message(edited=True)]) == ["ignored (edited Subfleet reply)"]
    assert records(home / "phone.jsonl") == [["phone", "owns", "701"]]
    assert records(home / "api.jsonl") == []


def test_cos_callbacks_and_unthreaded_rulings_are_unchanged(gateway):
    home, _, poll = gateway
    assert poll([tap("dec:d011:yes"), message("d012 park", card=None)]) == [
        "recorded", "ruling d012 ok"]
    assert records(home / "phone.jsonl") == []
    assert records(home / "decisions.jsonl") == [
        ["decide", "d011", "yes"], ["card", "--refresh", "--message-id", "701"],
        ["decide", "d012", "park"], ["card", "--refresh"]]


def test_unthreaded_text_is_stored_without_subfleet_dispatch(gateway):
    home, _, poll = gateway
    assert poll([message("hello", card=None)]) == ["stored"]
    assert records(home / "phone.jsonl") == []
    assert records(home / "api.jsonl") == []


def test_cli_timeout_is_an_operational_error_with_uncertainty(gateway, monkeypatch):
    home, _, _ = gateway
    loader = importlib.machinery.SourceFileLoader("patched_poller", str(home / "bin/tg-poller"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)

    def timeout(argv, **kwargs):
        assert argv == ["subfleet", "phone", "tap", "sf:token:a0"]
        assert kwargs["timeout"] == 30
        assert "shell" not in kwargs
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.delenv("SUBFLEET_BIN", raising=False)
    monkeypatch.setattr(module.subprocess, "run", timeout)
    rc, detail = module.run_subfleet("tap", "sf:token:a0")
    assert rc == 69
    assert "check the conversation before retrying" in detail
