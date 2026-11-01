"""Independent return-equation checks; exact stable input-head evidence comparison."""
import functools, json, os, types
from pathlib import Path
from subfleet import dependencies, folders
D=Path(__file__).resolve().parent
BASE=types.ModuleType('subfleet.review_r11_baseline'); BASE.__package__='subfleet'
exec(compile((D/'base-dependencies.py.txt').read_text(),'475f63278:dependencies.py','exec'),BASE.__dict__)
NODE=''
COMPARISONS=[]
AUDITS=[]
def pytest_runtest_setup(item):
 global NODE
 NODE=item.nodeid

def pytest_configure(config):
 original_git=dependencies.git_storage
 @functools.wraps(original_git)
 def storage(*args,**kwargs):
  captures=[];old=dependencies._Aliases
  class Capture(old):
   def __init__(self):super().__init__();captures.append(self)
  dependencies._Aliases=Capture
  try:places=original_git(*args,**kwargs)
  finally:dependencies._Aliases=old
  links=set().union(*(a.links for a in captures))
  equations=[]
  for p in places:
   a=os.path.join(folders.canonical(os.path.dirname(p)),os.path.basename(p))==p
   b=p in links or folders.canonical(p)==p
   equations.append(dict(path=p,parent=a,canonical=b,physical_link=p in links,predicate=dependencies._spelled_location(p)))
   assert a and b,(NODE,p,a,b)
  AUDITS.append(dict(scope='git_storage',node=NODE,input=args,equations=equations))
  return places
 dependencies.git_storage=storage
 original=dependencies.record
 @functools.wraps(original)
 def observed(*args,**kwargs):
  captures=[]
  old=dependencies._Aliases
  class Capture(old):
   def __init__(self):
    super().__init__(); captures.append(self)
  dependencies._Aliases=Capture
  try: actual=original(*args,**kwargs)
  finally: dependencies._Aliases=old
  links=set().union(*(a.links for a in captures))
  equations=[]
  for p in actual.get('git',[]):
   a=os.path.join(folders.canonical(os.path.dirname(p)),os.path.basename(p))==p
   b=p in links or folders.canonical(p)==p
   equations.append(dict(path=p,parent=a,canonical=b,physical_link=p in links,predicate=dependencies._spelled_location(p)))
   assert a and b,(NODE,p,a,b)
  row=dict(node=NODE,input=args,kwargs=kwargs,actual=actual,equations=equations)
  AUDITS.append(row)
  if 'test_retention_location_inputs.py::' in NODE or 'test_retention_location_spelling.py::' in NODE:
   baseline=BASE.record(*args,**kwargs)
   assert baseline.get('incomplete',[])==actual.get('incomplete',[]),(NODE,baseline,actual)
   assert baseline==actual,(NODE,baseline,actual)
   COMPARISONS.append(dict(node=NODE,input=args,kwargs=kwargs,baseline=baseline,actual=actual))
  return actual
 dependencies.record=observed

def pytest_sessionfinish(session,exitstatus):
 name='audit-'+str(os.getpid())
 (D/(name+'.json')).write_text(json.dumps(dict(comparisons=COMPARISONS,audits=AUDITS),indent=2))
