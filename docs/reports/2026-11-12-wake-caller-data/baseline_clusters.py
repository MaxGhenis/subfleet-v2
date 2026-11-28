import json
import os
from pathlib import Path
import subprocess

root = Path('/private/tmp/sf-wc-b1')
rows = json.loads((root/'regression-failures.json').read_text())
first = {}
for row in rows:
    first.setdefault(row['file'], row['node'])
assert len(first) == 7
env = dict(os.environ, SF_JOB_SOURCE_ROOT=str(root/'baseline'), SF_JOB_TEST_ROOT=str(root))
command = ['python3', str(root/'run.py'), 'baseline-clusters', 'pytest', '-q',
           '-o', 'pythonpath='+str(root/'baseline'), '-p', 'sf_baseline_verify', *first.values()]
(root/'baseline-clusters-command.json').write_text(json.dumps(command,indent=2)+'\n')
raise SystemExit(subprocess.run(command, env=env).returncode)
