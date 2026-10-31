"""C-8.4, R11-F1: an accepted case alias stays readable when its spelling changes."""
import inspect
import json
import os
from pathlib import Path
import types

import pytest

from subfleet import dependencies, folders
from tests.fake.test_retention_dependency_review_r2 import world, reader, maintain
from tests.fake.test_retention_dependency_review_r5 import source
from tests.unit.test_retention_location_inputs import git


def baseline_reader():
    module = types.ModuleType('subfleet.review_r11_baseline')
    module.__package__ = 'subfleet'
    snapshot = (Path(__file__).resolve().parents[2] / 'docs' / 'reports' /
                '2026-10-31-retention-pins-fix-r10-data' / 'base-dependencies.py.txt')
    exec(compile(snapshot.read_text(), '475f63278:subfleet/dependencies.py', 'exec'), module.__dict__)
    return module


def check_case_rename(world, monkeypatch, route, module, record_property):
    tree = world.job('owner').resolve()
    owner, commit = source(world, tree)
    checkout = world.base / 'outside'
    if route == 'alternates':
        cloned = git('clone', '-q', '--shared', owner, checkout)
        assert cloned.returncode == 0, cloned.stderr
        target = owner / '.git' / 'objects'
    else:
        assert route == 'scan'
        stable = world.base / 'stable'
        cloned = git('clone', '-q', '--no-hardlinks', owner, stable)
        assert cloned.returncode == 0, cloned.stderr
        packed = git('-C', stable, 'gc', '--prune=now')
        assert packed.returncode == 0, packed.stderr
        target = tree / 'packs'
        (stable / '.git' / 'objects' / 'pack').rename(target)
        (stable / '.git' / 'objects' / 'pack').symlink_to(target)
        checkout.mkdir()
        (checkout / '.git').write_text('gitdir: ' + str(stable / '.git') + '\n')
    renamed = target.with_name(target.name.upper())
    canonical = folders.canonical
    if not os.path.exists(renamed):
        pytest.skip('requires a volume accepting case aliases')
    assert canonical(target) == str(target)
    before = git('-C', checkout, 'show', commit + ':owner-only.txt')
    health = git('-C', checkout, 'fsck', '--no-dangling')
    assert before.returncode == health.returncode == 0, (before.stderr, health.stderr)
    before_info = target.stat()
    before_identity = (before_info.st_dev, before_info.st_ino)
    changed = []

    def spelling(path):
        value = canonical(path)
        caller = inspect.currentframe().f_back.f_code.co_name
        # Alternates reach found's final canonical generator; scan targets are
        # canonicalized before insertion into entries. Return the spelling just
        # read, then perform one real case-only rename before its filter runs.
        wanted = '<genexpr>' if route == 'alternates' else 'scan'
        if os.fspath(path) == str(target) and caller == wanted and not changed:
            target.rename(renamed)
            changed.append(True)
        return value

    with monkeypatch.context() as patch:
        patch.setattr(folders, 'canonical', spelling)
        recorded = module.record(str(checkout))
    assert changed
    assert canonical(target) == str(renamed)
    middle = git('-C', checkout, 'show', commit + ':owner-only.txt')
    assert middle.returncode == 0 and middle.stdout == before.stdout, middle.stderr
    middle_health = git('-C', checkout, 'fsck', '--no-dangling')
    assert middle_health.returncode == 0, middle_health.stderr
    current_info = target.stat()
    assert before_identity == (current_info.st_dev, current_info.st_ino)
    assert os.path.samestat(os.stat(target), os.stat(renamed))
    # Both equations cannot be necessary for readability on a changing,
    # case-insensitive volume: target remains readable under its earlier case.
    assert not dependencies._spelled_location(str(target))
    target_fixed_point = canonical(target) == str(target)
    reader(world, recorded)
    result = maintain(world)
    after = git('-C', checkout, 'show', commit + ':owner-only.txt')
    record_property('R11-F1', json.dumps(dict(route=route, target=str(target),
        renamed=str(renamed), recorded=recorded, result=result, fsck=health.returncode,
        fsck_after_rename=middle_health.returncode, identity=before_identity,
        before=before.returncode, middle=middle.returncode, after=after.returncode,
        target_stat_errno=0, target_fixed_point=target_fixed_point,
        error_after=after.stderr.decode()), sort_keys=True))
    assert 'owner' not in result['pruned'] and tree.is_dir(), (recorded, result, after.stderr)
    assert after.returncode == 0 and after.stdout == before.stdout, after.stderr


@pytest.mark.parametrize('route', ['alternates', 'scan'])
def test_baseline_keeps_readable_store_across_case_rename(world, monkeypatch, route, record_property):
    check_case_rename(world, monkeypatch, route, baseline_reader(), record_property)


@pytest.mark.xfail(strict=True, reason='R11-F1: return filter drops a readable case alias without incomplete evidence')
@pytest.mark.parametrize('route', ['alternates', 'scan'])
def test_return_filter_keeps_readable_store_across_case_rename(world, monkeypatch, route, record_property):
    check_case_rename(world, monkeypatch, route, dependencies, record_property)
