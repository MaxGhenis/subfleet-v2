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


# --- paging (review of 14f818c, 2026-09-25) ------------------------------------


def every_page(read, path, limit):
    """Every page from the newest to the file's start: the items, and each page's
    size. Each cursor is below the one before it, so paging always ends."""
    items, sizes, cursor = [], [], None
    for _ in range(10_000):
        before = cursor
        page, cursor = read(path, cursor, limit)
        assert cursor is None or before is None or cursor < before, (before, cursor)
        items += page
        sizes.append(len(page))
        if cursor is None:
            return items, sizes
    raise AssertionError("paging never reached the file's start")


def test_the_page_that_reaches_the_first_row_ends_the_history(tmp_path):
    """A page that fills its limit on the file's first row (or on the first row
    after leading blank lines) says the history ended, so the app offers no
    "Load earlier" that would load an empty page (review, 2026-09-25)."""
    rows = [assistant(f"a-{i:02d}", {"type": "text", "text": f"row {i}"}) for i in range(1, 5)]
    for lead in ("", "\n", "\n \n"):
        path = tmp_path / f"s{len(lead)}.jsonl"
        path.write_text(lead + "".join(json.dumps(r) + "\n" for r in rows))
        first, cursor = history._claude_items(path, None, 2)
        rest, end = history._claude_items(path, cursor, 2)
        assert [i["text"] for i in first + rest] == ["row 4", "row 3", "row 2", "row 1"] and end is None, repr(lead)
    codex = [codex_row({"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": f"say {i}"}]}, i) for i in range(1, 5)]
    for lead in ("", "\n"):
        path = tmp_path / f"r{len(lead)}.jsonl"
        path.write_text(lead + "".join(json.dumps(r) + "\n" for r in codex))
        first, cursor = history._codex_items(path, None, 2)
        rest, end = history._codex_items(path, cursor, 2)
        assert [i["text"] for i in first + rest] == ["say 4", "say 3", "say 2", "say 1"] and end is None, repr(lead)


def test_the_read_cap_is_measured_from_the_cursor(tmp_path, monkeypatch):
    """Each page reads up to the cap below its own cursor, so a page far from the
    file's end is as full as the first (measured from the end, every page past
    the cap would hold one row). Both providers."""
    monkeypatch.setattr(history, "READ_CAP", 2_000)
    claude = write(tmp_path / "s.jsonl", [assistant(f"a-{i % 100:02d}", {"type": "text", "text": f"row {i}" + "." * 150})
                                          for i in range(80)])
    codex = write(tmp_path / "r.jsonl", [codex_row({"type": "message", "role": "assistant",
                                                    "content": [{"type": "output_text", "text": f"say {i}" + "." * 150}]}, i % 60)
                                         for i in range(80)])
    for read, path in ((history._claude_items, claude), (history._codex_items, codex)):
        items, sizes = every_page(read, path, 10**6)
        assert len(items) == 80
        assert len(sizes) <= 16 and min(sizes[:-1]) >= 5, (read.__name__, sizes)


def test_a_page_never_splits_a_rows_blocks(tmp_path):
    """A row's blocks all land on one page: the page may pass its limit by the rest
    of the row, and the next page starts below it (before, a row cut at the limit
    lost its remaining blocks for good)."""
    path = write(tmp_path / "s.jsonl", [
        assistant("a-01", {"type": "text", "text": "old"}),
        assistant("a-02", {"type": "thinking", "thinking": "think2", "signature": "s"},
                  {"type": "text", "text": "text2"},
                  {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "ls"}}),
        assistant("a-03", {"type": "text", "text": "new"})])
    first, cursor = history._claude_items(path, None, 2)
    assert [i["text"] for i in first] == ["new", "ls", "text2", "think2"]
    rest, cursor = history._claude_items(path, cursor, 2)
    assert [i["text"] for i in rest] == ["old"] and cursor is None


def test_a_read_cap_hands_on_a_cursor_instead_of_ending_the_history(tmp_path, monkeypatch):
    """More than the cap of rows with nothing to show (a huge tool result) below the
    cursor: the page may be empty, but its cursor goes on, and the rows below it
    are reached."""
    monkeypatch.setattr(history, "READ_CAP", 2_000)
    path = write(tmp_path / "s.jsonl", [
        assistant("a-01", {"type": "text", "text": "before the big result"}),
        results("u-02", ("t0", "x" * 5_000, False)),
        results("u-03", ("t9", "y" * 5_000, False)),
        assistant("a-04", {"type": "text", "text": "after"})])
    first, cursor = history._claude_items(path, None, 5)
    assert [i["text"] for i in first] == ["after"] and cursor is not None
    items, _ = every_page(history._claude_items, path, 5)
    assert [i["text"] for i in items] == ["after", "before the big result"]


def test_a_row_too_large_for_the_reader_is_stepped_over(tmp_path, monkeypatch):
    """A row larger than the reader's whole budget is never yielded; the history
    steps past it to the rows below instead of calling itself complete."""
    monkeypatch.setattr(history, "READ_BUDGET", 1_000)
    monkeypatch.setattr(history, "READ_CHUNK", 100)
    path = write(tmp_path / "s.jsonl", [
        assistant("a-01", {"type": "text", "text": "oldest"}),
        assistant("a-02", {"type": "text", "text": "z" * 20_000}),
        assistant("a-03", {"type": "text", "text": "newest"})])
    items, _ = every_page(history._claude_items, path, 1)
    assert [i["text"] for i in items] == ["newest", "oldest"]


def test_a_cursor_far_from_the_end_reads_from_the_cursor(tmp_path, monkeypatch):
    """The reader starts near the cursor, not at the file's end, so the whole history
    stays reachable whatever the reader's budget (before, rows more than 64 MiB from
    the end could not be paged to)."""
    monkeypatch.setattr(history, "READ_BUDGET", 3_000)
    monkeypatch.setattr(history, "READ_CHUNK", 500)
    rows = [assistant(f"a-{i % 100:02d}", {"type": "text", "text": f"row {i}"}) for i in range(300)]
    path = write(tmp_path / "s.jsonl", rows)
    items, _ = every_page(history._claude_items, path, 7)
    assert [i["text"] for i in items] == [f"row {i}" for i in reversed(range(300))]


def test_paging_through_random_transcripts_shows_every_item_once_in_order(tmp_path, monkeypatch):
    """Model check: for random transcripts (rows of one to four blocks, results on
    later rows, blank lines, huge rows), random limits and small caps and budgets,
    the pages joined are exactly the items one unbounded read gives, and every call
    keeps its result."""
    import random
    rng = random.Random(20260925)
    for trial in range(150):
        rows, pending, n = [], [], 0
        for _ in range(rng.randint(0, 40)):
            if pending and rng.random() < 0.3:
                rows.append(results(f"u-{n % 100:02d}", *[(t, f"out {t}", rng.random() < 0.2) for t in pending]))
                pending = []
            blocks = []
            for _ in range(rng.randint(1, 4)):
                kind = rng.choice(["text", "thinking", "tool"])
                n += 1
                if kind == "text":
                    blocks.append({"type": "text", "text": f"text {n}" + "·" * rng.choice([0, 0, 300, 3_000])})
                elif kind == "thinking":
                    blocks.append({"type": "thinking", "thinking": f"thought {n}", "signature": "s"})
                else:
                    pending.append(f"t{n}")
                    blocks.append({"type": "tool_use", "id": f"t{n}", "name": "Bash", "input": {"command": f"cmd {n}"}})
            rows.append(assistant(f"a-{n % 100:02d}", *blocks))
        path = tmp_path / f"s{trial}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" + ("\n" if rng.random() < 0.1 else "") for r in rows))
        with monkeypatch.context() as patch:
            patch.setattr(history, "READ_CAP", 10**9)
            patch.setattr(history, "READ_BUDGET", 10**9)
            expected, cursor = history._claude_items(path, None, 10**6)
        assert cursor is None
        budget = rng.choice([2_000, 20_000])
        limit = rng.randint(1, 6)
        for window in (10**9, 0):
            with monkeypatch.context() as patch:
                patch.setattr(history, "READ_CAP", rng.choice([500, 2_000, 8_000]))
                patch.setattr(history, "READ_BUDGET", budget)
                patch.setattr(history, "READ_CHUNK", rng.choice([64, 700, 5_000]))
                patch.setattr(history, "RESULT_WINDOW", window)
                paged, _ = every_page(history._claude_items, path, limit)
            # A row larger than the reader's budget is stepped over: it may be
            # missing, but everything else is there, once, in order.
            # (To be read, a row and the newline before it must fit in the budget.)
            big = {json.dumps(r) for r in rows if len(json.dumps(r)) + 2 >= budget}
            dropped = {i["text"] for r in big for i in history._claude_items(write(tmp_path / "one.jsonl",
                                                                                   [json.loads(r)]), None, 99)[0]}
            want = [i for i in expected if i["text"] not in dropped]
            got = [i for i in paged if i["text"] not in dropped]
            if window:     # every result is within reach: every call keeps its own
                key = lambda i: (i["kind"], i["text"], i.get("preview"), i.get("is_error"))
            else:          # no result past the cursor is looked for: the items still are
                key = lambda i: (i["kind"], i["text"])
            assert [key(i) for i in got] == [key(i) for i in want], (trial, window)


def test_codex_failures_are_judged_on_the_status_line_and_the_exit_code():
    """Review B-5: only an output's header says the command failed (its lines before
    `Output:`, or its first line), so a log it printed may say anything; a nonzero
    `metadata.exit_code` is a failure. The exec_command header is the one lane
    rollouts carry: `Chunk ID`, `Wall time`, `Process exited with code N`."""
    outcome = history._codex_outcome
    shell = "Chunk ID: 29e2d1\nWall time: 0.0000 seconds\nProcess exited with code {}\nOriginal token count: 9\nOutput:\n{}"
    assert outcome(shell.format(1, "boom"))[1] is True
    assert outcome(shell.format(137, ""))[1] is True
    assert outcome(shell.format(0, "Process exited with code 1\nScript failed"))[1] is False
    assert outcome("Script failed\nWall time 0.4 seconds\nOutput:\n\nScript error:\nno such process")[1] is True
    assert outcome("Script completed\nWall time 0.1 seconds\nOutput:\nExit code: 1\nScript failed")[1] is False
    assert outcome("Wall time: 20.0 seconds\nSleep completed.")[1] is False
    assert outcome([{"type": "input_text", "text": "Script completed\nExit code: 1\n"}])[1] is False
    assert outcome("Exit code: 0\nProcess exited with code 2 (in the log)")[1] is False
    assert outcome("Exit code: 1\nboom")[1] is True
    assert outcome("  Script failed\n")[1] is True
    assert outcome({"output": "boom", "metadata": {"exit_code": 2}}) == ("boom", True)
    assert outcome({"output": "fine", "metadata": {"exit_code": 0}}) == ("fine", False)
    assert outcome({"output": "x", "success": False})[1] is True


def test_paging_through_random_rollouts_shows_every_item_once_in_order(tmp_path, monkeypatch):
    """The model check for Codex rollouts: calls, outputs, reasoning and messages."""
    import random
    rng = random.Random(925)
    for trial in range(100):
        rows, pending = [], []
        for n in range(rng.randint(0, 40)):
            kind = rng.choice(["call", "output", "reasoning", "message", "other"])
            if kind == "call":
                pending.append(f"c{n}")
                payload = {"type": "function_call", "call_id": f"c{n}", "name": "shell",
                           "arguments": json.dumps({"command": [f"cmd{n}"]})}
            elif kind == "output" and pending:
                payload = {"type": "function_call_output", "call_id": pending.pop(0),
                           "output": rng.choice(["Exit code: 0\nok", "Exit code: 1\nno"])}
            elif kind == "reasoning":
                payload = {"type": "reasoning", "summary": [{"type": "summary_text", "text": f"why {n}"}]}
            elif kind == "message":
                payload = {"type": "message", "role": rng.choice(["user", "assistant"]),
                           "content": [{"type": "output_text", "text": f"say {n}" + "-" * rng.choice([0, 400, 4_000])}]}
            else:
                rows.append({"type": "event_msg", "payload": {"type": "token_count"}})
                continue
            rows.append(codex_row(payload, n % 60))
        path = write(tmp_path / f"r{trial}.jsonl", rows)
        with monkeypatch.context() as patch:
            patch.setattr(history, "READ_CAP", 10**9)
            patch.setattr(history, "READ_BUDGET", 10**9)
            expected, cursor = history._codex_items(path, None, 10**6)
        assert cursor is None
        budget = rng.choice([3_000, 50_000])
        with monkeypatch.context() as patch:
            patch.setattr(history, "READ_CAP", rng.choice([300, 3_000]))
            patch.setattr(history, "READ_BUDGET", budget)
            patch.setattr(history, "READ_CHUNK", rng.choice([64, 1_000]))
            paged, _ = every_page(history._codex_items, path, rng.randint(1, 5))
        big = {i["text"] for i in expected if len(i["text"]) > budget - 300}
        key = lambda i: (i["kind"], i["text"], i.get("preview"), i.get("is_error"))
        assert [key(i) for i in paged if i["text"] not in big] == [key(i) for i in expected if i["text"] not in big], trial


def test_leading_blank_space_ends_the_history_however_long(tmp_path):
    """Review of 5aa2718, findings 3 and 7: a page that does not fill its limit and
    reaches the first row after blank lines ends the history; so does a page
    below which lies only blank space longer than the probe (before, 66,000
    blank lines took 466 pages, most of them empty)."""
    rows = [assistant(f"a-{i:02d}", {"type": "text", "text": f"row {i}"}) for i in range(1, 4)]
    short = tmp_path / "short.jsonl"
    short.write_text("\n\n" + "".join(json.dumps(r) + "\n" for r in rows))
    items, cursor = history._claude_items(short, None, 10)
    assert [i["text"] for i in items] == ["row 3", "row 2", "row 1"] and cursor is None
    long = tmp_path / "long.jsonl"
    long.write_text("\n" * 70_000 + "".join(json.dumps(r) + "\n" for r in rows))
    items, sizes = every_page(history._claude_items, long, 2)
    assert [i["text"] for i in items] == ["row 3", "row 2", "row 1"] and len(sizes) <= 3, sizes


def test_a_status_line_is_judged_only_in_the_outputs_head(tmp_path):
    """A negative exit code is a failure; a header whose `Output:` line lies past
    `STATUS_HEAD` is not read for a status (outputs are bounded before judging)."""
    outcome = history._codex_outcome
    assert outcome("Chunk ID: a\nWall time: 0 seconds\nProcess exited with code -1\nOutput:\n")[1] is True
    late = "Chunk ID: a\n" + "w" * (history.STATUS_HEAD + 100) + "\nProcess exited with code 1\nOutput:\nok"
    assert outcome(late)[1] is False
