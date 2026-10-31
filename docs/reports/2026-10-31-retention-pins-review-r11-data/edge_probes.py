"""C-8.4: r10 canonicalization boundary probes; all repositories are fixtures."""
import json
import os
from pathlib import Path

import pytest

from subfleet import dependencies, folders
from tests.unit.test_retention_location_inputs import repo, git
from tests.unit.test_retention_location_spelling import assert_location_spelling
from tests.fake.test_retention_dependency_review_r2 import world


@pytest.mark.parametrize('kind', ['dangling', 'loop', '64-hops'])
def test_scan_canonicalization_on_quiet_stop(tmp_path, kind, record_property):
    base=tmp_path.resolve()
    checkout=repo(base/'checkout')
    entry=checkout/'.git'/'unused'
    if kind=='dangling':
        entry.symlink_to(base/'absent'/'child')
    elif kind=='loop':
        entry.symlink_to('unused')
    else:
        target=repo(base/'destination')/'.git'
        for n in reversed(range(65)):
            link=base/('hop'+str(n)); link.symlink_to(target); target=link
        entry.symlink_to(target)
    before=git('-C',checkout,'show','HEAD:f.txt')
    incomplete=[]
    places=dependencies.git_storage(str(checkout), incomplete=incomplete)
    assert before.returncode==0
    assert not incomplete, incomplete
    assert_location_spelling(checkout,places)
    walked, chain=dependencies._Aliases._walk(str(entry))
    aliases=dependencies._Aliases(); errors=[]; aliases.scan(str(checkout/'.git'),errors)
    assert not errors
    spelled=folders.canonical(walked)
    assert spelled in aliases.entries
    if kind=='64-hops':
        assert len(chain)==64 and walked!=spelled and spelled==str(base/'destination'/'.git')
        direct=git('--git-dir',spelled,'show','HEAD:f.txt')
        excessive=git('--git-dir',entry,'show','HEAD:f.txt')
        assert direct.returncode==0 and excessive.returncode!=0, (direct.stderr,excessive.stderr)
    record_property('quiet-stop',json.dumps(dict(kind=kind,places=places,incomplete=incomplete,
        walked=walked,spelled=spelled,links=len(chain),entries=sorted(aliases.entries))))


@pytest.mark.parametrize('change', ['entry', 'target', 'missing-target'])
def test_scan_change_between_walk_and_spelling_is_incomplete(tmp_path, monkeypatch, record_property, change):
    base=tmp_path.resolve(); checkout=repo(base/'checkout')
    first=base/'first'
    if change!='missing-target': first.mkdir()
    replacement=base/'replacement'; replacement.mkdir()
    entry=checkout/'.git'/'unused'; entry.symlink_to(first)
    original=dependencies._Aliases.resolve; changed=[]
    def resolving(self,path):
        out=original(self,path)
        if str(path)==str(entry) and not changed:
            if change=='entry':
                entry.unlink(); entry.symlink_to(replacement)
            else:
                if first.exists(): first.rmdir()
                first.symlink_to(replacement)
            changed.append(True)
        return out
    monkeypatch.setattr(dependencies._Aliases,'resolve',resolving)
    recorded=dependencies.record(str(checkout))
    assert changed and recorded.get('incomplete'), recorded
    assert any('changed' in e['reason'] for e in recorded['incomplete'])
    record_property('walk-change',json.dumps(recorded))


def test_canonical_never_runs_inside_retention_transaction(world,monkeypatch,record_property):
    tree=world.job('owner')
    checkout=repo(world.base/'outside')
    # A legacy active borrower forces a recovery census.
    world.store.add_job(job_id='borrower',request_id='borrower',payload_digest='d',kind='dispatch',state='running',sandbox='read-only',workdir=str(checkout),prompt_path='/p')
    original=folders.canonical; calls=[]
    def canonical(path):
        assert not world.store.connection.in_transaction, path
        calls.append(str(path)); return original(path)
    monkeypatch.setattr(folders,'canonical',canonical)
    from tests.fake.test_retention_dependency_review_r2 import maintain
    result=maintain(world)
    assert calls and 'owner' in result['pruned'], result
    record_property('canonical-outside-transactions',json.dumps(dict(calls=len(calls),result=result)))


def test_unused_parent_oversized_alternate_remains_listed_difference(world,record_property):
    from tests.fake.test_retention_dependency_review_r2 import reader, maintain
    tree=world.job('owner'); checkout=repo(world.base/'outside')
    parent=world.base/'container'; admin=parent/'worktrees'/'slot'
    admin.parent.mkdir(parents=True)
    (checkout/'.git').rename(admin)
    (checkout/'.git').write_text('gitdir: '+str(admin)+'\n')
    pointer=parent/'objects'/'info'/'alternates'; pointer.parent.mkdir(parents=True)
    pointer.write_bytes(b'x'*1048577)
    assert git('-C',checkout,'fsck','--no-dangling').returncode==0
    assert git('-C',checkout,'show','HEAD:f.txt').returncode==0
    recorded=dependencies.record(str(checkout)); reader(world,recorded)
    result=maintain(world)
    assert recorded.get('incomplete') and 'owner' not in result['pruned'], (recorded,result)
    record_property('D-R7-1',json.dumps(dict(recorded=recorded,result=result)))
