"""Daemon timer acceptance with local provider fakes; no real HTTP (C-20.1)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import threading
import time
import tempfile

import pytest

from subfleet.adapters.codex import CodexAdapter, WHAM_USAGE_URL, WHAM_RESET_CREDITS_URL, WHAM_RESET_CREDITS_CONSUME_URL
from subfleet.contracts import Credential, Lane, LaneOwner, Outcome, OutcomeClass, Reading, ReadingLabel
from subfleet.daemon import Daemon
from subfleet.timers import iso


def until(fn, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if value := fn():
            return value
        time.sleep(.01)
    raise AssertionError('timer condition did not arrive')


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    # Process containment has its own suite; this acceptance fake exercises real
    # daemon control/socket/SQL plus deterministic provider boundary callbacks.
    monkeypatch.setattr('subfleet.daemon.procs.boot_id', lambda: 'fake-boot')
    monkeypatch.setattr('subfleet.daemon.procs.proc_start', lambda pid: 'fake-start')
    policy = json.loads(Path('subfleet/default_policy.json').read_text())
    policy['timers'] = {'probe_interval_s': .15, 'keepalive_interval_s': .12}
    policy['reset_credits']['min_interval_min'] = .01
    policy['alerts']['operator_session'] = 'test-operator'
    with tempfile.TemporaryDirectory(prefix='sft-', dir='/tmp') as temporary:
        root = Path(temporary)
        (root / 'policy.json').write_text(json.dumps(policy))
        value = Daemon(root, tick_s=.01, term_grace_s=.01)
        yield value
        value.close()


def codex(daemon, number):
    home = daemon.root / f'codex-{number}'
    home.mkdir()
    (home / 'auth.json').write_text(json.dumps({'tokens': {'account_id': str(number), 'access_token': 'FAKE-ONLY'}}))
    lane = Lane(f'codex-{number}', 'codex', f'codex:{number}', Credential('codex', str(home), 'home'), str(home), LaneOwner.V2, False)
    daemon.store.put_lane(lane)
    return lane


def test_daemon_cycles_reset_alert_recovery_and_keepalive(daemon):
    """C-18.1 C-19.1 C-23.16–19 C-23.27 C-23.29 C-23.44 C-23.52: one integrated cycle verdict."""
    codex(daemon, 1)
    codex(daemon, 2)
    dead = codex(daemon, 3)
    claude = Lane('claude-1', 'claude', 'claude:fake:org', Credential('claude', 'fake-token', 'env'), None, LaneOwner.V2, False)
    daemon.store.put_lane(claude)
    state = {'limited': True, 'consumes': 0, 'dead_probes': 0, 'keepalives': 0}

    def opener(request, timeout):
        account = request.get_header('Chatgpt-account-id')
        if request.full_url == WHAM_USAGE_URL:
            if account == '3':
                state['dead_probes'] += 1
                return 401, b'{"error":{"code":"unauthorized"}}'
            limited = state['limited']
            return 200, json.dumps({'rate_limit': {'limit_reached': limited, 'allowed': not limited,
                'primary_window': {'window_minutes':10080, 'used_percent':100 if limited else 5,
                    'reset_at':int(time.time()+3*86400)}},
                'rate_limit_reset_credits':{'available_count':1, 'applicable_available_count':1}}).encode()
        if request.full_url == WHAM_RESET_CREDITS_URL:
            return 200, b'{"credits":[{"id":"gift","status":"available","reset_type":"codex_rate_limits"}]}'
        assert request.full_url == WHAM_RESET_CREDITS_CONSUME_URL
        state['consumes'] += 1
        assert daemon.store.query('SELECT state FROM actions')[0]['state'] == 'executing'
        return 200, b'{"code":"reset","windows_reset":2}'

    daemon.timers.adapter_factory = lambda provider: CodexAdapter(opener=opener)

    def turn(lane, purpose, holder, **kwargs):
        sent = iso(datetime.now(timezone.utc))
        if purpose == 'keepalive':
            state['keepalives'] += 1
        return Outcome(OutcomeClass.OK, 'fake Haiku', evidence={'requested_at':sent}, native_session_id='fake-session')

    daemon.timers.turn = turn
    thread = threading.Thread(target=daemon._control, daemon=True)
    thread.start()
    try:
        until(lambda: daemon.store.query('SELECT * FROM actions'))
        until(lambda: daemon.store.query("SELECT * FROM events WHERE kind='timer.cycle'"))
        assert daemon.store.query('SELECT state FROM actions')[0]['state'] == 'confirmed'
        assert not daemon.store.get_lane(dead.lane_id).enabled
        assert state['consumes'] == 1
        until(lambda: state['keepalives'] == 1)
        first_notices = daemon.dispatch('notice.pending', {'session_id':'test-operator'})['notices']
        assert first_notices
        assert all(row['notice_id'] < 0 for row in first_notices)
        state['limited'] = False
        until(lambda: any('recovered' in row['text'].lower() for row in daemon.dispatch('notice.pending', {'session_id':'test-operator'})['notices']))
        time.sleep(.35)
        assert state['consumes'] == 1 and state['dead_probes'] == 1 and state['keepalives'] == 1
        events = daemon.store.query("SELECT data_json FROM events WHERE kind='timer.keepalive'")
        assert any('skipped-open' in row['data_json'] for row in events)
        payload = json.loads((daemon.root / 'status.json').read_bytes())
        assert payload['codex']['homes'] and payload['claude']['accounts']
        assert 'five_hour_pct' not in payload['claude']['accounts'][0]['live']
        assert daemon.dispatch('daemon.status', {})['timers']['probe']['last_run']
        ids = [row['notice_id'] for row in first_notices]
        daemon.dispatch('notice.ack', {'session_id':'test-operator', 'notice_ids':ids})
        assert not set(ids) & {row['notice_id'] for row in daemon.dispatch('notice.pending', {'session_id':'test-operator'})['notices']}
    finally:
        daemon.stopping.set()
        thread.join(3)
        assert not thread.is_alive()


def test_hung_usage_cannot_block_api_or_shutdown(daemon):
    """C-16.4 C-18.1: a blocked injectable HTTP opener cannot block control or late-publish."""
    codex(daemon, 1)
    entered, release = threading.Event(), threading.Event()
    def opener(request, timeout):
        entered.set()
        release.wait(5)
        return 200, b'{"rate_limit":{"limit_reached":false}}'
    daemon.timers.adapter_factory = lambda provider: CodexAdapter(opener=opener)
    thread = threading.Thread(target=daemon._control, daemon=True)
    thread.start()
    try:
        assert entered.wait(2)
        begin = time.monotonic()
        assert daemon.dispatch('daemon.status', {})['timers']['probe']
        assert time.monotonic() - begin < 1
        daemon.close()
        thread.join(2)
        assert not thread.is_alive()
        assert time.monotonic() - begin < 2
        assert not (daemon.root / 'status.json').exists()
    finally:
        release.set()
        thread.join(2)


def test_socket_timer_status(daemon):
    """C-16.4 C-18.1: the actual Unix API reports timer runs when sandbox permits sockets."""
    try:
        with socket.socket(socket.AF_UNIX) as probe:
            probe.bind(str(daemon.root / 'socket-check'))
    except PermissionError:
        pytest.skip('sandbox denies Unix socket binding; daemon control tests still run')
    thread = threading.Thread(target=daemon.serve_forever, daemon=True)
    thread.start()
    try:
        until(lambda: (daemon.root / 'daemon.sock').exists())
        time.sleep(.25)
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(2)
            sock.connect(str(daemon.root / 'daemon.sock'))
            sock.sendall(b'{"v":1,"id":"timers","op":"daemon.status","args":{}}\n')
            result = json.loads(sock.recv(200000))
        assert result['ok'] and result['result']['timers']['probe']['last_run']
    finally:
        daemon.stopping.set()
        thread.join(3)


def test_keepalive_uses_real_guardian_when_process_inspection_allowed(monkeypatch):
    """C-5.1 C-8.4 C-23.19 C-23.29: guardian fake turn publishes activity without a job."""
    from subfleet import procs
    from tests.fake_adapter import FakeAdapter
    try:
        procs.boot_id()
        procs.proc_start(__import__('os').getpid())
    except (OSError, procs.InspectionError):
        pytest.skip('sandbox denies process inspection; bounded turn unit tests cover this seam')
    monkeypatch.setattr('subfleet.daemon.get_adapter', lambda provider: FakeAdapter())
    with tempfile.TemporaryDirectory(prefix='sfg-', dir='/tmp') as temporary:
        value = Daemon(temporary, term_grace_s=.05)
        try:
            lane = Lane('claude-1', 'claude', 'claude:fake', Credential('claude', temporary, 'home'), temporary, LaneOwner.V2, False)
            value.store.put_lane(lane)
            assert value.timers._keepalive_lane(lane) == 'ok'
            reading = value.store.list_readings(lane.lane_id)[0]
            assert reading['source'] == 'keepalive' and reading['label'] == 'admission-observed'
            assert reading['utilization'] is None and reading['resets_at'] is None
            assert not value.store.list_jobs() and not value.store.list_leases()
        finally:
            value.close()
