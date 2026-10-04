"""Foreground, sequential pytest files with exact-node baseline comparisons."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / '.horizon-checks'
PYTHON = ROOT / '.venv-312/bin/python'
records = []


def run(cwd, nodes, label):
    log = ARTIFACTS / (label + '.log')
    xml = ARTIFACTS / (label + '.xml')
    env = {**os.environ, 'PYTHONPATH': str(ARTIFACTS) + os.pathsep + str(cwd),
           'UV_CACHE_DIR': str(ROOT / '.uv-cache'),
           'SUBFLEET_HOME': str(ARTIFACTS / 'isolated-state')}
    started = time.monotonic()
    with log.open('w') as stream:
        result = subprocess.run([str(PYTHON), '-m', 'pytest', '-q', '--tb=short',
                                 '-p', 'sf_horizon_results', '--junitxml=' + str(xml), *nodes],
                                cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
    cases = []
    if xml.exists():
        for case in ET.parse(xml).getroot().iter('testcase'):
            props = {p.attrib['name']: p.attrib['value'] for p in case.findall('./properties/property')}
            status = next((s for s in ('failure', 'error', 'skipped') if case.find(s) is not None), 'passed')
            cases.append({'nodeid': props.get('nodeid', case.attrib['name']), 'status': status,
                          'detail': case.find(status).text if status != 'passed' else None})
    counts = {s: sum(c['status'] == s for c in cases) for s in ('passed', 'failure', 'error', 'skipped')}
    record = {'label': label, 'nodes': nodes, 'exit': result.returncode, 'seconds': round(time.monotonic()-started, 2),
              'counts': counts, 'cases': cases}
    print(label, record['exit'], counts, record['seconds'], flush=True)
    return record


for suite in sys.argv[1:]:
    priority = ['test_decision_horizon.py', 'test_picker.py', 'test_scheduler_weekly.py', 'test_route_check.py', 'test_scheduler_split.py']
    files = sorted((ROOT / 'tests' / suite).rglob('test_*.py'),
                   key=lambda p: (priority.index(p.name) if p.name in priority else len(priority), str(p)))
    for i, file in enumerate(files):
        relative = str(file.relative_to(ROOT))
        label = suite + '-' + file.stem
        record = run(ROOT, [relative], label)
        records.append(record)
        for j, case in enumerate(record['cases']):
            if case['status'] in ('failure', 'error'):
                baseline = run(ARTIFACTS / 'baseline', [case['nodeid']], label + '-baseline-' + str(j))
                case['baseline'] = baseline
                print('Failure:', case['nodeid'], '\n', (case['detail'] or '')[-1800:], flush=True)
                for compared in baseline['cases']:
                    if compared['status'] in ('failure', 'error'):
                        print('Baseline:', (compared['detail'] or '')[-1800:], flush=True)
        (ARTIFACTS / 'results.json').write_text(json.dumps(records, indent=2))
print('COMPLETE', flush=True)
