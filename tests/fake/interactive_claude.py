"""A fake interactive `claude -p --input-format stream-json` (C-26, C-27 tests).

`tests/bin/claude` hands over to `main` here when its argv asks for
stream-json input. It speaks the wire shapes the Claude turn driver reads: the
`initialize` control request and its answer (account, model catalog, Fast
state, as Claude Code 2.1.280 reported them on 2026-09-24), the replayed user
message, stream events, assistant and user rows, `can_use_tool` control
requests, `rate_limit_event` and `result`, and exit 0 on stdin EOF. It never
reaches the network and never reads a credential: the account's email is
derived from the fixture token the lane resolves (`tests/fake/profile.py`).

Each user message picks its behaviour with a `[fake:<scenario>]` directive in
its text:

    (none)            streamed text, then a success result
    approval          asks to run a Bash command; allow runs it, deny says so
    question          AskUserQuestion; the chosen answers are echoed back
    slow              streams until interrupted; the interrupt ends the turn
    stubborn          acknowledges interrupts and keeps going; SIGINT ends it with no result
    limit             a rejected rate_limit_event, then an error result
    exit-after-ack    replays the message, then exits with no result
    exit-before-ack   reads the message, then exits without replaying it
    background        a success result, then more assistant text before EOF
    wrong-model       serves another model than the one asked for

Environment:

    CLAUDE_CODE_OAUTH_TOKEN   the fixture token; its suffix names the account
    SUBFLEET_FAKE_FAST        the `fast_mode_state` to report (default "on")
    SUBFLEET_FAKE_TURN_LOG    append argv and every stdin line here (JSON lines)
    CLAUDE_FAKE_PROJECTS_DIR  write the session transcript under this directory
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

DIRECTIVE = re.compile(r"\[fake:([a-z-]+)\]")
LEVELS = ["low", "medium", "high", "xhigh", "max"]
CATALOG = [
    {"value": "default", "resolvedModel": "claude-opus-5-5[1m]", "displayName": "Default (recommended)",
     "supportsEffort": True, "supportedEffortLevels": LEVELS, "supportsFastMode": True},
    {"value": "opus[1m]", "resolvedModel": "claude-opus-5-5[1m]", "displayName": "Opus",
     "supportsEffort": True, "supportedEffortLevels": LEVELS, "supportsFastMode": True},
    {"value": "claude-fable-5-1[1m]", "resolvedModel": "claude-fable-5-1[1m]", "displayName": "Fable",
     "supportsEffort": True, "supportedEffortLevels": LEVELS, "supportsFastMode": False},
    {"value": "sonnet", "resolvedModel": "claude-sonnet-5", "displayName": "Sonnet",
     "supportsEffort": True, "supportedEffortLevels": ["low", "medium", "high"], "supportsFastMode": False},
    {"value": "haiku", "resolvedModel": "claude-haiku-4-5-20251001", "displayName": "Haiku"},
]
ALIASES = {"opus": "claude-opus-5-5", "fable": "claude-fable-5-1", "sonnet": "claude-sonnet-5",
           "haiku": "claude-haiku-4-5-20251001"}


def flag(argv: list[str], name: str) -> str | None:
    for index, item in enumerate(argv):
        if item == name and index + 1 < len(argv):
            return argv[index + 1]
    return None


def served_model(value: str | None) -> str:
    """What `--model <value>` serves, `[1m]` kept, as the CLI reports it."""
    value = value or "default"
    for entry in CATALOG:
        if entry["value"] == value:
            return entry["resolvedModel"]
    base = value[:-4] if value.endswith("[1m]") else value
    return ALIASES.get(base, base) + ("[1m]" if value.endswith("[1m]") else "")


def account_email() -> str:
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not token:
        return "fake@example.test"
    from tests.fake.profile import response_for
    status, body = response_for(token, os.environ.get("SUBFLEET_FAKE_PROFILE"))
    try:
        return json.loads(body)["account"]["email"] if status == 200 else "unknown@example.test"
    except (ValueError, KeyError, TypeError):
        return "unknown@example.test"


def encode_project_dir(workdir: str) -> str:
    text = str(workdir)
    for char in ("/", ".", "_"):
        text = text.replace(char, "-")
    return text


class Fake:
    def __init__(self, argv: list[str]):
        self.argv = argv
        self.model_value = flag(argv, "--model")
        self.session_id = flag(argv, "--session-id") or flag(argv, "--resume") or str(uuid.uuid4())
        self.inbox: "queue.Queue[dict | None]" = queue.Queue()
        self.interrupted = threading.Event()
        self.out_lock = threading.Lock()
        self.log_path = os.environ.get("SUBFLEET_FAKE_TURN_LOG")
        self.transcript: Path | None = None
        projects = os.environ.get("CLAUDE_FAKE_PROJECTS_DIR")
        if projects:
            directory = Path(projects) / encode_project_dir(os.getcwd())
            directory.mkdir(parents=True, exist_ok=True)
            self.transcript = directory / f"{self.session_id}.jsonl"
        self.log({"argv": argv, "pid": os.getpid()})

    # --- plumbing --------------------------------------------------------------

    def log(self, row: dict) -> None:
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(row) + "\n")

    def emit(self, row: dict) -> None:
        row.setdefault("session_id", self.session_id)
        with self.out_lock:
            sys.stdout.write(json.dumps(row, separators=(",", ":")) + "\n")
            sys.stdout.flush()

    def record(self, row: dict) -> None:
        if self.transcript is not None:
            row = {**row, "sessionId": self.session_id, "cwd": os.getcwd(),
                   "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
            with open(self.transcript, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, separators=(",", ":")) + "\n")

    def reader(self) -> None:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            self.log({"stdin": raw, "pid": os.getpid()})
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            request = row.get("request") or {}
            if row.get("type") == "control_request" and request.get("subtype") == "interrupt":
                self.emit({"type": "control_response", "response": {
                    "subtype": "success", "request_id": row.get("request_id"), "response": {}}})
                self.interrupted.set()
                continue
            self.inbox.put(row)
        self.inbox.put(None)

    def next_row(self, timeout: float | None = None) -> dict | None:
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            return {}

    # --- the session -----------------------------------------------------------

    def run(self) -> int:
        threading.Thread(target=self.reader, daemon=True).start()
        while True:
            row = self.next_row()
            if row is None:
                return 0                        # stdin EOF
            if row.get("type") == "control_request":
                self.control(row)
            elif row.get("type") == "user":
                code = self.turn(row)
                if code is not None:
                    return code

    def control(self, row: dict) -> None:
        subtype = (row.get("request") or {}).get("subtype")
        if subtype != "initialize":
            self.emit({"type": "control_response", "response": {
                "subtype": "error", "request_id": row.get("request_id"), "error": f"fake: no {subtype}"}})
            return
        fast = os.environ.get("SUBFLEET_FAKE_FAST", "on")
        self.emit({"type": "control_response", "response": {
            "subtype": "success", "request_id": row.get("request_id"), "response": {
                "account": {"email": account_email(), "organization": "Fake org",
                            "subscriptionType": "max", "apiProvider": "firstParty"},
                "models": CATALOG, "fast_mode_state": fast,
                "fast_mode_disabled_reason": None if fast == "on" else "fake: off",
                "current_permission_mode": flag(self.argv, "--permission-mode") or "default",
                "pid": os.getpid()}}})

    def turn(self, row: dict) -> int | None:
        message = row.get("message") or {}
        content = message.get("content")
        text = content if isinstance(content, str) else " ".join(
            str(b.get("text") or "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
        images = [b for b in content or [] if isinstance(b, dict) and b.get("type") == "image"] \
            if isinstance(content, list) else []
        match = DIRECTIVE.search(text)
        scenario = match.group(1) if match else "reply"
        self.interrupted.clear()
        if scenario == "exit-before-ack":
            return 1
        self.record({"type": "user", "uuid": row.get("uuid"), "message": {"role": "user", "content": text}})
        self.emit({"type": "user", "uuid": row.get("uuid"), "message": message, "parent_tool_use_id": None,
                   "isReplay": True})
        model = served_model(self.model_value)
        if scenario == "wrong-model":
            model = "claude-haiku-4-5-20251001"
        self.emit({"type": "system", "subtype": "init", "model": model, "cwd": os.getcwd(),
                   "permissionMode": flag(self.argv, "--permission-mode") or "default",
                   "fast_mode_state": os.environ.get("SUBFLEET_FAKE_FAST", "on")})
        if scenario == "exit-after-ack":
            return 1
        handler = getattr(self, "scenario_" + scenario.replace("-", "_"), None)
        if handler is None:
            self.say(model, f"fake claude has no scenario {scenario!r}")
            return self.result(False, "error_during_execution")
        reply = f"Fake Claude read {len(text)} characters" + (f" and {len(images)} image(s)" if images else "") + "."
        return handler(model, reply)

    def say(self, model: str, text: str, *, stream: bool = True) -> None:
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        if stream:
            self.emit({"type": "stream_event", "event": {"type": "message_start", "message": {"id": mid}}})
            for piece in re.findall(r".{1,12}", text, re.S):
                self.emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                              "delta": {"type": "text_delta", "text": piece}}})
            self.emit({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}})
        body = {"id": mid, "type": "message", "role": "assistant", "model": model,
                "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5}}
        self.emit({"type": "assistant", "message": body, "parent_tool_use_id": None})
        self.record({"type": "assistant", "uuid": str(uuid.uuid4()), "message": body})

    def result(self, ok: bool, subtype: str = "success", *, text: str = "", errors=None) -> None:
        self.emit({"type": "result", "subtype": subtype, "is_error": not ok, "num_turns": 1, "result": text,
                   "errors": errors or [], "permission_denials": [], "duration_ms": 5})
        return None

    # --- scenarios -------------------------------------------------------------

    def scenario_reply(self, model: str, reply: str):
        self.say(model, reply)
        return self.result(True, text=reply)

    def scenario_background(self, model: str, reply: str):
        self.say(model, reply)
        self.result(True, text=reply)
        time.sleep(0.2)
        self.say(model, "Background task finished.", stream=False)
        return None

    def ask(self, model: str, tool: str, tool_input: dict) -> dict | None:
        """A tool_use, its `can_use_tool` request, and the host's answer."""
        tool_id = f"toolu_{uuid.uuid4().hex[:10]}"
        request_id = f"perm-{uuid.uuid4().hex[:8]}"
        self.emit({"type": "assistant", "parent_tool_use_id": None, "message": {
            "id": f"msg_{uuid.uuid4().hex[:12]}", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "tool_use", "id": tool_id, "name": tool, "input": tool_input}]}})
        self.emit({"type": "control_request", "request_id": request_id, "request": {
            "subtype": "can_use_tool", "tool_name": tool, "input": tool_input, "tool_use_id": tool_id,
            "permission_suggestions": [], "decision_reason": "fake: asks every time"}})
        while True:
            if self.interrupted.is_set():
                self.emit({"type": "control_cancel_request", "request_id": request_id})
                return None
            row = self.next_row(timeout=0.05)
            if row is None:
                return None
            if row.get("type") == "control_response" and (row.get("response") or {}).get("request_id") == request_id:
                answer = (row["response"].get("response") or {})
                answer["_tool_id"] = tool_id
                return answer

    def scenario_approval(self, model: str, reply: str):
        answer = self.ask(model, "Bash", {"command": "echo approved-by-person", "description": "Say hello"})
        if answer is None:
            return self.result(False, "error_during_execution", text="interrupted")
        if answer.get("behavior") == "allow":
            self.emit({"type": "user", "parent_tool_use_id": None, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": answer["_tool_id"], "content": "approved-by-person",
                 "is_error": False}]}})
            self.say(model, "The command ran.")
            return self.result(True, text="The command ran.")
        if answer.get("interrupt"):
            return self.result(False, "error_during_execution", text="stopped by the person")
        self.say(model, f"Not run: {answer.get('message') or 'denied'}")
        return self.result(True, text="Not run.")

    def scenario_question(self, model: str, reply: str):
        questions = [{"question": "Which color?", "header": "Color", "multiSelect": False,
                      "options": [{"label": "Blue", "description": "calm"}, {"label": "Red", "description": "bold"}]}]
        answer = self.ask(model, "AskUserQuestion", {"questions": questions})
        if answer is None:
            return self.result(False, "error_during_execution", text="interrupted")
        chosen = (answer.get("updatedInput") or {}).get("answers")
        self.say(model, f"You chose {json.dumps(chosen, sort_keys=True)}.")
        return self.result(True, text="answered")

    def scenario_slow(self, model: str, reply: str):
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        self.emit({"type": "stream_event", "event": {"type": "message_start", "message": {"id": mid}}})
        for n in range(1200):
            if self.interrupted.is_set():
                return self.result(False, "error_during_execution", text="interrupted")
            self.emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                          "delta": {"type": "text_delta", "text": f"tick {n}\n"}}})
            time.sleep(0.05)
        self.say(model, reply)
        return self.result(True, text=reply)

    def scenario_stubborn(self, model: str, reply: str):
        signal.signal(signal.SIGINT, lambda *_: os._exit(130))
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        self.emit({"type": "stream_event", "event": {"type": "message_start", "message": {"id": mid}}})
        for n in range(1200):
            self.emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                          "delta": {"type": "text_delta", "text": f"still {n}\n"}}})
            time.sleep(0.05)
        return self.result(True, text=reply)

    def scenario_limit(self, model: str, reply: str):
        resets = int(time.time()) + 3 * 3600
        self.emit({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": resets, "rateLimitType": "five_hour",
            "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": resets}}}})
        self.emit({"type": "assistant", "error": "rate_limit", "is_api_error_message": True,
                   "parent_tool_use_id": None, "message": {
                       "id": f"msg_{uuid.uuid4().hex[:12]}", "type": "message", "role": "assistant",
                       "model": "<synthetic>", "content": [{"type": "text", "text": "You've hit your limit."}],
                       "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                                 "cache_read_input_tokens": 0}}})
        return self.result(False, "success", text="You've hit your limit.")


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    return Fake(argv).run()
