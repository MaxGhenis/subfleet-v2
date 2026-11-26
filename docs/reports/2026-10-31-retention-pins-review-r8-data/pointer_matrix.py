import json, shutil, os
from pathlib import Path
import pytest
from subfleet import dependencies, folders
from tests.unit.test_retention_pointer_whitespace import git, pointer_repository

OUT=Path('docs/reports/2026-10-31-retention-pins-review-r8-data')

@pytest.mark.parametrize('layout',['gitfile','commondir'])
@pytest.mark.parametrize('absolute',[False,True])
@pytest.mark.parametrize('name',[' metadata','metadata ',' metadata ','   '])
@pytest.mark.parametrize('ending',[b'\n',b'\r\n'])
def test_valid_pointer(tmp_path,layout,absolute,name,ending):
    owner,checkout,result=pointer_repository(tmp_path,layout,name,absolute=absolute,ending=ending)
    assert result.returncode==0 and result.stdout==b'pointer-only blob\n',result.stderr
    recorded=dependencies.record(str(checkout))
    assert folders.canonical(owner/'objects') in recorded.get('git',[]) or recorded.get('incomplete'),recorded

@pytest.mark.parametrize('named',[False,True])
@pytest.mark.parametrize('own',[False,True])
@pytest.mark.parametrize('content',[None,b'',b'\n',b'\r\n'])
def test_empty_common(tmp_path,named,own,content):
    owner=tmp_path/'owner.git';assert git('init','-q','--bare',owner).returncode==0
    blob=git('--git-dir',owner,'hash-object','-w','--stdin',data=b'own-store blob\n');assert blob.returncode==0
    checkout=tmp_path/'checkout';checkout.mkdir()
    admin=checkout/'.git'
    if named:
        admin=tmp_path/'container'/'worktrees'/'slot';admin.parent.mkdir(parents=True)
        (checkout/'.git').write_text('gitdir: '+str(admin)+'\n')
    if own:
        assert git('init','-q','--bare',admin).returncode==0
        shutil.rmtree(admin/'objects');(admin/'objects').symlink_to(owner/'objects',target_is_directory=True)
    else:
        admin.mkdir();(admin/'HEAD').write_text('ref: refs/heads/main\n')
    if content is not None:(admin/'commondir').write_bytes(content)
    result=git('-C',checkout,'cat-file','-p',blob.stdout.decode().strip())
    accepted=own and content!=b''
    assert (result.returncode==0)==accepted,result.stderr
    recorded=dependencies.record(str(checkout))
    covered=folders.canonical(owner/'objects') in recorded.get('git',[]) or bool(recorded.get('incomplete'))
    assert covered==(own and not named),(recorded,accepted,named)
    label=f'named-{named}-own-{own}-content-{repr(content)}'
    (OUT/('pointer-'+label+'.json')).write_text(json.dumps({'label':label,'git_rc':result.returncode,'git_stdout':result.stdout.decode(),'git_stderr':result.stderr.decode(),'recorded':recorded,'actual_storage_covered':covered},indent=2)+'\n')

from tests.fake.test_retention_dependency_review_r2 import world, reader, maintain
from tests.fake.test_retention_dependency_review_r5 import source
from tests.unit.test_retention_dependency_units import repo

@pytest.mark.parametrize('marker',[b'gitdir: \n',b'gitdir:metadata\n',b'',b'\n',b'\r\n'])
def test_rejected_child_with_valid_parent(world,marker):
    tree=world.job('owner');owner,commit=source(world,tree)
    parent=repo(world.base/'outside')
    shutil.rmtree(parent/'.git'/'objects');(parent/'.git'/'objects').symlink_to(owner/'.git'/'objects',target_is_directory=True)
    child=parent/'child';child.mkdir();dotgit=child/'.git'
    if marker.startswith(b'gitdir:'):dotgit.write_bytes(marker)
    else:
        dotgit.mkdir();(dotgit/'HEAD').write_text('ref: refs/heads/main\n');(dotgit/'commondir').write_bytes(marker)
    result=git('-C',child,'show',commit+':owner-only.txt')
    recorded=dependencies.record(str(child))
    # Even when Git falls back to the parent, discovery records that store.
    assert folders.canonical(owner/'.git'/'objects') in recorded.get('git',[]) or recorded.get('incomplete'),recorded
    reader(world,recorded);report=maintain(world)
    assert 'owner' not in report['pruned'],report
    (OUT/('parent-fallback-'+repr(marker)+'.json')).write_text(json.dumps({'marker':repr(marker),'git_rc':result.returncode,'git_stdout':result.stdout.decode(),'git_stderr':result.stderr.decode(),'recorded':recorded,'maintenance':report},indent=2)+'\n')

@pytest.mark.parametrize('layout',['gitfile','commondir'])
def test_padded_pointer(world,layout):
    tree=world.job('owner');owner,commit=source(world,tree)
    outside=repo(world.base/'outside')
    if layout=='gitfile':
        (outside/'.git').rename(world.base/'unused.git')
        pointer=outside/'.git';prefix=b'gitdir: '
    else:pointer=outside/'.git'/'commondir';prefix=b''
    pointer.write_bytes(prefix+os.fsencode(owner/'.git')+b'\r\n'*35000)
    resolved=git('-C',outside,'show',commit+':owner-only.txt')
    recorded=dependencies.record(str(outside))
    data={'layout':layout,'bytes':pointer.stat().st_size,'git_rc':resolved.returncode,'git_stdout':resolved.stdout.decode(),'git_stderr':resolved.stderr.decode(),'recorded':recorded}
    if resolved.returncode==0:
        reader(world,recorded);data['maintenance']=maintain(world)
        data['owner_exists_after']=tree.is_dir()
        after=git('-C',outside,'show',commit+':owner-only.txt')
        data['git_after_rc']=after.returncode;data['git_after_stderr']=after.stderr.decode()
    (OUT/('padded-pointer-'+layout+'.json')).write_text(json.dumps(data,indent=2)+'\n')
