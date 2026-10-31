"""A case-only directory rename in the new return-spelling gap."""
import json,os
from pathlib import Path
import pytest
from subfleet import dependencies,folders
from tests.fake.test_retention_dependency_review_r2 import world,reader,maintain
from tests.fake.test_retention_dependency_review_r5 import source
from tests.unit.test_retention_location_inputs import git
from round_probes import BASE

@pytest.mark.parametrize('version',['baseline','head'])
def test_case_only_object_directory_rename(world,monkeypatch,version,record_property):
 tree=world.job('owner').resolve();owner,commit=source(world,tree)
 checkout=world.base/'outside'
 cloned=git('clone','-q','--shared',owner,checkout);assert cloned.returncode==0,cloned.stderr
 objects=owner/'.git'/'objects';renamed=objects.with_name('OBJECTS')
 assert folders.canonical(objects)==str(objects)
 before=git('-C',checkout,'show',commit+':owner-only.txt')
 health=git('-C',checkout,'fsck','--no-dangling')
 assert before.returncode==health.returncode==0
 canonical=folders.canonical;changed=[]
 def spelling(path):
  value=canonical(path)
  if os.fspath(path)==str(objects) and not changed:
   objects.rename(renamed);changed.append(True)
  return value
 module=BASE if version=='baseline' else dependencies
 with monkeypatch.context() as patch:
  patch.setattr(folders,'canonical',spelling)
  recorded=module.record(str(checkout))
 assert changed
 assert canonical(objects)==str(renamed)
 middle=git('-C',checkout,'show',commit+':owner-only.txt')
 assert middle.returncode==0 and middle.stdout==before.stdout
 assert not recorded.get('incomplete'),recorded
 reader(world,recorded)
 result=maintain(world)
 after=git('-C',checkout,'show',commit+':owner-only.txt')
 row=dict(version=version,objects=str(objects),renamed=str(renamed),recorded=recorded,
  before=before.returncode,middle=middle.returncode,after=after.returncode,result=result,
  pinned=any(folders.within(p,str(tree)) for p in recorded.get('git',[])))
 record_property('case-rename',json.dumps(row,sort_keys=True))
 # Record both outcomes without supplying an oracle that hides a regression.
 if version=='baseline':assert 'owner' not in result['pruned'] and after.returncode==0
