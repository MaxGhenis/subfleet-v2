"""Review probes for #223 (D-WALL1 / D-KILL1). Retained from review r1, with expected results updated to the hub decisions.

Each probe drives the real daemon store and passes; a probe documenting a
finding asserts the behaviour observed, and its docstring says why it matters.
"""
from datetime import datetime, timedelta, timezone
import json
import os
import socket
import struct
import threading

import pytest
import random

from hypothesis import HealthCheck, assume, event as hevent, given, settings, strategies as st

from subfleet import daemon as daemon_module, protocol
from subfleet.conversations.peers import AuditToken
from subfleet.daemon import LIVE_TICK
from subfleet.offline import Offline
from tests.fake.test_state_contract import state_daemon, reserve, receipt_fixture  # noqa: F401

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.t = NOW


@pytest.fixture
def clocked(state_daemon, monkeypatch):
    service, harness = state_daemon
    clock = Clock()

    class Moving(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.t if tz else clock.t.replace(tzinfo=None)

    monkeypatch.setattr(daemon_module, 'datetime', Moving)
    monkeypatch.setattr(service, '_read_answer', lambda *args: None)
    monkeypatch.setattr(service, '_inspect_running', lambda *args: True)
    monkeypatch.setattr(service.conversations, 'stop', lambda *args: False)
    return service, harness, clock


def fmt(moment, style):
    if style == 'Zs':
        return moment.isoformat(timespec='seconds').replace('+00:00', 'Z')
    if style == 'Zms':
        return moment.isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    if style == 'Zus':
        return moment.isoformat(timespec='microseconds').replace('+00:00', 'Z')
    return moment.isoformat(timespec='microseconds')          # '+00:00' offset


def parse(stamp):
    return datetime.fromisoformat(stamp.replace('Z', '+00:00'))


def oracle(rows, now):
    """Independent D-WALL1 sum: every non-reserved attempt from its start to its
    end (now while live), clamped at zero; a missing end point charges nothing."""
    total = 0.0
    for row in rows:
        if row['state'] == 'reserved':
            continue
        end = row['finished_at'] or (fmt(now, 'Zus') if row['state'] in ('starting', 'running') else None)
        if row['started_at'] and end:
            total += max(0.0, (parse(end) - parse(row['started_at'])).total_seconds())
    return total


def cancel_events(service, jid):
    return [json.loads(row['data_json']) for row in service.store.query(
        "SELECT data_json FROM events WHERE job_id=? AND kind='job.cancel_requested' ORDER BY event_id", (jid,))]


STYLES = st.sampled_from(['Zs', 'Zms', 'Zus', 'off'])
PRIOR = st.tuples(st.sampled_from(['failed', 'succeeded', 'interrupted', 'lost', 'quarantined']),
                  st.integers(-2_000_000, 30_000_000),            # duration in microseconds (negative: skew)
                  st.integers(0, 10_000_000_000),                 # gap after it, microseconds
                  st.sampled_from([True, True, True, False]),     # has start
                  st.sampled_from([True, True, True, False]),     # has end
                  STYLES, STYLES)


@settings(max_examples=150, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(priors=st.lists(PRIOR, max_size=5),
       live=st.sampled_from(['running', 'starting', 'finalizing', 'reserved', None]),
       live_us=st.integers(1, 40_000_000), live_start=st.sampled_from([True, True, True, False]), live_style=STYLES,
       delta=st.sampled_from([-1.0, -0.002, -0.001, -0.0005, -0.000001, 0.0, 0.000001, 0.0005, 0.001, 0.002, 1.0]))
def test_probe_exact_sum_matches_an_independent_oracle_and_the_tick_never_skips(
        clocked, monkeypatch, priors, live, live_us, live_start, live_style, delta):
    service, harness, clock = clocked
    monkeypatch.setattr(service, '_kill_attempt', lambda a: None)
    monkeypatch.setattr(service, '_finalize', lambda a: None)
    jid = service.dispatch('submit', harness.submit_args(caller_session='caller'))['job_id']
    span = sum(max(d, 0) + g for _, d, g, *_ in priors) + live_us + 1_000_000
    cursor = NOW - timedelta(microseconds=span)
    rows = []
    for seq, (state, dur, gap, has_start, has_end, s1, s2) in enumerate(priors, 1):
        start = cursor
        end = cursor + timedelta(microseconds=dur)
        rows.append(dict(attempt_id=f'{jid}/a{seq}', job_id=jid, seq=seq, lane_id='codex-1',
                         model_requested='astra', state=state,
                         started_at=fmt(start, s1) if has_start else None,
                         finished_at=fmt(end, s2) if has_end else None))
        cursor = end + timedelta(microseconds=gap) if dur > 0 else start + timedelta(microseconds=gap)
    aid = None
    if live:
        seq = len(priors) + 1
        aid = f'{jid}/a{seq}'
        start = NOW - timedelta(microseconds=live_us)
        rows.append(dict(attempt_id=aid, job_id=jid, seq=seq, lane_id='codex-1', model_requested='astra',
                         state=live,
                         started_at=fmt(start, live_style) if live_start and live != 'reserved' else None,
                         finished_at=fmt(NOW - timedelta(microseconds=live_us // 2), 'Zs')
                         if live == 'finalizing' else None))
    expected = oracle(rows, NOW)
    assume(expected + delta > 0)
    cap = expected + delta
    service.store.update_job(jid, state='running' if live else 'waiting', started_at=fmt(NOW - timedelta(
        microseconds=span), 'Zs'), max_wall_s=cap)
    for row in rows:
        service.store.add_attempt(**row)
    assert service._running_s(jid) == pytest.approx(expected, abs=1e-6)
    if live:
        (service.root / 'jobs' / jid / f'a{len(rows)}').mkdir(exist_ok=True)
        service._inspect_next.pop(aid, None)
        service._starting_deadlines[aid] = float('inf')
        tick = next(row for row in service.store.query(LIVE_TICK) if row['attempt_id'] == aid)
        if live == 'running' and expected >= cap:
            # The SQL prefilter must never skip an attempt the exact sum has expired.
            assert service._has_work(tick), (expected, cap, tick['prior_running_s'])
        service._process_attempt(aid)
    else:
        service.store.update_lane('codex-1', enabled=False)
        service._admit()
        service.store.update_lane('codex-1', enabled=True)
    stopped = bool(service.store.get_job(jid)['cancel_requested_at'])
    hevent(f'live={live} stopped={stopped} delta={delta}')
    hevent(f'missing-end={any(r["finished_at"] is None and r["state"] not in ("starting","running","finalizing") for r in rows)}')
    assert stopped == (live in ('starting', 'running') and expected >= cap), (expected, cap, live)
    if stopped:
        [event] = cancel_events(service, jid)
        assert event['by'] == 'daemon' and event['reason'] == 'max_wall_s'
        assert event['running_s'] == pytest.approx(expected, abs=1e-6)


def mark_starting(service, aid):
    """The launch's own commit (daemon._launch), without spawning a guardian."""
    with service.store.transaction('attempt.starting', attempt_id=aid) as tx:
        tx.execute("UPDATE attempts SET state='starting',started_at=? WHERE attempt_id=? AND state='reserved'",
                   (daemon_module.utcnow_ms(), aid))


def test_review_lifecycle_charges_execution_only_through_real_transitions(clocked, monkeypatch):
    """A launch failure straight to finalizing, a transient settle, a 30 h hold, a
    reservation held 2 h, then a short run: only the execution is charged."""
    service, harness, clock = clocked
    killed = []
    monkeypatch.setattr(service, '_kill_attempt', lambda a: killed.append(a['attempt_id']))
    from subfleet.adapters.registry import register
    from subfleet.contracts import Outcome, OutcomeClass
    from tests.fake_adapter import FakeAdapter

    class TransientAdapter(FakeAdapter):
        def classify(self, attempt_dir, launch, exit_info):
            return Outcome(OutcomeClass.TRANSIENT, 'fixture transport disconnected')

    register('codex', TransientAdapter)
    jid, first, adir = reserve(service, harness)
    service.store.update_job(jid, max_wall_s=100)
    clock.t += timedelta(hours=3)                       # reserved: charges nothing
    # Launch failure: reserved -> finalizing directly records its start now.
    clock.t += timedelta(seconds=0)
    finalizing = receipt_fixture(service, first, adir, rc=1)
    assert parse(finalizing['started_at']) == clock.t
    clock.t += timedelta(seconds=30)                    # 30 s of finalization
    service._finalize(finalizing)
    assert service.store.get_attempt(first['attempt_id'])['state'] == 'failed'
    assert service._running_s(jid) == pytest.approx(0)
    assert service.store.get_job(jid)['state'] == 'waiting'
    clock.t += timedelta(hours=30)                      # a hold far past the old wall
    service.store.update_job(jid, next_check_at=None)
    service._admit()
    second = service.store.list_attempts(jid)[-1]
    assert second['seq'] == 2 and second['state'] == 'reserved'
    clock.t += timedelta(hours=2)                       # held in reserved
    mark_starting(service, second['attempt_id'])
    service._pending_launches.discard(second['attempt_id'])
    service._starting_deadlines[second['attempt_id']] = float('inf')
    (adir.parent / 'a2').mkdir(exist_ok=True)
    clock.t += timedelta(seconds=99)                    # 99 s of provider execution < 100
    service._process_attempt(second['attempt_id'])
    assert service.store.get_job(jid)['cancel_requested_at'] is None
    clock.t += timedelta(seconds=1)                     # 100 s of provider execution reaches the cap
    service._process_attempt(second['attempt_id'])
    assert cancel_events(service, jid) == [{'by': 'daemon', 'reason': 'max_wall_s', 'running_s': 100.0}]
    assert killed == [second['attempt_id']]


@pytest.mark.parametrize("reboot", [False, True])
def test_review_restart_inspects_dead_provider_before_wall_and_retries(clocked, monkeypatch, reboot):
    """One hour alive, six hours unobserved: lost and read-only retry, never a wall kill."""
    service, harness, clock = clocked
    jid, attempt, adir = reserve(service, harness)
    mark_starting(service, attempt['attempt_id'])
    aid = attempt['attempt_id']
    service.store.update_attempt(aid, state='running', boot_id='old-boot' if reboot else 'unit-test-boot',
                                 guardian_pid=424242, pgid=424242, proc_start='fixture-start')
    clock.t += timedelta(hours=1)
    last_alive = daemon_module.utcnow_ms()
    service.store.update_attempt(aid, evidence_json=json.dumps({'last_observed_alive_at': last_alive}))
    clock.t += timedelta(hours=6)
    monkeypatch.setattr(service, '_inspect_running', daemon_module.Daemon._inspect_running.__get__(service))
    inspected = []
    def table(due):
        inspected.append(due)
        return daemon_module.procs.ProcessTable({}, 'unit-test-boot'), due + 1
    monkeypatch.setattr(service, '_process_table', table)
    monkeypatch.setattr(daemon_module.procs, 'liveness', lambda *args: 'dead')
    monkeypatch.setattr(service, '_kill_attempt', lambda *args, **kw: pytest.fail('dead provider was killed'))
    service._process_attempt(aid)
    assert inspected
    final = service.store.get_attempt(aid)
    assert final['state'] == 'lost'
    assert final['finished_at'] == last_alive
    assert service._running_s(jid) == pytest.approx(3600)
    assert cancel_events(service, jid) == []
    assert service.store.get_job(jid)['state'] == 'waiting'
    clock.t += timedelta(seconds=61)
    service._admit()
    assert [a['state'] for a in service.store.list_attempts(jid)] == ['lost', 'reserved']


def test_review_finalization_crossing_the_cap_preserves_a_verified_success(clocked, monkeypatch):
    """Review r1 probe, asserting the accepted round-3 behavior."""
    service, harness, clock = clocked
    jid, attempt, adir = reserve(service, harness)
    service.store.update_job(jid, max_wall_s=21600)
    mark_starting(service, attempt['attempt_id'])
    clock.t += timedelta(seconds=21599)
    finalizing = receipt_fixture(service, attempt, adir, rc=0)
    salvage = service._salvage

    def slow_salvage(*args, **kwargs):
        clock.t += timedelta(seconds=2)
        return salvage(*args, **kwargs)

    monkeypatch.setattr(service, '_salvage', slow_salvage)
    service._finalize(finalizing)
    job = service.store.get_job(jid)
    final = service.store.get_attempt(attempt['attempt_id'])
    assert (job['state'], job['accepted_attempt_id']) == ('succeeded', attempt['attempt_id'])
    assert final['killed_by'] is None and final['signal'] is None and final['rc'] == 0
    assert final['outcome_class'] == 'ok'
    assert service._running_s(jid) == 21599
    assert (adir / 'deliverable.md').read_bytes().strip() == b'fixture result'


def test_probe_finalization_under_the_cap_is_accepted(clocked, monkeypatch):
    """Control for the finding above: the same attempt, salvage under 1 s, succeeds."""
    service, harness, clock = clocked
    jid, attempt, adir = reserve(service, harness)
    service.store.update_job(jid, max_wall_s=21600)
    mark_starting(service, attempt['attempt_id'])
    clock.t += timedelta(seconds=21598)
    service._finalize(receipt_fixture(service, attempt, adir, rc=0))
    assert service.store.get_job(jid)['state'] == 'succeeded'


def socket_kill(service, args, peer_pid):
    client, server = socket.socketpair()
    peer = (AuditToken(struct.pack('=8I', 0, 0, 0, 0, 0, peer_pid, 0, 1), server)
            if peer_pid is not None else None)
    try:
        future = service._start_request(server, threading.Lock(),
                                        protocol.Request(v=1, id='k', op='kill', args=args), 0, peer)
        future.result(timeout=10)
        return json.loads(client.recv(65536))
    finally:
        client.close()
        server.close()


def test_probe_client_cannot_forge_daemon_source_or_pid(clocked):
    service, harness, clock = clocked
    jid = service.dispatch('submit', harness.submit_args(caller_session='caller'))['job_id']
    reply = socket_kill(service, {'job_id': jid, 'session_id': 'other', 'by': 'daemon', 'pid': 1,
                                  'reason': 'max_wall_s', 'running_s': 99999}, os.getpid())
    assert reply['ok']
    assert cancel_events(service, jid) == [{'by': 'client', 'session_id': 'other', 'pid': os.getpid(),
                                          'reason': 'max_wall_s'}]
    # Same-session spoof: a client naming the caller's session suppresses the notice.
    other = service.dispatch('submit', harness.submit_args(caller_session='caller'))['job_id']
    socket_kill(service, {'job_id': other, 'session_id': 'caller'}, os.getpid())
    assert [row for row in service.store.list_notices('caller')
            if row['job_id'] == other and 'requested by' in row['text']] == []


def test_probe_kill_without_a_peer_token_records_null_pid(clocked):
    service, harness, clock = clocked
    jid = service.dispatch('submit', harness.submit_args(caller_session='caller'))['job_id']
    assert socket_kill(service, {'job_id': jid, 'session_id': 'other'}, None)['ok']
    assert cancel_events(service, jid) == [{'by': 'client', 'session_id': 'other', 'pid': None,
                                          'reason': 'client-request'}]


def test_probe_kill_after_the_wall_keeps_the_wall_as_first_request(clocked, monkeypatch):
    service, harness, clock = clocked
    monkeypatch.setattr(service, '_kill_attempt', lambda a: None)
    jid, attempt, adir = reserve(service, harness, caller_session='caller')
    service.store.update_job(jid, max_wall_s=5)
    mark_starting(service, attempt['attempt_id'])
    service._starting_deadlines[attempt['attempt_id']] = float('inf')
    clock.t += timedelta(seconds=6)
    service._process_attempt(attempt['attempt_id'])
    socket_kill(service, {'job_id': jid, 'session_id': 'other', 'reason': 'late'}, os.getpid())
    shown = service.dispatch('show', {'job_id': jid})['cancel']
    assert (shown['by'], shown['reason']) == ('daemon', 'max_wall_s')
    assert Offline(harness.root).show_job(jid)['cancel']['reason'] == 'max_wall_s'
    assert [e['reason'] for e in cancel_events(service, jid)] == ['max_wall_s']


def test_probe_legacy_empty_cancel_event_shows_without_attribution(clocked, capsys, monkeypatch):
    """A pre-D-KILL1 row (`data_json = {}`) must not break show, offline or online."""
    service, harness, clock = clocked
    jid = service.dispatch('submit', harness.submit_args(caller_session='caller'))['job_id']
    with service.store.transaction('job.cancel_requested', job_id=jid) as tx:
        tx.execute("UPDATE jobs SET cancel_requested_at=?,state='cancelled',rc=130,finished_at=? WHERE job_id=?",
                   (daemon_module.utcnow(), daemon_module.utcnow(), jid))
        service._notice(tx, service._job(jid), 'legacy cancel')
    shown = service.dispatch('show', {'job_id': jid})
    assert set(shown['cancel']) == {'at'}
    assert set(Offline(harness.root).show_job(jid)['cancel']) == {'at'}
    from subfleet import cli
    monkeypatch.setattr(cli.Client, 'call', lambda self, op, args: shown)
    monkeypatch.setattr(cli, 'session_id', lambda: None)
    cli.main(['runs', 'show', jid, '--json'])
    assert json.loads(capsys.readouterr().out)['cancel'] == shown['cancel']


def test_review_repeat_kill_reaches_children_without_rewriting_root(clocked):
    """Review r1 probe, asserting the accepted round-3 behavior."""
    service, harness, clock = clocked
    parent, attempt, adir = reserve(service, harness, caller_session='caller')
    child = service.dispatch('submit', harness.submit_args(caller_session='caller', parent_job_id=parent))['job_id']
    service._request_cancel(parent, {'by': 'daemon', 'reason': 'interrupt', 'running_s': 0.0},
                            attempt_id=attempt['attempt_id'])
    first = service.store.get_job(parent)['cancel_requested_at']
    assert service.dispatch('kill', {'job_id': parent, 'session_id': 'caller'})['status'] == 'cancel requested'
    assert service.store.get_job(parent)['cancel_requested_at'] == first
    assert cancel_events(service, parent) == [{'by': 'daemon', 'reason': 'interrupt', 'running_s': 0.0}]
    assert service.store.get_job(child)['state'] == 'cancelled'
    assert cancel_events(service, child) == [{'by': 'client', 'session_id': 'caller', 'pid': None,
                                             'reason': 'client-request', 'parent_job_id': parent}]


def test_probe_sql_prior_sum_is_never_below_the_exact_sum(clocked):
    """Differential, 2000 seeded cases: the tick's SQL `prior_running_s` against
    the exact Python sum of the same terminal attempts, over every timestamp
    precision the writers use (guardian and utcnow seconds, utcnow_ms, the
    receipt), microsecond offsets and skewed rows. The prefilter can only skip a
    crossed cap if SQL comes out below the exact sum."""
    service, harness, clock = clocked
    rng = random.Random(223)
    worst = float('inf')
    for case in range(2000):
        jid = service.dispatch('submit', harness.submit_args())['job_id']
        rows, cursor = [], NOW - timedelta(seconds=rng.uniform(1, 50000))
        for seq in range(1, rng.randint(1, 6) + 1):
            start = cursor + timedelta(microseconds=rng.randint(0, 999_999))
            end = start + timedelta(microseconds=rng.choice([rng.randint(-1500, 1500), rng.randint(0, 10**8)]))
            rows.append(dict(attempt_id=f'{jid}/a{seq}', job_id=jid, seq=seq, lane_id='codex-1',
                             model_requested='astra', state=rng.choice(['failed', 'succeeded', 'lost', 'interrupted']),
                             started_at=fmt(start, rng.choice(['Zs', 'Zms', 'Zus', 'off'])),
                             finished_at=fmt(end, rng.choice(['Zs', 'Zms', 'Zus', 'off']))))
            cursor = max(start, end) + timedelta(microseconds=rng.randint(0, 10**9))
        live = f'{jid}/a{len(rows) + 1}'
        rows.append(dict(attempt_id=live, job_id=jid, seq=len(rows) + 1, lane_id='codex-1',
                         model_requested='astra', state='running', started_at=fmt(NOW, 'Zms'), finished_at=None))
        for row in rows:
            service.store.add_attempt(**row)
        exact_prior = oracle(rows[:-1], NOW)
        tick = next(row for row in service.store.query(LIVE_TICK) if row['attempt_id'] == live)
        worst = min(worst, tick['prior_running_s'] - exact_prior)
        assert tick['prior_running_s'] >= exact_prior, (case, rows, tick['prior_running_s'], exact_prior)
        assert service._running_s(jid) == pytest.approx(exact_prior, abs=1e-6)
        with service.store.transaction('probe.cleanup', job_id=jid) as tx:
            tx.execute("UPDATE attempts SET state='failed',finished_at=started_at WHERE attempt_id=?", (live,))
    assert worst >= 0


@pytest.mark.parametrize('pinned', [False, True])
def test_probe_a_retry_refused_for_good(clocked, pinned):
    """Review r1 probe, asserting the accepted round-3 behavior."""
    service, harness, clock = clocked
    from subfleet.adapters.registry import register
    from subfleet.contracts import Outcome, OutcomeClass
    from tests.fake_adapter import FakeAdapter

    class TransientAdapter(FakeAdapter):
        def classify(self, attempt_dir, launch, exit_info):
            return Outcome(OutcomeClass.TRANSIENT, 'fixture transport disconnected')

    register('codex', TransientAdapter)
    jid, first, adir = reserve(service, harness, caller_session='caller',
                               **({'pinned_lane': 'codex-1'} if pinned else {}))
    service._finalize(receipt_fixture(service, first, adir, rc=1))
    service.store.update_lane('codex-1', enabled=False)
    service._admit()
    clock.t += timedelta(seconds=daemon_module.PIN_NOTICE_AFTER_S)
    service._admit()
    notices = service.store.query("SELECT data_json FROM events WHERE job_id=? AND kind='job.pin_noticed'", (jid,))
    assert len(notices) == 1
    assert json.loads(notices[0]['data_json'])['reasons'] == ['disabled']
    clock.t += timedelta(days=30)
    service._admit()
    job = service.store.get_job(jid)
    assert (job['state'], job['rc']) == ('failed', 3)
    assert job['cancel_requested_at'] is None
    assert len(service.store.list_attempts(jid)) == 1
    assert 'disabled' in service.store.list_notices('caller')[0]['text']


@pytest.mark.parametrize('daemon_late_h', [0, 6])
def test_review_a_late_receipt_keeps_its_success(clocked, monkeypatch, daemon_late_h):
    """Review r1 probe, asserting the accepted round-3 behavior."""
    service, harness, clock = clocked
    jid, attempt, adir = reserve(service, harness)
    mark_starting(service, attempt['attempt_id'])
    service._starting_deadlines[attempt['attempt_id']] = float('inf')
    clock.t += timedelta(hours=1)
    (adir / 'stdout').write_bytes(b'fixture result\n')
    (adir / 'stderr').write_bytes(b'')
    (adir / 'lane.log').write_bytes(b'')
    (adir / 'stream.jsonl').write_text('{"type":"turn.completed"}\n')
    (adir / 'exit.json').write_text(json.dumps({'rc': 0, 'signal': None, 'wall_s': 3600, 'child_pid': None,
                                                'finished_at': daemon_module.utcnow()}))
    clock.t += timedelta(hours=daemon_late_h)
    service._process_attempt(attempt['attempt_id'])         # reads the receipt (or the wall fires first)
    service._process_attempt(attempt['attempt_id'])         # finalizes
    job = service.store.get_job(jid)
    assert job['state'] == 'succeeded'
    assert service._running_s(jid) == 3600
    assert cancel_events(service, jid) == []


@pytest.mark.parametrize('form', [[], ['--json']])
def test_probe_offline_cli_runs_show_prints_the_cancel(clocked, capsys, monkeypatch, form):
    """`runs show` with the daemon down reads the store offline and prints `cancel`."""
    service, harness, clock = clocked
    from subfleet import cli
    jid = service.dispatch('submit', harness.submit_args(caller_session='caller'))['job_id']
    service.dispatch('kill', {'job_id': jid, 'session_id': 'other', 'reason': 'offline check'})

    class Down:
        def call(self, *args, **kwargs):
            raise cli.DaemonUnavailable('daemon is down (probe)')

    monkeypatch.setattr(cli, '_client', lambda args, **kw: Down())
    monkeypatch.setattr(cli, '_root', lambda args: harness.root)
    monkeypatch.setattr(cli, 'session_id', lambda: None)
    cli.main(['runs', 'show', jid, *form])
    metadata = json.loads(capsys.readouterr().out.split('\n--- out.md ---')[0])
    assert metadata['cancel']['session_id'] == 'other'
    assert metadata['cancel']['reason'] == 'offline check'
    assert metadata['cancel']['by'] == 'client' and 'at' in metadata['cancel']


@pytest.mark.parametrize("online", [False, True])
@pytest.mark.parametrize("form", [[], ["--json"]])
def test_review_runs_show_labels_client_identity_sources(clocked, capsys, monkeypatch, online, form):
    service, harness, clock = clocked
    from subfleet import cli
    jid = service.dispatch('submit', harness.submit_args(caller_session='caller'))['job_id']
    service.dispatch('kill', {'job_id': jid, 'session_id': 'stated-session'})
    class Reader:
        def call(self, op, args):
            if not online:
                raise cli.DaemonUnavailable('fixture offline')
            return service.dispatch(op, args)
    monkeypatch.setattr(cli, '_client', lambda *args, **kw: Reader())
    monkeypatch.setattr(cli, '_root', lambda args: harness.root)
    monkeypatch.setattr(cli, 'session_id', lambda: None)
    cli.main(['runs', 'show', jid, *form])
    cancel = json.loads(capsys.readouterr().out.split('\n--- out.md ---')[0])['cancel']
    assert cancel['session_id_source'] == 'client-stated'
    assert cancel['pid_source'] == 'socket-peer-audit-token'
