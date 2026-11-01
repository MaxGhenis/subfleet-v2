"""Independent review r11 checks, all repositories and stores are fixtures."""
import contextlib, errno, io, json, os, runpy, statistics, sys, time, types
from pathlib import Path
import pytest
from subfleet import dependencies, folders
from tests.unit.test_retention_location_cycles import ROWS, cycle_route, expected_error_files
from tests.unit.test_retention_location_inputs import git, repo
from tests.unit.test_retention_locations import routine_input
D=Path(__file__).resolve().parent
BASE=types.ModuleType('subfleet.review_r11_baseline');BASE.__package__='subfleet'
exec(compile((D/'base-dependencies.py.txt').read_text(),'475f63278:dependencies.py','exec'),BASE.__dict__)

def discover(module, checkout, **kw):
 seen=[];original=module._Aliases
 class Observed(original):
  def __init__(self):
   super().__init__();seen.append(self)
 module._Aliases=Observed
 try:r=module.record(str(checkout),**kw)
 finally:module._Aliases=original
 links=set().union(*(a.links for a in seen))
 equations=[]
 for p in r.get('git',[]):
  a=os.path.join(folders.canonical(os.path.dirname(p)),os.path.basename(p))==p
  fixed=folders.canonical(p)==p
  b=p in links or fixed
  equations.append(dict(path=p,parent=a,canonical=b,raw_fixed_point=fixed,link=p in links))
 return r,equations

@pytest.mark.parametrize('route,cycle,git_exit,error_count',ROWS)
def test_all_nine_routes_against_baseline(tmp_path,route,cycle,git_exit,error_count,record_property):
 checkout=cycle_route(tmp_path,route,cycle)
 stable=repo(tmp_path.resolve()/'stable')
 health=git('-C',checkout,'fsck','--no-dangling');read=git('-C',checkout,'show','HEAD:f.txt')
 assert health.returncode==read.returncode==git_exit
 before,old_eq=discover(BASE,checkout,review_root=str(stable))
 after,new_eq=discover(dependencies,checkout,review_root=str(stable))
 assert before.get('incomplete',[])==after.get('incomplete',[])
 assert [e['file'] for e in before.get('incomplete',[])]==expected_error_files(tmp_path,route,cycle)
 assert len(before.get('incomplete',[]))==error_count
 assert all(e['parent'] and e['canonical'] for e in new_eq)
 dropped=sorted(set(before.get('git',[]))-set(after.get('git',[])))
 errors=[]
 for p in dropped:
  try:os.stat(p)
  except OSError as exc:errors.append(dict(path=p,errno=exc.errno,length=len(os.fsencode(p))))
  else:raise AssertionError(('readable dropped path',p))
 assert all(r['errno']==errno.ELOOP for r in errors)
 (tmp_path.resolve()/'loop').unlink()
 healthy_before=BASE.git_storage(str(stable));healthy_after=dependencies.git_storage(str(stable))
 assert healthy_before==healthy_after
 assert set(healthy_before)<=set(after.get('git',[]))
 row=dict(route=route,cycle=cycle,fsck=health.returncode,read=read.returncode,before=before,after=after,
  old_equations=old_eq,new_equations=new_eq,dropped=errors,healthy_before=healthy_before,healthy_after=healthy_after)
 record_property('route',json.dumps(row,sort_keys=True))

@pytest.mark.parametrize('layout',['plain','worktree','shared','new-workdir','none'])
def test_routine_controls_and_cost(tmp_path,layout,record_property):
 checkout,expected=routine_input(tmp_path,layout)
 before,old_eq=discover(BASE,checkout);after,new_eq=discover(dependencies,checkout)
 assert before==after==expected
 for p in after.get('git',[]):assert dependencies._spelled_location(p)
 row=dict(layout=layout,before=before,after=after)
 if layout=='plain':
  times={'base':[],'head':[]};calls={}
  real=folders.canonical
  for label,module in [('base',BASE),('head',dependencies)]:
   count=[]
   def counted(p):count.append(str(p));return real(p)
   folders.canonical=counted
   try:module.record(str(checkout))
   finally:folders.canonical=real
   calls[label]=len(count)
  # Alternating small sample of an ordinary record, not a load benchmark.
  for _ in range(15):
   for label,module in [('base',BASE),('head',dependencies)]:
    start=time.perf_counter();module.record(str(checkout));times[label].append((time.perf_counter()-start)*1000)
  row.update(times_ms=times,median_ms={k:statistics.median(v) for k,v in times.items()},canonical_calls=calls)
 record_property('routine',json.dumps(row,sort_keys=True))

@pytest.mark.parametrize('label',['baseline','head'])
def test_hub_route_script_unmodified(tmp_path,label,monkeypatch,record_property):
 import subfleet
 module=BASE if label=='baseline' else dependencies
 monkeypatch.setattr(subfleet,'dependencies',module)
 monkeypatch.setattr(sys,'argv',['r10-routes-probe.py',str(tmp_path.resolve())])
 for k,v in {'GIT_CONFIG_COUNT':'2','GIT_CONFIG_KEY_0':'maintenance.auto','GIT_CONFIG_VALUE_0':'false','GIT_CONFIG_KEY_1':'gc.auto','GIT_CONFIG_VALUE_1':'0'}.items():monkeypatch.setenv(k,v)
 output=io.StringIO()
 with contextlib.redirect_stdout(output):
  runpy.run_path('/Users/maxghenis/reviews/subfleet-hub/retention-live-2026-10-27/r10-routes-probe.py',run_name='__main__')
 record_property('hub-route-script',json.dumps(dict(label=label,output=output.getvalue())))
 assert len(output.getvalue().splitlines())==7
