"""A pending approval is always within reach (C-27.5; design §12; queue order
C-24.8, C-26.7).

On 2026-09-27 a turn waited on a card nobody saw: the timeline put the card
under its own message, above five queued messages and a failover turn that
sorted last although it ran first, and the pinned strip said "Needs your
approval" with only a Stop button. These tests pin what the conversation view
reads from the timeline:

- the strip's Review exists exactly while a card is pending, counts every
  pending card, and opens the oldest; answering it moves Review to the next, so
  every pending card is reachable from the strip without scrolling;
- whenever a card is pending there is a strip to carry the Review, and a card
  whose message has ended is not pending;
- turns show in the order they began, and a message still queued shows below
  every turn that has begun, in the order the daemon sends the queue;
- the view follows the newest row of the turn that began last;
- once the conversation's log is read, each new card is scrolled to once, the
  oldest again when the conversation opens or the person asks, and once more
  when the first page of older history lands above it.

The example tests replay the incident and the reviews' cases; the property tests
check the same statements over generated timelines, and compare the display
order with a reference written from the rule above.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import uuid

from hypothesis import HealthCheck, event, given, settings, strategies as st

from subfleet.conversations.store import REPAIR_ORIGINS
from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import ServiceHarness, claude_init
from tests.frontend.test_core_timeline import claude_approval, items_of, page, replay

pytestmark = needs_swift

CID = "cv-approval-reach"
T0 = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)
WAITING = {"queued", "sending"}
TERMINAL = {"complete", "failed", "interrupted", "cancelled"}
# A message settled this way has had its attempt's approvals withdrawn (service.py settle).
SETTLED = TERMINAL | {"delivery-unknown"}
CONVERSATION_KEY = "(conversation)"


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
    """A synthetic `conversation.events` log, handed out as pages. A page with
    nothing new is how the app learns it has read the log to its end."""

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


def listed(approval_id: str, mid: str, command: str, created: float, request_id: str | None = None) -> dict:
    """An approval as `approval.list` shows it (pending only). A daemon older than
    C-27.5 sends no `request_id`."""
    view = {"approval_id": approval_id, "message_id": mid, "conversation_id": CID, "kind": "tool",
            "display": {"tool": "Bash", "input": f"command: {command}"}, "options": ["allow", "deny", "cancel-turn"],
            "created_at": ts(created), "state": "pending"}
    if request_id is not None:
        view["request_id"] = request_id
    return view


def history(*texts: str, next_before: int | None = None) -> dict:
    """A page of the native transcript from before the first event."""
    return {"history": {"items": [{"role": "assistant", "kind": "text", "text": text, "ts": ts(-3600 + i),
                                   "cursor": 100 + i} for i, text in enumerate(texts)],
                        "next_before": next_before}}


def person_positions(result: dict) -> dict[str, int]:
    return {item["message_id"]: index for index, item in enumerate(result["items"])
            if item["id"] == f"person:{item['message_id']}"}


# MARK: - The 2026-09-27 conversation


def incident(log: Log) -> tuple[list[dict], str]:
    """Messages 6 to 13 of cv-1790290856733-385ce6acfca0 as they stood at 01:58:52Z:
    6 hit a usage limit, 13 continued it (and ran), 7 asked for approval, 8 to 12
    were queued behind 7. The app had read the log to its end before the card."""
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
    steps = [{"receipts": receipts}, log.page(), log.page()]
    card = ask(log, "m7", "perm-rm")
    steps += [log.page(), {"receipts": [receipt("m7", 7, "approval-needed")]}]
    return steps, card


def test_c27_5_the_incident_card_is_behind_review_and_scrolled_to(core_probe):
    """C-27.5 on the incident: Review on the strip, the card in view, the live turn followed."""
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
    # The failover turn shows where it ran, before 7; the queued messages come
    # after the turn they wait behind.
    assert result["display_order"] == ["m6", "m13", "m7", "m8", "m9", "m10", "m11", "m12"]
    positions = person_positions(result)
    card_at = [item["id"] for item in result["items"]].index(card)
    assert all(positions[f"m{n}"] > card_at for n in range(8, 13))
    assert positions["m13"] < positions["m7"]
    # The view follows the live turn's newest row, not the last queued bubble.
    assert result["followed_item"] == "tool:m7:read-1"
    assert result["items"][-1]["id"] == "person:m12"
    # Scrolled to once, on the page that brought it (the log had been read).
    assert result["scrolls"] == [None, None, None, card, None, None]


def test_c27_5_the_incident_in_sequence_order_is_what_the_person_saw(core_probe):
    """Sequence order alone put the finished failover turn last: the bottom of the
    view showed its unfinished thinking, and the card sat above five bubbles."""
    log = Log()
    steps, _ = incident(log)
    result = fold(core_probe, steps)
    assert result["order"] == ["m6", "m7", "m8", "m9", "m10", "m11", "m12", "m13"]
    assert result["display_order"] != result["order"]


def test_c27_5_withdrawing_a_queued_message_keeps_the_live_turn_followed(core_probe):
    """A message withdrawn from the queue while a turn streams never began; the
    view keeps following the live turn (review of 6e1b505)."""
    log = Log()
    steps, _ = incident(log)
    steps.append({"receipts": [receipt("m9", 9, "cancelled")]})
    log.add("m7", "text", block="1", text="Still reading")
    steps.append(log.page())
    result = fold(core_probe, steps)
    assert result["display_order"] == ["m6", "m13", "m7", "m9", "m8", "m10", "m11", "m12"]
    assert result["followed_item"] == "text:m7:1"


# MARK: - The strip's Review


def test_c27_5_review_opens_the_oldest_card_and_moves_on_when_it_is_answered(core_probe):
    """C-27.5: Review (N) counts every card; answering the oldest moves Review to the next."""
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


def test_c27_5_a_card_known_from_approval_list_first_is_ordered_by_when_it_was_asked(core_probe):
    """`approval.list` can hand the app a card before its event; it is older than
    one asked later, wherever the fold appended it."""
    log = Log()
    log.add("m1", "accepted")
    later = ask(log, "m1", "perm-late", "rm late")
    result = fold(core_probe, [{"receipts": [receipt("m1", 1, "approval-needed")]}, log.page(),
                               {"approvals": [listed("ap-early", "m1", "rm early", 0.5)]}])
    assert result["pending_items"] == ["approval:m1:ap-early", later]


def test_c27_5_a_listed_card_moves_to_where_its_request_came(core_probe):
    """Joined to its event, a card `approval.list` made first sits below what the
    turn did before asking (review of 6e1b505)."""
    log = Log()
    log.add("m1", "accepted")
    log.add("m1", "text", block="0", text="Let me clean up.")
    ask(log, "m1", "perm-j", "rm j")
    result = fold(core_probe, [{"receipts": [receipt("m1", 1, "approval-needed")]},
                               {"approvals": [listed("ap-j", "m1", "rm j", 3)]}, log.page()])
    assert [item["type"] for item in items_of(result, "m1")] == ["person", "text", "approval"]
    card = items_of(result, "m1", "approval")[0]["card"]
    assert card["approval_id"] == "ap-j" and card["request_id"] == "perm-j"


def test_c27_5_a_card_of_an_ended_turn_is_withdrawn(core_probe):
    """A turn that ends without `result` writes no event withdrawing its request,
    but the daemon withdraws it before settling the message (C-27.3): the card is
    not pending, and neither the strip nor a later turn offers it (review of 6e1b505)."""
    log = Log()
    log.add("m1", "accepted")
    card = ask(log, "m1", "perm-dead")
    log.add("m1", "status", phase="stopping")
    ended = [{"receipts": [receipt("m1", 1, "interrupted")]}, log.page()]
    result = fold(core_probe, ended)
    assert items_of(result, "m1", "approval")[0]["card"]["state"] == "withdrawn"
    assert result["pending_items"] == [] and result["review_label"] is None and result["pinned_turn"] is None
    # The events first, the ended receipt after: withdrawn either way.
    assert fold(core_probe, ended[::-1])["pending_items"] == []
    log.add("m2", "accepted")
    later = fold(core_probe, ended + [{"receipts": [receipt("m2", 2, "running")]}, log.page()])
    assert later["pinned_turn"] == "m2" and later["review_label"] is None and card not in later["pending_items"]


def test_c27_5_a_waiting_card_has_the_strip_when_the_newest_live_turn_has_answered(core_probe):
    """The strip falls back to the turn of the oldest pending card, so a card
    waiting on the person always has Review."""
    log = Log()
    log.add("m1", "accepted")
    card = ask(log, "m1", "perm-x")
    log.add("m2", "accepted")
    log.add("m2", "turn.completed", state="complete")
    result = fold(core_probe, [{"receipts": [receipt("m1", 1, "approval-needed"), receipt("m2", 2, "running")]},
                               log.page()])
    assert result["live_message"] == "m2" and result["turns"]["m2"]["outcome"] is not None
    assert result["pinned_turn"] == "m1" and result["pending_items"] == [card]
    assert result["review_label"] == "Review"


def test_c27_5_no_review_without_a_pending_card(core_probe):
    """C-27.5: a live turn with no card has a strip and no Review."""
    log = Log()
    log.add("m1", "accepted")
    log.add("m1", "text", block="0", text="Thinking about it")
    result = fold(core_probe, [{"receipts": [receipt("m1", 1, "running")]}, log.page()])
    assert result["pinned_turn"] == "m1" and result["pending_items"] == []
    assert result["review_label"] is None


# MARK: - Scrolling to a card


def test_c27_5_scrolls_once_per_card_again_on_request_and_on_opening(core_probe):
    """C-27.5: each new card once; the oldest on request and when the conversation opens again."""
    log = Log()
    log.add("m1", "accepted")
    steps = [{"receipts": [receipt("m1", 1, "running")]}, log.page(), log.page()]
    first = ask(log, "m1", "perm-1")
    steps.append(log.page())
    log.add("m1", "text", block="0", text="still going")
    steps.append(log.page())
    second = ask(log, "m1", "perm-2")
    steps += [log.page(), {"reveal": True}, {}, {"elsewhere": True}, {}]
    result = fold(core_probe, steps)
    assert result["scrolls"] == [None, None, None, first, None, second, first, None, None, first]


def test_c27_5_opening_waits_for_the_log_then_the_first_history_page(core_probe):
    """Opening a conversation from its badge: the card `conversation.open` lists
    is scrolled to once the log has been read, since rows arriving above it move
    it; and again when the first page of older history lands above it, not when
    the person pages further back (review of 6e1b505)."""
    log = Log()
    log.add("m1", "accepted")
    log.add("m1", "text", block="0", text="Let me clean up.")
    card_row = "approval:m1:ap-1"
    ask(log, "m1", "perm-1", "rm one")
    steps = [{"receipts": [receipt("m1", 1, "approval-needed")]},
             {"approvals": [listed("ap-1", "m1", "rm one", 3)], "reveal": True},
             log.page(), log.page(), history("before", next_before=50), history("even earlier"), {}]
    result = fold(core_probe, steps)
    assert result["results"] == ["receipts", "approvals", "applied:3", "applied:0", "history", "history"]
    assert result["scrolls"] == [None, None, None, card_row, card_row, None, None]
    assert result["caught_up"] is True and result["history_pages"] == 2


def test_c27_5_a_reset_reads_the_log_again_before_scrolling(core_probe):
    """A reset (C-25.4) drops what the events made and reads the log from 0: rows
    arrive again above the card, so the view waits until that read reaches the end.
    The person's request stands until then."""
    log = Log()
    log.add("m1", "accepted")
    card = ask(log, "m1", "perm-r")
    first = log.page()
    steps = [{"receipts": [receipt("m1", 1, "approval-needed")]}, first, log.page(),
             {"page": {"events": [], "next": 0, "reset": True, "floor": 1}, "reveal": True},
             {"page": {"events": first["page"]["events"], "next": 2, "reset": False}},
             {"page": {"events": [], "next": 2, "reset": False}}]
    for step in steps:
        step["snapshot"] = True
    result = fold(core_probe, steps)
    assert result["results"] == ["receipts", "applied:2", "applied:0", "reset", "applied:2", "applied:0"]
    assert [snapshot["caught_up"] for snapshot in result["snapshots"]] == [False, False, True, False, False, True]
    assert result["scrolls"] == [None, None, card, None, None, card]


def test_c27_5_opening_again_reads_before_scrolling_and_an_empty_history_page_moves_nothing(core_probe):
    """Opening a conversation again reads its log from where it stopped, and the
    next older history page lands above the card: the view waits for the read and
    brings the card back after that page, but not after a page that added nothing
    (independent review of fa30c51)."""
    log = Log()
    log.add("m1", "accepted")
    card = ask(log, "m1", "perm-v")
    visit = [{"receipts": [receipt("m1", 1, "approval-needed")]}, log.page(), log.page()]
    steps = visit + [{"elsewhere": True}, {"reopen": True}, log.page(), history("older", next_before=40),
                     history("oldest")]
    result = fold(core_probe, steps)
    assert result["scrolls"] == [None, None, card, None, None, card, card, None]
    quiet = fold(core_probe, visit + [{"elsewhere": True}, {"reopen": True}, log.page(), history()])
    assert quiet["scrolls"][-1] is None


def test_c27_5_two_identical_requests_join_their_own_approvals(core_probe):
    """One turn asks for `git status` twice; the first is answered, the second
    waits, and the app opens the conversation afresh. With the request id on the
    daemon's views each card joins its own approval; from an older daemon, whose
    views join by display, a card that is not pending lets go of an approval the
    daemon says is pending, so Review still reaches the waiting one (independent
    review of fa30c51)."""
    for request_ids in (True, False):
        log = Log()
        log.add("m1", "accepted")
        ask(log, "m1", "r1", "git status")
        log.add("m1", "approval.resolved", request_id="r1", decision="allow")
        waiting = ask(log, "m1", "r2", "git status")
        view = listed("ap2", "m1", "git status", 3, request_id="r2" if request_ids else None)
        steps = [{"receipts": [receipt("m1", 1, "approval-needed")]}, {"pending": [view]}, log.page(), log.page(),
                 {"pending": [view]}]
        result = fold(core_probe, steps)
        pending = [item for item in result["items"] if item["id"] in result["pending_items"]]
        assert len(pending) == 1 and pending[0]["card"]["approval_id"] == "ap2", request_ids
        assert pending[0]["card"]["request_id"] == "r2", request_ids
        answered = [item["card"] for item in items_of(result, "m1", "approval") if item["card"]["state"] != "pending"]
        assert [card["request_id"] for card in answered] == ["r1"], request_ids
        if request_ids:
            assert result["pending_items"] == ["approval:m1:ap2"] or result["pending_items"] == [waiting]


def test_c27_5_a_view_joins_the_card_of_its_own_request_in_any_order(core_probe):
    """Two identical requests wait; views that name their request join their own
    cards whatever order they arrive in, where a join by display pairs them by order."""
    log = Log()
    log.add("m1", "accepted")
    first, second = ask(log, "m1", "r1", "git status"), ask(log, "m1", "r2", "git status")
    result = fold(core_probe, [{"receipts": [receipt("m1", 1, "approval-needed")]}, log.page(),
                               {"approvals": [listed("ap2", "m1", "git status", 2, request_id="r2"),
                                              listed("ap1", "m1", "git status", 1, request_id="r1")]}])
    cards = {item["id"]: item["card"] for item in items_of(result, "m1", "approval")}
    assert (cards[first]["approval_id"], cards[second]["approval_id"]) == ("ap1", "ap2")


def test_c27_5_a_card_the_daemon_no_longer_lists_is_withdrawn(core_probe):
    """A turn admitted again, or one whose attempt ended with no event, leaves a
    card the daemon no longer lists as pending: the next full pending list (a
    Review, an answer, a watch count below the cards shown) withdraws it."""
    log = Log()
    log.add("m1", "accepted")
    ask(log, "m1", "perm-gone", "rm gone")
    steps = [{"receipts": [receipt("m1", 1, "waiting")]}, log.page(),
             {"pending": [listed("ap-gone", "m1", "rm gone", 2, request_id="perm-gone")]}]
    joined = fold(core_probe, steps)
    assert joined["review_label"] == "Review"
    gone = fold(core_probe, steps + [{"pending": []}])
    assert gone["pending_items"] == [] and gone["review_label"] is None
    assert items_of(gone, "m1", "approval")[0]["card"]["state"] == "withdrawn"


def test_c27_5_a_card_of_a_message_whose_delivery_is_unknown_is_withdrawn(core_probe):
    """Delivery unknown is settled like an end: its attempt's approvals are gone."""
    log = Log()
    log.add("m1", "accepted")
    ask(log, "m1", "perm-du")
    result = fold(core_probe, [{"receipts": [receipt("m1", 1, "delivery-unknown")]}, log.page()])
    assert result["pending_items"] == [] and result["pinned_turn"] is None


# MARK: - Display order


def test_c27_5_a_queued_message_moves_up_when_its_turn_starts(core_probe):
    """C-27.5: a queued bubble waits below the running turn, then takes its place."""
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


def test_c27_5_the_queue_shows_in_the_order_the_daemon_sends_it(core_probe):
    """A `leave` note is sent ahead of the queued person messages (C-24.8)."""
    log = Log()
    log.add("m1", "accepted")
    log.add("m1", "turn.completed", state="interrupted")
    result = fold(core_probe, [{"receipts": [receipt("m1", 1, "interrupted"), receipt("m2", 2, "queued"),
                                             receipt("m3", 3, "queued", origin="unblock-note")]}, log.page()])
    assert result["display_order"] == ["m1", "m3", "m2"]
    assert [item["type"] for item in result["items"] if item["message_id"] == "m3"] == ["notice"]


def test_c27_5_an_unblock_note_that_ran_first_stays_above_the_turn_after_it(core_probe):
    """The note runs before the queued person message (C-24.8); once that message's
    turn runs, the note stays where it ran and the new turn is followed (review
    of 6e1b505)."""
    log = Log()
    log.add("m1", "accepted")
    log.add("m1", "turn.completed", state="interrupted")
    log.add("m3", "accepted")
    log.add("m3", "text", block="0", text="Understood")
    log.add("m3", "turn.completed", state="complete")
    log.add("m2", "accepted")
    log.add("m2", "text", block="0", text="Starting fresh")
    card = ask(log, "m2", "perm-n")
    result = fold(core_probe, [{"receipts": [receipt("m1", 1, "interrupted"), receipt("m2", 2, "approval-needed"),
                                             receipt("m3", 3, "complete", origin="unblock-note")]}, log.page()])
    assert result["display_order"] == ["m1", "m3", "m2"]
    assert result["followed_item"] == card and result["pinned_turn"] == "m2"


def test_c27_5_a_local_message_not_yet_received_waits_with_the_queue(core_probe):
    """A message the daemon has not received yet sits with the queue."""
    log = Log()
    log.add("m1", "accepted")
    log.add("m1", "text", block="0", text="Working")
    result = fold(core_probe, [{"local": {"message_id": "m-local", "text": "and then this"}},
                               {"receipts": [receipt("m1", 1, "running")]}, log.page()])
    assert result["display_order"] == ["m1", "m-local"]
    assert result["followed_item"] == "text:m1:0"
    assert result["items"][-1]["id"] == "person:m-local"


def test_c27_5_real_driver_two_requests_in_one_turn(core_probe, tmp_path):
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
        events = page(harness, cid)
        result = run_probe(core_probe, "fold", write_json(tmp_path / "fold.json", {"conversation_id": cid, "steps": [
            {"receipts": opened["messages"]}, {"approvals": opened["pending_approvals"]}, {"page": events},
            {"page": page(harness, cid, after=events["next"])},
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
    # Scrolled to once the log has been read, not when `conversation.open` listed it.
    assert result["scrolls"] == [None, None, None, result["pending_items"][0]]


# MARK: - Properties over generated timelines

STATES = ["queued", "waiting", "starting", "running", "approval-needed", "complete", "failed", "interrupted",
          "cancelled", "delivery-unknown"]


@st.composite
def timelines(draw):
    """Steps for a conversation of up to 7 messages, and the daemon's pending
    approvals at the end. Receipts come in any order and at any time, events
    interleave across messages (a few with no message id), requests repeat a
    command (identical displays), the daemon's pending set is listed whole at
    any time, and views carry the request id or, as from an older daemon, not."""
    request_ids = draw(st.booleans())
    count = draw(st.integers(1, 7))
    mids = [f"m{n}" for n in range(1, count + 1)]
    received = [mid for mid in mids if draw(st.booleans()) or mid == "m1"]
    receipts, final = [], {}
    for index, mid in enumerate(mids):
        if mid not in received:
            continue
        continues, origin = None, draw(st.sampled_from(["person"] * 5 + ["unblock-note"]))
        if index and draw(st.integers(0, 3)) == 0:
            continues, origin = draw(st.sampled_from(mids)), "failover"
        # Live states twice as often, so cards stay pending in more timelines.
        final[mid] = draw(st.sampled_from(STATES + ["running", "approval-needed", "queued"]))
        receipts.append(receipt(mid, index + 1, final[mid], origin=origin, continues=continues))
    log, steps, truth = Log(), [], {}
    for _ in range(draw(st.integers(0, 16))):
        mid = draw(st.sampled_from(mids))
        action = draw(st.sampled_from(["text", "tool", "ask", "ask", "answer", "complete", "accepted", "loose",
                                       "page", "page", "receipts", "list", "reveal", "elsewhere", "history"]))
        if action == "text":
            log.add(mid, "text", block=str(len(log.events)), text="words")
        elif action == "loose":
            log.add(None, "text", block=str(len(log.events)), text="for the conversation")
        elif action == "tool":
            log.add(mid, "tool.started", id=f"t{len(log.events)}", name="Bash", summary="ls")
        elif action == "ask":
            request = f"r{len(truth)}"
            command = draw(st.sampled_from(["git status", "git status", "ls", f"cmd {request}"]))
            ask(log, mid, request, command=command)
            truth[f"ap-{request}"] = {"view": listed(f"ap-{request}", mid, command, len(log.events),
                                                     request_id=request if request_ids else None),
                                      "request": request, "state": "pending"}
        elif action == "answer" and truth:
            approval = truth[draw(st.sampled_from(sorted(truth)))]
            if approval["state"] == "pending":
                approval["state"] = "answered"
                log.add(approval["view"]["message_id"], "approval.resolved", request_id=approval["request"],
                        decision="allow")
        elif action == "complete":
            log.add(mid, "turn.completed", state="complete")
            for approval in truth.values():
                if approval["view"]["message_id"] == mid and approval["state"] == "pending":
                    approval["state"] = "withdrawn"
        elif action == "accepted":
            log.add(mid, "accepted")
        elif action == "page":
            steps.append(log.page())
        elif action == "receipts" and receipts:
            steps.append({"receipts": draw(st.permutations(receipts))})
        elif action == "list":
            steps.append({"pending": [a["view"] for a in truth.values() if a["state"] == "pending"]})
        elif action == "reveal":
            steps.append({"reveal": True})
        elif action == "elsewhere":
            steps.append({"elsewhere": True})
        elif action == "history":
            steps.append(history(f"older {len(steps)}", next_before=draw(st.sampled_from([None, 10]))))
    # A settled message's approvals are gone (service.py settle); the rest stay pending.
    for approval in truth.values():
        if approval["state"] == "pending" and final.get(approval["view"]["message_id"]) in SETTLED:
            approval["state"] = "withdrawn"
    # The last pages read the log to its end, as the app's events loop does; then the
    # daemon's whole pending set, as a Review or an answer reads it.
    steps += [{"receipts": receipts}, log.page(), log.page(),
              {"pending": [a["view"] for a in truth.values() if a["state"] == "pending"]}]
    for mid in mids:
        if mid not in received and draw(st.booleans()):
            steps.insert(draw(st.integers(0, len(steps))), {"local": {"message_id": mid, "text": f"local {mid}"}})
    for step in steps:
        step["snapshot"] = True
    return {"steps": steps, "request_ids": request_ids,
            "pending": {id: a for id, a in truth.items() if a["state"] == "pending"}}


PROPERTY = settings(max_examples=120, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


def rows_of(snapshot: dict, mid: str) -> list[str]:
    return [item["id"] for item in snapshot["items"] if item["message_id"] == mid]


def waits_in_queue(snapshot: dict, mid: str) -> bool:
    turn = snapshot["turns"][mid]
    return (mid != CONVERSATION_KEY and turn["state"] in WAITING and turn["continues"] is None
            and turn["first_event"] is None and rows_of(snapshot, mid) in ([], [f"person:{mid}"]))


def reference_display_order(snapshot: dict) -> list[str]:
    """The rule, written again: turns in the order they began (first event); a
    message that never began right after the latest that began among the
    messages before it; the no-message rows last of those; queued messages last,
    in the order the daemon sends them (`next_dispatchable`: repairs first)."""
    order, turns = snapshot["order"], snapshot["turns"]
    queue = [mid for mid in order if waits_in_queue(snapshot, mid)]
    waiting = sorted(queue, key=lambda mid: turns[mid]["origin"] not in REPAIR_ORIGINS)
    latest, keyed = 0, []
    for place, mid in enumerate(order):
        if mid in queue:
            continue
        began = turns[mid]["first_event"]
        if mid == CONVERSATION_KEY:
            keyed.append(((float("inf"), 1, place), mid))
        elif began is not None:
            keyed.append(((began, 0, place), mid))
            latest = max(latest, began)
        else:
            keyed.append(((latest, 1, place), mid))
    return [mid for _, mid in sorted(keyed)] + waiting


def reference_followed_item(snapshot: dict) -> str | None:
    """The newest row of the turn that began last; before any began, the last
    row above the queue."""
    begun = [mid for mid, turn in snapshot["turns"].items()
             if turn["first_event"] is not None and mid != CONVERSATION_KEY and rows_of(snapshot, mid)]
    if begun:
        return rows_of(snapshot, max(begun, key=lambda mid: snapshot["turns"][mid]["first_event"]))[-1]
    waiting = {mid for mid in snapshot["order"] if waits_in_queue(snapshot, mid)}
    above = [item["id"] for item in snapshot["items"] if item["message_id"] not in waiting]
    return above[-1] if above else None


def pending_rows(snapshot: dict) -> list[str]:
    return [item["id"] for item in snapshot["items"]
            if item["type"] == "approval" and item["card"]["state"] == "pending"]


def when(stamp: str | None) -> datetime:
    return datetime.fromisoformat(stamp) if stamp else T0


def check_snapshot(snapshot: dict) -> None:
    pending = snapshot["pending_items"]
    ids = [item["id"] for item in snapshot["items"]]
    turns, shown = snapshot["turns"], snapshot["display_order"]
    # Every row is shown once (SwiftUI's ForEach needs unique ids), every message once.
    assert len(ids) == len(set(ids))
    assert sorted(shown) == sorted(snapshot["order"])
    # The display rule, as statements...
    waiting = [mid for mid in snapshot["order"] if waits_in_queue(snapshot, mid)]
    assert shown[len(shown) - len(waiting):] == sorted(
        waiting, key=lambda mid: (turns[mid]["origin"] not in REPAIR_ORIGINS, snapshot["order"].index(mid)))
    begun = [mid for mid in shown if turns[mid]["first_event"] is not None and mid != CONVERSATION_KEY]
    assert begun == sorted(begun, key=lambda mid: turns[mid]["first_event"])
    for mid in shown:
        parent = turns[mid]["continues"]
        if parent in turns and turns[parent]["first_event"] is not None and parent != mid and (
                turns[mid]["first_event"] is None or turns[mid]["first_event"] > turns[parent]["first_event"]) \
                and snapshot["order"].index(parent) < snapshot["order"].index(mid):
            assert shown.index(parent) < shown.index(mid)       # a continuation under its origin
    # ...and as the rule written again.
    assert shown == reference_display_order(snapshot)
    # The strip's Review: exactly while a card waits, counting every one, oldest
    # asked first (a row with no time after those with one; a tie keeps its place).
    rows = pending_rows(snapshot)
    stamped = {item["id"]: item["ts"] for item in snapshot["items"]}
    assert pending == sorted(rows, key=lambda row: (stamped[row] is None, when(stamped[row])))
    assert len(pending) == len(set(pending))
    assert snapshot["review_label"] == (None if not pending else "Review" if len(pending) == 1
                                        else f"Review ({len(pending)})")
    # No card of a message the daemon has settled is pending (C-27.3).
    owners = {item["id"]: item["message_id"] for item in snapshot["items"]}
    assert all(turns[owners[row]]["state"] not in SETTLED for row in pending)
    # A waiting card always has a strip to carry its Review.
    if pending:
        assert snapshot["pinned_turn"] is not None
    if snapshot["live_message"] is not None and turns[snapshot["live_message"]]["outcome"] is None:
        assert snapshot["pinned_turn"] == snapshot["live_message"]
    # Nothing a turn did sits below a queued message.
    first_waiting = min((ids.index(f"person:{mid}") for mid in waiting if f"person:{mid}" in ids), default=len(ids))
    assert all(item["message_id"] in waiting for item in snapshot["items"][first_waiting:])
    assert snapshot["followed_item"] == reference_followed_item(snapshot)


@PROPERTY
@given(case=timelines())
def test_c27_5_property_the_strip_review_order_and_follow_hold_for_every_timeline(core_probe, case):
    """C-27.5 over generated timelines: Review, the strip, display order, the followed row."""
    result = fold(core_probe, case["steps"])
    for snapshot in result["snapshots"]:
        check_snapshot(snapshot)
    # What the generated timelines exercised, in the statistics.
    event(f"pending cards at the end: {min(len(result['pending_items']), 3)}")
    event(f"display order differs from sequence order: {result['display_order'] != result['order']}")
    event(f"a queued message below a turn: {any(waits_in_queue(result, m) for m in result['order'])}")
    event(f"identical requests: {len({json.dumps(a['view']['display']) for a in case['pending'].values()}) < len(case['pending'])}")


@PROPERTY
@given(case=timelines())
def test_c27_5_property_cards_join_the_daemons_pending_approvals(core_probe, case):
    """After the daemon's whole pending set, the pending cards are its approvals:
    each once, and, where views carry the request id, each on its own request."""
    result = fold(core_probe, case["steps"])
    cards = {item["id"]: item["card"] for item in result["items"] if item["type"] == "approval"}
    joined = [cards[row]["approval_id"] for row in result["pending_items"]]
    assert all(joined) and len(joined) == len(set(joined))
    assert set(joined) == set(case["pending"])
    if case["request_ids"]:
        for row in result["pending_items"]:
            assert cards[row]["request_id"] == case["pending"][cards[row]["approval_id"]]["request"]


@PROPERTY
@given(case=timelines())
def test_c27_5_property_each_card_is_scrolled_to_once_and_on_request(core_probe, case):
    """C-27.5: the follower's targets, against the rule written again."""
    steps = case["steps"]
    result = fold(core_probe, steps)
    conversation, shown, last, pages_at_open, loaded, reveal = None, set(), None, 0, False, False
    for step, snapshot, target in zip(steps, result["snapshots"], result["scrolls"]):
        reveal = reveal or bool(step.get("reveal"))
        here = "elsewhere" if step.get("elsewhere") else CID
        if here != conversation:
            conversation, shown, last, loaded = here, set(), None, False
            pages_at_open = 0 if step.get("elsewhere") else snapshot["history_pages"]
        if step.get("elsewhere") or not snapshot["caught_up"]:
            # Another conversation not yet read, or this one still reading: rows may arrive above.
            assert target is None
            continue
        pending = snapshot["pending_items"]
        expected = (pending[0] if pending else None) if reveal else \
            next((row for row in pending if row not in shown), None)
        shown |= set(pending)
        if not loaded and snapshot["history_pages"] > pages_at_open:
            loaded = True
            if expected is None and snapshot["history_added"] > 0 and last in pending:
                expected = last
        if expected:
            last, reveal = expected, False
        assert target == expected
        assert target is None or target in pending


@PROPERTY
@given(case=timelines())
def test_c27_5_property_answering_the_strips_card_reaches_every_pending_card(core_probe, case):
    """Review opens the oldest card; once it is answered Review opens the next.
    Following Review alone answers every approval the daemon holds pending, in order."""
    steps = case["steps"]
    result = fold(core_probe, steps)
    pending = result["pending_items"]
    cards = {item["id"]: item["card"] for item in result["items"] if item["type"] == "approval"}
    reached = []
    next_seq = max((e["seq"] for step in steps if "page" in step for e in step["page"]["events"]), default=0)
    extra: list[dict] = []
    for _ in range(len(pending) + 1):
        current = fold(core_probe, steps + extra) if extra else result
        if not current["pending_items"]:
            break
        target = current["pending_items"][0]
        assert current["review_label"] is not None and current["pinned_turn"] is not None
        card = cards[target]
        reached.append(card["approval_id"])
        # The daemon answers the approval the card holds; its event names the request.
        answered = case["pending"][card["approval_id"]]
        next_seq += 1
        extra.append({"page": {"events": [{"seq": next_seq, "message_id": answered["view"]["message_id"],
                                           "kind": "approval.resolved", "ts": ts(next_seq),
                                           "data": {"request_id": answered["request"], "decision": "allow"}}],
                               "next": next_seq, "reset": False}})
    assert sorted(reached) == sorted(case["pending"]) and len(reached) == len(set(reached))
