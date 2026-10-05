"""Round-two continuation boundaries and generated request resolution histories."""
import json
import uuid
from unittest.mock import Mock

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet.conversations import wakes
from subfleet.conversations.store import ConversationError
from tests.unit.test_conversation_service import submit, svc  # noqa: F401
from tests.unit.test_conversation_wakes import bound, rows, settle_all
from tests.unit.test_review_r2_repro import iso, run_under, turn_job, T0


@pytest.mark.parametrize('remaining', [-1, 0, 1, 299, 300])
def test_final_timer_is_five_minutes_after_turn_start_and_still_ahead(svc, remaining):
    cid = bound(svc)
    mid = submit(svc, cid)
    job = turn_job(svc, cid, 1)
    with svc.daemon.store.transaction() as tx:
        tx.execute('UPDATE jobs SET created_at=? WHERE job_id=?', (iso(T0), job))
    with svc.store.transaction() as tx:
        tx.execute('UPDATE messages SET job_id=?,state=\'complete\' WHERE message_id=?', (job, mid))
    svc.wakes.now = lambda: T0 + 600
    svc.wakes.from_final(cid, mid, f'WAKE-ME: at={iso(T0 + 600 + remaining)}')
    # The turn started 600 s before settlement, so every case clears the floor;
    # only a time already past at settlement is refused.
    assert bool(svc.store.query('SELECT * FROM wake_requests')) == (remaining > 0)
    events = svc.store.query('SELECT data_json FROM events WHERE message_id=?', (mid,))
    assert any('wake-refused' in e['data_json'] for e in events) == (remaining <= 0)


def test_first_pr_watch_keeps_events_between_turn_creation_and_settlement(svc, monkeypatch):
    cid = bound(svc)
    mid = submit(svc, cid)
    job = turn_job(svc, cid, 1)
    with svc.daemon.store.transaction() as tx:
        tx.execute('UPDATE jobs SET created_at=? WHERE job_id=?', (iso(T0), job))
    with svc.store.transaction() as tx:
        tx.execute('UPDATE messages SET job_id=?,state=\'complete\' WHERE message_id=?', (job, mid))
    svc.wakes.now = lambda: T0 + 600
    svc.wakes.from_final(cid, mid, 'WAKE-ME: prs=o/r#1')
    monkeypatch.setattr(wakes, 'query_prs', lambda _: {'o/r#1': {
        'state': 'OPEN', 'checks': [['COMPLETED', 'ci', 'SUCCESS', iso(T0 + 240), 'fix']], 'reviews': []}})
    svc.wakes.tick()
    assert len(rows(svc, cid)) == 1


def test_pr_refusal_survives_restart_without_a_second_wake_and_clears_once_resolved(svc, monkeypatch):
    cid = bound(svc)
    svc.wakes.now = lambda: T0
    request_id = str(uuid.uuid4())
    spec = wakes.normalize(prs=['o/r#7'], now=T0)
    svc.wakes.register(cid, request_id, spec)
    monkeypatch.setattr(wakes, 'query_prs', lambda _: {'o/r#7': {'error': 'missing'}})
    svc.wakes.tick()
    assert len(rows(svc, cid)) == 1
    settle_all(svc, cid)
    engine = wakes.WakeEngine(svc)
    try:
        engine.now = lambda: T0 + 120
        assert engine.register(cid, request_id, spec)['request_id'] == request_id
        engine.register(cid, str(uuid.uuid4()), spec)            # re-armed: watched, not announced again
        engine.tick()
        assert len(rows(svc, cid)) == 1
        merged = {'state': 'MERGED', 'merged_at': iso(T0 + 200), 'checks': [], 'reviews': []}
        monkeypatch.setattr(wakes, 'query_prs', lambda _: {'o/r#7': merged})
        engine.now = lambda: T0 + 300
        engine.tick()                                           # the PR now resolves: refusal cleared
        assert svc.store.query("SELECT * FROM wake_pr_refusals WHERE conversation_id=?", (cid,)) == []
    finally:
        engine.close()


def test_rearm_does_not_baseline_an_undelivered_mid_turn_pr_event(svc, monkeypatch):
    cid = bound(svc)
    mid = submit(svc, cid)
    job = turn_job(svc, cid, 1, state='running')
    with svc.daemon.store.transaction() as tx:
        tx.execute('UPDATE jobs SET created_at=? WHERE job_id=?', (iso(T0), job))
    with svc.store.transaction() as tx:
        tx.execute('UPDATE messages SET job_id=?,state=\'running\' WHERE message_id=?', (job, mid))
    svc.wakes.now = lambda: T0
    register_id = str(uuid.uuid4())
    svc.wakes.register(cid, register_id, wakes.normalize(prs=['o/r#1'], now=T0))
    snapshot = {'state': 'OPEN', 'checks': [['COMPLETED', 'ci', 'SUCCESS', iso(T0 + 240), 'fix']], 'reviews': []}
    monkeypatch.setattr(wakes, 'query_prs', lambda _: {'o/r#1': snapshot})
    svc.wakes.now = lambda: T0 + 300
    svc.wakes.tick()
    assert rows(svc, cid) == []
    svc.daemon.store.update_job(job, state='succeeded')
    svc.store.set_state(mid, 'complete')
    svc.wakes.now = lambda: T0 + 600
    svc.wakes.from_final(cid, mid, 'WAKE-ME: prs=o/r#1')
    svc.wakes.tick()
    assert len(rows(svc, cid)) == 1


@settings(max_examples=32, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(timer=st.booleans(), pr=st.booleans(), delivered=st.booleans(),
       terminal=st.booleans(), pruned=st.booleans(), due=st.booleans(), changed=st.booleans())
def test_property_request_cannot_be_satisfied_with_unresolved_kinds(
        svc, timer, pr, delivered, terminal, pruned, due, changed):
    cid = bound(svc)
    svc.wakes.now = lambda: T0
    job_id = f'run-{uuid.uuid4()}'
    parent = turn_job(svc, cid, str(uuid.uuid4()))
    run_under(svc, cid, parent, job_id, state='succeeded' if delivered or terminal else 'running')
    if delivered:
        svc.wakes.tick(poll=False)
        settle_all(svc, cid)
    request_id = str(uuid.uuid4())
    args = {'runs': [job_id], 'now': T0}
    if timer:
        args['at'] = iso(T0 + 300)
    if pr:
        args['prs'] = ['o/r#1']
    svc.wakes.register(cid, request_id, wakes.normalize(**args))
    if pruned:
        with svc.daemon.store.transaction() as tx:
            tx.execute('DELETE FROM jobs WHERE job_id=?', (job_id,))
    if pr and changed:
        with svc.store.transaction() as tx:
            tx.execute('UPDATE wake_requests SET ready_json=? WHERE conversation_id=? AND kind=\'pr\'',
                       (json.dumps(['o/r#1']), cid))
    svc.wakes.now = lambda: T0 + (300 if due else 1)
    before = len(rows(svc, cid))
    for _ in range(3):
        svc.wakes.tick(poll=False)
        settle_all(svc, cid)
    requests = svc.store.query('SELECT * FROM wake_requests WHERE conversation_id=? AND request_id=?',
                              (cid, request_id))
    after = rows(svc, cid)[before:]
    assert len(after) <= 1
    for r in requests:
        if r['state'] == 'satisfied':
            # Silent resolution is backed by a delivered result, not merely an
            # alternative on the same line. No other kind has that evidence.
            assert r['kind'] == 'runs' and delivered
        if r['state'] == 'fired':
            assert r['message_id'] in {m['message_id'] for m in after}
    expected = ((not delivered and (terminal or pruned)) or (timer and due) or (pr and changed))
    assert bool(after) == expected
    if not after:
        assert {r['kind'] for r in requests if r['state'] == 'pending'} == (
            ({'time'} if timer else set()) | ({'pr'} if pr else set()) |
            (set() if delivered else {'runs'}))


@pytest.mark.parametrize('runs_per_conversation', [0, 16])
def test_idle_control_ticks_do_no_writes_and_batch_target_reads(svc, monkeypatch, runs_per_conversation):
    svc.wakes.now = lambda: T0
    for n in range(40):
        cid = bound(svc)
        parent = turn_job(svc, cid, n)
        ids = [f'idle-{n}-{i}' for i in range(runs_per_conversation)]
        for job_id in ids:
            run_under(svc, cid, parent, job_id, state='running')
        svc.wakes.register(cid, str(uuid.uuid4()), wakes.normalize(runs=ids, at=iso(T0 + 21600), now=T0))
    svc.wakes.control_tick()
    main_query = Mock(wraps=svc.daemon.store.query)
    conversation_query = Mock(wraps=svc.store.query)
    monkeypatch.setattr(svc.daemon.store, 'query', main_query)
    monkeypatch.setattr(svc.store, 'query', conversation_query)
    for store in (svc.store, svc.daemon.store):
        monkeypatch.setattr(store, 'transaction', Mock(side_effect=AssertionError('idle tick must not write')))
    # Twenty evaluations use the production pacing: one scan per second.
    for n in range(1, 21):
        svc.wakes.now = lambda n=n: T0 + n / 20
        svc.wakes.control_tick()
    assert main_query.call_count <= 8
    # Small bounded housekeeping reads are allowed between evaluations; target
    # reads must not multiply by either ticks, conversations or run count.
    assert conversation_query.call_count <= 30
