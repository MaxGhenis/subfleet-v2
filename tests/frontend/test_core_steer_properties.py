"""Properties of the app's steer logic, for every input Hypothesis finds (C-24.9, C-24.2, C-28.3).

Invariants, each checked after every step of a generated history:

Timeline (`Timeline.items`, the fold probe, over arbitrary receipts, events, resets,
local sends and steer answers):
  T1. Every row's id is unique (SwiftUI's `ForEach` needs it).
  T2. Each message's bubble is drawn exactly once, or never for a tombstone.
  T3. The `steer.delivered` marker is never drawn itself.
  T4. A message is drawn inside a host turn only while it is steering or steered
      (or has no receipt yet), after its host's own bubble.

Outbox (the Swift outbox against the real service, `message.steer` recorded in the
section 6 shape, with lost answers, busy answers and refusals):
  O1. At most one thing per conversation may be sent at any moment.
  O2. A conversation's submits are first sent in journal order; a steer is never
      sent before its own message's submit was answered.
  O3. Nothing stays open: every submit is acknowledged and every steer closes, a
      refused one holding nothing behind it.
  O4. Exactly once: a message is steering in the daemon iff its steer was
      acknowledged; a refused steer leaves it queued; no message is stored twice.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import uuid

from hypothesis import HealthCheck, given, settings, strategies as st

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import STEER_REFUSALS, RecordedSteer, ServiceHarness, ServiceServer

pytestmark = needs_swift

CID = "cv-steer-properties"
MESSAGES = [f"00000000-0000-4000-8000-00000000000{i}" for i in range(5)]
STATES = ["queued", "waiting", "starting", "running", "approval-needed", "complete", "failed", "interrupted",
          "cancelled", "delivery-unknown", "steering", "steered", "unknown"]
REASONS = [None, "steer:{h}", "steered:{h}", "steered-unanswered:{h}", "steer-missed: stopped", "stopped"]
ORIGINS = ["person", "person", "person", "tombstone", "failover"]


# --- T1-T4 -----------------------------------------------------------------------------

message = st.sampled_from(MESSAGES)
receipt = st.builds(lambda m, state, reason, origin, host: {
    "message_id": m, "conversation_id": CID, "seq": MESSAGES.index(m) + 1, "origin": origin, "state": state,
    "state_reason": reason.format(h=host) if reason else None},
    message, st.sampled_from(STATES), st.sampled_from(REASONS), st.sampled_from(ORIGINS), message)
event = st.one_of(
    st.tuples(st.just("steer.delivered"), message, message),
    st.tuples(st.just("steer.missed"), message, message),
    st.tuples(st.just("text"), message, st.sampled_from(["a", "b"])),
    st.tuples(st.just("tool.started"), message, st.sampled_from(["t1", "t2"])),
    st.tuples(st.just("accepted"), message, st.just("")),
    st.tuples(st.just("turn.completed"), message, st.just("")),
)
step = st.one_of(
    st.tuples(st.just("receipts"), st.lists(receipt, min_size=1, max_size=3)),
    st.tuples(st.just("events"), st.lists(event, min_size=1, max_size=4)),
    st.tuples(st.just("reset"), st.just(None)),
    st.tuples(st.just("local"), st.tuples(message, st.booleans())),
    st.tuples(st.just("steer_request"), message),
    st.tuples(st.just("steer_answer"), st.tuples(message, st.sampled_from([None, *STEER_REFUSALS]))),
)


def event_row(seq: int, kind: str, host: str, arg: str) -> dict:
    data = {"steer.delivered": {"message_id": arg}, "steer.missed": {"message_id": arg, "why": "stopped"},
            "text": {"block": arg, "text": f"text {arg}"}, "tool.started": {"id": arg, "name": "Bash"},
            "accepted": {}, "turn.completed": {"state": "complete"}}[kind]
    return {"seq": seq, "message_id": host, "kind": kind, "ts": f"2026-09-28T12:00:{seq % 60:02d}.000Z", "data": data}


def fold_steps(history: list) -> list[dict]:
    """The generated history as fold-probe steps, a snapshot after each; a reset is
    the daemon's `reset` answer and then the whole log read again from 0."""
    out, log = [], []
    for kind, value in history:
        if kind == "receipts":
            out.append({"receipts": value})
        elif kind == "events":
            rows = [event_row(len(log) + i + 1, *e) for i, e in enumerate(value)]
            log += rows
            out.append({"page": {"events": rows, "next": len(log), "reset": False}})
        elif kind == "reset":
            out.append({"page": {"events": [], "next": 0, "reset": True}})
            out.append({"page": {"events": log, "next": len(log), "reset": False}})
        elif kind == "local":
            out.append({"local": {"message_id": value[0], "text": "local", "steer": value[1]}})
        elif kind == "steer_request":
            out.append({"steer_request": value})
        else:
            mid, code = value
            out.append({"steer_answer": {"message_id": mid,
                                         "refusal": code and {"reason": code, "message": f"{code}: no"}}})
        out[-1]["snapshot"] = True
    return out


def check_timeline(snapshot: dict) -> None:
    ids = [item["id"] for item in snapshot["items"]]
    assert len(ids) == len(set(ids)), ids                                                # T1
    assert not any(i.startswith("steer:") for i in ids), ids                              # T3
    for mid, turn in snapshot["turns"].items():
        if mid == "(conversation)":
            continue
        drawn = ids.count(f"person:{mid}")
        assert drawn == (0 if turn["origin"] == "tombstone" else 1), (mid, turn, ids)     # T2
    for mid in snapshot["placed_steers"]:
        turn = snapshot["turns"][mid]
        assert turn["steer_delivered_in"] and (turn["state"] in ("steering", "steered", "sending")), turn   # T4
        host = f"person:{turn['steer_delivered_in']}"
        if host in ids and f"person:{mid}" in ids:
            assert ids.index(host) < ids.index(f"person:{mid}"), (host, mid, ids)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(history=st.lists(step, min_size=1, max_size=14))
def test_t1_t4_a_message_is_drawn_once_whatever_the_daemon_says(core_probe, history):
    with tempfile.TemporaryDirectory(prefix="sf-steer-t-") as directory:
        path = write_json(Path(directory) / "fold.json", {"conversation_id": CID, "steps": fold_steps(history)})
        out = run_probe(core_probe, "fold", path)
    for snapshot in out["snapshots"]:
        check_timeline(snapshot)


# --- O1-O4 -------------------------------------------------------------------------------

HOST = "0f0f0f0f-0000-4000-8000-000000000000"


class PlannedSteer(RecordedSteer):
    """The recorded op, refusing the messages the plan names, every time they are steered."""

    def __init__(self, harness, plan: dict[str, str]):
        super().__init__(harness, HOST)
        self.plan = plan

    def __call__(self, args, peer):
        code = self.plan.get(args["message_id"])
        if code:
            self.refuse = [code]
        return super().__call__(args, peer)


op = st.one_of(
    st.tuples(st.just("submit"), st.sampled_from("AB"), st.booleans()),
    st.tuples(st.just("steer"), st.sampled_from("AB"), st.integers(0, 5)),
    st.tuples(st.just("pump"), st.just(None), st.just(None)),
    st.tuples(st.just("advance"), st.just(None), st.sampled_from([0.4, 1, 5])),
    st.tuples(st.just("sendable"), st.just(None), st.just(None)),
)
fate = st.sampled_from(["ok", "ok", "drop", "busy", *STEER_REFUSALS])


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops=st.lists(op, min_size=1, max_size=16), fates=st.lists(fate, min_size=12, max_size=12))
def test_o1_o4_the_outbox_sends_in_order_and_steers_exactly_once(core_probe, ops, fates):
    root = Path(tempfile.mkdtemp(prefix="sf-steer-o-", dir="/tmp"))
    harness = ServiceHarness(root / "daemon")
    server = ServiceServer(harness)
    try:
        run_outbox_history(core_probe, root, harness, server, ops, fates)
    finally:
        server.close()
        harness.close()


def run_outbox_history(core_probe, root: Path, harness, server, ops, fates) -> None:
    pool = [str(uuid.uuid4()) for _ in range(len(ops))]
    messages: dict[str, list[str]] = {"A": [], "B": []}
    plan: dict[str, str] = {}
    steps = [{"do": "create", "request_id": f"req-{c}", "workspace": str(harness.workspace)} for c in "AB"]
    steps.append({"do": "pump"})
    steered: list[str] = []
    for kind, conversation, arg in ops:
        if kind == "submit":
            mid = pool.pop()
            messages[conversation].append(mid)
            steps.append({"do": "submit", "conversation": f"@conv:req-{conversation}", "message_id": mid,
                          "text": f"m{len(steps)}", "steer": arg})
            if arg:
                steered.append(mid)
        elif kind == "steer":
            if not messages[conversation]:
                continue
            mid = messages[conversation][arg % len(messages[conversation])]
            steps.append({"do": "steer", "key": mid, "conversation": f"@conv:req-{conversation}"})
            steered.append(mid)
        elif kind == "advance":
            steps.append({"do": "advance", "seconds": arg})
        else:
            steps.append({"do": kind})
    for index, mid in enumerate(dict.fromkeys(steered)):
        how = fates[index % len(fates)]
        if how in ("drop", "busy"):
            server.faults[("message.steer", mid)] = how            # once: the next try is answered
        elif how != "ok":
            plan[mid] = how
    harness.service.op_message_steer = PlannedSteer(harness, plan)
    # Then time passes and the app keeps pumping until nothing is left.
    steps += [s for _ in range(8) for s in ({"do": "advance", "seconds": 40}, {"do": "pump"})]
    out = run_probe(core_probe, "outbox", server.path, root / "support" / "outbox.json",
                    write_json(root / "steps.json", steps), timeout=180)
    assert not any("error" in r for r in out["results"]), [r for r in out["results"] if "error" in r]

    entries = {e["key"]: e for e in out["entries"]}
    steers = {s["message_id"]: s for s in out["steers"]}
    conversation_of = {**{e["key"]: e["conversation"] for e in out["entries"]},
                       **{s["key"]: s["conversation"] for s in out["steers"]}}
    for result in out["results"]:                                                          # O1
        if result["do"] == "sendable":
            lanes = [conversation_of[k] for k in result["keys"] if k in conversation_of and not k.startswith("req-")]
            assert len(lanes) == len(set(lanes)), result

    submits = [c.split(" ")[1] for c in out["calls"] if c.startswith("message.submit")]
    first_sends = list(dict.fromkeys(submits))
    for conversation in {e["conversation"] for e in out["entries"] if e["kind"] == "message.submit"}:   # O2
        journal = [e["key"] for e in sorted(out["entries"], key=lambda e: e["order"])
                   if e["kind"] == "message.submit" and e["conversation"] == conversation]
        assert [k for k in first_sends if k in journal] == journal
    answered: set[str] = set()
    for call in out["calls"]:
        op_name, key = call.split(" ")[:2]
        if op_name == "message.submit" and call.endswith("answered"):
            answered.add(key)
        if op_name == "message.steer":
            assert key in answered, (key, out["calls"])

    assert all(e["state"] == "acknowledged" for e in out["entries"]), out["entries"]      # O3
    assert all(s["state"] in ("acknowledged", "refused") for s in out["steers"]), out["steers"]

    for mid, steer in steers.items():                                                       # O4
        stored = harness.store.message(mid)
        if steer["state"] == "acknowledged":
            assert stored["state"] == "steering" and steer["receipt"]["steered_into"] == HOST, (steer, stored)
        else:
            assert stored["state"] == "queued" and steer["failure"]["reason"] == plan[mid], (steer, stored)
    for mid in [m for ms in messages.values() for m in ms if m not in steers]:
        assert harness.store.message(mid)["state"] == "queued"
    rows = harness.store.query("SELECT message_id FROM messages")
    assert len(rows) == len({r["message_id"] for r in rows}) == sum(map(len, messages.values()))
    for conversation, mids in messages.items():
        if mids:
            chain = [r["after_message_id"] for r in harness.store.query(
                "SELECT after_message_id FROM messages WHERE conversation_id=? ORDER BY seq",
                (harness.store.message(mids[0])["conversation_id"],))]
            assert chain == [None, *mids[:-1]]
