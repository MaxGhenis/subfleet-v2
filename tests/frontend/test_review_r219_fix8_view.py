"""Fix 8: reviewer r4's tray streams, catch-up pages and busy search reveals."""
import pytest
from hypothesis import given, settings, strategies as st
from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.swift import ROOT, compile_probe

pytestmark = needs_swift
END = 30  # 24 pt column padding plus fractional layout tolerance.


@pytest.fixture(scope="module")
def fix8_view_binary(tmp_path_factory):
    return compile_probe(tmp_path_factory.mktemp("fix8-view-bin") / "probe",
                         ROOT / "tests/frontend/ReviewR219Fix8ViewProbe.swift", "SUBFLEET_VIEW_TEST", timeout=900)


@pytest.fixture(scope="module")
def fix8_view(fix8_view_binary, tmp_path_factory):
    root = tmp_path_factory.mktemp("fix8-view")
    result = run_probe(fix8_view_binary, root, timeout=180)
    write_json(root / "result.json", result)
    return result


def test_tray_reveal_keeps_following_two_40_line_streams(fix8_view):
    run = fix8_view["tray_reveal"]
    assert fix8_view["visible_windows"] == 0
    assert run["activation"]["found"] and run["activation"]["consumed"], run
    for key in ("opened", "after_reveal", "after_stream", "after_second_stream"):
        assert run[key]["from_end"] <= END, run


def test_tray_reveal_during_ten_catchup_pages_keeps_open_at_end(fix8_view):
    for key in ("opening_control", "tray_opening"):
        run = fix8_view[key]
        assert run["caught_up"] and run["reveal_consumed"], run
        assert run["after_open"]["from_end"] <= END, run


@pytest.mark.parametrize("name", ["busy_200ms", "busy_400ms"])
def test_continuous_rows_reveal_at_most_twice(fix8_view, name):
    run = fix8_view[name]
    assert run["found"] and 0 <= run["consumed_at"] <= 1.5, run
    assert len(run["reveals_at"]) == 2, run
    assert run["max_from_end_after_person"] <= END and run["final"]["from_end"] <= END, run


@settings(max_examples=12, deadline=None, derandomize=True)
@given(schedule=st.lists(st.integers(min_value=0, max_value=1800), min_size=0, max_size=18).map(lambda xs: sorted(x / 1000 for x in xs)))
def test_one_reveal_pair_for_bounded_row_schedules(fix8_view_binary, tmp_path_factory, schedule):
    root = tmp_path_factory.mktemp("fix8-arrivals")
    result = run_probe(fix8_view_binary, root, write_json(root / "schedule.json", schedule), timeout=15)
    assert len(result["reveals"]) == 2, result
    assert 0 <= result["consumed_at"] <= 1.5, result
