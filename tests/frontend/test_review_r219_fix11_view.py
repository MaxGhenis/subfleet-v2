"""Suspended opening reveals survive settling with a stale end marker."""
import json

import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.test_review_r219_fix8_view import END, fix8_view_binary

pytestmark = needs_swift


def settling_run(binary, root, mode):
    run = run_probe(binary, root, f"fix11-{mode}", timeout=240)
    write_json(root / "result.json", run)
    print("settling reveal: " + json.dumps(run, sort_keys=True))
    diagnostic = json.dumps(run, sort_keys=True)
    assert run["visible_windows"] == 0, diagnostic
    assert run["held_before_reveal"] and run["before_catchup"]["from_end"] <= 30, diagnostic
    assert run["caught_up"] and run["reveal_consumed"], diagnostic
    return run


def assert_distant_settling_holds(run):
    diagnostic = json.dumps(run, sort_keys=True)
    assert run["after_initial_reveal"]["from_end"] > 600, diagnostic
    for key in ("after_open", "after_stream", "horizon"):
        assert run[key]["from_end"] > 600, diagnostic
    # Catch-up measures previously unloaded rows above the reveal. Its absolute
    # offset can change even when every follow decision stays. Once caught up,
    # subsequent output and the quiet horizon must retain that settled offset.
    for key in ("after_stream", "horizon"):
        assert abs(run[key]["offset"] - run["after_open"]["offset"]) <= 2, diagnostic
    assert run["max_drift"] <= 2, diagnostic


@pytest.mark.parametrize("mode", ["stale", "control"])
def test_settling_cannot_undo_distant_opening_reveal(fix8_view_binary, tmp_path, mode):
    run = settling_run(fix8_view_binary, tmp_path, mode)
    diagnostic = json.dumps(run, sort_keys=True)
    if mode == "stale":
        # Prove that this executes the bypass, rather than an already-correct
        # non-settling or marker-false path. Keep position assertions primary.
        assert any(e["event"] == "move" and e["settling"] and e["suspended"]
                   and e["at_bottom"] for e in run["timeline"]), diagnostic
    assert_distant_settling_holds(run)


@pytest.mark.parametrize("mode", ["plain", "near"])
def test_confirmed_visible_end_follows_catchup(fix8_view_binary, tmp_path, mode):
    run = settling_run(fix8_view_binary, tmp_path, mode)
    diagnostic = json.dumps(run, sort_keys=True)
    if mode == "near":
        assert any(e["event"] == "reveal_end_visible" for e in run["timeline"]), diagnostic
    assert any(e["event"] == "move" and e["settling"] and not e["suspended"]
               and e["move"] == "jump" for e in run["timeline"]), diagnostic
    for key in ("after_initial_reveal", "after_open", "after_stream", "horizon"):
        assert run[key]["from_end"] <= 30, diagnostic


def test_completion_before_scroll_still_confirms_a_near_end_reveal(fix8_view_binary, tmp_path):
    """Round 6's control: with each completion delivered before its scroll is presented, a
    reveal that leaves the end visible is still confirmed, and catch-up follows the end."""
    run = settling_run(fix8_view_binary, tmp_path, "near-completion-first")
    diagnostic = json.dumps(run, sort_keys=True)
    early = run["early_completions"]
    assert len(early) == 2 and early[0]["position"]["from_end"] <= END, diagnostic
    assert any(e["event"] == "reveal_end_visible" for e in run["timeline"]), diagnostic
    assert any(e["event"] == "move" and e["settling"] and not e["suspended"]
               and e["move"] == "jump" for e in run["timeline"]), diagnostic
    for key in ("after_initial_reveal", "after_open", "after_stream", "horizon"):
        assert run[key]["from_end"] <= END, diagnostic


def test_new_live_turn_while_reveal_is_suspended_holds_position(fix8_view_binary, tmp_path):
    run = run_probe(fix8_view_binary, tmp_path, "fix11-live-turn", timeout=240)
    write_json(tmp_path / "result.json", run)
    print("suspended live turn: " + json.dumps(run, sort_keys=True))
    diagnostic = json.dumps(run, sort_keys=True)
    assert run["visible_windows"] == 0, diagnostic
    assert run["injected"] and run["injection_suspended"], diagnostic
    assert run["reveal_consumed"] and run["caught_up"] and run["reveals"] == 2, diagnostic
    assert run["live_message"] == "next-live", diagnostic
    assert any(e["event"] == "move" and e["live_message"] == "next-live"
               and e["suspended"] and e["move"] == "stay" for e in run["timeline"]), diagnostic
    assert run["landed_stable_samples"] >= 3 and run["landed"]["from_end"] > 600, diagnostic
    assert run["after_stream"]["from_end"] > 600, diagnostic
    assert run["after_stream"]["offset"] == run["landed"]["offset"], diagnostic
    assert run["max_drift"] == 0, diagnostic
