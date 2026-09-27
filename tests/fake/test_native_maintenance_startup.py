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


def test_the_hourly_pass_prunes_attachments(state_daemon, tmp_path, monkeypatch):
    """C-28.2: the hourly pass deletes, from the conversation store, an attachment
    no message needs that was last used 30 days ago, then its copy; one used since
    stays."""
    from datetime import UTC, datetime, timedelta
    from subfleet.conversations import attachments
    service, _ = state_daemon
    store = service.conversations.store
    shas = []
    for k in range(2):
        image = tmp_path / f"{k}.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([k]) * 32)
        shas.append(attachments.add(store, str(image))["sha256"])
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with store.transaction() as tx:
        tx.execute("UPDATE attachments SET last_used_at=? WHERE sha256=?", (old, shas[0]))
    service._retention()
    assert store.attachment(shas[0]) is None and not (store.root / "attachments" / f"{shas[0]}.png").exists()
    assert store.attachment(shas[1]) is not None and (store.root / "attachments" / f"{shas[1]}.png").exists()
    assert service.timers.status()['retention']['last_error_type'] is None


def test_the_attachment_pass_follows_the_job_pass_with_its_cancel_and_deadline(state_daemon, monkeypatch):
    """C-28.2, C-16.4: the attachment pass runs after the job pass, with the timers'
    cancel and a deadline of its own; a cancelled attachment pass leaves the hourly
    pass cancelled, as a cancelled job pass does."""
    import time
    service, _ = state_daemon
    calls = []
    monkeypatch.setattr(daemon_module, 'maintenance', lambda store, root, **kwargs: calls.append('jobs') or {})
    seen = {}
    def prune(store, **kwargs):
        calls.append('attachments')
        seen.update(store=store, **kwargs)
        return {"deleted": [], "bytes": 0, "strays": [], "errors": [], "interrupted": "cancelled"}
    monkeypatch.setattr(daemon_module, 'prune_attachments', prune)
    before = time.monotonic()
    service._retention()
    assert calls == ['jobs', 'attachments']
    assert seen['store'] is service.conversations.store
    assert seen['cancel'] is service.timers.cancel and seen['deadline'] >= before + 60
    assert service.timers.status()['retention']['last_error_type'] == 'CancelledError'


def test_a_failed_attachment_pass_is_recorded_and_the_hourly_pass_goes_on(state_daemon, monkeypatch):
    """C-28.2 (review of 9ac51ef, finding 3): an attachment pass that raises (here a
    corrupt message row, which the needed set cannot be read past) deletes nothing,
    is logged and recorded, and the hourly pass still cleans the notices and re-arms
    in an hour, rather than re-running the job pass at the worker's backoff."""
    import uuid
    service, _ = state_daemon
    store = service.conversations.store
    settings = {"model": "opus", "effort": "high", "fast": False, "permission": "ask"}
    cid = store.create_conversation(provider="claude", workspace="/w", workspace_kind="in-place",
                                    settings=settings, origin="new")[0]["conversation_id"]
    store.submit_message(conversation_id=cid, message_id=str(uuid.uuid4()), after_message_id=None, text="x",
                         attachments=[], settings=settings)
    with store.transaction() as tx:
        tx.execute("UPDATE messages SET attachments_json='[broken'")
    with service.store.transaction("test") as tx:
        tx.execute("INSERT INTO service_notices(session_id,text,state,created_at) "
                   "VALUES('s','old','acknowledged','2020-01-01T00:00:00Z')")
    monkeypatch.setattr(daemon_module, 'after', lambda seconds: f'due:{seconds:g}')
    service._retention()
    assert not service.store.query("SELECT 1 FROM service_notices WHERE text='old'")
    status = service.timers.status()['retention']
    assert status['last_error_type'] == 'OperationalError' and status['next_due'] == 'due:3600'


def test_attachments_are_pruned_when_the_job_pass_stops_at_its_deadline(state_daemon, monkeypatch, caplog):
    """C-28.2 (review of 9ac51ef, finding 4): a job pass that stops at its deadline
    still raises for the worker's retry, but the attachment pass runs first, so a
    job pass that always stops never keeps attachments from being pruned; an
    attachment pass that stops at its own deadline says so."""
    import logging
    service, _ = state_daemon
    monkeypatch.setattr(daemon_module, 'maintenance', lambda store, root, **kwargs: {'interrupted': 'deadline'})
    calls = []
    def prune(store, **kwargs):
        calls.append('attachments')
        return {"deleted": [], "bytes": 0, "strays": [], "errors": [], "interrupted": "deadline"}
    monkeypatch.setattr(daemon_module, 'prune_attachments', prune)
    with caplog.at_level(logging.INFO), pytest.raises(TimeoutError):
        service._retention()
    assert calls == ['attachments']
    assert any("attachment retention stopped at its deadline" in record.getMessage() for record in caplog.records)
