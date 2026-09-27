"""Disabled binding recovery must not rewrite history or outrun its process fence."""
from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest

from subfleet import protocol
from subfleet.adapters.registry import register
from subfleet.contracts import Closure, ClosureReason, ClockSource
from tests.fake.test_lanes_enroll import core, FakeClaude, login_dir, lanes_json


def disabled(core, tmp_path):
    daemon, _ = core
    home = login_dir(tmp_path, 'lane1@example.test')
    first = daemon.dispatch('lanes', {'action': 'enroll', 'credential': str(home)})['enrolled']
    daemon.store.update_lane(first['lane_id'], enabled=0)
    return daemon, home, daemon.store.one('SELECT * FROM lanes WHERE lane_id=?', (first['lane_id'],))


def test_reenrollment_preserves_binding_history_and_account_limits(core, tmp_path):
    daemon, home, old = disabled(core, tmp_path)
    daemon.store.add_closure(Closure(old['lane_id'], 'account', '2099-01-01T00:00:00Z',
                                    ClosureReason.OPERATOR_HOLD, ClockSource.REPORTED, 'operator'))
    before_readings = daemon.store.list_readings(old['lane_id'])
    row = daemon.dispatch('lanes', {'action': 'enroll', 'credential': str(home)})['enrolled']
    assert row['lane_id'] != old['lane_id'] and row['enabled']
    assert row['credential_epoch'] == old['credential_epoch'] + 1
    assert row['owner'] == old['owner'] and row['identity'] == old['identity']
    assert daemon.store.one('SELECT * FROM lanes WHERE lane_id=?', (old['lane_id'],)) == old
    assert daemon.store.list_readings(old['lane_id']) == before_readings
    assert daemon.store.list_closures(row['lane_id'])[0]['reason'] == 'operator-hold'
    assert {r['lane_id'] for r in lanes_json(core[1])} >= {old['lane_id'], row['lane_id']}
    assert not daemon.store.list_leases()


@pytest.mark.parametrize('mode', ['owner', 'desktop', 'lease', 'running', 'quarantined'])
def test_reenrollment_fences_live_or_foreign_bindings(core, tmp_path, mode):
    daemon, home, old = disabled(core, tmp_path)
    if mode in ('owner', 'desktop'):
        daemon.store.update_lane(old['lane_id'], **({'owner': 'v1'} if mode == 'owner' else {'desktop': 1}))
    elif mode == 'lease':
        daemon.store.acquire_lease(f"lane:{old['lane_id']}:slot:0", 'another-holder')
    else:
        daemon.store.add_job(job_id='j', request_id='r', payload_digest='digest', kind='dispatch',
                             workdir=str(tmp_path), prompt_path='/fake', sandbox='read-only')
        daemon.store.add_attempt(attempt_id='a', job_id='j', seq=1, lane_id=old['lane_id'],
                                 model_requested='haiku', state=mode)
    with pytest.raises(protocol.ProtocolError):
        daemon.dispatch('lanes', {'action': 'enroll', 'credential': str(home)})
    assert not daemon.store.get_lane('claude-2')


@pytest.mark.parametrize('change', ['account_key', 'identity', 'identity_status'])
def test_reenrollment_cannot_change_verified_account(core, tmp_path, change):
    daemon, home, old = disabled(core, tmp_path)
    class Changed(FakeClaude):
        def enroll(self, credential):
            info = super().enroll(credential)
            return replace(info, **{change: 'unverified' if change == 'identity_status' else 'different'})
    register('claude', Changed)
    with pytest.raises(protocol.ProtocolError):
        daemon.dispatch('lanes', {'action': 'enroll', 'credential': str(home)})
    assert not daemon.store.get_lane('claude-2') and not daemon.store.list_leases()
    assert daemon.store.get_lane(old['lane_id']).identity == old['identity']


def test_lease_remains_held_through_roster_publication(core, tmp_path, monkeypatch):
    daemon, home, old = disabled(core, tmp_path)
    original = daemon._append_lanes_json
    def publish(lane):
        leases = daemon.store.list_leases()
        assert leases and leases[0]['lease_key'] == f"lane:{old['lane_id']}:slot:0"
        assert leases[0]['holder'].startswith('probe:timer:enroll:')
        assert daemon.store.get_lane(lane.lane_id) is not None
        original(lane)
    monkeypatch.setattr(daemon, '_append_lanes_json', publish)
    daemon.dispatch('lanes', {'action': 'enroll', 'credential': str(home)})
    assert not daemon.store.list_leases()


@pytest.mark.parametrize('field,value', [('owner', 'v1'), ('desktop', 1), ('enabled', 1)])
def test_rechecks_binding_after_adapter_returns(core, tmp_path, field, value):
    daemon, home, old = disabled(core, tmp_path)
    class Changed(FakeClaude):
        def enroll(self, credential):
            daemon.store.update_lane(old['lane_id'], **{field: value})
            return super().enroll(credential)
    register('claude', Changed)
    with pytest.raises(protocol.ProtocolError, match='changed'):
        daemon.dispatch('lanes', {'action': 'enroll', 'credential': str(home)})
    assert not daemon.store.get_lane('claude-2') and not daemon.store.list_leases()


def test_restart_recovers_only_contained_enrollment_leases(core, tmp_path, monkeypatch):
    daemon, home, old = disabled(core, tmp_path)
    holder = 'probe:timer:enroll:interrupted'
    directory = tmp_path / 'interrupted'
    directory.mkdir()
    daemon.store.acquire_lease(f"lane:{old['lane_id']}:slot:0", holder)
    daemon.store.acquire_lease('lane:unrelated:slot:0', 'real-attempt')
    record = {'holder': holder, 'job_id': None, 'lane_id': old['lane_id'], 'timer_kind': 'enroll',
              'model_id': 'enrollment', 'directory': str(directory), 'state': 'quarantined',
              'created_at': '2026-09-01T00:00:00Z', 'owned_identities': {},
              'deadline_at': '2026-09-01T00:01:00Z'}
    daemon._save_probe(record)
    monkeypatch.setattr(daemon, '_await_probe', lambda record: (False, None))
    daemon._recover_probes()
    assert daemon.store.list_leases(holder)
    monkeypatch.setattr(daemon, '_await_probe', lambda record: (True, None))
    daemon._recover_probes()
    assert not daemon.store.list_leases(holder)
    assert daemon.store.list_leases('real-attempt')
    assert not daemon.store.list_jobs()
    assert daemon.store.get_lane(old['lane_id']).enabled is False


def test_startup_does_not_recover_an_active_enrollment_before_its_first_turn(core, tmp_path, monkeypatch):
    daemon, home, old = disabled(core, tmp_path)
    active, orphan = 'probe:timer:enroll:active', 'probe:timer:enroll:orphan'
    daemon.store.acquire_lease(f"lane:{old['lane_id']}:slot:0", active)
    daemon.store.acquire_lease('lane:old:slot:0', orphan)
    daemon.timers.active_holders.add(active)
    monkeypatch.setattr(daemon.timers, 'start', lambda: None)
    daemon._recover_then_start_timers()
    assert daemon.store.list_leases(active) and not daemon.store.list_leases(orphan)


def test_read_only_operator_and_picker_do_not_refresh_cold_desktop_profile(core, tmp_path, monkeypatch):
    daemon, home, old = disabled(core, tmp_path)
    monkeypatch.setattr('subfleet.capacity.read_desktop_account', lambda: 'desktop@example.test')
    def forbidden(*args, **kwargs):
        raise AssertionError('read-only operation fetched profile')
    monkeypatch.setattr(daemon, '_desktop_profile', forbidden)
    before = daemon.store.list_events()
    assert daemon.dispatch('operations', {'command': 'brief'})['text']
    assert 'ranked' in daemon.dispatch('pick', {'family': 'claude', 'model': 'haiku'})
    assert daemon.store.list_events() == before


def test_canonical_helper_preserves_suffix_and_retired_policy(core):
    daemon, _ = core
    current = daemon.policy['models']['opus']['id']
    for retired in ('fable', 'claude-fable-5', 'claude-fable-5-1'):       # retired onto opus (2026-09-27)
        assert daemon.dispatch('operations', {'command': 'canonical-model', 'target': retired + '[1m]'}) == {'model': current + '[1m]'}
    assert daemon.dispatch('operations', {'command': 'canonical-model', 'target': 'future-model[2m]'}) == {'model': 'future-model[2m]'}


def test_failed_reservation_clears_volatile_enrollment_holder(core, tmp_path, monkeypatch):
    daemon, home, old = disabled(core, tmp_path)
    def fail(*args, **kwargs):
        raise OSError('injected lease failure')
    monkeypatch.setattr(daemon.store, 'acquire_lease', fail)
    with pytest.raises(OSError):
        daemon.dispatch('lanes', {'action': 'enroll', 'credential': str(home)})
    assert not daemon.timers.active_holders and not daemon.store.list_leases()


def test_provider_enrollment_does_not_hold_the_submission_lock(core, tmp_path):
    daemon, home, old = disabled(core, tmp_path)
    class CheckLock(FakeClaude):
        def enroll(self, credential):
            assert daemon._submit_lock.acquire(blocking=False), 'provider validation stalled unrelated submissions'
            daemon._submit_lock.release()
            return super().enroll(credential)
    register('claude', CheckLock)
    daemon.dispatch('lanes', {'action': 'enroll', 'credential': str(home)})
