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


class _Clock:
    """Stands in for the `time` module inside `subfleet.daemon` only."""

    def __init__(self, now):
        self.now = now

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        import time
        return getattr(time, name)


#: What a pass does, and how long it takes. `deadline` ran out of time while still
#: sizing; `pruning-deadline` ran out of time with a prune in hand.
_OUTCOMES = {'ok': 5.0, 'deadline': 60.0, 'pruning-deadline': 60.0, 'cancelled': 1.0, 'raise': 0.25}


def _drive(service, monkeypatch, start, gaps, outcomes):
    """Run the control loop at `start` and again `gap` seconds after each
    iteration ends; the (began, ended, outcome) of every retention pass.

    `outcomes` is what each pass does, in order; a pass past the end of it runs
    to its end. A pass takes time, so "after it ended" and "after it began" differ.
    """
    service.workers = Inline(getattr(service.workers, 'real', service.workers))
    service._recovery_complete.set()
    service.stopping.clear()
    service._worker_failures.clear()
    service._worker_retry_at.clear()
    monkeypatch.setattr(service, '_admit', lambda: None)
    monkeypatch.setattr(service.timers, 'tick', lambda: None)
    clock, later, script, passes = _Clock(start), iter(gaps), iter(outcomes), []
    monkeypatch.setattr(daemon_module, 'time', clock)
    monkeypatch.setattr(daemon_module, 'after', lambda seconds: f'due:{clock.now + seconds:g}')
    service._last_maintenance = 0

    def maintenance(*args, **kwargs):
        began, outcome = clock.now, next(script, 'ok')
        clock.now += _OUTCOMES[outcome]
        passes.append((began, clock.now, outcome))
        if outcome == 'raise':
            raise OSError('transient filesystem failure')
        if outcome == 'cancelled':
            return {'interrupted': 'cancelled', 'pruned': [], 'jobs_after': 3, 'bytes_before': None}
        if outcome == 'deadline':
            return {'interrupted': 'deadline', 'pruned': [], 'jobs_after': 3, 'bytes_before': None}
        if outcome == 'pruning-deadline':
            return {'interrupted': 'deadline', 'pruned': ['job-1'], 'jobs_after': 2, 'bytes_before': 4096}
        return {}
    monkeypatch.setattr(daemon_module, 'maintenance', maintenance)

    def next_tick(_):
        gap = next(later, None)
        if gap is None:
            service.stopping.set()
        else:
            clock.now += gap
    monkeypatch.setattr(service.stopping, 'wait', next_tick)
    service._control()
    return passes


def _began(passes):
    return [began for began, _ended, _outcome in passes]


@pytest.mark.parametrize('failure', ['raise', 'pruning-deadline'])
def test_retention_failure_is_reoffered_after_backoff_not_after_an_hour(state_daemon, monkeypatch, failure):
    """C-5.10, C-8.4: a pass that raised, or ran out of time with a prune in hand,
    is tried again on the worker retry clock."""
    service, _ = state_daemon
    seen = []
    real_mark = service.timers.mark
    def mark(name, **fields):
        real_mark(name, **fields)
        seen.append(dict(service.timers.status()['retention']))
    monkeypatch.setattr(service.timers, 'mark', mark)
    took = _OUTCOMES[failure]
    passes = _drive(service, monkeypatch, 7200.0, [.25, .25, .5], [failure])
    # The failure is counted when the pass ends; the retry is 0.5 s after that.
    assert _began(passes) == [7200.0, 7200.0 + took + .5]
    assert seen[0]['next_due'] == f'due:{7200.0 + took + .5:g}'
    assert seen[0]['last_error_type'] == ('OSError' if failure == 'raise' else 'TimeoutError')
    ended = passes[1][1]
    assert service._last_maintenance == ended
    assert service.timers.status()['retention']['next_due'] == f'due:{ended + 3600:g}'
    assert service.timers.status()['retention']['last_error_type'] is None
    assert 'retention' not in service._worker_retry_at
    assert 'retention' not in service._worker_failures


def test_a_retention_pass_that_runs_out_of_time_sizing_is_due_again_in_an_hour(state_daemon, monkeypatch):
    """C-8.4: a pass that reached its deadline before it had sized every job is
    not retried on C-5.10's clock.

    The next pass sizes every job again from the first, so one offered 60 s later
    meets the same deadline. Incident 2026-10-02: retried on that clock, 128 passes
    in a row reached the deadline and the walk held a worker half of the time.
    """
    service, _ = state_daemon
    warnings = []
    monkeypatch.setattr(service.log, 'warning', lambda text, *args: warnings.append(text % args))
    # Ticks at 7200, then 7260.05 (the pass took 60 s), 7260.5, 7261, 7321, 7322,
    # 10859.95 (an hour less 50 ms after the pass ended), 10860, 10920.05.
    passes = _drive(service, monkeypatch, 7200.0, [.05, .45, .5, 60, 1, 3537.95, .05, .05],
                    ['deadline', 'deadline'])
    assert passes == [(7200.0, 7260.0, 'deadline'), (10860.0, 10920.0, 'deadline')]
    status = service.timers.status()['retention']
    assert status['last_error_type'] == 'TimeoutError' and status['next_due'] == 'due:14520'
    assert service._last_maintenance == 10920.0
    assert daemon_module.RETENTION_INTERVAL_S == 3600 and daemon_module.RETENTION_PASS_S == 60
    assert 'retention' not in service._worker_retry_at and 'retention' not in service._worker_failures
    assert len(warnings) == 2 and all('before it had sized' in line and "store's 3 jobs" in line for line in warnings)


def test_a_raise_after_a_sizing_deadline_starts_its_backoff_over(state_daemon, monkeypatch):
    """C-5.10: raise, sizing deadline, then a raise an hour later: the deadline pass
    cleared the count, so the second raise is retried 0.5 s after it, not 1 s."""
    service, _ = state_daemon
    passes = _drive(service, monkeypatch, 7200.0, [.5, 3600, .25, .25, .25, .25], ['raise', 'deadline', 'raise'])
    assert [outcome for *_times, outcome in passes] == ['raise', 'deadline', 'raise', 'ok']
    assert passes[1][0] == passes[0][1] + .5                   # the first raise: 0.5 s after it ended
    assert passes[2][0] == passes[1][1] + 3600                 # the deadline pass: the hour
    assert passes[3][0] == passes[2][1] + .5                   # the count started over
    assert 'retention' not in service._worker_failures


def test_a_sizing_deadline_whose_bookkeeping_raises_is_retried(state_daemon, monkeypatch):
    """C-5.10, C-8.4: the hour is rearmed last, as after a completed pass, so a
    pass whose status could not be recorded stays due and is retried."""
    service, _ = state_daemon
    real_mark, calls = service.timers.mark, []
    def mark(name, **fields):
        calls.append(fields.get('error'))
        if len(calls) == 1:
            raise OSError('disk full')
        real_mark(name, **fields)
    monkeypatch.setattr(service.timers, 'mark', mark)
    passes = _drive(service, monkeypatch, 7200.0, [.25, .25, .25], ['deadline'])
    assert _began(passes) == [7200.0, 7260.5]
    assert calls[:2] == ['TimeoutError', 'OSError']


def test_old_service_notices_go_whatever_becomes_of_the_pass(state_daemon, monkeypatch):
    """C-8.4: the 14-day delete used to follow a completed pass only."""
    service, _ = state_daemon
    with service.store.transaction('fixture.notices') as tx:
        for state, created in (('acknowledged', '2026-01-01T00:00:00Z'), ('surfaced', '2026-01-01T00:00:00Z'),
                               ('pending', '2026-01-01T00:00:00Z'), ('acknowledged', '2999-01-01T00:00:00Z')):
            tx.execute("INSERT INTO service_notices(session_id,text,state,created_at) VALUES('session','t',?,?)",
                       (state, created))
    monkeypatch.setattr(daemon_module, 'maintenance',
                        lambda *a, **k: {'interrupted': 'deadline', 'pruned': [], 'jobs_after': 3, 'bytes_before': None})
    service._retention()
    left = sorted((row['state'], row['created_at'][:4]) for row in service.store.query('SELECT * FROM service_notices'))
    assert left == [('acknowledged', '2999'), ('pending', '2026')]


_gaps = st.one_of(st.floats(min_value=.05, max_value=2), st.floats(min_value=10, max_value=120),
                  st.floats(min_value=600, max_value=4000))


@settings(max_examples=80, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(gaps=st.lists(_gaps, min_size=1, max_size=60),
       outcomes=st.lists(st.sampled_from(sorted(_OUTCOMES)), max_size=40))
def test_retention_runs_exactly_when_its_two_clocks_allow(state_daemon, monkeypatch, gaps, outcomes):
    """C-8.4, C-5.10: for every sequence of outcomes and ticks, a pass starts at the
    first tick an hour after one ended that ran to its end, was cancelled, or ran out
    of time sizing, or the worker retry delay after one ended that raised or ran out
    of time pruning, and at no other tick."""
    service, _ = state_daemon
    passes = _drive(service, monkeypatch, 7200.0, gaps, outcomes)

    # The reference: one clock from the end of the last pass that did not fail,
    # one from the end of the last that did.
    expected, script, now, ended, retry_at, failures = [], iter(outcomes), 7200.0, 0.0, 0.0, 0
    for gap in [None, *gaps]:
        now += gap or 0
        if now - ended < 3600 or now < retry_at:
            continue
        outcome = next(script, 'ok')
        began, now = now, now + _OUTCOMES[outcome]
        expected.append((began, now, outcome))
        if outcome in ('raise', 'pruning-deadline'):
            failures += 1
            retry_at = now + min(60, .5 * 2 ** (failures - 1))
        else:
            ended, retry_at, failures = now, 0.0, 0
    assert passes == expected
    # The bound itself, stated without the model: a pass that did not fail is
    # followed by none for an hour from when it ended, whatever it found.
    for (_began_at, ended_at, outcome), (next_began, *_rest) in zip(passes, passes[1:]):
        if outcome in ('raise', 'pruning-deadline'):
            assert next_began - ended_at >= .5
        else:
            assert next_began - ended_at >= 3600


def test_retention_gets_both_budgets_from_policy_and_the_conversation_services_pins(state_daemon, monkeypatch):
    """C-8.4, C-26.12, IR-17: the hourly pass hands retention the detached and turn
    budgets of the `retention` policy section and the conversation service's pins."""
    service, _ = state_daemon
    service.policy = {**service.policy, 'retention': {**service.policy['retention'], 'jobs': 7, 'turn_jobs': 9,
                                                      'turn_bytes': 1234, 'turn_keep_days': 2}}
    seen = {}
    def maintenance(store, root, **kwargs):
        seen.update(kwargs)
        return {}
    monkeypatch.setattr(daemon_module, 'maintenance', maintenance)
    service._retention()
    assert (seen['max_jobs'], seen['max_bytes']) == (7, 2 * 1024 ** 3)
    assert (seen['turn_max_jobs'], seen['turn_max_bytes'], seen['turn_keep_s']) == (9, 1234, 2 * 86400)
    assert seen['pins'] == service.conversations.retention_pins
