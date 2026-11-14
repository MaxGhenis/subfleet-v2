"""Attempt a coherent named-file commit without touching other staged work."""
import json
from pathlib import Path
import subprocess
import sys

data = Path(__file__).resolve().parent
root = data.parents[2]
label, message, *paths = sys.argv[1:]
git = ['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null']
records = []
for command in (git + ['add', '--', *paths],
                git + ['commit', '--only', '-m', message, '--', *paths]):
    result = subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=60)
    records.append({'command': command, 'returncode': result.returncode,
                    'stdout': result.stdout, 'stderr': result.stderr})
    if result.returncode:
        break
(data / (label + '.json')).write_text(json.dumps(records, indent=2) + '\n')
print(json.dumps(records, indent=2))
raise SystemExit(records[-1]['returncode'])
