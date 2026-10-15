"""D-WALL1 / D-KILL1: elapsed execution, attribution and caller visibility."""
from datetime import datetime, timedelta, timezone
import json
import os
import socket
import struct
import threading

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import cli, daemon as daemon_module, protocol
from subfleet.conversations.peers import AuditToken
from subfleet.daemon import LIVE_TICK
from subfleet.offline import Offline
from tests.fake.test_state_contract import state_daemon  # noqa: F401

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def stamp(seconds):
    return (NOW + timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')


@pytest.fixture
def wall(state_daemon, monkeypatch):
    service, harness = state_daemon

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(daemon_module, 'datetime', FrozenDatetime)
    monkeypatch.setattr(service, '_read_answer', lambda *args: None)
    monkeypatch.setattr(service, '_inspect_running', lambda *args: None)
    monkeypatch.setattr(service.conversations, 'stop', lambda *args: False)
    return service, harness


def history(wall, durations=(4, 3), waits=(1000,), state='running', cap=7):
    service, harness = wall
    jid = service.dispatch('submit', harness.submit_args(caller_session='caller'))['job_id']
    start = -sum(durations) - sum(waits)
    service.store.update_job(jid, state='running' if state else 'waiting',
                             started_at=stamp(start), max_wall_s=cap)
    for seq, duration in enumerate(durations, 1):
        live = seq == len(durations) and state
        end = start + duration
        service.store.add_attempt(attempt_id=f'{jid}/a{seq}', job_id=jid, seq=seq,
                                  lane_id='codex-1', model_requested='astra',
                                  state=state if live else 'failed', started_at=stamp(start),
                                  finished_at=stamp(start + 1) if live and state == 'finalizing' else
                                              None if live else stamp(end))
        start = end + (waits[seq-1] if seq <= len(waits) else 0)
    aid = f'{jid}/a{len(durations)}'
    (service.root / 'jobs' / jid / f'a{len(durations)}').mkdir(exist_ok=True)
    service._inspect_next[aid] = float('inf')
    return jid, aid


def cancel_events(service, jid):
    return [json.loads(row['data_json']) for row in service.store.query(
        "SELECT data_json FROM events WHERE job_id=? AND kind='job.cancel_requested' ORDER BY event_id", (jid,))]


@pytest.mark.parametrize('state', ['starting', 'running', 'finalizing'])
def test_wall_stops_at_sum_of_attempt_running_times(wall, monkeypatch, state):
    service, _ = wall
    jid, aid = history(wall, state=state)
    stopped = []
    monkeypatch.setattr(service, '_kill_attempt', lambda a: stopped.append(a['attempt_id']))
    monkeypatch.setattr(service, '_finalize', lambda a: None)
    assert service._has_work(next(row for row in service.store.query(LIVE_TICK) if row['attempt_id'] == aid))
    service._process_attempt(aid)
    assert service.store.get_job(jid)['cancel_requested_at']
    assert service.store.get_attempt(aid)['killed_by'] == 'max_wall_s'
    assert cancel_events(service, jid) == [{'by': 'daemon', 'reason': 'max_wall_s', 'running_s': 7.0}]
    service._process_attempt(aid)
    assert len(cancel_events(service, jid)) == 1
    if state != 'finalizing':
        assert stopped


def test_long_gap_does_not_expire_a_short_running_retry(wall, monkeypatch):
    service, _ = wall
    jid, aid = history(wall, cap=8)
    monkeypatch.setattr(service, '_kill_attempt', lambda a: pytest.fail('wall counted the wait'))
    assert not service._has_work(next(row for row in service.store.query(LIVE_TICK) if row['attempt_id'] == aid))
    service._process_attempt(aid)
    assert service.store.get_job(jid)['cancel_requested_at'] is None


@settings(max_examples=40, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(durations=st.lists(st.integers(1, 20), min_size=1, max_size=5),
       waits=st.lists(st.integers(0, 10000), min_size=5, max_size=5), cap=st.integers(1, 100),
       state=st.sampled_from([None, 'starting', 'running', 'finalizing', 'reserved']))
def test_wall_property_counts_only_execution_and_never_stops_waiting(wall, monkeypatch, durations, waits, cap, state):
    service, _ = wall
    jid, aid = history(wall, durations=durations, waits=waits[:len(durations)-1], state=state, cap=cap)
    monkeypatch.setattr(service, '_kill_attempt', lambda a: None)
    monkeypatch.setattr(service, '_finalize', lambda a: None)
    if state in ('starting', 'running', 'finalizing'):
        service._process_attempt(aid)
    else:
        # Admission must never cancel a waiting job, even with a spent budget.
        service.store.update_attempt(aid, state='failed', finished_at=stamp(0))
        service.store.update_job(jid, state='waiting', next_check_at=None)
        service.store.update_lane('codex-1', enabled=False)
        service._admit()
    assert bool(service.store.get_job(jid)['cancel_requested_at']) == (
        state in ('starting', 'running', 'finalizing') and sum(durations) >= cap)


def test_client_cancel_records_socket_pid_and_session_and_notifies_caller(wall, monkeypatch, capsys):
    service, harness = wall
    jid, _ = history(wall, cap=100)
    # Exercise the real request pool boundary: pid comes from accept's audit token,
    # never from the untrusted request arguments.
    client, server = socket.socketpair()
    peer = AuditToken(struct.pack('=8I', 0, 0, 0, 0, 0, os.getpid(), 0, 1), server)
    try:
        request = protocol.Request(v=1, id='cancel', op='kill', args={
            'job_id': jid, 'session_id': 'other-session', 'reason': 'stop duplicate', 'pid': -123})
        future = service._start_request(server, threading.Lock(), request, 0, peer)
        future.result(timeout=10)
        assert json.loads(client.recv(65536))['ok']
    finally:
        client.close()
        server.close()
    expected = {'by': 'client', 'session_id': 'other-session', 'pid': os.getpid(), 'reason': 'stop duplicate'}
    assert cancel_events(service, jid) == [expected]
    notices = service.dispatch('notice.pending', {'session_id': 'caller'})['notices']
    assert any('other-session' in row['text'] and str(os.getpid()) in row['text'] for row in notices)
    shown = service.dispatch('show', {'job_id': jid})
    assert shown['cancel']['session_id'] == 'other-session'
    assert shown['cancel']['pid'] == os.getpid()
    assert Offline(harness.root).show_job(jid)['cancel']['reason'] == 'stop duplicate'
    monkeypatch.setattr(cli.Client, 'call', lambda self, op, args: shown)
    monkeypatch.setattr(cli, 'session_id', lambda: None)
    for suffix in ([], ['--json']):
        cli.main(['runs', 'show', jid, *suffix])
        output = capsys.readouterr().out
        metadata = json.loads(output.split('\n--- out.md ---')[0])
        assert metadata['cancel']['session_id'] == 'other-session'
        assert metadata['cancel']['pid'] == os.getpid()


def test_same_session_cancel_is_idempotent_and_does_not_alert_itself(wall):
    service, _ = wall
    jid, _ = history(wall, cap=100)
    for _ in range(2):
        service.dispatch('kill', {'job_id': jid, 'session_id': 'caller'})
    assert cancel_events(service, jid) == [{'by': 'client', 'session_id': 'caller', 'pid': None,
                                          'reason': 'client-request'}]
    assert service.dispatch('notice.pending', {'session_id': 'caller'})['notices'] == []


def test_daemon_message_cancel_has_an_explicit_cause(wall):
    service, harness = wall
    jid = service.dispatch('submit', harness.submit_args())['job_id']
    assert service.conversations._cancel_job_without_attempt(jid)
    assert cancel_events(service, jid) == [{'by': 'daemon', 'reason': 'message-cancelled', 'running_s': 0.0}]


def test_client_cancel_family_keeps_first_request_and_names_requester(wall):
    service, harness = wall
    parent = service.dispatch('submit', harness.submit_args(caller_session='caller'))['job_id']
    child = service.dispatch('submit', harness.submit_args(caller_session='caller', parent_job_id=parent))['job_id']
    independent = service.dispatch('submit', harness.submit_args(parent_job_id=parent, independent=True))['job_id']
    first = {'job_id': parent, 'session_id': 'other-session', 'reason': 'duplicate family'}
    service.dispatch('kill', first)
    service.dispatch('kill', {**first, 'session_id': 'later-session', 'reason': 'later'})
    expected = {'by': 'client', 'session_id': 'other-session', 'pid': None, 'reason': 'duplicate family'}
    assert cancel_events(service, parent) == [expected]
    assert cancel_events(service, child) == [{**expected, 'parent_job_id': parent}]
    assert cancel_events(service, independent) == []
    for jid in (parent, child):
        notices = service.store.list_notices('caller')
        assert any(row['job_id'] == jid and 'other-session' in row['text'] and 'duplicate family' in row['text']
                   for row in notices)


def test_starting_clock_begins_before_guardian_receipt(wall, monkeypatch):
    service, _ = wall
    jid, aid = history(wall, durations=(7,), waits=(), state='starting')
    service.store.update_attempt(aid, reserved_at=stamp(-1000))
    monkeypatch.setattr(service, '_kill_attempt', lambda a: None)
    service._process_attempt(aid)
    assert cancel_events(service, jid) == [{'by': 'daemon', 'reason': 'max_wall_s', 'running_s': 7.0}]


def test_finalization_time_is_charged_to_later_attempts(wall):
    service, harness = wall
    from tests.fake.test_state_contract import reserve, receipt_fixture
    jid, first, adir = reserve(service, harness)
    service.store.update_attempt(first['attempt_id'], started_at=stamp(-4))
    finalizing = receipt_fixture(service, first, adir)
    service.store.update_attempt(first['attempt_id'], finished_at=stamp(-2))
    service._finalize(finalizing)
    # The provider receipt predates finalization. The terminal attempt must end
    # when finalization completes, so that a retry cannot shed that running time.
    assert datetime.fromisoformat(service.store.get_attempt(first['attempt_id'])['finished_at']) == NOW


def test_reserved_time_and_unstarted_attempts_do_not_spend_the_wall(wall, monkeypatch):
    service, _ = wall
    jid, aid = history(wall, durations=(4, 3), cap=4)
    first = f'{jid}/a1'
    service.store.update_attempt(first, started_at=None, reserved_at=stamp(-10000))
    monkeypatch.setattr(service, '_kill_attempt', lambda a: pytest.fail('unstarted attempt spent the wall'))
    service._process_attempt(aid)
    assert service.store.get_job(jid)['cancel_requested_at'] is None


def test_legacy_starting_attempt_without_a_start_does_not_charge_reservation(wall, monkeypatch):
    service, _ = wall
    jid, aid = history(wall, durations=(3,), waits=(), state='starting', cap=1)
    service.store.update_attempt(aid, started_at=None, reserved_at=stamp(-10000))
    service._starting_deadlines[aid] = float('inf')
    monkeypatch.setattr(service, '_kill_attempt', lambda a: pytest.fail('reservation spent the wall'))
    service._process_attempt(aid)
    assert cancel_events(service, jid) == []


def test_finalization_rechecks_wall_at_terminal_commit(wall, monkeypatch):
    service, harness = wall
    from tests.fake.test_state_contract import reserve, receipt_fixture
    jid, first, adir = reserve(service, harness)
    service.store.update_job(jid, max_wall_s=10)
    service.store.update_attempt(first['attempt_id'], started_at=stamp(-4))
    receipt_fixture(service, first, adir)
    salvage = service._salvage

    def slow_finalization(*args, **kwargs):
        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW + timedelta(seconds=6)
        monkeypatch.setattr(daemon_module, 'datetime', Later)
        return salvage(*args, **kwargs)

    monkeypatch.setattr(service, '_salvage', slow_finalization)
    service._process_attempt(first['attempt_id'])
    assert service.store.get_job(jid)['state'] == 'cancelled'
    assert service.store.get_attempt(first['attempt_id'])['killed_by'] == 'max_wall_s'
    assert cancel_events(service, jid) == [{'by': 'daemon', 'reason': 'max_wall_s', 'running_s': 10.0}]


@pytest.mark.parametrize('cap,expired', [(0.0112, True), (0.0113, False)])
def test_tick_rounding_cannot_hide_a_spent_running_budget(wall, monkeypatch, cap, expired):
    service, _ = wall
    jid, aid = history(wall, durations=(0.0014,) * 8, waits=(0.9986,) * 7, cap=cap)
    monkeypatch.setattr(service, '_kill_attempt', lambda a: None)
    row = next(row for row in service.store.query(LIVE_TICK) if row['attempt_id'] == aid)
    if expired:
        assert service._has_work(row)
    service._process_attempt(aid)
    assert bool(service.store.get_job(jid)['cancel_requested_at']) == expired


def test_kill_reports_already_finished_when_settlement_wins_the_transaction(wall, monkeypatch):
    service, harness = wall
    jid = service.dispatch('submit', harness.submit_args())['job_id']
    request_cancel = service._request_cancel

    def finish_first(job_id, data, **kwargs):
        with service.store.transaction('fixture.finished', job_id=jid) as tx:
            tx.execute("UPDATE jobs SET state='failed',rc=1,finished_at=? WHERE job_id=?", (stamp(0), jid))
            service._notice(tx, service._job(jid), 'finished before cancellation committed')
        return request_cancel(job_id, data, **kwargs)

    monkeypatch.setattr(service, '_request_cancel', finish_first)
    assert service.dispatch('kill', {'job_id': jid})['status'] == 'already finished'
    assert cancel_events(service, jid) == []


def test_launch_failure_starts_execution_at_finalization_not_reservation(wall, monkeypatch):
    service, _ = wall
    jid, aid = history(wall, durations=(1000,), waits=(), state='reserved')
    service.store.update_attempt(aid, started_at=None, reserved_at=stamp(-1000))
    service._begin_finalizing(service.store.get_attempt(aid), {'rc': 127, 'spawn_error': 'unavailable'})
    assert datetime.fromisoformat(service.store.get_attempt(aid)['started_at']) == NOW

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(seconds=7)

    monkeypatch.setattr(daemon_module, 'datetime', Later)
    monkeypatch.setattr(service, '_finalize', lambda a: None)
    service._process_attempt(aid)
    assert cancel_events(service, jid) == [{'by': 'daemon', 'reason': 'max_wall_s', 'running_s': 7.0}]
