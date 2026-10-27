"""Review r7: keep an own send at the end through lazy document growth.

Review r8: a quiet landing ends only the landing check. Growth after it is still followed,
until the person scrolls or a reveal starts.
"""
import json
import os

import pytest

from tests.frontend.conftest import needs_swift
from tests.frontend.test_review_r219_fix8_view import fix8_view_binary
from tests.frontend.test_review_r219_fix12_view import (
    GAP, assert_own_send_order, fix12_run,
)

pytestmark = needs_swift
# Review r8's input: the regrow scene lands, then the rows above the clip grow 600 pt again.
QUIET = ["quiet-regrow", "quiet-regrow-natural", "quiet-regrow-late"]


def landing_run(binary, root, mode):
    load = os.getloadavg()[0]
    result, steps, diagnostic = fix12_run(binary, root, mode)
    print(f"landing {mode}: load={load:.2f}, gap={steps['reply']['position']['from_end']}")
    (root / "load.json").write_text(json.dumps({"load": load, "mode": mode}))
    assert_own_send_order(result, steps, catchup=False)
    if mode.endswith("-natural"):
        assert_natural_presentation(result, steps)
    return result, steps, diagnostic


def assert_natural_presentation(result, steps):
    """Both animation controls were off: the send's glide reached SwiftUI with its animation, and
    the root transaction modifier let every transaction through unchanged."""
    diagnostic = json.dumps(result, sort_keys=True)
    presentation = result["presentation"]
    assert presentation["natural"] and presentation["root_transactions"] > 0, diagnostic
    assert any(e["event"] == "move" and e["move"] == "glide" for e in result["trace"]
               if e["at"] >= steps["pending"]["at"]), diagnostic
    assert any(animation != "nil" for animation in presentation["forwarded_animations"]), diagnostic


def assert_landing_follows(result, steps):
    diagnostic = json.dumps(result, sort_keys=True)
    assert steps["reply"]["position"]["from_end"] <= GAP, diagnostic


def own_send_landings(result, steps):
    return [e for e in result["trace"] if e["event"] == "own_send_landed" and e["at"] >= steps["pending"]["at"]]


def assert_landed_once(result, steps):
    """The send's landing check measured the end once: `onOwnSendLanded` fires once per send."""
    diagnostic = json.dumps(result, sort_keys=True)
    landings = own_send_landings(result, steps)
    assert len(landings) == 1 and landings[0]["position"]["from_end"] <= GAP, diagnostic


def assert_quiet_landing_order(result, steps, mode):
    """The regrow scene's growth, then `own_send_landed` measuring a 0 pt gap, then 600 pt more
    growth above the clip: 50 ms after the landing, or for `-late` at least 350 ms after it, on a
    main-queue timer past the landing check's quiet window."""
    diagnostic = json.dumps(result, sort_keys=True)
    assert {"growing", "landed", "regrowing"} <= steps.keys(), diagnostic
    landings = own_send_landings(result, steps)
    assert len(landings) == 1, diagnostic
    landed = steps["landed"]["landed"]
    assert landed == landings[0] and landed["position"]["from_end"] <= 1, diagnostic
    assert steps["growing"]["at"] < landed["at"] < steps["regrowing"]["at"] < steps["sent"]["at"], diagnostic
    assert steps["regrowing"]["after_landing"] >= (0.35 if mode == "quiet-regrow-late" else 0.05), diagnostic


@pytest.mark.parametrize("mode", ["short", "regrow", "regrow-natural"])
def test_tall_own_send_keeps_the_end_through_measurement(fix8_view_binary, tmp_path, mode):
    result, steps, diagnostic = landing_run(fix8_view_binary, tmp_path, mode)
    if "regrow" in mode:
        assert "growing" in steps, diagnostic
        assert_landed_once(result, steps)
    assert_landing_follows(result, steps)


@pytest.mark.parametrize("mode", QUIET)
def test_growth_after_a_quiet_landing_is_followed(fix8_view_binary, tmp_path, mode):
    """Review r8 P2-1: once the landing check had measured the end (0 pt) and ended the latch,
    600 pt of growth above the clip left the reply 779–814 pt behind (`stay`, `landing: false`)."""
    result, steps, _ = landing_run(fix8_view_binary, tmp_path, mode)
    assert_quiet_landing_order(result, steps, mode)
    assert_landing_follows(result, steps)


def test_person_scroll_ends_the_own_send_latch(fix8_view_binary, tmp_path):
    result, steps, diagnostic = landing_run(fix8_view_binary, tmp_path, "person")
    after = steps["person_scrolled"]
    moves = [e for e in result["trace"] if e["event"] == "move" and e["at"] >= after["at"]]
    assert moves and all(e["move"] == "stay" and not e["landing"] for e in moves), diagnostic
    assert steps["reply"]["position"]["offset"] == after["position"]["offset"], diagnostic
