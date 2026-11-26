"""C-25.7: an agent inside a conversation turn learns about `subfleet suggest`.

The `SessionStart` hook (C-15.2 layer 3, C-26.13) adds one stable line to its
`additionalContext` inside a turn (`SUBFLEET_TURN_JOB`), on a new, cleared or
compacted session; nothing else changes, so a turn's command line, environment
and frames stay as they were (the d714 argv pin and C-24.9's recording).
"""

from __future__ import annotations

import io
import json

import pytest

from subfleet import hooks
from tests.unit.test_hooks import MARKERS, SESSION, notice, payload, service_notice, turn_markers


@pytest.fixture(autouse=True)
def outside_subfleet(monkeypatch):
    for name in (*MARKERS, "SUBFLEET_TURN_JOB"):
        monkeypatch.delenv(name, raising=False)


def in_a_turn(monkeypatch, root):
    for name, value in {**turn_markers(root), "SUBFLEET_TURN_JOB": "20260924-120000-turn-cv-demo"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(hooks, "wake_worker", lambda *a, **k: pytest.fail("a turn hands no wake to the kit"))


def context(stdout: io.StringIO) -> str | None:
    text = stdout.getvalue()
    return json.loads(text)["hookSpecificOutput"]["additionalContext"] if text else None


@pytest.mark.parametrize("source", hooks.SUGGEST_HINT_SOURCES)
def test_c25_7_a_turns_new_session_is_told_about_subfleet_suggest(daemon, root, monkeypatch, source):
    in_a_turn(monkeypatch, root)
    server = daemon({"notice.pending": lambda request: {"notices": []}})
    stdout = io.StringIO()
    assert hooks.session_event("SessionStart", payload("SessionStart", source=source), root, stdout=stdout) == 0
    told = context(stdout)
    assert told == hooks.SUGGEST_HINT
    assert "subfleet suggest --title" in told and "--brief-file" in told and "out-of-scope follow-up" in told
    assert server.ops() == ["notice.pending"]          # nothing is marked: the line is no notice


@pytest.mark.parametrize("source", ["resume", None, "unknown"])
def test_c25_7_a_resumed_turn_already_holds_the_line(daemon, root, monkeypatch, source):
    in_a_turn(monkeypatch, root)
    daemon({"notice.pending": lambda request: {"notices": []}})
    stdout = io.StringIO()
    hooks.session_event("SessionStart", payload("SessionStart", source=source), root, stdout=stdout)
    assert stdout.getvalue() == ""


def test_c25_7_a_prompt_is_never_given_the_line(daemon, root, monkeypatch):
    in_a_turn(monkeypatch, root)
    daemon({"notice.pending": lambda request: {"notices": []}})
    stdout = io.StringIO()
    hooks.session_event("UserPromptSubmit", payload("UserPromptSubmit", source="startup"), root, stdout=stdout)
    assert stdout.getvalue() == ""


def test_c25_7_a_lane_run_or_a_persons_session_is_not_told(daemon, root, monkeypatch):
    """Only a conversation turn names its job in SUBFLEET_TURN_JOB: a lane run carries
    the C-5.1 markers alone, and a person's session neither."""
    monkeypatch.setattr(hooks, "wake_worker", lambda *a, **k: None)
    daemon({"notice.pending": lambda request: {"notices": []}})
    stdout = io.StringIO()
    hooks.session_event("SessionStart", payload("SessionStart", source="startup"), root, stdout=stdout)
    assert stdout.getvalue() == ""
    for name, value in turn_markers(root).items():
        monkeypatch.setenv(name, value)
    hooks.session_event("SessionStart", payload("SessionStart", source="startup"), root, stdout=stdout)
    assert stdout.getvalue() == ""


def test_c25_7_the_line_rides_beside_a_job_notice_which_alone_is_marked(daemon, root, monkeypatch):
    in_a_turn(monkeypatch, root)
    marked: list[dict] = []
    server = daemon({"notice.pending": lambda request: {"notices": [notice(1, text="20260924-120500-demo: ok"),
                                                                     service_notice(2)]},
                     "notice.mark": lambda request: marked.append(request.args) or {"notices": []}})
    stdout = io.StringIO()
    hooks.session_event("SessionStart", payload("SessionStart", source="startup"), root, stdout=stdout)
    told = context(stdout)
    assert "20260924-120500-demo: ok" in told and told.endswith(hooks.SUGGEST_HINT)
    assert "resume nudge" not in told
    assert marked == [{"session_id": SESSION, "notice_ids": [1], "state": "surfaced",
                       "transport": "hook:SessionStart"}]
    assert server.ops() == ["notice.pending", "notice.mark"]


def test_c25_7_with_the_daemon_down_the_line_is_still_given(root, monkeypatch):
    in_a_turn(monkeypatch, root)
    monkeypatch.setattr(hooks, "_offline_pending", lambda path, session: [])
    stdout = io.StringIO()
    assert hooks.session_event("SessionStart", payload("SessionStart", source="startup"), root, stdout=stdout) == 0
    assert context(stdout) == hooks.SUGGEST_HINT
