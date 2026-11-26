"""Reconstruct unfinished prior coverage from collection order and actual results.

Run after c1-planned-nodes.json has been collected with pytest's own -k matcher.
Interrupted JUnit files include an unnamed partial testcase: it is never counted.
"""
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

DATA = Path(__file__).resolve().parent
plans = json.loads((DATA / 'c1-planned-nodes.json').read_text())
planned = {node for plan in plans for node in plan['nodes']}
completed = set()
sources = {}
for file in sorted(DATA.glob('*.xml')):
    nodes = set()
    for case in ET.parse(file).findall('.//testcase'):
        if not case.get('classname') or not case.get('name'):
            continue
        if case.find('failure') is not None or case.find('error') is not None:
            continue
        node = case.get('classname').replace('.', '/') + '.py::' + case.get('name')
        nodes.add(node)
    sources[file.name] = sorted(nodes)
    completed.update(nodes)

# Quiet output still gives an ordered prefix, including the intentional xfail.
log = (DATA / 'admission-a.log').read_text()
markers = ''.join(re.match(r'^[.sxEF]+', line)[0] for line in log.splitlines()
                  if re.match(r'^[.sxEF]+(?:\s|$)', line))
assert markers.count('.') == 162 and markers.count('x') == 1, markers
admission = next(p['nodes'] for p in plans if p['plan'] == 'admission-plan.json' and p['label'] == 'admission-a')
assert len(admission) == 165, len(admission)
sources['admission-a.log:completed-prefix'] = admission[:len(markers)]
completed.update(admission[:len(markers)])

# The earlier archive interruption ended in the 16th archive-1 node. Its 15
# completed nodes are all included in the subsequent completed archive-1 hold.
archive = json.loads((DATA / 'retention-archive-1-collected.json').read_text())
assert set(archive[:15]) <= completed
sources['retention-archive-a.log:redundant-prefix'] = archive[:15]

for label, count in [('retention-light', 37), ('admission-b-1', 21)]:
    collected = json.loads((DATA / (label + '-collected.json')).read_text())
    assert set(collected[:count]) <= completed
    assert collected[count] not in sources[label + '.xml']

remaining = sorted(planned - completed)
(DATA / 'remaining-nodes.txt').write_text(''.join(n + '\n' for n in remaining))
accounting = {'planned_unique': len(planned), 'completed_planned': len(planned & completed),
              'remaining': len(remaining), 'completed_sources': sources,
              'plans': [{'file': p['plan'], 'label': p['label'], 'planned': len(p['nodes']),
                         'remaining': len(set(p['nodes']) - completed)} for p in plans]}
(DATA / 'c1-remaining-accounting.json').write_text(json.dumps(accounting, indent=2) + '\n')
print(json.dumps({k: v for k, v in accounting.items() if k not in ('completed_sources', 'plans')}, indent=2))
