"""Round-three preservation and recovery regressions; real temporary Git/SQLite."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import retention, retention_salvage, retention_trash
from test_retention_worktrees import owned, git
from test_retention_salvage import snapshot
from test_retention_recovery_properties import case, SimulatedProcessDeath
from test_retention_progress import add_job, MIB
from subfleet.store import Store


@pytest.mark.parametrize("name", ["build", "dist", ".next", "venv"])
def test_output_directories_are_not_caches(owned, name):
    store, root, repo, tree = owned
    (repo / '.git/info/exclude').write_text(name + '/\n')
    (tree / name).mkdir()
    (tree / name / 'sole-output').write_bytes(b'irreplaceable')
    result = retention.maintenance(store, root, max_jobs=0)
    assert result['pruned'] == [] and result['errors']
    assert (tree / name / 'sole-output').read_bytes() == b'irreplaceable'


@settings(max_examples=12, deadline=None, database=None)
@given(parent=st.sampled_from(['build', 'node_modules', '.venv', 'thing.egg-info']),
       payload=st.binary(max_size=64))
def test_ignored_file_below_tracked_cache_named_package_is_never_discarded(parent, payload):
    with case(owned=True) as (store, root, repo, tree, _):
        package = tree / 'packages/microcosm' / parent / 'us_runtime/data'
        package.mkdir(parents=True)
        (package / 'module.py').write_text('# real namespace package')
        (tree / '.gitignore').write_text('*.h5\n')
        git(tree, 'add', '.')
        git(tree, '-c', 'user.name=test', '-c', 'user.email=t@x', 'commit', '-m', 'package')
        git(repo, 'update-ref', 'refs/heads/held', git(tree, 'rev-parse', 'HEAD'))
        store.update_job('job', workdir_head=git(tree, 'rev-parse', 'HEAD'))
        output = package / 'calibrated.h5'
        output.write_bytes(payload)
        result = retention.maintenance(store, root, max_jobs=0)
        assert result['pruned'] == [] and 'ignored' in result['errors'][0]['error']
        assert output.read_bytes() == payload and not store.list_leases()


@pytest.mark.parametrize('name', ['private.git', 'bare-store', '.GIT'])
def test_nested_repositories_inside_ignored_caches_are_pinned(owned, name):
    store, root, repo, tree = owned
    (repo / '.git/info/exclude').write_text('__pycache__/\n')
    nested = tree / '__pycache__/pkg' / name
    nested.mkdir(parents=True)
    git(nested, 'init', '--bare')
    result = retention.maintenance(store, root, max_jobs=0)
    assert result['pruned'] == [] and 'Git' in result['errors'][0]['error']
    assert (nested / 'HEAD').exists()


def test_unheld_baseline_does_not_authorize_retirement(owned):
    store, root, repo, tree = owned
    (tree / 'tracked').write_text('detached baseline')
    git(tree, 'add', '.')
    git(tree, '-c', 'user.name=t', '-c', 'user.email=t@x', 'commit', '-m', 'unheld')
    head = git(tree, 'rev-parse', 'HEAD')
    store.update_job('job', workdir_head=head)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result['pruned'] == [] and 'allowed named ref' in result['errors'][0]['error']
    assert git(tree, 'rev-parse', 'HEAD') == head


@pytest.mark.parametrize('unmerged', [False, True])
def test_index_only_content_is_preserved(owned, unmerged):
    store, root, repo, tree = owned
    baseline = (tree / 'tracked').read_bytes()
    (tree / 'tracked').write_bytes(b'unique staged bytes')
    git(tree, 'add', 'tracked')
    blob = git(tree, 'rev-parse', ':tracked')
    if unmerged:
        git(tree, 'update-index', '--force-remove', 'tracked')
        subprocess.run(['git', '-C', str(tree), 'update-index', '--index-info'],
                       input=f'100644 {blob} 1\ttracked\n100644 {blob} 2\ttracked\n',
                       text=True, check=True)
    (tree / 'tracked').write_bytes(baseline)
    index = Path(git(tree, 'rev-parse', '--git-path', 'index'))
    before = index.read_bytes()
    result = retention.maintenance(store, root, max_jobs=0)
    assert result['pruned'] == [] and 'index content' in result['errors'][0]['error']
    assert index.read_bytes() == before and git(repo, 'cat-file', '-p', blob) == 'unique staged bytes'


def test_cleanup_keeps_unrelated_missing_worktree_registration(owned, tmp_path):
    store, root, repo, tree = owned
    other = tmp_path / 'operator-checkout'
    git(repo, 'worktree', 'add', '--detach', str(other), 'HEAD')
    admin = Path(git(other, 'rev-parse', '--absolute-git-dir'))
    evidence = {p.name: p.read_bytes() for p in admin.iterdir() if p.is_file()}
    other.rename(tmp_path / 'temporarily-relocated')
    selected_admin = Path(git(tree, 'rev-parse', '--absolute-git-dir'))
    result = retention.maintenance(store, root, max_jobs=0)
    assert result['pruned'] == ['job'] and not selected_admin.exists()
    assert {p.name: p.read_bytes() for p in admin.iterdir() if p.is_file()} == evidence


def test_new_crash_lease_never_gets_deletion_waiver(owned, monkeypatch):
    store, root, _, tree = owned
    (tree / 'tracked').unlink()
    with monkeypatch.context() as patch:
        def crash(*args, **kwargs):
            raise SimulatedProcessDeath()
        patch.setattr(retention, '_remove_worktree', crash)
        with pytest.raises(SimulatedProcessDeath):
            retention.maintenance(store, root, max_jobs=0)
    assert any(lease['lease_key'].startswith(retention._LEASE_MARKER) for lease in store.list_leases())
    result = retention.maintenance(store, root, max_jobs=100)
    assert result['pruned'] == [] and tree.exists()
    assert not (tree / 'tracked').exists() and not store.list_leases()


def test_deep_file_edit_invalidates_cached_size_without_directory_mtime_change(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=0.)
    monkeypatch.setattr(retention, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    with Store(tmp_path / 'db') as store:
        add_job(store, tmp_path, 'j1', order=0, size=MIB)
        big = add_job(store, tmp_path, 'j2', order=1, size=3*MIB)
        add_job(store, tmp_path, 'j3', order=2, size=MIB)
        original = retention._size
        def stop(path, **kwargs):
            if Path(path).name == 'j3':
                raise retention._Interrupted('deadline')
            return original(path, **kwargs)
        monkeypatch.setattr(retention, '_size', stop)
        assert retention.maintenance(store, tmp_path, max_bytes=10*MIB)['interrupted']
        before = big.stat().st_mtime_ns
        with (big / 'part-0/payload-0.bin').open('wb') as file:
            file.truncate(MIB)
        assert big.stat().st_mtime_ns == before
        clock.now += 5
        monkeypatch.setattr(retention, '_size', original)
        result = retention.maintenance(store, tmp_path, max_bytes=4*MIB)
        assert result['pruned'] == [] and result['bytes_after'] == 3*MIB


def test_deadline_git_timeout_is_interruption(monkeypatch, tmp_path):
    clock = SimpleNamespace(now=0.)
    monkeypatch.setattr(retention, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    def timeout(*args, **kwargs):
        clock.now = 2
        raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
    monkeypatch.setattr(retention.subprocess, 'run', timeout)
    with pytest.raises(retention._Interrupted, match='deadline'):
        retention._git(tmp_path, 'status', deadline=1)


@pytest.mark.parametrize('flag', ['--assume-unchanged', '--skip-worktree'])
def test_fresh_proof_ignores_hiding_flags_without_changing_real_index(owned, monkeypatch, flag):
    store, root, _, tree = owned
    git(tree, 'update-index', flag, 'tracked')
    index = Path(git(tree, 'rev-parse', '--git-path', 'index'))
    before = index.read_bytes()
    (tree / 'tracked').write_text('hidden unsalvaged edit')
    result = retention.maintenance(store, root, max_jobs=0)
    assert result['pruned'] == [] and index.read_bytes() == before
    assert (tree / 'tracked').read_text() == 'hidden unsalvaged edit'


@settings(max_examples=10, deadline=None, database=None)
@given(payload=st.binary(min_size=1, max_size=100))
def test_restore_collision_preserves_both_versions_and_releases_lease(payload):
    with case() as (store, root, _, _, directory):
        checks = 0
        def pins():
            nonlocal checks
            if store.connection.in_transaction:
                checks += 1
            if checks == 2:
                directory.mkdir(exist_ok=True)
                (directory / 'stdout').write_bytes(payload)
                return {'job'}
            return set()
        result = retention.maintenance(store, root, max_jobs=0, pins=pins)
        assert result['pruned'] == [] and not store.list_leases()
        assert (directory / 'stdout').read_bytes() == b'job output'
        assert [p.read_bytes() for p in (root / 'retention-conflicts/job').glob('job-*/stdout')] == [payload]


def test_readonly_deep_trash_is_iterative_and_reclaimed(tmp_path):
    with case() as (store, root, _, _, directory):
        current = directory
        for _ in range(160):
            current = current / 'd'
            current.mkdir()
        (current / 'payload').write_text('cache')
        current.chmod(0o500)
        target = retention_trash.prepare(root, 'job', None, None, 5)
        retention_trash.stage(root, 'job', None, target)
        with store.transaction() as tx:
            tx.execute("DELETE FROM jobs WHERE job_id='job'")
        previous = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(150)
            progress = {'errors': [], 'made_progress': False}
            retention_trash.clean(store, root, checkpoint=lambda: None, git=None, progress=progress)
        finally:
            sys.setrecursionlimit(previous)
        assert not progress['errors'] and not target.exists()


def test_cleanup_refuses_repointed_selected_registration(owned, tmp_path):
    store, root, repo, tree = owned
    common = git(tree, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    admin = Path(git(tree, 'rev-parse', '--absolute-git-dir'))
    target = retention_trash.prepare(root, 'job', tree, common, 100)
    retention_trash.stage(root, 'job', tree, target)
    with store.transaction() as tx:
        tx.execute("DELETE FROM jobs WHERE job_id='job'")
    (admin / 'gitdir').write_text(str(tmp_path / 'new-owner/.git'))
    progress = {'errors': [], 'made_progress': False}
    retention_trash.clean(store, root, checkpoint=lambda: None, git=None, progress=progress)
    assert admin.exists() and target.exists()
    assert 'another worktree' in progress['errors'][0]['error']


def test_version_marker_collision_cannot_create_unmarked_new_lease():
    with case() as (store, root, _, _, directory):
        store.acquire_lease(retention._LEASE_MARKER + 'job', 'another-owner')
        result = retention.maintenance(store, root, max_jobs=0)
        assert result['pruned'] == [] and directory.exists()
        assert 'another owner' in result['errors'][0]['error']
        assert not store.list_leases('retention:job')


def test_selected_admin_on_another_filesystem_can_resume_cleanup(owned, monkeypatch):
    store, root, _, tree = owned
    common = git(tree, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    admin = Path(git(tree, 'rev-parse', '--absolute-git-dir'))
    target = retention_trash.prepare(root, 'job', tree, common, 100)
    retention_trash.stage(root, 'job', tree, target)
    with store.transaction() as tx:
        tx.execute("DELETE FROM jobs WHERE job_id='job'")
    original_stat, reclaim = Path.stat, retention_trash._reclaim
    def separate_device(path, *args, **kwargs):
        metadata = original_stat(path, *args, **kwargs)
        return SimpleNamespace(st_dev=metadata.st_dev + 1) if path == target else metadata
    def interrupt(path, checkpoint, progress):
        raise retention._Interrupted('cancelled')
    monkeypatch.setattr(Path, 'stat', separate_device)
    monkeypatch.setattr(retention_trash, '_reclaim', interrupt)
    progress = {'errors': [], 'made_progress': False}
    with pytest.raises(retention._Interrupted):
        retention_trash.clean(store, root, checkpoint=lambda: None, git=None, progress=progress)
    import json
    manifest = json.loads((target / 'manifest.json').read_text())
    detached = Path(manifest['admin_retired'])
    assert detached.parent == admin.parent and detached != admin
    assert (detached / 'gitdir').exists() and not admin.exists()
    monkeypatch.setattr(retention_trash, '_reclaim', reclaim)
    retention_trash.clean(store, root, checkpoint=lambda: None, git=None, progress=progress)
    assert not progress['errors'] and not detached.exists() and not target.exists()


@pytest.mark.parametrize("seconds", [120, 0])
def test_live_survey_uses_real_readonly_git_and_sqlite(snapshot, tmp_path, seconds):
    import json
    store, root, repo, tree, saved = snapshot
    git(tree, 'reset', '--hard', saved.commit)
    (root / 'policy.json').write_text(json.dumps({'retention': {'jobs': 0}}))
    index = Path(git(tree, 'rev-parse', '--git-path', 'index'))
    before = index.read_bytes()
    objects = sorted(str(p.relative_to(repo / '.git/objects')) for p in (repo / '.git/objects').rglob('*'))
    output = tmp_path / 'survey.json'
    subprocess.run([sys.executable, 'tools/retention_live_survey.py', '--state', str(root),
                    '--output', str(output), '--candidate-seconds', str(seconds)], check=True, capture_output=True, text=True, timeout=120)
    report = json.loads(output.read_text())
    if seconds:
        assert report['summary']['retirable_worktrees'] == 1 and not report['summary']['blockers']
    else:
        assert report['summary']['retirable_worktrees'] == 0
        assert report['summary']['blockers'] == {'survey-timeout': 1}
    assert index.read_bytes() == before and store.get_job('job') is not None
    assert sorted(str(p.relative_to(repo / '.git/objects')) for p in (repo / '.git/objects').rglob('*')) == objects
