"""Select only unfinished nodes from completed, time-bounded ordinary holds."""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent
runs = []
for path in DATA.glob('*.json'):
    try:
        value = json.loads(path.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        continue
    if isinstance(value, dict) and 'phases' in value and 'end' in value:
        runs.append(value)
passed = {p['node'] for r in runs if not r['name'].startswith(('red-', 'mutant-'))
          for p in r['phases'] if p['phase'] == 'call' and p['outcome'] == 'passed'}
plan = []
scheduled = set()
for run in sorted(runs, key=lambda r: (r['name'].startswith('dependency-property'), r['start'])):
    if run['exit'] != 2 or run['name'].startswith(('red-', 'mutant-')):
        continue
    assert run['seconds'] >= 535, run['name']
    nodes = [n for n in run['collected'] if n not in passed and n not in scheduled]
    scheduled.update(nodes)
    # A whole Hypothesis node keeps its settings and gets its own invocation.
    heavy = [n for n in nodes if n.startswith('tests/unit/test_retention_dependency_properties.py::')]
    rest = [n for n in nodes if n not in heavy]
    groups = [rest[i:i+5] for i in range(0, len(rest), 5)] + [[n] for n in heavy]
    for i, group in enumerate(groups):
        plan.append(['early-resume-' + run['name'] + '-' + str(i), group])
(DATA / 'early-resume-plan.json').write_text(json.dumps(plan, indent=2) + '\n')
print('Planned',len(plan),'holds for',len(scheduled),'previously unfinished nodes.')
