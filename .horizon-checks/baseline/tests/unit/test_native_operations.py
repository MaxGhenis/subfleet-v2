"""Native operator requests use cached evidence and the existing reset policy."""
import json
import threading
from types import SimpleNamespace

import pytest

from subfleet import operations, protocol
from tests.unit.test_timers_probe import rig, events
from tests.unit.test_timers_reset_credits import store, lane, snapshot, component, HTTP, NOW


def test_targeted_dry_run_does_not_read_credit_endpoint_or_write_actions(store, tmp_path):
    first = lane(store, tmp_path)
    second = lane(store, tmp_path, number=2)
    http = HTTP(store=store)
    before = store.list_events()
    result = component(store, http).evaluate(snapshot(store), now=NOW, target_lane_id=second.lane_id, dry_run=True)
    assert result['status'] == 'would-evaluate' and result['candidate_lanes'] == [second.lane_id]
    assert result['credit_verified'] is False
    assert http.calls == [] and store.query('SELECT * FROM actions') == []
    assert store.list_events() == before


def test_targeted_reset_spends_only_named_lane_and_retains_interval(store, tmp_path):
    first = lane(store, tmp_path)
    second = lane(store, tmp_path, number=2)
    http = HTTP(store=store)
    resets = component(store, http)
    result = resets.evaluate(snapshot(store), now=NOW, target_lane_id=second.lane_id)
    assert result['status'] == 'confirmed' and result['lane_id'] == second.lane_id
    assert store.list_closures(first.lane_id, active_at=NOW.isoformat())
    again = resets.evaluate(snapshot(store), now=NOW, target_lane_id=first.lane_id)
    assert again['status'] == 'interval-blocked'
    assert sum(request.get_method() == 'POST' for request, _ in http.calls) == 1


@pytest.mark.parametrize('mode', ['healthy-other-lane', 'disabled-policy', 'unknown-target'])
def test_target_does_not_bypass_fleet_trigger_or_enable_background_policy(store, tmp_path, mode):
    target = lane(store, tmp_path)
    if mode == 'healthy-other-lane':
        lane(store, tmp_path, number=2, utilization=.1)
    http = HTTP(store=store)
    resets = component(store, http, enabled=mode != 'disabled-policy')
    result = resets.evaluate(snapshot(store), now=NOW,
                             target_lane_id='missing' if mode == 'unknown-target' else target.lane_id)
    assert result['status'] in ('not-triggered', 'disabled', 'no-concrete-credit')
    assert not http.calls and not store.query('SELECT * FROM actions')


def test_target_does_not_bypass_unshadowed_gift_priority(store, tmp_path):
    target = lane(store, tmp_path)
    other = lane(store, tmp_path, number=2)
    http = HTTP(store=store)
    result = component(store, http).evaluate(snapshot(store, **{target.lane_id: {'app_shadowed': True}}),
                                             now=NOW, target_lane_id=target.lane_id)
    assert result['status'] == 'shadow-excluded'
    assert http.calls and all(request.get_method() == 'GET' for request, _ in http.calls)
    assert not store.query('SELECT * FROM actions')


@pytest.mark.parametrize('command', ['brief', 'watch', 'keepalive', 'reset'])
def test_read_and_preview_commands_never_refresh_desktop_or_execute(rig, monkeypatch, command):
    timer, store, clock, adapter, enroll = rig
    enroll()
    def forbidden(*args, **kwargs):
        raise AssertionError('read/preview attempted an effect')
    monkeypatch.setattr(operations, 'brief', lambda view: 'cached brief')
    service = SimpleNamespace(store=store, timers=timer, _desktop_identity=forbidden, _cached_desktop_identity=lambda: None,
                              _capacity_view=lambda desktop: timer.snapshot())
    before = store.list_events()
    result = operations.dispatch(service, protocol.OperationsArgs(command, dry_run=True))
    assert result and not adapter.calls
    assert store.list_events() == before and not store.query('SELECT * FROM actions')


def test_manual_timer_request_coalesces_with_probe_and_observes_shutdown(rig, monkeypatch):
    timer, store, clock, adapter, enroll = rig
    timer.start()
    entered, release = threading.Event(), threading.Event()
    def reset(*, target):
        assert target == 'codex-2'
        entered.set()
        assert release.wait(3)
    monkeypatch.setattr(timer, 'reset_credits_cycle', reset)
    try:
        assert timer.request('reset_credits', target='codex-2')['status'] == 'scheduled'
        assert entered.wait(3)
        assert timer.request('reset_credits', target='codex-3')['status'] == 'already-running'
        assert timer.request('probe')['status'] == 'already-running'
        assert events(store, 'timer.requested') == [{'timer': 'reset_credits', 'target': 'codex-2'}]
        timer.cancel.set()
        assert timer.request('keepalive')['status'] == 'stopping'
    finally:
        release.set()


@pytest.mark.parametrize('hours', [float('inf'), float('nan'), -1, 0, True, 1e100])
def test_errors_rejects_invalid_intervals(store, hours):
    with pytest.raises(protocol.ProtocolError):
        operations.error_report(store.query, hours, now=NOW)


def test_error_report_queries_actual_schema_and_keeps_probe_diagnostics(store, tmp_path):
    target = lane(store, tmp_path)
    store.add_event('timer.verdict', lane_id=target.lane_id, data={'status': 'unknown', 'error_type': 'TimeoutError'})
    report = operations.error_report(store.query, 24)
    assert report['probes'][0]['error_type'] == 'TimeoutError'
    assert report['attempts'] == []


def test_brief_preserves_unmeasured_and_offline_labels():
    view = {'offline': True, 'lanes': [{'lane_id': 'claude-1', 'provider': 'claude', 'owner': 'v2',
                                      'identity_status': 'unverified'}], 'readings': []}
    text = operations.brief(view)
    assert 'daemon offline' in text and 'usage unknown' in text and 'identity unverified' in text
    assert '100%' not in text and 'dispatchable' not in text


@pytest.mark.parametrize('mode', ['enabled', 'desktop', 'owner', 'lease', 'ready'])
def test_login_guidance_never_authenticates_or_changes_lane(rig, mode):
    timer, store, clock, adapter, enroll = rig
    target = enroll(enabled=mode == 'enabled')
    if mode == 'desktop':
        store.update_lane(target.lane_id, desktop=1)
    elif mode == 'owner':
        store.update_lane(target.lane_id, owner='v1')
    elif mode == 'lease':
        store.acquire_lease(f'lane:{target.lane_id}:slot:0', 'busy')
    service = SimpleNamespace(store=store)
    before = store.list_events()
    if mode != 'ready':
        with pytest.raises(protocol.ProtocolError):
            operations.dispatch(service, protocol.OperationsArgs('login', target=target.lane_id))
    else:
        result = operations.dispatch(service, protocol.OperationsArgs('login', target=target.lane_id))
        assert result['status'] == 'manual-login-required'
        assert 'codex login' in result['login_command'] and 'subfleet lanes enroll' in result['enroll_command']
    assert store.list_events() == before and not adapter.calls
