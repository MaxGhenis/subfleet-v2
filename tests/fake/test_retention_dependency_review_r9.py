"""R9-F1: firmlink spelling must not hide physical metadata dependencies."""
import json
from pathlib import Path
import shutil

import pytest

from subfleet import dependencies, folders
from tests.fake.test_retention_dependency_review_r2 import world, reader, maintain
from tests.fake.test_retention_dependency_review_r5 import source
from tests.unit.test_retention_location_inputs import git, repo


def data_alias(path):
    path = path.resolve()
    alias = Path('/System/Volumes/Data') / path.relative_to('/')
    if not alias.exists():
        pytest.skip('requires the macOS Data-volume firmlink')
    assert alias.stat().st_ino == path.stat().st_ino
    assert folders.canonical(alias) == str(path)
    return alias


@pytest.mark.xfail(strict=True, reason='R9-F1: auxiliary metadata locations retain a Data-volume alias that misses the canonical owner tree')
@pytest.mark.parametrize('layout', ['gitfile', 'commondir', 'alternates', 'objects-pack'])
def test_firmlink_metadata_location_keeps_the_tree_real_git_reads(world, layout, record_property):
    tree = world.job('owner').resolve()
    owner, commit = source(world, tree)
    stable = repo(world.base / 'stable')
    assert git('-C', stable, 'fetch', '-q', owner, 'HEAD').returncode == 0
    checkout = world.base / 'outside'
    checkout.mkdir()
    shutil.copy2(stable / 'f.txt', checkout / 'f.txt')
    pointer = checkout / '.git'
    if layout == 'gitfile':
        physical = tree / 'pointer'
        physical.write_text('gitdir: ' + str(stable / '.git') + '\n')
        pointer.symlink_to(data_alias(physical))
    elif layout == 'commondir':
        admin = world.base / 'admin'
        shutil.copytree(stable / '.git', admin)
        pointer.write_text('gitdir: ' + str(admin) + '\n')
        physical = tree / 'pointer'
        physical.write_text(str(stable / '.git') + '\n')
        (admin / 'commondir').symlink_to(data_alias(physical))
    elif layout == 'alternates':
        admin = world.base / 'admin'
        shutil.copytree(stable / '.git', admin)
        pointer.write_text('gitdir: ' + str(admin) + '\n')
        shutil.rmtree(admin / 'objects')
        (admin / 'objects' / 'info').mkdir(parents=True)
        physical = tree / 'pointer'
        physical.write_text(str(stable / '.git' / 'objects') + '\n')
        (admin / 'objects' / 'info' / 'alternates').symlink_to(data_alias(physical))
    else:
        pointer.write_text('gitdir: ' + str(stable / '.git') + '\n')
        assert git('-C', stable, 'reset', '--hard', commit).returncode == 0
        assert git('-C', stable, 'gc', '--prune=now').returncode == 0
        shutil.copy2(stable / 'f.txt', checkout / 'f.txt')
        shutil.copy2(stable / 'owner-only.txt', checkout / 'owner-only.txt')
        physical = tree / 'packs'
        (stable / '.git' / 'objects' / 'pack').rename(physical)
        (stable / '.git' / 'objects' / 'pack').symlink_to(data_alias(physical))
    query = ('HEAD' if layout == 'objects-pack' else commit) + ':owner-only.txt'
    before = git('-C', checkout, 'show', query)
    health = git('-C', checkout, 'fsck', '--no-dangling')
    assert before.returncode == health.returncode == 0, (before.stderr, health.stderr)
    assert before.stdout == b'R5 owner-only storage\n'
    status = git('-C', checkout, 'status', '--porcelain')
    assert status.returncode == 0 and status.stdout == b'', status.stderr
    recorded = dependencies.record(str(checkout))
    assert not recorded.get('incomplete'), recorded
    reader(world, recorded)
    result = maintain(world)
    after = git('-C', checkout, 'show', query)
    record_property('R9-F1', json.dumps(dict(layout=layout, physical=str(physical),
        alias=str(data_alias(physical)) if physical.exists() else '/System/Volumes/Data' + str(physical),
        recorded=recorded, result=result, before=before.returncode, after=after.returncode,
        error_after=after.stderr.decode()), sort_keys=True))
    assert 'owner' not in result['pruned'] and tree.is_dir(), (recorded, result, after.stderr)
    assert after.returncode == 0 and after.stdout == before.stdout, after.stderr
