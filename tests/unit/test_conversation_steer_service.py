"""C-24.9: durable claims, operator races and settlement through the real stores."""
import threading

import pytest

from subfleet import protocol
from subfleet.conversations.peers import Verdict
from subfleet.conversations.store import ConversationError
from tests.unit.test_conversation_service import svc, conversation, submit  # noqa: F401


class Runner:
    attempt_id = "host/a1"
    offset = 0
    next_seq = 1
    steerable = True

    def __init__(self, mid, cid):
        self.message_id, self.conversation_id = mid, cid
        self.finished = threading.Event()
        self.sent, self.commands = set(), []

    def steer(self, mid):
        self.commands.append(("steer", mid))

    def steer_written(self, mid):
        return mid in self.sent

    def interrupt(self, why):
        self.commands.append(("interrupt", why))

    def stop(self):
        pass

    def join(self, timeout):
        return True


@pytest.fixture
def live(svc):
    svc._person = lambda peer, what: Verdict(True, "test", peer)
    cid = conversation(svc)
    host = submit(svc, cid)
    svc.store.set_state(host, "running")
    runner = Runner(host, cid)
    svc.runners[runner.attempt_id] = runner
    mid = submit(svc, cid, "steer", after=host)
    return cid, host, mid, runner


def steer(svc, mid):
    return svc.handle("message.steer", {"message_id": mid}, None)


def test_claim_is_durable_before_command_and_idempotent(svc, live):
    cid, host, mid, runner = live
    def command(mid):
        assert svc.store.message(mid)["state"] == "steering"
        runner.commands.append(mid)
    runner.steer = command
    first = steer(svc, mid)
    assert first["state"] == "steering" and first["steered_into"] == host
    assert steer(svc, mid) == first and runner.commands == [mid]
    assert svc.store.message(mid)["job_id"] is None
    assert svc.store.next_dispatchable(cid) == []
    change = svc.store.changes_after(0)["changes"][-1]
    assert change["state"] == "steering" and change["steered_into"] == host
    assert svc.op_message_status({"message_ids": [mid]}, None)["messages"][0]["steered_into"] == host


def test_queue_head_and_repair_messages_cannot_be_bypassed(svc, live):
    cid, host, mid, _ = live
    later = submit(svc, cid, after=mid)
    with pytest.raises(ConversationError, match="earlier") as exc:
        steer(svc, later)
    assert exc.value.reason == "not-next"
    import uuid
    svc.store.submit_message(conversation_id=cid, message_id=str(uuid.uuid4()), after_message_id=later,
                             text="repair", attachments=[], settings=svc.store.message(mid)["settings"],
                             origin="unblock-note")
    with pytest.raises(ConversationError) as exc:
        steer(svc, mid)
    assert exc.value.reason == "not-next"
    assert svc.store.message(mid)["state"] == "queued"


@pytest.mark.parametrize("change,reason", [("no-runner", "no-live-turn"), ("ended", "not-steerable"),
                                            ("not-queued", "not-queued"), ("narrow", "settings-narrower")])
def test_claim_refusals_leave_message_unchanged(svc, live, change, reason):
    _, _, mid, runner = live
    if change == "no-runner":
        svc.runners.clear()
    elif change == "ended":
        runner.steerable = False
    elif change == "not-queued":
        svc.store.set_state(mid, "cancelled")
    else:
        with svc.store.transaction() as tx:
            tx.execute("UPDATE messages SET settings_json=json_set(settings_json,'$.permission','read-only') "
                       "WHERE message_id=?", (mid,))
    before = svc.store.message(mid)
    with pytest.raises(ConversationError) as exc:
        steer(svc, mid)
    assert exc.value.reason == reason and svc.store.message(mid) == before


def test_steer_is_person_only(svc, live):
    _, _, mid, _ = live
    def refuse(peer, what):
        raise ConversationError("person-only", what, code=7)
    svc._person = refuse
    with pytest.raises(ConversationError) as exc:
        steer(svc, mid)
    assert exc.value.reason == "person-only" and svc.store.message(mid)["state"] == "queued"


def test_steer_requires_canonical_lowercase_uuid(svc, live):
    _, _, mid, _ = live
    with pytest.raises(ConversationError) as exc:
        steer(svc, mid.upper())
    assert exc.value.reason == "bad-message-id"


def test_cancel_before_handover_is_terminal_and_after_is_too_late(svc, live):
    cid, host, mid, runner = live
    steer(svc, mid)
    assert svc.op_message_cancel({"message_id": mid}, None)["state"] == "cancelled"
    after = submit(svc, cid, after=mid)
    steer(svc, after)
    runner.sent.add(after)
    with pytest.raises(ConversationError) as exc:
        svc.op_message_cancel({"message_id": after}, None)
    assert exc.value.reason == "too-late"
    assert svc.store.message(after)["stop_requested_at"] is None


def test_interrupt_of_steer_stops_its_host(svc, live):
    _, host, mid, runner = live
    steer(svc, mid)
    answer = svc.op_turn_interrupt({"message_id": mid}, None)
    assert answer["message_id"] == mid
    assert svc.store.message(host)["stop_requested_at"]
    assert ("interrupt", "stopped") in runner.commands


@pytest.mark.parametrize("frame,fate,state", [
    ("written", "consumed", "steered"), ("written", "delivered", "steered"),
    ("written", "unanswered", "steered"), ("unsent", "unknown", "queued"),
    ("refused", "refused", "queued"), ("written", "cancelled", "queued"),
    ("written", "unknown", "delivery-unknown"),
])
def test_settlement_uses_evidence_and_preserves_sequence(svc, live, frame, fate, state):
    cid, host, mid, runner = live
    seq = steer(svc, mid)["seq"]
    turn = {"steers": {mid: {"frame": frame, "fate": fate, "detail": "test"}}}
    svc._settle_steers(runner, turn, {"model": "served-model"})
    settled = svc.store.message(mid)
    assert settled["state"] == state and settled["seq"] == seq
    if state == "steered":
        assert settled["served"] == {"model": "served-model", "steered_into": host}
        assert ("unanswered" in settled["state_reason"]) == (fate == "unanswered")
        assert steer(svc, mid)["state"] == "steered"
    elif state == "delivery-unknown":
        assert svc.store.conversation(cid)["blocked_by"] == "delivery-unknown"
    svc._settle_steers(runner, turn, {"model": "different"})
    assert svc.store.message(mid) == settled


def test_handoff_refuses_steering_but_skips_steered_history(svc, live):
    cid, host, mid, runner = live
    steer(svc, mid)
    svc.store.set_state(host, "complete")
    with pytest.raises(ConversationError) as exc:
        svc._handoff_plan(svc.store.conversation(cid))
    assert exc.value.reason == "live-turn"
    svc._settle_steers(runner, {"steers": {mid: {"frame": "written", "fate": "consumed"}}}, {})
    assert svc._handoff_plan(svc.store.conversation(cid)) == []


def test_capability_and_op_order(svc):
    caps = svc.op_capabilities({}, None)
    assert "steer.v1" in caps["capabilities"] and caps["steer_providers"] == ["claude", "codex"]
    assert protocol.CONVERSATION_OPS[protocol.CONVERSATION_OPS.index("message.cancel") + 1] == "message.steer"


def test_pending_steer_pins_and_replays_its_host_even_after_partial_settlement(svc, live, monkeypatch):
    from tests.unit.test_conversation_service import turn_attempt
    _, host, mid, _ = live
    steer(svc, mid)
    aid = turn_attempt(svc, host, state="succeeded", n=0)
    svc.store.set_state(host, "complete", job_id="turn-job-0")
    svc.runners.clear()
    adir = svc.root / "jobs" / "turn-job-0" / "a1"
    adir.mkdir(parents=True)
    (adir / "stdin.jsonl").write_text("")
    assert svc.retention_pins() == {"turn-job-0"}
    adopted = []
    monkeypatch.setattr(svc, "_adopt", lambda attempt, ended: adopted.append((attempt["attempt_id"], ended)) or True)
    svc._replay_unsettled()
    svc._replay_unsettled()
    assert adopted == [(aid, True)]


def test_steer_on_unstarted_host_returns_to_its_queue_position(svc, live):
    from tests.unit.test_conversation_service import turn_attempt
    cid, host, mid, _ = live
    original = steer(svc, mid)["seq"]
    later = submit(svc, cid, after=mid)
    turn_attempt(svc, host, state="failed", n=0)
    svc.store.set_state(host, "starting", job_id="turn-job-0")
    with svc.daemon.store.transaction() as tx:
        tx.execute("UPDATE jobs SET state='failed' WHERE job_id='turn-job-0'")
    svc.runners.clear()
    svc._settle_unstarted()
    assert svc.store.message(host)["state"] == "failed"
    assert svc.store.message(mid)["state"] == "queued"
    assert svc.store.message(mid)["seq"] == original
    assert svc.store.next_dispatchable(cid)[0]["message_id"] == mid
    assert svc.store.message(later)["state"] == "queued"


def test_resolving_one_unknown_steer_keeps_another_blocked(svc, live):
    cid, host, mid, runner = live
    steer(svc, mid)
    later = submit(svc, cid, after=mid)
    steer(svc, later)
    svc._settle_steers(runner, {"steers": {m: {"frame": "written", "fate": "unknown"}
                                         for m in (mid, later)}}, {})
    svc.op_message_resolve({"message_id": mid, "resolution": "not-delivered", "confirm": True}, None)
    assert svc.store.conversation(cid)["blocked_by"] == "delivery-unknown"
    svc.op_message_resolve({"message_id": later, "resolution": "not-delivered", "confirm": True}, None)
    assert svc.store.conversation(cid)["blocked_by"] is None


@pytest.mark.parametrize("host_block,first_resolution", [("unfinished-turn", "not-delivered"),
                                                         (None, "delivered")])
def test_resolving_last_unknown_preserves_unfinished_host(svc, live, host_block, first_resolution):
    cid, host, mid, runner = live
    steer(svc, mid)
    later = submit(svc, cid, after=mid)
    steer(svc, later)
    svc._settle_steers(runner, {"steers": {m: {"frame": "written", "fate": "unknown"}
                                         for m in (mid, later)}}, {}, host_block=host_block)
    svc.op_message_resolve({"message_id": mid, "resolution": first_resolution, "confirm": True}, None)
    assert svc.store.conversation(cid)["blocked_by"] == "delivery-unknown"
    svc.op_message_resolve({"message_id": later, "resolution": "not-delivered", "confirm": True}, None)
    assert svc.store.conversation(cid)["blocked_by"] == "unfinished-turn"


def test_missing_outcome_cannot_requeue_a_written_steer(svc, live):
    _, _, mid, runner = live
    steer(svc, mid)
    runner.sent.add(mid)
    svc._settle_steers(runner, {}, {})
    assert svc.store.message(mid)["state"] == "delivery-unknown"


@pytest.mark.parametrize("fate,expected", [("consumed", "steered"), ("unknown", "delivery-unknown")])
def test_every_steer_settles_before_its_host(svc, live, monkeypatch, fate, expected):
    import json
    from tests.unit.test_conversation_service import EndedRunner
    cid, host, mid, _ = live
    steer(svc, mid)
    adir = svc.root / "outcome"
    adir.mkdir()
    (adir / "turn.json").write_text(json.dumps({"state": "complete", "accepted": True,
        "ended_by": "provider", "steers": {mid: {"frame": "written", "fate": fate}}}))
    set_state = svc.store.set_state
    def observe(message_id, state, **kwargs):
        if message_id == host:
            assert svc.store.message(mid)["state"] == expected
            if expected == "delivery-unknown":
                assert svc.store.conversation(cid)["blocked_by"] == "delivery-unknown"
        return set_state(message_id, state, **kwargs)
    monkeypatch.setattr(svc.store, "set_state", observe)
    svc._on_outcome(EndedRunner(adir, host, cid))
    assert svc.store.message(host)["state"] == "complete"
