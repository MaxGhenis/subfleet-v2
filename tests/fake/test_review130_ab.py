"""PR #130 review: A/B probes of the daemon's retention driver.

Run unchanged against 08d6a09a (#76) and a5ea556a (#130). Each test states the
behavior §5 of review-109-r1 requires; on the base it reproduces the gap the
port names, on the head it shows the fix. `_due` is the control loop's own test
(daemon.py: `time.monotonic() - self._last_maintenance >= 3600`).
"""
import sqlite3
import time

import pytest

from subfleet import daemon as daemon_module
from tests.fake.test_state_contract import state_daemon  # noqa: F401


def _fake(monkeypatch, result):
    monkeypatch.setattr(daemon_module, 'maintenance', lambda *a, **k: dict(result))


def _due(service):
    return time.monotonic() - service._last_maintenance >= 3600


def test_gap1_a_cancelled_pass_rearms_the_hour(state_daemon, monkeypatch):
    """daemon.py:3400 at 08d6a09a: marks CancelledError but leaves the pass due."""
    service, _ = state_daemon
    service._last_maintenance = time.monotonic() - 7200
    _fake(monkeypatch, {'interrupted': 'cancelled', 'pruned': []})
    service._retention()
    assert service.timers.status()['retention']['last_error_type'] == 'CancelledError'
    assert not _due(service), 'a cancelled pass is still due: the next tick offers it again'


def test_gap2_a_deadline_without_progress_waits_the_hour_without_raising(state_daemon, monkeypatch):
    """daemon.py:3403 at 08d6a09a: every deadline raises onto C-5.10's clock."""
    service, _ = state_daemon
    service._last_maintenance = time.monotonic() - 7200
    _fake(monkeypatch, {'interrupted': 'deadline', 'pruned': [], 'jobs_after': 3})
    service._retention()
    assert not _due(service)
    assert service.timers.status()['retention']['last_error_type'] == 'TimeoutError'


def test_gap5_a_stalled_catch_up_waits_the_hour_at_once(state_daemon, monkeypatch):
    """daemon.py:3408 at 08d6a09a: a non-advancing batch doubles 10, 20, 40 ... toward the hour."""
    service, _ = state_daemon
    _fake(monkeypatch, {'more': True, 'progressed': False, 'pruned': []})
    waits = []
    for _ in range(3):
        service._retention()
        waits.append(round(3600 - (time.monotonic() - service._last_maintenance)))
    assert waits == [3600, 3600, 3600], waits


def test_gap6a_a_failing_notice_prune_does_not_fail_the_pass(state_daemon, monkeypatch):
    """daemon.py:3404 at 08d6a09a: the prune follows the pass and its error fails it,
    so a completed pass is retried on C-5.10's clock (at most 60 s) for as long as
    the error lasts (for example SQLITE_FULL on a full disk)."""
    service, _ = state_daemon
    service._last_maintenance = time.monotonic() - 7200
    def broken():
        raise sqlite3.OperationalError('database or disk is full')
    monkeypatch.setattr(service, '_prune_service_notices', broken)
    _fake(monkeypatch, {'pruned': []})
    service._retention()
    assert not _due(service)


@pytest.mark.parametrize('result', [{'interrupted': 'cancelled', 'pruned': []},
                                    {'interrupted': 'deadline', 'pruned': [], 'jobs_after': 3},
                                    {'interrupted': 'deadline', 'pruned': ['a'], 'jobs_after': 3}])
def test_gap6b_notices_are_pruned_whatever_becomes_of_the_pass(state_daemon, monkeypatch, result):
    service, _ = state_daemon
    calls = []
    monkeypatch.setattr(service, '_prune_service_notices', lambda: calls.append(1) or 0)
    _fake(monkeypatch, result)
    try:
        service._retention()
    except TimeoutError:
        pass
    assert calls == [1]


def test_pins_are_asked_again_inside_the_delete_transaction_through_the_daemon(state_daemon, monkeypatch):
    """C-26.12: the conversation service pins a job only once the pass is inside
    that job's delete transaction. The real pass, called by the daemon, must keep it."""
    from contextlib import contextmanager
    service, _ = state_daemon
    for name in ('late-pin', 'free'):
        service.store.add_job(job_id=name, request_id=name, payload_digest='digest', kind='turn',
                              workdir=str(service.root), prompt_path='/prompt', sandbox='read-only', state='succeeded')
        (service.root / 'jobs' / name).mkdir(parents=True)
        (service.root / 'jobs' / name / 'stdout').write_bytes(b'x' * 10)
    service.policy = {**service.policy, 'retention': {**service.policy['retention'], 'turn_jobs': 0,
                                                      'turn_keep_days': 0}}
    kinds, asked, real_transaction = [], [], service.store.transaction

    @contextmanager
    def transaction(kind='state.changed', **options):
        kinds.append(kind)
        try:
            with real_transaction(kind, **options) as conn:
                yield conn
        finally:
            kinds.pop()
    monkeypatch.setattr(service.store, 'transaction', transaction)

    def pins():
        inside = bool(kinds) and kinds[-1] == 'retention.pruned'
        asked.append(inside)
        return {'late-pin'} if inside else set()
    monkeypatch.setattr(service.conversations, 'retention_pins', pins)
    service._last_maintenance = 0
    service._retention()
    assert any(asked), 'pins were never asked inside a delete transaction'
    assert service.store.get_job('late-pin') is not None
    assert service.store.get_job('free') is None


class _Skipping:
    """The real monotonic clock plus skipped seconds, restarted for every pass."""

    def __init__(self):
        self.skipped = 0.0

    def __call__(self):
        return time.monotonic() + self.skipped


def _old_jobs(service, names):
    for index, name in enumerate(names):
        service.store.add_job(job_id=name, request_id=name, payload_digest='digest', kind='dispatch',
                              workdir=str(service.root), prompt_path='/prompt', sandbox='read-only',
                              state='succeeded')
        (service.root / 'jobs' / name).mkdir(parents=True)
        (service.root / 'jobs' / name / 'stdout').write_bytes(b'x' * 10)
        with service.store.transaction('fixture.age') as conn:
            conn.execute('UPDATE jobs SET created_at=? WHERE job_id=?', (f'2026-01-01T00:00:{index:02d}Z', name))
    service.policy = {**service.policy, 'retention': {**service.policy['retention'], 'jobs': 0}}


def _real_pass(monkeypatch, clock, holders):
    real = daemon_module.maintenance

    def maintenance(*args, **kwargs):
        clock.skipped = 0.0
        return real(*args, clock=clock, holders=holders, **kwargs)
    monkeypatch.setattr(daemon_module, 'maintenance', maintenance)


def _passes(service, count):
    rows = []
    for _ in range(count):
        try:
            service._retention()
            outcome = 'returned'
        except TimeoutError:
            outcome = 'raised'
        wait = round(3600 - (time.monotonic() - service._last_maintenance))
        rows.append((outcome, wait, len(service.store.list_jobs())))
    return rows


@pytest.mark.parametrize("overhead", [200, 400])
def test_f2_slow_pin_reads_never_stall_retention_at_the_hour(state_daemon, monkeypatch, overhead):
    """Each pass spends 200 s before selection (between the 180 s deadline and
    deadline + SLICE_S). 08d6a09a still starts, archives and commits one job a
    pass; a5ea556a raises its deadline before selection, has done nothing, and
    rearms the hour, pass after pass."""
    from subfleet import retention
    service, _ = state_daemon
    _old_jobs(service, ['a', 'b'])
    clock = _Skipping()
    _real_pass(monkeypatch, clock, lambda *a, **k: {})
    real_reasons = retention._Pass._reasons

    def reasons(run, *args, **kwargs):
        clock.skipped += overhead
        return real_reasons(run, *args, **kwargs)
    monkeypatch.setattr(retention._Pass, '_reasons', reasons)
    rows = _passes(service, 3)
    print('\nSLOW-PINS-DAEMON', rows)
    assert rows[-1][2] == 0, rows


def test_churn_a_failing_holder_scan_waits_the_hour_then_recovers(state_daemon, monkeypatch):
    """The old 5 s catch-up cycled every job through quarantine on failed lsof.
    A pass-wide blocker now waits the hour; a recovered scanner permits catch-up."""
    from subfleet.retention_holders import ScanFailed
    service, _ = state_daemon
    _old_jobs(service, [f'j{n:02d}' for n in range(70)])

    scans, unavailable = [], [True]

    def failing(*args, **kwargs):
        scans.append(1)
        if unavailable[0]:
            raise ScanFailed('lsof exited 1: fixture')
        return {}
    _real_pass(monkeypatch, _Skipping(), failing)
    rows = _passes(service, 1)
    assert rows == [('returned', 3600, 70)]
    assert len(scans) == 1
    assert not _due(service), 'failed listings must not offer the next batch after 5 s'
    assert service.timers.status()['retention']['last_error_type'] == 'ScanFailed'
    unavailable[0] = False
    assert _passes(service, 1) == [('returned', 5, 38)]


def test_rule1_a_deadline_after_progress_still_raises_with_more_waiting(state_daemon, monkeypatch):
    """§5 rule 1 with the review's fixes applied as well: three old jobs, a batch
    of two, the clock passes the deadline after the first commit. The pass keeps
    `pruned`, reports more waiting, and the daemon raises onto C-5.10's clock."""
    from contextlib import contextmanager
    from subfleet import retention
    service, _ = state_daemon
    _old_jobs(service, ['a', 'b', 'c'])
    clock = _Skipping()
    real = daemon_module.maintenance

    def maintenance(*args, **kwargs):
        return real(*args, clock=clock, holders=lambda *a, **k: {}, batch=2, **kwargs)
    monkeypatch.setattr(daemon_module, 'maintenance', maintenance)
    real_transaction = service.store.transaction

    @contextmanager
    def transaction(kind='state.changed', **options):
        with real_transaction(kind, **options) as conn:
            yield conn
        if kind == 'retention.pruned':
            clock.skipped = 10 ** 6
    monkeypatch.setattr(service.store, 'transaction', transaction)
    service._last_maintenance = 0
    with pytest.raises(TimeoutError):
        service._retention()
    assert service._last_maintenance == 0
    assert service.store.get_job('c') is not None and service.store.get_job('a') is None
