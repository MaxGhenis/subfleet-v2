"""Review r5: exercise the real UIModel reopen and live-row catch-up follow."""
import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.test_review_r219_fix8_view import END, fix8_view_binary

pytestmark = needs_swift


@pytest.fixture(scope="module")
def fix9_view(fix8_view_binary, tmp_path_factory):
    root = tmp_path_factory.mktemp("fix9-view")
    result = run_probe(fix8_view_binary, root, "fix9", timeout=600)
    write_json(root / "result.json", result)
    print("review r5 regressions: " + str(result))
    return result


def test_real_uimodel_reopen_fetches_no_known_receipt(fix9_view):
    run = fix9_view["reopen"]
    rows = [f"queue-tray-withdraw-q{i}" for i in range(3)]
    assert len(run["first_open_status_ids"]) == 14 and run["known"] == 64, run
    assert run["reopen_status_ids"] == [], run
    assert run["tray_first"] == run["tray_again"] == rows, run
    assert fix9_view["visible_windows"] == 0


@pytest.mark.parametrize("name", ["open_live_250", "open_live_100"])
def test_live_row_reveal_during_slow_catchup_keeps_the_end(fix9_view, name):
    run = fix9_view[name]
    assert run["held_before_reveal"], run
    assert run["before_catchup"]["from_end"] <= END, run
    assert run["after_initial_reveal"]["from_end"] <= END, run
    assert run["caught_up"] and run["reveal_consumed"] and run["notices"] == 2, run
    # The slow UIModel opening remains covered. A separate, sequence-controlled
    # page proves catch-up after actual near-end completion releases suspension.
    control = run["settling_control"]
    assert control["reveals"] == 2 and control["consumed"], control
    assert control["before_page"]["from_end"] <= END, control
    assert control["changed_key"] and control["settling"], control
    assert control["confirmations"] >= 1 and not control["suspended"] and control["at_bottom"], control
    assert control["after_page"]["from_end"] <= END, control
    assert run["after_open"]["from_end"] <= END, run
    assert run["after_stream"]["from_end"] <= END, run
