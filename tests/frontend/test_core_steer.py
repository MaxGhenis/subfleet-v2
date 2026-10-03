"""Steer in the app (C-24.9; DESIGN.md sections 6, 8 and 9 of the 2026-09-28 steer design).

A message sent while a turn runs can be steered into that turn instead of
queued behind it. `message.steer` is the daemon's own op here, steering into a
real `TurnRunner` for the host (`daemon_harness.make_live`, `go_live_when_submitted`),
so claims, refusals and receipts are the daemon's, and a refusal comes from the
condition that causes it. Later states a settlement or the driver would write come
from the store's own `set_state` and `append_events` (`record_state`,
`record_steer_event`); receipts come from the real `_receipt`, change rows from the
real `conversation.watch`, events from the real `conversation.events`.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import uuid

import pytest

from subfleet import protocol
from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import (
    STEER_REFUSALS, ServiceHarness, ServiceServer, after_submitted, claude_assistant, claude_block, claude_init,
    claude_result, go_live_when_submitted, make_live, record_state, record_steer_event, steer_frames, steer_requests,
)

pytestmark = needs_swift

# The read states of a steered message, in Claude Code's words (DESIGN.md section 8).
UNREAD = "Unread until Claude's next step."
UNREAD_TOOL = "Unread until the current step finishes."
UNREAD_APPROVAL = "Unread. Claude needs your approval first."
MISSED = "Unread until the current turn ends."
READ = "Read"
UNANSWERED = "Read after the turn's last step; ask again for a reply"
QUEUED = "Queued behind the current turn"
HINT = "Steering uses the running turn's settings; ⌘⏎ queues with yours"
REFUSED_WORDS = {
    "not-queued": "Queued, not steered: it had already left the queue",
    "not-next": "Queued, not steered: a recovery message goes first",
    "no-live-turn": "Queued, not steered: the turn it was sent to had ended",
    "settings-narrower": "Queued, not steered: it asks for a narrower permission than the running turn has",
    "not-steerable": "Queued, not steered: the running turn could not take it",
    "unsupported": "Queued, not steered: this daemon cannot steer",
    "no-answer": "Queued, not steered: the daemon did not answer",
}


@pytest.fixture
def harness():
    harness = ServiceHarness(Path(tempfile.mkdtemp(prefix="sf-steer-", dir="/tmp")))
    yield harness
    harness.close()


@pytest.fixture
def daemon():
    harness = ServiceHarness(Path(tempfile.mkdtemp(prefix="sf-steer-ob-", dir="/tmp")))
    server = ServiceServer(harness)
    yield harness, server
    server.close()
    harness.close()


# --- helpers -----------------------------------------------------------------------


def ids(n: int) -> list[str]:
    return [str(uuid.uuid4()) for _ in range(n)]


def replay(mid: str, text: str = "hi") -> dict:
    return {"type": "user", "uuid": mid, "isReplay": True, "message": {"role": "user", "content": text}}


def message_start(message_id: str) -> dict:
    return {"type": "stream_event", "event": {"type": "message_start", "message": {"id": message_id}}}


def tool_result(tool_id: str, content: str = "3 passed") -> dict:
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": False}]}}


def running_host(harness, text: str = "fix the parser", **create):
    """A conversation whose first message's turn runs: the provider accepted it."""
    cid = harness.create(**create)["conversation_id"]
    host = harness.submit(cid, text)["message_id"]
    turn = harness.attempt(cid, host)
    turn.feed(claude_init(), replay(host, text))
    assert harness.store.message(host)["state"] == "running"
    return cid, host, turn


def steer_capabilities(harness, providers=("claude", "codex"), steer: bool = True) -> dict:
    """The daemon's real `capabilities`, with `steer.v1` and `steer_providers` as section 6 adds them."""
    caps = harness.call("capabilities")
    names = [c for c in caps["capabilities"] if c != "steer.v1"] + (["steer.v1"] if steer else [])
    return {**caps, "capabilities": names, "steer_providers": list(providers)}


def statuses(harness, *mids) -> list[dict]:
    return harness.call("message.status", message_ids=list(mids))["messages"]


def page(harness, cid: str, after: int = 0) -> dict:
    return harness.call("conversation.events", conversation_id=cid, after=after)


def watch(harness, after: int) -> dict:
    return harness.call("conversation.watch", after=after)


def fold(core_probe, tmp_path, cid: str, steps: list[dict]) -> dict:
    return run_probe(core_probe, "fold", write_json(tmp_path / f"fold-{uuid.uuid4().hex}.json",
                                                    {"conversation_id": cid, "steps": steps}))


def store(core_probe, tmp_path, steps: list[dict], picked: dict | None = None) -> dict:
    return run_probe(core_probe, "store", write_json(tmp_path / f"store-{uuid.uuid4().hex}.json",
                                                     {"now": time.time(), "steps": steps, "picked": picked or {}}))


def run_steps(core_probe, tmp_path, server_path, steps, journal: Path | None = None) -> dict:
    journal = journal or tmp_path / "support" / "outbox.json"
    return run_probe(core_probe, "outbox", server_path, journal,
                     write_json(tmp_path / f"steps-{uuid.uuid4().hex}.json", steps), timeout=120)


def by_key(rows: list[dict], key: str = "key") -> dict:
    return {row[key]: row for row in rows}


def person_ids(items: list[dict]) -> list[str]:
    return [i["id"] for i in items if i["type"] == "person"]


def create_step(harness, request_id: str) -> dict:
    return {"do": "create", "request_id": request_id, "workspace": str(harness.workspace)}


def change_rows(harness, mid: str) -> list[tuple]:
    return [(r["state"], r["state_reason"]) for r in harness.store.query(
        "SELECT state, state_reason FROM changes WHERE message_id=? ORDER BY seq", (mid,))]


# --- protocol (C-25.1, C-25.2) -----------------------------------------------------------


def test_c25_2_the_steer_op_and_fields_round_trip(core_probe, tmp_path, harness):
    """`message.steer` encodes `{message_id}`, and `into` when the app knows the turn it
    steers into; the daemon's receipt, the capability's `steer_providers`, and
    `steered_into` on change rows and status entries survive the Swift models."""
    cid, host, _ = running_host(harness)
    mid = harness.submit(cid, "also the lexer", after=host)["message_id"]
    for args in ({"message_id": mid}, {"message_id": mid, "into": host}):
        line = run_probe(core_probe, "request", "message.steer", write_json(tmp_path / "a.json", args), "app-7",
                         raw=True)
        request = protocol.decode_request(line.encode())
        assert (request.op, request.id, request.args) == ("message.steer", "app-7", args)
    assert "message.steer" in run_probe(core_probe, "ops")

    make_live(harness, cid, host)
    receipt = harness.call("message.steer", message_id=mid, into=host)
    assert (receipt["state"], receipt["state_reason"], receipt["steered_into"]) == ("steering", f"steer:{host}", host)
    record_state(harness, mid, "steered", f"steered:{host}", served={"steered_into": host})
    status = statuses(harness, mid)[0]
    status["steered_into"] = host                      # section 6: status entries carry it
    changes = watch(harness, 0)
    for change in changes["changes"]:
        change["steered_into"] = host if change["message_id"] == mid and change["state"] != "queued" else None
    capabilities = steer_capabilities(harness)
    for op, result in (("message.steer", receipt), ("message.status", {"messages": [status]}),
                       ("conversation.watch", changes), ("capabilities", capabilities)):
        back = json.loads(run_probe(core_probe, "roundtrip", op, write_json(tmp_path / f"{op}.json", result), raw=True))
        assert strip_nulls(back) == strip_nulls(result), op
    assert run_probe(core_probe, "availability", write_json(tmp_path / "c.json", capabilities)) == {"ready": True}


def strip_nulls(value):
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value]
    return value


# --- availability: the composer and the queued bubble (design section 1) --------------------


def test_c24_9_the_composer_steers_only_while_a_running_turn_takes_steers(core_probe, tmp_path, harness):
    """Return steers (and ⌘Return queues for later) only while the conversation's live turn runs or
    asks, is not stopping, has not answered, and the daemon steers this provider;
    otherwise nothing changes. A picked setting that differs gets the one-line hint."""
    cid, host, turn = running_host(harness)
    caps = steer_capabilities(harness)
    opened = harness.call("conversation.open", conversation_id=cid)
    events = page(harness, cid)
    base = [{"capabilities": caps}, {"open": opened}, {"events": events, "conversation_id": cid}]

    out = store(core_probe, tmp_path, base)
    assert out["steer_hosts"][cid] == host
    assert out["steer_hints"][cid] is None                     # the picks are the running turn's
    picked = {**opened["conversation"]["settings"], "effort": "high"}
    assert store(core_probe, tmp_path, base, picked={cid: picked})["steer_hints"][cid] == HINT
    assert store(core_probe, tmp_path, base, picked={cid: {**picked, "effort": None, "fast": True}})[
        "steer_hints"][cid] == HINT

    # The daemon does not steer: no capability, or not for this provider.
    without = {**caps, "capabilities": [c for c in caps["capabilities"] if c != "steer.v1"]}
    assert store(core_probe, tmp_path, [{"capabilities": without}, *base[1:]])["steer_hosts"][cid] is None
    codex_only = steer_capabilities(harness, providers=("codex",))
    assert store(core_probe, tmp_path, [{"capabilities": codex_only}, *base[1:]])["steer_hosts"][cid] is None
    no_list = {k: v for k, v in caps.items() if k != "steer_providers"}
    assert store(core_probe, tmp_path, [{"capabilities": no_list}, *base[1:]])["steer_hosts"][cid] is None

    # A stop was asked for: the turn is winding down.
    stopping = {**opened, "messages": [{**m, "stop_requested": True} for m in opened["messages"]]}
    assert store(core_probe, tmp_path, [base[0], {"open": stopping}, base[2]])["steer_hosts"][cid] is None
    # The provider has answered ("Finishing"): nothing more joins this turn.
    turn.feed(claude_assistant("msg_1", [{"type": "text", "text": "Done."}]), claude_result())
    finished = page(harness, cid)
    assert store(core_probe, tmp_path, [base[0], base[1], {"events": finished, "conversation_id": cid}])[
        "steer_hosts"][cid] is None


def test_c24_9_a_turn_asking_for_approval_takes_steers_and_a_narrower_permission_does_not(core_probe, tmp_path,
                                                                                         harness):
    cid, host, turn = running_host(harness)
    turn.feed(claude_assistant("msg_b", [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                                          "input": {"command": "ls"}}]),
              {"type": "control_request", "request_id": "perm-1", "request": {
                  "subtype": "can_use_tool", "tool_name": "Bash", "tool_use_id": "toolu_1", "input": {"command": "ls"}}})
    assert harness.store.message(host)["state"] == "approval-needed"
    caps = steer_capabilities(harness)
    opened = harness.call("conversation.open", conversation_id=cid)
    base = [{"capabilities": caps}, {"open": opened}, {"events": page(harness, cid), "conversation_id": cid}]
    assert store(core_probe, tmp_path, base)["steer_hosts"][cid] == host

    # The person narrowed the conversation to read-only after the turn started (under
    # ask): its next message would be refused `settings-narrower`, so Return queues.
    narrowed = harness.call("conversation.settings", conversation_id=cid, settings={"permission": "read-only"})
    out = store(core_probe, tmp_path, [base[0], {"open": {**opened, "conversation": narrowed["conversation"]}}, base[2]])
    assert out["steer_hosts"][cid] is None


def test_c24_9_queued_messages_do_not_stop_steering(core_probe, tmp_path, harness):
    """The steer lane and the queue are independent (DESIGN.md section 8, and the
    integration decision of 2026-09-28): while messages wait for later, Return still
    steers, and any queued person message may be steered now ("Send now" on the queue
    tray). Stop on a queued message is `message.cancel`; on a steering one, a cancel
    that never interrupts."""
    cid, host, _ = running_host(harness)
    first = harness.submit(cid, "also the lexer", after=host)["message_id"]
    second = harness.submit(cid, "and the docs", after=first)["message_id"]
    caps = steer_capabilities(harness)
    opened = harness.call("conversation.open", conversation_id=cid)
    base = [{"capabilities": caps}, {"open": opened}, {"events": page(harness, cid), "conversation_id": cid}]
    out = store(core_probe, tmp_path, base)
    assert out["steer_hosts"][cid] == host
    assert (out["steer_offers"][first], out["steer_offers"][second], out["steer_offers"][host]) == (True, True, False)
    assert out["stops"][first] == {"action": "cancel", "message_id": first}
    assert out["stops"][second] == {"action": "cancel", "message_id": second}
    assert out["statuses"][first] == out["statuses"][second] == QUEUED

    # Without steer.v1 nothing is offered, and the bubbles read as before.
    plain = store(core_probe, tmp_path, [{"capabilities": steer_capabilities(harness, steer=False)}, *base[1:]])
    assert plain["steer_offers"][first] is False and plain["statuses"][first] == QUEUED

    # A message with a permission narrower than the running turn's is not offered.
    narrow = {**statuses(harness, first)[0], "settings": {**opened["messages"][1]["settings"], "permission": "read-only"}}
    assert store(core_probe, tmp_path, [*base, {"receipts": [narrow]}])["steer_offers"][first] is False

    # A slash command or shell input is never steered (DESIGN.md section 9).
    command = harness.submit(cid, "/compact", after=second)["message_id"]
    shell = harness.submit(cid, "!make test", after=command)["message_id"]
    with_commands = store(core_probe, tmp_path, [base[0], {"open": harness.call("conversation.open", conversation_id=cid)},
                                                 base[2]])
    assert (with_commands["steer_offers"][command], with_commands["steer_offers"][shell]) == (False, False)
    assert with_commands["steer_offers"][first] is True

    # A message already on its way is not offered again.
    asked = store(core_probe, tmp_path, [*base, {"steers": [journaled(first, cid, "queued")]}])
    assert asked["steer_offers"][first] is False and asked["steer_offers"][second] is True
    assert asked["statuses"][first] == UNREAD
    record_state(harness, first, "steering", f"steer:{host}")
    record_state(harness, second, "steering", f"steer:{host}")
    both = store(core_probe, tmp_path, [base[0], {"open": harness.call("conversation.open", conversation_id=cid)}, base[2]])
    assert both["steer_hosts"][cid] == host
    assert both["stops"][first] == {"action": "cancel-steer", "message_id": first}
    record_state(harness, first, "steered", f"steered:{host}")
    settled = store(core_probe, tmp_path, [base[0], {"open": harness.call("conversation.open", conversation_id=cid)}])
    assert settled["stops"][first] == {"action": "none"}


def journaled(mid: str, cid: str, state: str, reason: str | None = None) -> dict:
    """An outbox steer as the journal holds it (OutboxSteer's own keys)."""
    out = {"messageID": mid, "conversation": cid, "order": 1, "state": state, "attempts": 1,
           "createdAt": "2026-09-28T12:00:00.000Z"}
    if reason:
        out["failure"] = {"code": 2, "reason": reason, "message": f"{reason}: the message stays queued",
                          "retryable": False}
    return out


# --- status lines (design section 1) ---------------------------------------------------------


def test_c24_9_every_steer_status_line(core_probe, tmp_path, harness):
    """The words under a steered message, from what the app asked and the daemon
    answered: unread while the turn thinks, then Read (DESIGN.md section 8)."""
    cid, host, turn = running_host(harness)
    mids = []
    after = host
    for name in ("asked", "steering", "delivered", "steered", "unanswered", "missed", "event-missed", "answered",
                 *STEER_REFUSALS, "unsupported", "no-answer"):
        mids.append((name, harness.submit(cid, name, after=after)["message_id"]))
        after = mids[-1][1]
    mid = dict(mids)
    record_state(harness, mid["steering"], "steering", f"steer:{host}")
    record_state(harness, mid["delivered"], "steering", f"steer:{host}")
    record_steer_event(turn, "steer.delivered", mid["delivered"])
    record_state(harness, mid["steered"], "steered", f"steered:{host}")
    record_state(harness, mid["unanswered"], "steered", f"steered-unanswered:{host}")
    record_state(harness, mid["missed"], "queued", "steer-missed: the turn ended before the provider read it")
    record_state(harness, mid["event-missed"], "steering", f"steer:{host}")
    record_steer_event(turn, "steer.missed", mid["event-missed"], why="the turn ended")
    local = str(uuid.uuid4())
    receipts = statuses(harness, *mid.values())
    refusals = [{"steer_answer": {"message_id": mid[code], "refusal": {"reason": code, "message": f"{code}: no"}}}
                for code in (*STEER_REFUSALS, "unsupported", "no-answer")]
    out = fold(core_probe, tmp_path, cid, [
        {"receipts": receipts}, {"page": page(harness, cid)},
        {"local": {"message_id": local, "text": "right now", "steer": True}},
        {"steer_request": mid["asked"]},
        {"steer_request": mid["answered"]}, {"steer_answer": {"message_id": mid["answered"], "refusal": None}},
        *refusals,
        # Esc takes back the newest the daemon may still give back; with none left, it stops the turn.
        {"escape": []},
        {"escape": [local, mid["no-answer"]]},
        {"escape": [m for _, m in mids] + [local]},
    ])
    words = {name: out["turns"][m]["status_text"] for name, m in mid.items()}
    assert out["turns"][local]["status_text"] == UNREAD
    assert words["asked"] == UNREAD
    assert words["steering"] == UNREAD
    assert words["delivered"] == READ
    assert words["steered"] == READ
    assert words["unanswered"] == UNANSWERED
    assert words["missed"] == MISSED
    assert words["event-missed"] == MISSED              # the host's steer.missed, before the receipt
    assert words["answered"] == QUEUED            # taken back or answered: the receipts say the rest
    for code in (*STEER_REFUSALS, "unsupported", "no-answer"):
        assert words[code] == REFUSED_WORDS[code], code
    assert out["turns"][mid["steered"]]["steered_into"] == host           # from the reason when the row has no field
    assert out["turns"][mid["unanswered"]]["steered_into"] == host
    assert out["turns"][mid["missed"]]["steered_into"] is None
    assert out["unknown_kinds"] == {}
    # What Esc may take back: steers not read yet, and refused ones still queued.
    recallable = [m for name, m in mids if name in ("asked", "steering", "missed", "event-missed", *STEER_REFUSALS,
                                                    "unsupported", "no-answer")] + [local]
    assert out["recallable_steers"] == recallable
    assert out["results"][-3:] == [f"escape:recall:{local}", f"escape:recall:{mid['unsupported']}",
                                   f"escape:stop:{host}"]
    assert all(out["turns"][m]["read_steer"] for m in (mid["delivered"], mid["steered"], mid["unanswered"]))


def test_c24_9_an_unread_steer_says_what_the_turn_is_doing(core_probe, tmp_path, harness):
    """Unread until the model's next step; until a running tool finishes; or until the
    person answers the turn's approval. In a Codex conversation, Codex's name."""
    cid, host, turn = running_host(harness)
    steered = harness.submit(cid, "also the lexer", after=host)["message_id"]
    record_state(harness, steered, "steering", f"steer:{host}")
    early = statuses(harness, host, steered)
    thinking = page(harness, cid)
    turn.feed(message_start("msg_1"), *claude_block("msg_1", 0, {"type": "tool_use", "id": "tu1", "name": "Bash",
                                                                 "input": {"command": "ls"}}, ['{"command": "ls"}']))
    tool = page(harness, cid, thinking["next"])
    turn.feed(tool_result("tu1"),
              claude_assistant("msg_2", [{"type": "tool_use", "id": "tu2", "name": "Bash", "input": {"command": "rm x"}}]),
              {"type": "control_request", "request_id": "perm-1", "request": {
                  "subtype": "can_use_tool", "tool_name": "Bash", "tool_use_id": "tu2", "input": {"command": "rm x"}}})
    asking = page(harness, cid, tool["next"])
    out = fold(core_probe, tmp_path, cid, [
        {"receipts": early}, {"page": thinking, "snapshot": True},
        {"page": tool, "snapshot": True}, {"page": asking, "snapshot": True},
    ])
    assert [s["turns"][steered]["status_text"] for s in out["snapshots"]] == [UNREAD, UNREAD_TOOL, UNREAD_APPROVAL]

    codex = harness.create(provider="codex", settings={"model": "gpt-6-astra", "permission": "read-only"})
    ccid = codex["conversation_id"]

    def submit(text, after=None):
        return harness.call("message.submit", conversation_id=ccid, message_id=str(uuid.uuid4()), after_message_id=after,
                            text=text, attachments=[], settings=codex["settings"])["message_id"]
    chost = submit("survey the repo")
    harness.store.set_state(chost, "running", expect=("queued",))
    csteer = submit("and the tests", after=chost)
    record_state(harness, csteer, "steering", f"steer:{chost}")
    words = store(core_probe, tmp_path, [{"open": harness.call("conversation.open", conversation_id=ccid)}])
    assert words["statuses"][csteer] == "Unread until Codex's next step."


# --- timeline placement (design section 3, "Timeline") ------------------------------------


def test_c24_9_a_steered_message_is_drawn_once_where_the_turn_took_it(core_probe, tmp_path, harness):
    """Before the provider takes it, the steer is drawn in its own place; at the host's
    `steer.delivered` it moves inside the host turn, between the step before and the
    step after, and it is never drawn twice, also after a reset re-reads the log."""
    cid, host, turn = running_host(harness)
    turn.feed(message_start("msg_1"),
              *claude_block("msg_1", 0, {"type": "text", "text": "Looking."}, ["Looking."]),
              *claude_block("msg_1", 1, {"type": "tool_use", "id": "tu1", "name": "Bash",
                                         "input": {"command": "pytest -q"}}, ['{"command": "pytest -q"}']))
    steered = harness.submit(cid, "also check the lexer", after=host)["message_id"]
    make_live(harness, cid, host)
    harness.call("message.steer", message_id=steered)
    before = page(harness, cid)
    early = statuses(harness, host, steered)
    turn.feed(tool_result("tu1"))
    record_steer_event(turn, "steer.sent", steered)
    record_steer_event(turn, "steer.delivered", steered)
    turn.feed(message_start("msg_2"),
              *claude_block("msg_2", 0, {"type": "text", "text": "Checking the lexer too."}, ["Checking the lexer too."]))
    during = page(harness, cid, before["next"])
    record_state(harness, steered, "steered", f"steered:{host}", served={"steered_into": host})
    turn.feed(claude_result())
    rest = page(harness, cid, during["next"])
    late = statuses(harness, host, steered)
    out = fold(core_probe, tmp_path, cid, [
        {"receipts": early}, {"page": before, "snapshot": True},
        {"page": during, "snapshot": True},
        {"receipts": late}, {"page": rest, "snapshot": True},
        {"page": {"events": [], "next": 0, "reset": True}}, {"page": page(harness, cid), "snapshot": True},
    ])
    pending, taken, settled, reread = out["snapshots"]

    ids = [i["id"] for i in pending["items"]]
    assert person_ids(pending["items"]) == [f"person:{host}", f"person:{steered}"]
    assert ids[-1] == f"person:{steered}"                       # in its own place, at the end
    assert pending["turns"][steered]["status_text"] == UNREAD_TOOL          # the provider is running Bash
    assert pending["placed_steers"] == [] and pending["recallable_steers"] == [steered]

    for snapshot in (taken, settled, reread):
        ids = [i["id"] for i in snapshot["items"]]
        assert ids.count(f"person:{steered}") == 1
        assert not any(i.startswith("steer:") for i in ids)      # the marker itself is never drawn
        at = ids.index(f"person:{steered}")
        assert ids[at - 1] == f"tool:{host}:tu1"                  # after the step the provider finished
        after = snapshot["items"][at + 1]
        assert after["type"] == "text" and after["text"] == "Checking the lexer too."
        assert snapshot["placed_steers"] == [steered] and snapshot["recallable_steers"] == []
        assert snapshot["turns"][steered]["status_text"] == READ
        assert snapshot["unknown_kinds"] == {}
        assert len(ids) == len(set(ids))                          # every row's id is unique
    assert taken["turns"][steered]["state"] == "steering" and taken["turns"][steered]["steer_delivered_in"] == host
    assert settled["turns"][steered]["state"] == "steered" and settled["turns"][host]["outcome"]["state"] == "complete"
    assert [i["id"] for i in reread["items"]] == [i["id"] for i in settled["items"]]
    assert out["resets"] == 1


def test_c24_9_a_steer_that_missed_is_drawn_in_its_own_place_and_runs_next(core_probe, tmp_path, harness):
    """The provider took it but the turn was stopped before the model read it: it goes
    back to the queue (`steer-missed:`), is drawn in its own place, and then runs as a
    turn of its own, once."""
    cid, host, turn = running_host(harness)
    steered = harness.submit(cid, "also check the lexer", after=host)["message_id"]
    make_live(harness, cid, host)
    harness.call("message.steer", message_id=steered)
    record_steer_event(turn, "steer.delivered", steered)
    turn.feed(claude_result(ok=False, subtype="error_during_execution"))
    record_steer_event(turn, "steer.missed", steered, why="stopped")
    record_state(harness, steered, "queued", "steer-missed: the turn stopped before the model read it")
    missed = page(harness, cid)
    first = statuses(harness, host, steered)
    own = harness.attempt(cid, steered)
    own.feed(claude_init(), replay(steered, "also check the lexer"),
             claude_assistant("msg_9", [{"type": "text", "text": "The lexer is fine."}]), claude_result())
    out = fold(core_probe, tmp_path, cid, [
        {"receipts": first}, {"page": missed, "snapshot": True},
        {"receipts": statuses(harness, steered)}, {"page": page(harness, cid, missed["next"]), "snapshot": True},
    ])
    queued, ran = out["snapshots"]
    for snapshot in (queued, ran):
        assert person_ids(snapshot["items"]) == [f"person:{host}", f"person:{steered}"]
        assert snapshot["placed_steers"] == []
    assert queued["turns"][steered]["status_text"] == MISSED
    ids = [i["id"] for i in ran["items"]]
    assert ids[ids.index(f"person:{steered}") + 1:] and ran["items"][-1]["text"] == "The lexer is fine."
    assert ran["turns"][steered]["status_text"] == "Completed"


# --- the outbox (C-24.2, C-28.3; design section 2) -------------------------------------------


def test_c24_9_a_steer_is_its_submit_then_the_steer_in_journal_order(core_probe, tmp_path, daemon):
    harness, server = daemon
    host, steered, after = ids(3)
    runners = go_live_when_submitted(harness, host)
    out = run_steps(core_probe, tmp_path, server.path, [
        create_step(harness, "req-s1"),
        {"do": "submit", "conversation": "@draft:req-s1", "message_id": host, "text": "fix the parser"},
        {"do": "pump"},
        {"do": "submit", "conversation": "@conv:req-s1", "message_id": steered, "text": "also the lexer", "steer": True},
        {"do": "submit", "conversation": "@conv:req-s1", "message_id": after, "text": "then the docs"},
        {"do": "sendable"}, {"do": "pump"},
    ])
    assert out["calls"] == ["conversation.create req-s1 answered", f"message.submit {host} after=null answered",
                            f"message.submit {steered} after={host} answered", f"message.steer {steered} answered",
                            f"message.submit {after} after={steered} answered"]
    assert out["results"][5]["keys"] == [steered]                   # one at a time per conversation
    report = out["results"][6]["report"]
    assert report["acknowledged"] == [steered, f"steer:{steered}", after] and report["refused"] == []
    (steer,) = out["steers"]
    assert (steer["message_id"], steer["state"], steer["attempts"]) == (steered, "acknowledged", 1)
    assert steer["receipt"]["state"] == "steering" and steer["receipt"]["steered_into"] == host
    assert [s["message_id"] for s in report["steers"]] == [steered]
    orders = {e["key"]: e["order"] for e in out["entries"]}
    assert orders[steered] < steer["order"] < orders[after]
    assert steer_requests(server) == [steered]
    assert harness.store.message(steered)["state"] == "steering"
    assert steer_frames(runners[host]) == [steered]                  # the provider gets it once
    assert harness.store.message(after)["state"] == "queued"
    assert out["chains"] == {harness.store.message(host)["conversation_id"]: after}


def test_c24_9_a_steer_under_way_at_a_restart_or_without_an_answer_is_sent_again_and_lands_once(core_probe, tmp_path,
                                                                                            daemon):
    harness, server = daemon
    host, steered = ids(2)
    runners = go_live_when_submitted(harness, host)
    journal = tmp_path / "support" / "outbox.json"
    first = run_steps(core_probe, tmp_path, server.path, [
        create_step(harness, "req-s2"),
        {"do": "submit", "conversation": "@draft:req-s2", "message_id": host, "text": "fix the parser"},
        {"do": "pump"},
        {"do": "submit", "conversation": "@conv:req-s2", "message_id": steered, "text": "also", "steer": True},
        {"do": "pump"},
    ], journal)
    # The submit landed; its steer is refused a connection once, then journaled as sending
    # when the app stops.
    assert first["steers"][0]["state"] == "acknowledged"
    record_state(harness, steered, "queued")                         # the first steer, undone for the rerun
    server.faults[("message.steer", steered)] = "drop"               # handled, answer lost
    again = run_steps(core_probe, tmp_path, server.path, [
        {"do": "steer", "key": steered, "conversation": "@conv:req-s2"},
        {"do": "begin-steer", "key": steered},
        {"do": "reload"},                                             # a new process: sending is queued again
        {"do": "pump"}, {"do": "sendable"}, {"do": "advance", "seconds": 1}, {"do": "pump"},
    ], journal)
    assert again["results"][2]["states"] == ["acknowledged", "acknowledged", "acknowledged"]
    lost, waiting, landed = again["results"][3]["report"], again["results"][4]["keys"], again["results"][6]["report"]
    assert lost["retrying"] == [f"steer:{steered}"] and waiting == []           # a 0.5 s backoff first
    assert landed["acknowledged"] == [f"steer:{steered}"]
    steer = again["steers"][0]
    assert (steer["state"], steer["attempts"]) == ("acknowledged", 3)          # the begun send counts
    assert steer_requests(server) == [steered] * 3                              # the repeat answered, not redone
    assert change_rows(harness, steered).count(("steering", f"steer:{host}")) == 2   # once per real steer
    assert [c for c in again["calls"] if "message.steer" in c] == [f"message.steer {steered} no-answer",
                                                                    f"message.steer {steered} answered"]
    assert steer_frames(runners[host]) == [steered]                              # written once for all of it


@pytest.mark.parametrize("code", STEER_REFUSALS)
def test_c24_9_a_refused_steer_leaves_its_message_queued_and_holds_nothing(core_probe, tmp_path, daemon, code):
    """Each refusal the daemon gives, from the condition that causes it: the message was
    withdrawn first, a repair message is queued, no turn runs, the message asks for a
    narrower permission, or the runner is still catching up after a restart."""
    harness, server = daemon
    host, steered, after = ids(3)
    runners = go_live_when_submitted(harness, host) if code != "no-live-turn" else {}
    submitted = {"do": "submit", "conversation": "@conv:req-r", "message_id": steered, "text": "also", "steer": True}
    following = {"do": "submit", "conversation": "@conv:req-r", "message_id": after, "text": "then"}
    if code == "not-queued":
        after_submitted(harness, steered, lambda r: harness.call("message.cancel", message_id=steered))
    elif code == "not-next":
        after_submitted(harness, steered, lambda r: harness.store.submit_message(
            conversation_id=r["conversation_id"], message_id=str(uuid.uuid4()), after_message_id=steered,
            text="the stopped turn was left", attachments=[], settings=harness.settings(), origin="unblock-note"))
    elif code == "settings-narrower":
        # A narrower message narrows its conversation: the next one is as narrow.
        submitted["settings"] = following["settings"] = harness.settings(permission="read-only")
    elif code == "not-steerable":
        after_submitted(harness, steered, lambda r: setattr(runners[host], "replay_caught_up", False))
    out = run_steps(core_probe, tmp_path, server.path, [
        create_step(harness, "req-r"),
        {"do": "submit", "conversation": "@draft:req-r", "message_id": host, "text": "fix the parser"},
        {"do": "pump"},
        submitted,
        following,
        {"do": "pump"},
    ])
    report = out["results"][-1]["report"]
    assert report["refused"] == [f"steer:{steered}"] and report["failed"] == []
    assert report["acknowledged"] == [steered, after]                # the next message was not held
    (steer,) = out["steers"]
    assert steer["state"] == "refused"
    assert steer["failure"]["reason"] == code and steer["failure"]["retryable"] is False
    assert [s["state"] for s in report["steers"]] == ["refused"]
    assert harness.store.message(steered)["state"] == ("cancelled" if code == "not-queued" else "queued")
    assert all(steer_frames(runner) == [] for runner in runners.values())


@pytest.mark.parametrize("cause", ["catching-up", "not-taken-back"])
@pytest.mark.parametrize("catches_up", [True, False])
def test_c24_9_a_steer_the_turn_cannot_take_yet_is_asked_again_briefly(core_probe, tmp_path, daemon, catches_up,
                                                                       cause):
    """Steer review finding 3: the daemon answers `not-steerable` while the running turn's
    runner is still catching up after a daemon restart, or while the restarted daemon has
    not taken the turn's runner back yet, or the provider is not ready. A steer that names
    its turn (`into`) is asked again briefly, so it lands once the turn can take it,
    written to the provider once; a turn that never can leaves it queued, the steer
    refused `not-steerable` after `steerAttempts` tries, holding nothing after."""
    harness, server = daemon
    host, steered, after = ids(3)
    runners = go_live_when_submitted(harness, host)
    attempt = f"job-{host[:8]}/a1"                          # the runner's key in the service (`make_live`)

    def not_ready(ready: bool) -> None:
        if cause == "catching-up":
            runners[host].replay_caught_up = ready
        elif ready:
            harness.service.runners[attempt] = runners[host]
        else:
            harness.service.runners.pop(attempt, None)
    after_submitted(harness, steered, lambda r: not_ready(False))
    real = harness.service.op_message_steer

    def steer(args, peer):
        try:
            return real(args, peer)
        finally:
            not_ready(catches_up)                           # ready by the next try, or never
    harness.service.op_message_steer = steer
    out = run_steps(core_probe, tmp_path, server.path, [
        create_step(harness, "req-n"),
        {"do": "submit", "conversation": "@draft:req-n", "message_id": host, "text": "fix the parser"},
        {"do": "pump"},
        {"do": "submit", "conversation": "@conv:req-n", "message_id": steered, "text": "also", "steer": True,
         "into": host},
        {"do": "submit", "conversation": "@conv:req-n", "message_id": after, "text": "then"},
        *[s for _ in range(6) for s in ({"do": "pump"}, {"do": "advance", "seconds": 10})],
    ])
    (steer_entry,) = out["steers"]
    tries = steer_requests(server)
    assert all(r["args"] == {"message_id": steered, "into": host}
               for r in server.requests if r["op"] == "message.steer")
    if catches_up:
        assert steer_entry["state"] == "acknowledged" and len(tries) == steer_entry["attempts"] == 2
        assert harness.store.message(steered)["state"] == "steering"
        assert steer_frames(runners[host]) == [steered]
    else:
        assert steer_entry["state"] == "refused" and len(tries) == steer_entry["attempts"] == 5
        assert steer_entry["failure"]["reason"] == "not-steerable" and steer_entry["failure"]["retryable"] is False
        assert harness.store.message(steered)["state"] == "queued" and steer_frames(runners[host]) == []
    assert all(e["state"] == "acknowledged" for e in out["entries"])            # nothing held behind it
    assert harness.store.message(after)["state"] == "queued"


def scripted(tmp_path, answers: list[tuple[str, dict]]) -> str:
    return "script:" + str(write_json(tmp_path / f"script-{uuid.uuid4().hex}.json",
                                      [{"op": op, "answer": answer} for op, answer in answers]))


def test_c24_9_an_older_daemon_answers_unknown_op_and_the_message_stays_queued(core_probe, tmp_path, harness):
    """A daemon replaced by one without `message.steer` answers `unknown op` (id ""):
    the steer closes as unsupported and holds nothing."""
    cid = harness.create()["conversation_id"]
    steered, after = ids(2)
    first = harness.submit(cid, "also", message_id=steered)
    second = harness.submit(cid, "then", message_id=after, after=steered)
    try:
        protocol.decode_request(b'{"v":1,"id":"app-1","op":"message.frobnicate","args":{}}')
    except protocol.ProtocolError as exc:
        unknown = json.loads(protocol.encode(protocol.fail("", 2, str(exc).replace("message.frobnicate", "message.steer"))))
    script = scripted(tmp_path, [("message.submit", json.loads(protocol.encode(protocol.ok("", first)))),
                                 ("message.steer", unknown),
                                 ("message.submit", json.loads(protocol.encode(protocol.ok("", second))))])
    out = run_steps(core_probe, tmp_path, script, [
        {"do": "know_chain", "conversation": cid, "last": None},
        {"do": "submit", "conversation": cid, "message_id": steered, "text": "also", "steer": True},
        {"do": "submit", "conversation": cid, "message_id": after, "text": "then"},
        {"do": "pump"},
    ])
    report = out["results"][-1]["report"]
    assert report["acknowledged"] == [steered, after] and report["refused"] == [f"steer:{steered}"]
    assert out["steers"][0]["failure"]["reason"] == "unsupported"


def test_c24_9_a_steer_without_an_answer_holds_its_conversation_briefly_then_gives_up(core_probe, tmp_path, harness):
    """A busy daemon (exit 69) is asked again with a backoff, and meanwhile the next
    message of that conversation waits (one outstanding per conversation); after five
    tries the steer is given up, the message stays queued, and the next one goes."""
    cid = harness.create()["conversation_id"]
    steered, after = ids(2)
    first = harness.submit(cid, "also", message_id=steered)
    second = harness.submit(cid, "then", message_id=after, after=steered)
    from subfleet.daemon import busy_answer
    busy = json.loads(busy_answer("the daemon is serving 512 connections"))
    script = scripted(tmp_path, [("message.submit", json.loads(protocol.encode(protocol.ok("", first)))),
                                 *[("message.steer", busy)] * 5,
                                 ("message.submit", json.loads(protocol.encode(protocol.ok("", second))))])
    retries = [step for _ in range(4) for step in ({"do": "sendable"}, {"do": "advance", "seconds": 10}, {"do": "pump"})]
    out = run_steps(core_probe, tmp_path, script, [
        {"do": "know_chain", "conversation": cid, "last": None},
        {"do": "submit", "conversation": cid, "message_id": steered, "text": "also", "steer": True},
        {"do": "submit", "conversation": cid, "message_id": after, "text": "then"},
        {"do": "pump"}, *retries,
    ])
    pumps = [r["report"] for r in out["results"] if r["do"] == "pump"]
    held = [r["keys"] for r in out["results"] if r["do"] == "sendable"]
    assert pumps[0]["acknowledged"] == [steered] and pumps[0]["retrying"] == [f"steer:{steered}"]
    assert held == [[], [], [], []]                                   # nothing of it goes until due
    assert [p["retrying"] for p in pumps[1:4]] == [[f"steer:{steered}"]] * 3
    assert pumps[4]["refused"] == [f"steer:{steered}"] and pumps[4]["acknowledged"] == [after]
    steer = out["steers"][0]
    assert (steer["state"], steer["attempts"], steer["failure"]["reason"]) == ("refused", 5, "no-answer")
    assert by_key(out["entries"])[after]["last_after"] == steered


def test_c24_9_a_steer_waits_behind_its_own_conversation_only(core_probe, tmp_path, daemon):
    harness, server = daemon
    host, steered, other = ids(3)
    go_live_when_submitted(harness, host)
    server.faults[("message.steer", steered)] = "busy"
    out = run_steps(core_probe, tmp_path, server.path, [
        create_step(harness, "req-a"), create_step(harness, "req-b"),
        {"do": "submit", "conversation": "@draft:req-a", "message_id": host, "text": "fix the parser"},
        {"do": "pump"},
        {"do": "submit", "conversation": "@conv:req-a", "message_id": steered, "text": "also", "steer": True},
        {"do": "pump"},
        {"do": "submit", "conversation": "@conv:req-b", "message_id": other, "text": "elsewhere"},
        {"do": "sendable"}, {"do": "pump"},
    ])
    assert out["results"][5]["report"]["retrying"] == [f"steer:{steered}"]
    assert out["results"][7]["keys"] == [other]                        # another conversation is not held
    assert out["results"][8]["report"]["acknowledged"] == [other]


def test_c24_9_esc_takes_back_a_steer_the_provider_has_not_read(core_probe, tmp_path, daemon):
    """Esc (DESIGN.md sections 8 and 9) recalls an unread steer: one still journaled
    here is withdrawn with no daemon call; one the daemon holds is `message.cancel`ed,
    its unsent steer taken back first. Either way its words come back for the composer."""
    harness, server = daemon
    host, queued, local = ids(3)
    go_live_when_submitted(harness, host)
    out = run_steps(core_probe, tmp_path, server.path, [
        create_step(harness, "req-c"),
        {"do": "submit", "conversation": "@draft:req-c", "message_id": host, "text": "fix the parser"},
        {"do": "submit", "conversation": "@draft:req-c", "message_id": queued, "text": "also the lexer"},
        {"do": "pump"},
        {"do": "steer", "key": queued, "conversation": "@conv:req-c"},
        {"do": "recall", "key": queued, "state": "queued"},
        {"do": "submit", "conversation": "@conv:req-c", "message_id": local, "text": "and the docs", "steer": True},
        {"do": "recall", "key": local, "state": "sending"},
        {"do": "pump"},
    ])
    recalled = out["results"][5]["result"]
    assert recalled["outcome"] == "recalled" and recalled["text"] == "also the lexer"
    assert recalled["receipt"]["state"] == "cancelled"
    assert out["results"][7]["result"] == {"outcome": "recalled", "text": "and the docs", "staged": [], "receipt": None}
    steers = by_key(out["steers"], "message_id")
    assert steers[queued]["state"] == "withdrawn" and steers[local]["state"] == "withdrawn"
    assert by_key(out["entries"])[local]["state"] == "withdrawn"
    assert steer_requests(server) == [] and not any("message.steer" in c or local in c for c in out["calls"])
    assert out["results"][8]["report"]["sent"] == []
    assert harness.store.message(queued)["state"] == "cancelled"


def test_c24_9_esc_on_a_steer_already_read_changes_nothing_and_never_interrupts(core_probe, tmp_path, harness):
    """The daemon takes a steering message's cancel only before its frame is written;
    after, `too-late`: nothing changes and no `turn.interrupt` is sent, since the
    running turn is another message's. Taken in time, its words come back even when
    another client sent it. A withdrawal (the queue tray's) of a message steered
    meanwhile is refused the same way, after one `message.status`."""
    cid = harness.create()["conversation_id"]
    host, steered = ids(2)
    harness.submit(cid, "fix the parser", message_id=host)
    harness.submit(cid, "also the lexer", message_id=steered, after=host)
    record_state(harness, steered, "steering", f"steer:{host}")
    too_late = {"id": "fixture", "ok": False, "v": 1, "error": {
        "code": 2, "message": "too-late: the provider may already have this message", "fix": "use turn.interrupt"}}
    status = json.loads(protocol.encode(protocol.ok("", {"messages": statuses(harness, steered)})))
    cancelled = {**statuses(harness, steered)[0], "state": "cancelled", "state_reason": "withdrawn"}
    script = scripted(tmp_path, [("message.cancel", too_late), ("message.cancel", too_late),
                                 ("message.cancel", json.loads(protocol.encode(protocol.ok("", cancelled)))),
                                 ("message.cancel", too_late), ("message.status", status),
                                 ("message.cancel", too_late)])
    out = run_steps(core_probe, tmp_path, script, [
        {"do": "recall", "key": steered, "state": "steering"},
        {"do": "recall", "key": steered, "state": "queued"},
        {"do": "recall", "key": steered, "state": "steering", "text": "also the lexer"},
        {"do": "stop", "key": steered, "state": "queued"},
        {"do": "stop", "key": steered, "state": "steering"},
    ])
    late, late_queued, recalled, withdraw_queued, withdraw_steering = out["results"]
    assert late["result"] == {"outcome": "too-late", "receipt": None}
    assert late_queued["result"] == {"outcome": "too-late", "receipt": None}
    assert recalled["result"]["outcome"] == "recalled" and recalled["result"]["text"] == "also the lexer"
    assert withdraw_queued["error"]["kind"] == "engine" and "steerTooLate" in withdraw_queued["error"]["message"]
    assert withdraw_steering["error"]["kind"] == "engine" and "steerTooLate" in withdraw_steering["error"]["message"]
    assert [c.split(" ")[0] for c in out["calls"]] == ["message.cancel", "message.cancel", "message.cancel",
                                                       "message.cancel", "message.status", "message.cancel"]


def test_c24_9_esc_on_a_message_seen_sending_asks_the_daemon_where_it_is(core_probe, tmp_path, harness):
    """The app saw the steer before its receipt ("sending"); by the time Esc is
    handled its submit was answered. The daemon is asked where the message is, and a
    message still queued is taken back, not reported too late (review of a4a3414a)."""
    cid = harness.create()["conversation_id"]
    (mid,) = ids(1)
    receipt = harness.submit(cid, "also the lexer", message_id=mid)
    status = {"messages": statuses(harness, mid)}
    cancelled = {**receipt, "state": "cancelled", "state_reason": "withdrawn"}
    script = scripted(tmp_path, [("message.submit", json.loads(protocol.encode(protocol.ok("", receipt)))),
                                 ("message.status", json.loads(protocol.encode(protocol.ok("", status)))),
                                 ("message.cancel", json.loads(protocol.encode(protocol.ok("", cancelled))))])
    out = run_steps(core_probe, tmp_path, script, [
        {"do": "know_chain", "conversation": cid, "last": None},
        {"do": "submit", "conversation": cid, "message_id": mid, "text": "also the lexer"},
        {"do": "pump"},
        {"do": "recall", "key": mid, "state": "sending"},
    ])
    result = out["results"][-1]["result"]
    assert result["outcome"] == "recalled" and result["text"] == "also the lexer"
    assert [c.split(" ")[0] for c in out["calls"]] == ["message.submit", "message.status", "message.cancel"]


def test_c24_9_a_withdrawal_that_cannot_read_where_the_message_is_never_interrupts(core_probe, tmp_path, harness):
    """After `too-late`, the app interrupts only a message it read as running. The
    daemon points an interrupt of a steering message at its host turn, so a failed
    `message.status` is reported, not guessed past (review of a4a3414a)."""
    cid = harness.create()["conversation_id"]
    (mid,) = ids(1)
    harness.submit(cid, "also", message_id=mid)
    from subfleet.daemon import busy_answer
    too_late = {"id": "fixture", "ok": False, "v": 1, "error": {
        "code": 2, "message": "too-late: the provider may already have this message", "fix": "use turn.interrupt"}}
    script = scripted(tmp_path, [("message.cancel", too_late),
                                 ("message.status", json.loads(busy_answer("the daemon is serving 512 connections")))])
    out = run_steps(core_probe, tmp_path, script, [{"do": "stop", "key": mid, "state": "queued"}])
    assert out["results"][0]["error"]["kind"] == "daemon" and out["results"][0]["error"]["code"] == 69
    assert [c.split(" ")[0] for c in out["calls"]] == ["message.cancel", "message.status"]


@pytest.mark.parametrize("state,interrupts", [("starting", True), ("running", True), ("approval-needed", True),
                                              ("queued", False), ("complete", False), ("cancelled", False)])
def test_c24_9_a_withdrawal_answered_too_late_interrupts_only_a_message_read_as_its_own_turn_s(
        core_probe, tmp_path, harness, state, interrupts):
    """C-29.7: after `too-late`, Stop reads where the message is and interrupts it only
    while it is its own turn's (waiting to start, starting, running or asking). One
    queued again (another client could steer it before the interrupt arrived, and the
    daemon would point that interrupt at the host) or already settled is left alone."""
    cid = harness.create()["conversation_id"]
    (mid,) = ids(1)
    harness.submit(cid, "also", message_id=mid)
    too_late = {"id": "fixture", "ok": False, "v": 1, "error": {
        "code": 2, "message": "too-late: the provider may already have this message", "fix": "use turn.interrupt"}}
    (row,) = statuses(harness, mid)
    now = {**row, "state": state}
    answers = [("message.cancel", too_late),
               ("message.status", json.loads(protocol.encode(protocol.ok("", {"messages": [now]}))))]
    if interrupts:
        answers.append(("turn.interrupt", json.loads(protocol.encode(protocol.ok("", {**now, "stop_requested": True})))))
    out = run_steps(core_probe, tmp_path, scripted(tmp_path, answers), [{"do": "stop", "key": mid, "state": "queued"}])
    result = out["results"][0]
    assert "error" not in result, result
    assert [c.split(" ")[0] for c in out["calls"]] == ["message.cancel", "message.status"] + (
        ["turn.interrupt"] if interrupts else [])
    assert result["receipt"]["state"] == state


def test_c24_9_slash_commands_and_shell_input_are_never_steered(core_probe, tmp_path):
    """They wait for the turn to end, as in Claude Code (DESIGN.md section 9)."""
    texts = ["/compact", "  /model opus", "!ls -la", "\n!git status", "fix it", "a/b and !c", "", "   "]
    assert run_probe(core_probe, "steerable", write_json(tmp_path / "t.json", texts)) == [
        False, False, False, False, True, True, True, True]


def test_c28_3_an_older_journal_opens_and_steers_never_enter_its_entries(core_probe, tmp_path, daemon):
    """A journal written before steer (no `steers` key) opens; a journal with steers
    keeps them out of `entries`, whose kinds an older app knows, so that app still
    opens it (and drops the steers: their messages simply stay queued)."""
    harness, server = daemon
    host, steered = ids(2)
    go_live_when_submitted(harness, host)
    journal = tmp_path / "support" / "outbox.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"version": 1, "nextOrder": 1, "entries": [], "chains": {}}))
    out = run_steps(core_probe, tmp_path, server.path, [
        {"do": "reload"}, create_step(harness, "req-o"),
        {"do": "submit", "conversation": "@draft:req-o", "message_id": host, "text": "fix the parser"},
        {"do": "pump"},
        {"do": "submit", "conversation": "@conv:req-o", "message_id": steered, "text": "also", "steer": True},
        {"do": "pump"},
    ], journal)
    assert out["results"][0]["states"] == []
    written = json.loads(journal.read_text())
    assert {e["kind"] for e in written["entries"]} == {"conversation.create", "message.submit"}
    assert [s["messageID"] for s in written["steers"]] == [steered]
    assert set(written) == {"version", "nextOrder", "entries", "chains", "steers"}


# --- the watch feed: notifications and the sidebar (D-24) -----------------------------------


def test_c24_9_a_steer_never_notifies_twice(core_probe, tmp_path, harness):
    """A steered message settles inside its host's turn: that turn's completion is the
    one notification, however the steer settled. While it steers, the conversation is active."""
    focused = harness.create(title="Focused")["conversation_id"]
    cid, host, turn = running_host(harness, title="Elsewhere")
    listed = harness.call("conversation.list")
    baseline = watch(harness, 0)
    quiet = watch(harness, baseline["next"])                  # the first empty page: notifications may post
    assert quiet["changes"] == []
    steered = harness.submit(cid, "also check the lexer", after=host)["message_id"]
    unanswered = harness.submit(cid, "and the docs", after=steered)["message_id"]
    for mid in (steered, unanswered):
        record_state(harness, mid, "steering", f"steer:{host}")
    steering = watch(harness, quiet["next"])
    record_steer_event(turn, "steer.delivered", steered)
    record_state(harness, steered, "steered", f"steered:{host}", served={"steered_into": host})
    record_state(harness, unanswered, "steered", f"steered-unanswered:{host}", served={"steered_into": host})
    turn.feed(claude_assistant("msg_1", [{"type": "text", "text": "Done."}]), claude_result())
    settled = watch(harness, steering["next"])
    steps = [{"list": listed}, {"focus": focused}, {"watch": baseline}, {"watch": quiet}, {"watch": steering}]
    during = store(core_probe, tmp_path, steps)
    after = store(core_probe, tmp_path, steps + [{"watch": settled}])
    conversations = {c["id"]: c for c in during["conversations"]}
    assert during["watch_baselined"] is True
    assert conversations[cid]["active"] is True and during["notifications"] == []
    assert [n["id"] for n in after["notifications"]] == [f"complete:{host}"]
    conversations = {c["id"]: c for c in after["conversations"]}
    assert conversations[cid]["active"] is False
    # Folded again from the start, nothing more is posted.
    again = store(core_probe, tmp_path, steps + [{"watch": settled}, {"watch": watch(harness, 0)}])
    assert [n["id"] for n in again["notifications"]] == [f"complete:{host}"]


def test_c24_9_the_focused_timeline_follows_the_feed_and_a_steer_has_no_chip_of_its_own(core_probe, tmp_path, harness):
    cid, host, turn = running_host(harness)
    steered = harness.submit(cid, "also check the lexer", after=host)["message_id"]
    opened = harness.call("conversation.open", conversation_id=cid)
    baseline = watch(harness, 0)
    record_state(harness, steered, "steering", f"steer:{host}")
    record_steer_event(turn, "steer.delivered", steered)
    record_state(harness, steered, "steered", f"steered:{host}", served={"steered_into": host})
    turn.feed(claude_assistant("msg_1", [{"type": "text", "text": "Done."}]), claude_result())
    out = store(core_probe, tmp_path, [
        {"capabilities": steer_capabilities(harness)}, {"open": opened}, {"focus": cid}, {"watch": baseline},
        {"events": page(harness, cid), "conversation_id": cid}, {"watch": watch(harness, baseline["next"])},
        {"open": harness.call("conversation.open", conversation_id=cid)},
    ])
    assert out["statuses"][steered] == READ
    assert host in out["chips"] and steered not in out["chips"]
    items = out["items"][cid]
    assert items.count(f"person:{steered}") == 1
    assert items.index(f"person:{steered}") > items.index(f"person:{host}")
    assert out["notifications"] == []                                   # focused: nothing posted


def test_c24_9_journaled_steers_restore_after_a_restart(core_probe, tmp_path, harness):
    """What the outbox holds says what the daemon has not: a steer still on its way
    reads as steering, a refused one says why its message stayed queued."""
    cid, host, _ = running_host(harness)
    pending = harness.submit(cid, "also", after=host)["message_id"]
    refused = harness.submit(cid, "and", after=pending)["message_id"]
    opened = harness.call("conversation.open", conversation_id=cid)
    out = store(core_probe, tmp_path, [
        {"capabilities": steer_capabilities(harness)}, {"open": opened},
        {"steers": [journaled(pending, cid, "sending"), journaled(refused, cid, "refused", "not-next")]},
    ])
    assert out["statuses"][pending] == UNREAD
    assert out["statuses"][refused] == REFUSED_WORDS["not-next"]
    assert out["steer_offers"][pending] is False                    # already on its way
    # A journaled steer of a message that runs by now (the daemon dispatched it
    # first) does not make it read as unread: it is past steering.
    ran = store(core_probe, tmp_path, [{"capabilities": steer_capabilities(harness)}, {"open": opened},
                                       {"steers": [journaled(host, cid, "queued")]}])
    assert not ran["statuses"][host].startswith("Unread")
    # An answered one is its receipts' to show.
    done = store(core_probe, tmp_path, [{"open": opened}, {"steers": [journaled(pending, cid, "acknowledged")]}])
    assert done["statuses"][pending] == QUEUED


# --- after the steer review (2026-09-28): Esc, the missed flag, recalled words, late steers ---


def test_c24_9_esc_passes_over_a_too_late_steer_only_while_it_is_still_in_that_turn(core_probe, tmp_path, harness):
    """Review finding 16, through `TooLateSteers` as `UIModel.escape` keeps it. Esc on S,
    whose frame is written, answers too-late, and the next Esc stops the turn. Stopped,
    S goes back to the queue and A (queued for later) runs: Esc now takes S back rather
    than stopping A. Steered into A, S is in another turn, so Esc takes it back again;
    too late there as well, the next Esc stops A."""
    cid, host, _ = running_host(harness)
    queued_for_later = harness.submit(cid, "then the docs", after=host)["message_id"]
    steered = harness.submit(cid, "also the lexer", after=queued_for_later)["message_id"]
    make_live(harness, cid, host)
    assert harness.call("message.steer", message_id=steered, into=host)["state"] == "steering"
    first = statuses(harness, host, queued_for_later, steered)
    record_state(harness, host, "interrupted", "stopped")
    record_state(harness, steered, "queued", "steer-missed: interrupt-cancelled")
    record_state(harness, queued_for_later, "running")
    stopped = statuses(harness, host, queued_for_later, steered)
    record_state(harness, steered, "steering", f"steer:{queued_for_later}")
    again = statuses(harness, steered)
    out = fold(core_probe, tmp_path, cid, [
        {"receipts": first},
        {"esc": True},                                     # takes S back ...
        {"too_late": steered},                             # ... too late: the next Esc stops the turn
        {"esc": True},
        {"receipts": stopped},
        {"esc": True},                                     # S is queued again: taken back, A runs on
        {"receipts": again},
        {"esc": True},                                     # steered into A: not the turn it was too late for
        {"too_late": steered},
        {"esc": True},                                     # too late in A as well: stop A
    ])
    stop_words = "Claude already has it; it joins at the next step. Press Esc again to stop the turn."
    assert [r for r in out["results"] if r.startswith(("escape:", "too-late:"))] == [
        f"escape:recall:{steered}", f"too-late:{stop_words}", f"escape:stop:{host}", f"escape:recall:{steered}",
        f"escape:recall:{steered}", f"too-late:{stop_words}", f"escape:stop:{queued_for_later}"]


def test_c24_9_esc_passes_over_a_queued_steer_the_daemon_says_has_left_the_queue(core_probe, tmp_path, harness):
    """C-29.7: the app shows a refused steer S queued, but the daemon answers Esc's cancel
    `too-late`: S left the queue meanwhile (another client steered it, its frame written).
    Esc does not ask about S again while the app still shows it as it was, nor once it
    sees S steering in that turn; once S is back in the queue (the turn ended without
    reading it), Esc takes it back."""
    cid, host, _ = running_host(harness)
    steered = harness.submit(cid, "also the lexer", after=host)["message_id"]
    first = statuses(harness, host, steered)
    record_state(harness, steered, "steering", f"steer:{host}")
    steering = statuses(harness, steered)
    record_state(harness, steered, "queued", "steer-missed: the turn ended before the provider read it")
    missed = statuses(harness, steered)
    out = fold(core_probe, tmp_path, cid, [
        {"receipts": first},
        {"steer_answer": {"message_id": steered, "refusal": {"reason": "not-steerable", "message": "not-steerable"}}},
        {"esc": True},                                     # takes S back ...
        {"too_late": steered},                             # ... but it has left the queue
        {"esc": True},                                     # shown as it was: passed over, the turn stops
        {"receipts": steering},
        {"esc": True},                                     # steering in that turn: still passed over
        {"receipts": missed},
        {"esc": True},                                     # back in the queue: taken back
    ])
    stop_words = "Claude already has it; it joins at the next step. Press Esc again to stop the turn."
    assert [r for r in out["results"] if r.startswith(("escape:", "too-late:"))] == [
        f"escape:recall:{steered}", f"too-late:{stop_words}", f"escape:stop:{host}", f"escape:stop:{host}",
        f"escape:recall:{steered}"]


def test_c24_9_a_too_late_answer_says_what_the_next_esc_does(core_probe, tmp_path, harness):
    """Review finding 16: with an older steer still unread, the next Esc takes that one
    back, and the words say so; only with none left does it stop the turn."""
    cid, host, _ = running_host(harness)
    older = harness.submit(cid, "also the lexer", after=host)["message_id"]
    newer = harness.submit(cid, "and the parser tests", after=older)["message_id"]
    make_live(harness, cid, host)
    for mid in (older, newer):
        assert harness.call("message.steer", message_id=mid, into=host)["state"] == "steering"
    out = fold(core_probe, tmp_path, cid, [
        {"receipts": statuses(harness, host, older, newer)},
        {"esc": True}, {"too_late": newer, "assistant": "Codex"},
        {"esc": True}, {"too_late": older, "assistant": "Codex"},
        {"esc": True},
    ])
    told = "Codex already has it; it joins at the next step. Press Esc again to "
    assert [r for r in out["results"] if r.startswith(("escape:", "too-late:"))] == [
        f"escape:recall:{newer}", f"too-late:{told}take back the message before it.",
        f"escape:recall:{older}", f"too-late:{told}stop the turn.", f"escape:stop:{host}"]
    # Too late for the older one while the newer is still unread: the next Esc takes the newer.
    out = fold(core_probe, tmp_path, cid, [{"receipts": statuses(harness, host, older, newer)},
                                           {"too_late": older, "assistant": "Codex"}, {"esc": True}])
    assert [r for r in out["results"] if r.startswith(("escape:", "too-late:"))] == [
        f"too-late:{told}take back the newer message.", f"escape:recall:{newer}"]


def test_c24_9_a_missed_steer_steered_again_reads_unread_in_its_new_turn(core_probe, tmp_path, harness):
    """Review finding 19: the missed flag belongs to the turn that missed it. Steered
    again into the next turn ("Send now"), it is unread there, not "until the turn ends"."""
    cid, host, turn = running_host(harness)
    steered = harness.submit(cid, "also the lexer", after=host)["message_id"]
    make_live(harness, cid, host)
    harness.call("message.steer", message_id=steered)
    record_steer_event(turn, "steer.missed", steered, why="stopped")
    missed_here = statuses(harness, steered)
    record_state(harness, host, "interrupted", "stopped")
    record_state(harness, steered, "queued", "steer-missed: stopped")
    requeued = statuses(harness, host, steered)
    next_host = harness.submit(cid, "the next turn", after=steered)["message_id"]
    record_state(harness, next_host, "running")
    record_state(harness, steered, "steering", f"steer:{next_host}")
    out = fold(core_probe, tmp_path, cid, [
        {"receipts": missed_here}, {"page": page(harness, cid), "snapshot": True},
        {"receipts": requeued, "snapshot": True},
        {"receipts": statuses(harness, next_host, steered), "snapshot": True},
        {"page": {"events": [], "next": 0, "reset": True}}, {"page": page(harness, cid), "snapshot": True},
    ])
    words = [snapshot["turns"][steered]["status_text"] for snapshot in out["snapshots"]]
    assert words == [MISSED, MISSED, UNREAD, UNREAD]


def test_c24_9_words_esc_takes_back_are_kept_in_the_draft(core_probe, tmp_path):
    """Review finding 18: recalled words and images join the conversation's draft on disk,
    ahead of what it held, so leaving the conversation or quitting keeps them."""
    image = {"path": str(tmp_path / "shot.png"), "sha256": "a" * 64, "media_type": "image/png", "bytes": 10}
    drafts = tmp_path / "drafts"
    cases = [
        ({"existing": None, "text": "also the lexer", "staged": []}, "also the lexer", []),
        ({"existing": {"text": "half a thought", "attachments": [], "settings": None, "updated_at": "x"},
          "text": "also the lexer", "staged": [image]}, "also the lexer\n\nhalf a thought", [image]),
        ({"existing": None, "text": "", "staged": [image]}, "", [image]),       # images alone are kept too
    ]
    for index, (recall, text, staged) in enumerate(cases):
        out = run_probe(core_probe, "recall-draft", drafts, f"cv-{index}", write_json(tmp_path / f"r{index}.json", recall))
        assert out["draft"]["text"] == text and out["draft"]["attachments"] == staged
        assert out["mode"] == "600"


def test_c24_9_a_steer_that_arrives_after_its_turn_ended_is_refused_not_joined_to_the_next(core_probe, tmp_path,
                                                                                            daemon):
    """Review finding 17: the journaled steer names the turn it was sent to. The app
    stops before it is answered; by the resend that turn has ended and the message
    queued for later runs. The daemon refuses it (`no-live-turn`): it stays queued, to
    run as its own turn, never inside another under that turn's settings."""
    harness, server = daemon
    host, later, steered = ids(3)
    runners = go_live_when_submitted(harness, host)
    journal = tmp_path / "support" / "outbox.json"
    server.faults[("message.steer", steered)] = "refuse"          # the daemon never saw it
    first = run_steps(core_probe, tmp_path, server.path, [
        create_step(harness, "req-l"),
        {"do": "submit", "conversation": "@draft:req-l", "message_id": host, "text": "fix the parser"},
        {"do": "pump"},
        {"do": "submit", "conversation": "@conv:req-l", "message_id": later, "text": "then the docs"},
        {"do": "submit", "conversation": "@conv:req-l", "message_id": steered, "text": "also", "steer": True,
         "into": host},
        {"do": "pump"},
    ], journal)
    assert first["steers"][0]["into"] == host and first["steers"][0]["state"] == "queued"
    cid = harness.store.message(host)["conversation_id"]
    record_state(harness, host, "complete")
    runners.pop(host).stop()
    harness.service.runners.clear()
    make_live(harness, cid, later)                                 # the next turn runs now
    again = run_steps(core_probe, tmp_path, server.path, [
        {"do": "reload"}, {"do": "advance", "seconds": 30}, {"do": "pump"},
    ], journal)
    (steer,) = again["steers"]
    assert steer["state"] == "refused" and steer["failure"]["reason"] == "no-live-turn"
    assert [r["args"] for r in server.requests if r["op"] == "message.steer"][-1] == {"message_id": steered,
                                                                                     "into": host}
    assert harness.store.message(steered)["state"] == "queued"
