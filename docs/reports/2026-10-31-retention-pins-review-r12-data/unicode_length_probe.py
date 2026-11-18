"""Review r12: does a readable short normalization survive an overlong canonical string?"""
import inspect,json,os,shutil,unicodedata
from pathlib import Path
import pytest
from subfleet import dependencies,folders
from tests.fake.test_retention_dependency_review_r2 import world,reader,maintain
from tests.fake.test_retention_dependency_review_r5 import source
from tests.fake.test_retention_dependency_review_r11 import baseline_reader
from tests.unit.test_retention_location_inputs import git

@pytest.mark.parametrize('route',['alternates','scan'])
@pytest.mark.parametrize('version',['475f63278','head'])
def test_normalization_length_at_return_boundary(world,monkeypatch,record_property,version,route):
 tree=world.job('owner').resolve();old,commit=source(world,tree)
 names=[unicodedata.normalize('NFC','é'*35)+str(n) for n in range(10)]
 fd=os.open(tree,os.O_RDONLY)
 try:
  for name in names:
   physical=unicodedata.normalize('NFD',name)
   os.mkdir(physical,dir_fd=fd)
   new=os.open(physical,os.O_RDONLY,dir_fd=fd);os.close(fd);fd=new
 finally:os.close(fd)
 checkout=world.base/'outside'
 if route=='alternates':
  raw=tree.joinpath(*names,'source');old.rename(raw)
  target=raw/'.git'/'objects'
  assert git('init','-q','-b','main',checkout).returncode==0
  (checkout/'.git'/'refs'/'heads'/'main').write_text(commit+'\n')
  (checkout/'.git'/'objects'/'info'/'alternates').write_text(str(target)+'\n')
 else:
  assert git('clone','-q','--no-hardlinks',old,checkout).returncode==0
  assert git('-C',checkout,'gc','--prune=now').returncode==0
  target=tree.joinpath(*names,'packs')
  pack=checkout/'.git'/'objects'/'pack';pack.rename(target);pack.symlink_to(target)
 spelled=folders.canonical(target)
 raw_stat=target.stat();error=None
 try:os.stat(spelled)
 except OSError as e:error=e.errno
 before=git('-C',checkout,'show',commit+':owner-only.txt');health=git('-C',checkout,'fsck','--no-dangling')
 assert before.returncode==health.returncode==0,(before.stderr,health.stderr)
 changed=[];canonical=folders.canonical
 def spelling(path):
  value=canonical(path)
  caller=inspect.currentframe().f_back.f_code.co_name
  if os.fspath(path)==str(target) and caller==('<genexpr>' if route=='alternates' else 'scan') and not changed:
   fd=os.open(tree,os.O_RDONLY)
   try:
    for name in names:
     physical=unicodedata.normalize('NFD',name)
     os.rename(physical,name,src_dir_fd=fd,dst_dir_fd=fd)
     new=os.open(name,os.O_RDONLY,dir_fd=fd);os.close(fd);fd=new
   finally:os.close(fd)
   changed.append(True)
  return value
 module=dependencies if version=='head' else baseline_reader()
 with monkeypatch.context() as patch:
  patch.setattr(folders,'canonical',spelling)
  recorded=module.record(str(checkout))
 assert changed
 middle=git('-C',checkout,'show',commit+':owner-only.txt')
 reader(world,recorded);result=maintain(world)
 after=git('-C',checkout,'show',commit+':owner-only.txt')
 data=dict(version=version,route=route,raw=str(target),spelled=spelled,raw_bytes=len(os.fsencode(target)),canonical_bytes=len(os.fsencode(spelled)),canonical_stat_errno=error,recorded=recorded,result=result,fsck=health.returncode,before=before.returncode,middle=middle.returncode,after=after.returncode,changed=bool(changed))
 record_property('unicode-length',json.dumps(data,sort_keys=True))
 print(json.dumps({k:v for k,v in data.items() if k not in ('result','recorded','raw','spelled')}))
