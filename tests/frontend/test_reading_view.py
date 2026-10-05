"""The real views at each text scale (C-29.13) and the ⌘K palette's keys and
layout (C-29.12), hosted without a window on screen."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.frontend.conftest import needs_swift
from tests.frontend.swift import ROOT, compile_probe

pytestmark = needs_swift


@pytest.fixture(scope="session")
def reading_probe(tmp_path_factory):
    return compile_probe(tmp_path_factory.mktemp("subfleet-reading-view") / "probe",
                         ROOT / "tests/frontend/ReadingViewProbe.swift", "SUBFLEET_VIEW_TEST")


def invoke(probe, command: str) -> dict:
    result = subprocess.run([str(probe), command], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(result.stdout)


def test_c29_13_conversation_views_grow_with_the_text_scale(reading_probe):
    out = invoke(reading_probe, "sizes")
    rows = out["rows"]
    assert [row["scale"] for row in rows] == pytest.approx([1.2 ** -1, 1.0, 1.2 ** 2, 1.2 ** 5])
    for view in ("paragraph", "heading", "code", "long_code", "bubble", "chip", "tool", "banner", "result_row"):
        heights = [row[view][1] for row in rows]
        assert heights == sorted(heights) and heights[0] < heights[-1], (view, heights)
    for view in ("paragraph", "heading", "code", "chip"):
        widths = [row[view][0] for row in rows]
        assert all(a < b for a, b in zip(widths, widths[1:])), (view, widths)
    assert [row["composer_font"] for row in rows] == [13.5, 16, 23, 40]
    # At actual size the body is larger than the system's body text.
    actual = rows[1]
    system_width, system_height = out["system_body"]
    assert actual["paragraph"][0] > system_width * 1.1 and actual["paragraph"][1] > system_height
    assert out["visible_windows"] == 0


def test_c29_13_command_equals_is_bigger_too(reading_probe):
    out = invoke(reading_probe, "equals")
    assert out["handled"] is True and out["after_equals"] == pytest.approx(1.2 ** 0.5)
    assert out["after_minus"] == out["after_equals"], "⌘− is the menu's, not this button's"
    assert out["at_largest"] == pytest.approx(1.2 ** 5), "no bigger than the largest"
    assert out["visible_windows"] == 0


def test_c29_12_palette_keys_selection_and_opening(reading_probe):
    out = invoke(reading_probe, "palette")
    assert out["first"] == "cv:a"
    assert out["down"] == "cv:b"
    assert out["up_past_top"] == "cv:a"
    # Past the results is the new-conversation item.
    assert out["down_past_end"] == "action:new-conversation"
    assert out["back_up"] == "cv:c"
    # Return on a message match closes the palette and scrolls to that message.
    assert out["closed"] is True and out["reveal"] == ["c", "person:m1"]
    # With nothing matching, Return starts a new conversation.
    assert out["nothing_matches_selection"] == "action:new-conversation"
    assert out["new_conversation_posted"] == 1 and out["closed_after_new"] is True
    # Tab moves too, so it never takes the keys to what is behind the palette.
    assert out["keys"] == {"down": True, "up": True, "page-down": True, "scroll-page-up": True, "return": True,
                           "escape": True, "tab": True, "left": False}
    assert out["log"] == ["move 1", "move -1", "move 8", "move -8", "submit", "cancel", "move 1"]
    assert out["typed"] == "café"
    assert out["runs"] == [["Résumé", True], [" polish", False]]
    assert out["visible_windows"] == 0


def test_c29_12_palette_layout_fits_its_results(reading_probe):
    out = invoke(reading_probe, "palette")
    few_width, few_height = out["few"]
    many_width, many_height = out["many"]
    assert few_width <= 680 and many_width <= 680
    assert 120 < few_height < many_height, "a short list takes only the room it needs"
    assert many_height < 700, "a long list scrolls inside the palette"
    assert out["many_large"][1] > many_height, "the list's room grows with the text"
    assert 60 < out["empty"][1] < few_height
    # In a window too short for the whole list, the palette stays inside it.
    assert out["many_large_short_window"][1] <= 460
    assert out["few_short_window"][1] == pytest.approx(few_height, abs=1)


def test_c29_12_the_palette_is_modal(reading_probe):
    """Behind the open palette the composer's Send took ⌘↩ and sent the draft
    (review of 158db058). Now nothing behind it takes ⌘↩; its field opens the
    selection."""
    out = invoke(reading_probe, "modal")
    assert out["sent_while_closed"] == 1
    assert out["sent_while_open"] == 1, "nothing behind the open palette takes ⌘↩"
    assert out["field_editing"] is True and out["field_handled"] is True and out["field_submitted"] == 1
    assert out["visible_windows"] == 0
