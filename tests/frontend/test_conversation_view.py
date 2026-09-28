"""Host the conversation window's real views: the pinned strip carries a Review
button exactly while cards wait, and the sidebar's hand badge is a button that
shows them (design §12; 2026-09-27)."""

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


def test_the_strip_shows_review_only_while_a_card_waits(views):
    strips = {strip["pending"]: strip for strip in views["strips"]}
    assert [strips[n]["label"] for n in (0, 1, 2, 12)] == [None, "Review", "Review (2)", "Review (12)"]
    # No card, no button: the strip is its words and Stop.
    assert strips[0]["width"] < strips[1]["width"] < strips[2]["width"] <= strips[12]["width"]
    # A bordered button is taller than the caption line it joins.
    assert strips[0]["height"] < strips[1]["height"]
    assert strips[1]["height"] == strips[2]["height"] == strips[12]["height"]
    assert views["visible_windows"] == 0


def test_the_strip_keeps_its_stop(views):
    for strip in views["strips"]:
        # Stop is the strip's one AppKit-drawn (link) button; clicking it stops the turn.
        assert strip["appkit_buttons"] == 1 and strip["stop"] == 1
        assert strip["review"] == 0     # and it never opens a request


def test_the_sidebar_hand_badge_is_a_button_that_shows_the_cards(views):
    rows = {row["pending"]: row for row in views["rows"]}
    assert rows[0]["appkit_buttons"] == 0 and rows[0]["badge"] == 0
    assert rows[2]["appkit_buttons"] == 1 and rows[2]["badge"] == 1
    assert rows[2]["width"] > rows[0]["width"]
    assert rows[2]["spoken"] == "2 approvals waiting"
