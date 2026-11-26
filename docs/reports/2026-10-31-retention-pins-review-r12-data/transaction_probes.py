"""Review-only controls. Every store and removable tree is a fixture."""
import errno
import json
import os
from pathlib import Path
import shutil
import types

import pytest

from subfleet import dependencies, folders
from tests.fake.test_retention_dependency_review_r2 import world, reader, maintain
from tests.fake.test_retention_dependency_review_r5 import source
from tests.unit.test_retention_location_inputs import git, repo
from tests.unit.test_retention_location_inputs import build_location_input


@pytest.mark.parametrize('target', ['inside-owner', 'tree', 'worktrees', 'home', 'fixture-root', 'slash'])
def test_unused_link_width_and_lifetime(world, target, record_property, monkeypatch):
    owner = world.job('owner').resolve()
    other = world.job('other').resolve()
    stable = repo(world.base / 'stable')
    checkout = world.base / 'outside'
    checkout.mkdir()
    (checkout / '.git').write_text('gitdir: ' + str(stable / '.git') + '\n')
    broad = owner / 'broad'
    broad.mkdir()
    destination = {'inside-owner': broad, 'tree': owner, 'worktrees': owner.parent,
                   'home': world.base / 'home', 'fixture-root': world.root, 'slash': Path('/')}[target].resolve()
    (stable / '.git' / 'unused-entry').symlink_to(destination)
    before = git('-C', checkout, 'show', 'HEAD:f.txt')
    assert before.returncode == 0
    recorded = dependencies.record(str(checkout))
    assert not recorded.get('incomplete'), recorded
    assert str(destination) in recorded['git'], recorded
    reader(world, recorded)
    def denied(*a, **kw):
        raise AssertionError('filesystem discovery in a store transaction')
    states = {}
    for state, exporting in [('running', False), ('succeeded', True), ('succeeded', False)]:
        world.store.update_job('borrower', state=state)
        world.store.release_leases('borrower')
        if exporting:
            world.store.acquire_lease('out:/outside/result', 'borrower')
        with monkeypatch.context() as patch:
            patch.setattr(dependencies, 'record', denied)
            patch.setattr(dependencies, '_unreadable_location', denied)
            patch.setattr(folders, 'canonical', denied)
            with world.store.transaction():
                states[state + ('-exporting' if exporting else '')] = {
                    name: dependencies.dependents(world.store.query, str(tree), exclude=name)
                    for name, tree in [('owner', owner), ('other', other)]}
    assert states['running']['owner'] == (['borrower'] if target in ('inside-owner', 'tree') else [])
    assert states['running']['other'] == []
    assert states['succeeded-exporting']['owner'] == []
    assert states['succeeded']['owner'] == []
    result = maintain(world)
    assert {'owner', 'other'} <= set(result['pruned']), result
    after = git('-C', checkout, 'show', 'HEAD:f.txt')
    assert after.returncode == 0 and after.stdout == before.stdout
    record_property('width', json.dumps(dict(target=target, recorded=recorded, states=states,
                                             before=before.returncode, after=after.returncode), sort_keys=True))


@pytest.mark.parametrize('layout', ['admin-path', 'gitfile'])
def test_r8_owner_removal_breaks_the_identical_real_git_read(world, layout, record_property):
    tree = world.job('owner').resolve()
    owner, commit = source(world, tree)
    stable = repo(world.base / 'stable')
    assert git('-C', stable, 'fetch', '-q', owner, 'HEAD').returncode == 0
    checkout = world.base / 'outside'
    checkout.mkdir()
    alias = tree / 'alias'
    if layout == 'admin-path':
        alias.symlink_to(stable / '.git')
        (checkout / '.git').write_text('gitdir: ' + str(alias) + '\n')
    else:
        alias.write_text('gitdir: ' + str(stable / '.git') + '\n')
        (checkout / '.git').symlink_to(alias)
    before = git('-C', checkout, 'show', commit + ':owner-only.txt')
    assert before.returncode == 0
    recorded = dependencies.record(str(checkout))
    assert not recorded.get('incomplete')
    assert any(folders.within(place, str(tree)) for place in recorded['git'])
    shutil.rmtree(tree)
    after = git('-C', checkout, 'show', commit + ':owner-only.txt')
    assert after.returncode == 128
    record_property('owner-removal', json.dumps(dict(layout=layout, before=before.returncode,
                                                      after=after.returncode, recorded=recorded), sort_keys=True))


@pytest.mark.parametrize('hops', [1, 8, 31, 32, 33, 40, 63, 64, 65, 80])
def test_kernel_hop_boundary_against_git(tmp_path, hops, record_property):
    base = tmp_path.resolve()
    stable = repo(base / 'stable')
    checkout = base / 'checkout'
    checkout.mkdir()
    target = stable / '.git'
    for n in reversed(range(hops)):
        link = base / ('hop' + str(n))
        link.symlink_to(target)
        target = link
    (checkout / '.git').write_text('gitdir: ' + str(target) + '\n')
    resolved, links = dependencies._Aliases._walk(str(target))
    read = git('-C', checkout, 'show', 'HEAD:f.txt')
    # The walker must never stop short of a target that this host's Git can read.
    if read.returncode == 0:
        assert resolved == str(stable / '.git')
        assert len(links) == hops
    assert len(links) == min(hops, 64)
    record_property('hop-boundary', json.dumps(dict(hops=hops, git_rc=read.returncode,
                                                   walked=len(links), resolved=resolved), sort_keys=True))


@pytest.mark.parametrize('failure', [errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EACCES])
def test_scan_failure_classification(tmp_path, failure, monkeypatch, record_property):
    checkout = repo(tmp_path.resolve() / 'checkout')
    real = dependencies.os.scandir
    def scanning(path):
        if str(path) == str(checkout / '.git'):
            raise OSError(failure, os.strerror(failure), str(path))
        return real(path)
    with monkeypatch.context() as patch:
        patch.setattr(dependencies.os, 'scandir', scanning)
        recorded = dependencies.record(str(checkout))
    assert bool(recorded.get('incomplete')) == (failure != errno.ENOENT), recorded
    record_property('scan-failure', json.dumps(dict(errno=failure, recorded=recorded), sort_keys=True))


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('incomplete', [False, True])
def test_terminal_gone_target_and_evidence_cannot_pin(world, legacy, incomplete, monkeypatch):
    owner = world.job('owner').resolve()
    checkout = world.base / 'outside'
    checkout.mkdir()
    (checkout / '.git').write_text('gitdir: ' + str(world.base / 'gone') + '\n')
    if legacy:
        world.store.add_job(job_id='borrower', request_id='borrower', payload_digest='d',
                            kind='dispatch', state='succeeded', sandbox='read-only',
                            workdir=str(checkout), prompt_path='/p')
        if incomplete:
            (checkout / '.git').write_bytes(b'gitdir: x' + b'\r\n' * 35000)
    else:
        recorded = dependencies.record(str(checkout))
        if incomplete:
            recorded['incomplete'] = [{'file': str(world.base / 'gone'), 'reason': 'old error'}]
        reader(world, recorded, state='succeeded')
    real = dependencies.record
    seen = []
    def outside_transactions(*a, **kw):
        assert not world.store.connection.in_transaction
        seen.append(True)
        return real(*a, **kw)
    monkeypatch.setattr(dependencies, 'record', outside_transactions)
    result = maintain(world)
    assert 'owner' in result['pruned'] and not owner.exists(), result
    assert not seen  # terminal jobs with no pending output need no discovery.


def test_parent_alternate_hint_is_not_scanned(tmp_path, monkeypatch):
    paths = build_location_input(tmp_path, dict(position='gitfile-target', chain=2,
                                               relative=True, named=True, entry='HEAD'))
    inferred = paths['admin'].parent.parent
    alternate = paths['stable'] / 'hint-objects'
    alternate.mkdir()
    (alternate / 'unused').symlink_to(paths['unrelated'])
    info = inferred / 'objects' / 'info'
    info.mkdir(parents=True)
    (info / 'alternates').write_text(str(alternate) + '\n')
    real = dependencies.os.scandir
    scanned = []
    def scanning(path):
        scanned.append(str(path))
        return real(path)
    with monkeypatch.context() as patch:
        patch.setattr(dependencies.os, 'scandir', scanning)
        recorded = dependencies.record(str(paths['checkout']))
    assert not recorded.get('incomplete'), recorded
    assert str(alternate) in recorded['git']
    assert not any(folders.within(place, str(paths['unrelated'])) for place in recorded['git'])
    assert not {str(inferred), str(inferred / 'objects'), str(alternate)} & set(scanned)


@pytest.mark.parametrize('relative', [False, True])
def test_absolute_and_relative_walks_apply_dotdot_after_links(tmp_path, monkeypatch, relative):
    paths = build_location_input(tmp_path, dict(position='gitfile-target', chain=3,
                                               relative=relative, named=False, entry='HEAD'))
    target = str(paths['owner'] / 'hop0') + '/../admin'
    (paths['checkout'] / '.git').write_text('gitdir: ' + target + '\n')
    before = git('-C', paths['checkout'], 'show', 'HEAD:f.txt')
    assert before.returncode == 0
    monkeypatch.chdir(paths['base'])
    resolved, links = dependencies._Aliases._walk('owner/hop0/../admin')
    assert resolved == str(paths['admin'])
    assert {link[0] for link in links} == {str(paths['owner'] / ('hop' + str(n))) for n in range(3)}
    recorded = dependencies.record(str(paths['checkout']))
    assert not recorded.get('incomplete'), recorded
    shutil.rmtree(paths['owner'])
    assert git('-C', paths['checkout'], 'show', 'HEAD:f.txt').returncode != 0


def test_outside_checkout_alias_is_a_listed_extra_location(tmp_path, record_property):
    base = tmp_path.resolve()
    owner, stable = base / 'owner', base / 'stable'
    owner.mkdir()
    stable.mkdir()
    alias = owner / 'unused-as-execution-path'
    alias.symlink_to(stable)
    recorded = dependencies.record(str(alias))
    assert recorded == {'git': [str(alias)]}, recorded
    assert dependencies.record(str(stable)) == {}
    baseline = types.ModuleType('subfleet.r9_baseline')
    exec(compile(Path(__file__).with_name('r8-prerequisite.py.txt').read_text(), '<373a7f5bf>', 'exec'), baseline.__dict__)
    assert baseline.record(str(alias)) == {}
    record_property('outside-checkout-extra-location', json.dumps(dict(
        input=str(alias), target=str(stable), recorded=recorded), sort_keys=True))


@pytest.mark.parametrize('change', ['retarget', 'unlink'])
def test_recorded_link_entry_stays_stable_for_fences(world, change, monkeypatch, record_property):
    owner = world.job('owner').resolve()
    stable = repo(world.base / 'stable')
    replacement = repo(world.base / 'replacement')
    checkout = world.base / 'outside'
    checkout.mkdir()
    alias = owner / 'storage-alias'
    alias.symlink_to(stable / '.git')
    (checkout / '.git').write_text('gitdir: ' + str(alias) + '\n')
    assert git('-C', checkout, 'show', 'HEAD:f.txt').returncode == 0
    recorded = dependencies.record(str(checkout))
    assert str(alias) in recorded['git'] and not recorded.get('incomplete'), recorded
    fence = folders.exclusive_key(str(owner))
    assert world.store.acquire_lease(fence, 'retention:owner')
    places = dependencies.needed({'depends': recorded})
    before = dependencies.fences(world.store.query, places)
    assert before == [(dependencies.GIT, str(alias), [fence])], before
    alias.unlink()
    if change == 'retarget':
        alias.symlink_to(replacement / '.git')
        assert git('-C', checkout, 'show', 'HEAD:f.txt').returncode == 0
    def denied(*a, **kw):
        raise AssertionError('recorded link entry respelled during DB check')
    with monkeypatch.context() as patch:
        patch.setattr(folders, 'canonical', denied)
        patch.setattr(dependencies, 'record', denied)
            patch.setattr(dependencies, '_unreadable_location', denied)
        with world.store.transaction():
            assert dependencies.fenced({'depends': recorded}, {'workdir': str(checkout)}) == places
            after = dependencies.fences(world.store.query, places)
    assert after == before
    world.store.release_leases('retention:owner')
    record_property('stable-link-fence', json.dumps(dict(change=change, before=before, after=after), sort_keys=True))
