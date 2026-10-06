"""Restart ordering and acceptance races around durable final-text intents."""
import time
import uuid

import pytest

from subfleet.conversations import wakes
from tests.unit.test_conversation_service import svc  # noqa: F401
from tests.unit.test_review_r2_repro import bound, iso, wake_rows, wake_texts
from tests.unit.test_review_r6_probes import finished_turn
from tests.unit.test_review_r5_repro import SimulatedCrash


def record_final(svc, runner, text, now):
    svc.store.set_state(runner.message_id, "complete", expect=("running",),
                        final_wake=(text, now))


def test_tick_replays_all_recorded_finals_before_first_wake_evaluation(svc, monkeypatch):
    cid = bound(svc)
    now = time.time()
    svc.wakes.now = lambda: now
    text = f'WAKE-ME: at={iso(now + 600)} note="Replacement"'
    runner = finished_turn(svc, cid, text)
    record_final(svc, runner, text, now)
    monkeypatch.setattr(svc, "_catalog_tick", lambda: None)
    evaluate = svc.wakes.control_tick
    evaluated = []

    def first_evaluation():
        assert svc.store.query("SELECT * FROM final_wake_intents") == []
        assert len(svc.store.query("SELECT * FROM wake_requests WHERE state='pending'")) == 1
        evaluated.append(True)
        evaluate()

    monkeypatch.setattr(svc.wakes, "control_tick", first_evaluation)
    svc.tick()
    # tick logs exceptions and continues, so verify the evaluation reached its end.
    assert evaluated == [True]


def test_recorded_finals_replay_in_message_order_after_partial_registration(svc, monkeypatch):
    cid = bound(svc)
    now = time.time()
    clock = [now]
    svc.wakes.now = lambda: clock[0]
    older_text = (f'WAKE-ME: at={iso(now + 300)} note="Partial older intent"\n'
                  f'WAKE-ME: at={iso(now + 600)} note="Older intent"')
    newer_text = f'WAKE-ME: at={iso(now + 1200)} note="Newer intent"'
    older = finished_turn(svc, cid, older_text)
    # The older completion committed first; only the newer intent is inserted
    # first, as can happen when an older runner is recovered after a later turn.
    svc.store.set_state(older.message_id, "complete")
    newer = finished_turn(svc, cid, newer_text, after=older.message_id)
    record_final(svc, newer, newer_text, now + 60)
    svc.store.set_state(older.message_id, "complete", final_wake=(older_text, now))
    # A crash left the first registration durable but neither intent acknowledged.
    svc.wakes.register(cid, f"final:{older.message_id}:0",
                       wakes.normalize(at=iso(now + 300), note="Partial older intent", now=now))
    replayed = []
    from_final = svc.wakes.from_final

    def replay(*args):
        replayed.append(args[1])
        return from_final(*args)

    monkeypatch.setattr(svc.wakes, "from_final", replay)
    monkeypatch.setattr(svc, "_catalog_tick", lambda: None)
    clock[0] = now + 601
    svc.tick()
    assert replayed == [older.message_id, newer.message_id]
    assert svc.store.query("SELECT * FROM final_wake_intents") == []
    assert wake_rows(svc, cid) == []
    clock[0] = now + 1201
    svc.tick()
    assert len(wake_rows(svc, cid)) == 1
    assert "Newer intent" in wake_texts(svc, cid)[0]


def test_claim_rechecks_intents_committed_after_candidate_read(svc, monkeypatch):
    cid = bound(svc)
    now = time.time()
    svc.wakes.now = lambda: now
    svc.wakes.register(cid, str(uuid.uuid4()),
                       wakes.normalize(at=iso(now + 600), note="Old timer", now=now))
    text = f'WAKE-ME: at={iso(now + 1200)} note="Replacement"'
    svc.wakes.now = lambda: now + 601
    submit = svc.store.submit_message
    attempted = []

    def commit_between_read_and_claim(**kwargs):
        if kwargs.get("origin") == "wake":
            attempted.append(kwargs["origin"])
            runner = finished_turn(svc, cid, text)
            record_final(svc, runner, text, now + 601)
        return submit(**kwargs)

    with monkeypatch.context() as race:
        race.setattr(svc.store, "submit_message", commit_between_read_and_claim)
        svc.wakes.tick(poll=False)
    assert attempted == ["wake"], "race must reach the acceptance transaction"
    assert wake_rows(svc, cid) == []
    assert svc.store.conversation(cid)["wake_streak"] == 0
    assert svc.store.one("SELECT state FROM wake_requests")["state"] == "pending"
    svc._replay_unsettled()
    svc.wakes.now = lambda: now + 1201
    svc.wakes.tick(poll=False)
    assert len(wake_rows(svc, cid)) == 1
    assert "Replacement" in wake_texts(svc, cid)[0]


def test_failed_replay_defers_only_the_conversation_with_an_intent(svc, monkeypatch):
    cid, other = bound(svc), bound(svc)
    now = time.time()
    svc.wakes.now = lambda: now
    for target in (cid, other):
        svc.wakes.register(target, str(uuid.uuid4()),
                           wakes.normalize(at=iso(now + 600), note="Due timer", now=now))
    text = f'WAKE-ME: at={iso(now + 1200)} note="Replacement"'
    runner = finished_turn(svc, cid, text)
    record_final(svc, runner, text, now)
    monkeypatch.setattr(svc, "_catalog_tick", lambda: None)
    svc.wakes.now = lambda: now + 601
    with monkeypatch.context() as failure:
        def fail(*_args):
            raise RuntimeError("registration temporarily unavailable")

        failure.setattr(svc.wakes, "from_final", fail)
        svc.tick()
    assert wake_rows(svc, cid) == []
    assert svc.store.conversation(cid)["wake_streak"] == 0
    assert len(wake_rows(svc, other)) == 1
    assert svc.store.one("SELECT 1 FROM final_wake_intents WHERE message_id=?", (runner.message_id,))
    svc.wakes.now = lambda: now + 1201
    svc.tick()
    assert len(wake_rows(svc, cid)) == 1
    assert "Replacement" in wake_texts(svc, cid)[0]


def test_direct_evaluation_recovers_partial_final_batch_after_replay_crash(svc, monkeypatch):
    cid = bound(svc)
    now = time.time()
    clock = [now]
    svc.wakes.now = lambda: clock[0]
    text = (f'WAKE-ME: at={iso(now + 600)} note="Partial timer"\n'
            f'WAKE-ME: at={iso(now + 1200)} note="Replacement"')
    runner = finished_turn(svc, cid, text)
    register = svc.wakes.register

    with monkeypatch.context() as crash:
        def interrupt(*args, **kwargs):
            register(*args, **kwargs)
            raise SimulatedCrash()

        crash.setattr(svc.wakes, "register", interrupt)
        with pytest.raises(SimulatedCrash):
            svc._on_outcome(runner)
    clock[0] = now + 601
    svc.wakes.tick(poll=False)
    assert svc.store.query("SELECT * FROM final_wake_intents") == []
    assert wake_rows(svc, cid) == []
    clock[0] = now + 1201
    svc.wakes.tick(poll=False)
    assert len(wake_rows(svc, cid)) == 1
    assert "Replacement" in wake_texts(svc, cid)[0]
