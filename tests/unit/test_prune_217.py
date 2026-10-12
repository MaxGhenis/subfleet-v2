"""Release/217 reader and schema proofs for C-11.5, using disposable stores."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest
from hypothesis import example, given, settings, strategies as st

from subfleet import daemon as daemon_module
from subfleet import prune_decisions as pruning
from subfleet import retention_archive as archives
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.daemon import Daemon, DaemonUnavailable, Unroutable
from subfleet.offline import Offline
from subfleet.store import SCHEMA_VERSION, Store

STATES = ('queued', 'waiting', 'running', 'succeeded', 'failed', 'cancelled', 'lost')
TABLES = {
    'schema_version', 'lanes', 'readings', 'closures', 'jobs', 'job_owned_branches',
    'job_pushes', 'attempts', 'artifacts', 'notices', 'decisions', 'leases',
    'actions', 'events', 'service_notices',
}
# Independent copies of the live readers, including why's evaluation timestamp.
WHY = ('SELECT decision_json,evaluated_at FROM decisions WHERE job_id=? '
       'ORDER BY decision_id DESC LIMIT 1')
SHOW = ('SELECT decision_json FROM decisions WHERE job_id = ? '
        'ORDER BY evaluated_at DESC, decision_id DESC LIMIT 1')
DECISION = st.tuples(st.booleans(), st.integers(0, 3), st.text(max_size=30), st.booleans())
STORES = st.lists(st.tuples(st.sampled_from(STATES), st.lists(DECISION, max_size=8)), max_size=5)


def _seed(root, specs):
    """Real schema 7, with attempts, timestamp ties and distinct raw JSON bytes."""
    with Store(root / 'state.sqlite3') as store:
        store.put_lane(Lane('codex-1', 'codex', 'codex:one',
                            Credential('codex', '/synthetic/home', 'home'),
                            '/synthetic/home', LaneOwner.V2, False))
        # Every example exercises a real delete and a job with no decision.
        specs = [('succeeded', [(False, 0, 'redundant', False)] * 4),
                 ('lost', []), *specs]
        for index, (state, decisions) in enumerate(specs):
            job_id = f'job-{index}'
            store.add_job(job_id=job_id, request_id=job_id, payload_digest='digest',
                          kind='dispatch', workdir=str(root), prompt_path='/synthetic/prompt',
                          sandbox='read-only', state=state)
            for seq, (attempt, clock, note, spaced) in enumerate(decisions, 1):
                attempt_id = f'{job_id}/a{seq}' if attempt else None
                if attempt:
                    store.add_attempt(attempt_id=attempt_id, job_id=job_id, seq=seq,
                                      lane_id='codex-1', model_requested='gpt-6-astra',
                                      state='succeeded')
                payload = json.dumps({'policy_hash': 'hash', 'note': note}, ensure_ascii=False)
                if spaced:
                    payload = ' \n' + payload + '\t '
                store.connection.execute(
                    'INSERT INTO decisions(job_id,attempt_id,evaluated_at,policy_hash,decision_json) '
                    'VALUES (?,?,?,?,?)',
                    (job_id, attempt_id, f'2026-10-01T00:00:0{clock}Z', 'hash', payload))


def _tables(database):
    with sqlite3.connect(database) as conn:
        return {name: conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
                for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _service(store, root):
    """The real why reader with a fixed admission context, without starting a daemon."""
    service = object.__new__(Daemon)
    service.store, service.root, service.policy = store, root, {}
    service._holds, service._capacity_waits = {}, {}
    service._liveness = lambda jobs: None
    service._priority_jobs = lambda jobs: {}
    service._desktop_identity = lambda: None
    service.log = logging.getLogger('synthetic-prune-reader')

    def no_route(*args, **kwargs):
        raise Unroutable('synthetic job has no route')
    service._pick = no_route
    return service


def _readers(root):
    """Compare raw SQL bytes and the actual why/show answers for every job."""
    with Store(root / 'state.sqlite3', read_only=True) as store:
        service = _service(store, root)
        offline = Offline(root)
        answers = {}
        for job in store.list_jobs():
            job_id = job['job_id']
            raw = tuple(tuple(row) if row else None for row in (
                store.connection.execute(WHY, (job_id,)).fetchone(),
                store.connection.execute(SHOW, (job_id,)).fetchone()))
            answers[job_id] = (raw, service._why_job(job), offline.show_job(job_id))
        return answers


@settings(max_examples=40, deadline=None)
@given(specs=STORES, vacuum=st.booleans())
@example(specs=[(state, [(True, 0, 'attempt', True), (False, 3, 'clock', True),
                        (False, 1, 'middle', False), (False, 1, 'id', False)])
                for state in STATES], vacuum=True)
def test_partition_unfinished_jobs_readers_and_backup_restore(specs, vacuum):
    """C-11.5: partition, live histories, latest answers, and exact schema-7 restore."""
    with tempfile.TemporaryDirectory(prefix='sf-prune217-property-') as directory:
        root = Path(directory)
        _seed(root, specs)
        original = _tables(root / 'state.sqlite3')
        before = _readers(root)
        with Store(root / 'state.sqlite3', read_only=True) as store:
            planned = pruning.plan(store.connection)
            all_rows = {row['decision_id']: row for row in store.query('SELECT * FROM decisions ORDER BY decision_id')}
            states = {job['job_id']: job['state'] for job in store.list_jobs()}
            # Independently derive the keep set from the stated contract.
            expected = set()
            for job_id, state in states.items():
                history = [row for row in all_rows.values() if row['job_id'] == job_id]
                if state in ('queued', 'waiting', 'running'):
                    expected.update(row['decision_id'] for row in history)
                expected.update(row['decision_id'] for row in history if row['attempt_id'] is not None)
                for candidates in (history, [row for row in history if row['attempt_id'] is None]):
                    if candidates:
                        expected.add(max(candidates, key=lambda row: row['decision_id'])['decision_id'])
                        expected.add(max(candidates, key=lambda row: (row['evaluated_at'], row['decision_id']))['decision_id'])
            assert set(planned.keep) == expected
            assert set(planned.keep).isdisjoint(planned.delete)
            assert set(planned.keep) | set(planned.delete) == set(all_rows)
            assert len(planned.delete) == len(set(planned.delete))
            assert all(states[all_rows[key]['job_id']] not in ('queued', 'waiting', 'running')
                       for key in planned.delete)

        report = pruning.prune(root, apply=True, confirm=True, vacuum=vacuum,
                               batch_size=3, write_report=False)
        assert _readers(root) == before
        assert report.schema == {'store': 7, 'build': 7}
        assert report.verification['readers_checked'] == len(states)
        assert report.verification['readers_identical'] == len(states)
        if vacuum:
            assert report.verification['after_vacuum']['readers_identical'] == len(states)
        with Store(root / 'state.sqlite3', read_only=True) as store:
            for job_id, state in states.items():
                expected_history = [row for row in all_rows.values()
                                    if row['job_id'] == job_id and row['decision_id'] in planned.keep]
                assert store.list_decisions(job_id) == expected_history
                assert archives.job_rows(store, job_id)['decisions'] == expected_history
            survivors = store.query('SELECT * FROM decisions ORDER BY decision_id')
            assert survivors == [row for key, row in all_rows.items() if key in planned.keep]
        with gzip.open(report.backups['rows']['path'], 'rt') as stream:
            recovered = [json.loads(line) for line in stream]
        assert recovered == [all_rows[key] for key in planned.delete]

        # Execute the published restore, not just a read of the backup.
        script = pruning._restore_fix(report.backups['copy']['path']).split('```sh\n', 1)[1].split('```', 1)[0]
        restored = subprocess.run(['/bin/sh', '-c', script], check=True, capture_output=True,
                                  text=True, env={**os.environ, 'state_root': str(root)}, timeout=30)
        assert restored.stdout.splitlines() == ['ok']
        assert _tables(root / 'state.sqlite3') == original
        assert _readers(root) == before


def test_schema_7_has_no_decision_dependents_and_hashes_every_table(tmp_path):
    """C-3.1, C-11.5: the leaf-table and enumeration assumptions hold on this build."""
    _seed(tmp_path, [])
    with Store(tmp_path / 'state.sqlite3', read_only=True) as store:
        assert SCHEMA_VERSION == 7
        names = {row['name'] for row in store.query("SELECT name FROM sqlite_master WHERE type='table'")}
        assert names == TABLES == set(pruning._tables(store.connection))
        assert set(pruning._fingerprints(store.connection)) == TABLES - {'decisions', 'events'}
        assert not [fk for name in names for fk in store.query(f'PRAGMA foreign_key_list("{name}")')
                    if fk['table'] == 'decisions']
        # The schema has no hidden trigger that could make this DELETE write elsewhere.
        assert store.query("SELECT name FROM sqlite_master WHERE type='trigger'") == []


def test_a_new_table_is_included_in_the_proof(tmp_path):
    """C-3.2: table enumeration covers future additions, not just a frozen list."""
    _seed(tmp_path, [])
    with Store(tmp_path / 'state.sqlite3') as store:
        store.connection.execute('CREATE TABLE extra_evidence (payload BLOB)')
        store.connection.execute("INSERT INTO extra_evidence VALUES (X'00FF')")
    original = _tables(tmp_path / 'state.sqlite3')
    report = pruning.prune(tmp_path, apply=True, confirm=True, write_report=False)
    assert 'extra_evidence' in report.verification['tables_checked']
    assert _tables(tmp_path / 'state.sqlite3')['extra_evidence'] == original['extra_evidence']


def test_the_release_daemon_and_prune_share_the_lifetime_lock(tmp_path, monkeypatch):
    """C-3.4: exercise this line's daemon, beyond the PR's independent flock test."""
    monkeypatch.setattr(daemon_module.procs, 'boot_id', lambda: 'synthetic-boot')
    monkeypatch.setattr(daemon_module.procs, 'proc_start', lambda pid: 'synthetic-start')
    _seed(tmp_path, [])
    original = _tables(tmp_path / 'state.sqlite3')
    service = Daemon(tmp_path)
    try:
        with pytest.raises(pruning.PruneRefused, match='a daemon holds') as refusal:
            pruning.prune(tmp_path, apply=True, confirm=True, write_report=False)
        assert refusal.value.code == 7
        # A live daemon permits the read-only plan.
        assert pruning.prune(tmp_path, write_report=False).decisions['delete'] == 3
    finally:
        service.close()
    assert _tables(tmp_path / 'state.sqlite3') == original
    handle = pruning._hold_daemon_lock(tmp_path)
    try:
        with pytest.raises(DaemonUnavailable, match='another daemon holds'):
            Daemon(tmp_path)
    finally:
        pruning._release_daemon_lock(handle)
    assert pruning.prune(tmp_path, apply=True, confirm=True, write_report=False).deleted == 3


def test_a_pre_prune_archive_stays_readable_and_restorable(tmp_path):
    """C-8.4, C-11.5: existing archives keep their full rows and restore API."""
    _seed(tmp_path, [])
    with Store(tmp_path / 'state.sqlite3', read_only=True) as store:
        original_rows = archives.job_rows(store, 'job-0')
    # A minimal published archive containing a stored job file, with no Git objects.
    archive = tmp_path / 'archive' / 'before'
    (archive / 'files').mkdir(parents=True)
    content = b'pre-prune deliverable\n'
    (archive / 'files' / 'deliverable').write_bytes(content)
    manifest = {'job_id': 'job-0', 'trees': {'job': {'entries': [
        {'p': '', 'sig': {'t': 'd', 'mode': 0o700, 'mtime': 1}},
        {'p': 'out.md', 'sig': {'t': 'f', 'mode': 0o600, 'mtime': 1},
         'store': 'deliverable', 'sha256': hashlib.sha256(content).hexdigest()},
    ]}}}
    (archive / 'manifest.json').write_text(json.dumps(manifest))
    (archive / 'rows.json').write_text(json.dumps(original_rows))
    before = {path.relative_to(archive): path.read_bytes() for path in archive.rglob('*') if path.is_file()}
    pruning.prune(tmp_path, apply=True, confirm=True, write_report=False)
    assert {path.relative_to(archive): path.read_bytes() for path in archive.rglob('*') if path.is_file()} == before
    assert json.loads((archive / 'rows.json').read_text()) == original_rows
    assert archives.check_archive(tmp_path, 'before')['ok']
    destination = tmp_path / 'restored'
    report = archives.restore(tmp_path, 'before', to=destination)
    assert Path(report['restored']['job'], 'out.md').read_bytes() == content
    with Store(tmp_path / 'state.sqlite3', read_only=True) as store:
        assert len(store.list_decisions('job-0')) == 1
        assert len(json.loads((archive / 'rows.json').read_text())['decisions']) == 4


def test_audit_kind_is_readable_and_does_not_change_job_answers(tmp_path):
    """C-3.2: generic event readers accept the new kind; why/show ignore its jobless audit."""
    _seed(tmp_path, [])
    before = _readers(tmp_path)
    report = pruning.prune(tmp_path, apply=True, confirm=True, write_report=False, batch_size=2)
    with Store(tmp_path / 'state.sqlite3', read_only=True) as store:
        added = [row for row in store.list_events() if row['kind'] == 'decisions.pruned']
        assert len(added) == report.batches == 2
        assert all(row['job_id'] is None and row['attempt_id'] is None and row['lane_id'] is None
                   and isinstance(json.loads(row['data_json']), dict) for row in added)
        assert not [row for row in store.list_events('job-0') if row['kind'] == 'decisions.pruned']
    assert _readers(tmp_path) == before


def test_after_vacuum_proof_checks_the_added_audit_rows(tmp_path, monkeypatch):
    """C-3.2, C-11.5: a rebuild must retain the audit batch counts proved after deletion."""
    _seed(tmp_path, [])
    original = pruning._reverify

    def lose_audit(conn, report, **kwargs):
        conn.execute('DELETE FROM events WHERE event_id>?', (kwargs['events_max'],))
        return original(conn, report, **kwargs)

    monkeypatch.setattr(pruning, '_reverify', lose_audit)
    with pytest.raises(pruning.PruneVerificationError, match='events gained nothing') as stopped:
        pruning.prune(tmp_path, apply=True, confirm=True, vacuum=True, write_report=False)
    report = stopped.value.report
    assert report.verification['after_vacuum']['events_added'] == {}
    assert report.verification['after_vacuum']['ok'] is False
    assert report.backups['copy']['path'] in stopped.value.fix
