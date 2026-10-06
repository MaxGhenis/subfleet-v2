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
its text; `[fake:write]` combines with any other:

    (none)            streamed text, then a success result
    write             first writes `fake-<first 8 of the message uuid>.txt` (three
                      lines) and appends `edited by <those 8>` to `tracked.txt` in its
                      working directory, as a turn that edits files does (C-26.14)
    approval          asks to run a Bash command; allow runs it, deny says so
    question          AskUserQuestion; the chosen answers are echoed back
    questions         AskUserQuestion with several questions, including multiSelect
    slow              streams until interrupted; the interrupt ends the turn
    quiet-slow        waits without assistant output until interrupted
    stubborn          acknowledges interrupts and keeps going; SIGINT ends it with no result
    stubborn-result   acknowledges interrupts and keeps going; SIGINT ends the turn with an
                      `error_during_execution` result, as Claude Code 2.1.280 did in the
                      2026-09-24 live probe (docs/desktop/reviews/2026-09-24-live-probes.md)
    immovable         ignores interrupts, SIGINT and the end of stdin; only containment ends it
    limit             a rejected rate_limit_event, then an error result
    exit-after-ack    replays the message, then exits with no result
    exit-before-ack   reads the message, then exits without replaying it
    stop-before-ack   reads the message, then waits for interrupt without acknowledging it
    background        a success result, then more assistant text before EOF
    wrong-model       serves another model than the one asked for
    bash              runs SUBFLEET_FAKE_BASH_COMMAND as its Bash tool, then succeeds
    steer             waits for a queued message, folds it at a tool boundary, then replies
    steer-late        waits for a queued message, ends without another boundary; it runs next

Title directives combine with turn scenarios: `title-error` answers the
`generate_session_title` control with an error, `title-null` returns no title,
`title-timeout` never answers, and `title-delayed` answers after 1.5 seconds.
Otherwise that control returns "Fixture session title", even during a turn.

Environment:

    CLAUDE_CODE_OAUTH_TOKEN   the fixture token; its suffix names the account
    SUBFLEET_FAKE_FAST        the `fast_mode_state` to report (default "on")
    SUBFLEET_FAKE_TURN_LOG    append argv (with the cwd) and every stdin line here (JSON lines)
    CLAUDE_FAKE_PROJECTS_DIR  write the session transcript under this directory
    SUBFLEET_FAKE_BASH_COMMAND
                              the command the `bash` scenario runs, under
                              `/bin/sh -c` in this process's cwd, with
                              CLAUDECODE=1 and CLAUDE_CODE_SESSION_ID set to this
                              session: the two variables `cli.in_claude_session`
                              records the harness exporting to every Bash tool
                              process. Logged to SUBFLEET_FAKE_TURN_LOG with its
                              rc, stdout and stderr.
    SUBFLEET_FAKE_HOOK_COMMAND
                              run this command as a SessionStart hook at startup
                              and a UserPromptSubmit hook for each message, as
                              Claude Code 2.1.280 runs a command hook: its own
                              process, this process's environment, the hook JSON
                              on stdin, and the event name appended (the shape
                              `daemon install --hooks` writes). Source `startup`
                              for `--session-id`, `resume` for `--resume`, as
                              observed (docs/desktop/reviews/2026-09-24-live-
                              probes.md, "Hooks inside a Subfleet launch"). Each
                              run is logged to SUBFLEET_FAKE_TURN_LOG with its rc
                              and stdout.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import signal
import subprocess
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
        self.queue_lock = threading.RLock()
        self.queued: dict[str, dict] = {}
        self.active_uuids: list[str] = []
        self.result_index = 0
        self.log_path = os.environ.get("SUBFLEET_FAKE_TURN_LOG")
        self.transcript: Path | None = None
        projects = os.environ.get("CLAUDE_FAKE_PROJECTS_DIR")
        if projects:
            directory = Path(projects) / encode_project_dir(os.getcwd())
            directory.mkdir(parents=True, exist_ok=True)
            self.transcript = directory / f"{self.session_id}.jsonl"
        self.log({"argv": argv, "pid": os.getpid(), "cwd": os.getcwd()})

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
            if row.get("type") == "control_request" and request.get("subtype") == "generate_session_title":
                # 2.1.280 dispatches this control asynchronously; a turn or a
                # slow titler must not prevent the reader handling an interrupt.
                threading.Thread(target=self.generate_title, args=(row,), daemon=True).start()
                continue
            if row.get("type") == "user":
                self.lifecycle(row.get("uuid"), "queued")
                if row.get("priority") is not None:
                    with self.queue_lock:
                        self.queued[row["uuid"]] = row
                    self.inbox.put({})             # wake an idle main loop
                    continue
            if row.get("type") == "control_request" and request.get("subtype") == "get_settings":
                # Answered at once, mid-turn too, as 2.1.280 does (C-26.8; observed
                # 2026-09-28: 77 ms after the message, before the turn's `system init`).
                self.emit({"type": "control_response", "response": {
                    "subtype": "success", "request_id": row.get("request_id"),
                    "response": {"applied": self.applied(), "effective": {}, "sources": {}}}})
                continue
            if row.get("type") == "control_request" and request.get("subtype") == "interrupt":
                with self.queue_lock:
                    cancelled = list(self.queued) if request.get("cancel_queued") else []
                    for mid in cancelled:
                        self.queued.pop(mid)
                        self.lifecycle(mid, "cancelled")
                    receipt = {"cancelled": cancelled, "still_queued": list(self.queued)}
                self.emit({"type": "control_response", "response": {
                    "subtype": "success", "request_id": row.get("request_id"), "response": receipt}})
                self.interrupted.set()
                continue
            if row.get("type") == "control_request" and request.get("subtype") == "cancel_async_message":
                mid = request.get("message_uuid")
                with self.queue_lock:
                    cancelled = self.queued.pop(mid, None) is not None
                    if cancelled:
                        self.lifecycle(mid, "cancelled")
                self.emit({"type": "control_response", "response": {
                    "subtype": "success", "request_id": row.get("request_id"),
                    "response": {"cancelled": cancelled}}})
                continue
            self.inbox.put(row)
        self.inbox.put(None)

    def generate_title(self, row: dict) -> None:
        """The installed CLI's `{description, persist?}` -> `{title: str|null}` control."""
        request = row["request"]
        description = request.get("description")
        response = {"subtype": "success", "request_id": row.get("request_id")}
        if not isinstance(description, str) or not isinstance(request.get("persist", True), bool):
            response.update(subtype="error", error="fake: invalid title request")
        else:
            directives = DIRECTIVE.findall(description)
            if "title-timeout" in directives:
                return
            if "title-delayed" in directives:
                time.sleep(1.5)
            if "title-error" in directives:
                response.update(subtype="error", error="fake: titler unavailable")
            else:
                response["response"] = {"title": None if "title-null" in directives else "Fixture session title"}
        self.log({"title_response": response, "pid": os.getpid()})
        self.emit({"type": "control_response", "response": response})

    def applied(self) -> dict:
        """`get_settings`'s `applied`, from the launch flags: ultracode is on where the
        settings ask for it at xhigh (or `--effort ultracode`); Haiku applies no effort."""
        model = served_model(flag(self.argv, "--model"))
        effort = flag(self.argv, "--effort")
        try:
            settings = json.loads(flag(self.argv, "--settings") or "{}")
        except ValueError:
            settings = {}
        ultracode = effort == "ultracode" or (effort == "xhigh" and settings.get("ultracode") is True)
        if "haiku" in model:
            effort, ultracode = None, False
        return {"model": model, "effort": "xhigh" if effort == "ultracode" else effort, "advisor": None,
                "ultracode": ultracode}

    def hook(self, event: str, **fields) -> None:
        """Run the configured command hook for `event`, if there is one."""
        command = os.environ.get("SUBFLEET_FAKE_HOOK_COMMAND")
        if not command:
            return
        body = {"session_id": self.session_id,
                "transcript_path": str(self.transcript) if self.transcript else "",
                "cwd": os.getcwd(), "hook_event_name": event, **fields}
        try:
            done = subprocess.run([*shlex.split(command), event], input=json.dumps(body),
                                  capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            self.log({"hook": event, "error": f"{type(exc).__name__}: {exc}", "pid": os.getpid()})
            return
        # The C-5.1 markers the hook inherited: ids and a path, never a secret.
        markers = {name: os.environ[name] for name in ("SUBFLEET_ATTEMPT", "SUBFLEET_JOB", "SUBFLEET_ROOT")
                   if name in os.environ}
        self.log({"hook": event, "rc": done.returncode, "stdout": done.stdout,
                  "source": fields.get("source"), "markers": markers, "pid": os.getpid()})

    def next_row(self, timeout: float | None = None) -> dict | None:
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            return {}

    # --- the session -----------------------------------------------------------

    def run(self) -> int:
        self.hook("SessionStart", source="resume" if flag(self.argv, "--resume") else "startup")
        threading.Thread(target=self.reader, daemon=True).start()
        while True:
            with self.queue_lock:
                row = self.queued.pop(next(iter(self.queued))) if self.queued else None
            if row is None:
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
        self.active_uuids = [row.get("uuid")]
        message = row.get("message") or {}
        content = message.get("content")
        text = content if isinstance(content, str) else " ".join(
            str(b.get("text") or "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
        images = [b for b in content or [] if isinstance(b, dict) and b.get("type") == "image"] \
            if isinstance(content, list) else []
        self.hook("UserPromptSubmit", prompt=text)
        directives = DIRECTIVE.findall(text)
        scenario = next((d for d in directives if d != "write" and not d.startswith("title-")), "reply")
        self.interrupted.clear()
        if scenario == "stop-before-ack":
            self.log({"stop_before_ack": row.get("uuid")})
            self.interrupted.wait(30)
            return 1
        if scenario == "exit-before-ack":
            return 1
        self.lifecycle(row.get("uuid"), "started")
        self.record({"type": "user", "uuid": row.get("uuid"), "message": {"role": "user", "content": text}})
        self.emit({"type": "user", "uuid": row.get("uuid"), "message": message, "parent_tool_use_id": None,
                   "isReplay": True})
        model = served_model(self.model_value)
        if scenario == "wrong-model":
            model = "claude-haiku-4-5-20251001"
        self.emit({"type": "system", "subtype": "init", "model": model, "cwd": os.getcwd(),
                   "permissionMode": flag(self.argv, "--permission-mode") or "default",
                   "capabilities": ["msg_lifecycle_v1", "interrupt_receipt_v1", "interrupt_cancel_queued_v1"],
                   "fast_mode_state": os.environ.get("SUBFLEET_FAKE_FAST", "on")})
        if scenario == "exit-after-ack":
            return 1
        if "write" in directives:
            self.write_files(str(row.get("uuid") or "no-uuid"))
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
        # The actual protocol ends folded commands before result, and the command
        # which started this turn after it. Consumption evidence survives failure.
        for mid in self.active_uuids[1:]:
            self.lifecycle(mid, "completed" if ok else "cancelled")
        with self.queue_lock:
            queued_turn_count = len(self.queued)
        self.emit({"type": "result", "subtype": subtype, "is_error": not ok, "num_turns": 1, "result": text,
                   "errors": errors or [], "permission_denials": [], "duration_ms": 5,
                   "user_message_uuids": list(self.active_uuids), "queued_turn_count": queued_turn_count,
                   "result_index": self.result_index})
        self.result_index += 1
        if self.active_uuids:
            self.lifecycle(self.active_uuids[0], "completed" if ok else "cancelled")
        return None

    def lifecycle(self, mid: str, state: str) -> None:
        self.emit({"type": "command_lifecycle", "command_uuid": mid, "state": state,
                   "uuid": str(uuid.uuid4())})

    def tool_boundary(self, model: str) -> None:
        """A priority next/now input becomes part of the running query at this boundary."""
        with self.queue_lock:
            folded = [row for row in self.queued.values() if row.get("priority") in ("next", "now")]
            for row in folded:
                self.queued.pop(row["uuid"])
                self.active_uuids.append(row["uuid"])
                self.lifecycle(row["uuid"], "started")
                self.record({"type": "user", "uuid": row["uuid"], "message": row["message"]})
                self.emit({**row, "isReplay": True})
        for row in folded:
            text = " ".join(b.get("text", "") for b in row["message"]["content"] if b.get("type") == "text")
            self.say(model, f"Steered: {text}")

    def write_files(self, message_uuid: str) -> None:
        """Edit the working directory as a writing turn would: one new file, one
        appended line in an existing one."""
        tag = message_uuid[:8]
        cwd = Path(os.getcwd())
        (cwd / f"fake-{tag}.txt").write_text(f"written by fake claude\nfor message {tag}\nline three\n")
        with open(cwd / "tracked.txt", "a", encoding="utf-8") as stream:
            stream.write(f"edited by {tag}\n")

    # --- scenarios -------------------------------------------------------------

    def scenario_reply(self, model: str, reply: str):
        self.say(model, reply)
        return self.result(True, text=reply)

    def wait_for_steer(self) -> bool:
        for _ in range(1200):
            if self.interrupted.is_set():
                return False
            with self.queue_lock:
                if self.queued:
                    return True
            time.sleep(0.05)
        return False

    def scenario_steer(self, model: str, reply: str):
        if not self.wait_for_steer():
            return self.result(False, "error_during_execution", text="interrupted")
        self.tool_boundary(model)
        self.say(model, reply)
        return self.result(True, text=reply)

    def scenario_steer_late(self, model: str, reply: str):
        if not self.wait_for_steer():
            return self.result(False, "error_during_execution", text="interrupted")
        self.say(model, reply)
        return self.result(True, text=reply)

    def scenario_quiet_slow(self, model: str, reply: str):
        if self.interrupted.wait(timeout=60):
            return self.result(False, "error_during_execution", text="interrupted")
        return self.scenario_reply(model, reply)

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
            self.tool_boundary(model)
            self.say(model, "The command ran.")
            return self.result(True, text="The command ran.")
        if answer.get("interrupt"):
            return self.result(False, "error_during_execution", text="stopped by the person")
        self.say(model, f"Not run: {answer.get('message') or 'denied'}")
        return self.result(True, text="Not run.")

    def scenario_question(self, model: str, reply: str):
        questions = [{"question": "Which color?", "header": "Color", "multiSelect": False,
                      "options": [{"label": "Blue", "description": "calm"}, {"label": "Red", "description": "bold"}]}]
        return self.answer_questions(model, questions)

    def scenario_questions(self, model: str, reply: str):
        questions = [
            {"question": "Which color?", "header": "Color", "multiSelect": False,
             "options": [{"label": "Blue", "description": "calm"}, {"label": "Red", "description": "bold"}]},
            {"question": "Which features?", "header": "Features", "multiSelect": True,
             "options": [{"label": "Fast", "description": "Short turnaround"},
                         {"label": "Thorough", "description": "More detail"},
                         {"label": "Quiet", "description": "Fewer updates"}]},
            {"question": "When is it needed?", "header": "Deadline", "multiSelect": False,
             "options": [{"label": "Today"}, {"label": "Tomorrow"}]},
        ]
        return self.answer_questions(model, questions)

    def answer_questions(self, model: str, questions: list[dict]):
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

    def scenario_bash(self, model: str, reply: str):
        """One Bash tool call, run without a permission request (as for a command an
        allow rule approved), its output returned as the tool result."""
        command = os.environ.get("SUBFLEET_FAKE_BASH_COMMAND")
        if not command:
            self.say(model, "fake claude has no SUBFLEET_FAKE_BASH_COMMAND")
            return self.result(False, "error_during_execution")
        tool_id = f"toolu_{uuid.uuid4().hex[:10]}"
        self.emit({"type": "assistant", "parent_tool_use_id": None, "message": {
            "id": f"msg_{uuid.uuid4().hex[:12]}", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "tool_use", "id": tool_id, "name": "Bash",
                         "input": {"command": command, "description": "Run the command"}}]}})
        env = {**os.environ, "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": self.session_id}
        try:
            done = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True,
                                  timeout=120, env=env)
            rc, stdout, stderr = done.returncode, done.stdout, done.stderr
        except (OSError, subprocess.SubprocessError) as exc:
            rc, stdout, stderr = None, "", f"{type(exc).__name__}: {exc}"
        self.log({"bash": command, "rc": rc, "stdout": stdout, "stderr": stderr[-4000:],
                  "session_id": self.session_id, "pid": os.getpid()})
        self.emit({"type": "user", "parent_tool_use_id": None, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": stdout or stderr,
             "is_error": rc != 0}]}})
        self.tool_boundary(model)
        final = "The command ran." if rc == 0 else f"The command failed (rc={rc})."
        if os.environ.get("SUBFLEET_FAKE_BASH_WAKE") and rc == 0:
            final += "\nWAKE-ME: runs=" + stdout.strip() + ' note="Inspect the finished run"'
        self.say(model, final)
        return self.result(rc == 0, "success" if rc == 0 else "error_during_execution",
                           text=final)


    def scenario_stubborn_result(self, model: str, reply: str):
        sigint = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: sigint.set())
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        self.emit({"type": "stream_event", "event": {"type": "message_start", "message": {"id": mid}}})
        for n in range(1200):
            if sigint.is_set():
                return self.result(False, "error_during_execution", text="interrupted by SIGINT")
            self.emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                          "delta": {"type": "text_delta", "text": f"still {n}\n"}}})
            time.sleep(0.05)
        return self.result(True, text=reply)

    def scenario_immovable(self, model: str, reply: str):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        self.emit({"type": "stream_event", "event": {"type": "message_start", "message": {"id": mid}}})
        for n in range(1200):
            self.emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                          "delta": {"type": "text_delta", "text": f"immovable {n}\n"}}})
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
