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
    items = list(reversed(history._codex_items(path, None, 50)))
    shown = [(i["kind"], i.get("tool"), i["text"][:40], i.get("preview")) for i in items]
    assert shown == [
        ("text", None, "check the notes", None),
        ("thinking", None, "Read the notes first", None),
        ("tool", "command", "cat notes.md", "Script completed\nnotes"),
        ("tool", "collaboration/send_message", "description: to /root", ""),
        ("text", None, "All read.", None),
    ]
