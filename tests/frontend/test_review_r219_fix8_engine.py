"""Fix 8: reviewer r4's executed costs and bounded queue-completeness properties."""
import pytest
from hypothesis import given, settings, strategies as st
from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.swift import ROOT, compile_probe

pytestmark = needs_swift


@pytest.fixture(scope="module")
def fix8_engine(tmp_path_factory):
    return compile_probe(tmp_path_factory.mktemp("fix8-engine") / "probe",
                         ROOT / "tests/frontend/ReviewR219Fix8EngineProbe.swift", "SUBFLEET_MODEL_TEST")


@pytest.fixture(scope="module")
def costs(fix8_engine, tmp_path_factory):
    root = tmp_path_factory.mktemp("fix8-costs")
    result = run_probe(fix8_engine, write_json(root / "input.json", {"histories": [500, 2000, 5000], "batch": 5000}), timeout=180)
    write_json(root / "costs.json", result)
    return result


@pytest.mark.parametrize("history", [500, 2000, 5000])
def test_open_and_reopen_stay_under_50ms(costs, history):
    row = next(r for r in costs["cost"] if r["history"] == history)
    assert row["tray"] == 3, row
    assert row["first_apply_ms"] < 50 and row["reapply_ms"] < 50, row


@pytest.mark.parametrize("history", [500, 2000, 5000])
def test_reopen_fetches_no_known_receipt(costs, history):
    row = next(r for r in costs["cost"] if r["history"] == history)
    assert row["reopen_status_ids"] == 0, row


def test_receipt_batch_sorts_once_within_50ms(costs):
    batch = costs["batch"]
    assert batch["order"] == batch["expected"]
    assert batch["apply_ms"] < 50, batch["apply_ms"]


@settings(max_examples=16, deadline=None, derandomize=True)
@given(counts=st.lists(st.integers(min_value=0, max_value=601), min_size=1, max_size=5))
def test_queue_complete_for_bounded_sizes(fix8_engine, tmp_path_factory, counts):
    root = tmp_path_factory.mktemp("fix8-queues")
    result = run_probe(fix8_engine, write_json(root / "input.json", {"queues": counts}), timeout=60)
    for row in result["queues"]:
        expected = [f"m{i:06d}" for i in range(17, 17 + row["count"])]
        assert row["first"] == expected and row["again"] == expected, row
        assert row["reads"] == [], row
