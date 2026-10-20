"""Review r3's four executed failures, through the real engine and conversation view."""
from pathlib import Path

import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import ServiceHarness
from tests.frontend.swift import ROOT, compile_probe

pytestmark = needs_swift


@pytest.fixture(scope="module")
def fix6_engine(tmp_path_factory):
    return compile_probe(tmp_path_factory.mktemp("fix6-engine") / "probe",
                         ROOT / "tests/frontend/ReviewR219Fix6EngineProbe.swift", "SUBFLEET_MODEL_TEST")


def engine_case(probe, tmp_path, count, *, legacy=False):
    harness = ServiceHarness(tmp_path / "state")
    try:
        cid = harness.create()["conversation_id"]
        harness.store.update_conversation(cid, blocked_by="unfinished-turn")
        mids, previous = [], None
        for index in range(count):
            previous = harness.submit(cid, f"queued {index}", after=previous)["message_id"]
            mids.append(previous)
        opening = harness.call("conversation.open", conversation_id=cid)
        baseline = harness.call("conversation.watch", after=0, wait_s=0)
        if legacy:
            harness.store.set_state(mids[-1], "queued", reason="deferred: review regression")
        watch = harness.call("conversation.watch", after=baseline["next"], wait_s=0)
        caps = harness.call("capabilities")
        if legacy:
            caps["capabilities"].remove("conversation.status.v1")
        all_receipts = []
        for start in range(0, len(mids), 200):
            all_receipts += harness.call("message.status", message_ids=mids[start:start + 200])["messages"]
        data = {"capabilities": caps, "open": opening, "watch": watch,
                "receipts": {r["message_id"]: r for r in all_receipts}}
        if not legacy:
            data.update(baseline=baseline, status=harness.call("conversation.status", conversation_id=cid))
        result = run_probe(probe, write_json(tmp_path / "input.json", data))
        return result, mids, watch
    finally:
        harness.close()


def test_c25_1_legacy_daemon_without_status_delivers_both_watch_pages(fix6_engine, tmp_path):
    result, mids, page = engine_case(fix6_engine, tmp_path, 2, legacy=True)
    assert result["ready"]
    assert result["delivered"] == 2 and result["errors"] == [], result
    assert result["cursor"] == page["next"] > 0
    assert result["tray"] == mids
    assert "conversation.status" not in [c["op"] for c in result["calls"]]


@pytest.mark.parametrize("count", [51, 120, 251])
def test_c29_7_open_hydrates_the_entire_queue_before_publishing(fix6_engine, tmp_path, count):
    result, mids, page = engine_case(fix6_engine, tmp_path, count)
    assert result["opening_tray"] == mids, result
    assert result["tray"] == mids
    assert result["cursor"] == page["next"]
    reads = [c["args"]["message_ids"] for c in result["calls"] if c["op"] == "message.status"]
    assert set().union(*map(set, reads)) == set(mids[:-50])
    assert all(0 < len(ids) <= 200 for ids in reads)
    assert len(reads) == (count - 50 + 199) // 200


@pytest.fixture(scope="module")
def fix6_view(tmp_path_factory):
    root = tmp_path_factory.mktemp("fix6-view")
    binary = compile_probe(root / "probe", ROOT / "tests/frontend/ReviewR219Fix6ViewProbe.swift",
                           "SUBFLEET_VIEW_TEST", timeout=900)
    return run_probe(binary, root, timeout=90)


def test_c27_5_row_reveal_survives_history_while_catching_up(fix6_view):
    reading, caught_up = fix6_view["scrolls"]
    assert not reading["caught_up"] and caught_up["caught_up"]
    for run in (reading, caught_up):
        assert abs(run["before"] - 250) <= 2, run
        assert abs(run["after"] - 250) <= 2, run
    assert fix6_view["visible_windows"] == 0


def test_c29_12_search_expands_and_reveals_the_sixth_held_message(fix6_view):
    search = fix6_view["search"]
    assert search["failure_cause"] == "none", search
    assert search["reveal_set"] and search["tray_saw_reveal"] and search["reveal_consumed"], search
    assert search["laid_out_rows"] == 6 and not search["timed_out"], search
    assert search["target"] == "person:queued5"
    assert search["after"] > search["before"], search
    assert search["before"] == 3, search
    assert search["revealed_id"] == "queued5", search
    assert search["target_visible"], search


def test_c29_12_withdraw_controls_are_found_by_row_identifier(fix6_view):
    search = fix6_view["search"]
    assert search["before_identifiers"] == 3, search
    assert search["after_identifiers"] == 6, search
    assert search["finder"] == "identifier", search
