"""The two brief readers of a labelled handoff: C-30.3, C-23.14, C-23.36.

A conversation handoff's first message is a brief of the source's native
history: a Claude transcript (`subfleet/sessions/handoff.py`) or a Codex rollout
(`subfleet/conversations/codex_brief.py`). Both are held to the same scrub
rules and the same caps, and neither runs git when a daemon handler builds it
(C-25.3).

Rollouts here are written in code from the record shapes the fake app-server
writes (`tests/fake/interactive_codex.py`: `session_meta`, `turn_context`,
`response_item` messages) and the `ResponseItem` variants of the pinned 0.153.3
schema (`tests/fixtures/codex/app-server-0.153.3/ClientRequest.json`). No real
rollout or transcript is read. `sessions_fixtures.FAKE_SECRET` is shaped like a
key and is not one.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from subfleet.conversations import codex_brief
from subfleet.sessions import handoff
from tests import sessions_fixtures as fx

THREAD = "0199a1b2-7c3d-4e5f-8a6b-1c2d3e4f5a6b"
SESSION = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
CODE = "def total(rows):\n    return sum(row.amount for row in rows)\n"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJlLXZhbHVl"
PLAIN = "hunter2-plain-value-no-regex-would-know"   # what a keychain read returns


# --- rollout records ------------------------------------------------------------

def record(kind: str, payload: dict) -> dict:
    return {"timestamp": "2026-09-24T12:00:00.000Z", "type": kind, "payload": payload}


def meta(thread: str = THREAD, cwd: str = "/work/repo", **extra) -> dict:
    return record("session_meta", {"id": thread, "cwd": cwd, "originator": "codex_desktop",
                                   "cli_version": "0.153.3", **extra})


def user(text: str, client_id: str | None = None) -> dict:
    return record("response_item", {"type": "message", "role": "user", "client_id": client_id,
                                    "content": [{"type": "input_text", "text": text}]})


def assistant(text: str) -> dict:
    return record("response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": text}]})


def call(name: str, arguments: dict, call_id: str) -> dict:
    return record("response_item", {"type": "function_call", "name": name,
                                    "arguments": json.dumps(arguments), "call_id": call_id})


def output(call_id: str, text) -> dict:
    return record("response_item", {"type": "function_call_output", "call_id": call_id, "output": text})


def custom_call(name: str, text: str, call_id: str) -> dict:
    return record("response_item", {"type": "custom_tool_call", "name": name, "input": text,
                                    "call_id": call_id})


def custom_output(call_id: str, text: str) -> dict:
    return record("response_item", {"type": "custom_tool_call_output", "call_id": call_id, "output": text})


def shell(command: list[str], call_id: str, env: dict | None = None) -> dict:
    return record("response_item", {"type": "local_shell_call", "call_id": call_id, "status": "completed",
                                    "action": {"type": "exec", "command": command, "env": env or {}}})


def reasoning(summary: str, raw: str) -> dict:
    return record("response_item", {"type": "reasoning", "summary": [{"type": "summary_text", "text": summary}],
                                    "content": [{"type": "reasoning_text", "text": raw}],
                                    "encrypted_content": "gAAAA" + "x" * 40})


def write_rollout(home: Path, records: list[dict], *, thread: str = THREAD, sub: str = "sessions") -> Path:
    directory = home / sub / "2026" / "09" / "24"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-09-24T12-00-00-{thread}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


@pytest.fixture
def policy():
    return fx.policy()


@pytest.fixture
def caps(policy):
    return dict(policy["sessions"]["handoff_caps"])


@pytest.fixture
def workspace(tmp_path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def codex(tmp_path, workspace, caps, records, **kw):
    path = write_rollout(tmp_path / "codex-home", [meta(cwd=str(workspace)), *records])
    return codex_brief.build_brief(THREAD, path, workspace, None, caps, repository=False, **kw), path


# --- both readers ---------------------------------------------------------------

def claude_source(tmp_path, monkeypatch, task: str, reply: str, tool_input: dict, tool_output: str):
    home = fx.claude_home(tmp_path, monkeypatch)
    return fx.transcript(home, SESSION, [
        fx.typed_prompt(task, uuid="p0", at=fx.ago(3600)),
        fx.assistant_tool_use(uuid="a1", at=fx.ago(600), tool_input=tool_input),
        fx.user_tool_result(tool_output, uuid="r1", at=fx.ago(500)),
        fx.assistant_text(reply, uuid="last", at=fx.ago(60))])


def codex_source(tmp_path, task: str, reply: str, tool_input: dict, tool_output: str):
    return write_rollout(tmp_path / "codex-home", [
        meta(), user("<environment_context>\n  <cwd>/work/repo</cwd>\n</environment_context>"), user(task),
        call("shell", tool_input, "c1"), output("c1", tool_output), assistant(reply)])


def brief_of(reader: str, tmp_path, monkeypatch, workspace, caps, *, task, reply, tool_input, tool_output):
    if reader == "claude":
        path = claude_source(tmp_path, monkeypatch, task, reply, tool_input, tool_output)
        return handoff.build_brief(SESSION, path, workspace, str(workspace), caps, repository=False), path
    path = codex_source(tmp_path, task, reply, tool_input, tool_output)
    return codex_brief.build_brief(THREAD, path, workspace, None, caps, repository=False), path


@pytest.mark.parametrize("reader", ["claude", "codex"])
def test_both_readers_scrub_secrets_and_binary_and_keep_code(reader, tmp_path, monkeypatch, workspace, caps):
    """C-30.3, C-23.14: private keys, JWTs, prefixed tokens and `Bearer` values are
    replaced and encoded binary omitted, while code, commands and output stay
    verbatim, whichever provider's history the brief is read from."""
    pem = ("-----BEGIN OPENSSH PRIVATE KEY-----\n" + "b3BlbnNzaC1rZXktdjEAAAAA\n" * 4
           + "-----END OPENSSH PRIVATE KEY-----")
    noisy = (f"export ANTHROPIC_API_KEY={fx.FAKE_SECRET}\n"
             f"curl -H 'Authorization: Bearer {JWT}' https://example.test/v1\n{CODE}{pem}\n"
             f"payload = 'data:image/png;base64,{'A' * 200}'\n")
    brief, path = brief_of(reader, tmp_path, monkeypatch, workspace, caps,
                           task=f"Port the importer. token {fx.FAKE_SECRET}", reply=f"done: Bearer {JWT}",
                           tool_input={"command": "cat notes.md"}, tool_output=noisy)
    for secret in (fx.FAKE_SECRET, JWT, "b3BlbnNzaC1rZXktdjEAAAAA", "A" * 200):
        assert secret not in brief.text
    assert "[REDACTED]" in brief.text and "PRIVATE KEY REDACTED" in brief.text
    assert CODE.strip() in brief.text, "ordinary code is retained verbatim"
    assert "cat notes.md" in brief.text and "curl -H" in brief.text
    assert "Port the importer." in brief.original
    assert f"Source transcript: {path}" in brief.text and brief.transcript == str(path)
    assert brief.redactions >= 4


@pytest.mark.parametrize("reader", ["claude", "codex"])
def test_both_readers_bound_every_section(reader, tmp_path, monkeypatch, workspace, caps):
    """C-23.36: each section is cut to its own cap, the cut is marked, and the
    newest context is the one kept."""
    caps.update(original_task=200, recent=400, tool_result=120, tool_input=80, progress=150, repository=60)
    task = "port the ledger importer to v2 " * 200
    big = "row imported ok\n" * 400
    (workspace / "PROGRESS.md").write_text("done so far: the manifest\n" * 300, encoding="utf-8")
    brief, _path = brief_of(reader, tmp_path, monkeypatch, workspace, caps, task=task, reply="the newest reply",
                            tool_input={"command": "cat big " + "x " * 200}, tool_output=big)
    sections = brief.text.split("\n## ")
    by_title = {s.split("\n", 1)[0]: s.split("\n", 1)[1].strip() for s in sections[1:]}
    assert len(by_title["Original task"]) <= 200
    recent = next(v for k, v in by_title.items() if k.startswith("Recent"))
    assert len(recent) <= 400 and recent.endswith("the newest reply")
    assert len(by_title["PROGRESS.md"]) <= 150
    assert len(by_title["Repository state"]) <= 60
    assert "characters omitted" in brief.text
    assert task not in brief.text and big not in brief.text


@pytest.mark.parametrize("reader", ["claude", "codex"])
def test_a_conversation_handoff_runs_no_git(reader, tmp_path, monkeypatch, workspace, caps):
    """C-25.3: the handler that builds a conversation's brief never waits on git;
    the repository section says the state was not collected."""
    def no_git(*args, **kwargs):
        raise AssertionError(f"git ran: {args}")
    monkeypatch.setattr(handoff.subprocess, "run", no_git)
    brief, _path = brief_of(reader, tmp_path, monkeypatch, workspace, caps, task="continue", reply="ok",
                            tool_input={"command": "ls"}, tool_output="a\nb\n")
    repository = brief.text.split("## Repository state", 1)[1].strip()
    assert repository == handoff.REPOSITORY_NOT_COLLECTED


@pytest.mark.parametrize("reader", ["claude", "codex"])
def test_both_readers_name_their_provider_and_source(reader, tmp_path, monkeypatch, workspace, caps):
    """C-30.3: the brief says where it came from, so it is never mistaken for the
    source session itself."""
    brief, path = brief_of(reader, tmp_path, monkeypatch, workspace, caps, task="continue", reply="ok",
                           tool_input={"command": "ls"}, tool_output="a\n")
    if reader == "claude":
        assert "- Source provider: Claude Code" in brief.text and f"- Source session: {SESSION}" in brief.text
        assert "## Recent main-chain excerpt" in brief.text
    else:
        assert "- Source provider: Codex" in brief.text and f"- Source thread: {THREAD}" in brief.text
        assert "## Recent rollout excerpt" in brief.text
    order = ["# Cross-agent handoff", "## Original task", "## Recent", "## PROGRESS.md", "## Repository state"]
    positions = [brief.text.index(heading) for heading in order]
    assert positions == sorted(positions)
    assert f"Target cwd: {workspace}" in brief.text


# --- the Codex reader -------------------------------------------------------------

@pytest.mark.parametrize("item", [
    call("exec_command", {"cmd": "agent-secret get github"}, "c9"),
    call("shell", {"command": ["bash", "-lc", "printenv"]}, "c9"),
    call("shell", {"command": ["bash", "-lc", "ls\nenv | sort"]}, "c9"),
    call("shell", {"command": ["cat", "/Users/someone/.codex/auth.json"]}, "c9"),
    call("shell", {"command": ["security", "find-generic-password", "-s", "x", "-w"]}, "c9"),
    shell(["cat", ".env"], "c9"),
    custom_call("keychain_read", "anything", "c9"),
])
def test_a_credential_reading_codex_call_is_omitted_by_pattern(item, tmp_path, workspace, caps):
    """C-23.14: the input and the output of a call that reads credentials are
    omitted by pattern, before any redaction: a keychain value has no shape."""
    kind = item["payload"]["type"]
    answer = custom_output("c9", PLAIN) if kind == "custom_tool_call" else output("c9", PLAIN)
    brief, _path = codex(tmp_path, workspace, caps, [user("fix the build"), item, answer, assistant("done")])
    assert PLAIN not in brief.text
    assert handoff.OMITTED_SENSITIVE in brief.text
    assert handoff.OMITTED_SENSITIVE_INPUT in brief.text


def test_ordinary_codex_calls_are_context_verbatim(tmp_path, workspace, caps):
    """C-23.14: commands, patches and their output are the value of a brief."""
    patch = "*** Begin Patch\n*** Update File: a.py\n+print('hi')\n*** End Patch"
    brief, _path = codex(tmp_path, workspace, caps, [
        user("fix the build"),
        call("shell", {"command": ["bash", "-lc", "git log --oneline -1"]}, "c1"), output("c1", "abc123 first"),
        call("exec_command", {"cmd": "pytest -q"}, "c2"),
        output("c2", [{"type": "input_text", "text": "3 passed"}]),
        custom_call("apply_patch", patch, "c3"), custom_output("c3", "Success. Updated a.py"),
        shell(["ls", "-la"], "c4", env={"TOKEN_VALUE": PLAIN}), output("c4", "a.py\n"),
        assistant("all green")])
    for kept in ("git log --oneline -1", "abc123 first", "pytest -q", "3 passed", patch, "Success. Updated a.py",
                 "ls -la", "all green", "Codex tool call (shell):", "Codex tool result (exec_command):"):
        assert kept in brief.text, kept
    assert PLAIN not in brief.text, "a local shell call's environment is never shown"
    assert handoff.OMITTED_SENSITIVE not in brief.text


def test_codex_reasoning_never_reaches_a_brief(tmp_path, workspace, caps):
    """C-23.14, design D-11: Codex reasoning (summaries, raw text, encrypted
    content) is not carried; the header says thinking was omitted."""
    brief, _path = codex(tmp_path, workspace, caps, [
        user("fix the build"), reasoning("SUMMARY-OF-THOUGHT", "RAW-CHAIN-OF-THOUGHT"), assistant("done")])
    assert "SUMMARY-OF-THOUGHT" not in brief.text and "RAW-CHAIN-OF-THOUGHT" not in brief.text
    assert "gAAAA" not in brief.text
    assert "thinking, binary payloads" in brief.text


def test_codex_binary_and_image_outputs_are_omitted(tmp_path, workspace, caps):
    """C-23.14: encoded binary and images are omitted rather than pasted."""
    brief, _path = codex(tmp_path, workspace, caps, [
        user("look at the logo"),
        call("shell", {"command": ["cat", "logo.png"]}, "c1"), output("c1", "\x00\x01\x02\x03" * 200),
        call("view_image", {"path": "shot.png"}, "c2"),
        output("c2", [{"type": "input_image", "image_url": "data:image/png;base64," + "Q" * 400}]),
        assistant("seen")])
    assert handoff.OMITTED_BINARY in brief.text and codex_brief.OMITTED_MEDIA in brief.text
    assert "\x00" not in brief.text and "Q" * 400 not in brief.text


def test_a_codex_output_whose_call_is_outside_the_excerpt_is_omitted(tmp_path, workspace, caps):
    """C-23.14: unknown sensitivity means omitted, as the Claude reader does."""
    caps["recent_records"] = 2
    brief, _path = codex(tmp_path, workspace, caps, [
        user("start"), call("shell", {"command": ["cat", "notes.md"]}, "c1"),
        output("c1", PLAIN), assistant("done")])
    assert PLAIN not in brief.text and handoff.OMITTED_UNMATCHED in brief.text


def test_injected_context_is_not_the_persons_task(tmp_path, workspace, caps):
    """C-30.3: the original task is the person's first message, not the context
    the client injected ahead of it (text beginning `<`, as catalog.py reads it)."""
    brief, _path = codex(tmp_path, workspace, caps, [
        user("<user_instructions>be terse</user_instructions>"), user("Port the importer to v2.", "cid-1"),
        assistant("working"), user("and add tests")])
    assert brief.original == "Port the importer to v2."
    recent = brief.text.split("## Recent rollout excerpt", 1)[1]
    assert "Codex user:\nand add tests" in recent
    assert "Port the importer to v2." not in recent, "the task is not repeated in the excerpt"
    assert "be terse" not in brief.text


def test_a_rollout_with_no_task_or_another_thread_is_refused(tmp_path, workspace, caps):
    """C-30.3: a brief needs the person's task, and the rollout must be the thread named."""
    path = write_rollout(tmp_path / "home", [meta(), assistant("hello")])
    with pytest.raises(handoff.HandoffError, match="no user task"):
        codex_brief.build_brief(THREAD, path, workspace, None, caps)
    other = "0199a1b2-0000-4000-8000-000000000000"
    path = write_rollout(tmp_path / "home2", [meta(thread=other), user("go")], thread=THREAD)
    with pytest.raises(handoff.HandoffError, match="belongs to thread"):
        codex_brief.build_brief(THREAD, path, workspace, None, caps)


def test_finding_a_rollout_by_thread_id(tmp_path):
    """C-30.2, C-30.3: a thread is found in the homes given, archived or not; a
    name that is not a UUID never reaches a path pattern; two rollouts for one
    thread in one home are refused."""
    home = tmp_path / "home"
    assert codex_brief.find_rollout(THREAD, [home]) is None
    archived = write_rollout(home, [meta(), user("go")], sub="archived_sessions")
    assert codex_brief.find_rollout(THREAD, [tmp_path / "elsewhere", home]) == archived
    for bad in ("*", f"{THREAD}*", "../x", "", None):
        with pytest.raises(handoff.HandoffError):
            codex_brief.find_rollout(bad, [home])
    assert codex_brief.canonical_thread_id(THREAD.upper()) == THREAD, "as a Claude id, case is canonicalised"
    write_rollout(home, [meta(), user("go")])
    with pytest.raises(handoff.HandoffError, match="2 rollouts"):
        codex_brief.find_rollout(THREAD, [home])


def test_exec_and_subagent_rollouts_are_lane_runs(tmp_path):
    """C-30.1, C-23.31: the catalog's own predicate says which rollouts are runs."""
    run = write_rollout(tmp_path / "a", [meta(source="exec"), user("brief")])
    sub = write_rollout(tmp_path / "b", [meta(source={"subagent": "review"}), user("brief")])
    person = write_rollout(tmp_path / "c", [meta(source="vscode"), user("hi")])
    assert codex_brief.headless_run(run) and codex_brief.headless_run(sub)
    assert not codex_brief.headless_run(person)


def test_the_claude_reader_still_runs_git_when_asked(tmp_path, monkeypatch, workspace, caps):
    """C-23.36: `subfleet sessions handoff` keeps its repository section; only a
    conversation handoff leaves git out."""
    home = fx.claude_home(tmp_path, monkeypatch)
    path = fx.transcript(home, SESSION, [fx.typed_prompt("go", uuid="p0", at=fx.ago(60))])
    subprocess.run(["git", "init", "-q", "-b", "work", str(workspace)], check=True, capture_output=True)
    brief = handoff.build_brief(SESSION, path, workspace, str(workspace), caps)
    assert "### Status" in brief.text
