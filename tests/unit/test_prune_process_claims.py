"""C-11.5 prune proofs over schema 8's process-claim tables; every store is disposable.

A schema 8 store holds the process-claim projection C-5.4's claim
authorization reads (`store_process_claims.sql`): two `WITHOUT ROWID` tables
and three singleton/revision tables, kept by triggers on `attempts`. They are not decision dependents. They reference
`attempts`, and a prune writes only `decisions` (DELETE) and `events` (one
audit row per batch). These tests prove that with populated tables:
* each batch changes exactly its own rows, with no trigger or cascade;
* the backups hold the tables;
* the published restore puts them back;
* any drift in them stops the pass before its first delete;
* a store missing one is refused.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from subfleet import prune_decisions as pruning
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.store import SCHEMA_VERSION, Store
from tests.table_order import ordered_select

CLAIM_TABLES = {
    'attempt_process_claims', 'process_claim_clock', 'process_claim_revisions',
    'process_claim_receipts', 'process_claim_receipt_pending',
}
WITHOUT_ROWID = {'attempt_process_claims', 'process_claim_receipt_pending'}
#: Tests below wrap `sqlite3.connect` to reach the backup copy; snapshots read past it.
CONNECT = sqlite3.connect


def _identity(pid: int, start: str) -> dict:
    return {'pid': pid, 'boot_id': 'synthetic-boot', 'proc_start': start}


def seed(root: Path) -> None:
    """A terminal job with two claim-bearing attempts and three redundant decisions."""
    with Store(root / 'state.sqlite3') as store:
        store.put_lane(Lane('codex-1', 'codex', 'codex:one',
                            Credential('codex', '/synthetic/home', 'home'),
                            '/synthetic/home', LaneOwner.V2, False))
        store.add_job(job_id='terminal', request_id='terminal', payload_digest='digest',
                      kind='dispatch', workdir=str(root), prompt_path='/synthetic/prompt',
                      sandbox='read-only', state='succeeded')
        for seq in (1, 2):
            evidence = {
                'owned_identities': {str(4300 + seq): _identity(4300 + seq, f'owned-{seq}')},
                'owned_identity_history': {str(4300 + seq): _identity(4300 + seq, f'owned-{seq}')},
                'descendant_identities': [_identity(4400 + seq, f'descendant-{seq}')],
                'ambiguous_identities': [_identity(4500 + seq, f'ambiguous-{seq}')],
            }
            if seq == 1:
                # A published child resolves this attempt's receipt import; the
                # second attempt's stays pending.
                evidence['child_identity'] = _identity(4600 + seq, f'child-{seq}')
            store.add_attempt(attempt_id=f'terminal/a{seq}', job_id='terminal', seq=seq,
                              lane_id='codex-1', model_requested='gpt-6-astra', state='succeeded',
                              guardian_pid=4200 + seq, pgid=4200 + seq, boot_id='synthetic-boot',
                              proc_start=f'guardian-{seq}', evidence_json=json.dumps(evidence))
        with store.transaction('synthetic.receipt_retry', audit=False) as tx:
            tx.execute("UPDATE process_claim_receipt_pending SET failures=3,retry_at=1.25,"
                       "last_error='awaiting-child-identity'")
            tx.execute('UPDATE process_claim_receipts SET enumerated=1 WHERE singleton=1')
        for note in range(4):
            store.add_decision('terminal', {'policy_hash': 'hash', 'note': str(note)})
        store.add_decision('terminal', {'policy_hash': 'hash', 'note': 'attempt'},
                           attempt_id='terminal/a1')


def snapshot(database) -> dict[str, list[tuple]]:
    with contextlib.closing(CONNECT(database)) as conn:
        return {name: conn.execute(ordered_select(conn, name)).fetchall()
                for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_the_seed_populates_every_claims_table(tmp_path):
    """The proofs below are not vacuous: every claims table holds rows of its own."""
    seed(tmp_path)
    rows = snapshot(tmp_path / 'state.sqlite3')
    assert {(row[0], row[1], row[4]) for row in rows['attempt_process_claims']} == {
        ('terminal/a1', 4201, 'guardian'), ('terminal/a1', 4301, 'owned_identities'),
        ('terminal/a1', 4301, 'owned_identity_history'),
        ('terminal/a1', 4401, 'descendant_identities'),
        ('terminal/a1', 4501, 'ambiguous_identities'), ('terminal/a1', 4601, 'child_identity'),
        ('terminal/a2', 4202, 'guardian'), ('terminal/a2', 4302, 'owned_identities'),
        ('terminal/a2', 4302, 'owned_identity_history'),
        ('terminal/a2', 4402, 'descendant_identities'),
        ('terminal/a2', 4502, 'ambiguous_identities')}
    assert rows['process_claim_receipt_pending'] == [('terminal/a2', 3, 1.25, 'awaiting-child-identity')]
    assert {row[0] for row in rows['process_claim_revisions']} == {'terminal/a1', 'terminal/a2'}
    assert rows['process_claim_clock'][0][1] >= 2
    assert rows['process_claim_receipts'] == [(1, 0, 1)]
    assert len(rows['decisions']) == 5


@pytest.mark.parametrize('vacuum', [False, True])
def test_claims_tables_are_not_decision_dependents_and_survive_byte_identical(
        tmp_path, monkeypatch, vacuum):
    """C-3.2, C-11.5: each batch changes only its decisions and one audit row.

    `total_changes` counts rows that triggers and foreign-key actions change,
    so a batch that changed exactly `len(batch) + 1` rows fired neither.
    """
    seed(tmp_path)
    database = tmp_path / 'state.sqlite3'
    before = snapshot(database)
    begin = Store.transaction
    changed = []

    @contextlib.contextmanager
    def counted(self, kind='state.changed', **kwargs):
        start = self.connection.total_changes
        with begin(self, kind, **kwargs) as tx:
            yield tx
        if kind == pruning.PRUNE_KIND:
            changed.append(self.connection.total_changes - start)

    monkeypatch.setattr(Store, 'transaction', counted)
    report = pruning.prune(tmp_path, apply=True, confirm=True, vacuum=vacuum, batch_size=2,
                           write_report=False)
    assert (report.deleted, report.batches) == (3, 2)
    assert changed == [2 + 1, 1 + 1]
    assert report.schema == {'store': SCHEMA_VERSION, 'build': SCHEMA_VERSION}
    assert CLAIM_TABLES <= set(report.verification['tables_checked'])
    assert report.verification['tables_identical'] is True
    for table in CLAIM_TABLES:
        assert report.fingerprints['after'][table] == report.fingerprints['before'][table]
        assert report.backups['copy']['fingerprints'][table] == report.fingerprints['before'][table]
        if vacuum:
            assert report.fingerprints['after_vacuum'][table] == report.fingerprints['before'][table]
    after = snapshot(database)
    assert {table: rows for table, rows in after.items() if table not in ('decisions', 'events')} == {
        table: rows for table, rows in before.items() if table not in ('decisions', 'events')}
    assert [row[0] for row in after['decisions']] == [4, 5]
    # The whole-store copy holds every claims row, and the published restore
    # puts the whole store back, claims tables included.
    assert snapshot(report.backups['copy']['path']) == before
    script = pruning._restore_fix(report.backups['copy']['path']).split('```sh\n', 1)[1].split('```', 1)[0]
    restored = subprocess.run(['/bin/sh', '-c', script], check=True, capture_output=True, text=True,
                              env={**os.environ, 'state_root': str(tmp_path)}, timeout=120)
    assert restored.stdout.splitlines() == ['ok']
    assert snapshot(database) == before


def _damage(table: str, fault: str) -> str:
    if fault == 'missing':
        return f'DROP TABLE "{table}"'
    if fault == 'count':
        return f'DELETE FROM "{table}"'
    return {
        'attempt_process_claims': "UPDATE attempt_process_claims SET boot_id='altered-boot' "
                                  "WHERE attempt_id='terminal/a1' AND source='guardian'",
        'process_claim_receipt_pending': "UPDATE process_claim_receipt_pending SET last_error='altered'",
        'process_claim_revisions': "UPDATE process_claim_revisions SET generation=generation+100 "
                                   "WHERE attempt_id='terminal/a1'",
        'process_claim_clock': 'UPDATE process_claim_clock SET generation=generation+100',
        'process_claim_receipts': 'UPDATE process_claim_receipts SET seeded=1',
        'extra_keyed': "UPDATE extra_keyed SET payload=X'FF00' WHERE key='b'",
    }[table]


@pytest.mark.parametrize('table,fault', [
    *[(table, 'content') for table in sorted(CLAIM_TABLES)],
    *[(table, fault) for table in sorted(WITHOUT_ROWID) for fault in ('count', 'missing')],
])
def test_claims_drift_in_the_whole_backup_is_refused_before_any_delete(
        tmp_path, monkeypatch, table, fault):
    """C-11.5: the whole-copy proof covers every claims table, at identical counts too."""
    seed(tmp_path)
    database = tmp_path / 'state.sqlite3'
    before = snapshot(database)
    connect = pruning.sqlite3.connect
    begin = Store.transaction
    deletes, altered = [], []

    def transaction(self, kind='state.changed', **kwargs):
        if kind == pruning.PRUNE_KIND:
            deletes.append(kind)
        return begin(self, kind, **kwargs)

    def damaged_connect(target, *args, **kwargs):
        if '/backups/state-' in str(target):
            path = Path(unquote(urlsplit(str(target)).path))
            with contextlib.closing(connect(path)) as backup:
                cursor = backup.execute(_damage(table, fault))
                assert fault == 'missing' or cursor.rowcount >= 1
                backup.commit()
                assert backup.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
            if fault == 'content':
                copied = snapshot(path)
                assert {name: len(rows) for name, rows in copied.items()} == {
                    name: len(rows) for name, rows in before.items()}
                assert copied[table] != before[table]
            altered.append(path)
        return connect(target, *args, **kwargs)

    monkeypatch.setattr(Store, 'transaction', transaction)
    monkeypatch.setattr(pruning.sqlite3, 'connect', damaged_connect)
    with pytest.raises(pruning.PruneVerificationError, match='differs from the planning snapshot') as stopped:
        pruning.prune(tmp_path, apply=True, confirm=True)
    assert len(altered) == 1                     # the whole-copy verification was reached
    assert table in str(stopped.value).rsplit('in tables: ', 1)[1].split(', ')
    assert stopped.value.report.deleted == stopped.value.report.batches == 0
    assert deletes == []
    assert snapshot(database) == before


def test_a_trigger_driven_claims_change_after_planning_is_refused_before_delete(tmp_path, monkeypatch):
    """C-11.5: a writer that commits after planning changes `attempts`, and its
    triggers change the claims tables. The copy no longer matches the planning
    snapshot, so the pass stops before its first delete and names every table."""
    seed(tmp_path)
    database = tmp_path / 'state.sqlite3'
    before = snapshot(database)
    original = pruning._copy_store

    def drift(conn, destination, expected):
        with contextlib.closing(sqlite3.connect(database)) as writer:
            writer.execute("UPDATE attempts SET guardian_pid=4299 WHERE attempt_id='terminal/a2'")
            writer.commit()
        return original(conn, destination, expected)

    monkeypatch.setattr(pruning, '_copy_store', drift)
    with pytest.raises(pruning.PruneVerificationError, match='differs from the planning snapshot') as stopped:
        pruning.prune(tmp_path, apply=True, confirm=True)
    named = set(str(stopped.value).rsplit('in tables: ', 1)[1].split(', '))
    assert {'attempts', 'attempt_process_claims', 'process_claim_clock',
            'process_claim_revisions'} <= named
    assert stopped.value.report.deleted == stopped.value.report.batches == 0
    after = snapshot(database)
    assert after['decisions'] == before['decisions']
    # The writer's change stays; only this pass's deletes were withheld.
    assert not any(row[1] == 4299 for row in before['attempt_process_claims'])
    assert ('terminal/a2', 4299, 'guardian-2', 'synthetic-boot', 'guardian') in after['attempt_process_claims']


@pytest.mark.parametrize('fault', [None, 'content'])
def test_a_new_without_rowid_table_is_included_in_the_proof(tmp_path, monkeypatch, fault):
    """C-3.2: enumeration covers a future WITHOUT ROWID table, not just rowid tables."""
    seed(tmp_path)
    database = tmp_path / 'state.sqlite3'
    with contextlib.closing(sqlite3.connect(database)) as conn:
        conn.execute('CREATE TABLE extra_keyed (key TEXT PRIMARY KEY, payload BLOB) WITHOUT ROWID')
        conn.executemany('INSERT INTO extra_keyed VALUES (?,?)', [('b', b'\x00\xff'), ('a', None)])
        conn.commit()
    before = snapshot(database)
    assert before['extra_keyed'] == [('a', None), ('b', b'\x00\xff')]
    if fault:
        connect = pruning.sqlite3.connect

        def damaged_connect(target, *args, **kwargs):
            if '/backups/state-' in str(target):
                with contextlib.closing(connect(unquote(urlsplit(str(target)).path))) as backup:
                    assert backup.execute(_damage('extra_keyed', fault)).rowcount == 1
                    backup.commit()
            return connect(target, *args, **kwargs)

        monkeypatch.setattr(pruning.sqlite3, 'connect', damaged_connect)
        with pytest.raises(pruning.PruneVerificationError, match='snapshot in tables: extra_keyed$') as stopped:
            pruning.prune(tmp_path, apply=True, confirm=True, write_report=False)
        assert stopped.value.report.deleted == 0
        assert snapshot(database) == before
        return
    report = pruning.prune(tmp_path, apply=True, confirm=True, write_report=False)
    assert 'extra_keyed' in report.verification['tables_checked']
    assert report.backups['copy']['fingerprints']['extra_keyed'] == report.fingerprints['before']['extra_keyed']
    assert report.fingerprints['before']['extra_keyed']['rows'] == 2
    assert snapshot(database)['extra_keyed'] == before['extra_keyed']


@pytest.mark.parametrize('table', sorted(CLAIM_TABLES))
def test_a_store_missing_a_claims_table_is_refused_without_repair(tmp_path, table):
    """C-3.1, C-11.5: schema 8 expects the claims tables; none is recreated before the backup."""
    seed(tmp_path)
    database = tmp_path / 'state.sqlite3'
    with contextlib.closing(sqlite3.connect(database)) as conn:
        conn.execute(f'DROP TABLE "{table}"')
        conn.commit()
    before = snapshot(database)
    with pytest.raises(pruning.PruneRefused,
                       match=f'schema {SCHEMA_VERSION} store is missing expected tables: {table}$') as refusal:
        pruning.prune(tmp_path, apply=True, confirm=True)
    assert refusal.value.code == 7
    assert snapshot(database) == before
    assert table not in before
    assert not (tmp_path / 'backups').exists()
    assert not list(tmp_path.glob('decisions-prune-report-*.json'))
