"""Host the conversation window's real views (C-27.5; design §12): the pinned
strip carries a Review button exactly while cards wait, and the sidebar's hand
badge is a button that shows them (2026-09-27)."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.frontend.conftest import needs_swift
from tests.frontend.swift import ROOT, compile_probe

pytestmark = needs_swift


@pytest.fixture(scope="module")
def views(tmp_path_factory) -> dict:
    probe = compile_probe(tmp_path_factory.mktemp("subfleet-conversation-view") / "probe",
                          ROOT / "tests/frontend/ConversationViewProbe.swift", "SUBFLEET_VIEW_TEST")
    result = subprocess.run([str(probe)], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(result.stdout)


def test_c27_5_the_strip_shows_review_only_while_a_card_waits(views):
    """C-27.5: no card, no Review; the label counts the cards."""
    strips = {strip["pending"]: strip for strip in views["strips"] if strip["stops"]}
    assert [strips[n]["label"] for n in (0, 1, 2, 12)] == [None, "Review", "Review (2)", "Review (12)"]
    # The same turn in every strip: only the Review button changes the room it takes.
    assert strips[0]["width"] < strips[1]["width"] < strips[2]["width"] <= strips[12]["width"]
    # A bordered button is taller than the caption line it joins.
    assert strips[0]["height"] < strips[1]["height"]
    assert strips[1]["height"] == strips[2]["height"] == strips[12]["height"]
    assert views["visible_windows"] == 0


def test_c27_5_the_strip_buttons_do_what_they_say(views):
    """C-27.5: Stop stops and never opens a request; where AppKit draws Review
    (before macOS 26) clicking it opens one, and only while a card waits."""
    for strip in views["strips"]:
        clicks = strip["clicks"]
        # No Stop where it would act on nothing (the turn of a settled message's card).
        assert clicks.count("stop") == (1 if strip["stops"] else 0) and "none" not in clicks
        assert clicks.count("review") == (0 if strip["pending"] == 0 else len(clicks) - clicks.count("stop"))
        assert clicks.count("review") <= 1


def test_c27_5_the_sidebar_hand_badge_is_a_button_that_shows_the_cards(views):
    """C-27.5: the badge opens its conversation at the oldest card."""
    rows = {row["pending"]: row for row in views["rows"]}
    assert rows[0]["clicks"] == []
    assert rows[2]["clicks"] == ["badge"]
    assert rows[2]["width"] > rows[0]["width"]
    assert rows[2]["spoken"] == "2 approvals waiting"


@pytest.mark.parametrize("reason,actions", [
    ("unfinished-turn", ["continue", "leave"]),
    ("delivery-unknown", ["delivered", "not-delivered"]),
])
def test_cutover_the_real_blocker_banner_buttons_and_sidebar_mark(views, reason, actions):
    """The opened banner's real buttons dispatch both choices; each row is marked."""
    blocker = next(row for row in views["blockers"] if row["reason"] == reason)
    assert blocker["clicks"] == actions
    assert blocker["needs_you"] == "Needs you" and blocker["marked_width"] > blocker["plain_width"]
    assert views["visible_windows"] == 0


def test_cutover_the_background_task_view_is_a_compact_system_notice(views):
    assert views["task_notice"]["detail"] == "Completed · Exit code 0"
    assert views["task_notice"]["buttons"] == 0
    assert 0 < views["task_notice"]["height"] < 80
