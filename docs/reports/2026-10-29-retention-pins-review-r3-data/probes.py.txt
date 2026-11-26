from contextlib import contextmanager
import os
from pathlib import Path
import pytest
from subfleet import dependencies, folders, retention
from tests.fake.test_retention_dependency_review_r2 import world, reader, maintain
from tests.unit.test_retention_dependency_units import repo
from tests.unit.retention_world import git

@pytest.mark.parametrize('state', ['running', 'succeeded'])
@pytest.mark.parametrize('change', ['replace', 'rewrite', 'unlink', 'rename-over', 'same-size', 'symlink-file', 'symlink-info'])
def test_changes(world, monkeypatch, change, state):
    trees=[world.job('owner-a'), world.job('owner-b')]
    world.store.update_job('owner-b', worktree=None)
    source=repo(trees[0]/'source')
    outside=world.base/'outside'
    git(world.base, 'init', '--quiet', str(outside))
    alternate=outside/'.git'/'objects'/'info'/'alternates'
    new=os.fsencode(source/'.git'/'objects')+b'\n'
    old=b'#'+b'x'*(len(new)-2)+b'\n'
    alternate.write_bytes(old)
    if change=='symlink-file':
        original=alternate.with_name('old');alternate.rename(original);alternate.symlink_to(original)
    if change=='symlink-info':
        info=alternate.parent
        info.rename(info.with_name('old-info'));info.symlink_to(info.with_name('old-info'))
    real=dependencies.open_regular
    changed=[]
    @contextmanager
    def opening(path,*a,**k):
        with real(path,*a,**k) as stream:
            if str(path)!=str(alternate):yield stream;return
            class Changing:
                def readline(self,size=-1):
                    line=stream.readline(size)
                    if not changed:
                        changed.append(True)
                        if change=='unlink':alternate.unlink()
                        elif change in ('replace','rename-over'):
                            replacement=alternate.with_name('replacement');replacement.write_bytes(new)
                            if change=='replace':alternate.unlink()
                            replacement.rename(alternate)
                        elif change=='symlink-file':
                            target=alternate.with_name('new');target.write_bytes(new)
                            alternate.unlink();alternate.symlink_to(target)
                        elif change=='symlink-info':
                            target=alternate.parent.with_name('new-info');target.mkdir();(target/'alternates').write_bytes(new)
                            alternate.parent.unlink();alternate.parent.symlink_to(target)
                        else:alternate.write_bytes(new if change=='same-size' else new+b'# longer\n')
                    return line
                def __getattr__(self,name):return getattr(stream,name)
            yield Changing()
    with monkeypatch.context() as p:
        p.setattr(dependencies,'open_regular',opening)
        recorded=dependencies.record(str(outside))
    assert changed and recorded.get('incomplete'), recorded
    reader(world,recorded,state=state)
    if state=='succeeded':world.store.acquire_lease('out:/outside/result','borrower')
    result=maintain(world)
    assert not result['pruned'],result
    assert all(t.is_dir() for t in trees)
    assert all('dependency-incomplete' in result['pin_reasons'][j] for j in ['owner-a','owner-b'])
    world.store.release_leases('borrower');world.store.update_job('borrower',state='succeeded')
    assert {'owner-a','owner-b'} <= set(maintain(world)['pruned'])

def test_symlink_git_parent(world,monkeypatch):
    tree=world.job('owner')
    source=repo(tree/'source')
    commit=git(source,'rev-parse','HEAD')
    old=repo(world.base/'old')
    outside=world.base/'outside';outside.mkdir()
    link=outside/'.git';link.symlink_to(old/'.git')
    alternate=old/'.git'/'objects'/'info'/'alternates';alternate.write_bytes(b'# first\n# second\n')
    real=dependencies.open_regular
    swapped=[]
    @contextmanager
    def opening(path,*a,**k):
        with real(path,*a,**k) as stream:
            if str(path)!=str(alternate):yield stream;return
            class Changing:
                def readline(self,size=-1):
                    line=stream.readline(size)
                    if not swapped:
                        swapped.append(True);link.unlink();link.symlink_to(source/'.git')
                    return line
                def __getattr__(self,name):return getattr(stream,name)
            yield Changing()
    with monkeypatch.context() as p:
        p.setattr(dependencies,'open_regular',opening)
        recorded=dependencies.record(str(outside))
    assert swapped
    assert git(outside,'cat-file','-t',commit)=='commit'
    reader(world,recorded)
    result=maintain(world)
    assert 'owner' not in result['pruned'],(recorded,result)
    assert recorded.get('incomplete'), recorded

@pytest.mark.parametrize('key,expected', [('out:',True),('out:/result',True),('out:/é',True),('out;',False),('out9',False),('outz:/result',False)])
def test_export_range(world,monkeypatch,key,expected):
    world.store.add_job(job_id='legacy',request_id='legacy',payload_digest='d',kind='dispatch',state='succeeded',sandbox='read-only',workdir=str(world.base/'outside'),prompt_path='/p')
    world.store.acquire_lease(key,'legacy')
    calls=[]
    def record(path,**kw):
        assert not world.store.connection.in_transaction
        calls.append(path)
        return {'incomplete':[{'file':'/bad/alternates','reason':'unreadable'}]}
    monkeypatch.setattr(dependencies,'record',record)
    census=dependencies.legacy(world.store.query)
    assert bool(calls)==expected
    def forbidden(*a,**k):raise AssertionError('filesystem in transaction')
    monkeypatch.setattr(dependencies,'record',forbidden)
    monkeypatch.setattr(folders,'canonical',forbidden)
    with world.store.transaction():
        live=dependencies.live(world.store.query,census)
        assert bool(live.incomplete)==expected
