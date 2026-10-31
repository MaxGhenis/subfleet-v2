"""Independently join all planned nodes to saved completion evidence, without reruns."""
from pathlib import Path
import json
from collections import defaultdict

DATA=Path(__file__).resolve().parent
plans=json.loads((DATA/'c1-planned-nodes.json').read_text())
planned_by=defaultdict(list)
for plan in plans:
    for node in plan['nodes']:
        planned_by[node].append([plan['plan'],plan['label']])
assert len(planned_by)==1104
proof=defaultdict(list)
old=json.loads((DATA/'c1-remaining-accounting.json').read_text())
for source,nodes in old['completed_sources'].items():
    for node in nodes:
        proof[node].append({'source':source,'outcome':'completed-prefix-or-junit'})
original=set((DATA/'c1-original-remaining-nodes.txt').read_text().splitlines())
assert set(planned_by)-proof.keys()==original
assert len(original)==761
for pattern in ('c1-*-results.jsonl','c1b-*-results.jsonl','c1c-remaining-*-results.jsonl'):
    for file in sorted(DATA.glob(pattern)):
        phases=defaultdict(dict)
        for line in file.read_text().splitlines():
            row=json.loads(line)
            phases[row['nodeid']][row['when']]=row
        for node,results in phases.items():
            if any(result['outcome']=='failed' for result in results.values()):
                continue
            call=results.get('call')
            if call is None and results.get('setup',{}).get('outcome')=='skipped':
                call=results['setup']
            if call and call['outcome'] in ('passed','skipped') and results.get('teardown',{}).get('outcome')=='passed':
                proof[node].append({'source':file.name,'outcome':call['outcome'],'xfail':call.get('xfail')})
remaining=set(planned_by)-proof.keys()
assert remaining==set((DATA/'c1c-current-remaining.txt').read_text().splitlines())
rows=[{'nodeid':node,'planned_by':planned_by[node],'completion_evidence':proof[node]} for node in sorted(planned_by)]
summary={'planned':len(rows),'completed':len(rows)-len(remaining),'remaining':len(remaining)}
(DATA/'c1c-planned-status.json').write_text(json.dumps({'summary':summary,'nodes':rows},indent=2)+'\n')
print(summary)
