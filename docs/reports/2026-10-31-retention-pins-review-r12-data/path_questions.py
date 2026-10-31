"""Review r12: real pathname probes for the four filter questions."""
import errno,json,os,shutil,types
from pathlib import Path
import pytest
from subfleet import dependencies,folders
from tests.unit.test_retention_location_inputs import git,repo
from tests.unit.test_retention_location_readable import base_of_length
from tests.fake.test_retention_dependency_review_r11 import baseline_reader
D=Path(__file__).resolve().parent

def ced_reader():
 m=types.ModuleType('subfleet.r12_ced');m.__package__='subfleet'
 exec(compile((D/'ced-dependencies.py.txt').read_text(),'ced02912b:dependencies.py','exec'),m.__dict__)
 return m

def observed(fn,*args):
 try:return dict(value=fn(*args))
 except (OSError,ValueError,AssertionError) as exc:return dict(error=type(exc).__name__,errno=getattr(exc,'errno',None),message=str(exc))

def stat_result(path):
 try:
  s=os.stat(path);return dict(errno=0,identity=[s.st_dev,s.st_ino])
 except (OSError,ValueError) as e:return dict(error=type(e).__name__,errno=getattr(e,'errno',None))

@pytest.mark.parametrize('route',['gitfile','commondir','alternates'])
def test_embedded_nul_at_all_three_readers(tmp_path,route,record_property):
 base=tmp_path.resolve();source=repo(base/'source');checkout=repo(base/'checkout')
 target=source/'.git'
 if route=='gitfile':
  shutil.rmtree(checkout/'.git');(checkout/'.git').write_bytes(b'gitdir: '+os.fsencode(target)+b'\0ignored\n')
 elif route=='commondir':(checkout/'.git'/'commondir').write_bytes(os.fsencode(target)+b'\0ignored\n')
 else:
  target=target/'objects'
  (checkout/'.git'/'objects'/'info'/'alternates').write_bytes(os.fsencode(target)+b'\0ignored\n')
  (checkout/'.git'/'refs'/'heads'/'main').write_bytes(git('-C',source,'rev-parse','HEAD').stdout)
  for p in list((checkout/'.git'/'objects').iterdir()):
   if p.name not in ('info','pack'):shutil.rmtree(p)
 values={name:observed(module.record,str(checkout)) for name,module in [('475f63278',baseline_reader()),('ced02912b',ced_reader()),('head',dependencies)]}
 assert values['head']==values['ced02912b']==values['475f63278'],values
 raw=str(target)+'\0ignored'
 filters=dict(stat=stat_result(raw),canonical=observed(folders.canonical,raw),predicate=observed(dependencies._unreadable_location,raw))
 record_property('nul',json.dumps(dict(route=route,read=git('-C',checkout,'show','HEAD:f.txt').returncode,records=values,filters=filters),sort_keys=True))

@pytest.mark.parametrize('failure',['missing','not-directory','denied'])
def test_real_stable_failure_spelling(tmp_path,failure,record_property):
 base=tmp_path.resolve();checkout=repo(base/'checkout')
 denied=None
 if failure=='missing':target=base/'absent'/'child'
 elif failure=='not-directory':
  (base/'file').write_text('ordinary');target=base/'file'/'child'
 else:
  denied=base/'DENIED';denied.mkdir();(denied/'child').mkdir();denied.chmod(0);target=base/'denied'/'child'
 try:
  (checkout/'.git'/'unused').symlink_to(target)
  values={name:observed(module.record,str(checkout)) for name,module in [('475f63278',baseline_reader()),('ced02912b',ced_reader()),('head',dependencies)]}
  assert values['head']==values['475f63278']==values['ced02912b'],values
  p=folders.canonical(target)
  record_property('stable-error',json.dumps(dict(failure=failure,raw=str(target),canonical=p,raw_stat=stat_result(target),canonical_stat=stat_result(p),equation=dependencies._spelled_location(p),records=values),sort_keys=True))
 finally:
  if denied:denied.chmod(0o700)

@pytest.mark.parametrize('hops',[31,32,33,64,65,80])
def test_many_links_on_alternates_route(tmp_path,hops,record_property):
 base=tmp_path.resolve();source=repo(base/'source');checkout=repo(base/'checkout');target=source/'.git'/'objects'
 for n in reversed(range(hops)):
  link=base/('link'+str(n));link.symlink_to(target);target=link
 (checkout/'.git'/'objects'/'info'/'alternates').write_text(str(target)+'\n')
 (checkout/'.git'/'refs'/'heads'/'main').write_bytes(git('-C',source,'rev-parse','HEAD').stdout)
 # Remove local objects so success cannot be explained by the checkout's copy.
 for p in list((checkout/'.git'/'objects').iterdir()):
  if p.name not in ('info','pack'):shutil.rmtree(p)
 values={name:module.record(str(checkout)) for name,module in [('475f63278',baseline_reader()),('ced02912b',ced_reader()),('head',dependencies)]}
 spelled=folders.canonical(target)
 assert values['head'].get('incomplete',[])==values['475f63278'].get('incomplete',[])==values['ced02912b'].get('incomplete',[])
 record_property('many-links',json.dumps(dict(hops=hops,raw=str(target),canonical=spelled,raw_stat=stat_result(target),canonical_stat=stat_result(spelled),read=git('-C',checkout,'show','HEAD:f.txt').returncode,records=values),sort_keys=True))

@pytest.mark.parametrize('route',['gitfile','alternates'])
def test_relative_long_path_real_git(tmp_path,route,record_property):
 base=tmp_path.resolve();source=repo(base/'source');checkout=repo(base/'checkout')
 parent_fd=os.open(base,os.O_RDONLY);old_fd=os.open('.',os.O_RDONLY)
 try:
  os.chdir(base)
  parts=[]
  while len(os.fsencode(os.getcwd()))<1100:
   part='p'*70;os.mkdir(part);os.chdir(part);parts.append(part)
  # Relative file-system operations reach this store although its absolute
  # spelling exceeds Darwin PATH_MAX. Source metadata stays real Git's.
  shutil.copytree(source/'.git','admin')
  deep=os.getcwd();absolute=deep+'/admin'
  if route=='gitfile':
   os.mkdir('checkout');Path('checkout/.git').write_text('gitdir: ../admin\n')
   target='checkout'
  else:
   shutil.copytree(checkout,'.checkout')
   # The relative alternate is short from this deep checkout's objects cwd.
   shutil.rmtree('.checkout/.git/objects');os.makedirs('.checkout/.git/objects/info');os.mkdir('.checkout/.git/objects/pack')
   Path('.checkout/.git/objects/info/alternates').write_text('../../../admin/objects\n')
   Path('.checkout/.git/refs/heads/main').write_bytes(git('-C',source,'rev-parse','HEAD').stdout)
   target='.checkout'
  health=git('-C',target,'fsck','--no-dangling');read=git('-C',target,'show','HEAD:f.txt')
  explicit=git('-c','core.bare=true','--git-dir=admin','show','HEAD:f.txt')
  values={name:observed(module.record,target) for name,module in [('475f63278',baseline_reader()),('ced02912b',ced_reader()),('head',dependencies)]}
  assert values['head'].get('error')==values['475f63278'].get('error')==values['ced02912b'].get('error')
  p=folders.canonical(absolute)
  record_property('relative-long',json.dumps(dict(route=route,length=len(os.fsencode(absolute)),raw_stat=stat_result(absolute),relative_stat=stat_result('admin'),canonical_stat=stat_result(p),spelled=observed(dependencies._spelled_location,p),read=read.returncode,read_error=read.stderr.decode(errors='replace'),fsck=health.returncode,explicit_read=explicit.returncode,explicit_error=explicit.stderr.decode(errors="replace"),records=values),sort_keys=True))
 finally:
  os.fchdir(parent_fd)
  # rmtree's fd traversal also handles absolute paths beyond PATH_MAX.
  shutil.rmtree('p'*70)
  os.fchdir(old_fd);os.close(parent_fd);os.close(old_fd)

@pytest.mark.parametrize('root_length',[180,360,370,371,500,995])
def test_length_fixture_outcome(tmp_path,root_length,record_property):
 # Exercise the padding helper directly and report its precondition rather
 # than treating arbitrary long TMPDIR as a supported CI configuration.
 root=base_of_length(tmp_path,root_length)
 result=observed(base_of_length,root,371)
 record_property('fixture-length',json.dumps(dict(root_length=root_length,target=371,result={k:str(v) for k,v in result.items()}),sort_keys=True))
 if root_length>=371:assert result.get('error')=='FileExistsError',result
 elif root_length==370:assert result.get('error')=='AssertionError',result
 else:assert 'value' in result,result
