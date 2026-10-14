"""Where a send that is not going through shows its notice (C-29.12, with the
queue tray of C-29.7; C-28.3).

2026-09-27: the app sent a failing `message.submit` again silently and without
end, and the message said "Sending" for good. A journaled send that fails now
has a notice (`SendNotice`, app/Sources/Outbox.swift), and the store shows it
where the message's row is (app/Sources/ConversationStore.swift `layout`,
`sendNotice`): under the bubble of the send at the timeline's end, which has
nothing ahead of it, or on its queue-tray row when it waits behind something (a
live turn, a block, an earlier message still waiting). These tests pin what the
store lays out, folded as the app folds it (the probe's `send-layout`,
tests/frontend/CoreProbeSendFailures.swift):

- P1 a message whose notice shows is a row in exactly one place, its bubble in
  the timeline or its tray row; in the focused conversation every such message
  has a row;
- P2 a tray row with a notice carries the store's notice and its status, the
  notice's actions except Withdraw, and the row's own Withdraw (the outbox's);
- P3 a send with a notice waits in the timeline only when nothing is ahead of
  it: no live turn, no block, no earlier message waiting;
- P4 a `waiting` message is always a tray row;
- P5 rows without a notice are the timeline's own (`Timeline.layout`);
- P6 the store's timeline items are the timeline's own;
- P7 a message a previous run left unsent gets its row back once the sends are
  read into a conversation that has a timeline, or when it is focused: with its
  journaled text, images and settings, idempotently, and never changing a row
  the timeline has;
- P8 the stuck send (the note above the composer), the sidebar's send problem
  and the create notices match their definitions;
- P9 `waiting` shows only while a stuck message ahead shows its own notice,
  above it.

The named examples replay the cases the change was made for. The property tests
compare the probe with a reference written again from the rules, over generated
conversations: up to 3 receipted turns in any state (a live one among them),
held or not, 1 to 4 sends journaled by this run or a previous one, each failing
in the ways the outbox classifies (C-28.3), receipts arriving while a send is
still journaled, Send now and Try again, withdrawals, a second conversation and
a new one whose create fails, and focus before and after the sends are read.
The generated steps keep the order the app keeps (UIModel): the journal is read
before the person sends anything, a send's row comes before its journal entry,
and a pump applies the sends before its receipts.
"""

from __future__ import annotations

from collections import Counter
import math
from pathlib import Path
import tempfile
import uuid

from hypothesis import HealthCheck, event, given, settings, strategies as st

from tests.frontend.conftest import needs_swift, run_probe, write_json

pytestmark = needs_swift

CID, OTHER = "cv-send-a", "cv-send-b"
CREATE = "req-new"
DRAFT = "draft:" + CREATE
SETTINGS = {"model": "opus[1m]", "effort": None, "fast": False, "permission": "ask", "auto_continue": True}
T0 = 1_790_000_000
OPEN = ("queued", "sending", "failed")
# `MessageState`: a turn with one of these has a receipt; "sending" is the app's own word before it.
MESSAGE_STATES = {"queued", "waiting", "starting", "running", "approval-needed", "complete", "failed", "interrupted",
                  "cancelled", "delivery-unknown", "unknown"}
# The states `Timeline.liveMessageID` counts as a live turn.
LIVE = {"waiting", "starting", "running", "approval-needed"}
REPAIR_ORIGINS = ("unblock-note", "failover")      # subfleet/conversations/store.py
PERSON_ACTION_REASONS = {"copy-blocked"}           # DaemonError.personActionReasons
LABELS = {"send-now": "Send now", "try-again": "Try again", "withdraw": "Withdraw"}
WAITING_STATUS = "Waiting: a message above has not been sent"
NOTICE_KEYS = ("notice", "notice_actions", "notice_line", "notice_help")

# Answers the outbox classifies (Outbox.classify, C-28.3): the error's code (nil for
# no daemon answer), its message (`summary`), the daemon's fix, and what the outbox
# does: retry (queued with a backoff), reread (out-of-order), or stop (failed).
ANSWERS = {
    "operational": (1, "operation failed; inspect daemon status", "run `subfleet daemon status`", "retry"),
    "busy": (69, "busy: the daemon is at its connection cap", "send again in a moment", "retry"),
    "no-answer": (None, "message.submit had no answer within 15 s", None, "retry"),
    "unavailable": (None, "The Subfleet daemon is not running at /tmp/sf/daemon.sock.", None, "retry"),
    "out-of-order": (2, "out-of-order: s1 is not the conversation's last person message", None, "reread"),
    "copy-blocked": (1, "copy-blocked: a directory sits at /Users/p/.subfleet/attachments/ab12.png",
                     "move that directory out of the way, then send again", "stop"),
    "bad-settings": (2, "bad-settings: settings.effort must be a string or null", None, "stop"),
    "protected": (7, "protected-workspace: /Users/p/work contains .git/hooks", "choose another workspace", "stop"),
    "too-large": (None, "the request is 2000000 bytes; the daemon takes at most 1 MiB", None, "stop"),
}


# MARK: - Inputs as the app has them


def stamp(n: int) -> str:
    return f"2026-09-28T09:{n // 60:02d}:{n % 60:02d}.000Z"


def conversation(cid: str) -> dict:
    """A conversation as `_view` returns it, with only the fields it must have."""
    return {"conversation_id": cid, "provider": "claude", "title": f"Conversation {cid[-1]}",
            "workspace": f"/tmp/{cid}", "workspace_kind": "in-place", "allow_main": False, "settings": SETTINGS,
            "origin": "app", "created_at": stamp(0), "updated_at": stamp(1), "pending_approvals": 0, "active": False}


def daemon_reason(message: str) -> str | None:
    """`DaemonError.reason`: the slug before the message's first ": "."""
    if ": " not in message:
        return None
    slug = message.split(": ", 1)[0]
    if not slug or not slug[0].isalpha() or not all(c.islower() or c.isdigit() or c == "-" for c in slug):
        return None
    return slug


def failure(answer: str, consecutive: int | None) -> dict:
    """`OutboxFailure` as `classify` records an answer, encoded as Swift encodes it
    (nil fields left out); `consecutive` None is a journal written before it existed."""
    code, message, fix, outcome = ANSWERS[answer]
    out: dict = {"message": message, "retryable": outcome != "stop"}
    if code is not None:
        out["code"] = code
        if daemon_reason(message) is not None:
            out["reason"] = daemon_reason(message)
        if fix is not None:
            out["fix"] = fix
    if consecutive is not None:
        out["consecutive"] = consecutive
    return out


def entry(key: str, order: int, *, conversation_id: str = CID, state: str = "queued", attempts: int = 0,
          failed: dict | None = None, text: str = "", attachments: tuple[str, ...] = (), waiting: bool = False,
          next_attempt_at: float | None = None, kind: str = "message.submit") -> dict:
    """`OutboxEntry` as Swift's synthesized Codable encodes it: every field that is
    not optional, and the optional ones only when set."""
    out: dict = {"key": key, "kind": kind, "order": order, "conversation": conversation_id, "state": state,
                 "attempts": attempts, "waitingForPredecessor": waiting, "createdAt": stamp(order)}
    if kind == "message.submit":
        out["message"] = {"text": text, "attachments": list(attachments), "settings": SETTINGS}
    else:
        out["create"] = {"request_id": key, "provider": "claude", "workspace": "/tmp/new", "workspace_kind": "in-place",
                         "settings": SETTINGS}
    if next_attempt_at is not None:
        out["nextAttemptAt"] = next_attempt_at
    if failed is not None:
        out["failure"] = failed
    return out


def answered(base: dict, answer: str) -> dict:
    """The entry after the daemon's answer to one more send (`begin` then `finish`)."""
    out = {key: value for key, value in base.items() if key not in ("failure", "nextAttemptAt")}
    previous = base.get("failure")
    count = (previous.get("consecutive", 1) if previous else 0) + 1
    out["failure"] = failure(answer, count)
    out["attempts"] = base["attempts"] + (0 if base["state"] == "sending" else 1)
    outcome = ANSWERS[answer][3]
    out["waitingForPredecessor"] = outcome == "reread"
    if outcome == "stop":
        out["state"] = "failed"
    else:
        out["state"] = "queued"
        out["nextAttemptAt"] = T0 + min(30.0, 0.5 * 2 ** max(0, out["attempts"] - 1))
    return out


def failing(key: str, order: int, answers: list[str], **fields) -> dict:
    out = entry(key, order, **fields)
    for answer in answers:
        out = answered(out, answer)
    return out


def receipt(mid: str, seq: int, state: str, *, conversation_id: str = CID, origin: str = "person",
            reason: str | None = None, text: str | None = None) -> dict:
    out = {"message_id": mid, "conversation_id": conversation_id, "seq": seq, "origin": origin, "state": state}
    if reason is not None:
        out["state_reason"] = reason
    if text is not None:
        out["text"] = text
    return out


def tombstone(mid: str, seq: int, conversation_id: str = CID) -> dict:
    """`message.cancel`'s answer for a message the daemon never received."""
    return receipt(mid, seq, "cancelled", conversation_id=conversation_id, origin="tombstone",
                   reason="withdrawn-before-receipt", text="(withdrawn before it was received)")


def local(mid: str, text: str, images: int = 0) -> dict:
    """The composer's row, made before the send is journaled (UIModel.send)."""
    return {"local": {"message_id": mid, "text": text, "attachments": [f"sha-{mid}-{n}" for n in range(images)],
                      "settings": SETTINGS}}


def sends(*entries: dict) -> dict:
    return {"sends": list(entries)}


def run_layout(core_probe, steps: list[dict]) -> list[dict]:
    with tempfile.TemporaryDirectory(prefix="sf-sendfail-") as scratch:
        path = write_json(Path(scratch) / f"{uuid.uuid4().hex}.json",
                          {"conversation_id": CID, "conversations": [CID, OTHER], "steps": steps})
        return run_probe(core_probe, "send-layout", path)["snapshots"]


def view(snapshot: dict, cid: str = CID) -> dict:
    return snapshot["conversations"][cid]


def item_ids(snapshot: dict, cid: str = CID) -> list[str]:
    return [item["id"] for item in view(snapshot, cid)["layout"]["items"]]


def bubble(snapshot: dict, mid: str, cid: str = CID) -> dict:
    return next(item for item in view(snapshot, cid)["layout"]["items"] if item["id"] == f"person:{mid}")


def tray_row(snapshot: dict, mid: str, cid: str = CID) -> dict:
    return next(row for row in view(snapshot, cid)["layout"]["tray"] if row["id"] == mid)


def tray_ids(snapshot: dict, cid: str = CID) -> list[str]:
    return [row["id"] for row in view(snapshot, cid)["layout"]["tray"]]


# MARK: - The notice, written again from the rules (Outbox.notice, Outbox.notices)


def detail(failed: dict) -> str:
    reason, message = failed.get("reason"), failed["message"]
    return message[len(reason) + 2:] if reason is not None and message.startswith(reason + ": ") else message


def needs_person(failed: dict) -> bool:
    return failed.get("code") == 1 and daemon_reason(failed["message"]) in PERSON_ACTION_REASONS


def notice(key: str, kind: str, status: str, actions: list[str], failed: dict | None = None,
           then: str | None = None, failures: int = 0) -> dict:
    return {"key": key, "kind": kind, "status": status, "detail": detail(failed) if failed else None,
            "reason": failed.get("reason") if failed else None, "fix": failed.get("fix") if failed else None,
            "then": then, "failures": failures, "actions": actions, "labels": [LABELS[a] for a in actions]}


def ref_notice(item: dict) -> dict | None:
    """One open entry's notice: at once when it stopped, from the second failed
    answer in a row while the outbox keeps trying."""
    failed = item.get("failure")
    if item["state"] not in OPEN or failed is None:
        return None
    withdraw = ["withdraw"] if item["kind"] == "message.submit" else []
    count = failed.get("consecutive", 1)
    if item["state"] == "failed":
        if needs_person(failed):
            return notice(item["key"], "needs-person", "Not sent: this needs you first", ["try-again"] + withdraw,
                          failed, "When that is done, choose Try again.", count)
        status = "Not sent: the app cannot send it" if failed.get("code") is None else "Not sent: the daemon refused it"
        return notice(item["key"], "refused", status, ["try-again"] + withdraw, failed, None, count)
    if count < 2:
        return None
    return notice(item["key"], "retrying", f"Not sent yet: {count} tries failed; the app keeps trying",
                  (["send-now"] if item["state"] == "queued" else []) + withdraw, failed, None, count)


def ref_notices(entries: list[dict]) -> dict[str, dict]:
    """Every open entry's notice; a message behind one of its conversation's
    messages that has a notice waits, unless it has a notice of its own."""
    out, stuck = {}, set()
    for item in sorted((e for e in entries if e["state"] in OPEN), key=lambda e: e["order"]):
        own = ref_notice(item)
        if own is not None:
            out[item["key"]] = own
            if item["kind"] == "message.submit":
                stuck.add(item["conversation"])
        elif item["kind"] == "message.submit" and item["conversation"] in stuck:
            out[item["key"]] = notice(item["key"], "waiting", WAITING_STATUS, ["withdraw"])
    return out


def row_line(shown: dict | None) -> str | None:
    if shown is None or shown["kind"] == "waiting":
        return None
    return "To fix: " + shown["fix"] if shown["fix"] is not None else shown["detail"]


def row_help(shown: dict | None) -> str | None:
    if shown is None:
        return None
    parts = [shown["detail"], "To fix: " + shown["fix"] if shown["fix"] is not None else None, shown["then"],
             "The daemon's reason: " + shown["reason"] if shown["reason"] is not None else None]
    parts = [part for part in parts if part is not None]
    return "\n".join(parts) if parts else None


# MARK: - Named cases


def test_c29_12_a_lone_failing_send_shows_its_notice_under_its_bubble(core_probe):
    """(a) C-29.12: the send with nothing ahead of it stays at the timeline's end
    (C-29.7), and its notice goes under its bubble, never in the tray: nothing
    after one failed answer, `retrying` from the second, and `needs-person` at
    once for a fix that is the person's (`copy-blocked`)."""
    first = entry("s1", 1, text="Look at this", attachments=("sha-s1-0",))
    steps = [{"upsert": [conversation(CID)]}, {"focus": CID}, local("s1", "Look at this", images=1), sends(first),
             sends(once := answered(first, "operational")), sends(twice := answered(once, "operational")),
             sends(stopped := answered(twice, "copy-blocked"))]
    snaps = run_layout(core_probe, steps)
    for snap in snaps[3:5]:
        assert bubble(snap, "s1")["notice"] is None and view(snap)["stuck"] is None
        assert view(snap)["send_problem"] is None and view(snap)["sidebar"] is None
    retrying = snaps[5]
    assert tray_ids(retrying) == [] and "person:s1" in item_ids(retrying)
    assert bubble(retrying, "s1")["notice"] == notice(
        "s1", "retrying", "Not sent yet: 2 tries failed; the app keeps trying", ["send-now", "withdraw"],
        twice["failure"], None, 2)
    assert bubble(retrying, "s1")["notice"]["detail"] == "operation failed; inspect daemon status"
    assert view(retrying)["send_problem"] == view(retrying)["sidebar"] == "retrying"
    last = snaps[-1]
    shown = bubble(last, "s1")["notice"]
    assert shown == notice("s1", "needs-person", "Not sent: this needs you first", ["try-again", "withdraw"],
                           stopped["failure"], "When that is done, choose Try again.", 3)
    assert shown["detail"] == "a directory sits at /Users/p/.subfleet/attachments/ab12.png"
    assert shown["labels"] == ["Try again", "Withdraw"]
    assert tray_ids(last) == [] and bubble(last, "s1")["state"] == "sending"
    assert view(last)["stuck"] == shown and view(last)["notices"]["s1"] == shown
    assert view(last)["send_problem"] == view(last)["sidebar"] == "needs-person"
    assert last["create_notices"] == []


def test_c29_12_the_same_send_behind_a_live_turn_carries_its_notice_on_its_tray_row(core_probe):
    """(b) C-29.12 and C-29.7: behind a live turn (or a block) the send is a tray
    row, and the row carries the notice: its status replaces "Sending", a line
    gives the fix, Try again or Send now sits beside the row's own Withdraw, and
    Withdraw appears once (the outbox's, D-22)."""
    first = entry("s1", 1, text="Look at this")
    stopped = failing("s1", 1, ["copy-blocked"], text="Look at this")
    retrying = failing("s1", 1, ["no-answer", "operational"], text="Look at this")
    live = [{"upsert": [conversation(CID)]}, {"receipts": [receipt("r1", 1, "running", text="earlier")]},
            {"focus": CID}, local("s1", "Look at this"), sends(first)]
    snaps = run_layout(core_probe, live + [sends(stopped), sends(retrying)])
    held = run_layout(core_probe, [{"upsert": [conversation(CID)]}, {"focus": CID}, local("s1", "Look at this"),
                                   {**sends(stopped), "held": {CID: True}}])
    for snap, shown, actions in ((snaps[5], stopped, ["try-again"]), (snaps[6], retrying, ["send-now"]),
                                 (held[-1], stopped, ["try-again"])):
        expected = ref_notice(shown)
        row = tray_row(snap, "s1")
        assert "person:s1" not in item_ids(snap)
        assert row["notice"] == expected == view(snap)["notices"]["s1"] == view(snap)["stuck"]
        assert row["status"] == expected["status"] and row["sending"] is True
        assert row["notice_actions"] == actions
        assert row["withdraw"] == {"action": "withdraw", "message_id": "s1"} and row["can_withdraw"] is True
        assert row["notice_line"] == "To fix: " + shown["failure"]["fix"]
        assert row["notice_help"] == row_help(expected)
    assert tray_row(snaps[4], "s1")["status"] == "Sending" and tray_row(snaps[4], "s1")["notice"] is None
    assert tray_row(snaps[5], "s1")["notice_help"] == (
        "a directory sits at /Users/p/.subfleet/attachments/ab12.png\n"
        "To fix: move that directory out of the way, then send again\n"
        "When that is done, choose Try again.\nThe daemon's reason: copy-blocked")
    assert tray_row(snaps[6], "s1")["status"] == "Not sent yet: 2 tries failed; the app keeps trying"
    assert view(snaps[5])["layout"]["title"] == "1 message queued; it goes when this turn ends"
    assert view(held[-1])["layout"]["title"] == "1 message queued; it waits until the conversation can continue"


def test_c29_12_a_message_behind_a_stuck_bubble_waits_on_a_tray_row(core_probe):
    """(c) C-29.12: the stuck head keeps its bubble and notice; the message after
    it is a tray row that says it waits, with only Withdraw, and no fix line."""
    stuck = failing("s1", 1, ["bad-settings"], text="first")
    steps = [{"upsert": [conversation(CID)]}, {"focus": CID}, local("s1", "first"), local("s2", "second"),
             sends(stuck, entry("s2", 2, text="second"))]
    last = run_layout(core_probe, steps)[-1]
    head = bubble(last, "s1")["notice"]
    assert head == ref_notice(stuck) and head["kind"] == "refused"
    assert head["status"] == "Not sent: the daemon refused it"
    assert head["detail"] == "settings.effort must be a string or null"
    assert tray_ids(last) == ["s2"] and "person:s2" not in item_ids(last)
    row = tray_row(last, "s2")
    assert row["notice"] == notice("s2", "waiting", WAITING_STATUS, ["withdraw"])
    assert row["status"] == WAITING_STATUS and row["notice_actions"] == []
    assert row["notice_line"] is None and row["notice_help"] is None
    assert row["withdraw"] == {"action": "withdraw", "message_id": "s2"}
    assert view(last)["stuck"] == head and view(last)["send_problem"] == "refused"
    assert view(last)["layout"]["title"] == "1 message queued; it goes next"


def test_c29_12_after_a_restart_an_unsent_message_gets_its_row_back_when_focused(core_probe):
    """(d) C-29.12 and C-28.3: the outbox a previous run left holds a stopped send
    and one behind it. The sidebar and the stuck send say so at once; the
    timeline a receipt made has no row for them until the conversation is
    focused, and then each has its row with its journaled text and images, in
    the order the outbox sends them, the first with its notice under its bubble
    and the second waiting on a tray row. Focusing again changes nothing."""
    stuck = failing("s1", 1, ["copy-blocked"], text="the chart, please", attachments=("sha-a", "sha-b"))
    behind = entry("s2", 2, text="and the table")
    steps = [{"upsert": [conversation(CID)]}, sends(stuck, behind),
             {"receipts": [receipt("r1", 1, "complete", text="earlier")]}, {"focus": CID}, {"focus": CID}]
    before, unrestored, focused, again = run_layout(core_probe, steps)[1:]
    assert view(before)["timeline"] is None and view(before)["layout"] is None
    assert view(before)["sidebar"] == view(before)["send_problem"] == "needs-person"
    assert view(before)["stuck"] == ref_notice(stuck)
    assert item_ids(unrestored) == ["person:r1"] and tray_ids(unrestored) == []
    assert set(view(unrestored)["timeline"]["turns"]) == {"r1"}
    assert view(unrestored)["notices"]["s1"] == ref_notice(stuck)       # it shows nowhere yet: no row
    assert focused["did"]["idempotent"] is True
    assert item_ids(focused) == ["person:r1", "person:s1"] and tray_ids(focused) == ["s2"]
    assert bubble(focused, "s1")["notice"] == ref_notice(stuck)
    assert bubble(focused, "s1")["text"] == "the chart, please" and bubble(focused, "s1")["attachments"] == ["sha-a", "sha-b"]
    assert tray_row(focused, "s2")["notice"]["kind"] == "waiting"
    assert tray_row(focused, "s2")["preview"] == "and the table"
    turns = view(focused)["timeline"]["turns"]
    assert [turns[mid]["state"] for mid in ("s1", "s2")] == ["sending", "sending"]
    assert turns["s1"]["settings"] == SETTINGS and turns["s1"]["origin"] == "person"
    assert view(focused)["timeline"]["order"] == ["r1", "s1", "s2"]
    assert view(again)["timeline"] == view(focused)["timeline"] and view(again)["layout"] == view(focused)["layout"]


def test_c29_12_a_receipt_takes_the_notice_away_everywhere(core_probe):
    """(e) C-29.12: once the app has the daemon's receipt for a message, its notice
    is gone from its row, the stuck note and the sidebar, and the message behind
    it no longer says it waits, although the outbox still holds both until the
    next send is answered."""
    retrying = failing("s1", 1, ["no-answer", "no-answer"], text="one")
    behind = entry("s2", 2, text="two")
    tray = [{"upsert": [conversation(CID)]}, {"receipts": [receipt("r1", 1, "running")]}, {"focus": CID},
            local("s1", "one"), local("s2", "two"), sends(retrying, behind),
            {"receipts": [receipt("s1", 2, "queued")]}]
    before, after = run_layout(core_probe, tray)[-2:]
    assert tray_ids(before) == ["s1", "s2"]
    assert tray_row(before, "s1")["notice"]["kind"] == "retrying" and tray_row(before, "s2")["notice"]["kind"] == "waiting"
    assert view(before)["stuck"]["key"] == "s1" and view(before)["sidebar"] == "retrying"
    assert after["sends"] == ["s1", "s2"] and after["send_notices"]["s2"]["kind"] == "waiting"
    assert tray_ids(after) == ["s1", "s2"]
    first, second = tray_row(after, "s1"), tray_row(after, "s2")
    assert first["notice"] is None and first["status"] is None and first["sending"] is False
    assert first["withdraw"] == {"action": "cancel", "message_id": "s1"}
    assert second["notice"] is None and second["status"] == "Sending"
    assert view(after)["notices"] == {"s1": None, "s2": None, "r1": None}
    assert view(after)["stuck"] is None and view(after)["send_problem"] is None and view(after)["sidebar"] is None
    # The head bubble: the notice goes when the receipt comes.
    stopped = failing("s1", 1, ["copy-blocked"], text="one")
    bubble_steps = [{"upsert": [conversation(CID)]}, {"focus": CID}, local("s1", "one"), sends(stopped),
                    {"receipts": [receipt("s1", 1, "running")]}]
    before, after = run_layout(core_probe, bubble_steps)[-2:]
    assert bubble(before, "s1")["notice"]["kind"] == "needs-person"
    assert bubble(after, "s1")["notice"] is None and bubble(after, "s1")["state"] == "running"
    assert view(after)["stuck"] is None and view(after)["sidebar"] is None


def test_c29_12_a_create_that_is_not_going_through_is_a_create_notice_only(core_probe):
    """(f) C-29.12: a new conversation's create has no conversation to show in: its
    notice is a create notice (the main window's banner), with Send now or Try
    again and no Withdraw, and it touches no conversation. Its first message,
    journaled under the draft, does not say it waits: only a message holds the
    ones behind it."""
    retrying = failing(CREATE, 3, ["operational", "operational"], kind="conversation.create",
                       conversation_id=DRAFT)
    refused = failing(CREATE, 3, ["protected"], kind="conversation.create", conversation_id=DRAFT)
    first = entry("d1", 4, conversation_id=DRAFT, text="hello there")
    steps = [{"upsert": [conversation(CID), conversation(OTHER)]}, {"focus": CID},
             {"receipts": [receipt("r1", 1, "complete")]}, sends(retrying, first), sends(refused, first)]
    for snap, expected in zip(run_layout(core_probe, steps)[-2:], (retrying, refused)):
        assert snap["create_notices"] == [ref_notice(expected)]
        assert "withdraw" not in snap["create_notices"][0]["actions"]
        assert snap["send_notices"] == {CREATE: ref_notice(expected)}
        for cid in (CID, OTHER):
            assert view(snap, cid)["stuck"] is None and view(snap, cid)["send_problem"] is None
            assert view(snap, cid)["sidebar"] is None
        assert view(snap)["layout"]["tray"] == [] and all(item.get("notice") is None
                                                          for item in view(snap)["layout"]["items"])
    last = run_layout(core_probe, steps)[-1]["create_notices"][0]
    assert last["status"] == "Not sent: the daemon refused it" and last["actions"] == ["try-again"]
    assert last["detail"] == "/Users/p/work contains .git/hooks" and last["fix"] == "choose another workspace"
