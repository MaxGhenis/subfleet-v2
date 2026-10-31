import collections,json
from pathlib import Path
D=Path(__file__).resolve().parent
old=D.parent/'docs/reports/2026-10-31-retention-pins-fix-r10-data'
files=json.loads((D/'earlier-files.json').read_text())
nodes=collections.defaultdict(set);seconds={}
for p in [*old.glob('*.json'),*(D.parent/'docs/reports/2026-10-31-retention-pins-review-r10-data').glob('*.json')]:
 r=json.loads(p.read_text())
 if not isinstance(r,dict) or 'phases' not in r:continue
 if r.get('name','').startswith(('mutant-','red-')):continue
 for n in r.get('collected',[]):
  if n.split('::')[0] in files:nodes[n.split('::')[0]].add(n)
 for phase in r['phases']:
  if phase['phase']=='call' and phase['outcome']=='passed':
   seconds[phase['node']]=min(seconds.get(phase['node'],float('inf')),phase['duration'])
plans=[]
for file in files:
 assert nodes[file],file
 group=[];cost=0;index=0
 def emit():
  global group,cost,index
  if group:
   index+=1;plans.append([f'earlier-{Path(file).stem}-{index}',group]);group=[];cost=0
 for node in sorted(nodes[file]):
  if 'test_real_lsof_sees_a_process_whose_cwd_is_in_the_tree' in node:continue
  if 'test_early_fences_equal_reservation_before_and_after_quarantine' in node:continue
  estimate=seconds.get(node,150)+2
  property_node=('properties.py' in file or any(s in node for s in ['test_no_schedule','test_interleavings','test_fence_hold_against_a_model','test_turn_folder_pin_is_exactly']))
  if property_node:
   emit();group=[node];emit();continue
  if group and (cost+estimate>180 or len(group)>=20):emit()
  group.append(node);cost+=estimate
 emit()
(D/'earlier-plan.json').write_text(json.dumps(plans,indent=2))
print(len(files),'files',sum(len(ns) for ns in nodes.values()),'nodes',len(plans),'holds')
