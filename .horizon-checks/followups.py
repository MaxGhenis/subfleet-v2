import json
from collections import defaultdict
from pathlib import Path
from run_tests import ARTIFACTS, ROOT, run

rows=json.loads((ARTIFACTS/'results.json').read_text())
groups=defaultdict(list)
for row in rows:
    for case in row['cases']:
        if case['status'] in ('failure','error') and case.get('baseline',{}).get('exit') == 0:
            groups[row['label']].append(case['nodeid'])
results=[]
for label, nodes in groups.items():
    results.append(run(ROOT,nodes,label+'-followup'))
(ARTIFACTS/'followups.json').write_text(json.dumps(results,indent=2))
