"""A pending approval is always within reach (design §12; C-27.1).

On 2026-09-27 a turn waited 10 minutes on a card nobody saw: the timeline put
the card under its own message, above five queued messages and a failover turn
that sorted last although it ran first, and the pinned strip said "Needs your
approval" with only a Stop button. These tests pin what the conversation view
reads from the timeline:

- the strip's Review exists exactly while a card is pending, counts every
  pending card, and opens the oldest; answering it moves Review to the next, so
  every pending card is reachable from the strip without scrolling;
- whenever a card is pending there is a strip to carry the Review;
- a continuation shows under the message it continues, and a message still
  queued shows below every turn that has started;
- the view follows the newest row of a started turn, not a queued bubble;
- each new card is scrolled to once when it appears, and the oldest whenever the
  person asks.

The example tests replay the incident; the property tests check the same
statements over generated timelines, and compare the display order with a
reference written from the rule above.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import uuid

from hypothesis import HealthCheck, event, given, settings, strategies as st
import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import ServiceHarness, claude_init
from tests.frontend.test_core_timeline import claude_approval, items_of, page, replay

pytestmark = needs_swift

CID = "cv-approval-reach"
T0 = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)
WAITING = {"queued", "sending"}


def fold(core_probe, steps: list[dict], cid: str = CID) -> dict:
    with tempfile.TemporaryDirectory(prefix="sf-reach-") as scratch:
        return run_probe(core_probe, "fold", write_json(Path(scratch) / f"{uuid.uuid4().hex}.json",
                                                        {"conversation_id": cid, "steps": steps}))


def ts(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def receipt(mid: str, seq: int, state: str, origin: str = "person", continues: str | None = None,
            text: str | None = None) -> dict:
    return {"message_id": mid, "conversation_id": CID, "seq": seq, "origin": origin, "continues": continues,
            "state": state, "text": text if text is not None else f"message {seq}"}


class Log:
    """A synthetic `conversation.events` log, handed out as pages."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.read = 0

    def add(self, mid: str, event_kind: str, /, **data) -> dict:
        seq = len(self.events) + 1
        event = {"seq": seq, "message_id": mid, "kind": event_kind, "ts": ts(seq), "data": data}
        self.events.append(event)
        return event

    def page(self) -> dict:
        events, self.read = self.events[self.read:], len(self.events)
        return {"page": {"events": events, "next": len(self.events), "reset": False}}


def ask(log: Log, mid: str, request_id: str, command: str = "rm -f mocktest/*") -> str:
    log.add(mid, "approval.requested", request_id=request_id, kind="tool", tool="Bash",
            input=f"command: {command}", options=["allow", "deny", "cancel-turn"])
    return f"approval:{mid}:{request_id}"


def person_positions(result: dict) -> dict[str, int]:
    return {item["message_id"]: index for index, item in enumerate(result["items"])
            if item["id"] == f"person:{item['message_id']}"}


# MARK: - The 2026-09-27 conversation


def incident(log: Log) -> tuple[list[dict], str]:
    """Messages 6 to 13 of cv-1790290856733-385ce6acfca0 as they stood at 01:58:52Z:
    6 hit a usage limit, 13 continued it (and ran), 7 asked for approval, 8 to 12
    were queued behind 7."""
    receipts = [receipt("m6", 6, "failed"), receipt("m7", 7, "running"),
                *(receipt(f"m{n}", n, "queued") for n in range(8, 13)),
                receipt("m13", 13, "complete", origin="failover", continues="m6")]
    log.add("m6", "accepted")
    log.add("m6", "text", block="0", text="Working on it.")
    log.add("m13", "accepted")
    log.add("m13", "thinking.delta", block="0", text="Picking up where the limit stopped us")
    log.add("m13", "turn.completed", state="complete")
    log.add("m7", "accepted")
    log.add("m7", "tool.started", id="agent-1", name="Agent", summary="clean the mock tests")
    steps = [{"receipts": receipts}, log.page()]
    card = ask(log, "m7", "perm-rm")
    steps += [log.page(), {"receipts": [receipt("m7", 7, "approval-needed")]}]
    return steps, card


def test_the_incident_card_is_behind_review_and_scrolled_to(core_probe):
    log = Log()
    steps, card = incident(log)
    # The main agent kept working while the sub-agent's Bash waited (02:05Z).
    log.add("m7", "text", block="1", text="Meanwhile, reading the rest.")
    log.add("m7", "tool.started", id="read-1", name="Read", summary="file_path: README.md")
    steps.append(log.page())
    result = fold(core_probe, steps)

    assert result["turns"]["m7"]["state"] == "approval-needed"
    assert result["pending_items"] == [card]
    # The strip: the live turn, its words, and a Review that opens the card.
    assert result["pinned_turn"] == "m7" and result["live_message"] == "m7"
    assert result["turns"]["m7"]["status_text"] == "Needs your approval"
    assert result["review_label"] == "Review"
    # The failover turn shows under the message it continued, where it ran;
    # the queued messages come after the turn they wait behind.
    assert result["display_order"] == ["m6", "m13", "m7", "m8", "m9", "m10", "m11", "m12"]
    positions = person_positions(result)
    card_at = [item["id"] for item in result["items"]].index(card)
    assert all(positions[f"m{n}"] > card_at for n in range(8, 13))
    assert positions["m13"] < positions["m7"]
    # The view follows the live turn's newest row, not the last queued bubble.
    assert result["followed_item"] == "tool:m7:read-1"
    assert result["items"][-1]["id"] == "person:m12"
    # The card was scrolled to once, on the page that brought it.
    assert result["scrolls"] == [None, None, card, None, None]


def test_the_incident_before_the_fix_order_is_what_the_person_saw(core_probe):
    """Sequence order alone put the finished failover turn last: the bottom of the
    view showed its unfinished thinking, and the card sat above five bubbles."""
    log = Log()
    steps, _ = incident(log)
    result = fold(core_probe, steps)
    assert result["order"] == ["m6", "m7", "m8", "m9", "m10", "m11", "m12", "m13"]
    assert result["display_order"] != result["order"]


def test_review_opens_the_oldest_card_and_moves_on_when_it_is_answered(core_probe):
    log = Log()
    log.add("m1", "accepted")
    first, second = ask(log, "m1", "perm-a", "rm a"), ask(log, "m1", "perm-b", "rm b")
    steps = [{"receipts": [receipt("m1", 1, "approval-needed")]}, log.page()]
    both = fold(core_probe, steps)
    assert both["pending_items"] == [first, second] and both["review_label"] == "Review (2)"
    log.add("m1", "approval.resolved", request_id="perm-a", decision="allow")
    steps.append(log.page())
    one = fold(core_probe, steps)
    assert one["pending_items"] == [second] and one["review_label"] == "Review"
    log.add("m1", "approval.resolved", request_id="perm-b", decision="deny")
    steps.append(log.page())
    none = fold(core_probe, steps)
    assert none["pending_items"] == [] and none["review_label"] is None
    assert none["turns"]["m1"]["state"] == "running" and none["pinned_turn"] == "m1"


def test_a_card_known_from_approval_list_first_is_ordered_by_when_it_was_asked(core_probe):
    """`approval.list` can hand the app a card before its event; it is older than
    one asked later, wherever the fold appended it."""
    log = Log()
    log.add("m1", "accepted")
    later = ask(log, "m1", "perm-late", "rm late")
    early = {"approval_id": "ap-early", "message_id": "m1", "conversation_id": CID, "kind": "tool",
             "display": {"tool": "Bash", "input": "command: rm early"}, "options": ["allow", "deny"],
             "created_at": ts(0.5), "state": "pending"}
    result = fold(core_probe, [{"receipts": [receipt("m1", 1, "approval-needed")]}, log.page(),
                               {"approvals": [early]}])
    assert result["pending_items"] == ["approval:m1:ap-early", later]


def test_every_pending_card_has_a_strip_even_without_a_live_turn(core_probe):
    """A receipt can say a turn ended before its events withdraw the card; the
    card stays pending, so the strip stays to carry its Review."""
    log = Log()
    log.add("m1", "accepted")
    card = ask(log, "m1", "perm-x")
    result = fold(core_probe, [{"receipts": [receipt("m1", 1, "complete")]}, log.page()])
    assert result["live_message"] is None
    assert result["pending_items"] == [card] and result["pinned_turn"] == "m1"
    assert result["review_label"] == "Review"


def test_no_review_without_a_pending_card(core_probe):
    log = Log()
    log.add("m1", "accepted")
    log.add("m1", "text", block="0", text="Thinking about it")
    result = fold(core_probe, [{"receipts": [receipt("m1", 1, "running")]}, log.page()])
    assert result["pinned_turn"] == "m1" and result["pending_items"] == []
    assert result["review_label"] is None


def test_scrolls_once_per_card_again_on_request_and_after_visiting_elsewhere(core_probe):
    log = Log()
    log.add("m1", "accepted")
    steps = [{"receipts": [receipt("m1", 1, "running")]}, log.page()]
    first = ask(log, "m1", "perm-1")
    steps.append(log.page())
    log.add("m1", "text", block="0", text="still going")
    steps.append(log.page())
    second = ask(log, "m1", "perm-2")
    steps += [log.page(), {"reveal": True}, {}, {"elsewhere": True}, {}]
    result = fold(core_probe, steps)
    assert result["scrolls"] == [None, None, first, None, second, first, None, None, first]


def test_a_queued_message_moves_up_when_its_turn_starts(core_probe):
    log = Log()
    log.add("m1", "accepted")
    steps = [{"receipts": [receipt("m1", 1, "running"), receipt("m2", 2, "queued")]}, log.page()]
    queued = fold(core_probe, steps)
    assert queued["display_order"] == ["m1", "m2"] and queued["followed_item"] == "person:m1"
    log.add("m1", "turn.completed", state="complete")
    log.add("m2", "accepted")
    log.add("m2", "text", block="0", text="On it")
    started = fold(core_probe, steps + [{"receipts": [receipt("m1", 1, "complete"), receipt("m2", 2, "running")]},
                                        log.page()])
    assert started["display_order"] == ["m1", "m2"] and started["followed_item"] == "text:m2:0"


def test_the_queue_shows_in_the_order_the_daemon_sends_it(core_probe):
    """A `leave` note is sent ahead of the queued person messages (C-24.8)."""
    log = Log()
    log.add("m1", "accepted")
    log.add("m1", "turn.completed", state="interrupted")
    result = fold(core_probe, [{"receipts": [receipt("m1", 1, "interrupted"), receipt("m2", 2, "queued"),
                                             receipt("m3", 3, "queued", origin="unblock-note")]}, log.page()])
    assert result["display_order"] == ["m1", "m3", "m2"]
    assert [item["type"] for item in result["items"] if item["message_id"] == "m3"] == ["notice"]


def test_a_local_message_not_yet_received_waits_with_the_queue(core_probe):
    log = Log()
    log.add("m1", "accepted")
    log.add("m1", "text", block="0", text="Working")
    result = fold(core_probe, [{"local": {"message_id": "m-local", "text": "and then this"}},
                               {"receipts": [receipt("m1", 1, "running")]}, log.page()])
    assert result["display_order"] == ["m1", "m-local"]
    assert result["followed_item"] == "text:m1:0"
    assert result["items"][-1]["id"] == "person:m-local"


def test_real_driver_two_requests_in_one_turn(core_probe, tmp_path):
    """Two requests the real Claude driver records (a sub-agent's and the main
    agent's) are both behind the strip's Review, oldest first."""
    harness = ServiceHarness(Path(tempfile.mkdtemp(prefix="sf-reach-", dir="/tmp")))
    try:
        cid = harness.create()["conversation_id"]
        mid = harness.submit(cid, "clean up")["message_id"]
        later = harness.submit(cid, "then this", after=mid)["message_id"]
        turn = harness.attempt(cid, mid)
        turn.feed(claude_init(), replay(mid))
        claude_approval(turn, mid, "perm-sub", tool_input={"command": "rm -f mocktest/*"})
        claude_approval(turn, mid, "perm-main", tool_input={"command": "git status"})
        opened = harness.call("conversation.open", conversation_id=cid)
        result = run_probe(core_probe, "fold", write_json(tmp_path / "fold.json", {"conversation_id": cid, "steps": [
            {"receipts": opened["messages"]}, {"approvals": opened["pending_approvals"]}, {"page": page(harness, cid)},
        ]}))
    finally:
        harness.close()
    assert result["turns"][mid]["state"] == "approval-needed"
    assert result["turns"][later]["state"] == "queued"
    cards = {item["id"]: item["card"] for item in items_of(result, mid, "approval")}
    assert [cards[row]["request_id"] for row in result["pending_items"]] == ["perm-sub", "perm-main"]
    assert all(cards[row]["approval_id"] for row in result["pending_items"])
    assert result["review_label"] == "Review (2)" and result["pinned_turn"] == mid
    assert result["display_order"] == [mid, later]
    # Known from `conversation.open` first: scrolled to then, not again when the events join them.
    assert result["scrolls"] == [None, result["pending_items"][0], None]


# MARK: - Properties over generated timelines

STATES = ["queued", "waiting", "starting", "running", "approval-needed", "complete", "failed", "interrupted",
          "cancelled", "delivery-unknown"]


@st.composite
def timelines(draw):
    """Steps for a conversation of up to 7 messages: receipts in any order and at
    any time, events interleaved across messages, approvals known from
    `approval.list` before or after their events, local messages with no receipt."""
    count = draw(st.integers(1, 7))
    mids = [f"m{n}" for n in range(1, count + 1)]
    received = [mid for mid in mids if draw(st.booleans()) or mid == "m1"]
    receipts = []
    for index, mid in enumerate(mids):
        if mid not in received:
            continue
        continues, origin = None, draw(st.sampled_from(["person"] * 5 + ["unblock-note"]))
        if index and draw(st.integers(0, 3)) == 0:
            continues, origin = draw(st.sampled_from(mids)), "failover"
        receipts.append(receipt(mid, index + 1, draw(st.sampled_from(STATES)), origin=origin, continues=continues))
    log, steps, asked, attached = Log(), [], [], []
    for _ in range(draw(st.integers(0, 14))):
        mid = draw(st.sampled_from(mids))
        action = draw(st.sampled_from(["text", "tool", "ask", "ask", "answer", "complete", "accepted",
                                       "page", "receipts", "list", "reveal", "elsewhere"]))
        if action == "text":
            log.add(mid, "text", block=str(len(log.events)), text="words")
        elif action == "tool":
            log.add(mid, "tool.started", id=f"t{len(log.events)}", name="Bash", summary="ls")
        elif action == "ask":
            request = f"r{len(asked)}"
            asked.append((mid, request))
            ask(log, mid, request, command=f"cmd {request}")
            if draw(st.booleans()):
                # `approval.list` knows it too, stamped a little before or after the event.
                attached.append({"approval_id": f"ap-{request}", "message_id": mid, "conversation_id": CID,
                                 "kind": "tool", "display": {"tool": "Bash", "input": f"command: cmd {request}"},
                                 "options": ["allow", "deny", "cancel-turn"],
                                 "created_at": ts(len(log.events) + draw(st.sampled_from([-3, -0.5, 0.5, 3]))),
                                 "state": "pending"})
        elif action == "answer" and asked:
            owner, request = draw(st.sampled_from(asked))
            log.add(owner, "approval.resolved", request_id=request, decision="allow")
        elif action == "complete":
            log.add(mid, "turn.completed", state="complete")
        elif action == "accepted":
            log.add(mid, "accepted")
        elif action == "page":
            steps.append(log.page())
        elif action == "receipts" and receipts:
            steps.append({"receipts": draw(st.permutations(receipts))})
        elif action == "list" and attached:
            steps.append({"approvals": list(attached)})
        elif action == "reveal":
            steps.append({"reveal": True})
        elif action == "elsewhere":
            steps.append({"elsewhere": True})
    steps += [{"receipts": receipts}, log.page()]
    for mid in mids:
        if mid not in received and draw(st.booleans()):
            steps.insert(draw(st.integers(0, len(steps))), {"local": {"message_id": mid, "text": f"local {mid}"}})
    for step in steps:
        step["snapshot"] = True
    return steps


PROPERTY = settings(max_examples=120, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


def waits_in_queue(snapshot: dict, mid: str) -> bool:
    turn = snapshot["turns"][mid]
    return (turn["state"] in WAITING and turn["continues"] is None
            and not [item for item in snapshot["items"] if item["message_id"] == mid and item["id"] != f"person:{mid}"])


REPAIR_ORIGINS = ("unblock-note", "failover")      # subfleet/conversations/store.py


def reference_display_order(snapshot: dict) -> list[str]:
    """The rule, written again: sequence order; a continuation under the earlier
    message it continues (and its earlier continuations); queued messages last,
    in the order the daemon sends them (`next_dispatchable`: repairs first)."""
    order = snapshot["order"]
    queue = [mid for mid in order if waits_in_queue(snapshot, mid)]
    waiting = sorted(queue, key=lambda mid: snapshot["turns"][mid]["origin"] not in REPAIR_ORIGINS)
    started = [mid for mid in order if mid not in waiting]
    under: dict[str, list[str]] = {}
    roots = []
    for mid in started:
        parent = snapshot["turns"][mid]["continues"]
        if parent in started and order.index(parent) < order.index(mid):
            under.setdefault(parent, []).append(mid)
        else:
            roots.append(mid)
    out: list[str] = []

    def place(mid: str) -> None:
        out.append(mid)
        for child in under.get(mid, []):
            place(child)

    for mid in roots:
        place(mid)
    return out + waiting


def when(stamp: str | None) -> datetime:
    return datetime.fromisoformat(stamp) if stamp else T0


def pending_rows(snapshot: dict) -> list[str]:
    return [item["id"] for item in snapshot["items"]
            if item["type"] == "approval" and item["card"]["state"] == "pending"]


def check_snapshot(snapshot: dict) -> None:
    pending = snapshot["pending_items"]
    ids = [item["id"] for item in snapshot["items"]]
    # Every row is shown once (SwiftUI's ForEach needs unique ids), every message once.
    assert len(ids) == len(set(ids))
    assert sorted(snapshot["display_order"]) == sorted(snapshot["order"])
    assert snapshot["display_order"] == reference_display_order(snapshot)
    # The strip's Review: exactly while a card waits, counting every one, oldest
    # asked first (a row with no time after those with one; a tie keeps its place).
    rows = pending_rows(snapshot)
    stamped = {item["id"]: item["ts"] for item in snapshot["items"]}
    assert pending == sorted(rows, key=lambda row: (stamped[row] is None, when(stamped[row])))
    assert len(pending) == len(set(pending))
    assert snapshot["review_label"] == (None if not pending else "Review" if len(pending) == 1
                                        else f"Review ({len(pending)})")
    # A waiting card always has a strip to carry its Review.
    if pending:
        assert snapshot["pinned_turn"] is not None
    if snapshot["live_message"] is not None and snapshot["turns"][snapshot["live_message"]]["outcome"] is None:
        assert snapshot["pinned_turn"] == snapshot["live_message"]
    # Nothing a started turn did sits below a queued message.
    waiting = [mid for mid in snapshot["order"] if waits_in_queue(snapshot, mid)]
    first_waiting = min((ids.index(f"person:{mid}") for mid in waiting if f"person:{mid}" in ids), default=len(ids))
    assert all(item["message_id"] in waiting for item in snapshot["items"][first_waiting:])
    # The view follows the newest row that is not a queued message.
    started_rows = [item["id"] for item in snapshot["items"][:first_waiting]]
    assert snapshot["followed_item"] == (started_rows[-1] if started_rows else None)


@PROPERTY
@given(steps=timelines())
def test_property_the_strip_review_order_and_follow_hold_for_every_timeline(core_probe, steps):
    result = fold(core_probe, steps)
    for snapshot in result["snapshots"]:
        check_snapshot(snapshot)
    # What the generated timelines exercised, in the statistics.
    event(f"pending cards at the end: {min(len(result['pending_items']), 3)}")
    event(f"display order differs from sequence order: {result['display_order'] != result['order']}")
    event(f"a queued message below a started turn: {any(waits_in_queue(result, m) for m in result['order'])}")


@PROPERTY
@given(steps=timelines())
def test_property_each_card_is_scrolled_to_once_and_on_request(core_probe, steps):
    result = fold(core_probe, steps)
    shown: set[str] = set()
    for step, snapshot, target in zip(steps, result["snapshots"], result["scrolls"]):
        if step.get("elsewhere"):
            assert target is None
            shown = set()
            continue
        pending = snapshot["pending_items"]
        if step.get("reveal"):
            assert target == (pending[0] if pending else None)
        else:
            assert target == next((row for row in pending if row not in shown), None)
        assert target is None or target in [item["id"] for item in snapshot["items"]]
        shown |= set(pending)


@PROPERTY
@given(steps=timelines())
def test_property_answering_the_strips_card_reaches_every_pending_card(core_probe, steps):
    """Review opens the oldest card; once it is answered Review opens the next.
    Following Review alone answers every card that was pending, in order."""
    result = fold(core_probe, steps)
    pending = result["pending_items"]
    cards = {item["id"]: item["card"] for item in result["items"] if item["type"] == "approval"}
    reached = []
    next_seq = max((event["seq"] for step in steps if "page" in step for event in step["page"]["events"]), default=0)
    extra: list[dict] = []
    for _ in range(len(pending) + 1):
        current = fold(core_probe, steps + extra) if extra else result
        if not current["pending_items"]:
            break
        target = current["pending_items"][0]
        assert current["review_label"] is not None and current["pinned_turn"] is not None
        reached.append(target)
        card = cards[target]
        if card["request_id"] is not None:
            next_seq += 1
            extra.append({"page": {"events": [{"seq": next_seq, "message_id": target.split(":")[1],
                                               "kind": "approval.resolved", "ts": ts(next_seq),
                                               "data": {"request_id": card["request_id"], "decision": "allow"}}],
                                   "next": next_seq, "reset": False}})
        else:
            extra.append({"approvals": [{"approval_id": card["approval_id"], "message_id": target.split(":")[1],
                                         "conversation_id": CID, "kind": card["kind"], "display": card["display"],
                                         "options": card["options"], "created_at": ts(0), "state": "answered"}]})
    assert reached == pending
