"""Operator maintenance waits for recovery; failed retention follows its retry clock."""
import json

import pytest

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


@pytest.mark.parametrize('failure', ['exception', 'deadline'])
def test_retention_failure_is_reoffered_after_backoff_not_after_an_hour(state_daemon, monkeypatch, failure):
    service, _ = state_daemon
    service.workers = Inline(service.workers)
    service._recovery_complete.set()
    monkeypatch.setattr(service, '_admit', lambda: None)
    monkeypatch.setattr(service.timers, 'tick', lambda: None)
    clock = [7200.0]
    monkeypatch.setattr(daemon_module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(daemon_module, 'after', lambda seconds: f'due:{clock[0] + seconds:g}')
    service._last_maintenance = 0
    calls = []
    def maintenance(*args, **kwargs):
        calls.append(clock[0])
        if len(calls) == 1:
            if failure == 'deadline':
                return {'interrupted': 'deadline'}
            raise OSError('transient filesystem failure')
        return {}
    monkeypatch.setattr(daemon_module, 'maintenance', maintenance)
    turns = iter([7200.4, 7200.5, 7201.0])
    def next_tick(_):
        if clock[0] == 7200.0:
            status = service.timers.status()['retention']
            assert status['next_due'] == 'due:7200.5'
            assert status['last_error_type'] == ('TimeoutError' if failure == 'deadline' else 'OSError')
        value = next(turns, None)
        if value is None:
            service.stopping.set()
        else:
            clock[0] = value
    monkeypatch.setattr(service.stopping, 'wait', next_tick)
    service._control()
    assert calls == [7200.0, 7200.5], 'retry respects backoff; successful retention rearms the hourly interval'
    assert service._last_maintenance == 7200.5
    assert service.timers.status()['retention']['next_due'] == 'due:10800.5'
    assert service.timers.status()['retention']['last_error_type'] is None
    assert 'retention' not in service._worker_retry_at
    assert 'retention' not in service._worker_failures


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


def test_retention_catch_up_backs_off_while_a_pass_changes_nothing(state_daemon, monkeypatch):
    """Review of a9a6cbf4, N2: a pass that reports more work but changed nothing
    (a job it can never decide) kept the daemon running one every 5 seconds,
    with an event and log lines each time. The wait doubles while nothing
    changes, up to an hour, and is 5 seconds again once a pass makes progress."""
    service, _ = state_daemon
    clock = [7200.0]
    monkeypatch.setattr(daemon_module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(daemon_module, 'after', lambda seconds: f'due:{clock[0] + seconds:g}')
    results = iter([{'more': True, 'progressed': False}] * 3 + [{'more': True, 'progressed': True}]
                   + [{'more': True, 'progressed': False}] * 12 + [{}])
    monkeypatch.setattr(daemon_module, 'maintenance', lambda *args, **kwargs: next(results))
    waits = []
    for _ in range(17):
        service._retention()
        waits.append(3600 - (clock[0] - service._last_maintenance))
    assert waits[:4] == [10, 20, 40, 5]
    assert waits[4:8] == [10, 20, 40, 80]
    assert waits[14:16] == [daemon_module.RETENTION_CATCH_UP_MAX_S] * 2
    assert waits[16] == 3600                                   # a pass with nothing more: hourly again
    assert service._retention_catch_up_s == daemon_module.RETENTION_CATCH_UP_S
    assert service.timers.status()['retention']['next_due'] == 'due:10800'


def test_retention_log_lines_report_what_the_archives_added(state_daemon, monkeypatch, caplog):
    """Final review of e50716e8, N1: the daemon's retention lines name the
    bytes the archives added (bundles, manifests, rows) and the net on disk,
    beside what was freed and what was moved into the archive."""
    import logging
    service, _ = state_daemon
    freed = {'pruned': ['a', 'b'], 'freed_bytes': 9000, 'freed_disk_bytes': 8000, 'archived_bytes': 7000,
             'added_bytes': 3000}
    results = iter([{**freed, 'more': True, 'progressed': True, 'in_flight': [], 'deferred': {}}, freed])
    monkeypatch.setattr(daemon_module, 'maintenance', lambda *args, **kwargs: next(results))
    with caplog.at_level(logging.INFO, logger=service.log.name):
        service._retention()
        service._retention()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith('retention')]
    assert len(lines) == 2, lines
    for line in lines:
        assert 'freed 9000 bytes' in line and '8000 on disk' in line and 'moved 7000 bytes' in line
        assert 'added 3000 bytes' in line and 'net 5000 on disk' in line
