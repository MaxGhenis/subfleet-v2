"""C-31: durable suggestions, scoped host calls and atomic person Start."""

from __future__ import annotations

import concurrent.futures
import json
import threading
import uuid
from pathlib import Path

import pytest

from subfleet.conversations.chips import ChipService, PROMPT_BYTES
from subfleet.conversations.store import ConversationError, ConversationStore
from tests.unit.test_conversation_service import SETTINGS, conversation, submit, svc  # noqa: F401


@pytest.fixture
def host(svc):
    cid = conversation(svc)
    mid = submit(svc, cid)
    credentials = svc.chips.host_credentials(cid, mid)
    return {"conversation_id": cid, "message_id": mid, "host_token": credentials["token"]}


def propose(svc, host, **kwargs):
    return svc.op_chip_spawn({**host, "request_id": str(uuid.uuid4()), "title": "Fix the parser",
                              "tldr": "Handle quoted paths.", "prompt": "Inspect and fix quoted paths.",
                              **kwargs}, None)["chip"]


@pytest.fixture
def person(svc, monkeypatch):
    calls = []
    monkeypatch.setattr(svc, "_person", lambda peer, what: calls.append((peer, what)))
    return calls


def test_spawn_persists_exact_prompt_snapshot_event_and_watch(svc, host):
    prompt = "  A self-contained prompt\r\nwith café and 空白.\r\n  "
    chip = propose(svc, host, prompt=prompt)
    assert chip["state"] == "pending" and chip["prompt"] == prompt
    assert chip["parent_conversation_id"] == host["conversation_id"]
    assert chip["message_id"] == host["message_id"]
    assert chip["cwd"] == svc.test_workspace
    assert len(svc.store.list_conversations()) == 1
    assert svc.op_chip_list({"conversation_id": host["conversation_id"]}, None)["chips"] == [chip]
    assert svc.op_conversation_open({"conversation_id": host["conversation_id"]}, None)["chips"] == [chip]
    events = svc.store.events_after(host["conversation_id"], 0)["events"]
    assert len(events) == 1 and events[0]["kind"] == "chip.created"
    assert events[0]["message_id"] == host["message_id"]
    assert "prompt" not in events[0]["data"]["chip"]
    assert "host_token" not in json.dumps(events)
    assert svc.store.changes_after(0)["changes"][-1]["conversation_id"] == host["conversation_id"]


def test_host_is_scoped_unforgeable_and_not_exposed(svc, host):
    other = conversation(svc)
    for bad in ({**host, "host_token": "invented"}, {**host, "conversation_id": other},
                {**host, "message_id": str(uuid.uuid4())}, {k: v for k, v in host.items() if k != "host_token"}):
        with pytest.raises(ConversationError, match="capability") as error:
            propose(svc, bad)
        assert error.value.code == 7
    row = svc.store.one("SELECT * FROM conversation_chip_hosts")
    assert host["host_token"] not in json.dumps(row)
    with pytest.raises(ConversationError, match="writable Claude"):
        svc.chips.host_credentials(other, host["message_id"])


def test_read_only_turns_never_get_a_host_capability(svc):
    cid = conversation(svc, settings={**SETTINGS, "permission": "read-only"})
    mid = str(uuid.uuid4())
    svc.store.submit_message(conversation_id=cid, message_id=mid, after_message_id=None, text="hi", attachments=[],
                             settings={**SETTINGS, "permission": "read-only"})
    with pytest.raises(ConversationError, match="writable Claude"):
        svc.chips.host_credentials(cid, mid)
    assert not svc.store.query("SELECT * FROM conversation_chip_hosts")


@pytest.mark.parametrize("kwargs", [
    {"title": " "}, {"title": "t" * 201}, {"tldr": "t" * 2001}, {"prompt": ""},
    {"prompt": "x" * (PROMPT_BYTES + 1)}, {"prompt": "é" * (PROMPT_BYTES // 2 + 1)},
    {"cwd": "relative/path"}, {"cwd": "/a-directory-that-does-not-exist/subfleet"}, {"request_id": ""},
    {"title": "\ud800"}, {"tldr": "\ud800"}, {"prompt": "\ud800"}, {"cwd": "/" + "d" * 4096},
])
def test_invalid_suggestions_change_nothing(svc, host, kwargs):
    before = svc.store.changes_after(0)
    with pytest.raises(ConversationError):
        propose(svc, host, **kwargs)
    assert svc.chips.list(host["conversation_id"]) == []
    assert svc.store.changes_after(0) == before


def test_maximum_prompt_stays_out_of_event_payload(svc, host):
    chip = propose(svc, host, prompt="\x01" * PROMPT_BYTES, title="T" * 200, tldr="\x01" * 1999 + "t")
    assert len(chip["prompt"]) == PROMPT_BYTES
    event = svc.store.one("SELECT data_json FROM events WHERE kind='chip.created'")
    assert len(event["data_json"].encode()) < 64 * 1024


def test_spawn_retry_returns_one_chip_and_rejects_changed_content(svc, host):
    chip = propose(svc, host, request_id="once")
    assert propose(svc, host, request_id="once") == chip
    with pytest.raises(ConversationError) as error:
        propose(svc, host, request_id="once", prompt="Something else")
    assert error.value.reason == "chip-request-conflict"
    assert len(svc.store.query("SELECT * FROM chips")) == 1
    assert len(svc.store.query("SELECT * FROM events")) == 1


def test_spawn_retry_survives_removed_cwd_and_archived_parent(svc, host, tmp_path):
    cwd = tmp_path / "gone-after-spawn"
    cwd.mkdir()
    chip = propose(svc, host, request_id="once", cwd=str(cwd))
    cwd.rmdir()
    svc.store.update_conversation(host["conversation_id"], archived_at="2026-09-28T00:00:00Z")
    assert propose(svc, host, request_id="once", cwd=str(cwd)) == chip


def test_concurrent_spawn_retries_make_one_chip_and_one_event(svc, host):
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        chips = list(pool.map(lambda _: propose(svc, host, request_id="once"), range(8)))
    assert len({chip["chip_id"] for chip in chips}) == 1
    assert len(svc.store.query("SELECT * FROM chips")) == 1
    assert len(svc.store.query("SELECT * FROM events")) == 1


def test_spawn_retry_keeps_original_default_workspace(svc, host, tmp_path):
    chip = propose(svc, host, request_id="once")
    svc.store.update_conversation(host["conversation_id"], workspace=str(tmp_path))
    assert propose(svc, host, request_id="once") == chip


def test_invalid_dismiss_reason_changes_nothing(svc, host):
    chip = propose(svc, host)
    with pytest.raises(ConversationError, match="UTF-8"):
        svc.op_chip_dismiss({**host, "chip_id": chip["chip_id"], "reason": "\ud800"}, None)
    assert svc.chips.list(host["conversation_id"])[0] == chip


def test_start_is_person_only_and_host_dismiss_cannot_cross_parents(svc, host):
    chip = propose(svc, host)
    with pytest.raises(ConversationError) as error:
        svc.op_chip_start({"chip_id": chip["chip_id"], **host}, None)
    assert error.value.reason == "person-only"
    with pytest.raises(ConversationError) as error:
        svc.op_chip_dismiss({"chip_id": chip["chip_id"]}, None)
    assert error.value.reason == "person-only"
    other = conversation(svc)
    other_mid = submit(svc, other)
    token = svc.chips.host_credentials(other, other_mid)["token"]
    with pytest.raises(ConversationError) as error:
        svc.op_chip_dismiss({"chip_id": chip["chip_id"], "conversation_id": other,
                             "message_id": other_mid, "host_token": token}, None)
    assert error.value.reason == "chip-host-refused"
    assert svc.chips.list(host["conversation_id"])[0]["state"] == "pending"


def test_start_creates_one_child_with_exact_prompt_current_defaults_and_relationship(svc, host, person, tmp_path, monkeypatch):
    cwd = tmp_path / "another workspace"
    cwd.mkdir()
    prompt = "  Please inspect café.\r\nThen fix it.\rKeep tabs\t  "
    chip = propose(svc, host, cwd=str(cwd), prompt=prompt)
    settings = {**SETTINGS, "effort": "high", "fast": True, "permission": "accept-edits", "auto_continue": False}
    svc.store.update_conversation(host["conversation_id"], settings=settings, allow_main=True)
    out = svc.op_chip_start({"chip_id": chip["chip_id"]}, 123)
    child, message = out["conversation"], out["message"]
    assert child["provider"] == "claude" and child["settings"] == settings
    assert child["workspace"] == str(cwd) and child["workspace_kind"] == "in-place"
    assert child["parent_conversation_id"] == host["conversation_id"]
    assert child["source_chip_id"] == chip["chip_id"] and child["native_session_id"] is None
    assert child["title"] == chip["title"] and not child["allow_main"]
    assert message["state"] == "queued" and message["text"] == prompt
    row = svc.store.message(message["message_id"])
    assert Path(row["text_path"]).read_bytes() == prompt.encode()
    assert svc.store.message_text(row) == prompt
    assert len(svc.store.messages(child["conversation_id"])) == 1
    assert len(svc.store.messages(host["conversation_id"])) == 1
    assert person and svc.daemon.notified == 1
    assert svc.op_chip_start({"chip_id": chip["chip_id"]}, 123) == out
    assert len(svc.store.list_conversations()) == 2
    assert [e["kind"] for e in svc.store.events_after(host["conversation_id"], 0)["events"]] == [
        "chip.created", "chip.started"]
    # The turn manifest the normal dispatcher sends to the provider is exact too.
    turns = []
    submit_turn = svc.daemon.submit

    def capture(args, *, turn):
        turns.append(turn)
        return submit_turn(args, turn=turn)

    monkeypatch.setattr(svc.daemon, "submit", capture)
    svc.store.set_state(host["message_id"], "complete")
    svc._dispatch()
    assert len(turns) == 1 and turns[0]["text"] == prompt


def test_start_same_cwd_inherits_main_permission(svc, host, person):
    svc.store.update_conversation(host["conversation_id"], allow_main=True)
    chip = propose(svc, host)
    assert svc.op_chip_start({"chip_id": chip["chip_id"]}, 123)["conversation"]["allow_main"] is True


def test_dismiss_is_idempotent_terminal_and_retains_reason(svc, host, person):
    chip = propose(svc, host)
    args = {**host, "chip_id": chip["chip_id"], "reason": "Already fixed"}
    dismissed = svc.op_chip_dismiss(args, None)
    assert dismissed["chip"]["state"] == "dismissed"
    assert dismissed["chip"]["dismissal_reason"] == "Already fixed"
    assert svc.op_chip_dismiss({**args, "reason": "Different"}, None) == dismissed
    with pytest.raises(ConversationError) as error:
        svc.op_chip_start({"chip_id": chip["chip_id"]}, 123)
    assert error.value.reason == "chip-dismissed"
    assert len(svc.store.list_conversations()) == 1


def test_started_chip_cannot_be_dismissed_or_cancel_child(svc, host, person):
    chip = propose(svc, host)
    out = svc.op_chip_start({"chip_id": chip["chip_id"]}, 123)
    with pytest.raises(ConversationError) as error:
        svc.op_chip_dismiss({**host, "chip_id": chip["chip_id"]}, None)
    assert error.value.reason == "chip-started"
    assert svc.store.message(out["message"]["message_id"])["state"] == "queued"


def test_missing_cwd_at_start_leaves_pending_without_child(svc, host, person, tmp_path):
    cwd = tmp_path / "vanishing"
    cwd.mkdir()
    chip = propose(svc, host, cwd=str(cwd))
    cwd.rmdir()
    with pytest.raises(ConversationError) as error:
        svc.op_chip_start({"chip_id": chip["chip_id"]}, 123)
    assert error.value.reason == "bad-workspace"
    assert svc.chips.list(host["conversation_id"])[0]["state"] == "pending"
    assert len(svc.store.list_conversations()) == 1


def test_concurrent_starts_make_exactly_one_child_and_message(svc, host, person):
    chip = propose(svc, host)
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: svc.op_chip_start({"chip_id": chip["chip_id"]}, 123), range(8)))
    assert len({r["conversation"]["conversation_id"] for r in results}) == 1
    assert len({r["message"]["message_id"] for r in results}) == 1
    assert len(svc.store.list_conversations()) == 2
    assert len(svc.store.query("SELECT * FROM messages")) == 2
    assert len(list(svc.store.dir.glob("*/messages/*.md"))) == 2


def test_dismiss_wins_while_start_prepares_and_leaves_no_orphans(svc, host, person, monkeypatch):
    chip = propose(svc, host)
    entered, release = threading.Event(), threading.Event()
    publish = svc.store._publish

    def paused(path, data):
        publish(path, data)
        entered.set()
        assert release.wait(10)

    monkeypatch.setattr(svc.store, "_publish", paused)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future = pool.submit(svc.op_chip_start, {"chip_id": chip["chip_id"]}, 123)
        assert entered.wait(10)
        svc.op_chip_dismiss({**host, "chip_id": chip["chip_id"]}, None)
        release.set()
        with pytest.raises(ConversationError) as error:
            future.result(10)
    assert error.value.reason == "chip-dismissed"
    assert len(svc.store.list_conversations()) == 1
    assert len(svc.store.query("SELECT * FROM messages")) == 1
    assert len(list(svc.store.dir.glob("*/messages/*.md"))) == 1


def test_failure_rolls_back_child_message_chip_and_prepared_payload(svc, host, person, monkeypatch):
    chip = propose(svc, host)

    def fail(*args):
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(svc.chips, "_event", fail)
    with pytest.raises(RuntimeError, match="injected"):
        svc.op_chip_start({"chip_id": chip["chip_id"]}, 123)
    assert svc.chips.list(host["conversation_id"])[0]["state"] == "pending"
    assert len(svc.store.list_conversations()) == 1
    assert len(svc.store.query("SELECT * FROM messages")) == 1
    assert len(list(svc.store.dir.glob("*/messages/*.md"))) == 1


def test_restart_retains_host_capability_chips_and_started_identity(svc, host, person):
    chip = propose(svc, host, request_id="persist")
    first = svc.op_chip_start({"chip_id": chip["chip_id"]}, 123)
    svc.store.close()
    svc.store = ConversationStore(svc.root)
    svc.chips = ChipService(svc)
    assert propose(svc, host, request_id="persist")["state"] == "started"
    assert svc.op_chip_start({"chip_id": chip["chip_id"]}, 123) == first


def test_new_ops_capability_and_file_pool(svc):
    for op in ("chip.spawn", "chip.list", "chip.dismiss", "chip.start"):
        assert svc.owns(op)
    assert "chips.v1" in svc.op_capabilities({}, None)["capabilities"]
    assert svc.pool_for("chip.spawn") is svc.files and svc.pool_for("chip.start") is svc.files
