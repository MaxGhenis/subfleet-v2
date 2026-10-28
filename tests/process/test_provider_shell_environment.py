"""D-ENV3 against the real binaries: a lane command sees the launch's environment.

Each test is skipped unless its CLI is on PATH. None calls a model or holds a
credential: the CLI talks to a mock API on 127.0.0.1 that answers the first
request with one shell call printing whether a credential variable is set, and
the next with a final message. The throwaway HOME's `.zshrc` exports a dummy
credential, as the person's did on 2026-10-26.

Codex: the control runs Subfleet's own argv without `CODEX_SHELL_CONFIG`; where
the installed Codex has the shell snapshot on by default it shows the `.zshrc`
dummy, so the vector is live and this test can see it. With the launch's argv,
neither the `.zshrc` dummy nor the daemon parent's ANTHROPIC_API_KEY reaches a
command of `codex exec`, `codex exec resume`, or the `app-server` a
conversation turn runs.

Claude Code: its Bash tool's snapshot replays no export but PATH (Claude Code
2.1.284, verified 2026-10-27), so a `.zshrc` OPENAI_API_KEY never reaches a
Claude lane's command. This test fails if a later Claude Code starts to.
"""

from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import threading
import time

import pytest

from subfleet.adapters.codex import CodexAdapter
from subfleet.contracts import Credential, JobSpec, Lane, LaneOwner, Sandbox
from subfleet.conversations.launch import codex_launch
from subfleet.daemon import Daemon
from subfleet.launch_env import CODEX_SHELL_CONFIG
from subfleet.provider_env import ProviderEnvironment

CODEX = shutil.which("codex")
CLAUDE = shutil.which("claude")
needs_codex = pytest.mark.skipif(CODEX is None, reason="the codex CLI is not on PATH")
needs_claude = pytest.mark.skipif(CLAUDE is None, reason="the claude CLI is not on PATH")

PARENT_DUMMY, ZSHRC_DUMMY = "parent-dummy", "zshrc-dummy"
SCRIPT = 'echo "ANTHROPIC=${ANTHROPIC_API_KEY:-MISSING}"'
TIMEOUT_S = 120


class _Model(http.server.BaseHTTPRequestHandler):
    """A Responses endpoint: one shell call, then a final message."""

    outputs: list[str]

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        if not self.path.endswith("/responses"):
            self.send_response(404)
            self.end_headers()
            return
        results = [item.get("output") for item in body.get("input", [])
                   if isinstance(item, dict) and item.get("type") == "function_call_output"]
        self.server.outputs.extend(str(result) for result in results)
        tools = {tool.get("name") for tool in body.get("tools", []) if isinstance(tool, dict)}
        if results:
            item = {"type": "message", "role": "assistant", "id": "msg_1",
                    "content": [{"type": "output_text", "text": "done"}]}
        elif "shell_command" in tools:
            item = {"type": "function_call", "name": "shell_command", "call_id": "call_1",
                    "arguments": json.dumps({"command": SCRIPT})}
        else:
            item = {"type": "function_call", "name": "exec_command", "call_id": "call_1",
                    "arguments": json.dumps({"cmd": SCRIPT})}
        events = [{"type": "response.created", "response": {"id": "resp"}},
                  {"type": "response.output_item.done", "item": item},
                  {"type": "response.completed", "response": {"id": "resp", "usage": {
                      "input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}}]
        data = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def model():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Model)
    server.outputs = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A lane, a throwaway HOME whose .zshrc exports a dummy, and the daemon's parent."""
    home, codex_home, workdir = tmp_path / "home", tmp_path / "codex-home", tmp_path / "work"
    for directory in (home, codex_home, workdir):
        directory.mkdir()
    (home / ".zshrc").write_text(f"export ANTHROPIC_API_KEY={ZSHRC_DUMMY}\n")
    node = shutil.which("node")
    path = ":".join(dict.fromkeys([str(Path(CODEX).parent), *([str(Path(node).parent)] if node else []),
                                   "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
    parent = {"PATH": path, "HOME": str(home), "SHELL": "/bin/zsh", "TMPDIR": str(tmp_path) + "/",
              "LANG": "en_US.UTF-8", "ANTHROPIC_API_KEY": PARENT_DUMMY}
    monkeypatch.setattr(os, "environ", parent)
    lane = Lane("codex-1", "codex", "codex:fake", Credential("codex", str(codex_home), "home"),
                str(codex_home), LaneOwner.V2, False)
    daemon = Daemon.__new__(Daemon)
    daemon._provider_environment = ProviderEnvironment({"providers": {"codex": {"ca_bundle": None}}})
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Run the check.\n")
    job = JobSpec(request_id="shell", kind="dispatch", workdir=str(workdir), prompt_path=str(prompt),
                  task=None, tier="standard", pinned_model=None, pinned_lane=None,
                  sandbox=Sandbox.READ_ONLY, out_path=None, name="shell")
    return {"lane": lane, "daemon": daemon, "job": job, "prompt": prompt, "tmp": tmp_path,
            "credential": {"CODEX_HOME": str(codex_home)}}


def _provider(model) -> list[str]:
    port = model.server_address[1]
    return ["-c", 'model_provider="mock"', "-c",
            f'model_providers.mock={{name="mock", base_url="http://127.0.0.1:{port}/v1", '
            'wire_api="responses", requires_openai_auth=false}']


def _with_provider(argv, model) -> list[str]:
    argv = list(argv)
    return [argv[0], argv[1], *_provider(model), *argv[2:]]


def _without_shell_config(argv) -> list[str]:
    argv, kept = list(argv), []
    at = 0
    while at < len(argv):
        if argv[at] == "-c" and at + 1 < len(argv) and argv[at + 1] in CODEX_SHELL_CONFIG:
            at += 2
            continue
        kept.append(argv[at])
        at += 1
    return kept


def _exec(world, model, argv, launch) -> tuple[list[str], str]:
    """Run one `codex exec` launch; the command outputs the model saw, and the thread id."""
    env = world["daemon"]._launch_environment(world["lane"], launch)
    assert "ANTHROPIC_API_KEY" not in env
    model.outputs.clear()
    with open(launch.stdin_path, "rb") as stdin:
        done = subprocess.run(_with_provider(argv, model), cwd=launch.cwd, env=env, stdin=stdin,
                              capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    thread = next((event.get("thread_id") for event in map(json.loads, filter(None, done.stdout.splitlines()))
                   if event.get("type") == "thread.started"), None)
    return list(model.outputs), thread


def _seen(outputs) -> set[str]:
    return {line.split("=", 1)[1] for output in outputs for line in output.splitlines()
            if line.startswith("ANTHROPIC=")}


def _snapshot_default_on(env) -> bool:
    listed = subprocess.run([CODEX, "features", "list"], env=env, capture_output=True, text=True,
                            timeout=TIMEOUT_S, check=False).stdout
    return any(line.split()[:1] == ["shell_snapshot"] and line.split()[-1] == "true"
               for line in listed.splitlines())


@needs_codex
def test_codex_exec_and_resume_commands_see_neither_dummy(world, model):
    adapter = CodexAdapter(codex_bin=CODEX)
    launch = adapter.build_launch(world["job"], "shell/a1", world["tmp"] / "a1", world["lane"],
                                  world["credential"], "mock-model", None, world["prompt"], None)
    env = world["daemon"]._launch_environment(world["lane"], launch)

    control, _ = _exec(world, model, _without_shell_config(launch.argv), launch)
    assert _seen(control), "the control's command never ran"
    assert PARENT_DUMMY not in _seen(control)
    if _snapshot_default_on(env):
        # The 2026-10-26 vector, live in this Codex: ~/.zshrc's export reaches the command.
        assert _seen(control) == {ZSHRC_DUMMY}

    outputs, thread = _exec(world, model, launch.argv, launch)
    assert _seen(outputs) == {"MISSING"}
    assert thread, "codex exec reported no thread"

    resumed = adapter.resume_launch(world["job"], "shell/a2", world["tmp"] / "a2", world["lane"],
                                    world["credential"], thread, world["prompt"], None)
    outputs, _ = _exec(world, model, resumed.argv, resumed)
    assert _seen(outputs) == {"MISSING"}


@needs_codex
def test_codex_conversation_turn_commands_see_neither_dummy(world, model):
    """The app-server path (C-26): `codex_turn.argv` carries the same pins."""
    turn = {"provider": "codex", "conversation_id": "conversation", "message_id": "message",
            "text": "Run the check.", "cwd": world["job"].workdir, "new_session_id": None,
            "settings": {"model": "mock-model", "permission": "read-only"}}
    launch = codex_launch(turn, attempt_id="turn/a1", attempt_dir=world["tmp"] / "t1", lane=world["lane"],
                          credential_env=world["credential"], model_id="mock-model", executable=CODEX,
                          override="hooks={}")
    env = world["daemon"]._launch_environment(world["lane"], launch)
    assert "ANTHROPIC_API_KEY" not in env
    process = subprocess.Popen(_with_provider(launch.argv, model), cwd=launch.cwd, env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)

    def send(message):
        process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()

    completed = False
    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "test", "title": "test", "version": "1"}}})
        send({"method": "initialized"})
        send({"id": 2, "method": "thread/start", "params": {"cwd": launch.cwd, "model": "mock-model",
                                                            "sandbox": "read-only", "approvalPolicy": "never"}})
        pending, deadline = b"", time.monotonic() + TIMEOUT_S
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while not completed and time.monotonic() < deadline:
                if not selector.select(1):
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    message = json.loads(line)
                    if message.get("id") == 2 and "result" in message:
                        send({"id": 3, "method": "turn/start", "params": {
                            "threadId": message["result"]["thread"]["id"],
                            "input": [{"type": "text", "text": "Run the check."}], "approvalPolicy": "never",
                            "sandboxPolicy": {"type": "readOnly", "networkAccess": False}}})
                    completed = completed or message.get("method") == "turn/completed"
    finally:
        process.terminate()
        process.wait(timeout=30)
    assert completed, "the turn did not complete"
    assert _seen(model.outputs) == {"MISSING"}


class _Messages(http.server.BaseHTTPRequestHandler):
    """A Messages endpoint: one Bash call, then a final message."""

    script = 'echo "OPENAI=${OPENAI_API_KEY:-MISSING}"'

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def _reply(self, data: bytes, kind: str):
        self.send_response(200)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        if "/v1/messages" not in self.path or "count_tokens" in self.path:
            self._reply(json.dumps({"input_tokens": 1}).encode(), "application/json")
            return
        results = [block.get("content") for message in body.get("messages", [])
                   if isinstance(message.get("content"), list) for block in message["content"]
                   if isinstance(block, dict) and block.get("type") == "tool_result"]
        self.server.outputs.extend(r if isinstance(r, str) else json.dumps(r) for r in results)
        tools = {tool.get("name") for tool in body.get("tools", []) if isinstance(tool, dict)}
        if "Bash" in tools and not results:
            block, stop = {"type": "tool_use", "id": "toolu_1", "name": "Bash",
                           "input": {"command": self.script, "description": "check"}}, "tool_use"
        else:
            block, stop = {"type": "text", "text": "done"}, "end_turn"
        usage = {"input_tokens": 1, "output_tokens": 1}
        if not body.get("stream"):
            self._reply(json.dumps({"id": "msg_1", "type": "message", "role": "assistant", "model": "mock",
                                    "content": [block], "stop_reason": stop, "stop_sequence": None,
                                    "usage": usage}).encode(), "application/json")
            return
        if block["type"] == "tool_use":
            opened = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        else:
            opened, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": block["text"]}
        events = [{"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
                                                        "model": "mock", "content": [], "stop_reason": None,
                                                        "stop_sequence": None, "usage": usage}},
                  {"type": "content_block_start", "index": 0, "content_block": opened},
                  {"type": "content_block_delta", "index": 0, "delta": delta},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                   "usage": {"output_tokens": 1}},
                  {"type": "message_stop"}]
        self._reply("".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode(),
                    "text/event-stream")


@needs_claude
def test_claude_code_bash_sees_no_zshrc_export(tmp_path):
    home, config, workdir = tmp_path / "home", tmp_path / "claude-config", tmp_path / "work"
    for directory in (home, config, workdir):
        directory.mkdir()
    (home / ".zshrc").write_text("export OPENAI_API_KEY=zshrc-dummy\n")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Messages)
    server.outputs = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {"HOME": str(home), "PATH": f"{Path(CLAUDE).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
           "SHELL": "/bin/zsh", "TMPDIR": str(tmp_path) + "/", "CLAUDE_CONFIG_DIR": str(config),
           "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}",
           "ANTHROPIC_API_KEY": "dummy-auth", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
           "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1"}
    try:
        done = subprocess.run([CLAUDE, "-p", "Run the check.", "--output-format", "json", "--model", "mock",
                               "--dangerously-skip-permissions"], cwd=workdir, env=env, capture_output=True,
                              text=True, timeout=TIMEOUT_S, stdin=subprocess.DEVNULL, check=False)
    finally:
        server.shutdown()
        server.server_close()
    assert done.returncode == 0, done.stderr[-2000:]
    seen = {line.split("=", 1)[1] for output in server.outputs for line in output.splitlines()
            if line.startswith("OPENAI=")}
    assert seen == {"MISSING"}
