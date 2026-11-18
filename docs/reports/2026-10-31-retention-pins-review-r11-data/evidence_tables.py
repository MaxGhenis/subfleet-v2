"""Independent review tables generated from this job's receipts."""
import collections,json,os
from pathlib import Path
D=Path(__file__).resolve().parent

def read(name):
 p=D/(name+'.json');return json.loads(p.read_text()) if p.exists() else {}
def observed(name,key):
 return [json.loads(o['value']) for o in read(name).get('observations',[]) if o['key']==key]

def compress(p,root):
 parts=p.removeprefix(root+'/').split('/');out=[];i=0
 while i<len(parts):
  choices=[]
  for size in (1,2):
   unit=parts[i:i+size];count=1
   while len(unit)==size and parts[i+count*size:i+(count+1)*size]==unit:count+=1
   if count>=3:choices.append((size*count,size,count,unit))
  if choices:
   length,size,count,unit=max(choices,key=lambda c:(c[0],-c[1]))
   out.append(('('+ '/'.join(unit)+')' if size>1 else unit[0])+f'×{count}');i+=length
  else:out.append(parts[i]);i+=1
 return '<B>/'+ '/'.join(out)

name='route-equations-final' if read('route-equations-final').get('exit')==0 else 'route-independent'
routes=observed(name,'route')
table='| Route / cycle | Base → head fsck/read | Incomplete files, base = head (order preserved) | Base unspelled → head |\n|---|---|---|---|\n'
for r in routes:
 root=str(Path(r['before']['review_root']).parent)
 files=[compress(e['file'],root) for e in r['before'].get('incomplete',[])]
 bad=[compress(e['path'],root) for e in r['old_equations'] if not(e['parent'] and e['canonical'])]
 table+=f"| {r['route']} / {r['cycle']} | {r['fsck']}/{r['read']} → {r['fsck']}/{r['read']} | {'; '.join('`'+f+'`' for f in files) or '[]'} | {'; '.join('`'+p+'`' for p in bad) or '[]'} → [] |\n"
(D/'route-table.md').write_text(table)
(D/'route-comparisons.json').write_text(json.dumps(routes,indent=2))

comparisons=[];audit_counts={};all_property_comparisons=[]
for p in D.glob('*.json'):
 r=json.loads(p.read_text())
 if not isinstance(r,dict) or 'pytest_pid' not in r:continue
 a=D/('audit-'+str(r['pytest_pid'])+'.json')
 if not a.exists():continue
 data=json.loads(a.read_text())
 if r['name'].startswith(('round-','linked-')):
  all_property_comparisons+=data['comparisons']
 if r['name'].startswith('linked-'):
  audit_counts[r['name']]=dict(records=sum('actual' in x for x in data['audits']),
   storage_returns=sum(x.get('scope')=='git_storage' for x in data['audits']),
   physical_predicate_false=sum(not e['predicate'] and e['physical_link'] for x in data['audits'] if 'actual' in x for e in x['equations']),
   nonphysical_predicate_false=sum(not e['predicate'] and not e['physical_link'] for x in data['audits'] if 'actual' in x for e in x['equations']))
counts=dict(property_and_example_records=len(all_property_comparisons),route_inputs=len(observed('route-independent','route')),
 routine_inputs=len(observed('route-independent','routine')),long_prefix_inputs=len(observed('ci-longer-paths','ci-path')),
 final_route_rechecks=len(observed('route-equations-final','route')),linked_audits=audit_counts)
counts['incomplete_comparisons']=sum(counts[k] for k in ['property_and_example_records','route_inputs','routine_inputs','long_prefix_inputs','final_route_rechecks'])
(D/'evidence-comparison-counts.json').write_text(json.dumps(counts,indent=2))

# Four case-rename rows from the final green run (or initial red while pending).
case_name='r11-green' if read('r11-green').get('exit')==0 else 'r11-red'
cases=[]
r=read(case_name)
for o in r.get('observations',[]):
 if o['key']!='R11-F1':continue
 v=json.loads(o['value']);v['version']='baseline' if 'test_baseline_' in o['node'] else 'head';cases.append(v)
(D/'r11-case-comparisons.json').write_text(json.dumps(cases,indent=2))
t='| Route | Reader | Git places | Incomplete | fsck before / after rename | Same read before / after rename / after retention | Owner pruned |\n|---|---|---:|---:|---|---|---|\n'
for v in cases:
 t+=f"| {v['route']} | {v['version']} | {len(v['recorded'].get('git',[]))} | {len(v['recorded'].get('incomplete',[]))} | {v['fsck']} / {v.get('fsck_after_rename','not recorded')} | {v['before']} / {v['middle']} / {v['after']} | {'Yes' if 'owner' in v['result']['pruned'] else 'No'} |\n"
(D/'r11-case-table.md').write_text(t)
print(json.dumps(counts,indent=2))
