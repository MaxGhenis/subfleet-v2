"""Distant search reveals with both gated output and overlapping streams."""
import json

import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.test_review_r219_fix8_view import fix8_view_binary

pytestmark = needs_swift
FAR = {"far_quiet": 0, "far_text_50ms": 50, "far_text_120ms": 21,
       "far_rows_150ms": 17, "far_rows_400ms": 7}


@pytest.fixture(scope="module")
def distant_reveals(fix8_view_binary, tmp_path_factory):
    root = tmp_path_factory.mktemp("fix10-distant")
    result = run_probe(fix8_view_binary, root, "fix10", timeout=600)
    write_json(root / "result.json", result)
    print("distant reveal positions: " + json.dumps(result, sort_keys=True))
    assert result["visible_windows"] == 0
    assert result["mounted_windows"] == 7 and result["animation_calls"] > 0
    return result


@pytest.mark.parametrize("name", FAR)
def test_distant_search_reveal_lands_and_keeps_position(distant_reveals, name):
    run = distant_reveals[name]
    diagnostic = json.dumps(run, sort_keys=True)
    assert run["found"] and run["reveal_consumed"] and run["caught_up"], diagnostic
    assert len(run["notices_at"]) == 2 and run["reveal_starts"] == 1, diagnostic
    assert run.get("output_released"), diagnostic
    before = run["before_output"]
    assert before["consumed"] and before["notices"] == 2, diagnostic
    assert before["stable_samples"] >= 3 and before["position"]["from_end"] > 600, diagnostic
    assert run["folded_seq"] == run["target_seq"], diagnostic
    expected = FAR[name]
    assert run["pushed"] == expected and run["arrival_pages"] == int(expected > 0), diagnostic
    assert run["quiet_stable_samples"] >= 3 and run["after_more_stable_samples"] >= 3, diagnostic
    assert abs(run["quiet"]["offset"] - before["position"]["offset"]) <= 2, diagnostic
    assert run["quiet"]["from_end"] > 600, diagnostic
    assert abs(run["after_more"]["offset"] - run["quiet"]["offset"]) <= 2, diagnostic
    assert run["horizon_at"] >= 6.3, diagnostic
    assert run["horizon_at"] - run["last_arrival_at"] >= 2.5, diagnostic
    assert run["horizon"]["from_end"] > 600, diagnostic
    assert abs(run["horizon"]["offset"] - before["position"]["offset"]) <= 2, diagnostic
    assert run["horizon_max_drift"] <= 2, diagnostic


@pytest.mark.parametrize("delay", ["250", "100"])
def test_distant_search_reveal_during_catchup_keeps_position(distant_reveals, delay):
    """Settling-follow coverage; the stream begins after reveal consumption."""
    run = distant_reveals[f"open_reveal_{delay}"]
    diagnostic = json.dumps(run, sort_keys=True)
    assert run["caught_up"] and run["reveal_consumed"] and run["notices"] == 2, diagnostic
    assert run["after_open"]["from_end"] > 600, diagnostic
    assert abs(run["after_stream"]["offset"] - run["after_open"]["offset"]) <= 2, diagnostic


def test_focused_50ms_probe_lands_and_keeps_position(fix8_view_binary, tmp_path):
    """Allow fresh-process repetitions without rerunning six unrelated scenes."""
    result = run_probe(fix8_view_binary, tmp_path, "fix10-far-text-50ms", timeout=240)
    write_json(tmp_path / "result.json", result)
    print("focused distant reveal positions: " + json.dumps(result, sort_keys=True))
    assert result["visible_windows"] == 0
    assert result["mounted_windows"] == 1 and result["animation_calls"] > 0
    test_distant_search_reveal_lands_and_keeps_position(result, "far_text_50ms")


@pytest.fixture(scope="module")
def streaming_reveals(fix8_view_binary, tmp_path_factory):
    root = tmp_path_factory.mktemp("fix10-streaming")
    result = run_probe(fix8_view_binary, root, "fix10-streaming", timeout=600)
    write_json(root / "result.json", result)
    print("streaming reveal positions: " + json.dumps(result, sort_keys=True))
    assert result["visible_windows"] == 0
    assert result["mounted_windows"] == 17 and result["animation_calls"] > 0
    return result


def assert_stream_retains_position(run, *, events, pages, at_least=False):
    diagnostic = json.dumps(run, sort_keys=True)
    assert run["found"] and run["caught_up"] and run["reveal_consumed"], diagnostic
    assert len(run["notices_at"]) == 2 and run["reveal_starts"] == 1, diagnostic
    assert not run["producer_deadline_exceeded"], diagnostic
    if at_least:
        assert run["pushed"] >= events and run["arrival_pages"] == run["pushed"], diagnostic
    else:
        assert run["pushed"] == events and run["arrival_pages"] == pages, diagnostic
    assert len(run["arrivals_at"]) == len(run["folds_at"]) == run["pushed"], diagnostic
    assert run["folded_seq"] == run["target_seq"], diagnostic
    assert run["landed_stable_samples"] >= 3, diagnostic
    assert run["landed"]["from_end"] > 600, diagnostic
    for key in ("quiet", "after_more", "horizon"):
        assert run[key]["from_end"] > 600, diagnostic
        assert run[key]["offset"] == run["landed"]["offset"], diagnostic
    assert run["horizon_at"] >= 6.3 and run["horizon_max_drift"] == 0, diagnostic


@pytest.mark.parametrize("kind", ["text", "tool"])
@pytest.mark.parametrize("delay_ms", [0, 16, 50, 120])
def test_stream_arrives_before_first_reveal_notice(streaming_reveals, kind, delay_ms):
    run = streaming_reveals[f"early_{kind}_{delay_ms}ms"]
    diagnostic = json.dumps(run, sort_keys=True)
    assert_stream_retains_position(run, events=1, pages=1)
    assert run["arrivals_at"][0] >= delay_ms / 1000, diagnostic
    assert run["arrivals_at"][0] <= run["folds_at"][0] < run["notices_at"][0], diagnostic


def check_background_stream(run, *, events):
    diagnostic = json.dumps(run, sort_keys=True)
    assert_stream_retains_position(run, events=events, pages=events, at_least=True)
    first, second = run["notices_at"]
    assert run["arrivals_at"][0] < first < second <= run["consumed_at"] < run["arrivals_at"][-1], diagnostic
    assert any(first < at < second for at in run["arrivals_at"]), diagnostic
    assert run["folds_at"][-1] > run["consumed_at"], diagnostic


@pytest.mark.parametrize("kind,events", [("text", 50), ("tool", 17)])
def test_background_stream_crosses_both_notices_and_consumption(streaming_reveals, kind, events):
    check_background_stream(streaming_reveals[f"background_{kind}"], events=events)


# The checks that must reject a reveal which jumps away from its destination
# before landing is captured (review r5's surviving overlap-loss replay).
REVEAL_CHECKS = {"assert_reveal_reaches_destination", "assert_reveal_interval_holds"}


def test_background_checks_reject_replayed_overlap_jump(fix8_view_binary, tmp_path):
    """Review r5's survivor as a fixed schedule: 50 events, 15 between the notices
    and four after consumption, with its 177-pt-short jump injected after consumption."""
    result = run_probe(fix8_view_binary, tmp_path, "fix10-streaming-replayed_jump", timeout=240)
    write_json(tmp_path / "result.json", result)
    run = result["replayed_jump"]
    diagnostic = json.dumps(run, sort_keys=True)
    print("replayed overlap jump: " + diagnostic)
    assert result["visible_windows"] == 0, diagnostic
    first, second = run["notices_at"]
    consumed = run["consumed_at"]
    assert run["replay"] and run["pushed"] == 50 and run["arrival_pages"] == 50, diagnostic
    assert sum(first < at < second for at in run["arrivals_at"]) == 15, diagnostic
    assert sum(at > consumed for at in run["arrivals_at"]) == 4, diagnostic
    jump = run["injected_jump"]
    assert jump and jump["to"]["offset"] - jump["from"]["offset"] > 3000, diagnostic
    assert jump["to"]["from_end"] <= 200 and jump["at"] > consumed, diagnostic
    # The survivor's signature: it lands more than 600 pt from the end and then
    # holds with zero drift, so every pre-round-6 position check accepts it.
    assert run["landed"]["from_end"] > 600 and run["horizon_max_drift"] == 0, diagnostic
    with pytest.raises(AssertionError) as rejected:
        check_background_stream(run, events=50)
    assert rejected.traceback[-1].name in REVEAL_CHECKS, rejected.getrepr(style="short")


def test_trailing_stream_waits_for_late_reveal_consumption(streaming_reveals):
    """The independent cadence continues through a slow consumption turn."""
    run = streaming_reveals["late_consumption_tool"]
    diagnostic = json.dumps(run, sort_keys=True)
    assert_stream_retains_position(run, events=17, pages=17, at_least=True)
    assert run["consumption_pause"] == 3 and run["consumed_at"] > 2.5, diagnostic
    # Preserve the original 16-event independent prefix before consumption;
    # the ongoing stream additionally supplies at least three later ticks.
    assert run["arrivals_at"][15] < run["consumed_at"], diagnostic
    assert sum(at > run["consumed_at"] for at in run["arrivals_at"]) >= 3, diagnostic
    first, second = run["notices_at"]
    assert run["arrivals_at"][0] < first < second <= run["consumed_at"] < run["arrivals_at"][-1], diagnostic
    assert any(first < at < second for at in run["arrivals_at"]), diagnostic
    assert run["folds_at"][-1] > run["consumed_at"], diagnostic


@pytest.mark.parametrize("kind", ["text", "tool"])
@pytest.mark.parametrize("phase", ["first_notice", "second_notice", "consumption"])
def test_stream_burst_at_reveal_boundary_keeps_position(streaming_reveals, kind, phase):
    run = streaming_reveals[f"burst_{kind}_{phase}"]
    diagnostic = json.dumps(run, sort_keys=True)
    assert_stream_retains_position(run, events=10, pages=1)
    boundary = run["consumed_at"] if phase == "consumption" else run["notices_at"][int(phase == "second_notice")]
    assert run["boundary_arrival_at"] == boundary, diagnostic


@pytest.mark.parametrize("kind,events", [("text", 50), ("tool", 17)])
def test_stale_end_marker_cannot_undo_search_reveal(fix8_view_binary, tmp_path, kind, events):
    result = run_probe(fix8_view_binary, tmp_path, f"fix10-streaming-stale_{kind}", timeout=240)
    write_json(tmp_path / "result.json", result)
    run = result[f"stale_{kind}"]
    diagnostic = json.dumps(run, sort_keys=True)
    assert result["visible_windows"] == 0, diagnostic
    assert run["stale_marker"], diagnostic
    assert any(entry["event"] == "move" and entry["at_bottom"] and entry["suspended"]
               and not entry["settling"] for entry in run["timeline"]), diagnostic
    assert_stream_retains_position(run, events=events, pages=events, at_least=True)


@pytest.mark.parametrize("kind,events", [("text", 50), ("tool", 17)])
def test_background_stream_survives_delayed_first_notice(fix8_view_binary, tmp_path, kind, events):
    """A paused presentation must not exhaust the independent background input."""
    name = f"delayed_background_{kind}"
    result = run_probe(fix8_view_binary, tmp_path, f"fix10-streaming-{name}", timeout=240)
    write_json(tmp_path / "result.json", result)
    run = result[name]
    print("delayed background: " + json.dumps(run, sort_keys=True))
    diagnostic = json.dumps(run, sort_keys=True)
    assert result["visible_windows"] == 0, diagnostic
    assert run["first_notice_pause"] == 3, diagnostic
    assert_stream_retains_position(run, events=events, pages=events, at_least=True)
    first, second = run["notices_at"]
    assert run["arrivals_at"][0] < first < second <= run["consumed_at"] < run["arrivals_at"][-1], diagnostic
    assert any(first < at < second for at in run["arrivals_at"]), diagnostic
    assert run["folds_at"][-1] > run["consumed_at"], diagnostic
