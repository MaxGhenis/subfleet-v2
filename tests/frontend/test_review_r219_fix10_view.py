"""R5's distant search reveals, with deterministic offscreen presentation."""
import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.test_review_r219_fix8_view import fix8_view_binary

pytestmark = needs_swift
FAR = ["far_quiet", "far_text_50ms", "far_text_120ms", "far_rows_150ms", "far_rows_400ms"]


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
    assert run["quiet"]["from_end"] > 600, run
    assert abs(run["after_more"]["offset"] - run["quiet"]["offset"]) <= 2, run


@pytest.mark.parametrize("delay", ["250", "100"])
def test_distant_search_reveal_during_catchup_keeps_position(distant_reveals, delay):
    run = distant_reveals[f"open_reveal_{delay}"]
    assert run["caught_up"] and run["reveal_consumed"] and run["notices"] == 2, run
    assert run["after_open"]["from_end"] > 600, run
    assert abs(run["after_stream"]["offset"] - run["after_open"]["offset"]) <= 2, run
