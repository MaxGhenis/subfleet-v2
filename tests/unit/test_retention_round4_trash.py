"""Round-four trash deletion and registration-generation regressions."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from subfleet import retention, retention_trash
from test_retention_worktrees import owned, git


def retired(owned):
    store, root, _, tree = owned
    common = git(tree, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    admin = Path(git(tree, 'rev-parse', '--absolute-git-dir'))
    target = retention_trash.prepare(root, 'job', tree, common, 100)
    retention_trash.stage(root, 'job', tree, target)
    with store.transaction() as tx:
        tx.execute("DELETE FROM jobs WHERE job_id='job'")
    return store, root, tree, admin, target


def clean(store, root):
    result = {'errors': [], 'made_progress': False}
    retention_trash.clean(store, root, checkpoint=lambda: None, git=None, progress=result)
    return result


def test_manifest_records_registration_generation_and_exact_backlink(owned):
    _, _, _, admin, target = retired(owned)
    data = json.loads((target / 'manifest.json').read_text())
    assert data['admin'] == str(admin)
    assert data['admin_inode'] == admin.stat().st_ino
    assert data['admin_device'] == admin.stat().st_dev
    assert bytes.fromhex(data['admin_gitdir']) == (admin / 'gitdir').read_bytes()


@pytest.mark.parametrize('change', ['inode', 'contents'])
def test_replacement_registration_is_preserved(owned, change):
    store, root, tree, admin, target = retired(owned)
    before = (admin / 'gitdir').read_bytes()
    if change == 'inode':
        admin.rename(admin.with_name(admin.name + '-original'))
        admin.mkdir()
        (admin / 'gitdir').write_bytes(before)
    else:
        (admin / 'gitdir').write_bytes(before.rstrip(b'\n') + b'\n\n')
    (admin / 'index').write_bytes(b'unique staged content')
    result = clean(store, root)
    assert result['errors']
    assert (admin / 'index').read_bytes() == b'unique staged content'
    assert target.exists()


def test_registration_replaced_during_detach_is_restored(owned, monkeypatch):
    store, root, _, admin, target = retired(owned)
    rename = Path.rename
    def replace(source, destination):
        if source == admin:
            backlink = (admin / 'gitdir').read_bytes()
            rename(admin, admin.with_name(admin.name + '-original'))
            admin.mkdir()
            (admin / 'gitdir').write_bytes(backlink)
            (admin / 'index').write_bytes(b'unique staged replacement')
        return rename(source, destination)
    monkeypatch.setattr(Path, 'rename', replace)
    result = clean(store, root)
    assert result['errors']
    assert (admin / 'index').read_bytes() == b'unique staged replacement'
    assert not (target / 'admin').exists()


def test_resumed_cleanup_never_touches_original_path_replacement(owned, monkeypatch):
    store, root, _, admin, target = retired(owned)
    original = retention_trash._reclaim
    def interrupt(path, checkpoint, progress):
        if path == target / 'admin':
            (path / 'gitdir').unlink()
            raise retention._Interrupted('deadline')
        return original(path, checkpoint, progress)
    monkeypatch.setattr(retention_trash, '_reclaim', interrupt)
    with pytest.raises(retention._Interrupted):
        clean(store, root)
    admin.mkdir()
    (admin / 'gitdir').write_text(str(root / 'worktrees/job/.git'))
    (admin / 'index').write_bytes(b'new generation')
    monkeypatch.setattr(retention_trash, '_reclaim', original)
    assert not clean(store, root)['errors']
    assert not target.exists()
    assert (admin / 'index').read_bytes() == b'new generation'


def test_rmtree_subprocess_is_stopped_and_reaped_at_checkpoint(tmp_path, monkeypatch):
    directory = tmp_path / 'trash'
    directory.mkdir()
    (directory / 'payload').write_text('still present')
    children = []
    popen = subprocess.Popen
    def slow_child(*args, **kwargs):
        child = popen([retention_trash.sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(retention_trash.subprocess, 'Popen', slow_child)
    calls = 0
    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise retention._Interrupted('deadline')
    with pytest.raises(retention._Interrupted):
        retention_trash._reclaim(directory, checkpoint, {'made_progress': False})
    assert children and children[0].poll() is not None
    assert (directory / 'payload').read_text() == 'still present'


def test_rmtree_child_ancestor_symlink_swap_preserves_outside_data(tmp_path, monkeypatch):
    tree, outside = tmp_path / 'trash', tmp_path / 'outside'
    child = tree / 'cache'
    child.mkdir(parents=True)
    outside.mkdir()
    (child / 'payload').write_text('discardable')
    (outside / 'payload').write_text('unique outside content')
    child_inode = child.stat().st_ino
    scandir, rmtree = os.scandir, shutil.rmtree
    deleting = False
    replaced = False
    @contextmanager
    def swap_after_enumeration(descriptor):
        nonlocal replaced
        with scandir(descriptor) as iterator:
            entries = list(iterator)
        if deleting and not replaced and os.fstat(descriptor).st_ino == child_inode:
            replaced = True
            child.rename(tmp_path / 'moved-owned-cache')
            child.symlink_to(outside, target_is_directory=True)
        yield iter(entries)
    def scan(descriptor):
        return swap_after_enumeration(descriptor) if deleting else scandir(descriptor)
    def remove(*args, **kwargs):
        nonlocal deleting
        deleting = True
        return rmtree(*args, **kwargs)
    remove.avoids_symlink_attacks = True
    monkeypatch.setattr(os, 'scandir', scan)
    monkeypatch.setattr(shutil, 'rmtree', remove)
    with pytest.raises(OSError):
        retention_trash._rmtree(tree)
    assert replaced
    assert (outside / 'payload').read_text() == 'unique outside content'


def test_registration_replaced_during_stage_pins_and_restores_job(owned, monkeypatch):
    store, root, _, tree = owned
    store.update_job('job', workdir_head=git(tree, 'rev-parse', 'HEAD'))
    admin = Path(git(tree, 'rev-parse', '--absolute-git-dir'))
    stage = retention_trash.stage
    def replace(*args, **kwargs):
        stage(*args, **kwargs)
        saved = admin.with_name(admin.name + '-original')
        admin.rename(saved)
        shutil.copytree(saved, admin)
        (admin / 'replacement-marker').write_bytes(b'new registration')
    monkeypatch.setattr(retention_trash, 'stage', replace)
    result = retention.maintenance(store, root, max_jobs=0)
    assert result['pruned'] == [] and store.get_job('job') is not None
    assert any('condition 9' in error['error'] for error in result['errors'])
    assert tree.exists() and not (root / 'trash/job').exists()
    assert (admin / 'replacement-marker').read_bytes() == b'new registration'


def test_interrupted_child_reports_partial_reclamation(tmp_path, monkeypatch):
    directory = tmp_path / 'trash'
    directory.mkdir()
    first, second = directory / 'first', directory / 'second'
    first.write_text('discardable')
    second.write_text('remaining')
    popen = subprocess.Popen
    def partial_child(*args, **kwargs):
        script = 'import os,sys,time; os.unlink(sys.argv[1]); os.write(1,b"+"); time.sleep(60)'
        return popen([retention_trash.sys.executable, '-c', script, str(first)], **kwargs)
    monkeypatch.setattr(retention_trash.subprocess, 'Popen', partial_child)
    progress = {'made_progress': False}
    calls = 0
    def checkpoint():
        nonlocal calls
        calls += 1
        assert calls < 200, 'child did not report progress'
        if progress['made_progress']:
            raise retention._Interrupted('deadline')
    with pytest.raises(retention._Interrupted):
        retention_trash._reclaim(directory, checkpoint, progress)
    assert progress['made_progress'] and not first.exists() and second.exists()


def test_rmtree_progress_marker_follows_successful_deletion(tmp_path, capfd):
    directory = tmp_path / 'trash'
    directory.mkdir()
    for name in ('one', 'two'):
        (directory / name).write_text('discardable')
    retention_trash._rmtree(directory, report_progress=True)
    assert not directory.exists()
    assert capfd.readouterr().out == '+'


@pytest.mark.parametrize('seam', ['after-untracked-scan', 'during-raw-hash'])
def test_post_stage_output_with_restored_directory_mtime_is_restored(owned, monkeypatch, seam):
    from subfleet import retention_salvage
    store, root, _, tree = owned
    store.update_job('job', workdir_head=git(tree, 'rev-parse', 'HEAD'))
    retired_tree = root / 'trash/job/worktree'
    changed = False
    def write_output():
        nonlocal changed
        if changed:
            return
        changed = True
        metadata = retired_tree.stat()
        (retired_tree / 'unique-late-output').write_bytes(b'irreplaceable analysis')
        os.utime(retired_tree, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    if seam == 'after-untracked-scan':
        inspect = retention_salvage._git
        def late_output(directory, *args, **kwargs):
            result = inspect(directory, *args, **kwargs)
            if directory == retired_tree and 'ls-files' in args and '-o' in args and '-i' not in args:
                write_output()
            return result
        monkeypatch.setattr(retention_salvage, '_git', late_output)
    else:
        inspect = retention_salvage._raw_blob
        def late_output(path, *args, **kwargs):
            result = inspect(path, *args, **kwargs)
            if path.is_relative_to(retired_tree):
                write_output()
            return result
        monkeypatch.setattr(retention_salvage, '_raw_blob', late_output)
    result = retention.maintenance(store, root, max_jobs=0)
    assert changed and result['pruned'] == []
    assert any('condition 5' in error['error'] for error in result['errors'])
    assert (tree / 'unique-late-output').read_bytes() == b'irreplaceable analysis'
    assert not (root / 'trash/job').exists() and not store.list_leases()


def test_child_refuses_trash_ancestor_swapped_before_start(owned, tmp_path, monkeypatch):
    store, root, _, _, _ = retired(owned)
    external = tmp_path / 'outside'
    payload = external / 'job/admin/unique-staged-content'
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b'outside registration contents')
    popen = subprocess.Popen
    changed = False
    def swap_before_child(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            (root / 'trash').rename(root / 'original-trash')
            (root / 'trash').symlink_to(external, target_is_directory=True)
        return popen(*args, **kwargs)
    monkeypatch.setattr(retention_trash.subprocess, 'Popen', swap_before_child)
    result = clean(store, root)
    assert changed and result['errors']
    assert payload.read_bytes() == b'outside registration contents'


def test_private_ref_added_during_post_stage_hash_pins_and_restores_job(owned, monkeypatch):
    from subfleet import retention_salvage
    store, root, repo, tree = owned
    baseline = git(tree, 'rev-parse', 'HEAD')
    store.update_job('job', workdir_head=baseline)
    private_commit = git(repo, '-c', 'user.name=test', '-c', 'user.email=t@x',
                         'commit-tree', git(repo, 'rev-parse', 'HEAD^{tree}'),
                         '-p', baseline, '-m', 'unpublished experiment')
    retired_tree = root / 'trash/job/worktree'
    inspect = retention_salvage._raw_blob
    changed = False
    def add_private_ref(path, *args, **kwargs):
        nonlocal changed
        result = inspect(path, *args, **kwargs)
        if not changed and path.is_relative_to(retired_tree):
            changed = True
            git(retired_tree, 'update-ref', 'refs/worktree/scratch', private_commit)
        return result
    monkeypatch.setattr(retention_salvage, '_raw_blob', add_private_ref)
    result = retention.maintenance(store, root, max_jobs=0)
    assert changed and result['pruned'] == []
    assert any('condition 6' in error['error'] for error in result['errors'])
    assert git(tree, 'rev-parse', 'refs/worktree/scratch') == private_commit
    assert not (root / 'trash/job').exists() and not store.list_leases()
