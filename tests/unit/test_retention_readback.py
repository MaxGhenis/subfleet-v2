"""Stored-copy preservation regressions, adapted from the independent PR #129 probes."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from subfleet import retention, retention_archive as ra, retention_fs as fs
from subfleet.store import Store
from tests.unit.retention_world import Clock, World, git, snapshot, trust_temporary_directories

DATA = b'only the archive can preserve these bytes\n'
REL = 'out/private.bin'

@pytest.fixture
def world(tmp_path, monkeypatch):
    trust_temporary_directories(monkeypatch)
    w = World(tmp_path)
    wt = w.job('job')
    (wt / 'out').mkdir()
    (wt / REL).write_bytes(DATA)
    yield w, wt
    w.close()

def run(w, **kwargs):
    return retention.maintenance(w.store, w.root, max_jobs=0, max_bytes=0,
        clock=Clock(), holders=lambda *a, **k: {}, **kwargs)

def stored(r):
    entry = next(e for e in r.manifest()['trees']['worktree']['entries'] if e['p'] == REL)
    return r.building / 'files' / entry['store'], entry

def tamper(r, mutation):
    copy, entry = stored(r)
    st = copy.stat()
    before = fs.sig_key(st)
    changed = bytes([DATA[0] ^ 1]) + DATA[1:]
    if mutation == 'lost':
        copy.unlink()
    elif mutation == 'hardlink':
        other = r.root / 'unrelated-bytes'
        other.write_bytes(changed)
        copy.unlink()
        os.link(other, copy)
    else:
        copy.write_bytes(changed)
        if mutation == 'mtime':
            os.utime(copy, ns=(st.st_atime_ns, st.st_mtime_ns))
            assert copy.stat().st_mtime_ns == st.st_mtime_ns
    after = fs.sig_key(copy.stat()) if copy.exists() else None
    assert after != before

def archived(w, monkeypatch, restart):
    def interrupt(self):
        raise ra.Interrupted('review restart at archived')
    with monkeypatch.context() as once:
        once.setattr(ra.Retirement, 'final_check', interrupt)
        first = run(w)
    assert first['pruned'] == [] and first.get('interrupted'), first
    if restart:
        w.store.close()
        w.store = Store(w.root / 'state.sqlite3')
    r = ra.Retirement(ra.Context(w.root, w.store), 'job')
    assert r.state == 'archived'
    return r

@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('mutation', ['lost', 'same-size', 'hardlink', 'mtime'])
def test_damage_before_final_copy_check_keeps_all_source_bytes(world, monkeypatch, restart, mutation):
    w, wt = world
    before = snapshot(wt)
    if restart:
        r = archived(w, monkeypatch, True)
        tamper(r, mutation)
    else:
        original = ra.Retirement.final_check
        def corrupt_then_check(self):
            assert self.state == 'archived'
            tamper(self, mutation)
            original(self)
        monkeypatch.setattr(ra.Retirement, 'final_check', corrupt_then_check)
    outcome = run(w)
    print(json.dumps({'case': 'before-check', 'restart': restart, 'mutation': mutation,
                      'pruned': outcome['pruned'], 'deferred': outcome['deferred']}))
    assert outcome['pruned'] == [] and outcome['protected'] == ['job'], outcome
    assert outcome['deferred']['job'].startswith('archive did not read back:'), outcome
    assert snapshot(wt) == before and w.store.get_job('job')
    assert str(wt) in git(w.repo, 'worktree', 'list', '--porcelain')
    assert w.store.list_leases() == []

@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('mutation', ['lost', 'same-size', 'hardlink', 'mtime'])
def test_damage_after_final_copy_check_must_not_delete_source(world, monkeypatch, restart, mutation):
    w, wt = world
    if restart:
        archived(w, monkeypatch, True)
    original = ra.Retirement._check_copies
    def check_then_corrupt(self, manifest):
        original(self, manifest)
        assert self.state == 'archived'
        tamper(self, mutation)
    monkeypatch.setattr(ra.Retirement, '_check_copies', check_then_corrupt)
    outcome = run(w)
    assert outcome['pruned'] == ['job'] and outcome['reclaimed'] == ['job'], outcome
    assert {c['job_id'] for c in outcome['conflicts']} == {'job'}, outcome
    conflict = w.root / 'retention-conflicts/job/worktree' / REL
    assert conflict.read_bytes() == DATA
    assert not wt.exists() and w.store.get_job('job') is None
    assert w.store.list_leases() == []
    assert not ra.check_archive(w.root, 'job')['ok']

def expire_cache(w):
    r = ra.Retirement(ra.Context(w.root, w.store), 'job')
    assert r.state == 'idle'
    r.save(defer_until=0)

@pytest.mark.parametrize('mutation', ['lost', 'same-size', 'hardlink', 'mtime'])
def test_damaged_kept_cache_eventually_rebuilds_and_restores(world, monkeypatch, mutation):
    w, wt = world
    before = snapshot(wt)
    def defer(self):
        raise ra.Defer('review cache deferral', ra.DEFER_CHANGED_S)
    with monkeypatch.context() as once:
        once.setattr(ra.Retirement, 'final_check', defer)
        assert run(w)['pruned'] == []
    r = ra.Retirement(ra.Context(w.root, w.store), 'job')
    assert r.state == 'idle'
    tamper(r, mutation)
    expire_cache(w)
    second = run(w)
    if not second['pruned']:
        assert second['deferred']['job'].startswith('archive did not read back:'), second
        assert snapshot(wt) == before
        expire_cache(w)
        third = run(w)
        assert third['pruned'] == ['job'], third
    else:
        assert mutation == 'lost' and second['pruned'] == ['job'], second
    assert ra.check_archive(w.root, 'job')['ok']
    ra.restore(w.root, 'job')
    assert snapshot(wt) == before

@pytest.mark.parametrize('resume', [False, True])
def test_old_progress_rechecks_once_and_finishes(world, monkeypatch, resume):
    w, wt = world
    before = snapshot(wt)
    r = archived(w, monkeypatch, True)
    records = [json.loads(line) for line in (r.building / ra.PROGRESS).read_text().splitlines()]
    names = set()
    for record in records:
        if record['k'].startswith('v:'):
            record.pop('sig')
            names.add(record['k'][2:])
    (r.building / ra.PROGRESS).write_text(''.join(json.dumps(v) + '\n' for v in records))
    if not resume:
        r.save(state='quarantined')
    reads = []
    original = ra._read_back
    def count(fd, name, entry, check):
        reads.append(name)
        return original(fd, name, entry, check)
    monkeypatch.setattr(ra, '_read_back', count)
    outcome = run(w)
    print(json.dumps({'case': 'legacy-progress', 'resume': resume, 'copies': len(names),
                      'readbacks': len(reads), 'pruned': outcome['pruned']}))
    assert outcome['pruned'] == ['job'], outcome
    assert set(reads) == names and len(reads) == len(names)
    ra.restore(w.root, 'job')
    assert snapshot(wt) == before

@pytest.mark.parametrize('force_copy', [False, True])
def test_source_edits_do_not_change_archive_copy(world, monkeypatch, force_copy):
    w, wt = world
    monkeypatch.setattr(fs, 'FORCE_COPY', force_copy)
    r = archived(w, monkeypatch, False)
    copy, entry = stored(r)
    source = r.q_worktree / REL
    assert (source.stat().st_dev, source.stat().st_ino) != (copy.stat().st_dev, copy.stat().st_ino)
    assert copy.stat().st_nlink == 1
    method = 'copies' if force_copy else 'clones'
    assert r.manifest()['totals'][method] > 0
    source.write_bytes(b'rewritten source')
    assert copy.read_bytes() == DATA
    copy.write_bytes(b'rewritten archive')
    assert source.read_bytes() == b'rewritten source'
    print(json.dumps({'case': 'copy-isolation', 'force_copy': force_copy, 'method': method}))

@pytest.mark.parametrize('upgrade', ['legacy', 'changed'])
def test_final_readback_persists_progress_across_cancelled_restarts(world, monkeypatch, upgrade):
    w, wt = world
    r = archived(w, monkeypatch, True)
    progress = r.building / ra.PROGRESS
    records = [json.loads(line) for line in progress.read_text().splitlines()]
    for record in records:
        if record['k'].startswith('v:'):
            if upgrade == 'legacy':
                record.pop('sig')
            else:
                copy = r.building / 'files' / record['k'][2:]
                st = copy.stat()
                os.utime(copy, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000))
                assert fs.sig_key(copy.stat()) != record['sig']
    progress.write_text(''.join(json.dumps(v) + '\n' for v in records))
    before = progress.read_bytes()
    cancel = threading.Event()
    reads = []
    original = ra._read_back
    def cancel_after_one_verified_copy(fd, name, entry, check):
        result = original(fd, name, entry, check)
        reads.append(name)
        cancel.set()
        return result
    with monkeypatch.context() as patch:
        patch.setattr(ra, '_read_back', cancel_after_one_verified_copy)
        for _ in range(3):
            cancel.clear()
            outcome = run(w, cancel=cancel)
            assert outcome['pruned'] == [] and outcome.get('interrupted'), outcome
            after = progress.read_bytes()
            assert after != before
            latest = json.loads(after.splitlines()[-1])
            assert latest['k'] == 'v:' + reads[-1] and latest['sig'] is not None
            before = after
            w.store.close()
            w.store = Store(w.root / 'state.sqlite3')
    assert len(reads) == 3 and len(set(reads)) == 3
    outcome = run(w)
    assert outcome['pruned'] == ['job'], outcome
    print(json.dumps({'case': 'legacy-cancel-progress', 'restarts': 3,
                      'reread_same_copy': reads, 'uncancelled_pruned': outcome['pruned']}))

def test_ten_thousand_copy_fast_path(tmp_path, monkeypatch):
    building = tmp_path / 'retention' / 'job' / 'archive'
    files = building / 'files'
    files.mkdir(parents=True)
    data = b'x' * 1024
    digest = hashlib.sha256(data).hexdigest()
    entries, records = [], []
    for i in range(10000):
        name = str(i)
        path = files / name
        path.write_bytes(data)
        entries.append({'p': name, 'store': name, 'size': len(data), 'sha256': digest})
        records.append({'k': 'v:' + name, 'sig': fs.sig_key(path.stat()), 'sha256': digest})
    (building / ra.PROGRESS).write_text(''.join(json.dumps(v) + '\n' for v in records))
    r = ra.Retirement(ra.Context(tmp_path, None), 'job')
    manifest = {'trees': {'worktree': {'entries': entries}}}
    original = ra._copy_sig
    stats = []
    def count(fd, name):
        stats.append(name)
        return original(fd, name)
    monkeypatch.setattr(ra, '_copy_sig', count)
    monkeypatch.setattr(ra, '_read_back', lambda *a: pytest.fail('normal pass re-read bytes'))
    durations = []
    for _ in range(3):
        stats.clear()
        start = time.perf_counter()
        r._check_copies(manifest)
        durations.append(time.perf_counter() - start)
        assert len(stats) == 10000 and len(set(stats)) == 10000
    print(json.dumps({'case': '10k-fast-path', 'files': 10000, 'bytes': 10240000,
                      'progress_bytes': (building / ra.PROGRESS).stat().st_size,
                      'seconds': durations, 'stats_per_run': len(stats), 'readbacks': 0}))


def test_damage_inside_final_check_loop_preserves_source_in_conflicts(world, monkeypatch):
    w, wt = world
    (wt / 'out/z-next.bin').write_bytes(b'a later stored copy')
    original_check = ra.Retirement._check_copies
    original_sig = ra._copy_sig
    state = {'seen': False, 'mutated': False}
    def wrap_check(self, manifest):
        copy, entry = stored(self)
        state['retirement'], state['target'] = self, entry['store']
        return original_check(self, manifest)
    def sig_then_lose_earlier_copy(fd, name):
        signature = original_sig(fd, name)
        if name == state['target']:
            state['seen'] = True
        elif state['seen'] and not state['mutated']:
            tamper(state['retirement'], 'lost')
            state['mutated'] = True
        return signature
    monkeypatch.setattr(ra.Retirement, '_check_copies', wrap_check)
    monkeypatch.setattr(ra, '_copy_sig', sig_then_lose_earlier_copy)
    outcome = run(w)
    assert state['mutated']
    assert {c['job_id'] for c in outcome['conflicts']} == {'job'}, outcome
    assert (w.root / 'retention-conflicts/job/worktree' / REL).read_bytes() == DATA


@pytest.mark.parametrize('unusable', ['permissions', 'symlink', 'directory', 'fifo', 'read-error'])
def test_unusable_kept_copy_is_evicted_and_rebuilt(world, monkeypatch, unusable):
    w, wt = world
    before = snapshot(wt)
    wall = [time.time()]
    monkeypatch.setattr(ra.time, 'time', lambda: wall[0])
    def defer(self):
        raise ra.Defer('review cache deferral', ra.DEFER_CHANGED_S)
    with monkeypatch.context() as once:
        once.setattr(ra.Retirement, 'final_check', defer)
        assert run(w)['pruned'] == []
    r = ra.Retirement(ra.Context(w.root, w.store), 'job')
    copy, _ = stored(r)
    if unusable == 'permissions':
        copy.chmod(0)
    elif unusable == 'symlink':
        copy.unlink()
        copy.symlink_to(wt / REL)
    elif unusable == 'directory':
        copy.unlink()
        copy.mkdir()
        (copy / 'junk').write_bytes(b'unusable cache')
    elif unusable == 'fifo':
        copy.unlink()
        os.mkfifo(copy)
    else:
        copy.touch()  # invalidate the recorded identity so it must read back
        original = fs.read_hashes
        inode = copy.stat().st_ino
        failed = []
        def fail_read(fd, *args, **kwargs):
            if os.fstat(fd).st_ino == inode and not failed:
                failed.append(True)
                raise OSError(5, 'injected copy read error')
            return original(fd, *args, **kwargs)
        monkeypatch.setattr(fs, 'read_hashes', fail_read)
    for _ in range(2):
        wall[0] = ra.load_journal(w.root, 'job')['defer_until'] + 1
        outcome = run(w)
        if outcome['pruned']:
            break
        assert outcome['protected'] == ['job'], outcome
        assert snapshot(wt) == before
        assert not os.path.lexists(copy), 'a failed cached readback must evict the copy'
    assert outcome['pruned'] == ['job'], outcome
    assert ra.check_archive(w.root, 'job')['ok']
    ra.restore(w.root, 'job')
    assert snapshot(wt) == before


def test_damage_after_publish_and_restart_preserves_source(world, monkeypatch):
    w, wt = world
    with monkeypatch.context() as once:
        once.setattr(ra.Retirement, 'reclaim', lambda self: (_ for _ in ()).throw(ra.Interrupted('restart')))
        outcome = run(w)
    assert outcome.get('interrupted') and w.store.get_job('job') is None, outcome
    w.store.close()
    w.store = Store(w.root / 'state.sqlite3')
    r = ra.Retirement(ra.Context(w.root, w.store), 'job')
    entry = next(e for e in r.manifest()['trees']['worktree']['entries'] if e['p'] == REL)
    (r.published_dir() / 'files' / entry['store']).unlink()
    outcome = run(w)
    assert {c['job_id'] for c in outcome['conflicts']} == {'job'}, outcome
    assert (w.root / 'retention-conflicts/job/worktree' / REL).read_bytes() == DATA
    assert not wt.exists()


def test_missing_property_mutation_unlinks_copy_with_manifest_intact(world, monkeypatch):
    from tests.unit.test_retention_salvage_properties import _tamper_saved
    w, _ = world
    r = archived(w, monkeypatch, True)
    copy, _ = stored(r)
    manifest = (r.building / 'manifest.json').read_bytes()
    _tamper_saved(r, REL, 'missing')
    assert not copy.exists()
    assert (r.building / 'manifest.json').read_bytes() == manifest


def test_each_source_hardlink_rechecks_shared_stored_copy(world, monkeypatch):
    w, wt = world
    other_rel = 'out/private-link.bin'
    os.link(wt / REL, wt / other_rel)
    original = fs.Reclaim._one
    def damage_before_second_link(self, fd, rel, name):
        if fs.join(rel, name) == REL and self.label == 'worktree':
            # private-link.bin sorts first and has already been unlinked.
            r = ra.Retirement(ra.Context(w.root, w.store), 'job')
            entry = self.entries[REL]
            (r.published_dir() / 'files' / entry['store']).unlink()
        return original(self, fd, rel, name)
    monkeypatch.setattr(fs.Reclaim, '_one', damage_before_second_link)
    outcome = run(w)
    assert {c['job_id'] for c in outcome['conflicts']} == {'job'}, outcome
    assert (w.root / 'retention-conflicts/job/worktree' / REL).read_bytes() == DATA


def test_source_write_during_copy_readback_is_preserved(world, monkeypatch):
    w, _ = world
    original_publish = ra.Retirement.publish
    target = []
    def publish_then_change_copy_identity(self):
        original_publish(self)
        entry = next(e for e in self.manifest()['trees']['worktree']['entries'] if e['p'] == REL)
        copy = self.published_dir() / 'files' / entry['store']
        copy.touch()  # still good bytes, but reclamation must re-read them
        target.append((entry['store'], self.q_worktree / REL))
    original_read = ra._read_back
    new = b'new source bytes during the slow archive readback'
    def read_then_write_source(fd, name, entry, check):
        result = original_read(fd, name, entry, check)
        if target and name == target[0][0]:
            target[0][1].write_bytes(new)
        return result
    monkeypatch.setattr(ra.Retirement, 'publish', publish_then_change_copy_identity)
    monkeypatch.setattr(ra, '_read_back', read_then_write_source)
    outcome = run(w)
    assert {c['job_id'] for c in outcome['conflicts']} == {'job'}, outcome
    assert (w.root / 'retention-conflicts/job/worktree' / REL).read_bytes() == new
