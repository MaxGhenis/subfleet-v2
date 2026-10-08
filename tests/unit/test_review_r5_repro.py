"""Round-five review probes and final-text registration crash regressions."""
import contextlib
import json
import time
import uuid

import pytest

from subfleet.conversations import reconcile, wakes
from subfleet.conversations.service import ConversationService
from subfleet.conversations.store import ConversationStore
from tests.unit.test_conversation_service import EndedRunner, submit, svc  # noqa: F401
from tests.unit.test_review_r2_repro import (
    T0, bound, fake_gh, iso, run_under, settle_wakes,
    turn_job, wake_rows, wake_texts,
)
from tests.unit.test_review_r4_repro import arm, poll_and_settle


class SimulatedCrash(BaseException):
    pass


def test_automatic_completion_batch_is_ordered_by_job_creation(svc):
    """Different seconds expose the job store's reverse chronological index scan."""
    cid = bound(svc)
    parent = turn_job(svc, cid, 1)
    run_under(svc, cid, parent, "z-first")
    run_under(svc, cid, parent, "a-second")
    now = time.time()
    svc.daemon.store.update_job("z-first", created_at=iso(now + 1))
    svc.daemon.store.update_job("a-second", created_at=iso(now + 2))
    svc.wakes.now = lambda: now + 3
    svc.wakes.tick(poll=False)
    assert wake_texts(svc, cid) == [
        "[Subfleet]\nz-first finished: succeeded; deliverable /work/z-first.md\n"
        "a-second finished: succeeded; deliverable /work/a-second.md"
    ]


@pytest.mark.parametrize("boundary", ["before-complete", "after-complete", "no-crash"])
def test_final_timer_is_recovered_after_settlement_restart(svc, monkeypatch, boundary):
    cid = bound(svc)
    mid = submit(svc, cid)
    svc._dispatch()
    host = svc.store.message(mid)["job_id"]
    svc.daemon.store.add_attempt(attempt_id=host + "/a1", job_id=host, seq=1,
                                lane_id="claude-1", model_requested="opus", state="succeeded")
    svc.daemon.store.update_job(host, state="succeeded")
    adir = svc.root / "jobs" / host / "a1"
    adir.mkdir(parents=True)
    (adir / "stdin.jsonl").write_text('{}\n')
    now = time.time()
    clock = [now]
    svc.wakes.now = lambda: clock[0]
    turn = {"state": "complete", "ended_by": "provider", "served": {},
            "native_session_id": svc.store.conversation(cid)["native_session_id"],
            "final_text": f'WAKE-ME: at={iso(now + 600)} note="Resume this task"'}
    (adir / "turn.json").write_text(json.dumps(turn))
    runner = EndedRunner(adir, mid, cid)
    runner.attempt_id = host + "/a1"
    runner.attempt["attempt_id"] = runner.attempt_id
    svc.store.set_state(mid, "running")
    original_set = svc.store.set_state
    original_final = svc.wakes.from_final

    def crash_before_complete(*args, **kwargs):
        if len(args) > 1 and args[1] == "complete":
            raise SimulatedCrash()
        return original_set(*args, **kwargs)

    def crash_after_complete(*args, **kwargs):
        assert svc.store.message(mid)["state"] == "complete"
        raise SimulatedCrash()

    if boundary == "before-complete":
        monkeypatch.setattr(svc.store, "set_state", crash_before_complete)
    elif boundary == "after-complete":
        monkeypatch.setattr(svc.wakes, "from_final", crash_after_complete)
    if boundary == "no-crash":
        svc._on_outcome(runner)
    else:
        with pytest.raises(SimulatedCrash):
            svc._on_outcome(runner)
    monkeypatch.setattr(svc.store, "set_state", original_set)
    monkeypatch.setattr(svc.wakes, "from_final", original_final)
    state_at_restart = svc.store.message(mid)["state"]
    svc.close()
    restarted = ConversationService(svc.daemon)
    restarted.wakes.now = lambda: clock[0]
    replayed = []

    # The selection is real. Avoid launching a provider for a completed fixture:
    # when selected, apply the actual durable outcome through the real service.
    def replay(attempt, *, ended=False):
        assert ended
        replayed.append(attempt["attempt_id"])
        restarted._on_outcome(runner)
        return True

    monkeypatch.setattr(restarted, "_adopt", replay)
    try:
        for _ in range(3):
            restarted._replay_unsettled()
        clock[0] = now + 601
        for _ in range(3):
            restarted.wakes.tick(poll=False)
            settle_wakes(restarted, cid)
        requests = restarted.store.query("SELECT kind,state FROM wake_requests")
        print(f"R5 final restart {boundary}: message={state_at_restart}, replayed={replayed}, "
              f"requests={requests}, wakes={wake_texts(restarted, cid)}")
        assert len(wake_rows(restarted, cid)) == 1, "durable completed final text lost its timer at restart"
        assert "Resume this task" in wake_texts(restarted, cid)[0]
    finally:
        restarted.close()


@pytest.mark.parametrize("boundary", [
    "intent-write", "partial-registration", "overdue-restart", "already-fired",
])
def test_final_wake_intent_survives_each_registration_boundary(svc, monkeypatch, boundary):
    cid = bound(svc)
    mid = submit(svc, cid)
    svc._dispatch()
    host = svc.store.message(mid)["job_id"]
    svc.daemon.store.add_attempt(attempt_id=host + "/a1", job_id=host, seq=1,
                                lane_id="claude-1", model_requested="opus", state="succeeded")
    svc.daemon.store.update_job(host, state="succeeded")
    adir = svc.root / "jobs" / host / "a1"
    adir.mkdir(parents=True)
    (adir / "stdin.jsonl").write_text('{}\n')
    # Whole seconds: `iso` keeps microseconds, so a float `now` can land a hair short of
    # `now + 300` after the round trip and fail the 5-minute floor (about 1 run in 6).
    now = float(int(time.time()))
    clock = [now]
    svc.wakes.now = lambda: clock[0]
    text = f'WAKE-ME: at={iso(now + 600)} note="Resume this task"'
    if boundary == "partial-registration":
        text = f'WAKE-ME: at={iso(now + 300)} note="Earlier request"\n' + text
    turn = {"state": "complete", "ended_by": "provider", "served": {},
            "native_session_id": svc.store.conversation(cid)["native_session_id"],
            "final_text": text}
    (adir / "turn.json").write_text(json.dumps(turn))
    runner = EndedRunner(adir, mid, cid)
    runner.attempt_id = host + "/a1"
    runner.attempt["attempt_id"] = runner.attempt_id
    svc.store.set_state(mid, "running")

    with monkeypatch.context() as crash:
        if boundary == "intent-write":
            transaction = svc.store.transaction

            class CrashOnIntent:
                def __init__(self, tx):
                    self.tx = tx

                def __getattr__(self, name):
                    return getattr(self.tx, name)

                def execute(self, sql, params=()):
                    if sql.startswith("INSERT INTO final_wake_intents"):
                        # The state change has happened inside this same transaction.
                        assert self.tx.execute("SELECT state FROM messages WHERE message_id=?", (mid,)).fetchone()[0] == "complete"
                        raise SimulatedCrash()
                    return self.tx.execute(sql, params)

            @contextlib.contextmanager
            def crash_on_intent():
                with transaction() as tx:
                    yield CrashOnIntent(tx)

            crash.setattr(svc.store, "transaction", crash_on_intent)
        elif boundary == "overdue-restart":
            crash.setattr(svc.wakes, "from_final", lambda *_: (_ for _ in ()).throw(SimulatedCrash()))
        else:
            register = svc.wakes.register

            def crash_after_registration(*args, **kwargs):
                register(*args, **kwargs)
                raise SimulatedCrash()

            crash.setattr(svc.wakes, "register", crash_after_registration)
        with pytest.raises(SimulatedCrash):
            svc._on_outcome(runner)

    intents = svc.store.query("SELECT * FROM final_wake_intents")
    if boundary == "intent-write":
        assert svc.store.message(mid)["state"] == "running"
        assert intents == []
    else:
        assert svc.store.message(mid)["state"] == "complete"
        assert len(intents) == 1
        assert intents[0]["final_text"] == text
        assert intents[0]["settled_at"] == now
    clock[0] = now + 601
    if boundary == "already-fired":
        svc.wakes.tick(poll=False)
        settle_wakes(svc, cid)
        assert len(wake_rows(svc, cid)) == 1
    svc.close()
    restarted = ConversationService(svc.daemon)
    restarted.wakes.now = lambda: clock[0]

    def replay(attempt, *, ended=False):
        assert ended
        restarted._on_outcome(runner)
        return True

    monkeypatch.setattr(restarted, "_adopt", replay)
    try:
        if boundary == "intent-write":
            # No completion committed: normal live-message recovery settles it.
            clock[0] = now
        for _ in range(3):
            restarted._replay_unsettled()
        assert restarted.store.query("SELECT * FROM final_wake_intents") == []
        clock[0] = now + 601
        for _ in range(3):
            restarted.wakes.tick(poll=False)
            settle_wakes(restarted, cid)
            restarted._replay_unsettled()
        assert len(wake_rows(restarted, cid)) == 1
        assert "Resume this task" in wake_texts(restarted, cid)[0]
        requests = restarted.store.query("SELECT state FROM wake_requests")
        assert len(requests) == (2 if boundary == "partial-registration" else 1)
        assert sum(r["state"] == "fired" for r in requests) == 1
    finally:
        restarted.close()


@pytest.mark.parametrize("replacement", ["final-turn", "explicit-wake"])
def test_pending_final_cannot_supersede_a_newer_rearm(svc, monkeypatch, replacement):
    cid = bound(svc)
    now = time.time()
    clock = [now]
    svc.wakes.now = lambda: clock[0]

    def finished_turn(after, deadline, note):
        mid = submit(svc, cid, after=after)
        svc._dispatch()
        host = svc.store.message(mid)["job_id"]
        svc.daemon.store.add_attempt(attempt_id=host + "/a1", job_id=host, seq=1,
                                    lane_id="claude-1", model_requested="opus", state="succeeded")
        svc.daemon.store.update_job(host, state="succeeded")
        adir = svc.root / "jobs" / host / "a1"
        adir.mkdir(parents=True)
        turn = {"state": "complete", "ended_by": "provider", "served": {},
                "native_session_id": svc.store.conversation(cid)["native_session_id"],
                "final_text": f'WAKE-ME: at={iso(deadline)} note="{note}"'}
        (adir / "turn.json").write_text(json.dumps(turn))
        runner = EndedRunner(adir, mid, cid)
        runner.attempt_id = host + "/a1"
        runner.attempt["attempt_id"] = runner.attempt_id
        svc.store.set_state(mid, "running")
        return runner

    old = finished_turn(None, now + 600, "Older request")
    with monkeypatch.context() as crash:
        crash.setattr(svc.wakes, "from_final", lambda *_: (_ for _ in ()).throw(SimulatedCrash()))
        with pytest.raises(SimulatedCrash):
            svc._on_outcome(old)
    assert svc.store.message(old.message_id)["state"] == "complete"
    if replacement == "final-turn":
        newer = finished_turn(old.message_id, now + 900, "Newer request")
        svc._on_outcome(newer)
    else:
        svc.op_conversation_wake({"session_id": svc.store.conversation(cid)["native_session_id"],
            "request_id": str(uuid.uuid4()), "at": iso(now + 900), "note": "Newer request"}, None)
    svc.close()
    restarted = ConversationService(svc.daemon)
    restarted.wakes.now = lambda: clock[0]
    try:
        for _ in range(3):
            restarted._replay_unsettled()
        clock[0] = now + 601
        restarted.wakes.tick(poll=False)
        assert wake_rows(restarted, cid) == [], "older recovered final replaced the newer re-arm"
        clock[0] = now + 901
        for _ in range(3):
            restarted.wakes.tick(poll=False)
            settle_wakes(restarted, cid)
        assert len(wake_rows(restarted, cid)) == 1
        assert "Newer request" in wake_texts(restarted, cid)[0]
        assert "Older request" not in wake_texts(restarted, cid)[0]
    finally:
        restarted.close()


@pytest.mark.parametrize("hold", ["blocked", "throttle", "legacy", "archived"])
def test_ready_refusal_survives_hold_rearm_and_ready_to_accept_restart(svc, tmp_path, monkeypatch, hold):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    if hold == "blocked":
        svc.store.update_conversation(cid, blocked_by="unfinished-turn")
    elif hold == "legacy":
        with svc.store.transaction() as tx:
            tx.execute("UPDATE conversations SET legacy_hold='fixture' WHERE conversation_id=?", (cid,))
    elif hold == "archived":
        svc.store.update_conversation(cid, archived_at=iso(T0))
    else:
        with svc.store.transaction() as tx:
            tx.execute("UPDATE conversations SET wake_streak=?,last_wake_at=? WHERE conversation_id=?",
                       (wakes.MAX_STREAK, T0, cid))
    arm(svc, cid, prs=["o/r#1"])
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": {"pullRequest": None}}})
    clock[0] += 61
    svc.wakes.tick()
    assert wake_rows(svc, cid) == []
    for _ in range(3):
        arm(svc, cid, prs=["o/r#1"])
    svc.wakes.close()
    svc.store.close()
    svc.store = ConversationStore(svc.root)
    svc.wakes = wakes.WakeEngine(svc)
    svc.wakes.now = lambda: clock[0]
    arm(svc, cid, prs=["o/r#1"])
    svc.wakes.tick(poll=False)
    assert wake_rows(svc, cid) == []
    if hold == "blocked":
        svc.store.update_conversation(cid, blocked_by=None)
    elif hold == "legacy":
        with svc.store.transaction() as tx:
            tx.execute("UPDATE conversations SET legacy_hold=NULL WHERE conversation_id=?", (cid,))
    elif hold == "archived":
        svc.store.update_conversation(cid, archived_at=None)
    else:
        clock[0] = T0 + wakes.COOLDOWN_S
    poll_and_settle(svc, cid, clock, n=3)
    print(f"R5 {hold} restart: wakes={wake_texts(svc, cid)}")
    assert len(wake_rows(svc, cid)) == 1
    assert "PR watch refused: o/r#1" in wake_texts(svc, cid)[0]


def test_pruned_all_of_target_survives_block_and_restart(svc):
    cid = bound(svc)
    svc.store.update_conversation(cid, blocked_by="unfinished-turn")
    parent = turn_job(svc, cid, 1)
    run_under(svc, cid, parent, "quick", state="running")
    run_under(svc, cid, parent, "slow", state="running")
    svc.wakes.register(cid, str(uuid.uuid4()), wakes.normalize(runs=["quick", "slow"]))
    svc.daemon.store.update_job("quick", state="succeeded")
    with svc.daemon.store.transaction() as tx:
        tx.execute("DELETE FROM jobs WHERE job_id='quick'")
    svc.wakes.close()
    svc.store.close()
    svc.store = ConversationStore(svc.root)
    svc.wakes = wakes.WakeEngine(svc)
    svc.daemon.store.update_job("slow", state="succeeded")
    svc.wakes.tick(poll=False)
    assert wake_rows(svc, cid) == []
    svc.store.update_conversation(cid, blocked_by=None)
    for _ in range(3):
        svc.wakes.tick(poll=False)
        settle_wakes(svc, cid)
    assert len(wake_rows(svc, cid)) == 1
    assert "quick finished: pruned" in wake_texts(svc, cid)[0]
    assert "slow finished: succeeded" in wake_texts(svc, cid)[0]


@pytest.mark.parametrize("boundary", ["inside-claim", "after-commit"])
def test_ready_refusal_acceptance_is_atomic_across_restart(svc, tmp_path, monkeypatch, boundary):
    cid = bound(svc)
    clock = [T0]
    svc.wakes.now = lambda: clock[0]
    svc.store.update_conversation(cid, blocked_by="unfinished-turn")
    arm(svc, cid, prs=["o/r#1"])
    fake_gh(tmp_path, monkeypatch, {"data": {"p0": {"pullRequest": None}}})
    clock[0] += 61
    svc.wakes.tick()
    assert wake_rows(svc, cid) == []
    svc.store.update_conversation(cid, blocked_by=None)
    real_claim = wakes.claim
    real_submit = svc.store.submit_message

    def crash_inside_claim(*args, **kwargs):
        real_claim(*args, **kwargs)
        raise SimulatedCrash()

    def crash_after_commit(*args, **kwargs):
        real_submit(*args, **kwargs)
        raise SimulatedCrash()

    if boundary == "inside-claim":
        monkeypatch.setattr(wakes, "claim", crash_inside_claim)
    else:
        monkeypatch.setattr(svc.store, "submit_message", crash_after_commit)
    with pytest.raises(SimulatedCrash):
        svc.wakes.tick(poll=False)
    monkeypatch.setattr(wakes, "claim", real_claim)
    monkeypatch.setattr(svc.store, "submit_message", real_submit)
    request_before = svc.store.one("SELECT state,message_id FROM wake_requests WHERE state<>'superseded'")
    assert request_before["state"] == ("pending" if boundary == "inside-claim" else "fired")
    svc.wakes.close()
    svc.store.close()
    svc.store = ConversationStore(svc.root)
    svc.wakes = wakes.WakeEngine(svc)
    svc.wakes.now = lambda: clock[0]
    for _ in range(3):
        svc.wakes.tick(poll=False)
    rows = wake_rows(svc, cid)
    request_after = svc.store.one("SELECT state,message_id FROM wake_requests WHERE state='fired'")
    print(f"R5 acceptance {boundary}: before={request_before}, after={request_after}, wakes={len(rows)}")
    assert len(rows) == 1
    assert request_after["message_id"] == rows[0]["message_id"]
    svc._dispatch()
    assert len(svc.daemon.submits) == 1
