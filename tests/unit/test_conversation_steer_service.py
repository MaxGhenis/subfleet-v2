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

    def end_title(self, why):
        pass

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


def test_a_later_message_may_steer_but_a_repair_message_goes_first(svc, live):
    """Claude Code steers a new message while earlier ones wait for later (DESIGN.md
    section 8): the queued head need not steer first. A queued repair message does."""
    cid, host, mid, _ = live
    later = submit(svc, cid, after=mid)
    assert steer(svc, later)["state"] == "steering"
    assert svc.store.message(mid)["state"] == "queued"          # the earlier one keeps its place
    import uuid
    svc.store.submit_message(conversation_id=cid, message_id=str(uuid.uuid4()), after_message_id=later,
                             text="repair", attachments=[], settings=svc.store.message(mid)["settings"],
                             origin="unblock-note")
    with pytest.raises(ConversationError, match="repair") as exc:
        steer(svc, mid)
    assert exc.value.reason == "not-next"
    assert svc.store.message(mid)["state"] == "queued"


@pytest.mark.parametrize("change,reason", [("no-host", "no-live-turn"), ("no-runner", "not-steerable"),
                                            ("ended", "not-steerable"), ("not-queued", "not-queued"),
                                            ("narrow", "settings-narrower")])
def test_claim_refusals_leave_message_unchanged(svc, live, change, reason):
    """A live host whose runner the daemon has not taken back yet (after a restart) is
    `not-steerable`, which the app asks again about; `no-live-turn` means no turn runs."""
    _, host, mid, runner = live
    if change == "no-host":
        svc.store.set_state(host, "complete")
        svc.runners.clear()
    elif change == "no-runner":
        svc.runners.clear()
    elif change == "ended":
        runner.steerable = False
    elif change == "not-queued":
        svc.store.set_state(mid, "cancelled")
    else:
        # Submitted narrower than the host: its accepted digest covers its settings.
        import uuid
        narrow = str(uuid.uuid4())
        svc.store.submit_message(conversation_id=svc.store.message(mid)["conversation_id"], message_id=narrow,
                                 after_message_id=mid, text="read only, please", attachments=[],
                                 settings={**svc.store.message(mid)["settings"], "permission": "read-only"})
        mid = narrow
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


def test_settlement_reads_its_evidence_outside_the_service_lock_and_holds_it_over_the_steer_snapshot(
        svc, live, monkeypatch):
    """Steer review, finding 14: `reconcile.gather` (a scan of the provider's transcripts)
    runs with the service lock free, so polls, dispatch and ops go on meanwhile. The lock
    covers the host's steer snapshot through the host's settlement: no claim lands between."""
    import json
    from subfleet.conversations import service as service_module
    from tests.unit.test_conversation_service import EndedRunner
    cid, host, mid, _ = live
    steer(svc, mid)
    adir = svc.root / "outcome"
    adir.mkdir()
    (adir / "turn.json").write_text(json.dumps({"state": "interrupted", "reason": "stopped", "accepted": True,
                                                "ended_by": "eof",
                                                "steers": {mid: {"frame": "written", "fate": "consumed"}}}))

    def lock_free_elsewhere() -> bool:
        got = []

        def take():
            if svc._lock.acquire(timeout=2):
                svc._lock.release()
                got.append(True)
        other = threading.Thread(target=take)
        other.start()
        other.join(10)
        return got == [True]

    seen = {}

    def gather(*args, **kwargs):
        seen["gather"] = lock_free_elsewhere()
        return service_module.reconcile.Evidence(acknowledged=True, frame="written", process_gone=True,
                                                 native="found", session_exists=True)

    steers = svc.store.steers

    def snapshot(host_id):
        seen["snapshot"] = lock_free_elsewhere()
        return steers(host_id)

    monkeypatch.setattr(service_module.reconcile, "gather", gather)
    monkeypatch.setattr(svc.store, "steers", snapshot)
    svc._on_outcome(EndedRunner(adir, host, cid))
    assert seen == {"gather": True, "snapshot": False}
    assert svc.store.message(mid)["state"] == "steered"
    assert svc.store.message(host)["state"] == "interrupted"


def _ended_without_result(svc, tmp_path):
    """A Claude host whose turn ended without `result`, and its outcome directory."""
    import json
    cid = conversation(svc)
    host = submit(svc, cid)
    svc.store.set_state(host, "running")
    adir = tmp_path / "outcome"
    adir.mkdir()
    (adir / "turn.json").write_text(json.dumps({"state": "failed", "ended_by": "eof",
                                                "reason": "ended-without-result"}))
    return cid, host, adir


def _delivered():
    from subfleet.conversations import service as service_module
    return service_module.reconcile.Evidence(acknowledged=True, frame="written", process_gone=True,
                                             native="found", session_exists=True)


def _free_elsewhere(lock) -> bool:
    """Whether another thread could take `lock` now."""
    got = []

    def take():
        if lock.acquire(timeout=1):
            lock.release()
            got.append(True)
    other = threading.Thread(target=take)
    other.start()
    other.join(10)
    return got == [True]


def test_settlement_gathers_with_both_locks_free_then_holds_them_through_settlement(svc, tmp_path, monkeypatch):
    """Transcript scans hold neither the service lock nor a shared Stop stripe.
    The fresh Stop decision and steer snapshot are serialized through settlement."""
    from subfleet.conversations import service as service_module
    from tests.unit.test_conversation_service import EndedRunner
    cid, host, adir = _ended_without_result(svc, tmp_path)
    seen = {}

    def gather(*args, **kwargs):
        seen["stop lock free"] = _free_elsewhere(svc._stop_lock(host))
        seen["service lock free"] = _free_elsewhere(svc._lock)
        return _delivered()

    steers = svc.store.steers

    def snapshot(host_id):
        seen["snapshot stop lock free"] = _free_elsewhere(svc._stop_lock(host))
        seen["snapshot service lock free"] = _free_elsewhere(svc._lock)
        return steers(host_id)

    monkeypatch.setattr(service_module.reconcile, "gather", gather)
    monkeypatch.setattr(svc.store, "steers", snapshot)
    svc._on_outcome(EndedRunner(adir, host, cid))
    assert seen == {"stop lock free": True, "service lock free": True,
                    "snapshot stop lock free": False, "snapshot service lock free": False}
    assert svc.store.conversation(cid)["blocked_by"] == "unfinished-turn"
    assert _free_elsewhere(svc._stop_lock(host))


@pytest.mark.parametrize("delivery", ["delivered", "not-delivered", "delivery-unknown"])
def test_a_stop_recorded_during_the_evidence_read_prevents_later_unfinished_blocking(
        svc, tmp_path, monkeypatch, delivery):
    """C-24.7/C-24.8: Stop returns during a transcript scan. Settlement uses its
    persisted stop with the cached evidence, preserving only unknown-delivery blocks."""
    from subfleet.conversations import service as service_module
    from tests.unit.test_conversation_service import EndedRunner
    cid, host, adir = _ended_without_result(svc, tmp_path)
    queued = submit(svc, cid, after=host)
    answers = []
    scans = []
    prompt = []

    def stop():
        try:
            answers.append(svc.op_turn_interrupt({"message_id": host}, None))
        except ConversationError as exc:
            answers.append(exc.reason)
    asked = threading.Thread(target=stop)

    def gather(*args, **kwargs):
        scans.append(True)
        asked.start()
        asked.join(2)
        prompt.append(not asked.is_alive())
        return service_module.reconcile.Evidence(
            acknowledged=delivery == "delivered", frame="absent" if delivery == "not-delivered" else "written",
            process_gone=True, native="found" if delivery == "delivered" else "absent", session_exists=True)
    monkeypatch.setattr(service_module.reconcile, "gather", gather)
    try:
        svc._on_outcome(EndedRunner(adir, host, cid))
    finally:
        asked.join(10)
    assert not asked.is_alive()
    assert prompt == [True] and scans == [True]
    assert len(answers) == 1 and isinstance(answers[0], dict) and answers[0]["stop_requested"]
    message = svc.store.message(host)
    assert message["stop_requested_at"]
    if delivery == "delivery-unknown":
        assert message["state"] == "delivery-unknown"
        assert svc.store.conversation(cid)["blocked_by"] == "delivery-unknown"
        assert svc.store.next_dispatchable(cid) == []
    else:
        assert message["state"] == "interrupted" and message["state_reason"] == "stopped"
        assert svc.store.conversation(cid)["blocked_by"] is None
        assert [row["message_id"] for row in svc.store.next_dispatchable(cid)] == [queued]


@pytest.mark.parametrize("action", ["stop", "steer"])
def test_evidence_gathering_never_blocks_another_conversations_colliding_stop_stripe(
        svc, tmp_path, monkeypatch, action):
    """C-25.3: an unrelated Stop starts escalation, or a steer claims its named
    running turn, while another host scans transcripts on the same Stop stripe."""
    import uuid
    from subfleet.conversations import service as service_module
    from tests.unit.test_conversation_service import EndedRunner
    cid, host, adir = _ended_without_result(svc, tmp_path)
    other = conversation(svc)
    for _ in range(4096):
        colliding = str(uuid.uuid4())
        if svc._stop_lock(colliding) is svc._stop_lock(host):
            break
    else:
        pytest.fail("could not find a UUID sharing the host's Stop stripe")
    other_host = submit(svc, other) if action == "steer" else colliding
    if action == "steer":
        after = other_host
    else:
        after = None
    svc.store.submit_message(conversation_id=other, message_id=colliding, after_message_id=after,
                             text="remember the second point", attachments=[],
                             settings=svc.store.conversation(other)["settings"])
    svc.store.set_state(other_host, "running")
    runner = Runner(other_host, other)
    svc.runners[runner.attempt_id] = runner
    svc._person = lambda peer, what: Verdict(True, "test", peer)
    replies, prompt, scans = [], [], []

    def operate():
        try:
            if action == "stop":
                replies.append(svc.op_turn_interrupt({"message_id": colliding}, None))
            else:
                replies.append(svc.op_message_steer({"message_id": colliding, "into": other_host}, None))
        except Exception as exc:
            replies.append(exc)

    asked = threading.Thread(target=operate)

    def gather(*args, **kwargs):
        scans.append(True)
        asked.start()
        asked.join(2)
        prompt.append(not asked.is_alive())
        return _delivered()

    monkeypatch.setattr(service_module.reconcile, "gather", gather)
    try:
        svc._on_outcome(EndedRunner(adir, host, cid))
    finally:
        asked.join(10)
    assert not asked.is_alive()
    assert prompt == [True] and scans == [True]
    assert len(replies) == 1 and isinstance(replies[0], dict), replies
    if action == "stop":
        assert replies[0]["stop_requested"] and runner.commands == [("interrupt", "stopped")]
    else:
        assert replies[0]["state"] == "steering" and replies[0]["steered_into"] == other_host
        assert runner.commands == [("steer", colliding)]
    assert svc.store.conversation(cid)["blocked_by"] == "unfinished-turn"


def dispatch_order(svc, cid, host) -> list[str]:
    """What the dispatcher runs after the host settles, one turn at a time."""
    svc.store.set_state(host, "complete")
    order = []
    while nxt := svc.store.next_dispatchable(cid):
        order.append(nxt[0]["message_id"])
        svc.store.set_state(nxt[0]["message_id"], "complete")
    return order


@pytest.mark.parametrize("repair", [False, True])
def test_a_missed_steer_runs_next_ahead_of_messages_queued_for_later(svc, live, repair):
    """Steer review finding 5, DESIGN.md sections 8 and 9: a steer that missed its turn
    ("Unread until the current turn ends.") runs next, ahead of a message the person
    queued for later, keeping its own sequence; only a repair message goes first."""
    import uuid
    cid, host, later_first, runner = live            # queued for later (⌘Return), before the steers
    first = submit(svc, cid, "steer one", after=later_first)
    steer(svc, first)
    later_second = submit(svc, cid, "queued for later too", after=first)
    second = submit(svc, cid, "steer two", after=later_second)
    steer(svc, second)
    seqs = {m: svc.store.message(m)["seq"] for m in (first, second)}
    svc._settle_steers(runner, {"steers": {m: {"frame": "written", "fate": "cancelled",
                                               "detail": "interrupt-cancelled"} for m in (first, second)}}, {})
    assert {m: svc.store.message(m)["seq"] for m in (first, second)} == seqs
    expected = [first, second, later_first, later_second]
    if repair:
        note = str(uuid.uuid4())
        svc.store.submit_message(conversation_id=cid, message_id=note, after_message_id=second, text="note",
                                 attachments=[], settings=svc.store.message(first)["settings"], origin="unblock-note")
        expected.insert(0, note)
    assert dispatch_order(svc, cid, host) == expected


@pytest.mark.parametrize("crashed", [False, True])
def test_a_missed_steer_still_runs_next_when_its_own_turn_is_deferred(svc, live, crashed):
    """C-24.5: the queue orders by a missed steer's mark (`steer-missed:`), so a submit
    refused in a way that may pass (a deferral) keeps the mark, and the message queued
    for later still waits behind the steer, however often it is deferred. A dispatcher's
    claim that a crash left behind (`dispatching`) is released with the mark too."""
    from subfleet.adapters.base import AdapterError
    from subfleet.conversations.service import CLAIMED
    cid, host, later, runner = live                  # queued for later (⌘Return), before the steer
    missed = submit(svc, cid, "steer", after=later)
    steer(svc, missed)
    svc._settle_steers(runner, {"steers": {missed: {"frame": "written", "fate": "cancelled",
                                                    "detail": "interrupt-cancelled"}}}, {})
    svc.store.set_state(host, "complete")
    svc.runners.clear()
    if crashed:                                      # claimed, then the daemon stopped before its job existed
        assert svc.store.set_state(missed, "waiting", reason=CLAIMED, expect=("queued",), unbound=True)
    svc.daemon.refuse = AdapterError("could not inspect the workdir", code=1)
    for _ in range(2):
        svc._dispatch()
        row = svc.store.message(missed)
        assert (row["state"], row["state_reason"]) == ("queued", "steer-missed: deferred: could not inspect the workdir")
        assert svc.store.next_dispatchable(cid)[0]["message_id"] == missed
        svc.clock.now += 10
    svc.daemon.refuse = None
    svc._dispatch()
    assert svc.store.message(missed)["state"] == "waiting" and svc.store.message(missed)["job_id"]
    assert svc.store.message(later)["state"] == "queued"
    assert [s.request_id.split(":")[1] for s in svc.daemon.submits] == [missed] * 3


def test_a_steer_for_a_turn_that_has_ended_is_refused_instead_of_joining_the_next(svc, live):
    """Steer review finding 17: a steer sent late (a retry, a resend after the app
    restarted) names the host it was meant for; once another turn is the live one it
    is refused `no-live-turn` and the message stays queued."""
    import uuid
    cid, host, mid, _ = live
    svc.store.set_state(host, "complete")
    svc.runners.clear()
    next_host = submit(svc, cid, "queued for later", after=mid)
    svc.store.set_state(next_host, "running")
    svc.runners["next/a1"] = Runner(next_host, cid)
    before = svc.store.message(mid)
    for into in (host, str(uuid.uuid4())):
        with pytest.raises(ConversationError) as exc:
            svc.handle("message.steer", {"message_id": mid, "into": into}, None)
        assert exc.value.reason == "no-live-turn" and svc.store.message(mid) == before
    with pytest.raises(ConversationError) as exc:
        svc.handle("message.steer", {"message_id": mid, "into": host.upper()}, None)
    assert exc.value.reason == "bad-message-id"
    assert svc.handle("message.steer", {"message_id": mid, "into": next_host}, None)["steered_into"] == next_host


@pytest.mark.parametrize("text", ["/compact", "  /model opus", "!ls", "\n\t!git status", "　/review"])
def test_slash_commands_and_shell_input_are_never_steered(svc, live, text):
    """DESIGN.md section 9: `/` commands and `!` shell input wait for the turn to end.
    The daemon refuses them too, whatever client asks."""
    cid, host, mid, runner = live
    command = submit(svc, cid, text, after=mid)
    before = svc.store.message(command)
    with pytest.raises(ConversationError) as exc:
        steer(svc, command)
    assert exc.value.reason == "not-steerable" and svc.store.message(command) == before
    assert runner.commands == []
    assert steer(svc, mid)["state"] == "steering"     # an ordinary message steers
