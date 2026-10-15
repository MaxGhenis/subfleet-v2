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
       wait=st.integers(0, 10000), cap=st.integers(1, 100),
       state=st.sampled_from([None, 'starting', 'running', 'finalizing', 'reserved']))
def test_wall_property_counts_only_execution_and_never_stops_waiting(wall, monkeypatch, durations, wait, cap, state):
    service, _ = wall
    jid, aid = history(wall, durations=durations, waits=[wait] * (len(durations)-1), state=state, cap=cap)
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
            'job_id': jid, 'session_id': 'other-session', 'reason': 'stop duplicate'})
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
    assert len(cancel_events(service, jid)) == 1
    assert service.dispatch('notice.pending', {'session_id': 'caller'})['notices'] == []


def test_daemon_message_cancel_has_an_explicit_cause(wall):
    service, harness = wall
    jid = service.dispatch('submit', harness.submit_args())['job_id']
    assert service.conversations._cancel_job_without_attempt(jid)
    assert cancel_events(service, jid) == [{'by': 'daemon', 'reason': 'message-cancelled', 'running_s': 0.0}]
