"""C-9.2: text classification reads only what the provider marked as an error.

A successful Claude turn can talk about usage limits, logins, organisation access
or CLI versions (Subfleet's own work does, constantly). Before this change the
classifier ran its limit and organisation-block patterns over that prose ahead of
the success check, so an answer that mentioned "usage limit" came back `limited`
with a closure, and one that mentioned "does not have access to Claude" came back
`auth-dead`, which disables the lane.
"""

from __future__ import annotations

import json

import pytest

from subfleet.adapters.claude import ClaudeAdapter
from subfleet.contracts import OutcomeClass
from tests.conftest import exit_info, make_launch


def _stage(tmp_path, rows):
    attempt = tmp_path / "a1"
    attempt.mkdir()
    (attempt / "stream.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (attempt / "stderr").write_text("", encoding="utf-8")
    return attempt, make_launch(attempt, session_id="s", model_id="claude-opus-5")


def _init():
    return {"type": "system", "subtype": "init", "session_id": "s", "model": "claude-opus-5"}


def _assistant(text, **extra):
    return {"type": "assistant", "session_id": "s", **extra,
            "message": {"model": "claude-opus-5", "role": "assistant", "type": "message",
                        "content": [{"type": "text", "text": text}]}}


PROSE = [
    "The classifier closes a lane when the result says: You've hit your usage limit.",
    "If the weekly limit is reached, the job waits for the reset.",
    "An organization has disabled Claude subscription access in that case.",
    "The error reads 'does not have access to Claude' when the plan lapses.",
    "Update Claude Code to use this model is the CLI-too-old message.",
    "Out of usage credits means only that model is closed.",
]


@pytest.mark.parametrize("text", PROSE)
def test_successful_turn_prose_never_classifies_as_a_lane_fault(tmp_path, text):
    """C-9.2 a successful turn whose answer quotes limit, org-block or CLI-version
    vocabulary is `ok`, with no closure."""
    attempt, launch = _stage(tmp_path, [
        _init(), _assistant(text),
        {"type": "result", "subtype": "success", "is_error": False, "session_id": "s", "result": text},
    ])
    outcome = ClaudeAdapter().classify(attempt, launch, exit_info(0))
    assert outcome.cls is OutcomeClass.OK
    assert outcome.closure is None


def test_error_result_with_limit_text_still_limits(tmp_path):
    """C-9.2 the provider's own error result keeps classifying: `is_error: true`
    with limit words is `limited` with a closure."""
    attempt, launch = _stage(tmp_path, [
        _init(), {"type": "result", "subtype": "success", "is_error": True, "session_id": "s",
                  "result": "You've hit your usage limit."},
    ])
    outcome = ClaudeAdapter().classify(attempt, launch, exit_info(1))
    assert outcome.cls is OutcomeClass.LIMITED
    assert outcome.closure is not None


def test_marked_assistant_error_still_classifies(tmp_path):
    """C-9.2 an assistant frame the CLI marks as an API error is evidence even when
    no result follows."""
    attempt, launch = _stage(tmp_path, [
        _init(), _assistant("You've reached your session limit.", error="rate_limit",
                            isApiErrorMessage=True),
    ])
    outcome = ClaudeAdapter().classify(attempt, launch, exit_info(1))
    assert outcome.cls is OutcomeClass.LIMITED


def test_unmarked_prose_in_a_failed_turn_is_not_a_limit(tmp_path):
    """C-9.2 a turn that failed for another reason is not `limited` because the
    model had been discussing limits before it failed."""
    attempt, launch = _stage(tmp_path, [
        _init(), _assistant("Next I'll check whether the usage limit applies."),
        {"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": "s",
         "errors": ["API Error: 529 Overloaded"]},
    ])
    outcome = ClaudeAdapter().classify(attempt, launch, exit_info(1))
    assert outcome.cls is OutcomeClass.TRANSIENT
    assert outcome.closure is None
