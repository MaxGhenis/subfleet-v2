"""The release #185 exit-child input, with updates persisted to the actual store."""
import json

import pytest

from subfleet import procs, protocol
from tests.fake.test_quarantine_attempt_isolation import running
from tests.fake.test_review_pr131_probes import script_table
from tests.fake.test_state_contract import state_daemon  # noqa: F401


@pytest.mark.parametrize('consumer', ['census', 'confirm-dead', 'automatic'])
def test_exit_only_child_remains_a_census_root(state_daemon, monkeypatch, consumer):
    daemon, harness = state_daemon
    ours, directory = running(daemon, harness, 200, 201, out_path=str(harness.root / 'exit-child-export.md'))
    daemon.store.update_attempt(ours['attempt_id'], child_pid=None, evidence_json='{}')
    (directory / 'start.json').write_text(json.dumps({
        'guardian_pid': 200, 'pgid': 200, 'boot_id': ours['boot_id'], 'proc_start': ours['proc_start']}))
    (directory / 'exit.json').write_text(json.dumps({
        'rc': 0, 'signal': None, 'child_pid': 201, 'finished_at': '2026-10-12T00:00:00Z'}))
    # Receipt is the sole publication of this child. Its identity is unavailable;
    # absence of the guardian and original group does not prove the child gone.
    rows = {201: (1, 201, 'S', 'unqualified-child'),
            202: (201, 201, 'S', 'unqualified-descendant')}
    script_table(monkeypatch, rows)
    monkeypatch.setattr(procs.os, 'kill', lambda *args: pytest.fail('signal from receipt PID'))
    monkeypatch.setattr(procs.os, 'killpg', lambda *args: pytest.fail('group signal from receipt PID'))
    ours = daemon.store.get_attempt(ours['attempt_id'])
    if consumer == 'census':
        census = daemon._contain(ours)
        assert not census.verified_empty, census.to_dict()
        assert {201, 202} <= census.live_pids, census.to_dict()
    else:
        daemon._quarantine(ours, procs.Containment(unverifiable=True), 'legacy child not yet identified')
        held = daemon.store.list_leases(ours['attempt_id']) + daemon.store.list_leases(ours['job_id'])
        assert held
        if consumer == 'confirm-dead':
            daemon._resolve_quarantine(daemon.store.get_attempt(ours['attempt_id']),
                                       protocol.KillArgs(ours['job_id'], confirm_dead=True))
        else:
            daemon.store.update_attempt(ours['attempt_id'], quarantine_recheck_at='')
            daemon._recheck_quarantines()
        actual = daemon.store.get_attempt(ours['attempt_id'])
        remaining = daemon.store.list_leases(ours['attempt_id']) + daemon.store.list_leases(ours['job_id'])
        releases = [event for event in daemon.store.list_events(ours['job_id'])
                    if event['kind'] in {'quarantine.confirmed_dead', 'quarantine.self_resolved'}]
        assert actual['state'] == 'quarantined' and remaining == held, {
            'state': actual['state'], 'remaining': remaining,
            'receipt_child_alive': 201 in rows, 'release_events': releases}


def test_recycled_probe_guardian_does_not_seed_a_stranger_group(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    ours, directory = running(daemon, harness, 200, 201)
    record = {'holder': 'probe:' + ours['job_id'], 'job_id': ours['job_id'],
              'lane_id': ours['lane_id'], 'directory': str(directory),
              'guardian_pid': 200, 'pgid': 200, 'child_pid': None,
              'boot_id': ours['boot_id'], 'proc_start': ours['proc_start'],
              'owned_identities': {}, 'state': 'running'}
    (directory / 'exit.json').write_text(json.dumps({'rc': 1, 'child_pid': None,
                                                    'spawn_error': 'child was not started'}))
    rows = {200: (1, 200, 'S', 'replacement-guardian'),
            202: (200, 200, 'S', 'stranger-child')}
    script_table(monkeypatch, rows)
    census = daemon._probe_census(record)
    assert census.verified_empty, census.to_dict()
