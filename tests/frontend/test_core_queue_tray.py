"""The queue tray above the composer (C-29.7; design §12; display order C-27.5).

Max, 2026-09-28: the conversation queue did not work the way the Claude app's
does. A message waiting its turn now leaves the scrolling timeline for a tray
pinned above the composer, in the order the daemon sends the queue, with
Withdraw (app/Sources/QueueTray.swift). These tests pin what the conversation
view reads from the timeline:

- a message waits in the tray when the daemon holds it (`queued`), or, before
  its receipt, when something is ahead of it: a live turn, a block holding the
  conversation, or an earlier message still waiting; only a send with nothing
  ahead shows in the timeline;
- the tray is in the order the daemon sends the queue (`next_dispatchable`:
  repair origins first, then sequence), a send with no receipt after those, in
  the order it was made; a failover continuation shows under the message it
  continues;
- a row's Withdraw is `message.cancel` for a message the daemon holds, the
  outbox's withdrawal for one with no receipt, and nothing for an unblock
  note; Steer is offered only on the head, a person's message the daemon
  holds, while a turn is live;
- a message withdrawn before it started reads as one line in the timeline;
- the view lands at the end with no scrolling while the conversation opens,
  always follows the person's own send, and follows anything else only while
  the end is on screen.

The example tests replay named cases, some with the daemon's own receipts; the
property tests compare the probe's layout and moves with references written
again from the rules above, over generated timelines.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import tempfile
import uuid

import pytest
from hypothesis import HealthCheck, event, given, settings, strategies as st

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import ServiceHarness

pytestmark = needs_swift

CID = "cv-queue-tray"
T0 = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
CONVERSATION_KEY = "(conversation)"
# The states `Timeline.liveMessageID` counts as a live turn.
LIVE = {"waiting", "starting", "running", "approval-needed"}
REPAIR_ORIGINS = ("unblock-note", "failover")      # subfleet/conversations/store.py
WITHDRAWN_REASONS = ("withdrawn", "withdrawn-before-receipt")
NOTE_PREVIEW = "Note to the next turn: the stopped turn is left, not resumed"
PREVIEW_LIMIT = 280
EMPTY_KEY = {"last": None, "last_is_own_send": False, "followed": None, "followed_length": 0, "tray": [],
             "settling": True, "has_history": False}


def run_queue(core_probe, steps: list[dict], cid: str = CID) -> dict:
    """The probe's `queue` command, with a snapshot after every step."""
    steps = [{**step, "snapshot": True} for step in steps]
    with tempfile.TemporaryDirectory(prefix="sf-tray-") as scratch:
        return run_probe(core_probe, "queue", write_json(Path(scratch) / f"{uuid.uuid4().hex}.json",
                                                         {"conversation_id": cid, "steps": steps}))


def queue_words(core_probe, **inputs) -> dict:
    with tempfile.TemporaryDirectory(prefix="sf-tray-") as scratch:
        return run_probe(core_probe, "queue-words", write_json(Path(scratch) / f"{uuid.uuid4().hex}.json", inputs))


def ts(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def receipt(mid: str, seq: int, state: str, origin: str = "person", continues: str | None = None,
            text: str | None = "", reason: str | None = None, stop: bool | None = None) -> dict:
    """A receipt as `conversation.open` or `message.status` gives it; `text=""`
    stands for "message <seq>", None for a receipt with no text."""
    out = {"message_id": mid, "conversation_id": CID, "seq": seq, "origin": origin, "continues": continues,
           "state": state, "state_reason": reason, "text": f"message {seq}" if text == "" else text}
    if stop is not None:
        out["stop_requested"] = stop
    return out


def local(mid: str, text: str, images: int = 0) -> dict:
    """The composer's optimistic row for a message this app is sending."""
    return {"local": {"message_id": mid, "text": text, "attachments": [f"sha-{mid}-{n}" for n in range(images)]}}


class Log:
    """A synthetic `conversation.events` log, handed out as pages. A page with
    nothing new is how the app learns it has read the log to its end."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.read = 0

    def add(self, mid: str, event_kind: str, /, **data) -> dict:
        seq = len(self.events) + 1
        entry = {"seq": seq, "message_id": mid, "kind": event_kind, "ts": ts(seq), "data": data}
        self.events.append(entry)
        return entry

    def page(self) -> dict:
        events, self.read = self.events[self.read:], len(self.events)
        return {"page": {"events": events, "next": len(self.events), "reset": False}}


def ask(log: Log, mid: str, request_id: str, command: str = "rm -f mocktest/*") -> str:
    log.add(mid, "approval.requested", request_id=request_id, kind="tool", tool="Bash",
            input=f"command: {command}", options=["allow", "deny", "cancel-turn"])
    return f"approval:{mid}:{request_id}"


def history(*texts: str, next_before: int | None = None) -> dict:
    """A page of the native transcript from before the first event."""
    return {"history": {"items": [{"role": "assistant", "kind": "text", "text": text, "ts": ts(-3600 + i),
                                   "cursor": 100 + i} for i, text in enumerate(texts)],
                        "next_before": next_before}}


def ids(snapshot: dict) -> list[str]:
    return [item["id"] for item in snapshot["layout"]["items"]]


def tray(snapshot: dict) -> list[str]:
    return [row["id"] for row in snapshot["layout"]["tray"]]


def tray_row(snapshot: dict, mid: str) -> dict:
    return next(row for row in snapshot["layout"]["tray"] if row["id"] == mid)


def shown(snapshot: dict, item_id: str) -> dict:
    return next(item for item in snapshot["layout"]["items"] if item["id"] == item_id)


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


def test_c29_7_the_incident_queue_waits_in_the_tray_below_the_card(core_probe):
    """C-29.7 and C-27.5 on the 2026-09-27 conversation: the five queued messages
    are the tray, in sequence order, each withdrawn with `message.cancel`; the
    timeline ends at the card, which the view follows."""
    log = Log()
    steps, card = incident(log)
    result = run_queue(core_probe, steps)
    last = result["snapshots"][-1]
    layout = last["layout"]
    assert tray(last) == ["m8", "m9", "m10", "m11", "m12"]
    assert not any(f"person:m{n}" in ids(last) for n in range(8, 13))
    # The failover turn where it ran, then the live turn and its card.
    assert ids(last) == ["person:m6", "text:m6:0", "person:m13", "thinking:m13:0", "person:m7", "tool:m7:agent-1",
                         card]
    assert last["timeline"]["pending_items"] == [card]
    assert layout["title"] == "5 messages queued; they go in order when this turn ends"
    assert layout["followed"] == card and layout["live"] is True and layout["held"] is False
    for n, row in zip(range(8, 13), layout["tray"]):
        assert row == {"id": f"m{n}", "origin": "person", "preview": f"message {n}", "text": f"message {n}",
                       "attachments": 0, "sending": False, "status": None,
                       "withdraw": {"action": "cancel", "message_id": f"m{n}"}, "can_withdraw": True,
                       "can_steer": False}
    # The page that brought the card moved the view to it; the receipt after it changed nothing it watches.
    assert result["moves"] == ["jump", "jump", "jump", "glide", "stay"]


# MARK: - Where a send shows


def test_c29_7_a_send_behind_a_live_turn_waits_in_the_tray_until_it_starts(core_probe):
    """C-29.7: a send made while a turn runs goes straight to the tray, stays there
    when its receipt says `queued` (it never shows in the timeline first), and
    moves into the timeline once its turn starts."""
    log = Log()
    log.add("m1", "accepted")
    log.add("m1", "text", block="0", text="Working on it")
    steps = [{"receipts": [receipt("m1", 1, "running")]}, log.page(), log.page(),
             local("m2", "and then\nthis"),
             {"receipts": [receipt("m2", 2, "queued", text=None)]}]
    log.add("m1", "turn.completed", state="complete")
    steps += [log.page(), {"receipts": [receipt("m1", 1, "complete")]}]
    log.add("m2", "accepted")
    log.add("m2", "text", block="0", text="On it")
    steps += [{"receipts": [receipt("m2", 2, "running", text=None)]}, log.page()]
    result = run_queue(core_probe, steps)
    snaps = result["snapshots"]
    sending, answered, completed, ended, started, running = snaps[3:]

    assert tray(sending) == ["m2"]
    assert tray_row(sending, "m2") == {
        "id": "m2", "origin": "person", "preview": "and then this", "text": "and then\nthis", "attachments": 0,
        "sending": True, "status": "Sending", "withdraw": {"action": "withdraw", "message_id": "m2"},
        "can_withdraw": True, "can_steer": False}
    assert sending["layout"]["title"] == "1 message queued; it goes when this turn ends"
    assert tray(answered) == ["m2"]
    assert tray_row(answered, "m2")["sending"] is False and tray_row(answered, "m2")["status"] is None
    assert tray_row(answered, "m2")["withdraw"] == {"action": "cancel", "message_id": "m2"}
    # The daemon holds it after the turn ended, until the dispatcher sends it.
    assert tray(completed) == ["m2"] and tray(ended) == ["m2"]
    assert ended["layout"]["title"] == "1 message queued; it goes next"
    for snapshot in (sending, answered, completed, ended):
        assert "person:m2" not in ids(snapshot)
        assert snapshot["layout"]["followed"] == "text:m1:0"
    assert tray(started) == [] and started["layout"]["title"] is None
    assert ids(started)[-1] == "person:m2" and shown(started, "person:m2")["state"] == "running"
    assert ids(running)[-1] == "text:m2:0" and running["layout"]["followed"] == "text:m2:0"
    # Into the tray glides; the receipt and the turn's end move nothing; the start glides.
    assert result["moves"][3:] == ["glide", "stay", "stay", "stay", "glide", "glide"]


def test_c29_7_a_send_into_an_idle_conversation_shows_in_the_timeline(core_probe):
    """C-29.7: with nothing ahead a send starts next and shows in the timeline at
    once; a second send made before the first's receipt waits in the tray."""
    log = Log()
    log.add("m1", "accepted")
    log.add("m1", "text", block="0", text="Done.")
    log.add("m1", "turn.completed", state="complete")
    steps = [{"receipts": [receipt("m1", 1, "complete")]}, log.page(), log.page(),
             local("m2", "next question"), local("m3", "and another", images=1)]
    result = run_queue(core_probe, steps)
    first, second = result["snapshots"][3:]
    assert tray(first) == [] and first["layout"]["title"] is None
    assert ids(first)[-1] == "person:m2"
    assert shown(first, "person:m2")["state"] == "sending" and shown(first, "person:m2")["text"] == "next question"
    assert first["key"]["last_is_own_send"] is True
    assert tray(second) == ["m3"] and ids(second)[-1] == "person:m2" and "person:m3" not in ids(second)
    assert tray_row(second, "m3")["preview"] == "and another (1 image)"
    assert tray_row(second, "m3")["attachments"] == 1 and tray_row(second, "m3")["status"] == "Sending"
    assert second["layout"]["title"] == "1 message queued; it goes next"
    assert result["moves"][3:] == ["glide", "glide"]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "app bug: QueueTray.swift Timeline.trayMessageIDs(held:) sends every `queued` message to the tray, but "
    "message.submit answers `queued` for a send into an idle conversation too (the dispatcher claims it "
    "later, design §4), so the person's own send moves from the timeline into the tray and back out once it "
    "starts; a send whose live turn ends before its receipt moves out of the tray, back in, and out again"))
def test_c29_7_an_idle_send_answered_queued_stays_in_the_timeline(core_probe):
    """C-29.7: a send with nothing ahead starts next; its receipt, which says
    `queued` until the dispatcher claims it, does not move it into the tray
    and back (the flash the tray exists to avoid)."""
    harness = ServiceHarness(Path(tempfile.mkdtemp(prefix="sf-tray-", dir="/tmp")))
    try:
        cid = harness.create()["conversation_id"]
        answer = harness.submit(cid, "hello there")
    finally:
        harness.close()
    if answer["state"] != "queued" or answer["created"] is not True:
        pytest.fail(f"precondition: the daemon's answer to an idle send is a new queued message: {answer}")
    mid = answer["message_id"]
    steps = [{"page": {"events": [], "next": 0, "reset": False}},
             {"local": {"message_id": mid, "text": "hello there", "attachments": []}},
             {"receipts": [answer]},
             {"receipts": [{**answer, "state": "waiting", "state_reason": "dispatching"}]}]
    idle = run_queue(core_probe, steps, cid=cid)["snapshots"][1:]
    if not (f"person:{mid}" in ids(idle[0]) and tray(idle[0]) == []):
        pytest.fail("precondition: before its receipt the send shows in the timeline")

    # A send made behind a turn that ends before the send's receipt comes.
    log = Log()
    log.add("m1", "accepted")
    behind = [{"receipts": [receipt("m1", 1, "running")]}, log.page(), log.page(), local("m2", "then this")]
    log.add("m1", "turn.completed", state="complete")
    behind += [log.page(), {"receipts": [receipt("m1", 1, "complete")]},
               {"receipts": [receipt("m2", 2, "queued", text=None)]},
               {"receipts": [receipt("m2", 2, "waiting", text=None, reason="dispatching")]}]
    moved = run_queue(core_probe, behind)["snapshots"][3:]
    if tray(moved[0]) != ["m2"]:
        pytest.fail("precondition: the send behind the live turn waits in the tray")

    for snapshot in idle:
        assert tray(snapshot) == [] and f"person:{mid}" in ids(snapshot)
    out_of_tray = [tray(snapshot) == [] for snapshot in moved]
    # Once out of the tray with nothing ahead of it, it stays out.
    assert out_of_tray == sorted(out_of_tray)


def test_c29_7_a_held_conversation_keeps_even_the_first_send_in_the_tray(core_probe):
    """C-29.7, C-24.5: while a block holds the conversation nothing sent starts,
    so the first send waits in the tray; once the block clears it shows in the
    timeline and a later send waits behind it."""
    log = Log()
    log.add("m1", "accepted")
    log.add("m1", "turn.completed", state="interrupted")
    steps = [{"receipts": [receipt("m1", 1, "interrupted")]}, log.page(), log.page(),
             {**local("m2", "try again"), "held": True}, {**local("m3", "with this"), "held": True},
             {"held": False}]
    result = run_queue(core_probe, steps)
    one, two, cleared = result["snapshots"][3:]
    assert tray(one) == ["m2"] and "person:m2" not in ids(one)
    assert one["layout"]["held"] is True and one["layout"]["live"] is False
    assert one["layout"]["title"] == "1 message queued; it waits until the conversation can continue"
    assert tray(two) == ["m2", "m3"]
    assert two["layout"]["title"] == "2 messages queued; they wait until the conversation can continue"
    assert tray(cleared) == ["m3"] and ids(cleared)[-1] == "person:m2"
    assert cleared["layout"]["title"] == "1 message queued; it goes next"


def test_c29_7_a_queued_unblock_note_heads_the_tray_and_is_not_withdrawn(core_probe):
    """C-29.7, C-24.8: the daemon sends a `leave` note ahead of the queued person
    messages; its row says what it is and offers no Withdraw (withdrawing it
    would turn "Leave it" into "Continue it")."""
    log = Log()
    log.add("m1", "accepted")
    log.add("m1", "turn.completed", state="interrupted")
    steps = [{"receipts": [receipt("m1", 1, "interrupted"), receipt("m2", 2, "queued"),
                           receipt("m3", 3, "queued", origin="unblock-note", text="(unblock note)"),
                           receipt("m4", 4, "queued")]}, log.page(), log.page()]
    result = run_queue(core_probe, steps)
    last = result["snapshots"][-1]
    assert tray(last) == ["m3", "m2", "m4"]
    assert tray_row(last, "m3") == {
        "id": "m3", "origin": "unblock-note", "preview": NOTE_PREVIEW, "text": None, "attachments": 0,
        "sending": False, "status": None, "withdraw": {"action": "none"}, "can_withdraw": False,
        "can_steer": False}
    assert [tray_row(last, mid)["withdraw"]["action"] for mid in ("m2", "m4")] == ["cancel", "cancel"]
    assert last["layout"]["title"] == "A note and 2 messages queued; they go in order"
    assert ids(last) == ["person:m1"]
    only = run_queue(core_probe, [{"receipts": [receipt("m1", 1, "interrupted"),
                                                receipt("m3", 3, "queued", origin="unblock-note")]}])
    assert only["layout"]["title"] == "A note to the next turn is queued; it goes next"


def test_c29_7_a_queued_failover_continuation_shows_under_its_message(core_probe):
    """C-29.7, C-26.7: a continuation is not a message the person queued; it shows
    under the message it continues, which the daemon sends first."""
    log = Log()
    log.add("m1", "accepted")
    log.add("m1", "text", block="0", text="Halfway")
    log.add("m1", "limits", status="rejected", type="five_hour")
    log.add("m1", "turn.completed", state="failed")
    steps = [{"receipts": [receipt("m1", 1, "failed"), receipt("m3", 3, "queued")]}, log.page(), log.page(),
             {"receipts": [receipt("m2", 2, "queued", origin="failover", continues="m1")]}]
    result = run_queue(core_probe, steps)
    last = result["snapshots"][-1]
    assert tray(last) == ["m3"]
    assert shown(last, "person:m2")["type"] == "notice"
    assert ids(last).index("person:m2") > ids(last).index("text:m1:0")


# MARK: - Withdrawn messages


def test_c29_7_a_message_withdrawn_from_the_queue_reads_as_one_line(core_probe):
    """C-29.7, D-12: a queued message withdrawn with `message.cancel` never reached
    the provider; it leaves the tray and reads as one timeline line in place of
    its bubble, under the same row id."""
    log = Log()
    log.add("m1", "accepted")
    log.add("m1", "text", block="0", text="Working")
    steps = [{"receipts": [receipt("m1", 1, "running"),
                           receipt("m2", 2, "queued", text="please also\ncheck the docs")]},
             log.page(), log.page(),
             {"receipts": [receipt("m2", 2, "cancelled", text=None, reason="withdrawn", stop=True)]}]
    result = run_queue(core_probe, steps)
    queued, withdrawn = result["snapshots"][2:]
    assert tray(queued) == ["m2"] and "person:m2" not in ids(queued)
    assert tray(withdrawn) == [] and withdrawn["layout"]["title"] is None
    assert shown(withdrawn, "person:m2") == {"id": "person:m2", "message_id": "m2", "ts": None, "type": "notice",
                                             "text": "Withdrawn before it was sent: please also check the docs"}
    # The view keeps following the live turn; the tray and the timeline changed at the end.
    assert withdrawn["layout"]["followed"] == "text:m1:0"
    assert result["moves"][-1] == "glide"


def test_c29_7_a_send_withdrawn_before_its_receipt_reads_as_one_line_and_a_tombstone_as_nothing(core_probe):
    """C-29.7, D-22: the app's own withdrawal of a send the daemon never had
    (`withdraw_local`) reads as one line; the daemon's tombstone for such a send
    shows nothing."""
    log = Log()
    log.add("m1", "accepted")
    steps = [{"receipts": [receipt("m1", 1, "running")]}, log.page(), log.page(),
             local("m2", "never mind", images=2), {"withdraw_local": "m2"},
             {"receipts": [receipt("m2", 2, "cancelled", origin="tombstone", reason="withdrawn-before-receipt",
                                   text="(withdrawn before it was received)")]}]
    result = run_queue(core_probe, steps)
    sending, withdrawn, tombstone = result["snapshots"][3:]
    assert tray(sending) == ["m2"]
    assert tray(withdrawn) == []
    assert shown(withdrawn, "person:m2")["type"] == "notice"
    assert shown(withdrawn, "person:m2")["text"] == "Withdrawn before it was sent: never mind (2 images)"
    assert tray(tombstone) == [] and not any(item["message_id"] == "m2" for item in tombstone["layout"]["items"])


def test_c29_7_a_cancelled_message_that_did_something_keeps_its_bubble(core_probe):
    """C-29.7: only a message withdrawn before it started reads as the one line;
    one whose turn left rows, or one handed off to another conversation, keeps
    its bubble."""
    log = Log()
    log.add("m1", "error", message="the provider refused the session", kind="provider", will_retry=True)
    steps = [log.page(), {"receipts": [receipt("m1", 1, "cancelled", reason="withdrawn", stop=True),
                                       receipt("m2", 2, "cancelled", reason="handed-off:cv-next"),
                                       receipt("m3", 3, "cancelled", reason=None)]}]
    last = run_queue(core_probe, steps)["snapshots"][-1]
    assert [shown(last, f"person:m{n}")["type"] for n in (1, 2, 3)] == ["person", "person", "person"]
    assert "error:1" in ids(last) and tray(last) == []


def test_c29_7_real_daemon_withdrawals_leave_one_line_or_nothing(core_probe):
    """C-29.7 with the daemon's own receipts (`message.submit`, `message.cancel`,
    and the tombstone `message.cancel` makes for an id it never received)."""
    harness = ServiceHarness(Path(tempfile.mkdtemp(prefix="sf-tray-", dir="/tmp")))
    try:
        cid = harness.create()["conversation_id"]
        first = harness.submit(cid, "start the migration")
        second = harness.submit(cid, "also update\nthe changelog", after=first["message_id"])
        withdrawn = harness.call("message.cancel", message_id=second["message_id"])
        ghost = str(uuid.uuid4())
        tombstone = harness.call("message.cancel", message_id=ghost, conversation_id=cid)
    finally:
        harness.close()
    assert (withdrawn["state"], withdrawn["state_reason"], withdrawn["stop_requested"]) == ("cancelled", "withdrawn",
                                                                                            True)
    assert (tombstone["origin"], tombstone["state"], tombstone["state_reason"]) == ("tombstone", "cancelled",
                                                                                  "withdrawn-before-receipt")
    one, two = first["message_id"], second["message_id"]
    steps = [{"local": {"message_id": one, "text": "start the migration", "attachments": []}},
             {"local": {"message_id": two, "text": "also update\nthe changelog", "attachments": []}},
             {"receipts": [first, second]}, {"receipts": [withdrawn]},
             {"local": {"message_id": ghost, "text": "and this", "attachments": []}},
             {"withdraw_local": ghost}, {"receipts": [tombstone]}]
    snaps = run_queue(core_probe, steps, cid=cid)["snapshots"]
    assert tray(snaps[2]) == [one, two]
    assert tray(snaps[3]) == [one]
    assert shown(snaps[3], f"person:{two}")["text"] == "Withdrawn before it was sent: also update the changelog"
    assert shown(snaps[5], f"person:{ghost}")["text"] == "Withdrawn before it was sent: and this"
    assert f"person:{ghost}" not in ids(snaps[6]) and ghost not in tray(snaps[6])


# MARK: - Steer


def test_c29_7_steer_is_offered_only_on_the_head_the_daemon_holds_while_a_turn_is_live(core_probe):
    """C-29.7: Steer sends the head of the queue into the running turn; it is
    offered on a person's message the daemon holds, only while a turn is live and
    the daemon steers the provider."""
    def live_log() -> dict:
        log = Log()
        log.add("m1", "accepted")
        return log.page()

    running = receipt("m1", 1, "running")
    offered = run_queue(core_probe, [{"receipts": [running, receipt("m2", 2, "queued"), receipt("m3", 3, "queued")]},
                                     live_log(), {"steer": True}, {"steer": False}])
    on, off = offered["snapshots"][-2:]
    assert [(row["id"], row["can_steer"]) for row in on["layout"]["tray"]] == [("m2", True), ("m3", False)]
    assert [row["can_steer"] for row in off["layout"]["tray"]] == [False, False]

    note = run_queue(core_probe, [{"receipts": [running, receipt("m2", 2, "queued"),
                                                receipt("m3", 3, "queued", origin="unblock-note")]},
                                  live_log(), {"steer": True}])["snapshots"][-1]
    assert tray(note) == ["m3", "m2"] and not any(row["can_steer"] for row in note["layout"]["tray"])

    sending = run_queue(core_probe, [{"receipts": [running]}, live_log(),
                                     {**local("m2", "steer me"), "steer": True}])["snapshots"][-1]
    assert tray(sending) == ["m2"] and tray_row(sending, "m2")["can_steer"] is False

    idle = run_queue(core_probe, [{"receipts": [receipt("m1", 1, "complete"), receipt("m2", 2, "queued")]},
                                  {"steer": True}])["snapshots"][-1]
    assert tray(idle) == ["m2"] and tray_row(idle, "m2")["can_steer"] is False


# MARK: - Keeping the end in view


def test_c29_7_follow_opens_at_the_end_then_follows_only_while_the_end_is_on_screen(core_probe):
    """C-29.7, design §12: while the conversation reads its log the view jumps to
    the end with no scrolling; after that a new row glides and a growing block
    jumps while the end is on screen, and neither moves the view, nor does a
    send that goes to the tray, while the person reads further up."""
    log = Log()
    log.add("m1", "accepted")
    log.add("m1", "text", block="0", text="Done.")
    log.add("m1", "turn.completed", state="complete")
    steps = [{"receipts": [receipt("m1", 1, "complete")]}, log.page(), log.page(), {}]
    log.add("m2", "accepted")
    log.add("m2", "text", block="0", text="On it")
    steps += [{"receipts": [receipt("m2", 2, "running")]}, log.page()]
    log.add("m2", "text.delta", block="1", text="Reading")
    steps.append(log.page())
    log.add("m2", "text.delta", block="1", text=" the files")
    steps.append(log.page())
    log.add("m2", "text.delta", block="1", text=" one by one")
    steps.append({**log.page(), "at_bottom": False})
    log.add("m2", "text.delta", block="2", text="Found it")
    steps.append({**log.page(), "at_bottom": False})
    steps += [{**local("m3", "then this"), "at_bottom": False},
              {"receipts": [receipt("m3", 3, "queued", text=None)], "at_bottom": False},
              local("m4", "and this")]
    result = run_queue(core_probe, steps)
    assert [snapshot["layout"]["settling"] for snapshot in result["snapshots"][:3]] == [True, True, False]
    assert result["moves"] == [
        "jump", "jump", "jump",   # opening: the receipts, the log, the page that read it to its end
        "stay",                   # nothing changed
        "glide", "glide",         # a new message, then its first row, with the end on screen
        "glide", "jump",          # a new block, then the block growing
        "stay", "stay",           # the same while the person reads further up
        "stay", "stay",           # a send into the tray, and its receipt, while reading further up
        "glide",                  # another send into the tray with the end on screen
    ]
    assert tray(result["snapshots"][-1]) == ["m3", "m4"]


def test_c29_7_follow_the_persons_own_send_focus_and_the_first_history_page(core_probe):
    """C-29.7, design §12: the person's own send that lands in the timeline glides
    even while they read further up; focusing the conversation again jumps on
    every change until the log is read; the first page of history landing with
    the end on screen jumps, and a later page moves nothing it watches."""
    log = Log()
    log.add("m1", "accepted")
    log.add("m1", "text", block="0", text="Done.")
    log.add("m1", "turn.completed", state="complete")
    steps = [{"receipts": [receipt("m1", 1, "complete")]}, log.page(), log.page(),
             {**local("m2", "one more thing"), "at_bottom": False},
             {"focus": True, "at_bottom": False}, {"at_bottom": False},
             {"receipts": [receipt("m2", 2, "running", text=None)], "at_bottom": False}]
    steps.append({**log.page(), "at_bottom": False})
    log.add("m2", "accepted")
    log.add("m2", "text", block="0", text="Sure")
    steps += [{**log.page(), "at_bottom": False},
              history("before", next_before=40), history("even earlier", next_before=None)]
    result = run_queue(core_probe, steps)
    snaps = result["snapshots"]
    assert snaps[3]["key"]["last_is_own_send"] is True and ids(snaps[3])[-1] == "person:m2"
    assert [snapshot["layout"]["settling"] for snapshot in snaps[4:8]] == [True, True, True, False]
    assert [snapshot["layout"]["has_history"] for snapshot in snaps[-3:]] == [False, True, True]
    assert result["moves"] == [
        "jump", "jump", "jump",
        "glide",                  # the person's own send, although they read further up
        "jump", "stay", "jump",   # focused: settling, nothing new, the receipt while settling
        "jump",                   # the page that read the log to its end
        "stay",                   # a new row while reading further up
        "jump", "stay",           # the first page of history with the end on screen; the next
    ]


# MARK: - The tray's words


def test_c29_7_queue_words_previews(core_probe):
    """C-29.7: a row's words are the message on one line, cut at 280 characters
    with "…", with the image count; the image count alone when there is no text."""
    words = queue_words(core_probe, previews=[
        {"text": "line one\nline two\n\n  indented\tand tabbed", "attachments": 0},
        {"text": "a\r\nb", "attachments": 0},
        {"text": "x" * 280, "attachments": 0},
        {"text": "x" * 281, "attachments": 0},
        {"text": "word " * 100, "attachments": 0},
        {"text": "see this", "attachments": 1},
        {"text": "see these", "attachments": 3},
        {"text": "x" * 300, "attachments": 2},
        {"text": "", "attachments": 1},
        {"text": " \n\t ", "attachments": 2},
        {"text": None, "attachments": 0},
        {"text": "", "attachments": 0},
        {"text": "e\u0301" * 300, "attachments": 0},
    ])
    assert words["preview_limit"] == PREVIEW_LIMIT
    assert words["previews"] == [
        "line one line two indented and tabbed", "a b", "x" * 280, "x" * 279 + "…",
        " ".join(["word"] * 100)[:279] + "…", "see this (1 image)", "see these (3 images)",
        "x" * 279 + "… (2 images)", "1 image", "2 images", "(message text not available)",
        "(message text not available)",
        # Characters are counted, and one is never split.
        "e\u0301" * 279 + "…",
    ]


def test_c29_7_queue_words_statuses(core_probe):
    """C-29.7: a queued row's second caption: nothing while it simply waits, the
    daemon's deferral as "Deferred: <why>", any other reason in words."""
    words = queue_words(core_probe, statuses=[None, "", "deferred: lane busy", "deferred: rate-limited", "deferred:",
                                              "handoff-rolled-back", "dispatching"])
    assert words["statuses"] == [None, None, "Deferred: lane busy", "Deferred: rate-limited", "Deferred:",
                                 "Handoff rolled back", "Dispatching"]


def test_c29_7_queue_words_titles(core_probe):
    """C-29.7: the tray's heading counts the person's messages, names a note, and
    says when they go: next, when the live turn ends, or once the block clears."""
    words = queue_words(core_probe, titles=[
        {"rows": [], "live": True, "held": True},
        {"rows": [{"origin": "person"}], "live": False, "held": False},
        {"rows": [{"origin": None}], "live": True, "held": False},
        {"rows": [{"origin": "person"}, {"origin": "person"}], "live": True, "held": False},
        {"rows": [{"origin": "unblock-note"}], "live": False, "held": False},
        {"rows": [{"origin": "unblock-note"}, {"origin": "person"}], "live": True, "held": True},
        {"rows": [{"origin": "person"}], "live": True, "held": True},
        {"rows": [{"origin": "person"}] * 3, "live": False, "held": False},
    ])
    assert words["titles"] == [
        None,
        "1 message queued; it goes next",
        "1 message queued; it goes when this turn ends",
        "2 messages queued; they go in order when this turn ends",
        "A note to the next turn is queued; it goes next",
        "A note and 1 message queued; they wait until the conversation can continue",
        "1 message queued; it waits until the conversation can continue",
        "3 messages queued; they go in order",
    ]


def test_c29_7_queue_words_rows_shown(core_probe):
    """C-29.7: up to four rows show; past that three and "Show N more", and every
    row once the person expands the tray."""
    words = queue_words(core_probe, visible=[
        {"count": 0, "expanded": False}, {"count": 4, "expanded": False}, {"count": 5, "expanded": False},
        {"count": 5, "expanded": True}, {"count": 10, "expanded": False, "limit": 2},
        {"count": 3, "expanded": False, "limit": 1}, {"count": -1, "expanded": False}])
    assert words["visible"] == [[0, 0], [4, 0], [3, 2], [5, 0], [1, 9], [1, 2], [0, 0]]


def test_c29_7_queue_words_the_control_under_a_message(core_probe):
    """C-29.7, D-22: Withdraw for a message the daemon holds (`message.cancel`) and
    for one with no receipt (the outbox's withdrawal); Stop while its turn is
    live; nothing once it has ended."""
    states = ["queued", "sending", "waiting", "starting", "running", "approval-needed", "complete", "failed",
              "interrupted", "cancelled", "delivery-unknown", "unknown", None]
    words = queue_words(core_probe, stops=[{"message_id": "m1", "state": state} for state in states])
    withdraw, stop = "Withdraw", "Stop"
    assert [(entry["action"]["action"], entry["label"]) for entry in words["stops"]] == [
        ("cancel", withdraw), ("withdraw", withdraw), ("interrupt", stop), ("interrupt", stop), ("interrupt", stop),
        ("interrupt", stop), ("none", None), ("none", None), ("none", None), ("none", None), ("none", None),
        ("none", None), ("none", None)]
    assert all(entry["action"].get("message_id", "m1") == "m1" for entry in words["stops"])


def test_c29_7_queue_words_the_steer_candidate(core_probe):
    """C-29.7: Steer may take only the head of the queue, and only a person's
    message the daemon holds: not a repair message, not a send with no receipt."""
    words = queue_words(core_probe, steer=[
        [], [{"id": "a", "origin": "person"}], [{"id": "a", "origin": None}],
        [{"id": "a", "origin": "unblock-note"}, {"id": "b", "origin": "person"}],
        [{"id": "a", "origin": "person", "sending": True}, {"id": "b", "origin": "person"}],
        [{"id": "a", "origin": "failover"}]])
    assert words["steer"] == [None, "a", "a", None, None, None]


# MARK: - References, written again from the rules


def split_words(text: str | None) -> list[str]:
    return (text or "").split()


def ref_images(count: int) -> str:
    return f"{count} image{'' if count == 1 else 's'}"


def ref_preview(text: str | None, attachments: int) -> str:
    folded = " ".join(split_words(text))
    if not folded:
        return ref_images(attachments) if attachments > 0 else "(message text not available)"
    cut = folded[:PREVIEW_LIMIT - 1] + "…" if len(folded) > PREVIEW_LIMIT else folded
    return f"{cut} ({ref_images(attachments)})" if attachments > 0 else cut


def ref_status_words(reason: str | None) -> str | None:
    if not reason:
        return None
    if reason.startswith("deferred:"):
        return "Deferred:" + reason[len("deferred:"):]
    return reason[0].upper() + reason[1:].replace("-", " ")


def ref_title(origins: list[str | None], live: bool, held: bool) -> str | None:
    if not origins:
        return None
    messages = sum(1 for origin in origins if origin != "unblock-note")
    count = "1 message" if messages == 1 else f"{messages} messages"
    what = ("A note to the next turn is queued" if messages == 0
            else f"A note and {count} queued" if messages < len(origins) else f"{count} queued")
    one = len(origins) == 1
    if held:
        return what + ("; it waits" if one else "; they wait") + " until the conversation can continue"
    if live:
        return what + ("; it goes when this turn ends" if one else "; they go in order when this turn ends")
    return what + ("; it goes next" if one else "; they go in order")


def ref_visible(count: int, expanded: bool, limit: int) -> list[int]:
    count = max(0, count)
    if expanded or count <= limit:
        return [count, 0]
    shown_rows = max(1, limit - 1)
    return [shown_rows, count - shown_rows]


def ref_move(old: dict, new: dict, at_bottom: bool) -> str:
    """The follow rule: equal keys stay; opening jumps; the person's own new
    send glides; otherwise only with the end on screen: a new last row, a new
    followed row or a changed tray glides, a growing block or the first
    history page jumps."""
    if old == new:
        return "stay"
    if old["settling"] or new["settling"]:
        return "jump"
    if new["last"] != old["last"] and new["last_is_own_send"]:
        return "glide"
    if not at_bottom:
        return "stay"
    if new["last"] != old["last"] or new["followed"] != old["followed"] or new["tray"] != old["tray"]:
        return "glide"
    if new["followed_length"] != old["followed_length"] or new["has_history"] != old["has_history"]:
        return "jump"
    return "stay"


def rows_of(timeline: dict, mid: str) -> list[str]:
    return [item["id"] for item in timeline["items"] if item["message_id"] == mid]


def did_nothing(timeline: dict, mid: str) -> bool:
    """No row of the message's own besides its person row: its turn made none."""
    return rows_of(timeline, mid) in ([], [f"person:{mid}"])


def waits_in_queue(timeline: dict, mid: str) -> bool:
    turn = timeline["turns"][mid]
    return (mid != CONVERSATION_KEY and turn["state"] in ("queued", "sending") and turn["continues"] is None
            and turn["first_event"] is None and did_nothing(timeline, mid))


def is_live(timeline: dict) -> bool:
    return any(turn["state"] in LIVE for mid, turn in timeline["turns"].items() if mid != CONVERSATION_KEY)


def ref_queue(timeline: dict, arrival: dict[str, int]) -> list[str]:
    """The queue as the daemon sends it: repair origins first, then sequence; a
    message with no receipt (no sequence) after, in the order it arrived."""
    turns = timeline["turns"]
    waiting = [mid for mid in turns if waits_in_queue(timeline, mid)]
    return sorted(waiting, key=lambda mid: (turns[mid]["origin"] not in REPAIR_ORIGINS,
                                            math.inf if turns[mid]["seq"] is None else turns[mid]["seq"],
                                            arrival[mid]))


def ref_tray(timeline: dict, arrival: dict[str, int], held: bool) -> list[str]:
    """A queued message waits in the tray; so does one with no receipt when
    anything is ahead of it: a live turn, a block, an earlier queue member."""
    ahead = held or is_live(timeline)
    out = []
    for mid in ref_queue(timeline, arrival):
        if timeline["turns"][mid]["state"] == "queued" or ahead:
            out.append(mid)
        ahead = True
    return out


def withdrawn_before_start(timeline: dict, mid: str) -> bool:
    turn = timeline["turns"][mid]
    return (turn["state"] == "cancelled" and turn["state_reason"] in WITHDRAWN_REASONS
            and did_nothing(timeline, mid))


def person_attachments(timeline: dict, mid: str) -> int:
    row = next((item for item in timeline["items"] if item["id"] == f"person:{mid}"), None)
    return len(row.get("attachments", [])) if row else 0


def ref_row(timeline: dict, mid: str, can_steer: bool) -> dict:
    turn = timeline["turns"][mid]
    note = turn["origin"] == "unblock-note"
    sending = turn["state"] == "sending"
    images = person_attachments(timeline, mid)
    return {"id": mid, "origin": turn["origin"],
            "preview": NOTE_PREVIEW if note else ref_preview(turn["person_text"], images),
            "text": None if note else turn["person_text"], "attachments": images, "sending": sending,
            "status": "Sending" if sending else ref_status_words(turn["state_reason"]),
            "withdraw": {"action": "none"} if note else {"action": "withdraw" if sending else "cancel",
                                                         "message_id": mid},
            "can_withdraw": not note, "can_steer": can_steer}


def ref_items(timeline: dict, tray_ids: list[str]) -> list[dict]:
    out = []
    for item in timeline["items"]:
        mid = item["message_id"]
        if mid in tray_ids:
            continue
        if mid in timeline["turns"] and item["type"] == "person" and withdrawn_before_start(timeline, mid):
            words = ref_preview(timeline["turns"][mid]["person_text"], len(item["attachments"]))
            out.append({"id": item["id"], "message_id": mid, "ts": item["ts"], "type": "notice",
                        "text": "Withdrawn before it was sent: " + words})
        else:
            out.append(item)
    return out


def ref_followed_item(timeline: dict) -> str | None:
    """The newest row of the turn that began last; before any began, the last row
    above the queue; else the last history row."""
    turns = timeline["turns"]
    begun = [mid for mid, turn in turns.items() if mid != CONVERSATION_KEY and turn["first_event"] is not None]
    if begun:
        rows = rows_of(timeline, max(begun, key=lambda mid: turns[mid]["first_event"]))
        if rows:
            return rows[-1]
    for mid in reversed(timeline["display_order"]):
        if mid in turns and not waits_in_queue(timeline, mid) and rows_of(timeline, mid):
            return rows_of(timeline, mid)[-1]
    older = [item["id"] for item in timeline["items"] if item["message_id"] is None]
    return older[-1] if older else None


def ref_key(layout: dict, caught_up: bool, history_pages: int) -> dict:
    items = layout["items"]
    last = items[-1] if items else None
    followed = next((item for item in items if item["id"] == layout["followed"]), None)
    return {"last": last["id"] if last else None,
            "last_is_own_send": bool(last) and last["type"] == "person" and last["state"] == "sending",
            "followed": layout["followed"],
            "followed_length": len(followed["text"]) if followed and followed["type"] in ("text", "thinking") else 0,
            "tray": [row["id"] for row in layout["tray"]], "settling": not caught_up,
            "has_history": history_pages > 0}


def replay(steps: list[dict]) -> list[dict]:
    """What the view knows besides the timeline after each step, followed by hand
    from the steps: when each message first arrived (a receipt, an event, an
    approval, a local send), whether the log has been read to its end since the
    timeline began or was focused, and how many history pages have loaded."""
    arrival: dict[str, int] = {}
    cursor, caught_up, pages, out = 0, False, 0, []
    for step in steps:
        if "page" in step:
            applied = 0
            for entry in sorted(step["page"]["events"], key=lambda entry: entry["seq"]):
                if entry["seq"] > cursor:
                    arrival.setdefault(entry["message_id"] or CONVERSATION_KEY, len(arrival))
                    cursor = entry["seq"]
                    applied += 1
            cursor = max(cursor, step["page"]["next"])
            caught_up = caught_up or applied == 0
        elif "receipts" in step:
            for entry in step["receipts"]:
                if entry.get("conversation_id") in (None, CID):
                    arrival.setdefault(entry["message_id"], len(arrival))
        elif "approvals" in step:
            for entry in step["approvals"]:
                if entry["conversation_id"] == CID:
                    arrival.setdefault(entry["message_id"], len(arrival))
        elif "history" in step:
            pages += 1
        elif "local" in step:
            arrival.setdefault(step["local"]["message_id"], len(arrival))
        elif step.get("focus"):
            caught_up = False
        out.append({"arrival": dict(arrival), "caught_up": caught_up, "history_pages": pages})
    return out


# MARK: - Generated timelines

TEXT_ALPHABET = ["a", "b", "z", "Q", ".", " ", " ", "\n", "\r", "\t", "\u00a0", "\u2003", "\u00e9", "\u2014"]
short_texts = st.text(alphabet=st.sampled_from(TEXT_ALPHABET), max_size=24)
long_texts = st.integers(240, 420).map(lambda n: ("lorem \n ipsum\tdolor " * 25)[:n])
send_texts = st.one_of(short_texts, short_texts, long_texts)
STATES = ["queued", "waiting", "starting", "running", "approval-needed", "complete", "failed", "interrupted",
          "cancelled", "delivery-unknown"]
REASONS = {
    "queued": [None, None, None, "deferred: lane busy", "deferred: rate-limited", "handoff-rolled-back"],
    "waiting": [None, "dispatching", "deferred: capacity", "external-writer: pid 42"],
    "cancelled": ["withdrawn", "withdrawn", "withdrawn-before-receipt", "handed-off:cv-next", None],
    "failed": [None, "not-delivered: refused"],
}
ANY_REASON = [None, "withdrawn", "deferred: x", "withdrawn-before-receipt", "handed-off:cv-next"]
ACTIONS = (["send"] * 5 + ["receipt"] * 5 + ["event"] * 5 + ["page"] * 4
           + ["withdraw_local", "tombstone", "focus", "history", "approvals", "idle"])
OPENING_STATES = ["complete", "complete", "running", "approval-needed", "waiting", "queued", "queued", "queued",
                  "interrupted", "cancelled"]
EVENTS = ["accepted", "accepted", "text", "delta", "delta", "tool", "ask", "ask", "answer", "complete", "status"]


@st.composite
def queue_timelines(draw):
    """Steps for a conversation of up to 7 messages: sends made before their
    receipts, receipts in any state and order with the daemon's state reasons,
    events interleaved across messages, withdrawals, tombstones, focus, history,
    approvals, and on every step whether a block holds the conversation, the
    daemon steers its provider, and the end of the timeline is on screen."""
    count = draw(st.integers(4, 7))
    mids = [f"m{n}" for n in range(1, count + 1)]
    kinds = {mid: "person" if index == 0 else draw(st.sampled_from(["person"] * 5 + ["unblock-note"] * 2
                                                                    + ["failover"]))
             for index, mid in enumerate(mids)}
    continues = {mid: draw(st.sampled_from(mids[:index])) for index, mid in enumerate(mids)
                 if kinds[mid] == "failover"}
    log, steps, sent, received, asked, attached = Log(), [], [], set(), [], []

    def flags(step: dict) -> dict:
        step["held"] = draw(st.sampled_from([False, False, False, True]))
        step["steer"] = draw(st.booleans())
        # A send is made as often from further up as with the end on screen.
        step["at_bottom"] = draw(st.sampled_from([True, False] if "local" in step else [True, True, False]))
        return step

    # How the conversation stands when the steps begin: nothing yet; opened on
    # messages the daemon already has (`conversation.open`); a stopped turn left
    # while messages were queued behind it, so the note came after them (C-24.8);
    # or a turn running, so sends wait behind it.
    opening = draw(st.sampled_from(["none", "open", "leave", "busy"]))
    if opening in ("open", "leave"):
        opened = mids[:draw(st.integers(3 if opening == "leave" else 1, count - 1))]
        if opening == "leave":
            kinds[opened[-1]] = "unblock-note"
            continues.pop(opened[-1], None)
        batch = []
        for index, mid in enumerate(opened):
            if kinds[mid] == "unblock-note":
                state = "queued"
            elif opening == "leave":
                state = "interrupted" if index == 0 else "queued"
            else:
                state = draw(st.sampled_from(OPENING_STATES))
            batch.append(receipt(mid, index + 1, state, origin=kinds[mid], continues=continues.get(mid),
                                 reason=draw(st.sampled_from(REASONS.get(state, [None])))))
            received.add(mid)
        steps.append(flags({"receipts": batch}))
    elif opening == "busy":
        kinds["m1"] = "person"
        received.add("m1")
        log.add("m1", "accepted")
        steps += [flags({"receipts": [receipt("m1", 1, draw(st.sampled_from(["running", "approval-needed"])))]}),
                  flags(log.page())]
    if opening == "none" or draw(st.sampled_from([True, True, False])):
        # The events loop reads the log to its end, as it does when the conversation opens.
        steps.append(flags(log.page()))
    for _ in range(draw(st.integers(1, 20))):
        action = draw(st.sampled_from(ACTIONS))
        step: dict = {}
        if action == "send":
            fresh = [mid for mid in mids if kinds[mid] == "person" and mid not in sent and mid not in received]
            if fresh:
                mid = fresh[0]
                sent.append(mid)
                step = local(mid, draw(send_texts), images=draw(st.sampled_from([0, 0, 1, 2])))
        elif action == "receipt":
            waiting = [mid for mid in sent if mid not in received]
            mid = draw(st.sampled_from(waiting)) if waiting and draw(st.booleans()) else draw(st.sampled_from(mids))
            if mid in waiting and draw(st.booleans()):
                state = "queued"        # the daemon's answer to a send behind something
            else:
                state = draw(st.sampled_from(STATES + ["queued", "queued", "running"]))
            reason = draw(st.sampled_from(REASONS.get(state, [None]))) if draw(st.integers(0, 4)) \
                else draw(st.sampled_from(ANY_REASON))
            text = draw(st.sampled_from([None, "", "from the daemon"]))
            step = {"receipts": [receipt(mid, mids.index(mid) + 1, state, origin=kinds[mid],
                                         continues=continues.get(mid), text=text, reason=reason,
                                         stop=draw(st.sampled_from([None, True, False])))]}
            received.add(mid)
        elif action == "event":
            mid = draw(st.sampled_from(mids))
            kind = draw(st.sampled_from(EVENTS))
            if kind == "accepted":
                log.add(mid, "accepted")
            elif kind == "text":
                log.add(mid, "text", block=f"b{len(log.events)}", text="All done here.")
            elif kind == "delta":
                log.add(mid, "text.delta", block=draw(st.sampled_from(["d0", "d1"])), text="more ")
            elif kind == "tool":
                log.add(mid, "tool.started", id=f"t{len(log.events)}", name="Bash", summary="ls")
            elif kind == "ask":
                request = f"r{len(asked)}"
                asked.append((mid, request))
                ask(log, mid, request, command=f"cmd {request}")
                if draw(st.booleans()):
                    attached.append({"approval_id": f"ap-{request}", "message_id": mid, "conversation_id": CID,
                                     "kind": "tool", "display": {"tool": "Bash", "input": f"command: cmd {request}"},
                                     "options": ["allow", "deny", "cancel-turn"], "created_at": ts(len(log.events)),
                                     "state": "pending"})
            elif kind == "answer" and asked:
                owner, request = draw(st.sampled_from(asked))
                log.add(owner, "approval.resolved", request_id=request, decision="allow")
            elif kind == "complete":
                log.add(mid, "turn.completed", state=draw(st.sampled_from(["complete", "interrupted"])))
            elif kind == "status":
                log.add(mid, "status", phase="starting-provider")
            if draw(st.booleans()):
                step = log.page()
        elif action == "page":
            step = log.page()
        elif action == "withdraw_local":
            step = {"withdraw_local": draw(st.sampled_from(sent or mids))}
        elif action == "tombstone" and sent:
            mid = draw(st.sampled_from(sent))
            step = {"receipts": [receipt(mid, mids.index(mid) + 1, "cancelled", origin="tombstone",
                                         reason="withdrawn-before-receipt", text="(withdrawn before it was received)")]}
            received.add(mid)
        elif action == "focus":
            step = {"focus": True}
        elif action == "history":
            step = history(f"older {len(steps)}", next_before=draw(st.sampled_from([None, 10])))
        elif action == "approvals" and attached:
            step = {"approvals": list(attached)}
        steps.append(flags(step))
    return steps


PROPERTY = settings(max_examples=120, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


def check_layout(step: dict, snapshot: dict, known: dict) -> None:
    """Every statement of the tray rule, on one snapshot."""
    timeline, layout = snapshot["timeline"], snapshot["layout"]
    turns, held, live = timeline["turns"], step.get("held", False), is_live(timeline)
    tray_ids = [row["id"] for row in layout["tray"]]
    layout_ids = [item["id"] for item in layout["items"]]
    assert len(layout_ids) == len(set(layout_ids))
    assert (layout["held"], layout["live"]) == (held, live)
    assert (timeline["live_message"] is not None) == live
    assert timeline["caught_up"] == known["caught_up"] and timeline["history_pages"] == known["history_pages"]
    assert (layout["settling"], layout["has_history"]) == (not known["caught_up"], known["history_pages"] > 0)

    # (1) Every message's person row is in exactly one of the timeline or the tray; a tombstone in neither.
    for mid, turn in turns.items():
        if mid == CONVERSATION_KEY:
            continue
        in_timeline, in_tray = f"person:{mid}" in layout_ids, mid in tray_ids
        if turn["origin"] == "tombstone":
            assert not in_timeline and not in_tray
        else:
            assert in_timeline != in_tray, mid
    assert layout["items"] == ref_items(timeline, tray_ids)

    # (2) The tray is the reference tray, in the order the daemon sends it.
    queue = ref_queue(timeline, known["arrival"])
    assert tray_ids == ref_tray(timeline, known["arrival"], held)

    # (3) No turn that has started is in the tray; every pending card is in the timeline.
    for mid in tray_ids:
        assert rows_of(timeline, mid) == [f"person:{mid}"]
    owners = {item["id"]: item["message_id"] for item in timeline["items"]}
    assert not any(item["type"] == "approval" and item["card"]["state"] == "pending" and item["message_id"] in tray_ids
                   for item in timeline["items"])
    assert all(row in layout_ids and owners[row] not in tray_ids for row in timeline["pending_items"])

    # (4) At most one queue message is in the timeline: a send with nothing ahead of it.
    in_timeline = [mid for mid in queue if f"person:{mid}" in layout_ids]
    assert len(in_timeline) <= 1
    if in_timeline:
        assert in_timeline == queue[:1] and turns[in_timeline[0]]["state"] == "sending"
        assert not held and not live

    # (5) Each row's Withdraw and Steer; with (2), the whole row as the rule writes it.
    candidate = tray_ids[0] if tray_ids and turns[tray_ids[0]]["origin"] in ("person", None) \
        and turns[tray_ids[0]]["state"] != "sending" else None
    steer = bool(step.get("steer")) and live
    assert layout["tray"] == [ref_row(timeline, mid, steer and mid == candidate) for mid in tray_ids]
    assert layout["title"] == ref_title([turns[mid]["origin"] for mid in tray_ids], live, held)

    # (6) The view follows a row of the timeline, never one of a message in the tray.
    followed = ref_followed_item(timeline)
    expected = followed if followed in layout_ids else (layout_ids[-1] if layout_ids else None)
    assert layout["followed"] == expected
    assert expected is None or owners.get(expected) not in tray_ids

    # (8) Previews are one line of at most 280 characters, before any image count.
    for row in layout["tray"]:
        assert "\n" not in row["preview"]
        suffix = f" ({ref_images(row['attachments'])})"
        body = row["preview"][:-len(suffix)] if row["attachments"] and row["preview"].endswith(suffix) \
            else row["preview"]
        assert len(body) <= PREVIEW_LIMIT


def labels(steps: list[dict], result: dict) -> set[str]:
    """The cases a generated timeline reached, for the statistics and the coverage test."""
    out: set[str] = set()
    known = replay(steps)
    for index, (step, snapshot) in enumerate(zip(steps, result["snapshots"])):
        layout, timeline = snapshot["layout"], snapshot["timeline"]
        rows = layout["tray"]
        tray_ids = [row["id"] for row in rows]
        layout_ids = {item["id"] for item in layout["items"]}
        if "local" in step and step["local"]["message_id"] in tray_ids:
            out.add("a send went straight to the tray")
        if "local" in step and f"person:{step['local']['message_id']}" in layout_ids:
            out.add("a send showed in the timeline")
        if index and "receipts" in step:
            before = {row["id"]: row for row in result["snapshots"][index - 1]["layout"]["tray"]}
            if any(row["id"] in before and before[row["id"]]["sending"] and not row["sending"] for row in rows):
                out.add("a send stayed in the tray when its receipt came")
        queue = ref_queue(timeline, known[index]["arrival"])
        if any(f"person:{mid}" in layout_ids for mid in queue):
            out.add("the queue's head in the timeline")
        if step.get("held") and rows:
            out.add("a held tray")
        if layout["live"] and rows:
            out.add("a tray behind a live turn")
        if not layout["live"] and not step.get("held") and rows:
            out.add("an idle tray")
        if len(rows) >= 3:
            out.add("a tray of three or more")
        if any(row["origin"] == "unblock-note" for row in rows):
            out.add("an unblock note in the tray")
        seqs = [timeline["turns"][mid]["seq"] for mid in tray_ids]
        if any(a is not None and b is not None and a > b for a, b in zip(seqs, seqs[1:])):
            out.add("a repair message ahead of an earlier message")
        if any(row["sending"] for row in rows) and any(not row["sending"] for row in rows):
            out.add("sending and queued rows together")
        if any(row["can_steer"] for row in rows):
            out.add("a steer offer")
        if any((row["status"] or "").startswith("Deferred") for row in rows):
            out.add("a deferred row")
        if any(row["attachments"] for row in rows):
            out.add("a row with images")
        if any("…" in row["preview"] for row in rows):
            out.add("a cut preview")
        if any(item["type"] == "notice" and item["text"].startswith("Withdrawn before it was sent")
               for item in layout["items"]):
            out.add("a withdrawn line")
        if any(turn["origin"] == "tombstone" for turn in timeline["turns"].values()):
            out.add("a tombstone")
        if any(item["type"] == "approval" and item["card"]["state"] == "pending" for item in layout["items"]):
            out.add("a pending card in the timeline")
        if layout["followed"] is not None and layout["followed"] != (layout["items"] or [{}])[-1].get("id"):
            out.add("followed is not the last row")
        move = result["moves"][index]
        out.add(f"move {move}")
        if move == "glide" and not step.get("at_bottom", True):
            out.add("an own send glided while reading further up")
        if move == "stay" and not step.get("at_bottom", True) and index and \
                snapshot["key"] != result["snapshots"][index - 1]["key"]:
            out.add("a change stayed while reading further up")
        if step.get("focus") and index + 1 < len(steps):
            out.add("focused again")
    return out


@PROPERTY
@given(steps=queue_timelines())
def test_c29_7_property_the_tray_holds_the_queue_as_the_daemon_sends_it(core_probe, steps):
    """C-29.7 (and C-27.5's display order) over generated timelines: the partition
    of the timeline and the tray, the tray's order, rows, Withdraw, Steer and
    title, the followed row, and the previews, against the reference."""
    result = run_queue(core_probe, steps)
    for step, snapshot, known in zip(steps, result["snapshots"], replay(steps)):
        check_layout(step, snapshot, known)
    for label in sorted(labels(steps, result)):
        event(label)


@PROPERTY
@given(steps=queue_timelines())
def test_c29_7_property_follow_moves_match_the_rule(core_probe, steps):
    """C-29.7: the view's follow key and its move after every step, against the
    rule written again (`ref_key`, `ref_move`)."""
    result = run_queue(core_probe, steps)
    old = EMPTY_KEY
    for step, snapshot, known, move in zip(steps, result["snapshots"], replay(steps), result["moves"]):
        key = ref_key(snapshot["layout"], known["caught_up"], known["history_pages"])
        assert snapshot["key"] == key
        assert move == ref_move(old, key, step.get("at_bottom", True))
        old = key
    event(f"moves: {'/'.join(sorted(set(result['moves'])))}")


follow_keys = st.fixed_dictionaries({
    "last": st.sampled_from([None, "a", "b"]), "last_is_own_send": st.booleans(),
    "followed": st.sampled_from([None, "a", "b"]), "followed_length": st.integers(0, 2),
    "tray": st.lists(st.sampled_from(["x", "y"]), max_size=2), "settling": st.booleans(),
    "has_history": st.booleans()})


@st.composite
def key_pairs(draw):
    """Two follow keys; often the second is the first with one field changed."""
    old = draw(follow_keys)
    if draw(st.booleans()):
        new = draw(follow_keys)
    else:
        field = draw(st.sampled_from(sorted(old) + [None]))
        new = dict(old) if field is None else {**old, field: draw(follow_keys)[field]}
    return {"old": old, "new": new, "at_bottom": draw(st.booleans())}


@PROPERTY
@given(pairs=st.lists(key_pairs(), min_size=1, max_size=40))
def test_c29_7_property_the_follow_move_for_any_two_keys(core_probe, pairs):
    """C-29.7: `FollowKey.move` for any two keys and either scroll position,
    against the rule written again."""
    moves = queue_words(core_probe, moves=pairs)["moves"]
    assert moves == [ref_move(pair["old"], pair["new"], pair["at_bottom"]) for pair in pairs]


WORD_ALPHABET = TEXT_ALPHABET + ["-", ":", "x", "\u2028"]


@PROPERTY
@given(previews=st.lists(st.fixed_dictionaries({"text": st.one_of(st.none(), st.text(st.sampled_from(WORD_ALPHABET),
                                                                                       max_size=40), long_texts),
                                                 "attachments": st.integers(0, 3)}), max_size=20),
       statuses=st.lists(st.one_of(st.none(), st.sampled_from(ANY_REASON + ["deferred:", "handoff-rolled-back"]),
                                   st.text(st.sampled_from(["a", "b", "-", ":", " ", "d"]), max_size=12)), max_size=20),
       visible=st.lists(st.fixed_dictionaries({"count": st.integers(-2, 12), "expanded": st.booleans(),
                                               "limit": st.integers(0, 6)}), max_size=20),
       titles=st.lists(st.fixed_dictionaries({"rows": st.lists(st.fixed_dictionaries(
           {"origin": st.sampled_from(["person", None, "unblock-note"])}), max_size=5),
           "live": st.booleans(), "held": st.booleans()}), max_size=20))
def test_c29_7_property_the_trays_words_for_any_input(core_probe, previews, statuses, visible, titles):
    """C-29.7: previews, statuses, titles and the rows shown, for any input,
    against the rules written again; a preview is one line of at most 280
    characters before its image count."""
    words = queue_words(core_probe, previews=previews, statuses=statuses, visible=visible, titles=titles)
    assert words["previews"] == [ref_preview(entry["text"], entry["attachments"]) for entry in previews]
    assert words["statuses"] == [ref_status_words(reason) for reason in statuses]
    assert words["visible"] == [ref_visible(entry["count"], entry["expanded"], entry["limit"]) for entry in visible]
    assert words["titles"] == [ref_title([row["origin"] for row in entry["rows"]], entry["live"], entry["held"])
                               for entry in titles]
    for preview, entry in zip(words["previews"], previews):
        assert "\n" not in preview and "\u2028" not in preview
        suffix = f" ({ref_images(entry['attachments'])})"
        body = preview[:-len(suffix)] if entry["attachments"] and preview.endswith(suffix) else preview
        assert len(body) <= PREVIEW_LIMIT
    for entry in visible:
        shown_rows, hidden = ref_visible(entry["count"], entry["expanded"], entry["limit"])
        assert shown_rows + hidden == max(0, entry["count"])


# The cases the property tests must reach to say anything, each in at least this many timelines of a batch
# of `COVERAGE_BATCH` drawn as they draw theirs.
REACHED = 5
COVERAGE_BATCH = 300
CASES = [
    "a send went straight to the tray", "a send showed in the timeline",
    "a send stayed in the tray when its receipt came", "the queue's head in the timeline", "a held tray",
    "a tray behind a live turn", "an idle tray", "a tray of three or more", "an unblock note in the tray",
    "a repair message ahead of an earlier message",
    "sending and queued rows together", "a steer offer", "a deferred row", "a row with images", "a cut preview",
    "a withdrawn line", "a tombstone", "a pending card in the timeline", "followed is not the last row",
    "move stay", "move jump", "move glide", "an own send glided while reading further up",
    "a change stayed while reading further up", "focused again",
]


def test_c29_7_the_generated_timelines_reach_every_case(core_probe):
    """C-29.7: the property tests are not vacuous: over a batch drawn as they draw
    theirs (the same generator and settings, derandomized, with more examples),
    every case they check comes up in several timelines."""
    seen: Counter = Counter()

    @settings(PROPERTY, max_examples=COVERAGE_BATCH)
    @given(steps=queue_timelines())
    def collect(steps):
        for label in labels(steps, run_queue(core_probe, steps)):
            seen[label] += 1

    collect()
    thin = {case: seen[case] for case in CASES if seen[case] < REACHED}
    assert not thin, (thin, dict(seen))
