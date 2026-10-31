"""Independent real-Git checks for review r8; fixture stores only."""
import json
import os
import shutil
import types
from pathlib import Path

import pytest

from subfleet import dependencies, folders
from tests.fake.test_retention_dependency_review_r2 import world, reader, maintain
from tests.fake.test_retention_dependency_review_r5 import source
from tests.unit.test_retention_dependency_units import repo
from tests.unit.test_retention_pointer_whitespace import git

DATA = Path(__file__).resolve().parent

def discovery(revision):
    if revision == 'head':
        return dependencies
    module = types.ModuleType('subfleet._review_r8_base')
    module.__package__ = 'subfleet'
    exec(compile((DATA / 'base-dependencies.py.txt').read_text(), str(DATA / 'base-dependencies.py.txt'), 'exec'), module.__dict__)
    return module

def evidence(record_property, **values):
    record_property('input', json.dumps(values, default=str, sort_keys=True))

@pytest.mark.parametrize('revision', ['base', 'head'])
@pytest.mark.parametrize('layout', ['plain', 'worktree', 'shared', 'no-git'])
def test_routine_repositories_keep_retiring(world, revision, layout, record_property):
    tree = world.job('owner')
    checkout = world.base / 'outside'
    if layout == 'plain':
        repo(checkout)
    elif layout == 'no-git':
        checkout.mkdir()
    else:
        origin = repo(world.base / 'independent')
        args = (['-C', origin, 'worktree', 'add', '--detach', checkout] if layout == 'worktree'
                else ['clone', '--shared', origin, checkout])
        result = git(*args)
        assert result.returncode == 0, result.stderr
    if layout != 'no-git':
        assert git('-C', checkout, 'fsck', '--no-dangling').returncode == 0
        assert git('-C', checkout, 'show', 'HEAD:f.txt').returncode == 0
    recorded = discovery(revision).record(str(checkout))
    assert not recorded.get('incomplete'), recorded
    reader(world, recorded)
    result = maintain(world)
    assert 'owner' in result['pruned'] and not tree.exists(), (recorded, result)
    evidence(record_property, revision=revision, layout=layout, recorded=recorded, result=result)

@pytest.mark.parametrize('parent_kind', ['absent-objects', 'ordinary', 'common-pointer', 'linked-admin'])
@pytest.mark.parametrize('common', ['absent', 'lf', 'crlf'])
def test_parent_pointer_is_not_a_git_object_store_fallback(world, parent_kind, common, record_property):
    tree = world.job('owner')
    owner, commit = source(world, tree)
    checkout = world.base / 'outside'
    checkout.mkdir()
    parent = world.base / 'container'
    if parent_kind == 'linked-admin':
        independent = repo(world.base / 'independent')
        assert git('-C', owner, 'fetch', '-q', independent, 'HEAD').returncode == 0
        shutil.rmtree(independent / '.git' / 'objects')
        (independent / '.git' / 'objects').symlink_to(owner / '.git' / 'objects', target_is_directory=True)
        helper = world.base / 'helper'
        assert git('-C', independent, 'worktree', 'add', '--detach', helper).returncode == 0
        parent = Path((helper / '.git').read_text().rstrip('\n').removeprefix('gitdir: '))
        assert git('-C', helper, 'fsck', '--no-dangling').returncode == 0
        assert git('-C', helper, 'show', commit + ':owner-only.txt').returncode == 0
    elif parent_kind == 'ordinary':
        parent.mkdir()
        (parent / 'ordinary').write_text('not a repository')
    elif parent_kind == 'common-pointer':
        assert git('init', '-q', '--bare', parent).returncode == 0
        (parent / 'HEAD').write_text(commit + '\n')
        (parent / 'commondir').write_text(str(owner / '.git') + '\n')
        assert git('--git-dir', parent, 'fsck', '--no-dangling').returncode == 0
        assert git('--git-dir', parent, 'show', commit + ':owner-only.txt').returncode == 0
    admin = parent / 'worktrees' / 'slot'
    assert git('init', '--bare', '-q', admin).returncode == 0
    (admin / 'HEAD').write_text(commit + '\n')
    (checkout / '.git').write_text('gitdir: ' + str(admin) + '\n')
    if common != 'absent':
        (admin / 'commondir').write_bytes({'lf': b'\n', 'crlf': b'\r\n'}[common])
    before = git('-C', checkout, 'show', commit + ':owner-only.txt')
    # Git does not use the inferred parent's commondir/linked-admin metadata.
    assert before.returncode != 0, before.stdout
    recorded = dependencies.record(str(checkout))
    assert not recorded.get('incomplete'), recorded
    assert folders.canonical(owner / '.git' / 'objects') not in recorded.get('git', [])
    reader(world, recorded)
    result = maintain(world)
    assert 'owner' in result['pruned'] and not tree.exists(), (recorded, result)
    evidence(record_property, parent=parent_kind, common=common, git_rc=before.returncode,
             git_error=before.stderr.decode(), recorded=recorded, result=result)

@pytest.mark.parametrize('revision', ['base', 'head'])
def test_d_r7_1_predates_r6(world, revision, record_property):
    tree = world.job('owner')
    checkout = repo(world.base / 'outside')
    parent = world.base / 'container'
    admin = parent / 'worktrees' / 'slot'
    admin.parent.mkdir(parents=True)
    (checkout / '.git').rename(admin)
    (checkout / '.git').write_text('gitdir: ' + str(admin) + '\n')
    pointer = parent / 'objects' / 'info' / 'alternates'
    pointer.parent.mkdir(parents=True)
    pointer.write_bytes(b'x' * 1048577)
    assert git('-C', checkout, 'fsck', '--no-dangling').returncode == 0
    assert git('-C', checkout, 'show', 'HEAD:f.txt').stdout == b'outside'
    recorded = discovery(revision).record(str(checkout))
    reader(world, recorded)
    result = maintain(world)
    assert 'owner' not in result['pruned'] and tree.is_dir()
    assert 'dependency-incomplete' in result['pin_reasons']['owner']
    evidence(record_property, revision=revision, bytes=pointer.stat().st_size, recorded=recorded, result=result)

@pytest.mark.parametrize('defect', ['absent', 'dangling', 'directory', 'permission', 'oversized'])
def test_pointer_failure_split_against_git(world, defect, record_property):
    checkout = repo(world.base / 'outside')
    pointer = checkout / '.git' / 'commondir'
    if defect == 'dangling':
        pointer.symlink_to(world.base / 'gone')
    elif defect == 'directory':
        pointer.mkdir()
    elif defect == 'permission':
        pointer.write_bytes(b'\n')
        pointer.chmod(0)
    elif defect == 'oversized':
        pointer.write_bytes(b'\r\n' * 35000)
    try:
        read = git('-C', checkout, 'show', 'HEAD:f.txt')
        recorded = dependencies.record(str(checkout))
    finally:
        if defect == 'permission':
            pointer.chmod(0o600)
    assert bool(recorded.get('incomplete')) == (defect != 'absent'), recorded
    assert (read.returncode == 0) == (defect in ('absent', 'oversized')), read.stderr
    evidence(record_property, defect=defect, git_rc=read.returncode, git_error=read.stderr.decode(), recorded=recorded)

@pytest.mark.parametrize('change', ['rewrite', 'oversized', 'unreadable'])
def test_final_pointer_failure_cannot_hide_changed_storage(world, monkeypatch, change, record_property):
    tree = world.job('owner')
    owner, commit = source(world, tree)
    checkout = repo(world.base / 'outside')
    initial = world.base / 'initial-admin'
    (checkout / '.git').rename(initial)
    pointer = checkout / '.git'
    pointer.write_text('gitdir: ' + str(initial) + '\n')
    identity = pointer.stat().st_ino
    real_check = dependencies._Aliases.check
    real_read = dependencies.read_regular
    changed = []
    def check(self, incomplete):
        if not changed:
            changed.append(True)
            padding = b'\r\n' * 35000 if change == 'oversized' else b'\n'
            pointer.write_bytes(b'gitdir: ' + os.fsencode(owner / '.git') + padding)
        return real_check(self, incomplete)
    def reading(path, **kwargs):
        if change == 'unreadable' and changed and str(path) == str(pointer):
            raise PermissionError('review final read seam')
        return real_read(path, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(dependencies._Aliases, 'check', check)
        patch.setattr(dependencies, 'read_regular', reading)
        recorded = dependencies.record(str(checkout))
    assert identity == pointer.stat().st_ino
    assert git('-C', checkout, 'fsck', '--no-dangling').returncode == 0
    assert git('-C', checkout, 'show', commit + ':owner-only.txt').returncode == 0
    assert recorded.get('incomplete'), recorded
    reader(world, recorded)
    result = maintain(world)
    assert 'owner' not in result['pruned'] and tree.is_dir(), (recorded, result)
    evidence(record_property, change=change, recorded=recorded, result=result)


@pytest.mark.parametrize("legacy", [False, True])
def test_ended_job_with_gone_named_target_does_not_pin(world, legacy, record_property):
    tree = world.job("owner")
    checkout = world.base / "outside"
    checkout.mkdir()
    parent = world.base / "container"
    (parent / "commondir").mkdir(parents=True)
    vanished = parent / "worktrees" / "slot"
    (checkout / ".git").write_text("gitdir: " + str(vanished) + "\n")
    recorded = dependencies.record(str(checkout))
    assert not recorded.get("incomplete"), recorded
    if legacy:
        world.store.add_job(job_id="borrower", request_id="borrower", payload_digest="d",
                            kind="dispatch", state="succeeded", sandbox="read-only",
                            workdir=str(checkout), prompt_path="/p")
        world.store.add_event("job.submitted", job_id="borrower", data={})
    else:
        reader(world, recorded, state="succeeded")
    result = maintain(world)
    assert "owner" in result["pruned"] and not tree.exists(), (recorded, result)
    evidence(record_property, legacy=legacy, recorded=recorded, result=result)


def test_not_a_directory_pointer_has_no_git_reader(tmp_path, record_property):
    parent = tmp_path / "file"
    parent.write_text("not a directory")
    pointer = parent / "commondir"
    aliases = dependencies._Aliases()
    assert aliases.text(str(pointer)) is None
    incomplete = []
    aliases.check(incomplete)
    assert not incomplete
    result = git("--git-dir", parent, "show", "HEAD")
    assert result.returncode != 0
    evidence(record_property, pointer=pointer, git_rc=result.returncode,
             git_error=result.stderr.decode(), incomplete=incomplete)


@pytest.mark.parametrize("revision", ["base", "head"])
@pytest.mark.parametrize("parent_kind", ["absent-objects", "ordinary"])
def test_unused_parent_without_objects_does_not_stop_retirement(world, revision, parent_kind, record_property):
    tree = world.job("owner")
    checkout = repo(world.base / "outside")
    parent = world.base / "container"
    admin = parent / "worktrees" / "slot"
    admin.parent.mkdir(parents=True)
    (checkout / ".git").rename(admin)
    (checkout / ".git").write_text("gitdir: " + str(admin) + "\n")
    if parent_kind == "ordinary":
        (parent / "ordinary-file").write_text("not a repository")
    assert not (parent / "objects").exists()
    assert git("-C", checkout, "fsck", "--no-dangling").returncode == 0
    assert git("-C", checkout, "show", "HEAD:f.txt").stdout == b"outside"
    recorded = discovery(revision).record(str(checkout))
    assert not recorded.get("incomplete"), recorded
    reader(world, recorded)
    result = maintain(world)
    assert "owner" in result["pruned"] and not tree.exists(), (recorded, result)
    evidence(record_property, revision=revision, parent=parent_kind, recorded=recorded, result=result)



@pytest.mark.parametrize("revision", ["base", "head"])
@pytest.mark.parametrize("layout", ["admin-path", "gitfile"])
def test_stable_pointer_alias_inside_retirable_tree_observation(world, revision, layout, record_property):
    tree = world.job("owner")
    owner, commit = source(world, tree)
    stable = repo(world.base / "independent-storage")
    fetched = git("-C", stable, "fetch", "-q", owner, "HEAD")
    assert fetched.returncode == 0, fetched.stderr
    checkout = world.base / "outside"
    checkout.mkdir()
    shutil.copy2(stable / "f.txt", checkout / "f.txt")
    alias = tree / "storage-alias"
    if layout == "admin-path":
        alias.symlink_to(stable / ".git", target_is_directory=True)
        (checkout / ".git").write_text("gitdir: " + str(alias) + "\n")
    else:
        alias.write_text("gitdir: " + str(stable / ".git") + "\n")
        (checkout / ".git").symlink_to(alias)
    health = git("-C", checkout, "fsck", "--no-dangling")
    before = git("-C", checkout, "show", commit + ":owner-only.txt")
    assert health.returncode == before.returncode == 0, (health.stderr, before.stderr)
    assert before.stdout == b"R5 owner-only storage\n"
    status = git("-C", checkout, "status", "--porcelain")
    assert status.returncode == 0 and status.stdout == b"", status.stderr
    recorded = discovery(revision).record(str(checkout))
    reader(world, recorded)
    result = maintain(world)
    after = git("-C", checkout, "show", commit + ":owner-only.txt")
    evidence(record_property, revision=revision, layout=layout, alias=alias, target=stable / ".git", checkout=checkout,
             commit=commit, recorded=recorded, result=result, health_rc=health.returncode,
             before_rc=before.returncode, status_rc=status.returncode, status=status.stdout.decode(),
             after_rc=after.returncode, after_error=after.stderr.decode())
