"""The daemon's own conversation code, without the daemon, for the app's core tests.

`ServiceHarness` builds the real `ConversationService` over a real
`ConversationStore` in a temporary state root; its op handlers, `_view`,
`_receipt` and `_approval_view` produce the JSON the app decodes. Nothing is
dispatched (there is no scheduler), so messages stay `queued` unless a test
moves them; events come from the real provider drivers (`claude_turn`,
`codex_turn`) fed scripted provider output and stored with the store's own
`append_events`, as the turn runner does.

`ServiceServer` answers the protocol on an AF_UNIX socket with the service's
own `respond` (so errors carry the daemon's `"<reason>: <message>"` form), and
can drop or delay one answer to test the app's retries. Person-only checks are
bypassed here (they are the peer check's, tested end to end in
`test_core_live.py`).

Nothing here touches `~/.subfleet`.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Callable
import uuid

from subfleet import protocol
from subfleet.conversations.claude_turn import ClaudeTurn
from subfleet.conversations.codex_turn import CodexTurn
from subfleet.conversations.peers import Verdict, peer_pid
from subfleet.conversations.service import ConversationService
from subfleet.conversations.turn import APPROVAL_NEEDED, RUNNING, TurnSpec
from subfleet.daemon import busy_answer

REPO = Path(__file__).resolve().parents[2]


class _MainStore:
    """The job store seams the conversation handlers touch: no jobs, no lanes."""

    def one(self, sql: str, params: tuple = ()) -> None:
        return None

    def query(self, sql: str, params: tuple = ()) -> list:
        return []

    def lane_rows(self) -> list:
        return []


class ServiceHarness:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace = self.root / "work"
        self.workspace.mkdir(exist_ok=True)
        policy = json.loads((REPO / "subfleet/default_policy.json").read_text())
        self.daemon = SimpleNamespace(root=self.root, policy=policy, store=_MainStore(), _notify=lambda: None,
                                      requests=ThreadPoolExecutor(4), log=logging.getLogger("frontend-harness"))
        self.service = ConversationService(self.daemon)
        # Person-only operations are the peer check's (C-25.6), exercised live elsewhere.
        self.service._person = lambda peer, what: Verdict(True, "frontend harness", peer)
        self.store = self.service.store

    def close(self) -> None:
        self.service.close()
        self.daemon.requests.shutdown(wait=False)

    def call(self, op: str, **args) -> dict:
        return self.service.handle(op, args, os.getpid())

    def response_line(self, op: str, args: dict, request_id: str = "fixture") -> bytes:
        """The exact line the daemon writes for a request, errors included."""
        left, right = socket.socketpair()
        try:
            self.service.respond(left, threading.Lock(), protocol.Request(op=op, args=args, id=request_id), os.getpid())
            with right.makefile("rb") as reader:
                return reader.readline()
        finally:
            left.close()
            right.close()

    # --- fixtures -------------------------------------------------------------

    def settings(self, **extra) -> dict:
        return {"model": "opus[1m]", "effort": None, "fast": False, "permission": "ask", "auto_continue": True, **extra}

    def create(self, provider: str = "claude", **extra) -> dict:
        args = {"request_id": str(uuid.uuid4()), "provider": provider, "workspace": str(self.workspace),
                "settings": self.settings(**extra.pop("settings", {})), **extra}
        return self.call("conversation.create", **args)["conversation"]

    def submit(self, cid: str, text: str, *, after: str | None = None, message_id: str | None = None) -> dict:
        return self.call("message.submit", conversation_id=cid, message_id=message_id or str(uuid.uuid4()),
                         after_message_id=after, text=text, attachments=[], settings=self.settings())

    def attempt(self, cid: str, mid: str, *, provider: str = "claude", seq: int = 1) -> "Attempt":
        return Attempt(self, cid, mid, provider=provider, attempt_id=f"job-{mid[:8]}/a{seq}")


class Attempt:
    """One turn attempt, driven by the real provider driver, stored as the runner stores it."""

    def __init__(self, harness: ServiceHarness, cid: str, mid: str, *, provider: str, attempt_id: str):
        self.harness = harness
        self.store = harness.store
        self.cid, self.mid, self.attempt_id = cid, mid, attempt_id
        spec = TurnSpec(provider=provider, message_id=mid, text="fixture", model_id="opus[1m]" if provider == "claude"
                        else "gpt-6-astra", permission="ask" if provider == "claude" else "read-only",
                        native_session_id=None, new_session_id=str(uuid.uuid4()) if provider == "claude" else None,
                        cwd=str(harness.workspace))
        self.driver = ClaudeTurn(spec, read_bytes=lambda path: b"") if provider == "claude" else CodexTurn(spec)
        self.offset = 0
        self.stdin_seq = 0
        self._apply(self.driver.start())

    def feed(self, *rows: dict) -> None:
        for row in rows:
            line = json.dumps(row)
            self._apply(self.driver.feed(line, self.offset))
            self.offset += len(line) + 1

    def respond(self, request_id: str, decision: str, message: str | None = None, answers: dict | None = None) -> None:
        if isinstance(self.driver, ClaudeTurn):
            step = self.driver.respond(request_id, decision, message, answers=answers)
        else:
            step = self.driver.respond(request_id, decision, message)
        self._apply(step)

    def _apply(self, step) -> None:
        """runner.TurnRunner._apply, without the relay."""
        batch = [("command" if e.source.startswith("cmd:") else "stdout", e.source, 0, e.kind, e.data)
                 for e in step.events]
        if batch:
            self.store.append_events(conversation_id=self.cid, message_id=self.mid, attempt_id=self.attempt_id,
                                     events=batch, stdout_offset=self.offset, stdin_seq=self.stdin_seq)
        self.stdin_seq += len(step.frames)
        for event in step.events:
            if event.kind == "accepted":
                self.store.set_state(self.mid, RUNNING, expect=("queued", "waiting", "starting"))
        for approval in step.approvals:
            self.store.add_approval(message_id=self.mid, conversation_id=self.cid, attempt_id=self.attempt_id,
                                    provider_request_id=approval.provider_request_id, kind=approval.kind,
                                    request=approval.request, display=approval.summary, options=approval.options)
            self.store.set_state(self.mid, APPROVAL_NEEDED, expect=("running", "starting"))
        if step.resolved:
            self.store.withdraw_approvals(attempt_id=self.attempt_id, provider_request_ids=list(step.resolved))
            if not self.store.approvals(message_id=self.mid) and self.driver.outcome is None:
                self.store.set_state(self.mid, RUNNING, expect=("approval-needed",))
        if step.outcome is not None:
            state = step.outcome.state
            self.store.set_state(self.mid, state, reason=step.outcome.reason,
                                 expect=("running", "approval-needed", "starting", "waiting", "queued"),
                                 served={"lane_id": "claude-1", "model": step.outcome.served_model})

    def compact(self) -> int:
        return self.store.compact(self.attempt_id)


# --- scripted provider output (the shapes tests/fake/interactive_claude.py emits) ---

LEVELS = ["low", "medium", "high", "xhigh", "max"]
CLAUDE_CATALOG = [
    {"value": "default", "resolvedModel": "claude-opus-5-5[1m]", "supportsEffort": True,
     "supportedEffortLevels": LEVELS, "supportsFastMode": True},
    {"value": "opus[1m]", "resolvedModel": "claude-opus-5-5[1m]", "supportsEffort": True,
     "supportedEffortLevels": LEVELS, "supportsFastMode": True},
]


def claude_init(email: str = "fixture@example.invalid") -> dict:
    return {"type": "control_response", "response": {
        "subtype": "success", "request_id": "subfleet-init", "response": {
            "account": {"email": email}, "models": CLAUDE_CATALOG, "fast_mode_state": "off",
            "fast_mode_disabled_reason": None, "current_permission_mode": "default"}}}


def claude_stream(message_id: str, pieces: list[str], *, kind: str = "text", index: int = 0) -> list[dict]:
    rows = [{"type": "stream_event", "event": {"type": "message_start", "message": {"id": message_id}}}]
    delta_type, field = ("text_delta", "text") if kind == "text" else ("thinking_delta", "thinking")
    for piece in pieces:
        rows.append({"type": "stream_event", "event": {"type": "content_block_delta", "index": index,
                                                        "delta": {"type": delta_type, field: piece}}})
    rows.append({"type": "stream_event", "event": {"type": "content_block_stop", "index": index}})
    return rows


def claude_block(message_id: str, index: int, block: dict, pieces: list[str] = ()) -> list[dict]:
    """One content block as Claude 2.1.280 frames it (observed 2026-09-24): its
    start, deltas, then an `assistant` row holding that block alone while it is
    still open, then its stop."""
    kind = block["type"]
    rows = [{"type": "stream_event", "event": {"type": "content_block_start", "index": index,
                                                "content_block": {"type": kind}}}]
    delta_type, field = {"text": ("text_delta", "text"), "thinking": ("thinking_delta", "thinking"),
                         "tool_use": ("input_json_delta", "partial_json")}[kind]
    for piece in pieces:
        rows.append({"type": "stream_event", "event": {"type": "content_block_delta", "index": index,
                                                        "delta": {"type": delta_type, field: piece}}})
    rows.append(claude_assistant(message_id, [block]))
    rows.append({"type": "stream_event", "event": {"type": "content_block_stop", "index": index}})
    return rows


def claude_assistant(message_id: str, blocks: list[dict], model: str = "claude-opus-5-5") -> dict:
    return {"type": "assistant", "parent_tool_use_id": None,
            "message": {"id": message_id, "type": "message", "role": "assistant", "model": model, "content": blocks}}


def claude_result(ok: bool = True, subtype: str = "success") -> dict:
    return {"type": "result", "subtype": subtype, "is_error": not ok, "num_turns": 1, "result": "",
            "errors": [], "permission_denials": [], "duration_ms": 5}


# --- a socket server around the service ---------------------------------------------


class ServiceServer:
    """The protocol on an AF_UNIX socket, answered by the real service.

    `faults[(op, key)]` applies once to the first matching request, where `key`
    is the request's `message_id` or `request_id` (or None for any):
    `"drop"` handles the request and closes without answering (a lost answer),
    `"refuse"` closes without handling it (the daemon never saw it),
    `"busy"` answers as the daemon does past its connection cap, without
    handling it (C-16.1: exit 69, request id ""),
    `"delay:<s>"` handles it and answers after a pause.
    """

    def __init__(self, harness: ServiceHarness):
        self.harness = harness
        self.directory = Path(tempfile.mkdtemp(prefix="sf-app-", dir="/tmp"))
        self.path = self.directory / "daemon.sock"
        self.faults: dict[tuple[str, str | None], str] = {}
        self.requests: list[dict] = []
        self.connections = 0
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        self._sock.listen(16)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        finally:
            try:
                self.path.unlink()
                self.directory.rmdir()
            except OSError:
                pass

    def _serve(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            self.connections += 1
            threading.Thread(target=self._connection, args=(conn,), daemon=True).start()

    def _fault(self, req: protocol.Request) -> str | None:
        key = req.args.get("message_id") or req.args.get("request_id")
        for candidate in ((req.op, key), (req.op, None)):
            if candidate in self.faults:
                return self.faults.pop(candidate)
        return None

    def _connection(self, conn: socket.socket) -> None:
        lock = threading.Lock()
        peer = peer_pid(conn)
        try:
            with conn.makefile("rb") as reader:
                line = reader.readline(1024 * 1024 + 1)
                if not line:
                    return
                try:
                    req = protocol.decode_request(line)
                except protocol.ProtocolError as exc:
                    conn.sendall(protocol.encode(protocol.fail("", 2, str(exc))))
                    return
                self.requests.append({"op": req.op, "id": req.id, "args": req.args, "raw": json.loads(line)})
                fault = self._fault(req)
                if fault == "refuse":
                    return
                if fault == "busy":
                    conn.sendall(busy_answer("the daemon is serving 512 connections"))
                    return
                if fault == "drop":
                    try:
                        self.harness.service.handle(req.op, req.args, peer)
                    except Exception:
                        pass
                    return
                if fault and fault.startswith("delay:"):
                    time.sleep(float(fault.split(":", 1)[1]))
                self.harness.service.respond(conn, lock, req, peer)
        finally:
            conn.close()


def wait_for(predicate: Callable[[], Any], timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"condition not met within {timeout}s")
