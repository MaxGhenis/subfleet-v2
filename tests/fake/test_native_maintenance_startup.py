"""Operator maintenance waits for recovery; failed retention follows its retry clock."""
import json

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import cli, daemon as daemon_module
from tests.fake.test_state_contract import state_daemon
from tests.fake.test_admission_visibility import Inline


@pytest.mark.parametrize('argv', [['watch'], ['keepalive'], ['reset', 'codex', '--policy']])
@pytest.mark.parametrize('as_json', [False, True])
def test_maintenance_before_recovery_is_rejected_without_queueing(state_daemon, monkeypatch, capsys, argv, as_json):
    service, _ = state_daemon
    assert not service.timers.started and not service._recovery_complete.is_set()
    class LocalClient:
        def call(self, op, args):
            return service.dispatch(op, args)
    monkeypatch.setattr(cli, '_client', lambda args: LocalClient())
    before = service.store.list_events()
    assert cli.main([*argv, *(['--json'] if as_json else [])]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'recovering' and 'retry' in result['fix'].lower()
    assert not service.timers._running
    assert service.store.list_events() == before
    assert not service.store.query('SELECT * FROM actions')


@pytest.mark.parametrize('argv', [['watch'], ['keepalive'], ['reset', 'codex', '--policy']])
def test_maintenance_preview_stays_available_during_recovery(state_daemon, monkeypatch, capsys, argv):
    service, _ = state_daemon
    class LocalClient:
        def call(self, op, args):
            return service.dispatch(op, args)
    monkeypatch.setattr(cli, '_client', lambda args: LocalClient())
    before = service.store.list_events()
    assert cli.main([*argv, '--dry-run', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['dry_run'] is True
    assert service.store.list_events() == before
    assert not service.timers._running


def test_maintenance_is_accepted_only_after_startup_recovery(state_daemon, monkeypatch):
    service, _ = state_daemon
    assert service.timers.request('reset_credits')['status'] == 'recovering'
    calls = []
    monkeypatch.setattr(service.timers, 'reset_credits_cycle', lambda **kwargs: calls.append(kwargs))
    service.timers._cycles = Inline(service.timers._cycles)
    service._recover_then_start_timers()
    assert service._recovery_complete.is_set() and service.timers.started
    assert service.timers.request('reset_credits', target='codex-1')['status'] == 'scheduled'
    assert calls == [{'target': 'codex-1'}]


def _drive(service, monkeypatch, ticks, outcomes):
    """Run the control loop once per instant in `ticks`; the instants retention ran at.

    `outcomes` is what each pass does, in order ('ok', 'deadline', 'cancelled' or
    'raise'); a pass past the end of it runs to its end.
    """
    service.workers = Inline(getattr(service.workers, 'real', service.workers))
    service._recovery_complete.set()
    service.stopping.clear()
    service._worker_failures.clear()
    service._worker_retry_at.clear()
    monkeypatch.setattr(service, '_admit', lambda: None)
    monkeypatch.setattr(service.timers, 'tick', lambda: None)
    clock, later, script, calls = [ticks[0]], iter(ticks[1:]), iter(outcomes), []
    monkeypatch.setattr(daemon_module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(daemon_module, 'after', lambda seconds: f'due:{clock[0] + seconds:g}')
    service._last_maintenance = 0

    def maintenance(*args, **kwargs):
        calls.append(clock[0])
        outcome = next(script, 'ok')
        if outcome == 'raise':
            raise OSError('transient filesystem failure')
        if outcome in ('deadline', 'cancelled'):
            return {'interrupted': outcome, 'pruned': [], 'jobs_after': 3}
        return {}
    monkeypatch.setattr(daemon_module, 'maintenance', maintenance)

    def next_tick(_):
        value = next(later, None)
        if value is None:
            service.stopping.set()
        else:
            clock[0] = value
    monkeypatch.setattr(service.stopping, 'wait', next_tick)
    service._control()
    return calls


def test_retention_failure_is_reoffered_after_backoff_not_after_an_hour(state_daemon, monkeypatch):
    """C-5.10: a retention pass that raised is tried again on the worker retry clock."""
    service, _ = state_daemon
    seen = []
    real_mark = service.timers.mark
    def mark(name, **fields):
        real_mark(name, **fields)
        seen.append(dict(service.timers.status()['retention']))
    monkeypatch.setattr(service.timers, 'mark', mark)
    calls = _drive(service, monkeypatch, [7200.0, 7200.4, 7200.5, 7201.0], ['raise'])
    assert calls == [7200.0, 7200.5], 'retry respects backoff; successful retention rearms the hourly interval'
    assert seen[0]['next_due'] == 'due:7200.5' and seen[0]['last_error_type'] == 'OSError'
    assert service._last_maintenance == 7200.5
    assert service.timers.status()['retention']['next_due'] == 'due:10800.5'
    assert service.timers.status()['retention']['last_error_type'] is None
    assert 'retention' not in service._worker_retry_at
    assert 'retention' not in service._worker_failures


def test_a_retention_pass_that_reaches_its_deadline_is_due_again_in_an_hour(state_daemon, monkeypatch):
    """C-8.4: a pass that ran out of time is not retried on C-5.10's clock.

    The next pass sizes every job again from the first, so one offered 60 s later
    meets the same deadline. Incident 2026-10-02: retried on that clock, 128 passes
    in a row reached the deadline and the walk held a worker half of the time.
    """
    service, _ = state_daemon
    warnings = []
    monkeypatch.setattr(service.log, 'warning', lambda text, *args: warnings.append(text % args))
    ticks = [7200.0, 7200.05, 7200.5, 7201.0, 7260.0, 7261.0, 7320.5, 10799.95, 10800.0, 10800.05]
    calls = _drive(service, monkeypatch, ticks, ['deadline', 'deadline'])
    assert calls == [7200.0, 10800.0]
    status = service.timers.status()['retention']
    assert status['last_error_type'] == 'TimeoutError' and status['next_due'] == 'due:14400'
    assert service._last_maintenance == 10800.0
    assert 'retention' not in service._worker_retry_at and 'retention' not in service._worker_failures
    assert len(warnings) == 2 and all('deadline' in line and '3 jobs kept' in line for line in warnings)


_gaps = st.one_of(st.floats(min_value=.05, max_value=2), st.floats(min_value=10, max_value=120),
                  st.floats(min_value=600, max_value=4000))


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(gaps=st.lists(_gaps, min_size=1, max_size=60),
       outcomes=st.lists(st.sampled_from(['ok', 'deadline', 'raise']), max_size=40))
def test_retention_runs_exactly_when_its_two_clocks_allow(state_daemon, monkeypatch, gaps, outcomes):
    """C-8.4, C-5.10: for every sequence of outcomes and ticks, a pass starts at the
    first tick an hour after one that ran to its end or its deadline, or the worker
    retry delay after one that raised, and at no other tick."""
    service, _ = state_daemon
    ticks = [7200.0]
    for gap in gaps:
        ticks.append(ticks[-1] + gap)
    calls = _drive(service, monkeypatch, ticks, outcomes)

    expected, script, ended, retry_at, failures = [], iter(outcomes), 0.0, 0.0, 0
    for instant in ticks:
        if instant - ended < daemon_module.RETENTION_INTERVAL_S or instant < retry_at:
            continue
        expected.append(instant)
        if next(script, 'ok') == 'raise':
            failures += 1
            retry_at = instant + daemon_module.worker_retry_delay(failures)
        else:
            ended, retry_at, failures = instant, 0.0, 0
    assert calls == expected
    # The bound itself, stated without the model: a pass that did not raise is
    # followed by none for an hour, whatever it found.
    results = (outcomes + ['ok'] * len(calls))[:len(calls)]
    for (began, result), following in zip(zip(calls, results), calls[1:]):
        if result != 'raise':
            assert following - began >= daemon_module.RETENTION_INTERVAL_S
        else:
            assert following - began >= daemon_module.WORKER_RETRY_BASE_S
