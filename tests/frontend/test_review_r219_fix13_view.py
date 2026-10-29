"""Review r7: keep an own send at the end through lazy document growth.

Review r8: a quiet landing ends only the landing check. Growth after it is still followed,
until the person scrolls or a reveal starts. The check's 300 ms quiet window still decides
when the send has settled: the glide's last frame can land inside it.
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
    """The regrow scene, then `own_send_landed` and a quiet frame at 0 pt, then 600 pt more growth above
    the clip: 50 ms after that frame, or for `-late` at least 350 ms after the callback, on a main-queue
    timer past the landing check's quiet window. (A probe slower than the window to react to the
    marker grows the rows only after the landing: then both growths follow it.)"""
    diagnostic = json.dumps(result, sort_keys=True)
    assert {"growing", "landed", "regrowing"} <= steps.keys(), diagnostic
    landings = own_send_landings(result, steps)
    assert len(landings) == 1, diagnostic
    landed = steps["landed"]["landed"]
    assert landed == landings[0] and landed["position"]["from_end"] <= GAP, diagnostic
    assert abs(steps["landed"]["position"]["from_end"]) <= 1, diagnostic
    assert landed["at"] <= steps["landed"]["at"] < steps["regrowing"]["at"] < steps["sent"]["at"], diagnostic
    assert steps["regrowing"]["after_landing"] >= (0.35 if mode == "quiet-regrow-late" else 0.05), diagnostic


def assert_stale_glide_order(result, steps):
    """The send glided to the end and the clip rested there; 150 ms later the glide's last frame
    took it back to the old end, far from the new one."""
    diagnostic = json.dumps(result, sort_keys=True)
    assert {"stale_glide", "rested"} <= steps.keys(), diagnostic
    stale = steps["stale_glide"]
    assert stale["position"]["from_end"] <= GAP, diagnostic
    assert any(e["event"] == "move" and e["move"] == "glide" for e in result["trace"]
               if steps["pending"]["at"] <= e["at"] <= stale["at"]), diagnostic
    moved = [g for g in result["geometry"] if g["at"] >= stale["at"]]
    assert moved and moved[0]["position"]["from_end"] > 1000, diagnostic
    assert own_send_landings(result, steps), diagnostic


def assert_rests_at_the_end(result, steps):
    """The landing is measured once the glide's last frame has landed, and from then on the clip
    stays at the end: no move takes it away before the send has rested."""
    diagnostic = json.dumps(result, sort_keys=True)
    assert_landed_once(result, steps)
    [landed] = own_send_landings(result, steps)
    assert landed["at"] > steps["stale_glide"]["at"], diagnostic
    assert all(g["position"]["from_end"] <= GAP for g in result["geometry"]
               if landed["at"] <= g["at"] <= steps["rested"]["at"]), diagnostic
    assert steps["rested"]["position"]["from_end"] <= GAP, diagnostic


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


def test_send_rests_at_the_end_after_its_glides_last_frame(fix8_view_binary, tmp_path):
    """Round 10: the landing check measures the end only after 300 ms without a document or clip
    change. Measured on the next turn instead, the landing came before the glide's last frame, which
    then took the clip 1,662–1,676 pt from the end after the check had ended."""
    result, steps, _ = landing_run(fix8_view_binary, tmp_path, "stale-glide")
    assert_stale_glide_order(result, steps)
    assert_rests_at_the_end(result, steps)
    assert_landing_follows(result, steps)


def test_person_scroll_ends_the_own_send_latch(fix8_view_binary, tmp_path):
    result, steps, diagnostic = landing_run(fix8_view_binary, tmp_path, "person")
    after = steps["person_scrolled"]
    moves = [e for e in result["trace"] if e["event"] == "move" and e["at"] >= after["at"]]
    assert moves and all(e["move"] == "stay" and not e["landing"] for e in moves), diagnostic
    assert steps["reply"]["position"]["offset"] == after["position"]["offset"], diagnostic
