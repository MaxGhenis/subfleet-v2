"""Distant search reveals with both gated output and overlapping streams."""
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
    print("distant reveal positions: " + str(result))
    assert result["visible_windows"] == 0
    assert result["mounted_windows"] == 7 and result["animation_calls"] > 0
    return result


@pytest.mark.parametrize("name", FAR)
def test_distant_search_reveal_lands_and_keeps_position(distant_reveals, name):
    run = distant_reveals[name]
    assert run["found"] and run["reveal_consumed"] and run["caught_up"], run
    assert len(run["notices_at"]) == 2 and run["reveal_starts"] == 1, run
    assert run.get("output_released"), run
    before = run["before_output"]
    assert before["consumed"] and before["notices"] == 2, run
    assert before["stable_samples"] >= 3 and before["position"]["from_end"] > 600, run
    assert run["folded_seq"] == run["target_seq"], run
    expected = FAR[name]
    assert run["pushed"] == expected and run["arrival_pages"] == int(expected > 0), run
    assert run["quiet_stable_samples"] >= 3 and run["after_more_stable_samples"] >= 3, run
    assert abs(run["quiet"]["offset"] - before["position"]["offset"]) <= 2, run
    assert run["quiet"]["from_end"] > 600, run
    assert abs(run["after_more"]["offset"] - run["quiet"]["offset"]) <= 2, run
    assert run["horizon_at"] >= 6.3, run
    assert run["horizon_at"] - run["last_arrival_at"] >= 2.5, run
    assert run["horizon"]["from_end"] > 600, run
    assert abs(run["horizon"]["offset"] - before["position"]["offset"]) <= 2, run
    assert run["horizon_max_drift"] <= 2, run


@pytest.mark.parametrize("delay", ["250", "100"])
def test_distant_search_reveal_during_catchup_keeps_position(distant_reveals, delay):
    """Settling-follow coverage; the stream begins after reveal consumption."""
    run = distant_reveals[f"open_reveal_{delay}"]
    assert run["caught_up"] and run["reveal_consumed"] and run["notices"] == 2, run
    assert run["after_open"]["from_end"] > 600, run
    assert abs(run["after_stream"]["offset"] - run["after_open"]["offset"]) <= 2, run


def test_focused_50ms_probe_lands_and_keeps_position(fix8_view_binary, tmp_path):
    """Allow fresh-process repetitions without rerunning six unrelated scenes."""
    result = run_probe(fix8_view_binary, tmp_path, "fix10-far-text-50ms", timeout=240)
    write_json(tmp_path / "result.json", result)
    print("focused distant reveal positions: " + str(result))
    assert result["visible_windows"] == 0
    assert result["mounted_windows"] == 1 and result["animation_calls"] > 0
    test_distant_search_reveal_lands_and_keeps_position(result, "far_text_50ms")


@pytest.fixture(scope="module")
def streaming_reveals(fix8_view_binary, tmp_path_factory):
    root = tmp_path_factory.mktemp("fix10-streaming")
    result = run_probe(fix8_view_binary, root, "fix10-streaming", timeout=600)
    write_json(root / "result.json", result)
    print("streaming reveal positions: " + str(result))
    assert result["visible_windows"] == 0
    assert result["mounted_windows"] == 16 and result["animation_calls"] > 0
    return result


def assert_stream_retains_position(run, *, events, pages):
    assert run["found"] and run["caught_up"] and run["reveal_consumed"], run
    assert len(run["notices_at"]) == 2 and run["reveal_starts"] == 1, run
    assert run["pushed"] == events and run["arrival_pages"] == pages, run
    assert len(run["arrivals_at"]) == len(run["folds_at"]) == events, run
    assert run["folded_seq"] == run["target_seq"], run
    assert run["landed_stable_samples"] >= 3, run
    assert run["landed"]["from_end"] > 600, run
    for key in ("quiet", "after_more", "horizon"):
        assert run[key]["from_end"] > 600, run
        assert run[key]["offset"] == run["landed"]["offset"], run
    assert run["horizon_at"] >= 6.3 and run["horizon_max_drift"] == 0, run


@pytest.mark.parametrize("kind", ["text", "tool"])
@pytest.mark.parametrize("delay_ms", [0, 16, 50, 120])
def test_stream_arrives_before_first_reveal_notice(streaming_reveals, kind, delay_ms):
    run = streaming_reveals[f"early_{kind}_{delay_ms}ms"]
    assert_stream_retains_position(run, events=1, pages=1)
    assert run["arrivals_at"][0] >= delay_ms / 1000, run
    assert run["arrivals_at"][0] <= run["folds_at"][0] < run["notices_at"][0], run


@pytest.mark.parametrize("kind,events", [("text", 50), ("tool", 17)])
def test_background_stream_crosses_both_notices_and_consumption(streaming_reveals, kind, events):
    run = streaming_reveals[f"background_{kind}"]
    assert_stream_retains_position(run, events=events, pages=events)
    first, second = run["notices_at"]
    assert run["arrivals_at"][0] < first < second <= run["consumed_at"] < run["arrivals_at"][-1], run
    assert any(first < at < second for at in run["arrivals_at"]), run
    assert run["folds_at"][-1] > run["consumed_at"], run


@pytest.mark.parametrize("kind", ["text", "tool"])
@pytest.mark.parametrize("phase", ["first_notice", "second_notice", "consumption"])
def test_stream_burst_at_reveal_boundary_keeps_position(streaming_reveals, kind, phase):
    run = streaming_reveals[f"burst_{kind}_{phase}"]
    assert_stream_retains_position(run, events=10, pages=1)
    boundary = run["consumed_at"] if phase == "consumption" else run["notices_at"][int(phase == "second_notice")]
    assert run["boundary_arrival_at"] == boundary, run
