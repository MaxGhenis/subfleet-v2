"""History pages read like live turns (C-29.8, design §12): a tool call carries its
result and outcome, thinking the transcript kept is a thought, and a call at a
page's edge still finds its result."""

from __future__ import annotations

import json

from subfleet.conversations import history


def write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def assistant(uid, *blocks):
    return {"type": "assistant", "uuid": uid, "timestamp": f"2026-09-01T10:00:{uid[-2:]}.000Z",
            "message": {"role": "assistant", "content": list(blocks)}}


def results(uid, *pairs):
    return {"type": "user", "uuid": uid, "timestamp": f"2026-09-01T10:00:{uid[-2:]}.000Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": t, "content": text, "is_error": err} for t, text, err in pairs]}}


def test_claude_calls_carry_results_errors_and_thoughts(tmp_path):
    path = write(tmp_path / "s.jsonl", [
        assistant("a-01", {"type": "thinking", "thinking": "plan", "signature": "s"},
                  {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                  {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "false"}}),
        results("u-02", ("t1", "a\nb", False), ("t2", "exit 1", True)),
        assistant("a-03", {"type": "thinking", "thinking": "", "signature": "omitted"},
                  {"type": "text", "text": "done"}),
    ])
    items, nxt = history._claude_items(path, None, 50)
    assert nxt is None
    shown = [(i["kind"], i["text"], i.get("preview"), i.get("is_error")) for i in reversed(items)]
    assert shown == [("thinking", "plan", None, None), ("tool", "ls", "a\nb", False),
                     ("tool", "false", "exit 1", True), ("text", "done", None, None)]


def test_a_call_at_a_pages_edge_keeps_its_result(tmp_path):
    """Newest first: the result was on the previous page; the next page still finds it."""
    path = write(tmp_path / "s.jsonl", [
        assistant("a-01", {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/x"}}),
        results("u-02", ("t1", "x's contents", False)),
        assistant("a-03", {"type": "text", "text": "one"}),
        assistant("a-04", {"type": "text", "text": "two"}),
    ])
    first, cursor = history._claude_items(path, None, 2)
    assert [i["text"] for i in first] == ["two", "one"] and cursor is not None
    second, _ = history._claude_items(path, cursor, 2)
    assert [(i["kind"], i["preview"]) for i in second] == [("tool", "x's contents")]


def test_a_hidden_call_keeps_no_output(tmp_path):
    path = write(tmp_path / "s.jsonl", [
        assistant("a-01", {"type": "tool_use", "id": "t1", "name": "Bash",
                           "input": {"command": "security find-generic-password -s x -w"}}),
        results("u-02", ("t1", "hunter2", False)),
    ])
    [item], _ = history._claude_items(path, None, 50)
    assert item["hidden"] is True and item["preview"] == "" and "hunter2" not in json.dumps(item)


def test_codex_calls_outputs_and_reasoning_as_the_rollout_writes_them(tmp_path):
    """Shapes from a codex-cli 0.153.3 rollout (2026-09-24): `custom_tool_call`
    (code mode `exec`), `function_call` with a namespace, their outputs by
    call id, and reasoning with a summary."""
    def item(payload, second):
        return {"type": "response_item", "timestamp": f"2026-09-24T21:59:{second:02d}.000Z", "payload": payload}
    path = write(tmp_path / "rollout.jsonl", [
        item({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "check the notes"}]}, 1),
        item({"type": "reasoning", "summary": [{"type": "summary_text", "text": "Read the notes first"}],
              "encrypted_content": "gAAA"}, 2),
        item({"type": "custom_tool_call", "call_id": "c1", "name": "exec",
              "input": 'const r = await tools.exec_command({cmd:"cat notes.md"})'}, 3),
        item({"type": "custom_tool_call_output", "call_id": "c1",
              "output": [{"type": "input_text", "text": "Script completed\nnotes"}]}, 4),
        item({"type": "function_call", "call_id": "c2", "name": "send_message", "namespace": "collaboration",
              "arguments": json.dumps({"target": "/root", "message": "done"})}, 5),
        item({"type": "function_call_output", "call_id": "c2", "output": ""}, 6),
        item({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "All read."}]}, 7),
    ])
    found, nxt = history._codex_items(path, None, 50)
    items = list(reversed(found))
    assert nxt is None
    shown = [(i["kind"], i.get("tool"), i["text"][:40], i.get("preview")) for i in items]
    assert shown == [
        ("text", None, "check the notes", None),
        ("thinking", None, "Read the notes first", None),
        ("tool", "command", "cat notes.md", "Script completed\nnotes"),
        ("tool", "collaboration/send_message", "description: to /root", ""),
        ("text", None, "All read.", None),
    ]



def codex_row(payload, second):
    return {"type": "response_item", "timestamp": f"2026-09-24T21:59:{second:02d}.000Z", "payload": payload}


def test_codex_credential_reads_hide_the_call_wherever_the_script_names_them(tmp_path):
    """Review: judged on the whole code-mode script, not only its cmd:"…" literals."""
    script = ('await tools.exec_command({cmd:"ls"}); '
              "await tools.exec_command({cmd:'security find-generic-password -s x -w'})")
    path = write(tmp_path / "r.jsonl", [
        codex_row({"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": script}, 1),
        codex_row({"type": "custom_tool_call_output", "call_id": "c1", "output": "hunter2"}, 2)])
    [item], _ = history._codex_items(path, None, 50)
    assert item["hidden"] is True and item["preview"] == "" and "hunter2" not in json.dumps(item)


def test_a_codex_row_that_cannot_be_read_is_skipped_and_escapes_are_kept(tmp_path):
    path = write(tmp_path / "r.jsonl", [
        codex_row({"type": "custom_tool_call", "call_id": "c1", "name": "exec",
                   "input": 'await tools.exec_command({cmd:"printf \\x41"})'}, 1),
        codex_row({"type": "message", "role": "assistant", "content": "not a list of parts"}, 2),
        codex_row({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}, 3)])
    found, _ = history._codex_items(path, None, 50)
    assert [i["text"] for i in reversed(found)] == ["printf \\x41", "ok"]


def test_codex_history_pages_and_its_cursor_survives_new_turns(tmp_path):
    """Review: Codex history pages like Claude's, and a cursor is a byte offset,
    so rows appended after a page was read do not shift the next page."""
    rows = [codex_row({"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"m{i}"}]}, i)
            for i in range(6)]
    path = write(tmp_path / "r.jsonl", rows)
    first, cursor = history._codex_items(path, None, 2)
    assert [i["text"] for i in first] == ["m5", "m4"] and cursor is not None
    with path.open("a") as f:
        for i in range(6, 9):
            f.write(json.dumps(codex_row({"type": "message", "role": "user",
                                          "content": [{"type": "input_text", "text": f"m{i}"}]}, i)) + "\n")
    second, cursor = history._codex_items(path, cursor, 2)
    assert [i["text"] for i in second] == ["m3", "m2"]
    last, cursor = history._codex_items(path, cursor, 5)
    assert [i["text"] for i in last] == ["m1", "m0"] and cursor is None


def test_claude_cursor_survives_new_turns(tmp_path):
    path = write(tmp_path / "s.jsonl", [assistant(f"a-{i:02d}", {"type": "text", "text": f"t{i}"}) for i in range(5)])
    first, cursor = history._claude_items(path, None, 2)
    with path.open("a") as f:
        f.write(json.dumps(assistant("a-09", {"type": "text", "text": "new"})) + "\n")
    second, _ = history._claude_items(path, cursor, 2)
    assert [i["text"] for i in first] == ["t4", "t3"] and [i["text"] for i in second] == ["t2", "t1"]


def test_codex_outputs_that_report_failure_are_failures(tmp_path):
    path = write(tmp_path / "r.jsonl", [
        codex_row({"type": "custom_tool_call", "call_id": "c1", "name": "exec",
                   "input": 'await tools.exec_command({cmd:"false"})'}, 1),
        codex_row({"type": "custom_tool_call_output", "call_id": "c1",
                   "output": [{"type": "input_text", "text": "Script failed\nError: exit 1"}]}, 2),
        codex_row({"type": "function_call", "call_id": "c2", "name": "shell", "arguments": json.dumps({"command": ["true"]})}, 3),
        codex_row({"type": "function_call_output", "call_id": "c2", "output": "Exit code: 0\nok"}, 4)])
    found, _ = history._codex_items(path, None, 50)
    assert [(i["text"], i["is_error"]) for i in reversed(found)] == [("false", True), ("true", False)]
