"""Review r1 of #219 (queue tray): daemon-backed regressions from the independent review.

The oracle here is the daemon's own code (store.next_dispatchable, LIVE_STATES,
blocked_by), not a transliteration of the Swift.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import uuid

from hypothesis import HealthCheck, event, given, settings, strategies as st
import pytest

from subfleet.conversations.turn import LIVE_STATES
from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import ServiceHarness, ServiceServer

pytestmark = needs_swift

PROPERTY = settings(max_examples=40, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


def store_probe(core_probe, steps: list[dict]) -> dict:
    with tempfile.TemporaryDirectory(prefix="sf-r219-") as scratch:
        return run_probe(core_probe, "store", write_json(Path(scratch) / f"{uuid.uuid4().hex}.json", {"steps": steps}))


def placement(layout: dict, mid: str) -> str:
    if mid in [row["id"] for row in layout["tray"]]:
        return "tray"
    if f"person:{mid}" in [item["id"] for item in layout["items"]]:
        return "timeline"
    return "absent"


def flickers(places: list[str]) -> bool:
    """timeline -> tray -> timeline anywhere in the sequence."""
    seen_timeline, then_tray = False, False
    for place in places:
        if place == "timeline":
            if then_tray:
                return True
            seen_timeline = True
        elif place == "tray" and seen_timeline:
            then_tray = True
    return False


# MARK: - R1. Tray membership and order against the daemon's own dispatcher


@st.composite
def conversation_specs(draw):
    n = draw(st.integers(1, 7))
    origins = ["person"] + [draw(st.sampled_from(["person"] * 4 + ["wake", "unblock-note", "failover"]))
                            for _ in range(n - 1)]
    live = draw(st.sampled_from([None, None, "running", "approval-needed", "waiting", "starting", "delivery-unknown"]))
    live_index = draw(st.integers(0, n - 1)) if live else None
    states, reasons, steer = [], [], []
    for index in range(n):
        if index == live_index:
            states.append(live)
            reasons.append(None)
            steer.append(False)
            continue
        state = draw(st.sampled_from(["queued"] * 6 + ["complete", "failed", "interrupted", "cancelled"]))
        states.append(state)
        reasons.append("steer-missed: host ended" if state == "queued" and origins[index] == "person"
                       and draw(st.integers(0, 4)) == 0 else None)
        steer.append(state == "queued" and origins[index] == "person" and draw(st.integers(0, 3)) == 0)
    parents = [draw(st.integers(0, index - 1)) if origins[index] == "failover" and index > 0 else None
               for index in range(n)]
    block = draw(st.sampled_from([None, None, None, "unfinished-turn"]))
    return {"origins": origins, "states": states, "reasons": reasons, "steer": steer, "parents": parents,
            "block": block}


def build(harness: ServiceHarness, spec: dict) -> tuple[str, list[str], dict]:
    cid = harness.create()["conversation_id"]
    mids, last_person = [], None
    for index, origin in enumerate(spec["origins"]):
        mid = str(uuid.uuid4())
        parent = spec["parents"][index]
        harness.store.submit_message(conversation_id=cid, message_id=mid, after_message_id=last_person,
                                     text=f"message {index}", attachments=[], settings=harness.settings(),
                                     origin=origin, continues=mids[parent] if parent is not None else None,
                                     state_reason=spec["reasons"][index])
        if origin == "person":
            last_person = mid
        mids.append(mid)
    why = {"waiting": "dispatching", "delivery-unknown": "ended: frame written, native record missing",
           "cancelled": "withdrawn"}
    for mid, state in zip(mids, spec["states"]):
        if state != "queued":
            assert harness.store.set_state(mid, state, reason=why.get(state))
    if "delivery-unknown" in spec["states"]:
        harness.store.update_conversation(cid, blocked_by="delivery-unknown")   # as service.py does with it
    elif spec["block"]:
        harness.store.update_conversation(cid, blocked_by=spec["block"])
    host = next((mid for mid, state in zip(mids, spec["states"]) if state in ("running", "approval-needed")), None)
    steered = []
    if host and not harness.store.conversation(cid)["blocked_by"]:
        for mid, wants in zip(mids, spec["steer"]):
            if wants:
                try:
                    harness.store.claim_steer(mid, host)
                    steered.append(mid)
                except Exception:                    # not-next behind a repair, etc.: stays queued
                    pass
    return cid, mids, {"steered": steered, "host": host}


def daemon_truth(harness: ServiceHarness, cid: str, mids: list[str]) -> list[str]:
    """The tray the daemon implies: every queued message the dispatcher would not
    send next right now (something live in LIVE_STATES, a block, or an earlier
    dispatch), except a failover continuation, which the design keeps as a notice.
    Order is next_dispatchable's, read by draining a copy of the queue."""
    rows = {m["message_id"]: m for m in harness.store.messages(cid)}
    conversation = harness.store.conversation(cid)
    live_now = any(rows[mid]["state"] in LIVE_STATES for mid in mids)
    blocked = bool(conversation["blocked_by"])
    # Drain: clear the block and settle everything live, then take the head until none is left.
    harness.store.update_conversation(cid, blocked_by=None)
    for mid in mids:
        if rows[mid]["state"] in LIVE_STATES:
            harness.store.set_state(mid, "complete")
    order = []
    while heads := harness.store.next_dispatchable(cid):
        order.append(heads[0]["message_id"])
        harness.store.set_state(heads[0]["message_id"], "cancelled", reason="drained")
    return [mid for index, mid in enumerate(order)
            if (index > 0 or live_now or blocked or (rows[mid]["state_reason"] or "").startswith("deferred:"))
            and not rows[mid]["continues"]
            and not (rows[mid]["state_reason"] or "").startswith("steer-missed:")]


@PROPERTY
@given(specs=st.lists(conversation_specs(), min_size=1, max_size=5))
def test_r1_tray_matches_the_daemons_dispatcher(core_probe, specs):
    with tempfile.TemporaryDirectory(prefix="sf-r219-r1-") as scratch:
        harness = ServiceHarness(Path(scratch))
        try:
            caps = harness.call("capabilities")
            built, steps = [], [{"capabilities": caps}]
            for spec in specs:
                cid, mids, extra = build(harness, spec)
                opened = harness.call("conversation.open", conversation_id=cid)
                steps.append({"open": opened})
                built.append((cid, mids, extra, spec))
            result = store_probe(core_probe, steps)
            for cid, mids, extra, spec in built:
                expected = daemon_truth(harness, cid, mids)
                layout = result["layouts"][cid]
                tray = [row["id"] for row in layout["tray"]]
                states = {m: s for m, s in zip(mids, spec["states"])}
                event(f"tray size {len(tray)}; steered {len(extra['steered'])}; block {spec['block']}")
                assert tray == expected, {"spec": spec, "tray": tray, "expected": expected, "mids": mids,
                                          "steered": extra["steered"]}
                for mid in mids:
                    assert placement(layout, mid) != "absent", (mid, states.get(mid), layout)
        finally:
            harness.close()


# MARK: - R2. A steer the provider misses (real daemon, watch rows as the app gets them)


def watch_page(harness: ServiceHarness, cursor: int) -> tuple[dict, int]:
    page = harness.call("conversation.watch", after=cursor, wait_s=0)
    return page, page["next"]


def positions(core_probe, prefix_steps: list[list[dict]], cid: str, mid: str) -> list[str]:
    out = []
    for upto in range(1, len(prefix_steps) + 1):
        steps = [step for group in prefix_steps[:upto] for step in group]
        out.append(placement(store_probe(core_probe, steps)["layouts"][cid], mid))
    return out


def test_r2_a_missed_steer_stays_in_the_timeline(core_probe):
    with tempfile.TemporaryDirectory(prefix="sf-r219-r2-") as scratch:
        harness = ServiceHarness(Path(scratch))
        try:
            caps = harness.call("capabilities")
            cid = harness.create()["conversation_id"]
            host = harness.submit(cid, "the running turn")["message_id"]
            assert harness.store.set_state(host, "running", expect=("queued",))
            later = harness.submit(cid, "queued for later", after=host)["message_id"]
            groups = [[{"capabilities": caps}, {"open": harness.call("conversation.open", conversation_id=cid)}]]
            cursor = harness.call("conversation.watch", after=0, wait_s=0)["next"]
            steer = str(uuid.uuid4())
            # The person presses Return while the turn runs: a steer.
            groups.append([{"local": {"conversation_id": cid, "message_id": steer, "text": "also do this",
                                      "steer": True}}])
            answer = harness.submit(cid, "also do this", after=later, message_id=steer)
            groups.append([{"receipts": [answer]}])
            harness.store.claim_steer(steer, host)
            page, cursor = watch_page(harness, cursor)
            groups.append([{"watch": page}])
            # The turn ends before the provider reads it: settled before its host (service._settle_steers).
            assert harness.store.set_state(steer, "queued", reason="steer-missed: host ended", expect=("steering",))
            page, cursor = watch_page(harness, cursor)
            groups.append([{"watch": page}])
            assert harness.store.set_state(host, "complete", expect=("running",))
            page, cursor = watch_page(harness, cursor)
            groups.append([{"watch": page}])
            places = positions(core_probe, groups, cid, steer)
            later_places = positions(core_probe, groups, cid, later)
            rows = []
            for upto in range(1, len(groups) + 1):
                layout = store_probe(core_probe, [s for g in groups[:upto] for s in g])["layouts"][cid]
                rows.append([(r["id"] == steer and "STEER" or r["id"] == later and "LATER", r["status"], r["can_steer"])
                             for r in layout["tray"]] + [layout["title"]])
        finally:
            harness.close()
    print("steer places", places, "later places", later_places)
    for row in rows:
        print("   tray", row)
    assert places == ["absent"] + ["timeline"] * (len(groups) - 1), places
    assert not flickers(places), places


# MARK: - R3. A block the app has not read yet (the list refresh is every 30 s)


@pytest.mark.parametrize("settle", ["delivery-unknown", "unfinished-turn"])
def test_r3_a_block_read_late_moves_the_head_out_and_back(core_probe, settle):
    with tempfile.TemporaryDirectory(prefix="sf-r219-r3-") as scratch:
        harness = ServiceHarness(Path(scratch))
        server = ServiceServer(harness)
        try:
            caps = harness.call("capabilities")
            cid = harness.create()["conversation_id"]
            host = harness.submit(cid, "the running turn")["message_id"]
            assert harness.store.set_state(host, "running", expect=("queued",))
            later = harness.submit(cid, "queued for later", after=host)["message_id"]
            groups = [[{"capabilities": caps}, {"open": harness.call("conversation.open", conversation_id=cid)},
                       {"focus": cid}]]
            cursor = harness.call("conversation.watch", after=0, wait_s=0)["next"]
            # The settlement: block first, then the message (service.py, "Before the message settles").
            harness.store.update_conversation(cid, blocked_by=settle)
            assert harness.store.set_state(host, "delivery-unknown" if settle == "delivery-unknown" else "interrupted",
                                           expect=("running",))
            page, cursor = watch_page(harness, cursor)
            groups.append([{"watch": page, "refresh_socket": str(server.path)}])
            # The 30 s list timer reads the block.
            groups.append([{"list": harness.call("conversation.list", include_catalog=False)}])
            places = positions(core_probe, groups, cid, later)
            truth_live = harness.store.message(host)["state"] in LIVE_STATES
            truth_blocked = harness.store.conversation(cid)["blocked_by"]
            assert any(request["op"] == "conversation.open" for request in server.requests)
        finally:
            server.close()
            harness.close()
    print(settle, "later places", places, "daemon live", truth_live, "blocked", truth_blocked)
    # The daemon holds `later` throughout (blocked, and for delivery-unknown also a live state).
    assert places == ["tray", "tray", "tray"], places


# MARK: - R4. Withdraw against the real daemon: a steer's host is never stopped


@pytest.fixture
def daemon():
    with tempfile.TemporaryDirectory(prefix="sf-r219-d-") as scratch:
        harness = ServiceHarness(Path(scratch))
        server = ServiceServer(harness)
        try:
            yield harness, server
        finally:
            server.close()
            harness.close()


def engine_live(core_probe, server, steps: list[dict]) -> dict:
    with tempfile.TemporaryDirectory(prefix="sf-r219-e-") as scratch:
        path = write_json(Path(scratch) / f"{uuid.uuid4().hex}.json", {"socket": str(server.path), "steps": steps})
        return run_probe(core_probe, "queue-engine", path, timeout=120)


def cancel(mid: str) -> dict:
    return {"do": "stop", "action": {"action": "cancel", "message_id": mid}}


def steering_setup(harness) -> tuple[str, str, str]:
    cid = harness.create()["conversation_id"]
    host = harness.submit(cid, "host")["message_id"]
    assert harness.store.set_state(host, "running", expect=("queued",))
    mid = harness.submit(cid, "steer me", after=host)["message_id"]
    harness.store.claim_steer(mid, host)
    return cid, host, mid


@pytest.mark.parametrize("then", ["steering", "steered", "delivery-unknown", "steer-missed"])
def test_r4_withdraw_of_a_steer_never_stops_its_host(core_probe, daemon, then):
    harness, server = daemon
    cid, host, mid = steering_setup(harness)
    if then == "steered":
        assert harness.store.set_state(mid, "steered", reason=f"steered:{host}", expect=("steering",))
    elif then == "delivery-unknown":
        harness.store.update_conversation(cid, blocked_by="delivery-unknown")
        assert harness.store.set_state(mid, "delivery-unknown", reason=f"steer:{host}", expect=("steering",))
    elif then == "steer-missed":
        assert harness.store.set_state(mid, "queued", reason="steer-missed: x", expect=("steering",))
    out = engine_live(core_probe, server, [cancel(mid), cancel(mid)])
    ops = [request["op"] for request in server.requests]
    host_row, row = harness.store.message(host), harness.store.message(mid)
    print(then, [r.get("error") or r.get("receipt", {}).get("state") for r in out["results"]], ops)
    assert "turn.interrupt" not in ops
    assert host_row["state"] == "running" and host_row["stop_requested_at"] is None
    if then == "steer-missed":
        assert row["state"] == "cancelled"
        assert all("error" not in r for r in out["results"])     # the second is settled, not an error


def test_r4b_withdraw_send_of_a_steer_the_daemon_holds(core_probe, daemon):
    """The tray's row with no receipt, whose send the daemon took and steered."""
    harness, server = daemon
    cid = harness.create()["conversation_id"]
    host = harness.submit(cid, "host")["message_id"]
    assert harness.store.set_state(host, "running", expect=("queued",))
    mid = str(uuid.uuid4())
    harness.submit(cid, "steer me", after=host, message_id=mid)
    harness.store.claim_steer(mid, host)
    out = engine_live(core_probe, server, [
        {"do": "journal", "conversation": cid, "message_id": mid, "text": "steer me"},
        {"do": "lose_answer", "message_id": mid},
        {"do": "withdraw_send", "message_id": mid},
    ])
    ops = [request["op"] for request in server.requests]
    print(out["results"][2], ops)
    assert "turn.interrupt" not in ops
    assert harness.store.message(host)["stop_requested_at"] is None


def test_r5_withdraw_of_its_own_running_turn_twice(core_probe, daemon):
    harness, server = daemon
    cid = harness.create()["conversation_id"]
    first = harness.submit(cid, "first")["message_id"]
    mid = harness.submit(cid, "second", after=first)["message_id"]
    assert harness.store.set_state(first, "complete", expect=("queued",))
    assert harness.store.set_state(mid, "running", expect=("queued",))
    out = engine_live(core_probe, server, [cancel(mid), cancel(mid)])
    print([(r.get("calls"), r.get("error")) for r in out["results"]])
    assert all("error" not in r for r in out["results"])
    assert [request["args"]["message_id"] for request in server.requests if request["op"] == "turn.interrupt"] \
        == [mid, mid]
    assert harness.store.message(first)["stop_requested_at"] is None


def test_r6_withdraw_after_a_daemon_restart(core_probe, tmp_path):
    root = tmp_path / "state"
    harness = ServiceHarness(root)
    cid = harness.create()["conversation_id"]
    host = harness.submit(cid, "host")["message_id"]
    assert harness.store.set_state(host, "running", expect=("queued",))
    mid = harness.submit(cid, "queued", after=host)["message_id"]
    harness.close()
    harness = ServiceHarness(root)                      # the daemon again, same store
    server = ServiceServer(harness)
    try:
        out = engine_live(core_probe, server, [cancel(mid)])
        row = harness.store.message(mid)
    finally:
        server.close()
        harness.close()
    assert "error" not in out["results"][0], out
    assert (row["state"], row["state_reason"]) == ("cancelled", "withdrawn")


@st.composite
def forced_specs(draw):
    """A live host first, steers likely; or a delivery-unknown host; the rest drawn as in R1."""
    spec = draw(conversation_specs())
    n = len(spec["origins"])
    host_state = draw(st.sampled_from(["running", "approval-needed", "delivery-unknown"]))
    spec["states"][0] = host_state
    spec["reasons"][0] = None
    spec["steer"] = [index > 0 and spec["origins"][index] == "person" and spec["states"][index] == "queued"
                     and draw(st.booleans()) for index in range(n)]
    for index in range(1, n):
        if spec["states"][index] in ("running", "approval-needed", "waiting", "starting", "delivery-unknown"):
            spec["states"][index] = "queued"
    return spec


@PROPERTY
@given(specs=st.lists(forced_specs(), min_size=2, max_size=5))
def test_r1b_tray_matches_the_daemon_with_steers_and_unknown_delivery(core_probe, specs):
    with tempfile.TemporaryDirectory(prefix="sf-r219-r1b-") as scratch:
        harness = ServiceHarness(Path(scratch))
        try:
            caps = harness.call("capabilities")
            built, steps = [], [{"capabilities": caps}]
            for spec in specs:
                cid, mids, extra = build(harness, spec)
                steps.append({"open": harness.call("conversation.open", conversation_id=cid)})
                built.append((cid, mids, extra, spec))
            result = store_probe(core_probe, steps)
            for cid, mids, extra, spec in built:
                expected = daemon_truth(harness, cid, mids)
                layout = result["layouts"][cid]
                tray = [row["id"] for row in layout["tray"]]
                event(f"host {spec['states'][0]}; steered {min(len(extra['steered']), 2)}; tray {min(len(tray), 3)}")
                assert tray == expected, {"spec": spec, "tray": tray, "expected": expected, "mids": mids,
                                          "steered": extra["steered"]}
                for mid in mids:
                    assert placement(layout, mid) != "absent", (mid, layout)
        finally:
            harness.close()


# MARK: - R7. A head the dispatcher deferred (claim released back to queued, service.py _defer)


def test_r7_a_deferred_head_has_its_reason_and_withdraw_in_the_tray(core_probe):
    with tempfile.TemporaryDirectory(prefix="sf-r219-r7-") as scratch:
        harness = ServiceHarness(Path(scratch))
        try:
            caps = harness.call("capabilities")
            cid = harness.create()["conversation_id"]
            first = harness.submit(cid, "done already")["message_id"]
            assert harness.store.set_state(first, "complete", expect=("queued",))
            mid = harness.submit(cid, "stuck one", after=first)["message_id"]
            behind = harness.submit(cid, "behind it", after=mid)["message_id"]
            # What _dispatch_one leaves after a transient refusal of the submit: queued, deferred.
            assert harness.store.set_state(mid, "queued", reason="deferred: the conversation's worktree was not created; "
                                           "repeat conversation.create", expect=("queued",))
            out = store_probe(core_probe, [{"capabilities": caps},
                                           {"open": harness.call("conversation.open", conversation_id=cid)}])
        finally:
            harness.close()
    layout = out["layouts"][cid]
    status = out["timeline"][cid]["turns"][mid]["status_text"] if "turns" in out["timeline"].get(cid, {}) else None
    print("placement", placement(layout, mid), "tray", [(r["id"] == behind, r["status"]) for r in layout["tray"]],
          "title", layout["title"], "status", status)
    assert placement(layout, mid) == "tray", "a deferred head has no Withdraw anywhere"

    row = next(row for row in layout["tray"] if row["id"] == mid)
    assert row["status"] == "Deferred: the conversation's worktree was not created; repeat conversation.create"
    assert row["can_withdraw"] and row["withdraw"] == {"action": "cancel", "message_id": mid}


@pytest.mark.parametrize("ahead", sorted(LIVE_STATES))
def test_r3b_every_dispatcher_live_state_holds_the_tray(core_probe, ahead):
    from tests.frontend.test_core_queue_tray import receipt, run_queue, tray
    result = run_queue(core_probe, [{"receipts": [receipt("host", 1, ahead), receipt("later", 2, "queued")]}])
    assert tray(result["snapshots"][-1]) == ["later"]


@pytest.fixture(scope="module")
def opening_scroll(tmp_path_factory):
    from tests.frontend.swift import ROOT, compile_probe
    binary = compile_probe(tmp_path_factory.mktemp("r219-scroll") / "probe",
                           ROOT / "tests/frontend/ReviewR219ScrollProbe.swift", "SUBFLEET_VIEW_TEST")
    return run_probe(binary)


@pytest.mark.parametrize("path", ["anchor-15", "emulated-14"])
def test_r8_opening_stays_at_the_end_after_late_measurement(opening_scroll, path):
    assert opening_scroll["visible_windows"] == 0
    run = next(run for run in opening_scroll["runs"] if run["name"] == path)
    assert run["opening"][-1]["found"] == 1
    assert abs(run["opening"][-1]["from_end"]) <= 2, run["opening"]
    assert abs(run["late_page"][-1]["from_end"]) <= 2, run["late_page"]
    assert abs(run["settling"][-1]["from_end"]) <= 2, run["settling"]
    assert run["after_person"][-1]["from_end"] > 100, run["after_person"]
