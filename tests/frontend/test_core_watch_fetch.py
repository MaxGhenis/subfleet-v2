"""Messages the watch feed names that this app did not send (C-29.9; design D-24, §12).

`ConversationStoreState.apply(watch:)` moved a message only when its
conversation's timeline already had it. So a message this app did not send (the
CLI's or another client's `message.submit`, the note `conversation.unblock`
leaves, a failover continuation) entered no timeline, and so not the queue the
person watches, until the conversation was opened again or the message's own
events arrived; and one its events brought had no text. Now a watch row naming a
message that a conversation's timeline has no receipt for asks for it with
`message.status`: at most 200 ids a call, one batch at a time, on the outbox
queue (UIModel `fetchNamedMessages`), never on the feed's thread.

Invariants, for every input:

- Selection: an id is asked for only after a row named it while its
  conversation had a timeline and that timeline had no turn for it, or only one
  its events or an approval made. This app's own sends are never asked for, nor
  is anything in a conversation with no timeline.
- Bookkeeping: no id waits twice or waits while it is asked for; the ids named
  again while asked for are a subset of the batch out.
- Batches: at most `limit` ids, oldest first (the order the feed first named
  them), none while a batch is out; a failed batch waits again ahead of the rest.
- Daemon order: every timeline is in sequence order, with the turns no receipt
  numbered after them; an answered message has its receipt's sequence, origin,
  continues and text.
- No regression: an answer for an id the feed named again while it was asked
  for never changes where a turn the timeline already has stands; the id is
  asked for again.
- Convergence: once the feed is read to its end and the fetches are drained,
  every message a row named while its conversation had a timeline (this app's
  own unacknowledged sends aside) shows the daemon's state, sequence, origin and
  text.

The property tests check them on the probe's state after every step, and
compare that state with a reference written again from the rules (`Model`).
The example tests use the daemon's own JSON; the end-to-end tests run the app's
engine against the daemon's service on a socket while a second client, the
Python client the CLI uses, submits.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import uuid

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet.client import Client
from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import (
    ServiceHarness, ServiceServer, claude_assistant, claude_init, claude_result,
)

pytestmark = needs_swift

LIMIT = 200                                    # MessageStatusArgs.limit, service.py op_message_status
BEFORE_ACCEPT = ("sending", "queued", "waiting", "starting")
TERMINAL = ("complete", "failed", "interrupted", "cancelled")
NOTE = "The stopped turn was left; the next turn starts without resuming it"
CONTINUED = "Continued after a usage limit"


def fold(core_probe, steps: list[dict]) -> list[dict]:
    """The probe's `watch-fetch`: the state after every step."""
    with tempfile.TemporaryDirectory(prefix="sf-wf-") as scratch:
        out = run_probe(core_probe, "watch-fetch", write_json(Path(scratch) / "steps.json", {"steps": steps}))
    return out["snapshots"]


def page(rows: list[dict]) -> dict:
    return {"changes": rows, "next": max((r["seq"] for r in rows), default=0)}


# MARK: - The reference


@dataclass
class Turn:
    arrival: int
    seq: int | None = None
    origin: str | None = None
    continues: str | None = None
    state: str = "sending"
    state_reason: str | None = None
    person_text: str | None = None
    stop_requested: bool = False

    @property
    def lacks_receipt(self) -> bool:
        return self.seq is None and self.origin is None

    def view(self) -> dict:
        return {"seq": self.seq, "origin": self.origin, "continues": self.continues, "state": self.state,
                "state_reason": self.state_reason, "person_text": self.person_text,
                "stop_requested": self.stop_requested, "lacks_receipt": self.lacks_receipt}


@dataclass
class TimelineModel:
    turns: dict[str, Turn] = field(default_factory=dict)
    cursor: int = 0

    def ensure(self, mid: str) -> Turn:
        if mid not in self.turns:
            self.turns[mid] = Turn(arrival=len(self.turns))
        return self.turns[mid]

    def receipt(self, r: dict, *, standing: bool = True) -> None:
        turn = self.ensure(r["message_id"])
        if standing:
            if r["state"] != "unknown":
                turn.state, turn.state_reason = r["state"], r.get("state_reason")
            if r.get("stop_requested") is not None:
                turn.stop_requested = r["stop_requested"]
        for key in ("seq", "origin", "continues"):
            if r.get(key) is not None:
                setattr(turn, key, r[key])
        if turn.person_text is None and r.get("text") is not None:
            turn.person_text = r["text"] + ("\n…" if r.get("text_truncated") else "")

    def local(self, mid: str, text: str) -> None:
        turn = self.ensure(mid)
        turn.person_text = text
        turn.origin = turn.origin or "person"

    def events(self, events_page: dict) -> None:
        for event in sorted(events_page["events"], key=lambda e: e["seq"]):
            if event["seq"] <= self.cursor:
                continue
            turn = self.ensure(event["message_id"])
            if event["kind"] == "accepted" and turn.state in BEFORE_ACCEPT:
                turn.state = "running"
            self.cursor = event["seq"]
        self.cursor = max(self.cursor, events_page["next"])

    def order(self) -> list[str]:
        return sorted(self.turns, key=lambda m: (math.inf if self.turns[m].seq is None else self.turns[m].seq,
                                                 self.turns[m].arrival))

    def view(self) -> dict:
        return {"order": self.order(), "turns": {m: t.view() for m, t in self.turns.items()}}


@dataclass
class Fetches:
    wanted: list[str] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)
    stale: set[str] = field(default_factory=set)

    def named(self, mid: str) -> None:
        if mid in self.asked:
            self.stale.add(mid)

    def want(self, mid: str) -> None:
        if mid not in self.wanted and mid not in self.asked:
            self.wanted.append(mid)

    def take(self, limit: int) -> list[str]:
        if self.asked or limit <= 0:
            return []
        self.asked, self.wanted = self.wanted[:limit], self.wanted[limit:]
        return list(self.asked)

    def answered(self, batch: list[str]) -> set[str]:
        renamed = self.stale & set(batch)
        self.asked = [m for m in self.asked if m not in batch]
        self.stale -= set(batch)
        return renamed

    def failed(self, batch: list[str]) -> None:
        back = [m for m in self.asked if m in batch]
        self.asked = [m for m in self.asked if m not in batch]
        self.stale -= set(batch)
        self.wanted = back + [m for m in self.wanted if m not in back]

    def view(self) -> dict:
        return {"wanted": list(self.wanted), "asked": list(self.asked), "stale": sorted(self.stale)}


class Model:
    """`ConversationStoreState` as far as C-29.9's fetches go, written again from the rules."""

    def __init__(self) -> None:
        self.timelines: dict[str, TimelineModel] = {}
        self.fetches = Fetches()
        self.cursor = 0
        self.batch: list[str] = []

    def timeline(self, cid: str) -> TimelineModel:
        return self.timelines.setdefault(cid, TimelineModel())

    def step(self, step: dict) -> list[str] | None:
        taken = None
        if "focus" in step:
            self.timeline(step["focus"])
        for r in step.get("receipts", []):
            self.timeline(r["conversation_id"]).receipt(r)
        if "local" in step:
            local = step["local"]
            self.timeline(local["conversation_id"]).local(local["message_id"], local["text"])
        if "events" in step:
            self.timeline(step["conversation_id"]).events(step["events"])
        if "watch" in step:
            self.watch(step["watch"])
        if "take" in step:
            taken = self.fetches.take(step["take"])
            if taken:
                self.batch = taken
        if "answer" in step:
            self.answer(step["answer"])
        if step.get("fail"):
            self.fetches.failed(self.batch)
            self.batch = []
        return taken

    def watch(self, watch_page: dict) -> None:
        for row in sorted(watch_page["changes"], key=lambda r: r["seq"]):
            if row["seq"] <= self.cursor:
                continue
            cid, mid, state = row["conversation_id"], row.get("message_id"), row.get("state")
            timeline = self.timelines.get(cid)
            if mid and state and timeline and mid in timeline.turns:
                turn = timeline.turns[mid]
                if turn.state != state or turn.state_reason != row.get("state_reason"):
                    timeline.receipt({"message_id": mid, "conversation_id": cid, "state": state,
                                      "state_reason": row.get("state_reason")})
            if mid:
                self.fetches.named(mid)
                if timeline is not None and (mid not in timeline.turns or timeline.turns[mid].lacks_receipt):
                    self.fetches.want(mid)
            self.cursor = max(self.cursor, row["seq"])
        self.cursor = max(self.cursor, watch_page["next"])

    def answer(self, receipts: list[dict]) -> None:
        batch, self.batch = self.batch, []
        renamed = self.fetches.answered(batch)
        answers: dict[str, dict] = {}
        for r in receipts:
            answers.setdefault(r["message_id"], r)
        for mid in batch:
            r = answers.get(mid)
            if r is None or r["state"] == "unknown" or not r.get("conversation_id") \
                    or r["conversation_id"] not in self.timelines:
                continue
            timeline = self.timelines[r["conversation_id"]]
            if mid in renamed:
                self.fetches.want(mid)
                if mid in timeline.turns:
                    timeline.receipt(r, standing=False)
                    continue
            timeline.receipt(r)

    def view(self) -> dict:
        return {"fetches": self.fetches.view(), "watch_cursor": self.cursor,
                "timelines": {cid: t.view() for cid, t in self.timelines.items()}}


def comparable(snapshot: dict) -> dict:
    """The probe's snapshot in the reference's terms."""
    return {"fetches": snapshot["fetches"], "watch_cursor": snapshot["watch_cursor"],
            "timelines": {cid: {"order": t["order"], "turns": {m: {k: v for k, v in turn.items()
                                                                     if k != "status_text"}
                                                                for m, turn in t["turns"].items()}}
                          for cid, t in snapshot["timelines"].items()}}


# MARK: - A daemon to draw from


class World:
    """What the daemon holds: messages, their states, and the change feed it writes."""

    CONVERSATIONS = ("cv-a", "cv-b", "cv-c")

    def __init__(self) -> None:
        self.messages: dict[str, dict] = {}
        self.seqs: Counter[str] = Counter()
        self.rows: list[dict] = []
        self.delivered = 0
        self.event_seq: Counter[str] = Counter()

    def row(self, cid: str, mid: str, state: str | None, reason: str | None = None) -> None:
        self.rows.append({"seq": len(self.rows) + 1, "conversation_id": cid, "message_id": mid, "state": state,
                          "state_reason": reason, "pending_approvals": 0, "ts": None})

    def submit(self, cid: str, origin: str) -> str:
        mid = f"m{len(self.messages) + 1:02d}"
        self.seqs[cid] += 1
        earlier = [m for m, v in self.messages.items() if v["conversation_id"] == cid]
        state = "cancelled" if origin == "tombstone" else "queued"
        self.messages[mid] = {"conversation_id": cid, "seq": self.seqs[cid], "origin": origin,
                              "continues": earlier[-1] if origin == "failover" and earlier else None,
                              "state": state, "state_reason": "withdrawn-before-receipt" if origin == "tombstone"
                              else None, "text": f"text of {mid}", "stop": False}
        self.row(cid, mid, state, self.messages[mid]["state_reason"])
        return mid

    def advance(self, mid: str, state: str, reason: str | None) -> None:
        message = self.messages[mid]
        message["state"], message["state_reason"] = state, reason
        if state == "interrupted":
            message["stop"] = True
        self.row(message["conversation_id"], mid, state, reason)

    def receipt(self, mid: str, *, unknown: bool = False) -> dict:
        if unknown:
            return {"message_id": mid, "state": "unknown"}
        m = self.messages[mid]
        return {"message_id": mid, "conversation_id": m["conversation_id"], "seq": m["seq"], "origin": m["origin"],
                "continues": m["continues"], "state": m["state"], "state_reason": m["state_reason"],
                "text": m["text"], "text_truncated": False, "stop_requested": m["stop"]}

    def deliver(self, count: int, *, again: bool = False) -> dict:
        rows = self.rows[self.delivered:self.delivered + count]
        replay = self.rows[max(0, self.delivered - 2):self.delivered] if again else []
        self.delivered += len(rows)
        return {"changes": replay + rows, "next": self.delivered}


@dataclass
class Scenario:
    steps: list[dict]
    model: Model
    world: World
    own: set[str]
    #: The messages a row named while their conversation had a timeline.
    named_with_timeline: set[str]
    #: Messages an answer said the daemon does not have: dropped, not asked again.
    answered_unknown: set[str]
    taken: list[list[str] | None]


def draw_scenario(data, *, drain: bool) -> Scenario:
    world, model = World(), Model()
    steps: list[dict] = []
    own: set[str] = set()
    timeline_convs: set[str] = set()
    named_with_timeline: set[str] = set()
    asked_at: dict[str, dict] = {}
    answered_unknown: set[str] = set()
    taken: list[list[str] | None] = []

    def emit(step: dict) -> None:
        for cid in [step.get("focus"), step.get("conversation_id"), (step.get("local") or {}).get("conversation_id"),
                    *(r.get("conversation_id") for r in step.get("receipts", []))]:
            if cid:
                timeline_convs.add(cid)
        if "watch" in step:
            for row in step["watch"]["changes"]:
                if row["seq"] > model.cursor and row["conversation_id"] in timeline_convs:
                    named_with_timeline.add(row["message_id"])
        steps.append(step)
        result = model.step(step)
        taken.append(result)
        if result:
            for mid in result:
                asked_at[mid] = world.receipt(mid)

    def answer(mode: str = "any") -> None:
        """Answer the batch out: each id as the daemon holds it now, as it held
        it when asked (an answer older than rows folded since), or unknown."""
        receipts = []
        for mid in model.batch:
            old = mode == "old" or (mode == "any" and data.draw(st.booleans(), label=f"old answer for {mid}"))
            unknown = mode == "any" and data.draw(st.integers(0, 9), label=f"unknown {mid}") == 0
            if unknown:
                answered_unknown.add(mid)
            receipts.append(world.receipt(mid, unknown=True) if unknown else asked_at[mid] if old
                            else world.receipt(mid))
        emit({"answer": receipts})

    def start(mid: str) -> None:
        """The dispatcher starts it: its `accepted` event, and the row."""
        cid = world.messages[mid]["conversation_id"]
        world.event_seq[cid] += 1
        world.advance(mid, "running", None)
        n = world.event_seq[cid]
        emit({"conversation_id": cid, "events": {"events": [
            {"seq": n, "message_id": mid, "kind": "accepted", "ts": None, "data": {}}], "next": n, "reset": False}})

    def advance(mid: str) -> None:
        if world.messages[mid]["state"] not in TERMINAL:
            state = data.draw(st.sampled_from(["waiting", "starting", "running", "approval-needed", "complete",
                                               "failed", "interrupted", "cancelled", "delivery-unknown"]),
                              label="state")
            world.advance(mid, state, data.draw(st.sampled_from([None, "capacity", "lease-held", "usage-limit"]),
                                                label="reason"))

    def submit_elsewhere() -> str:
        """Another client's message in a conversation with a timeline."""
        cid = data.draw(st.sampled_from(sorted(timeline_convs) or list(World.CONVERSATIONS)), label="conversation")
        if cid not in timeline_convs:
            emit({"focus": cid})
        return world.submit(cid, data.draw(st.sampled_from(["person", "unblock-note", "failover"]), label="origin"))

    def race() -> None:
        """A row names a message while its batch is out, and the answer, asked
        before that row, comes after it; the turn may exist by then."""
        if not model.batch:
            submit_elsewhere()
            emit({"watch": world.deliver(len(world.rows))})
            emit({"take": data.draw(st.sampled_from([1, LIMIT]), label="limit")})
        if not model.batch:
            return
        mid = data.draw(st.sampled_from(model.batch), label="raced")
        how = data.draw(st.sampled_from(["event", "receipt", "row only"]), label="how")
        if how == "event" and world.messages[mid]["state"] in BEFORE_ACCEPT:
            start(mid)
        elif how == "receipt":
            emit({"receipts": [world.receipt(mid)]})
        advance(mid)
        emit({"watch": world.deliver(len(world.rows))})
        answer("old")

    # Most scenarios look at a conversation from the start (the focused one).
    if data.draw(st.booleans(), label="focus first"):
        emit({"focus": data.draw(st.sampled_from(World.CONVERSATIONS), label="conversation")})
    kinds = ["submit", "submit", "own", "advance", "advance", "approval", "event", "focus", "receipt",
             "watch", "watch", "watch", "take", "take", "answer", "answer", "fail", "race", "race"]
    for _ in range(data.draw(st.integers(1, 36), label="steps")):
        kind = data.draw(st.sampled_from(kinds), label="kind")
        mids = sorted(world.messages)
        if kind in ("submit", "own"):
            cid = data.draw(st.sampled_from(World.CONVERSATIONS), label="conversation")
            origin = "person" if kind == "own" else data.draw(
                st.sampled_from(["person", "person", "unblock-note", "failover", "tombstone"]), label="origin")
            mid = world.submit(cid, origin)
            if kind == "own":
                own.add(mid)
                emit({"local": {"conversation_id": cid, "message_id": mid, "text": world.messages[mid]["text"]}})
        elif kind == "advance" and mids:
            advance(data.draw(st.sampled_from(mids), label="message"))
        elif kind == "approval" and mids:
            mid = data.draw(st.sampled_from(mids), label="message")
            world.row(world.messages[mid]["conversation_id"], mid, None)
        elif kind == "event" and mids:
            mid = data.draw(st.sampled_from(mids), label="message")
            if world.messages[mid]["state"] in BEFORE_ACCEPT:
                start(mid)
        elif kind == "focus":
            emit({"focus": data.draw(st.sampled_from(World.CONVERSATIONS), label="conversation")})
        elif kind == "receipt" and mids:
            emit({"receipts": [world.receipt(data.draw(st.sampled_from(mids), label="message"))]})
        elif kind == "watch":
            emit({"watch": world.deliver(data.draw(st.integers(0, 5), label="rows"),
                                         again=data.draw(st.booleans(), label="replay"))})
        elif kind == "take":
            emit({"take": data.draw(st.sampled_from([1, 2, 3, LIMIT]), label="limit")})
        elif kind == "answer" and model.batch:
            answer()
        elif kind == "fail" and model.batch:
            if data.draw(st.booleans(), label="more waiting"):
                submit_elsewhere()
                emit({"watch": world.deliver(len(world.rows))})
            emit({"fail": True})
        elif kind == "race":
            race()
    if drain:
        emit({"watch": world.deliver(len(world.rows))})
        if model.batch:
            answer("fresh")
        while True:
            emit({"take": LIMIT})
            if not model.batch:
                break
            answer("fresh")
    return Scenario(steps, model, world, own, named_with_timeline, answered_unknown, taken)


# MARK: - Properties


def check_state(snapshot: dict) -> None:
    fetches = snapshot["fetches"]
    wanted, asked, stale = fetches["wanted"], fetches["asked"], set(fetches["stale"])
    assert len(wanted) == len(set(wanted)) and len(asked) == len(set(asked))
    assert not set(wanted) & set(asked)
    assert stale <= set(asked)
    for timeline in snapshot["timelines"].values():
        seqs = [timeline["turns"][m]["seq"] for m in timeline["order"]]
        numbered = [s for s in seqs if s is not None]
        assert numbered == sorted(numbered), timeline["order"]                  # daemon order
        assert seqs[:len(numbered)] == numbered                                 # unnumbered turns after


@given(data=st.data())
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
def test_c29_9_property_the_probe_matches_the_rules_after_every_step(core_probe, data):
    """Differential and invariants: after every step the probe's fetches and
    timelines equal the reference's, and the invariants in the module docstring
    hold."""
    scenario = draw_scenario(data, drain=False)
    snapshots = fold(core_probe, scenario.steps)
    reference = Model()
    before: dict | None = None
    for step, snapshot, taken in zip(scenario.steps, snapshots, scenario.taken):
        wanted_before = list(reference.fetches.wanted)
        batch_before, stale_before = list(reference.batch), set(reference.fetches.stale)
        assert reference.step(step) == taken
        assert comparable(snapshot) == reference.view(), step
        check_state(snapshot)
        everything = set(snapshot["fetches"]["wanted"]) | set(snapshot["fetches"]["asked"])
        assert not everything & scenario.own                                    # never this app's own send
        assert everything <= scenario.named_with_timeline
        if "take" in step:
            assert snapshot["taken"] == taken and len(taken) <= step["take"]
            if taken:
                assert taken == wanted_before[:len(taken)]                      # oldest first
            if batch_before and before and before["fetches"]["asked"]:
                assert taken == []                                              # one batch at a time
        if step.get("fail") and batch_before:
            assert snapshot["fetches"]["wanted"][:len(batch_before)] == batch_before
        if "answer" in step and before is not None:
            answers = {r["message_id"]: r for r in step["answer"]}
            for mid in batch_before:
                r = answers[mid]
                if r["state"] == "unknown":
                    continue
                turn = snapshot["timelines"][r["conversation_id"]]["turns"][mid]
                assert (turn["seq"], turn["origin"], turn["person_text"]) == (r["seq"], r["origin"], r["text"])
                had = before["timelines"].get(r["conversation_id"], {}).get("turns", {}).get(mid)
                if mid in stale_before:
                    assert mid in snapshot["fetches"]["wanted"]                  # asked again
                    if had is not None:                                          # never regressed
                        assert (turn["state"], turn["state_reason"], turn["stop_requested"]) == \
                            (had["state"], had["state_reason"], had["stop_requested"])
                else:
                    assert (turn["state"], turn["state_reason"]) == (r["state"], r["state_reason"])
        before = snapshot


@given(data=st.data())
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
def test_c29_9_property_the_timelines_converge_on_the_daemon(core_probe, data):
    """Convergence: after the feed is read to its end and the fetches drained,
    every message a row named while its conversation had a timeline shows what
    the daemon holds, whatever answers came late, failed or said unknown before."""
    scenario = draw_scenario(data, drain=True)
    final = fold(core_probe, scenario.steps)[-1]
    assert comparable(final) == scenario.model.view()
    assert final["fetches"] == {"wanted": [], "asked": [], "stale": []}
    for mid in scenario.named_with_timeline - scenario.answered_unknown:
        daemon = scenario.world.messages[mid]
        turn = final["timelines"][daemon["conversation_id"]]["turns"][mid]
        assert (turn["state"], turn["state_reason"]) == (daemon["state"], daemon["state_reason"]), mid
        if mid in scenario.own and turn["seq"] is None:
            continue                                                             # the outbox's to settle
        assert (turn["seq"], turn["origin"], turn["continues"], turn["person_text"]) == \
            (daemon["seq"], daemon["origin"], daemon["continues"], daemon["text"]), mid


# MARK: - Examples on the daemon's own JSON


@pytest.fixture
def harness():
    harness = ServiceHarness(Path(tempfile.mkdtemp(prefix="sf-wf-", dir="/tmp")))
    yield harness
    harness.close()


def status(harness: ServiceHarness, ids: list[str]) -> list[dict]:
    return harness.call("message.status", message_ids=ids)["messages"]


def turns(snapshot: dict, cid: str) -> dict:
    return snapshot["timelines"][cid]["turns"]


def shown(snapshot: dict, cid: str) -> list:
    return [(s["message_id"], s.get("person") or s.get("notice")) for s in snapshot["timelines"][cid]["shown"]]


def leave_note(harness: ServiceHarness, cid: str) -> str:
    """`conversation.unblock` choice leave, which answers with the conversation only."""
    harness.store.update_conversation(cid, blocked_by="unfinished-turn")
    before = {m["message_id"] for m in harness.store.messages(cid)}
    answer = harness.call("conversation.unblock", conversation_id=cid, choice="leave", confirm=True)
    assert set(answer) == {"conversation"}
    (note,) = [m["message_id"] for m in harness.store.messages(cid) if m["message_id"] not in before]
    return note


def fail_over(harness: ServiceHarness, cid: str, mid: str) -> str:
    """The service's own usage-limit continuation of a failed message (D-6, C-26.7)."""
    assert harness.store.set_state(mid, "failed", reason="usage-limit")
    harness.service._continue_elsewhere(harness.store.conversation(cid), harness.store.message(mid))
    (continuation,) = [m["message_id"] for m in harness.store.messages(cid) if m.get("continues") == mid]
    return continuation


def test_c29_9_the_feed_brings_in_messages_this_app_did_not_send(core_probe, harness):
    """The reported defect, on the daemon's JSON: a message the CLI submitted,
    an unblock note and a failover continuation. Each enters the focused
    conversation's timeline in sequence order with its text or its notice; this
    app's own send, which has its receipt, is never asked for."""
    cid = harness.create(title="Queue")["conversation_id"]
    mine = harness.submit(cid, "my message")
    assert harness.store.set_state(mine["message_id"], "running", expect=("queued",))
    cli = harness.submit(cid, "from the CLI", after=mine["message_id"])["message_id"]
    note = leave_note(harness, cid)
    continuation = fail_over(harness, cid, mine["message_id"])
    rows = harness.call("conversation.watch", after=0)
    expected = [cli, note, continuation]
    steps = [{"focus": cid},
             {"local": {"conversation_id": cid, "message_id": mine["message_id"], "text": "my message"}},
             {"receipts": [mine]}, {"watch": rows}, {"take": LIMIT}, {"answer": status(harness, expected)}]
    snapshots = fold(core_probe, steps)
    watched, taken, answered = snapshots[3], snapshots[4], snapshots[5]

    assert watched["fetches"]["wanted"] == expected                              # in the order the feed named them
    assert set(turns(watched, cid)) == {mine["message_id"]}                     # the defect: nothing else, yet
    assert taken["taken"] == expected and taken["fetches"]["asked"] == expected
    assert answered["fetches"] == {"wanted": [], "asked": [], "stale": []}
    assert answered["timelines"][cid]["order"] == [mine["message_id"], cli, note, continuation]
    fetched = turns(answered, cid)
    assert fetched[cli] == {**fetched[cli], "seq": 2, "origin": "person", "state": "queued",
                            "person_text": "from the CLI", "status_text": "Queued behind the current turn"}
    assert fetched[note]["origin"] == "unblock-note" and fetched[note]["state"] == "queued"
    assert fetched[continuation]["origin"] == "failover" and fetched[continuation]["continues"] == mine["message_id"]
    assert shown(answered, cid) == [(mine["message_id"], "my message"), (cli, "from the CLI"), (note, NOTE),
                                    (continuation, CONTINUED)]


def test_c29_9_only_rows_a_timeline_lacks_a_receipt_for_ask(core_probe, harness):
    """No timeline, no fetch: an unopened conversation's messages wait for
    `conversation.open`. A message with a receipt, or this app's own send not
    yet acknowledged, follows the feed's rows and is never asked for."""
    focused = harness.create(title="Focused")["conversation_id"]
    other = harness.create(title="Unopened")["conversation_id"]
    acknowledged = harness.submit(focused, "acknowledged")
    unacknowledged = harness.submit(focused, "still in the outbox", after=acknowledged["message_id"])
    elsewhere = harness.submit(other, "into a conversation the app never opened")["message_id"]
    rows = harness.call("conversation.watch", after=0)
    out = fold(core_probe, [
        {"focus": focused}, {"receipts": [acknowledged]},
        {"local": {"conversation_id": focused, "message_id": unacknowledged["message_id"],
                   "text": "still in the outbox"}},
        {"watch": rows}, {"take": LIMIT}])[-1]
    assert out["taken"] == [] and out["fetches"]["wanted"] == []
    assert other not in out["timelines"] and elsewhere not in turns(out, focused)
    assert turns(out, focused)[unacknowledged["message_id"]]["state"] == "queued"   # the row moved it


def test_c29_9_a_turn_its_events_made_gets_its_receipt(core_probe, harness):
    """A turn the app met first through its events (the dispatcher started it
    before the feed was read) has no text or sequence; the feed asks for them."""
    cid = harness.create()["conversation_id"]
    mid = harness.submit(cid, "run it")["message_id"]
    turn = harness.attempt(cid, mid)
    turn.feed(claude_init(), {"type": "user", "uuid": mid, "isReplay": True, "message": {"role": "user", "content": "x"}},
              claude_assistant("a1", [{"type": "text", "text": "Done."}]))
    events = harness.call("conversation.events", conversation_id=cid, after=0)
    rows = harness.call("conversation.watch", after=0)
    steps = [{"focus": cid}, {"events": events, "conversation_id": cid}, {"watch": rows}, {"take": LIMIT},
             {"answer": status(harness, [mid])}]
    after_events, _, taken, answered = fold(core_probe, steps)[1:]
    assert turns(after_events, cid)[mid]["lacks_receipt"] is True
    assert turns(after_events, cid)[mid]["person_text"] is None
    assert taken["taken"] == [mid]
    assert turns(answered, cid)[mid] == {**turns(answered, cid)[mid], "seq": 1, "origin": "person",
                                         "person_text": "run it", "state": "running", "lacks_receipt": False}


def test_c29_9_a_row_while_the_fetch_is_out_asks_again_and_keeps_the_newer_state(core_probe, harness):
    """The answer may be older than a row folded while it was out: a turn the
    timeline has keeps the state the row gave it, takes the answer's identity,
    and is asked for again; the second answer settles it."""
    cid = harness.create()["conversation_id"]
    mid = harness.submit(cid, "go")["message_id"]
    first = harness.call("conversation.watch", after=0)
    turn = harness.attempt(cid, mid)
    turn.feed(claude_init(), {"type": "user", "uuid": mid, "isReplay": True, "message": {"role": "user", "content": "x"}},
              claude_assistant("a1", [{"type": "text", "text": "ok"}]))
    events = harness.call("conversation.events", conversation_id=cid, after=0)
    old = status(harness, [mid])
    turn.feed(claude_result())
    second = harness.call("conversation.watch", after=first["next"])
    assert [r["state"] for r in second["changes"]][-1] == "complete"
    old_queued = [{**old[0], "state": "queued"}]                                   # asked before it started
    steps = [{"focus": cid}, {"watch": first}, {"take": LIMIT}, {"events": events, "conversation_id": cid},
             {"watch": second}, {"answer": old_queued}, {"take": LIMIT}, {"answer": status(harness, [mid])}]
    snapshots = fold(core_probe, steps)
    during, stale_answer, again, settled = snapshots[4], snapshots[5], snapshots[6], snapshots[7]
    assert during["fetches"] == {"wanted": [], "asked": [mid], "stale": [mid]}
    assert turns(during, cid)[mid]["state"] == "complete"                      # the row moved the events' turn
    assert turns(stale_answer, cid)[mid] == {**turns(stale_answer, cid)[mid], "state": "complete", "seq": 1,
                                              "person_text": "go"}
    assert stale_answer["fetches"]["wanted"] == [mid]
    assert again["taken"] == [mid]
    assert turns(settled, cid)[mid]["state"] == "complete" and settled["fetches"]["wanted"] == []


def test_c29_9_a_failed_batch_waits_again_ahead_of_the_rest(core_probe, harness):
    cid = harness.create()["conversation_id"]
    ids = []
    for n in range(4):
        ids.append(harness.submit(cid, f"message {n}", after=ids[-1] if ids else None)["message_id"])
    rows = harness.call("conversation.watch", after=0)
    named = [r for r in rows["changes"] if r["message_id"]]
    head, tail = named[:2], named[2:]
    snapshots = fold(core_probe, [{"focus": cid}, {"watch": page(head)}, {"take": LIMIT}, {"watch": page(tail)},
                                  {"take": LIMIT}, {"fail": True}, {"take": LIMIT}])
    assert snapshots[2]["taken"] == ids[:2]
    assert snapshots[4]["taken"] == []                                          # one batch at a time
    assert snapshots[5]["fetches"] == {"wanted": ids, "asked": [], "stale": []}
    assert snapshots[6]["taken"] == ids


def test_c29_9_batches_hold_at_most_200_ids_in_feed_order(core_probe, harness):
    cid = harness.create()["conversation_id"]
    ids: list[str] = []
    for n in range(450):
        ids.append(harness.submit(cid, f"message {n}", after=ids[-1] if ids else None)["message_id"])
    rows = harness.call("conversation.watch", after=0)
    assert sum(1 for r in rows["changes"] if r["message_id"]) == 450
    steps = [{"focus": cid}, {"watch": rows}]
    for start in range(0, 450, LIMIT):
        steps += [{"take": LIMIT}, {"answer": status(harness, ids[start:start + LIMIT])}]
    snapshots = fold(core_probe, steps)
    assert [s["taken"] for s in snapshots[2::2]] == [ids[:200], ids[200:400], ids[400:]]
    final = snapshots[-1]
    assert final["timelines"][cid]["order"] == ids
    assert [turns(final, cid)[m]["person_text"] for m in ids] == [f"message {n}" for n in range(450)]


# MARK: - End to end: the app's engine, the daemon's service, a second client


class AppSession:
    """The probe's `app-session`: the app's engine and state, one command at a time."""

    def __init__(self, probe: Path, socket_path: Path, journal: Path):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("SUBFLEET_")}
        self.proc = subprocess.Popen([str(probe), "app-session", str(socket_path), str(journal)], text=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=environment)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)

    def __call__(self, do: str, **args) -> dict:
        self.proc.stdin.write(json.dumps({"do": do, **args}) + "\n")
        self.proc.stdin.flush()
        assert self.selector.select(timeout=120), f"no answer to {do}"
        line = self.proc.stdout.readline()
        assert line, self.proc.stderr.read()
        out = json.loads(line)
        assert out["error"] is None, out["error"]
        return out

    def close(self) -> None:
        try:
            self.proc.stdin.write(json.dumps({"do": "quit"}) + "\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()


@pytest.fixture
def served(core_probe, tmp_path):
    harness = ServiceHarness(Path(tempfile.mkdtemp(prefix="sf-wf-", dir="/tmp")))
    server = ServiceServer(harness)
    app = AppSession(core_probe, server.path, tmp_path / "outbox.json")
    # The second client: the Python client the CLI uses, over the same socket.
    cli = Client(server.directory, verify_lock=False)
    yield harness, app, cli
    app.close()
    server.close()
    harness.close()


def cli_submit(cli: Client, harness: ServiceHarness, cid: str, text: str) -> str:
    last = [m for m in harness.store.messages(cid) if m["origin"] == "person"]
    return cli.call("message.submit", {"conversation_id": cid, "message_id": str(uuid.uuid4()),
                                       "after_message_id": last[-1]["message_id"] if last else None, "text": text,
                                       "attachments": [], "settings": harness.settings()})["message_id"]


def statuses(out: dict) -> list[list[str]]:
    return [call["ids"] for call in out["calls"] if call["op"] == "message.status"]


def test_c29_9_end_to_end_a_second_client_submits_and_the_app_shows_it(served):
    """The app, focused on a conversation whose turn runs, sees a message the
    CLI submits, the note an unblock leaves and a failover continuation, each
    in sequence order with its words, through `conversation.watch` and one
    `message.status` per page; its own send and a conversation it never opened
    ask for nothing."""
    harness, app, cli = served
    cid = harness.create(title="Watched")["conversation_id"]
    unopened = harness.create(title="Never opened")["conversation_id"]
    app("connect")
    app("focus", conversation_id=cid)
    sent = app("send", text="my message")
    mine = sent["message_id"]
    assert harness.store.set_state(mine, "running", expect=("queued",))
    own = app("watch")
    assert statuses(own) == [] and turns(own, cid)[mine]["state"] == "running"

    from_cli = cli_submit(cli, harness, cid, "from the CLI")
    elsewhere = cli_submit(cli, harness, unopened, "into a conversation the app never opened")
    out = app("watch")
    assert [call["op"] for call in out["calls"]] == ["conversation.watch", "message.status"]
    assert statuses(out) == [[from_cli]]
    assert out["timelines"][cid]["order"] == [mine, from_cli]
    assert turns(out, cid)[from_cli] == {**turns(out, cid)[from_cli], "seq": 2, "origin": "person",
                                         "state": "queued", "person_text": "from the CLI",
                                         "status_text": "Queued behind the current turn"}
    assert unopened not in out["timelines"] and elsewhere not in turns(out, cid)

    note = leave_note(harness, cid)
    continuation = fail_over(harness, cid, mine)
    out = app("watch")
    assert statuses(out) == [[note, continuation]]
    assert out["timelines"][cid]["order"] == [mine, from_cli, note, continuation]
    assert shown(out, cid) == [(mine, "my message"), (from_cli, "from the CLI"), (note, NOTE),
                               (continuation, CONTINUED)]
    assert out["fetches"] == {"wanted": [], "asked": [], "stale": []}
    quiet = app("watch")
    assert statuses(quiet) == []                                                 # nothing asked twice


def test_c29_9_end_to_end_a_turn_that_started_before_the_feed_was_read(served):
    """The second client's message starts and streams before the app reads the
    feed: its events make a turn with no words, and the feed's fetch gives it
    them."""
    harness, app, cli = served
    cid = harness.create()["conversation_id"]
    app("connect")
    app("focus", conversation_id=cid)
    mid = cli_submit(cli, harness, cid, "started elsewhere")
    turn = harness.attempt(cid, mid)
    turn.feed(claude_init(), {"type": "user", "uuid": mid, "isReplay": True, "message": {"role": "user", "content": "x"}},
              claude_assistant("a1", [{"type": "text", "text": "Working."}]))
    streamed = app("events")
    assert turns(streamed, cid)[mid]["lacks_receipt"] is True and turns(streamed, cid)[mid]["person_text"] is None
    out = app("watch")
    assert statuses(out) == [[mid]]
    assert turns(out, cid)[mid] == {**turns(out, cid)[mid], "seq": 1, "person_text": "started elsewhere",
                                    "state": "running", "lacks_receipt": False}


def test_c29_9_end_to_end_many_messages_come_200_to_a_call(served):
    """450 messages from the second client: three `message.status` calls of at
    most 200 ids, one after another, and every message in sequence order."""
    harness, app, cli = served
    cid = harness.create()["conversation_id"]
    app("connect")
    app("focus", conversation_id=cid)
    ids = [cli_submit(cli, harness, cid, f"message {n}") for n in range(450)]
    out = app("watch")
    assert out["rows"] == 450
    assert statuses(out) == [ids[:200], ids[200:400], ids[400:]]
    assert out["timelines"][cid]["order"] == ids
    assert [turns(out, cid)[m]["person_text"] for m in ids] == [f"message {n}" for n in range(450)]


def test_c29_9_end_to_end_the_launch_baseline_fetches_for_an_open_conversation(served):
    """A conversation the app has open when it (re)connects: what the feed's
    baseline names that its timeline lacks is fetched then, not at the next row."""
    harness, app, cli = served
    cid = harness.create()["conversation_id"]
    app("focus", conversation_id=cid)
    mid = cli_submit(cli, harness, cid, "while the app was away")
    out = app("connect")
    assert statuses(out) == [[mid]]
    assert turns(out, cid)[mid]["person_text"] == "while the app was away"


@pytest.mark.parametrize("count, calls", [(0, []), (1, [1]), (200, [200]), (201, [200, 1]), (450, [200, 200, 50])])
def test_c29_9_message_status_asks_at_most_200_ids_a_call(served, count, calls):
    """The daemon reads only the first 200 ids of a call and answers for those
    (service.py `op_message_status`): `ConversationEngine.status` splits a
    longer list, in order, and asks nothing for none."""
    harness, app, cli = served
    cid = harness.create()["conversation_id"]
    ids = [cli_submit(cli, harness, cid, f"message {n}") for n in range(min(count, 3))]
    ids += [str(uuid.uuid4()) for _ in range(count - len(ids))]              # unknown to the daemon: still answered
    out = app("status", ids=ids)
    assert [len(batch) for batch in statuses(out)] == calls
    assert [m for batch in statuses(out) for m in batch] == ids
    assert out["receipts"] == ids
