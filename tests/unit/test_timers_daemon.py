"""Timer migration and integration seams (C-3, C-18.1)."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from subfleet.contracts import Credential, Lane, LaneOwner, Reading, ReadingLabel
from subfleet.store import SCHEMA_VERSION, Store
from subfleet.timers import Timers, iso


def test_existing_store_adds_jobless_notice_schema_without_losing_history(tmp_path):
    """C-3.1 C-3.2 C-18.1: schema v2 adds timer notice rows and retains original events."""
    path = tmp_path / 'state.sqlite3'
    with Store(path) as store:
        marker = store.add_event('imported.history', data={'value': 17})
        with store.transaction() as tx:
            tx.execute('DROP TABLE service_notices')
            tx.execute('UPDATE schema_version SET version=1')
    with Store(path) as store:
        assert store.one('SELECT MAX(version) version FROM schema_version')['version'] == SCHEMA_VERSION
        assert store.one('SELECT data_json FROM events WHERE event_id=?', (marker,))
        assert store.query('SELECT * FROM service_notices') == []


def test_foreign_usage_never_releases_original_accounts_closure_or_credit_count(tmp_path):
    """C-1.3 C-9.6 C-23.45: a changed home identity cannot become the bound account's usage."""
    policy = json.loads(Path('subfleet/default_policy.json').read_text())
    policy['reset_credits']['enabled'] = False
    now = datetime.now(timezone.utc)
    with Store(tmp_path / 'state.sqlite3') as store:
        lane = Lane('codex-1', 'codex', 'codex:original', Credential('codex', str(tmp_path), 'home'), str(tmp_path), LaneOwner.V2, False)
        store.put_lane(lane)
        timer = Timers(store, tmp_path, policy, now=lambda: now)
        try:
            timer._persist(lane, {'status':'ok', 'account_key':'codex:foreign', 'limit_reached':False,
                'checked_at':iso(now), 'reset_credits':{'available':10},
                'readings':(Reading(lane.lane_id,'account','seven_day',.01,iso(now+timedelta(days=5)), ReadingLabel.PROVIDER,'wham',iso(now)),)})
            view = timer.snapshot()['lanes'][0]
            assert not store.get_lane(lane.lane_id).enabled
            assert view['account_key'] == 'codex:original' and view['identity_status'] == 'mismatch'
            assert view['reset_credits_remaining'] is None
            assert not any(r['utilization'] is not None for r in store.list_readings(lane.lane_id))
        finally:
            timer.stop()


def test_revoked_home_is_excluded_from_the_real_scheduler_without_disabling_epoch_reprobe(tmp_path):
    """C-23.47 C-18.1: revoked-home latches block routing while preserving epoch-based re-probe."""
    from subfleet.daemon import Daemon
    policy = json.loads(Path('subfleet/default_policy.json').read_text())
    with Store(tmp_path / 'state.sqlite3') as store:
        lane = Lane('codex-1', 'codex', 'codex:one', Credential('codex', str(tmp_path), 'home'), str(tmp_path), LaneOwner.V2, False)
        store.put_lane(lane)
        timer = Timers(store, tmp_path, policy)
        daemon = Daemon.__new__(Daemon)
        daemon.store, daemon.policy, daemon.timers = store, policy, timer
        daemon.policy_digest = "test-policy"
        daemon._probe_record = lambda _: None
        timer.metadata[lane.lane_id] = {'probe_status':'revoked', 'verdict':'auth-revoked', 'revoked_epoch':'old'}
        try:
            decision = daemon._pick({'pinned_lane':lane.lane_id, 'pinned_model':'astra'})
            assert decision.chosen_lane is None
            assert store.get_lane(lane.lane_id).enabled
        finally:
            timer.stop()


def test_reenrolment_clears_the_homes_old_auth_condition_once(tmp_path):
    """C-1.3 C-23.44 C-23.52: new lane binding supersedes old auth-dead history for recovery."""
    policy = json.loads(Path('subfleet/default_policy.json').read_text())
    with Store(tmp_path / 'state.sqlite3') as store:
        old = Lane('codex-1', 'codex', 'codex:old', Credential('codex', str(tmp_path), 'home'), str(tmp_path), LaneOwner.V2, False, False)
        store.put_lane(old)
        notices = []
        timer = Timers(store, tmp_path, policy, deliver=notices.append)
        timer.record_auth_dead(old.lane_id)
        try:
            timer.alerts.evaluate(timer.snapshot())
            new = Lane('codex-2', 'codex', 'codex:new', Credential('codex', str(tmp_path), 'home', 2), str(tmp_path), LaneOwner.V2, False)
            store.put_lane(new)
            timer.metadata[new.lane_id] = {'verdict':'ok', 'probe_status':'ok'}
            view = timer.snapshot()
            assert next(row for row in view['lanes'] if row['lane_id'] == old.lane_id)['superseded_by'] == new.lane_id
            timer.alerts.evaluate(view)
            timer.alerts.evaluate(view)
            recovered = [notice for notice in notices if notice.get('recovery') and notice['home'] == str(tmp_path)]
            assert len(recovered) == 1
        finally:
            timer.stop()
