"""Labelled handoffs and the withdrawal guard they share with message.cancel:
C-30.3, C-24.7 (review IR-2, IR-28).

The service runs here against a real conversation store and a real job store
(`subfleet.store.Store`) in a temporary state root, with a stand-in daemon that
has no control loop: each test drives the dispatcher step it is about, so an
interleaving that is a race in the daemon is a fixed order here. The Claude
transcripts are the sessions kit's fixtures under `SUBFLEET_CLAUDE_DIR`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import stat
import uuid
from pathlib import Path

import pytest

from subfleet.adapters.base import AdapterError
from subfleet.contracts import Exit
from subfleet.conversations import service as service_module
from subfleet.conversations.service import CLAIMED, ConversationService
from subfleet.conversations.store import ConversationError
from subfleet.store import Store
from tests import sessions_fixtures as fx
from tests.conftest import make_lane

SESSION = "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"
ASK = {"model": "opus[1m]", "effort": None, "fast": False, "permission": "ask", "auto_continue": True}
CODEX = {"model": "gpt-6-astra", "effort": None, "fast": False, "permission": "read-only", "auto_continue": True}


class Daemon:
    """The daemon seams the service calls, without a control loop."""

    def __init__(self, root: Path):
        self.root = root
        self.store = Store(root / "state.sqlite3")
        self.store.put_lane(make_lane("claude-1"))
        self.policy = fx.policy()
        self.log = logging.getLogger("test-handoff")
        self.lane_runs: list[str] = []

    def _notify(self) -> None:
        pass

    def _lane_session_ids(self) -> list[str]:
        return list(self.lane_runs)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))         # no real ~/.codex or ~/.claude
    home = fx.claude_home(tmp_path, monkeypatch)
    workspace = tmp_path / "work"
    workspace.mkdir()
    daemon = Daemon(tmp_path / "state")
    service = ConversationService(daemon)
    yield type("World", (), {"home": home, "workspace": workspace, "daemon": daemon, "service": service,
                             "store": service.store})
    service.close()
    daemon.store.close()


def source(world, *texts: str, native: str | None = SESSION) -> tuple[str, list[str]]:
    """A Claude conversation bound to a fixture transcript, with queued messages."""
    if native:
        fx.transcript(world.home, native, [
            fx.typed_prompt("Port the ledger importer to v2.", uuid="p0", at=fx.ago(3600)),
            fx.assistant_text("the manifest is done", uuid="a0", at=fx.ago(60))], cwd=str(world.workspace))
    conversation, _ = world.store.create_conversation(
        provider="claude", workspace=str(world.workspace), workspace_kind="in-place", settings=ASK, origin="native",
        native_session_id=native, title="ledger importer")
    cid, ids, after = conversation["conversation_id"], [], None
    for text in texts:
        mid = str(uuid.uuid4())
        world.store.submit_message(conversation_id=cid, message_id=mid, after_message_id=after, text=text,
                                   attachments=[], settings=ASK)
        ids.append(mid)
        after = mid
    return cid, ids


def turn_job(world, mid: str, cid: str, *, state: str = "queued") -> str:
    message = world.store.message(mid)
    job_id = f"job-{mid[:8]}"
    world.daemon.store.add_job(job_id=job_id, request_id=f"turn:{mid}:{message['turn_seq']}",
                               payload_digest=message["digest"], kind="turn", state=state, workdir=str(world.workspace),
                               prompt_path=message["text_path"], sandbox="read-only", name=f"turn-{cid}")
    return job_id


def attempt(world, job_id: str, state: str = "running") -> None:
    world.daemon.store.add_attempt(attempt_id=f"{job_id}/a1", job_id=job_id, seq=1, lane_id="claude-1",
                                   model_requested="claude-opus-5-5", state=state)


def handoff(world, cid: str | None = None, *, request_id: str = "h-1", to=None, **source_arg) -> dict:
    args = {"request_id": request_id, "from": source_arg or {"conversation_id": cid},
            "to": to or {"provider": "codex", "settings": CODEX}}
    return world.service.op_conversation_handoff(args, None)


# --- the op -----------------------------------------------------------------------

def test_a_handoff_is_a_new_labelled_conversation_whose_first_message_is_the_brief(world):
    """C-30.3, D-18: a new conversation (origin `handoff`) on the other provider;
    its first message is the scrubbed brief, and `handoff_from` records the
    source's provider, native id, transcript path and the brief's SHA-256. It
    has no native session of its own until its first turn: never the source's."""
    cid, _ = source(world)
    out = handoff(world, cid)
    conversation = out["conversation"]
    assert out["created"] and conversation["origin"] == "handoff" and conversation["provider"] == "codex"
    assert conversation["native_session_id"] is None and conversation["conversation_id"] != cid
    record = conversation["handoff_from"]
    transcript = Path(record["transcript"])
    assert record["provider"] == "claude" and record["native_session_id"] == SESSION
    assert transcript.is_file() and transcript.name == f"{SESSION}.jsonl"
    assert record["conversation_id"] == cid
    brief = world.store.message(out["brief"]["message_id"])
    text = world.store.message_text(brief)
    assert record["brief_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert brief["seq"] == 1 and brief["origin"] == "handoff" and brief["state"] == "queued"
    assert brief["settings"] == CODEX
    assert "- Source provider: Claude Code" in text and f"- Source session: {SESSION}" in text
    assert "Port the ledger importer to v2." in text
    assert stat.S_IMODE(os.stat(brief["text_path"]).st_mode) == 0o600
    assert conversation["workspace"] == os.path.realpath(world.workspace)
    assert conversation["title"] == "ledger importer"


def test_pending_messages_move_in_order_and_leave_the_source_withdrawn(world):
    """IR-28, C-24.2: queued messages move behind the brief in their order, with new
    ids chained as a client would chain them, the target's settings and the same
    text; each is withdrawn from the source (`cancelled`, `handed-off:<new>`),
    which keeps its rows."""
    cid, ids = source(world, "first follow-up", "second follow-up")
    out = handoff(world, cid)
    new = out["conversation"]["conversation_id"]
    moved = [world.store.message(r["message_id"]) for r in out["moved"]]
    assert [world.store.message_text(m) for m in moved] == ["first follow-up", "second follow-up"]
    assert [m["seq"] for m in moved] == [2, 3] and all(m["origin"] == "person" for m in moved)
    assert moved[0]["after_message_id"] is None and moved[1]["after_message_id"] == moved[0]["message_id"]
    assert all(m["settings"] == CODEX and m["conversation_id"] == new for m in moved)
    assert [pair["from"] for pair in out["handoff_from"]["moved"]] == ids
    assert out["withdrawn"] == ids
    for mid in ids:
        left = world.store.message(mid)
        assert left["state"] == "cancelled" and left["state_reason"] == f"handed-off:{new}"
        assert left["conversation_id"] == cid
    # The client's next message chains on the last moved one (D-22).
    nxt = str(uuid.uuid4())
    world.store.submit_message(conversation_id=new, message_id=nxt, after_message_id=moved[-1]["message_id"],
                               text="third", attachments=[], settings=CODEX)


def test_the_same_request_returns_the_same_handoff_and_a_different_one_is_refused(world):
    """C-30.3, C-24.2: idempotent by request_id; the same id with another request is
    exit 2 and changes nothing."""
    cid, ids = source(world, "follow-up")
    first = handoff(world, cid)
    again = handoff(world, cid)
    assert not again["created"]
    assert again["conversation"]["conversation_id"] == first["conversation"]["conversation_id"]
    assert [m["message_id"] for m in again["moved"]] == [m["message_id"] for m in first["moved"]]
    count = world.store.one("SELECT COUNT(*) n FROM conversations")["n"]
    with pytest.raises(ConversationError) as err:
        handoff(world, cid, to={"provider": "claude", "settings": ASK})
    assert err.value.reason == "request-id-conflict" and err.value.code == 2
    created = world.store.create_conversation(provider="claude", workspace="/w", workspace_kind="in-place",
                                              settings=ASK, origin="new", request_id="h-create")[0]
    with pytest.raises(ConversationError) as err:
        handoff(world, cid, request_id="h-create")
    assert err.value.reason == "request-id-conflict"
    assert world.store.one("SELECT COUNT(*) n FROM conversations")["n"] == count + 1
    assert created["origin"] == "new"


@pytest.mark.parametrize("state", ["running", "starting", "approval-needed", "delivery-unknown"])
def test_a_source_with_a_live_turn_is_refused_and_nothing_changes(world, state):
    """IR-28, C-24.7: a message a provider may already have is never withdrawn; the
    handoff is refused, and the queued message behind it stays in the source."""
    cid, (live, queued) = source(world, "running now", "queued behind it")
    world.store.set_state(live, state)
    with pytest.raises(ConversationError) as err:
        handoff(world, cid)
    assert err.value.reason == "live-turn"
    assert world.store.message(queued)["state"] == "queued"
    assert world.store.by_request("h-1") is None


def test_a_waiting_message_moves_only_while_its_job_has_no_attempt(world):
    """IR-2, IR-28: a waiting message is withdrawn by the job store's guard, the one
    message.cancel uses. Its job is cancelled with the handoff's own marker on
    the cancel's audit event; with an attempt, the handoff is refused."""
    cid, (waiting, queued) = source(world, "waiting on capacity", "behind it")
    job_id = turn_job(world, waiting, cid, state="waiting")
    world.store.set_state(waiting, "waiting", reason="admission: sent to the daemon, which has not placed it yet", job_id=job_id)
    attempt(world, job_id, state="reserved")
    with pytest.raises(ConversationError) as err:
        handoff(world, cid)
    assert err.value.reason == "live-turn"
    assert world.daemon.store.get_job(job_id)["state"] == "waiting"
    world.daemon.store.connection.execute("DELETE FROM attempts WHERE job_id=?", (job_id,))

    out = handoff(world, cid)
    assert [world.store.message_text(world.store.message(m["message_id"])) for m in out["moved"]] == [
        "waiting on capacity", "behind it"]
    assert world.daemon.store.get_job(job_id)["state"] == "cancelled"
    event = world.daemon.store.one("SELECT data_json FROM events WHERE kind='job.cancel_requested' AND job_id=?",
                                   (job_id,))
    assert json.loads(event["data_json"]) == {"by": "conversation.handoff", "request_id": "h-1"}
    assert world.store.message(waiting)["state"] == "cancelled"


def test_a_retry_after_a_crash_still_moves_the_message_whose_job_it_cancelled(world):
    """IR-28: the job store's cancel and the conversation store's commit are two
    transactions. If the daemon dies between them and the tick settles the
    message `cancelled`, the same request finds its own marker and moves it."""
    cid, (waiting,) = source(world, "waiting on capacity")
    job_id = turn_job(world, waiting, cid, state="waiting")
    world.store.set_state(waiting, "waiting", reason="admission: sent to the daemon, which has not placed it yet", job_id=job_id)
    marker = {"by": "conversation.handoff", "request_id": "h-1"}
    assert world.service._cancel_job_without_attempt(job_id, by=marker)
    world.store.set_state(waiting, "cancelled", reason="cancelled: 130")      # what the tick does
    out = handoff(world, cid)
    assert [pair["from"] for pair in out["handoff_from"]["moved"]] == [waiting]
    assert world.store.message(waiting)["state_reason"].startswith("handed-off:")
    # Another request's marker is not this one's, and a person's cancel is not a handoff's.
    assert not world.service._cancel_job_without_attempt(job_id, by={**marker, "request_id": "h-2"})
    assert not world.service._cancel_job_without_attempt(job_id)


def test_a_rolled_back_handoff_keeps_a_missed_steer_running_next(world, monkeypatch):
    """C-24.5, C-30.3: a steer that missed its turn was dispatched ahead of a message
    queued for later; a handoff cancels its turn job and then fails. Put back, it keeps
    the `steer-missed:` mark the queue orders by, in its row and its change row, so it
    still runs next rather than behind the message queued for later."""
    cid, (later, missed) = source(world, "queued for later", "the steer that missed its turn")
    world.store.set_state(missed, "queued", reason="steer-missed: interrupt-cancelled")
    assert [m["message_id"] for m in world.store.next_dispatchable()] == [missed]
    job_id = turn_job(world, missed, cid, state="waiting")
    world.store.set_state(missed, "waiting", job_id=job_id)

    def fail_commit(*args, **kwargs):
        raise OSError("commit failed")
    monkeypatch.setattr(world.store, "commit_handoff", fail_commit)
    with pytest.raises(OSError):
        handoff(world, cid)
    row = world.store.message(missed)
    assert (row["state"], row["state_reason"]) == ("queued", "steer-missed: handoff-rolled-back")
    change = world.store.one("SELECT state_reason FROM changes WHERE message_id=? ORDER BY seq DESC LIMIT 1", (missed,))
    assert change["state_reason"] == "steer-missed: handoff-rolled-back"
    assert [m["message_id"] for m in world.store.next_dispatchable()] == [missed]
    assert world.store.message(later)["state"] == "queued"


@pytest.mark.parametrize("failure", [
    "cancel-refused", "cancel-error", "cancelled-then-error", "commit-error",
    "commit-interrupted", "commit-sql-error", "commit-deferred-error", "discard-error",
])
def test_every_failure_after_fencing_restores_the_source(world, monkeypatch, failure):
    """C-30.3, D-18: no failure after fencing strands the source. A cancelled
    turn gets a new admission in its original position; an uncancelled turn
    stays waiting. The SQL case fails after withdrawals and fence removal,
    proving that transaction rollback and the job-store repair work together.
    """
    cid, ids = source(world, "waiting first", "queued second", "queued third")
    waiting = ids[0]
    job_id = turn_job(world, waiting, cid, state="waiting")
    world.store.set_state(waiting, "waiting", reason="admission: sent to the daemon, which has not placed it yet", job_id=job_id)
    before = [world.store.message(mid) for mid in ids]
    cancel = world.service._cancel_job_without_attempt
    discard = world.store.discard_handoff

    def cancel_job(job_id, *, by):
        assert world.store.conversation(cid)["blocked_by"] == "handoff:h-1"
        if failure == "cancel-refused":
            attempt(world, job_id, state="reserved")
        if failure == "cancel-error":
            raise OSError("cancel failed")
        result = cancel(job_id, by=by)
        if failure == "cancelled-then-error":
            assert result
            raise OSError("cancel response failed")
        return result

    def fail_commit(*args, **kwargs):
        assert world.store.conversation(cid)["blocked_by"] == "handoff:h-1"
        assert world.daemon.store.get_job(job_id)["state"] == "cancelled"
        if failure == "commit-interrupted":
            raise KeyboardInterrupt("commit interrupted")
        raise OSError("commit failed")

    def fail_discard(prepared):
        discard(prepared)
        raise OSError("discard failed")

    monkeypatch.setattr(world.service, "_cancel_job_without_attempt", cancel_job)
    if failure in ("commit-error", "commit-interrupted", "discard-error"):
        monkeypatch.setattr(world.store, "commit_handoff", fail_commit)
    if failure == "discard-error":
        monkeypatch.setattr(world.store, "discard_handoff", fail_discard)
    if failure == "commit-sql-error":
        with world.store.transaction() as tx:
            tx.execute("CREATE TRIGGER fail_handoff_insert BEFORE INSERT ON conversations "
                       "WHEN NEW.origin='handoff' BEGIN SELECT RAISE(ABORT, 'handoff insert failed'); END")
    if failure == "commit-deferred-error":
        with world.store.transaction() as tx:
            tx.execute("CREATE TABLE handoff_commit_failure (source TEXT REFERENCES conversations(conversation_id) "
                       "DEFERRABLE INITIALLY DEFERRED)")
            tx.execute("CREATE TRIGGER fail_handoff_commit AFTER INSERT ON conversations WHEN NEW.origin='handoff' "
                       "BEGIN INSERT INTO handoff_commit_failure VALUES ('missing-source'); END")

    expected_error = (ConversationError if failure == "cancel-refused" else
                      KeyboardInterrupt if failure == "commit-interrupted" else
                      sqlite3.IntegrityError if failure in ("commit-sql-error", "commit-deferred-error") else OSError)
    with pytest.raises(expected_error) as err:
        handoff(world, cid)
    if failure == "cancel-refused":
        assert err.value.reason == "live-turn"
    assert world.store.conversation(cid)["blocked_by"] is None
    assert cid not in world.service._handing_off
    assert world.store.by_request("h-1") is None
    assert world.store.one("SELECT COUNT(*) n FROM conversations")["n"] == 1
    assert sorted(p.name for p in world.store.dir.iterdir()) == [cid]
    rows = world.store.query("SELECT message_id FROM messages WHERE conversation_id=? ORDER BY seq", (cid,))
    assert [row["message_id"] for row in rows] == ids
    after = [world.store.message(mid) for mid in ids]
    for original, restored in zip(before, after):
        for field in ("seq", "after_message_id", "origin", "digest", "text_path", "settings", "attachments"):
            assert restored[field] == original[field]
    assert [message["state"] for message in after[1:]] == ["queued", "queued"]
    if failure in ("cancel-refused", "cancel-error"):
        assert after[0] == before[0]
        assert world.daemon.store.get_job(job_id)["state"] == "waiting"
    else:
        assert after[0]["state"] == "queued" and after[0]["state_reason"] == "handoff-rolled-back"
        assert after[0]["turn_seq"] == before[0]["turn_seq"] + 1
        assert after[0]["job_id"] is None and world.service._turn_job(after[0]) is None
        assert world.daemon.store.get_job(job_id)["state"] == "cancelled"
        assert [m["message_id"] for m in world.store.next_dispatchable()] == [waiting]


@pytest.mark.parametrize("claimed", [False, True])
def test_a_failed_handoff_restores_a_message_whose_job_was_not_bound(world, monkeypatch, claimed):
    """D-18, IR-1: a crash before binding a newly created turn job does not
    hide that job from recovery; its request id still identifies the message.
    """
    cid, ids = source(world, "first", "second")
    mid = ids[0]
    if claimed:
        world.store.set_state(mid, "waiting", reason=CLAIMED)
    job_id = turn_job(world, mid, cid)
    before = world.store.message(mid)
    assert before["job_id"] is None

    def fail_commit(*args, **kwargs):
        assert world.daemon.store.get_job(job_id)["state"] == "cancelled"
        raise OSError("commit failed before binding the old job")

    monkeypatch.setattr(world.store, "commit_handoff", fail_commit)
    with pytest.raises(OSError):
        handoff(world, cid)
    restored = world.store.message(mid)
    assert restored["state"] == "queued" and restored["job_id"] is None
    assert restored["turn_seq"] == before["turn_seq"] + 1
    assert restored["seq"] == before["seq"]
    assert world.service._turn_job(restored) is None
    assert world.store.conversation(cid)["blocked_by"] is None
    assert [m["message_id"] for m in world.store.next_dispatchable()] == [mid]
    assert world.store.message(ids[1])["state"] == "queued"


def test_a_failure_reading_the_committed_handoff_keeps_its_messages_and_texts(world, monkeypatch):
    """D-18: an exception after the transaction commits cannot discard the
    now-referenced texts or restore messages that already moved. A retry
    returns the committed result with its brief and moved texts intact.
    """
    cid, ids = source(world, "waiting first", "queued second")
    job_id = turn_job(world, ids[0], cid, state="waiting")
    world.store.set_state(ids[0], "waiting", reason="admission: sent to the daemon, which has not placed it yet", job_id=job_id)
    conversation = world.store.conversation

    def fail_read(conversation_id):
        result = conversation(conversation_id)
        if result["origin"] == "handoff":
            raise OSError("committed response could not be read")
        return result

    monkeypatch.setattr(world.store, "conversation", fail_read)
    with pytest.raises(OSError, match="committed response"):
        handoff(world, cid)
    monkeypatch.setattr(world.store, "conversation", conversation)
    assert world.store.conversation(cid)["blocked_by"] is None
    assert world.store.by_request("h-1") is not None
    out = handoff(world, cid)
    assert out["created"] is False
    assert [pair["from"] for pair in out["handoff_from"]["moved"]] == ids
    assert world.store.message_text(world.store.message(out["brief"]["message_id"]))
    assert [world.store.message_text(world.store.message(m["message_id"])) for m in out["moved"]] == [
        "waiting first", "queued second"]
    assert all(world.store.message(mid)["state_reason"].startswith("handed-off:") for mid in ids)


@pytest.mark.parametrize("crash_at", ["before-cancel", "after-cancel", "after-settlement"])
def test_a_restart_lifts_a_stale_handoff_fence_before_dispatch(world, monkeypatch, crash_at):
    """C-30.3: close and reopen both on-disk stores and run the new service's
    first control-loop tick, without a handoff request. Recovery precedes
    dispatch, restores the cancelled message ahead of its followers, and
    runs once even when the old process's in-memory handoff guard was set.
    """
    cid, ids = source(world, "waiting first", "queued second", "queued third")
    waiting = ids[0]
    job_id = turn_job(world, waiting, cid, state="waiting")
    world.store.set_state(waiting, "waiting", reason="admission: sent to the daemon, which has not placed it yet", job_id=job_id)
    turn_seq = world.store.message(waiting)["turn_seq"]
    assert world.store.fence(cid, "handoff:h-1")
    world.service._handing_off.add(cid)
    if crash_at != "before-cancel":
        assert world.service._cancel_job_without_attempt(
            job_id, by={"by": "conversation.handoff", "request_id": "h-1"})
    if crash_at == "after-settlement":
        world.service._settle_unstarted()
        assert world.store.message(waiting)["state"] == "cancelled"
    world.service.close()
    world.daemon.store.close()

    restarted_daemon = Daemon(world.daemon.root)
    restarted = ConversationService(restarted_daemon)
    try:
        assert restarted.store.conversation(cid)["blocked_by"] == "handoff:h-1"
        dispatched = []

        def submit(conversation, message):
            assert conversation["blocked_by"] is None
            dispatched.append(message["message_id"])
            raise AdapterError("hold the restored message for inspection", code=int(Exit.OPERATIONAL))  # refused for now (C-26.1)

        monkeypatch.setattr(restarted, "_submit_turn", submit)
        restarted.tick()
        assert restarted.store.conversation(cid)["blocked_by"] is None
        assert restarted.store.by_request("h-1") is None
        rows = restarted.store.query("SELECT message_id FROM messages WHERE conversation_id=? ORDER BY seq", (cid,))
        assert [row["message_id"] for row in rows] == ids
        restored = restarted.store.message(waiting)
        if crash_at == "before-cancel":
            assert dispatched == [] and restored["state"] == "waiting"
            assert restored["job_id"] == job_id and restored["turn_seq"] == turn_seq
        else:
            assert dispatched == [waiting] and restored["state"] == "queued"
            assert restored["job_id"] is None and restored["turn_seq"] == turn_seq + 1
            assert restarted._turn_job(restored) is None
        assert [restarted.store.message(mid)["state"] for mid in ids[1:]] == ["queued", "queued"]
        restarted.tick()
        assert restarted.store.message(waiting)["turn_seq"] == restored["turn_seq"]
    finally:
        restarted.close()
        restarted_daemon.store.close()


def test_a_retry_after_a_fenced_crash_moves_the_restored_message_in_order(world):
    """D-18: retrying the interrupted request first undoes its durable fence,
    then includes the formerly waiting message ahead of every queued follower.
    """
    cid, ids = source(world, "waiting first", "queued second", "queued third")
    job_id = turn_job(world, ids[0], cid, state="waiting")
    world.store.set_state(ids[0], "waiting", reason="admission: sent to the daemon, which has not placed it yet", job_id=job_id)
    assert world.store.fence(cid, "handoff:h-1")
    assert world.service._cancel_job_without_attempt(job_id, by={"by": "conversation.handoff", "request_id": "h-1"})
    world.service._settle_unstarted()
    out = handoff(world, cid)
    assert [pair["from"] for pair in out["handoff_from"]["moved"]] == ids
    assert [world.store.message_text(world.store.message(m["message_id"])) for m in out["moved"]] == [
        "waiting first", "queued second", "queued third"]
    assert world.store.conversation(cid)["blocked_by"] is None
    assert all(world.store.message(mid)["state_reason"].startswith("handed-off:") for mid in ids)


def test_a_tick_during_handoff_keeps_its_fence_until_commit(world, monkeypatch):
    """D-18: stale-fence recovery leaves an active handoff alone. The tick may
    settle its cancelled job, but cannot dispatch a queued follower while the
    handoff is between cancellation and commit.
    """
    cid, ids = source(world, "waiting first", "queued second")
    job_id = turn_job(world, ids[0], cid, state="waiting")
    world.store.set_state(ids[0], "waiting", reason="admission: sent to the daemon, which has not placed it yet", job_id=job_id)
    cancel = world.service._cancel_job_without_attempt

    def cancel_and_tick(job_id, *, by):
        assert cancel(job_id, by=by)
        world.service.tick()
        assert world.store.conversation(cid)["blocked_by"] == "handoff:h-1"
        assert world.store.message(ids[0])["state"] == "cancelled"
        assert world.store.message(ids[1])["state"] == "queued"
        return True

    monkeypatch.setattr(world.service, "_cancel_job_without_attempt", cancel_and_tick)
    monkeypatch.setattr(world.service, "_submit_turn", lambda *a: pytest.fail("submitted during a handoff"))
    out = handoff(world, cid)
    assert [pair["from"] for pair in out["handoff_from"]["moved"]] == ids
    assert world.store.conversation(cid)["blocked_by"] is None


@pytest.mark.parametrize("cancel_kind", [
    "person-cancel", "other-handoff", "cancel-request-only", "cancelled-with-attempt",
])
def test_stale_fence_recovery_restores_only_its_own_cancelled_unattempted_jobs(world, cancel_kind):
    """IR-2, D-18: a cancellation request alone is not proof of a completed
    no-attempt cancellation, and another actor's withdrawal remains withdrawn.
    """
    cid, (mid,) = source(world, "waiting")
    job_id = turn_job(world, mid, cid, state="waiting")
    world.store.set_state(mid, "waiting", reason="admission: sent to the daemon, which has not placed it yet", job_id=job_id)
    marker = {"by": "conversation.handoff", "request_id": "h-1"}
    if cancel_kind == "cancel-request-only":
        with world.daemon.store.transaction("job.cancel_requested", job_id=job_id, data=marker) as tx:
            tx.execute("UPDATE jobs SET cancel_requested_at=? WHERE job_id=?", ("2026-09-25T00:00:00Z", job_id))
    else:
        by = (None if cancel_kind == "person-cancel" else
              {**marker, "request_id": "h-2"} if cancel_kind == "other-handoff" else marker)
        assert world.service._cancel_job_without_attempt(job_id, by=by)
        if cancel_kind == "cancelled-with-attempt":
            attempt(world, job_id, state="reserved")
        world.service._settle_unstarted()
    before = world.store.message(mid)
    assert world.store.fence(cid, "handoff:h-1")
    world.service._lift_stale_fences()
    assert world.store.conversation(cid)["blocked_by"] is None
    assert world.store.message(mid) == before


def test_a_failed_fence_lift_is_retried_by_the_next_control_loop_tick(world, monkeypatch):
    """D-18: a temporary recovery-store failure leaves the durable fence for
    the next tick, which repairs it before admitting the first message again.
    """
    cid, ids = source(world, "waiting first", "queued second")
    job_id = turn_job(world, ids[0], cid, state="waiting")
    world.store.set_state(ids[0], "waiting", reason="admission: sent to the daemon, which has not placed it yet", job_id=job_id)
    turn_seq = world.store.message(ids[0])["turn_seq"]
    restore = world.store.restore_after_handoff
    calls = []

    def fail_commit(*args, **kwargs):
        raise OSError("handoff commit failed")

    def temporarily_fail_restore(*args, **kwargs):
        if world.store.conversation(cid)["blocked_by"] is None:
            return restore(*args, **kwargs)
        calls.append(args[0])
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return restore(*args, **kwargs)

    dispatched = []

    def submit(conversation, message):
        assert conversation["blocked_by"] is None
        dispatched.append(message["message_id"])
        raise AdapterError("hold the restored message for inspection", code=int(Exit.OPERATIONAL))  # refused for now (C-26.1)

    monkeypatch.setattr(world.store, "commit_handoff", fail_commit)
    monkeypatch.setattr(world.store, "restore_after_handoff", temporarily_fail_restore)
    monkeypatch.setattr(world.service, "_submit_turn", submit)
    with pytest.raises(OSError, match="handoff commit failed"):
        handoff(world, cid)
    assert world.store.conversation(cid)["blocked_by"] == "handoff:h-1"
    assert cid not in world.service._handing_off
    world.service.tick()
    assert calls == [cid, cid] and dispatched == [ids[0]]
    assert world.store.conversation(cid)["blocked_by"] is None
    assert world.store.message(ids[0])["turn_seq"] == turn_seq + 1
    assert world.store.message(ids[0])["job_id"] is None
    assert [world.store.message(mid)["state"] for mid in ids] == ["queued", "queued"]


def test_subfleets_repair_messages_are_not_carried(world):
    """IR-28: a failover continuation would resume the source's work beside the
    handoff, so it is withdrawn and not moved; an unblock note still guards the
    source's next turn, so it stays queued there."""
    cid, (person,) = source(world, "the person's follow-up")
    note, failover = str(uuid.uuid4()), str(uuid.uuid4())
    world.store.submit_message(conversation_id=cid, message_id=note, after_message_id=person, text="[Subfleet] note",
                               attachments=[], settings=ASK, origin="unblock-note")
    world.store.submit_message(conversation_id=cid, message_id=failover, after_message_id=person,
                               text="Continue", attachments=[], settings=ASK, origin="failover")
    out = handoff(world, cid)
    assert [pair["from"] for pair in out["handoff_from"]["moved"]] == [person]
    assert set(out["withdrawn"]) == {person, failover}
    assert world.store.message(note)["state"] == "queued"
    assert world.store.message(failover)["state"] == "cancelled"


def test_a_native_session_hands_off_and_a_lane_run_is_refused(world):
    """C-30.3, C-23.31: a session no conversation holds hands off from its native
    id, into its recorded cwd; a headless lane run is not a session (exit 7)."""
    prompt = {**fx.typed_prompt("Port the ledger importer to v2.", uuid="p0", at=fx.ago(3600)),
              "cwd": str(world.workspace)}                       # the session's recorded cwd
    fx.transcript(world.home, SESSION, [prompt], cwd=str(world.workspace))
    out = handoff(world, native={"provider": "claude", "session_id": SESSION})
    assert out["handoff_from"]["conversation_id"] is None and out["moved"] == []
    assert out["conversation"]["workspace"] == os.path.realpath(world.workspace)

    run = "5a1b2c3d-0000-4000-8000-00000000abcd"
    fx.transcript(world.home, run, fx.headless(), cwd=str(world.workspace))
    with pytest.raises(ConversationError) as err:
        handoff(world, request_id="h-2", native={"provider": "claude", "session_id": run})
    assert err.value.reason == "lane-run" and err.value.code == 7


def test_refusals_before_anything_is_read(world):
    """C-30.3, C-25.6, IR-21, IR-32: a source with no native session yet, a writable
    Codex target, and a target above Ask from an agent are refused; so is a Codex
    home that no lane enrolled."""
    cid, (mid,) = source(world, "queued", native=None)
    with pytest.raises(ConversationError) as err:
        handoff(world, cid)
    assert err.value.reason == "no-history"
    world.store.set_state(mid, "running")          # a first turn: no native id is recorded until it ends
    with pytest.raises(ConversationError) as err:
        handoff(world, cid)
    assert err.value.reason == "live-turn", "the live turn is the reason, not the missing history"
    world.store.set_state(mid, "complete")
    with pytest.raises(ConversationError) as err:
        handoff(world, cid, to={"provider": "codex", "settings": {**CODEX, "permission": "ask"}})
    assert err.value.reason == "codex-read-only" and err.value.code == 7
    with pytest.raises(ConversationError) as err:
        handoff(world, cid, to={"provider": "claude", "settings": {**ASK, "permission": "bypass"}})
    assert err.value.reason == "person-only" and err.value.code == 7
    with pytest.raises(ConversationError) as err:
        handoff(world, native={"provider": "codex", "session_id": str(uuid.uuid4()), "home": "/etc"})
    assert err.value.reason == "bad-native"


# --- the dispatcher's claim (C-24.7) ------------------------------------------------

def test_a_withdrawal_and_the_dispatcher_cannot_both_win(world, monkeypatch):
    """C-24.7, IR-2: the dispatcher claims a message (`waiting`, reason
    `dispatching`) before it creates the job. A cancel while the job is being
    created withdraws the message (no job is bound yet), and the job the
    dispatcher then creates finds it withdrawn and is cancelled before any
    attempt: the withdrawal wins and the job never runs. Once a job is bound, the
    job store's guard decides."""
    cid, (mid, later) = source(world, "hello", "later")
    seen = {}

    def submit(conversation, message):
        claimed = world.store.message(message["message_id"])
        seen.setdefault("claim", (claimed["state"], claimed["state_reason"]))
        if message["message_id"] == mid:
            seen["cancel"] = world.service.op_message_cancel({"message_id": mid}, None)["state"]
        seen.setdefault("jobs", {})[message["message_id"]] = turn_job(world, message["message_id"], cid)
        return world.daemon.store.get_job(seen["jobs"][message["message_id"]])

    monkeypatch.setattr(world.service, "_submit_turn", submit)
    world.service._dispatch()
    assert seen["claim"] == ("waiting", CLAIMED) and seen["cancel"] == "cancelled"
    withdrawn = world.store.message(mid)
    assert withdrawn["state"] == "cancelled" and withdrawn["job_id"] is None
    assert world.daemon.store.get_job(seen["jobs"][mid])["state"] == "cancelled"
    world.service._dispatch()
    bound = world.store.message(later)
    assert bound["state"] == "waiting" and bound["job_id"]
    assert bound["state_reason"] == "admission: sent to the daemon, which has not placed it yet"      # I3
    receipt = world.service.op_message_cancel({"message_id": later}, None)
    assert receipt["state"] == "cancelled"
    assert world.daemon.store.get_job(bound["job_id"])["state"] == "cancelled"


def test_a_message_withdrawn_first_never_gets_a_job(world, monkeypatch):
    """C-24.7, IR-28: a handoff that commits first leaves nothing to claim."""
    cid, (mid,) = source(world, "hello")
    handoff(world, cid)
    submitted = []
    monkeypatch.setattr(world.service, "_submit_turn",
                        lambda conversation, message: submitted.append(message["message_id"]) or None)
    world.service._dispatch()
    assert mid not in submitted and submitted, "the handoff's own brief is what dispatches next"
    assert world.store.message(mid)["state"] == "cancelled"


def test_the_dispatcher_waits_while_a_handoff_takes_the_conversation(world, monkeypatch):
    """IR-28: while a handoff of a conversation runs, the dispatcher leaves its
    queued messages alone, so the handoff's own commit is not raced."""
    cid, (mid,) = source(world, "hello")
    world.service._handing_off.add(cid)
    monkeypatch.setattr(world.service, "_submit_turn", lambda *a: pytest.fail("submitted mid-handoff"))
    world.service._dispatch()
    assert world.store.message(mid)["state"] == "queued"


@pytest.mark.parametrize(("reason", "job_exists"), [
    (None, False), (CLAIMED, False), (CLAIMED, True), ("readmit:lane-failed", False), ("readmit:lane-failed", True),
])
def test_the_dispatcher_never_submits_or_binds_a_fenced_message(world, monkeypatch, reason, job_exists):
    """D-18: a durable fence also guards readmission and interrupted claims,
    including jobs not yet bound to their messages after a crash.
    """
    cid, (mid,) = source(world, "hello")
    if reason:
        world.store.set_state(mid, "waiting", reason=reason)
    if job_exists:
        turn_job(world, mid, cid)
    assert world.store.fence(cid, "handoff:h-1")
    before = world.store.message(mid)
    assert cid not in world.service._handing_off, "only the persisted fence guards this dispatch"
    monkeypatch.setattr(world.service, "_submit_turn", lambda *a: pytest.fail("submitted a fenced message"))
    world.service._dispatch()
    assert world.store.message(mid) == before
    assert world.store.conversation(cid)["blocked_by"] == "handoff:h-1"


def test_a_claim_the_handoff_did_not_expect_rolls_the_handoff_back(world):
    """IR-28, C-30.3: the handoff's commit re-checks that each queued message is
    still queued; a message claimed meanwhile rolls back the whole handoff and
    leaves no files."""
    cid, (mid,) = source(world, "hello")
    plan = world.service._handoff_plan(world.store.conversation(cid))
    world.store.set_state(mid, "waiting", reason=CLAIMED, expect=("queued",))       # the dispatcher's claim
    with pytest.raises(ConversationError) as err:
        world.store.create_handoff(
            request_id="h-1", provider="codex", workspace=str(world.workspace), settings=CODEX, title=None,
            allow_main=False, handoff_from={"moved": []}, brief={"message_id": str(uuid.uuid4()), "text": "brief"},
            moves=[{"message_id": str(uuid.uuid4()), "text": "hello", "attachments": []}],
            withdrawals=[{"message_id": step["message"]["message_id"], "expect": step["expect"]} for step in plan])
    assert err.value.reason == "source-changed"
    assert world.store.by_request("h-1") is None
    assert world.store.message(mid)["state"] == "waiting"
    assert sorted(p.name for p in (world.store.dir).iterdir()) == [cid]


def test_a_deferred_submit_puts_the_claim_back_and_waits(world, monkeypatch):
    """C-24.7: a submit refused before any provider saw the message leaves it
    `queued` (withdrawable) and is not retried on every tick."""
    cid, (mid,) = source(world, "hello")
    calls = []

    def refuse(conversation, message):
        calls.append(message["message_id"])
        raise AdapterError("could not inspect the workdir", code=int(Exit.OPERATIONAL))  # refused for now (C-26.1)

    monkeypatch.setattr(world.service, "_submit_turn", refuse)
    world.service._dispatch()
    world.service._dispatch()
    assert calls == [mid]
    assert world.store.message(mid)["state"] == "queued"
    assert world.service.op_message_cancel({"message_id": mid}, None)["state"] == "cancelled"


def test_a_claim_interrupted_by_a_crash_is_bound_or_submitted_again(world, monkeypatch):
    """Design §4's repair: a claimed message whose job exists is bound; one whose
    job was never created is submitted again."""
    cid, (first,) = source(world, "hello")
    world.store.set_state(first, "waiting", reason=CLAIMED, expect=("queued",))
    job_id = turn_job(world, first, cid)
    monkeypatch.setattr(world.service, "_submit_turn", lambda *a: pytest.fail("the job exists"))
    world.service._dispatch()
    assert world.store.message(first)["job_id"] == job_id

    other, (second,) = source(world, "again", native=None)
    world.store.set_state(second, "waiting", reason=CLAIMED, expect=("queued",))
    monkeypatch.setattr(world.service, "_submit_turn",
                        lambda conversation, message: world.daemon.store.get_job(turn_job(world, second, other)))
    monkeypatch.setattr(service_module.ConversationService, "_previous_released", lambda *a: True)
    world.service._dispatch()
    assert world.store.message(second)["job_id"] == f"job-{second[:8]}"


def codex_app_rollout(world, thread: str, **meta) -> Path:
    """A thread of the Codex app, in `~/.codex` (the test's HOME), as the fake app-server writes one."""
    directory = Path(os.environ["HOME"]) / ".codex" / "sessions" / "2026" / "09" / "24"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-09-24T12-00-00-{thread}.jsonl"
    records = [{"type": "session_meta", "payload": {"id": thread, "cwd": str(world.workspace), **meta}},
               {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                     "content": [{"type": "input_text", "text": "Look at the tests"}]}},
               {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                     "content": [{"type": "output_text", "text": "they pass"}]}}]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def test_a_codex_app_thread_continues_by_labelled_handoff(world):
    """C-30.2, C-30.3: a thread in `~/.codex` (no lane owns that home) continues only
    by handoff; its rollout is found there, named or not, and its recorded cwd is
    the workspace. A Codex exec run is a lane run (exit 7)."""
    thread = str(uuid.uuid4())
    rollout = codex_app_rollout(world, thread)
    out = handoff(world, native={"provider": "codex", "session_id": thread}, to={"provider": "claude", "settings": ASK})
    record = out["handoff_from"]
    assert (record["provider"], record["native_session_id"], record["lane_id"]) == ("codex", thread, None)
    assert record["transcript"] == str(rollout) and record["conversation_id"] is None
    assert out["conversation"]["workspace"] == os.path.realpath(world.workspace)
    text = world.store.message_text(world.store.message(out["brief"]["message_id"]))
    assert "- Source provider: Codex" in text and "Look at the tests" in text and "they pass" in text
    named = handoff(world, request_id="h-2", to={"provider": "claude", "settings": ASK},
                    native={"provider": "codex", "session_id": thread, "home": "~/.codex"})
    assert named["handoff_from"]["transcript"] == str(rollout)

    run = str(uuid.uuid4())
    codex_app_rollout(world, run, source="exec")
    with pytest.raises(ConversationError) as err:
        handoff(world, request_id="h-3", native={"provider": "codex", "session_id": run},
                to={"provider": "claude", "settings": ASK})
    assert err.value.reason == "lane-run" and err.value.code == 7


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_a_damaged_attachment_fails_the_handoff_before_the_source_is_touched(world, damage):
    """C-30.3, D-18: a moved message's attachment is checked (there, and hashing
    right) while preparing, so a damaged one refuses the handoff before any job is
    cancelled; before, the handoff committed and the target's turn failed
    `attachment-missing` after the source was withdrawn (review of 6290a51)."""
    import uuid as uuid_module
    from pathlib import Path as FilePath
    from subfleet.conversations import attachments
    cid, _ = source(world)
    original = world.workspace / "example.png"
    original.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image payload")
    sha = attachments.add(world.store, str(original))["sha256"]
    stored = FilePath(world.store.attachment(sha)["path"])
    mid = str(uuid_module.uuid4())
    world.store.submit_message(conversation_id=cid, message_id=mid, after_message_id=None,
                               text="review this image", attachments=[sha], settings=ASK)
    job_id = turn_job(world, mid, cid, state="waiting")
    world.store.set_state(mid, "waiting", reason="admission: sent to the daemon, which has not placed it yet", job_id=job_id)
    if damage == "missing":
        stored.unlink()
    else:
        stored.write_bytes(b"changed bytes")
    conversations_before = len(world.store.query("SELECT conversation_id FROM conversations"))
    with pytest.raises(ConversationError) as refused:
        handoff(world, cid)
    assert refused.value.reason == "attachment-missing"
    assert world.daemon.store.get_job(job_id)["state"] == "waiting"
    assert world.store.message(mid)["state"] == "waiting"
    assert world.store.conversation(cid)["blocked_by"] is None
    assert len(world.store.query("SELECT conversation_id FROM conversations")) == conversations_before
