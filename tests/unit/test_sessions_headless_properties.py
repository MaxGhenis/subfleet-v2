"""C-23.31's transcript shape as properties over generated transcripts.

Hypothesis draws transcripts line by line: entries of every type Claude Code
writes, each with or without an `entrypoint` (headless, interactive, another of
Claude Code's values, an unknown string, empty, or not a string), with or
without `promptSource`, as text prompts, tool results, or malformed messages,
mixed with lines that are valid JSON but not objects and lines that are not JSON.

  P1 a transcript any entry of which names a non-headless entrypoint is never
     headless (the 2026-10-03 bug: desktop sessions read as lane runs);
  P2 a transcript in which no entry names an entrypoint is judged exactly as
     before (`legacy`, v1's prompt rule as `headless_transcript` applied it
     until 2026-10-03, with the non-object guards the release line had added);
  P3 `headless_transcript` agrees with `spec`, an order-free restatement of the
     clause, on every transcript;
  P4 a headless transcript stays headless however many entries a headless
     process appends (v1's rule dropped a lane at its third prompt);
  P5 only the first `max_lines` lines are read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet.sessions import transcripts

HEADLESS = sorted(transcripts.HEADLESS_ENTRYPOINTS)
INTERACTIVE = ["claude-desktop", "cli", "claude-vscode", "mcp", "remote", "local-agent"]
ABSENT = object()


def _named(entry: dict) -> bool:
    value = entry.get("entrypoint")
    return isinstance(value, str) and bool(value)


def _objects(lines: list[str], max_lines: int) -> list[dict]:
    out = []
    for line in lines[:max_lines]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _is_prompt(entry: dict) -> bool:
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return not (isinstance(content, list) and content and all(
        isinstance(item, dict) and item.get("type") == "tool_result" for item in content))


def legacy(lines: list[str], max_lines: int = 5000) -> bool:
    """v1's prompt rule: one or two text prompts, all `promptSource: sdk`."""
    prompts = [entry for entry in _objects(lines, max_lines) if _is_prompt(entry)]
    return 0 < len(prompts) <= 2 and all(entry.get("promptSource") == "sdk" for entry in prompts)


def spec(lines: list[str], max_lines: int = 5000) -> bool:
    """C-23.31 as the contract states it, read in no particular order."""
    entries = _objects(lines, max_lines)
    named = [entry for entry in entries if _named(entry)]
    if any(entry["entrypoint"] not in transcripts.HEADLESS_ENTRYPOINTS for entry in named):
        return False
    prompts = [entry for entry in entries if not _named(entry) and _is_prompt(entry)]
    if len(prompts) > 2 or any(entry.get("promptSource") != "sdk" for entry in prompts):
        return False
    return bool(named) or bool(prompts)


# --- generated transcripts ----------------------------------------------------

entrypoints = st.one_of(
    st.just(ABSENT), st.sampled_from(HEADLESS), st.sampled_from(INTERACTIVE),
    st.sampled_from([None, "", 7, ["sdk-cli"]]), st.text(max_size=8))
sources = st.one_of(st.just(ABSENT), st.sampled_from(["sdk", "typed", "system", None]))
contents = st.one_of(
    st.just([{"type": "text", "text": "a prompt"}]),
    st.just([{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]),
    st.just("plain text"), st.just([]), st.just(None))


@st.composite
def entries(draw, entrypoint=entrypoints) -> dict:
    entry = {"type": draw(st.sampled_from(["user", "user", "user", "assistant", "system",
                                            "attachment", "custom-title"])),
             "uuid": "u"}
    content = draw(contents)
    if draw(st.booleans()):
        entry["message"] = {"role": entry["type"], "content": content}
    else:
        entry["message"] = draw(st.sampled_from([None, "x", 3]))
    if draw(st.integers(0, 4)) == 0:
        entry["isMeta"] = True
    for key, strategy in (("entrypoint", entrypoint), ("promptSource", sources)):
        value = draw(strategy)
        if value is not ABSENT:
            entry[key] = value
    return entry


def lines_of(entry_strategy):
    junk = st.sampled_from(["[1]", '"text"', "7", "null", "not json {", ""])
    return st.lists(st.one_of(entry_strategy.map(json.dumps), junk), max_size=14)


@pytest.fixture(scope="module")
def scratch(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("headless") / "t.jsonl"


def judge(path: Path, lines: list[str], **kwargs) -> bool:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return transcripts.headless_transcript(path, **kwargs)


PROPERTY = settings(max_examples=400, deadline=None)


@PROPERTY
@given(lines=lines_of(entries()), at=st.integers(0, 14), writer=st.sampled_from(INTERACTIVE))
def test_p1_an_interactive_entrypoint_is_never_headless(scratch, lines, at, writer):
    """P1 (C-23.31): one entry a non-headless process wrote makes a session."""
    entry = {"type": "assistant", "entrypoint": writer, "message": {"content": []}}
    lines = [*lines[:at], json.dumps(entry), *lines[at:]]
    assert judge(scratch, lines) is False


@PROPERTY
@given(lines=lines_of(entries(entrypoint=st.one_of(
    st.just(ABSENT), st.sampled_from([None, "", 7, ["sdk-cli"]])))))
def test_p2_with_no_entrypoint_the_legacy_rule_is_unchanged(scratch, lines):
    """P2 (C-23.31): a transcript that names no entrypoint reads as before."""
    assert judge(scratch, lines) is legacy(lines)


@PROPERTY
@given(lines=lines_of(entries()))
def test_p3_the_reader_agrees_with_the_clause(scratch, lines):
    """P3 (C-23.31): the streaming reader and the order-free statement agree."""
    assert judge(scratch, lines) is spec(lines)


@PROPERTY
@given(lines=lines_of(entries()), more=st.lists(entries(entrypoint=st.sampled_from(HEADLESS)),
                                                 min_size=1, max_size=6))
def test_p4_a_lane_stays_a_lane_whatever_a_headless_process_adds(scratch, lines, more):
    """P4 (C-23.31): a resumed or notified `claude -p` run is still a lane run."""
    if not judge(scratch, lines):
        return
    assert judge(scratch, [*lines, *map(json.dumps, more)]) is True


@PROPERTY
@given(lines=lines_of(entries()), window=st.integers(0, 14))
def test_p5_only_the_window_is_read(scratch, lines, window):
    """P5 (C-23.31): lines past `max_lines` are never read."""
    assert judge(scratch, lines, max_lines=window) is judge(scratch, lines[:window])
