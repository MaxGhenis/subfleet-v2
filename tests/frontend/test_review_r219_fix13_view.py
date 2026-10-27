"""Review r7: keep an own send at the end through lazy document growth."""
import json
import os

import pytest

from tests.frontend.conftest import needs_swift
from tests.frontend.test_review_r219_fix8_view import fix8_view_binary
from tests.frontend.test_review_r219_fix12_view import (
    GAP, assert_own_send_order, fix12_run,
)

pytestmark = needs_swift


def landing_run(binary, root, mode):
    load = os.getloadavg()[0]
    result, steps, diagnostic = fix12_run(binary, root, mode)
    print(f"landing {mode}: load={load:.2f}, gap={steps['reply']['position']['from_end']}")
    (root / "load.json").write_text(json.dumps({"load": load, "mode": mode}))
    assert_own_send_order(result, steps, catchup=False)
    return result, steps, diagnostic


def assert_landing_follows(result, steps):
    diagnostic = json.dumps(result, sort_keys=True)
    assert steps["reply"]["position"]["from_end"] <= GAP, diagnostic


@pytest.mark.parametrize("mode", ["short", "regrow"])
def test_tall_own_send_keeps_the_end_through_measurement(fix8_view_binary, tmp_path, mode):
    result, steps, diagnostic = landing_run(fix8_view_binary, tmp_path, mode)
    if mode == "regrow":
        assert "growing" in steps, diagnostic
    assert_landing_follows(result, steps)


def test_person_scroll_ends_the_own_send_latch(fix8_view_binary, tmp_path):
    result, steps, diagnostic = landing_run(fix8_view_binary, tmp_path, "person")
    after = steps["person_scrolled"]
    moves = [e for e in result["trace"] if e["event"] == "move" and e["at"] >= after["at"]]
    assert moves and all(e["move"] == "stay" and not e["landing"] for e in moves), diagnostic
    assert steps["reply"]["position"]["offset"] == after["position"]["offset"], diagnostic
