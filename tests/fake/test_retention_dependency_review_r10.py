"""C-8.4: independent review r10 spelling and home-firmlink controls."""
from pathlib import Path
import shutil

import pytest

from subfleet import procs
from tests.unit.retention_world import World
from tests.unit.test_retention_location_spelling import spelling_path
from tests.fake import test_retention_dependency_review_r9 as r9


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(procs, 'worktree_cwd_pids', lambda *a, **k: frozenset())
    base = tmp_path.resolve() / 'résumé'
    base.mkdir()
    value = World(base)
    yield value
    value.close()


@pytest.mark.parametrize('spelling', ['other-case', 'other-normalization'])
@pytest.mark.parametrize('layout', ['gitfile', 'commondir', 'alternates', 'objects-pack'])
def test_other_spelling_preserves_owner_through_retention(world, monkeypatch, layout, spelling, record_property):
    def alternate(path):
        alias = spelling_path(Path(path), spelling)
        if alias is None:
            pytest.skip('volume rejects ' + spelling)
        return alias
    monkeypatch.setattr(r9, 'data_alias', alternate)
    r9.test_firmlink_metadata_location_keeps_the_tree_real_git_reads(world, layout, record_property)


@pytest.mark.parametrize('layout', ['gitfile', 'objects-pack'])
def test_users_home_firmlink_preserves_owner_through_retention(monkeypatch, layout, record_property):
    monkeypatch.setattr(procs, 'worktree_cwd_pids', lambda *a, **k: frozenset())
    base = Path(__file__).resolve().parents[2] / 'sf-r272r10-fixtures' / layout
    base.mkdir(parents=True)
    world = World(base)
    try:
        r9.test_firmlink_metadata_location_keeps_the_tree_real_git_reads(world, layout, record_property)
    finally:
        world.close()
        shutil.rmtree(base)


@pytest.mark.xfail(strict=True, reason='R10-F1: returned scan targets in suffix-growing symlink cycles violate the new spelling invariant')
@pytest.mark.parametrize('cycle', ['self-suffix', 'two-link-suffix'])
def test_returned_cycle_target_obeys_the_claimed_spelling_invariant(tmp_path, cycle, record_property):
    import json
    from subfleet import dependencies, folders
    from tests.unit.test_retention_location_inputs import git, repo
    from tests.unit.test_retention_location_spelling import assert_location_spelling

    base = tmp_path.resolve()
    checkout = repo(base / 'checkout')
    loop = base / 'loop'
    if cycle == 'self-suffix':
        loop.symlink_to('loop/tail')
    else:
        loop.symlink_to('other/tail')
        (base / 'other').symlink_to('loop/extra')
    (checkout / '.git' / 'unused').symlink_to(loop)
    health = git('-C', checkout, 'fsck', '--no-dangling')
    status = git('-C', checkout, 'status', '--porcelain')
    read = git('-C', checkout, 'show', 'HEAD:f.txt')
    assert health.returncode == status.returncode == read.returncode == 0
    assert status.stdout == b'' and read.stdout == b'checkout'
    recorded = dependencies.record(str(checkout))
    assert not recorded.get('incomplete'), recorded
    record_property('R10-F1', json.dumps(dict(cycle=cycle, recorded=recorded,
        respelled={p: folders.canonical(p) for p in recorded.get('git', [])},
        fsck=health.returncode, status=status.returncode, read=read.returncode), sort_keys=True))
    assert_location_spelling(checkout, recorded.get('git', []))
