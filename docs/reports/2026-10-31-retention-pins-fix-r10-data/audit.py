"""Durable byte checks and final source/delivery manifest."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[3]
DATA = Path(__file__).resolve().parent
BASE = '475f6327890e'

def old(path):
    return subprocess.check_output(['git', 'show', BASE + ':' + path], cwd=ROOT)

def digest(value):
    return hashlib.sha256(value).hexdigest()

paths = ['tests/unit/test_retention_location_spelling.py',
         'tests/unit/test_retention_location_inputs.py',
         'docs/acceptance-contract.md']
checks = {}
for path in paths:
    current = (ROOT / path).read_bytes()
    before = old(path)
    assert current == before, path
    checks[path] = dict(unchanged=True, sha256=digest(current))
path = 'tests/fake/test_retention_dependency_review_r10.py'
before = old(path)
kept = b''.join(line for line in before.splitlines(keepends=True)
                if not line.startswith(b'@pytest.mark.xfail(strict=True'))
assert before != kept and kept == (ROOT / path).read_bytes()
checks[path] = dict(only_strict_mark_removed=True, sha256=digest(kept))
assert (DATA / 'base-dependencies.py.txt').read_bytes() == old('subfleet/dependencies.py')
checks['baseline_snapshot'] = dict(sha256=digest(old('subfleet/dependencies.py')), base=BASE)
(DATA / 'byte-audit.json').write_text(json.dumps(checks, indent=2) + '\n')
print(json.dumps(checks, indent=2))
