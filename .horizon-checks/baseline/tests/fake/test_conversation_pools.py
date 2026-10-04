"""C-25.3, C-25.1 (design §5, D-24, D-25, D-16): which pool answers a conversation op,
through the daemon's own socket handler, with no provider process."""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

from subfleet import protocol
from tests.fake.test_state_contract import admitted_reader, state_daemon  # noqa: F401 (fixture)

SETTINGS = {"model": "opus[1m]", "effort": None, "fast": False, "permission": "ask", "auto_continue": True}


def test_c25_3_the_diffs_run_on_the_file_pool_and_never_hold_up_other_ops(state_daemon, monkeypatch):
    """C-25.3, C-25.1: `turn.diff` and `conversation.diff` run on the file pool, never on
    the requests pool; with every file thread held by a diff and a third diff queued,
    `message.submit` and `capabilities` still answer, and every diff answers once git
    does. `capabilities` reports `conversation_schema` 1, the version of the ops'
    shapes, although the store is schema 2."""
    daemon, harness = state_daemon
    service = daemon.conversations
    assert service.pool_for("turn.diff") is service.files
    assert service.pool_for("conversation.diff") is service.files
    assert service.pool_for("conversation.create") is service.files
    assert service.pool_for("message.submit") is daemon.requests
    release = threading.Event()
    threads: list[str] = []

    def held(args, peer):
        threads.append(threading.current_thread().name)
        assert release.wait(20)
        return {"held": True}

    monkeypatch.setattr(service, "op_turn_diff", held)
    monkeypatch.setattr(service, "op_conversation_diff", held)
    conversation, _ = service.store.create_conversation(provider="claude", workspace=str(harness.workdir),
                                                        workspace_kind="in-place", settings=SETTINGS, origin="new")
    cid = conversation["conversation_id"]
    server, client = socket.socketpair()
    server.settimeout(5)
    client.settimeout(5)
    thread = admitted_reader(daemon, server)
    thread.start()
    try:
        with client.makefile("rb") as stream:
            for n, op in enumerate(("turn.diff", "conversation.diff", "turn.diff")):
                client.sendall(protocol.encode(protocol.Request(
                    op, {"message_id": str(uuid.uuid4()), "conversation_id": cid}, id=f"diff-{n}")))
            deadline = time.monotonic() + 10
            while len(threads) < 2 and time.monotonic() < deadline:
                time.sleep(.01)
            assert len(threads) == 2, "both file threads are held by a diff"
            message_id = str(uuid.uuid4())
            client.sendall(protocol.encode(protocol.Request(
                "message.submit", {"conversation_id": cid, "message_id": message_id, "text": "hello"}, id="submit")))
            client.sendall(protocol.encode(protocol.Request("capabilities", {}, id="capabilities")))
            replies = {}
            for _ in range(2):
                reply = json.loads(stream.readline())
                replies[reply["id"]] = reply
            assert set(replies) == {"submit", "capabilities"}
            assert replies["submit"]["ok"] and replies["submit"]["result"]["created"] is True
            assert replies["submit"]["result"]["message_id"] == message_id
            assert replies["capabilities"]["result"]["conversation_schema"] == 1
            assert "diff.v1" in replies["capabilities"]["result"]["capabilities"]
            assert len(threads) == 2, "the third diff waits for a file thread, not a request thread"
            release.set()
            done = [json.loads(stream.readline()) for _ in range(3)]
            assert sorted(r["id"] for r in done) == ["diff-0", "diff-1", "diff-2"]
            assert all(r["ok"] and r["result"] == {"held": True} for r in done)
        assert len(threads) == 3 and all(name.startswith("subfleet-files") for name in threads), threads
    finally:
        release.set()
        client.close()
        thread.join(timeout=5)
        server.close()
    assert not thread.is_alive()


def test_c25_3_a_worktree_create_cuts_its_worktree_on_the_file_pool(state_daemon, monkeypatch):
    """C-25.3, C-26.10 (design D-16): `conversation.create` of a worktree conversation
    runs git (`rev-parse`, `worktree add`) on a file thread, never a request thread, so
    while that git is held `message.submit` and `capabilities` still answer; once it
    runs, the create answers with the worktree as the conversation's workspace."""
    daemon, harness = state_daemon
    service = daemon.conversations
    for args in (("init", "-b", "feature/create"), ("config", "user.name", "Test User"),
                 ("config", "user.email", "test@example.invalid"), ("commit", "--allow-empty", "-m", "base")):
        subprocess.run(["git", "-C", str(harness.workdir), *args], check=True, capture_output=True)
    release = threading.Event()
    threads: list[str] = []
    cut = service._cut_worktree

    def held(conversation):
        threads.append(threading.current_thread().name)
        assert release.wait(20)
        return cut(conversation)

    monkeypatch.setattr(service, "_cut_worktree", held)
    other, _ = service.store.create_conversation(provider="claude", workspace=str(harness.workdir),
                                                 workspace_kind="in-place", settings=SETTINGS, origin="new")
    server, client = socket.socketpair()
    server.settimeout(5)
    client.settimeout(5)
    thread = admitted_reader(daemon, server)
    thread.start()
    try:
        with client.makefile("rb") as stream:
            client.sendall(protocol.encode(protocol.Request("conversation.create", {
                "provider": "claude", "request_id": "create-worktree", "settings": SETTINGS,
                "workspace": str(harness.workdir), "workspace_kind": "worktree"}, id="create")))
            deadline = time.monotonic() + 10
            while not threads and time.monotonic() < deadline:
                time.sleep(.01)
            assert threads, "the create reached its worktree cut"
            message_id = str(uuid.uuid4())
            client.sendall(protocol.encode(protocol.Request("message.submit", {
                "conversation_id": other["conversation_id"], "message_id": message_id, "text": "hello"},
                id="submit")))
            client.sendall(protocol.encode(protocol.Request("capabilities", {}, id="capabilities")))
            replies = {}
            for _ in range(2):
                reply = json.loads(stream.readline())
                replies[reply["id"]] = reply
            assert set(replies) == {"submit", "capabilities"}, "answered while the create's git is held"
            assert replies["submit"]["ok"] and replies["submit"]["result"]["message_id"] == message_id
            release.set()
            created = json.loads(stream.readline())
        assert created["id"] == "create" and created["ok"], created
        conversation = created["result"]["conversation"]
        assert created["result"]["created"] is True and conversation["workspace_kind"] == "worktree"
        worktree = Path(conversation["workspace"])
        assert worktree.parent == daemon.root / "worktrees" and worktree.is_dir()
        listed = subprocess.run(["git", "-C", str(harness.workdir), "worktree", "list", "--porcelain"],
                                check=True, capture_output=True, text=True).stdout
        assert f"worktree {worktree.resolve()}" in listed
        assert len(threads) == 1 and threads[0].startswith("subfleet-files"), threads
    finally:
        release.set()
        client.close()
        thread.join(timeout=5)
        server.close()
    assert not thread.is_alive()
