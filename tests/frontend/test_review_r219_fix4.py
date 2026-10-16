"""Fix-4 regressions for D1–D3, using the real conversation store and service."""
from pathlib import Path
import tempfile
import uuid

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from tests.frontend.conftest import needs_swift, run_probe
from tests.frontend.daemon_harness import ServiceHarness, ServiceServer
from tests.frontend.test_review_r219_probes import store_probe, placement, positions, watch_page
from tests.frontend.test_core_queue_tray import run_queue, receipt, local, tray

pytestmark = needs_swift
PROPERTY = settings(max_examples=40, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


@pytest.mark.parametrize("retry", range(15))
def test_n1_n2_claim_requeue_defer_never_releases_a_tray_row(core_probe, retry):
    # A queued row must survive both an ended host and the claim's unbound waiting state.
    steps = [{"receipts": [receipt("host", 1, "running"), receipt("mine", 2, "queued")]},
             {"receipts": [receipt("host", 1, "complete")]}]
    for _ in range(1 + retry):
        steps += [{"receipts": [receipt("mine", 2, "waiting", reason="dispatching")]},
                  {"receipts": [receipt("mine", 2, "queued")]},
                  {"receipts": [receipt("mine", 2, "queued", reason="deferred: disk busy")]}]
    result = run_queue(core_probe, steps)
    assert all("mine" in tray(s) for s in result["snapshots"])


def test_d1_a_deferred_timeline_send_never_goes_back(core_probe):
    result = run_queue(core_probe, [local("mine", "idle send"),
        {"receipts": [receipt("mine", 1, "queued")]},
        {"receipts": [receipt("mine", 1, "waiting", reason="dispatching")]},
        {"receipts": [receipt("mine", 1, "queued", reason="deferred: disk busy")]},
        {"held": True}])
    assert all("mine" not in tray(s) for s in result["snapshots"])
    assert "Deferred: disk busy" in result["snapshots"][-1]["timeline"]["turns"]["mine"]["status_text"]


@PROPERTY
@given(initial_tray=st.booleans(), retries=st.integers(1, 4), extra=st.integers(0, 3))
def test_d1_no_going_back_over_real_dispatch_retries(core_probe, initial_tray, retries, extra):
    """The generator always reads a watch page *inside* submit, before its refusal.

    _dispatch_one performs the real claim, requeue and _defer transactions. The
    submit seam only supplies a temporary operational refusal; it records the
    page while the claim is live, a transition the previous generator omitted.
    """
    with tempfile.TemporaryDirectory(prefix="sf-f4-history-") as scratch:
        h = ServiceHarness(Path(scratch))
        try:
            cid = h.create()["conversation_id"]
            if initial_tray:
                host = h.submit(cid, "host")["message_id"]
                h.store.set_state(host, "running")
            else:
                host = None
            mid = str(uuid.uuid4())
            groups = [[{"open": h.call("conversation.open", conversation_id=cid)}, {"focus": cid}],
                      [{"local": {"conversation_id": cid, "message_id": mid, "text": "mine"}}]]
            answer = h.submit(cid, "mine", after=host, message_id=mid)
            groups.append([{"receipts": [answer]}])
            previous = mid
            for _ in range(extra):
                previous = h.submit(cid, "later", after=previous)["message_id"]
            cursor = h.call("conversation.watch", after=0, wait_s=0)["next"]
            if host:
                h.store.set_state(host, "complete")
                page, cursor = watch_page(h, cursor)
                groups.append([{"watch": page}])
            def refuse(conversation, message):
                nonlocal cursor
                page, cursor = watch_page(h, cursor)
                assert any(c.get("state_reason") == "dispatching" for c in page["changes"])
                groups.append([{"watch": page}])
                raise OSError(28, "No space left on device")
            h.service._submit_turn = refuse
            for _ in range(retries):
                h.service._dispatch_one(h.store.message(mid))
                page, cursor = watch_page(h, cursor)
                groups.append([{"watch": page}])
            places = positions(core_probe, groups, cid, mid)[1:]
            assert places == ["tray" if initial_tray else "timeline"] * len(places), places
            seen = set()
            # Strong no-flicker: timeline -> tray alone is already a violation.
            for upto in range(1, len(groups) + 1):
                result = store_probe(core_probe, [s for g in groups[:upto] for s in g])
                layout = result["layouts"][cid]
                person = {i["message_id"] for i in layout["items"] if i["type"] == "person"}
                assert not seen.intersection(r["id"] for r in layout["tray"])
                seen.update(person)
        finally:
            h.close()


@pytest.mark.parametrize("repair", ["leave", "failover"])
def test_n5_n5b_n6_watch_discovers_repairs_before_publishing(core_probe, tmp_path, repair):
    h = ServiceHarness(tmp_path / "state")
    server = ServiceServer(h)
    try:
        cid = h.create()["conversation_id"]
        host = h.submit(cid, "host")["message_id"]
        h.store.set_state(host, "running")
        later = h.submit(cid, "later", after=host)["message_id"]
        steps = [{"open": h.call("conversation.open", conversation_id=cid)}, {"focus": cid}]
        cursor = h.call("conversation.watch", after=0, wait_s=0)["next"]
        h.store.set_state(host, "interrupted" if repair == "leave" else "failed")
        if repair == "leave":
            h.store.update_conversation(cid, blocked_by="unfinished-turn")
            h.call("conversation.unblock", conversation_id=cid, choice="leave", confirm=True)
        else:
            h.service._continue_elsewhere(h.store.conversation(cid), h.store.message(host))
        new = next(m for m in h.store.messages(cid) if m["origin"] in ("unblock-note", "failover"))
        page, cursor = watch_page(h, cursor)
        result = store_probe(core_probe, steps + [{"watch": page, "refresh_socket": str(server.path)}])
        layout = result["layouts"][cid]
        assert placement(layout, later) == "tray"
        assert placement(layout, new["message_id"]) != "absent"
        assert any(r["op"] == "message.status" and new["message_id"] in r["args"]["message_ids"] for r in server.requests)
        assert not any(r["op"] == "conversation.open" for r in server.requests)
        if repair == "leave":
            note = next(r for r in layout["tray"] if r["id"] == new["message_id"])
            assert note["origin"] == "unblock-note" and not note["can_withdraw"]
        else:
            assert next(i for i in layout["items"] if i["message_id"] == new["message_id"])["type"] == "notice"
    finally:
        server.close()
        h.close()


def test_n6_turn_completed_sets_state_without_a_receipt(core_probe):
    result = run_queue(core_probe, [{"receipts": [receipt("host", 1, "running"), receipt("later", 2, "queued")]},
        {"page": {"events": [{"seq": 1, "message_id": "host", "kind": "turn.completed", "ts": "2026-09-28T09:00:00Z",
                              "data": {"state": "complete"}}], "next": 1, "reset": False}}])
    last = result["snapshots"][-1]
    assert last["timeline"]["turns"]["host"]["state"] == "complete"
    assert not last["layout"]["live"] and "when this turn ends" not in last["layout"]["title"]


@pytest.mark.parametrize("reason", ["steer-missed: host ended", "steer-missed: deferred: disk busy", "held: external writer"])
def test_n8_missed_steer_names_the_wait_after_host_ends(core_probe, tmp_path, reason):
    h = ServiceHarness(tmp_path / "state")
    try:
        cid = h.create()["conversation_id"]
        host = h.submit(cid, "host")["message_id"]
        h.store.set_state(host, "running")
        mid = h.submit(cid, "steer", after=host)["message_id"]
        h.store.claim_steer(mid, host)
        opening = h.call("conversation.open", conversation_id=cid)
        h.store.set_state(mid, "queued", reason=reason)
        h.store.set_state(host, "complete")
        result = store_probe(core_probe, [{"open": opening}, {"receipts": h.call("message.status", message_ids=[host, mid])["messages"]}])
        assert placement(result["layouts"][cid], mid) == "timeline"
        assert "until the current turn ends" not in result["statuses"][mid].lower()
        assert result["queued_withdrawals"][mid] == {"action": "cancel", "message_id": mid}
        if "deferred:" in reason:
            assert "Deferred: disk busy" in result["statuses"][mid]
        if reason.startswith("held:"):
            assert "external writer" in result["statuses"][mid]
    finally:
        h.close()


@pytest.fixture(scope="module")
def fix4_pin(tmp_path_factory):
    from tests.frontend.swift import ROOT, compile_probe
    binary = compile_probe(tmp_path_factory.mktemp("fix4-pin") / "probe",
                           ROOT / "tests/frontend/ReviewR219Fix4PinProbe.swift", "SUBFLEET_VIEW_TEST", timeout=1100)
    return run_probe(binary)


@pytest.mark.parametrize("cause", ["approval", "search"])
def test_n9_app_row_scroll_suspends_even_an_already_scheduled_pin(fix4_pin, cause):
    run = next(r for r in fix4_pin["runs"] if r["cause"] == cause)
    assert fix4_pin["visible_windows"] == 0
    assert abs(run["after_queued_pin"] - 250) <= 2, run
    assert abs(run["after_history"] - 250) <= 2, run


def test_d1_binding_releases_the_row_and_later_requeues_keep_it_in_the_timeline(core_probe):
    result = run_queue(core_probe, [
        {"receipts": [receipt("host", 1, "running"), receipt("mine", 2, "queued")]},
        {"receipts": [receipt("host", 1, "complete")]},
        {"receipts": [{**receipt("mine", 2, "waiting", reason="admission: waiting"), "job_id": "job-mine"}]},
        {"receipts": [receipt("mine", 2, "queued", reason="deferred: try again")]},
        {"held": True}])
    snaps = result["snapshots"]
    assert ["mine" in tray(s) for s in snaps] == [True, True, False, False, False]
    assert snaps[-1]["timeline"]["turns"]["mine"]["queued_withdrawal"] == {"action": "cancel", "message_id": "mine"}


def test_d2_hydrates_every_unknown_id_beyond_the_status_batch_bound(core_probe, tmp_path):
    h = ServiceHarness(tmp_path / "state")
    server = ServiceServer(h)
    try:
        cid = h.create()["conversation_id"]
        steps = [{"open": h.call("conversation.open", conversation_id=cid)}, {"focus": cid}]
        previous, mids = None, []
        for index in range(201):
            previous = h.submit(cid, f"message {index}", after=previous)["message_id"]
            mids.append(previous)
        page = h.call("conversation.watch", after=0, wait_s=0)
        result = store_probe(core_probe, steps + [{"watch": page, "refresh_socket": str(server.path)}])
        layout = result["layouts"][cid]
        assert all(placement(layout, mid) != "absent" for mid in mids)
        reads = [r["args"]["message_ids"] for r in server.requests if r["op"] == "message.status"]
        assert sorted(map(len, reads)) == [1, 200]
        assert set().union(*map(set, reads)) == set(mids)
    finally:
        server.close()
        h.close()


def test_d2_an_event_page_hydrates_a_note_before_it_can_become_an_empty_person_bubble(core_probe, tmp_path):
    h = ServiceHarness(tmp_path / "state")
    server = ServiceServer(h)
    try:
        cid = h.create()["conversation_id"]
        host = h.submit(cid, "host")["message_id"]
        h.store.set_state(host, "interrupted")
        h.store.update_conversation(cid, blocked_by="unfinished-turn")
        steps = [{"open": h.call("conversation.open", conversation_id=cid)}, {"focus": cid}]
        h.call("conversation.unblock", conversation_id=cid, choice="leave", confirm=True)
        note = next(m for m in h.store.messages(cid) if m["origin"] == "unblock-note")
        h.store.append_events(conversation_id=cid, message_id=note["message_id"], attempt_id="note/a1",
                              events=[("stdout", "0", 0, "accepted", {})], stdout_offset=1, stdin_seq=0)
        page = h.call("conversation.events", conversation_id=cid, after=0, wait_s=0)
        result = store_probe(core_probe, steps + [{"events": page, "conversation_id": cid, "refresh_socket": str(server.path)}])
        item = next(i for i in result["layouts"][cid]["items"] if i["message_id"] == note["message_id"])
        assert item["type"] == "notice" and "stopped turn was left" in item["text"]
        assert [r["op"] for r in server.requests] == ["message.status"]
    finally:
        server.close()
        h.close()


def test_d1_an_unbound_external_writer_wait_keeps_its_timeline_reason_and_withdraw(core_probe):
    result = run_queue(core_probe, [local("mine", "idle send"),
        {"receipts": [receipt("mine", 1, "waiting", reason="external-writer: pid 42")]}])
    last = result["snapshots"][-1]
    assert "mine" not in tray(last)
    turn = last["timeline"]["turns"]["mine"]
    assert turn["shows_acknowledgment"]
    assert "close it there to continue here" in turn["status_text"]
    assert turn["queued_withdrawal"] == {"action": "cancel", "message_id": "mine"}
