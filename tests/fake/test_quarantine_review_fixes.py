"""PR #131: identity unions, durable child identity, parsing, and bounded audit."""
import json

import pytest

from subfleet import procs
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.fake.test_review_pr131_probes import NEXT_BOOT, confirm_dead, ident, quarantine as review_quarantine, script_table
from tests.fake.test_quarantine_self_resolve import Clock, assert_released


def quarantine(daemon, harness, **kwargs):
    a = review_quarantine(daemon, harness, **kwargs)
    daemon.store.acquire_lease(f"out:{harness.root / 'held-output.md'}", a['job_id'])
    return a


@pytest.mark.parametrize('start', ['owned-start', 'held-start', 'guardian-start'])
def test_any_recorded_identity_for_the_same_pid_keeps_its_writers(state_daemon, monkeypatch, start):
    daemon, harness = state_daemon
    a = quarantine(daemon, harness, owned={'100': ident(100, 'owned-start')},
                   held={'100': ident(100, 'held-start')})
    # Escaped leader's child has no marker and a different session.
    script_table(monkeypatch, {100: (1, 100, 'Ss', start), 301: (100, 301, 'S', 'child')})
    census = daemon._contain(a)
    assert {100, 301} <= census.live_pids, census.to_dict()
    before = daemon.store.list_leases()
    assert confirm_dead(daemon, a) == 'quarantined'
    assert daemon.store.list_leases() == before


def test_rechecks_preserve_all_observed_identities_per_pid(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness, held={'300': ident(300, 'first')})
    marker = f'SUBFLEET_ATTEMPT={a["attempt_id"]} SUBFLEET_ROOT={daemon.root}'
    script_table(monkeypatch, {300: (1, 300, 'Ss', 'second')}, markers=f'300 writer {marker}\n')
    clock.advance()
    daemon._recheck_quarantines()
    a = daemon.store.get_attempt(a['attempt_id'])
    reason = json.loads(a['quarantine_reason'])
    assert {i['proc_start'] for i in reason['identity_history']['300']} == {'first', 'second'}
    # Every retained observation remains usable without a marker or parent link.
    script_table(monkeypatch, {300: (1, 300, 'Ss', 'first')})
    clock.advance()
    daemon._recheck_quarantines()
    assert daemon.store.get_attempt(a['attempt_id'])['state'] == 'quarantined'


def test_owned_identity_recording_retains_older_observations(state_daemon):
    daemon, harness = state_daemon
    a = quarantine(daemon, harness, owned={'300': ident(300, 'first')})
    table = procs.ProcessTable({100: (1, 100, 'Ss', 'guardian-start'),
                                300: (100, 100, 'S', 'second')}, boot_id=ident(100, '')['boot_id'])
    daemon._record_owned(a, table)
    evidence = json.loads(daemon.store.get_attempt(a['attempt_id'])['evidence_json'])
    assert {i['proc_start'] for i in evidence['owned_identity_history']['300']} == {'first', 'second'}


def test_a_recycled_descendant_pid_is_not_removed_from_an_owned_writers_walk(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    a = quarantine(daemon, harness, held={'300': ident(300, 'writer'), '400': ident(400, 'old')})
    script_table(monkeypatch, {300: (1, 300, 'Ss', 'writer'), 400: (300, 400, 'S', 'new')})
    assert {300, 400} <= daemon._contain(a).live_pids
    assert confirm_dead(daemon, a) == 'quarantined'
    assert daemon.store.list_leases()


@pytest.mark.parametrize('operator', [False, True])
def test_recycled_group_member_keeps_every_lease_in_both_resolvers(state_daemon, monkeypatch, operator):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness, owned={'200': ident(200, 'old')})
    for key, holder in ((f'worktree:{harness.workdir}', a['job_id']),
                        ('native:review', a['job_id']), ('native-session:review', a['attempt_id']),
                        ('conversation:review', a['job_id'])):
        daemon.store.acquire_lease(key, holder)
    before = daemon.store.list_leases()
    script_table(monkeypatch, {200: (1, 100, 'S', 'new')})
    if operator:
        assert confirm_dead(daemon, a) == 'quarantined'
    else:
        clock.advance()
        daemon._recheck_quarantines()
    assert daemon.store.get_attempt(a['attempt_id'])['state'] == 'quarantined'
    assert daemon.store.list_leases() == before


def test_corrupt_owned_evidence_cannot_establish_death(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    a = quarantine(daemon, harness)
    daemon.store.update_attempt(a['attempt_id'], evidence_json='{broken', quarantine_recheck_at='')
    script_table(monkeypatch, {}, boot=NEXT_BOOT)
    daemon._recheck_quarantines()
    assert daemon.store.get_attempt(a['attempt_id'])['state'] == 'quarantined'
    assert daemon.store.list_leases()


@pytest.mark.parametrize('boots', [None, 'old-boot', ['old-boot', None], {'boot': 'old-boot'}])
@pytest.mark.parametrize('operator', [False, True])
def test_corrupt_diagnostic_boot_observations_do_not_pin_an_empty_census(
        state_daemon, monkeypatch, boots, operator):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness, held={'300': ident(300, 'writer')})
    reason = json.loads(a['quarantine_reason'])
    reason['lineage_boot_ids'] = boots
    daemon.store.update_attempt(a['attempt_id'], quarantine_reason=json.dumps(reason),
                                evidence_json=json.dumps({'lineage_boot_ids': boots}))
    a = daemon.store.get_attempt(a['attempt_id'])
    script_table(monkeypatch, {})
    clock.advance()
    if operator:
        assert confirm_dead(daemon, a) == 'lost'
    else:
        daemon._recheck_quarantines()
    assert_released(daemon, a)


def test_legacy_child_without_identity_still_holds_conservatively(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    a = quarantine(daemon, harness)
    daemon.store.update_attempt(a['attempt_id'], child_pid=500)
    a = daemon.store.get_attempt(a['attempt_id'])
    script_table(monkeypatch, {500: (1, 500, 'Ss', 'unknown-owner')})
    assert confirm_dead(daemon, a) == 'quarantined'
    assert daemon.store.list_leases()


@pytest.mark.parametrize('start,expected', [('provider-start', 'quarantined'), ('unrelated', 'lost')])
def test_child_launch_identity_distinguishes_a_live_escape_from_pid_reuse(
        state_daemon, monkeypatch, start, expected):
    daemon, harness = state_daemon
    a = quarantine(daemon, harness)
    adir = daemon.root / 'jobs' / a['job_id'] / 'a1'
    (adir / 'start.json').write_text(json.dumps({'child_pid': 500,
        'child_identity': ident(500, 'provider-start')}))
    daemon.store.update_attempt(a['attempt_id'], child_pid=500)
    a = daemon.store.get_attempt(a['attempt_id'])
    script_table(monkeypatch, {500: (1, 500, 'Ss', start), 501: (500, 501, 'S', 'child')})
    assert confirm_dead(daemon, a) == expected
    if expected == 'quarantined':
        census = daemon._contain(a)
        assert {500, 501} <= census.live_pids
        assert not census.verified_empty
        assert daemon.store.list_leases()
    else:
        assert_released(daemon, a)


@pytest.mark.parametrize('reason', ['held', 'null', '[]', '"held"', '42'])
@pytest.mark.parametrize('operator', [False, True])
def test_legacy_quarantine_reasons_do_not_wedge_either_resolver(state_daemon, monkeypatch, reason, operator):
    daemon, harness = state_daemon
    a = quarantine(daemon, harness)
    daemon.store.update_attempt(a['attempt_id'], quarantine_reason=reason, quarantine_recheck_at='')
    a = daemon.store.get_attempt(a['attempt_id'])
    script_table(monkeypatch, {}, boot=NEXT_BOOT)
    if operator:
        assert confirm_dead(daemon, a) == 'lost'
    else:
        daemon._recheck_quarantines()
    assert_released(daemon, a)


def test_bad_reason_still_holds_a_live_owned_writer(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    a = quarantine(daemon, harness, owned={'300': ident(300, 'owned')})
    daemon.store.update_attempt(a['attempt_id'], quarantine_reason='held')
    a = daemon.store.get_attempt(a['attempt_id'])
    script_table(monkeypatch, {300: (1, 300, 'Ss', 'owned')})
    assert confirm_dead(daemon, a) == 'quarantined'
    assert daemon.store.list_leases()


@pytest.mark.parametrize('manifest', ['{broken', 'null', '[]', '{"turn": []}'])
def test_corrupt_manifest_after_release_clears_the_notice_pin(state_daemon, monkeypatch, manifest):
    daemon, harness = state_daemon
    a = quarantine(daemon, harness)
    daemon.store.update_attempt(a['attempt_id'], state='lost', quarantine_notice_pending=1,
                                quarantine_recheck_at='')
    path = daemon.root / 'jobs' / a['job_id'] / 'manifest.json'
    path.write_text(manifest)
    daemon._recheck_quarantines()
    assert daemon.store.get_attempt(a['attempt_id'])['quarantine_notice_pending'] == 0
    assert daemon.store.one("SELECT 1 FROM events WHERE kind='quarantine.turn_notice_skipped'")


@pytest.mark.parametrize('unverifiable', [False, True])
def test_unchanged_census_does_not_grow_the_audit_but_changes_are_recorded(
        state_daemon, monkeypatch, unverifiable):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness, held={'300': ident(300, 'writer')})
    script_table(monkeypatch, {300: (1, 300, 'Ss', 'writer')}, markers_fail=unverifiable)
    clock.advance()
    daemon._recheck_quarantines()
    before = len(daemon.store.list_events(a['job_id']))
    leases = daemon.store.list_leases()
    for _ in range(20):
        clock.advance()
        daemon._recheck_quarantines()
        assert daemon.store.get_attempt(a['attempt_id'])['quarantine_recheck_at'] > clock.stamp()
    assert len(daemon.store.list_events(a['job_id'])) == before
    assert daemon.store.list_leases() == leases
    # Parent/group shape changes matter even when the PID and identity do not.
    script_table(monkeypatch, {300: (1, 301, 'S', 'writer')}, markers_fail=unverifiable)
    clock.advance()
    daemon._recheck_quarantines()
    assert len(daemon.store.list_events(a['job_id'])) == before + 1
    assert not daemon.store.one("SELECT 1 FROM events WHERE kind='quarantine.recheck_started'")


def test_pace_claim_survives_a_census_failure_without_an_audit_row(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    a = quarantine(daemon, harness)
    calls = []
    def fail(row):
        calls.append(row['attempt_id'])
        raise OSError('inspection failed')
    monkeypatch.setattr(daemon, '_contain', fail)
    before = len(daemon.store.list_events(a['job_id']))
    clock.advance()
    daemon._recheck_quarantines()
    daemon._recheck_quarantines()
    assert calls == [a['attempt_id']]
    assert daemon.store.get_attempt(a['attempt_id'])['quarantine_recheck_at'] > clock.stamp()
    assert len(daemon.store.list_events(a['job_id'])) == before
