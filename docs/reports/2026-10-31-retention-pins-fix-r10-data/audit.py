"""Durable byte checks and final source/delivery manifest."""
import ast
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
before = old('subfleet/dependencies.py').decode()
current = (ROOT / 'subfleet/dependencies.py').read_text()
def functions(source):
    tree = ast.parse(source)
    return {node.name: ast.get_source_segment(source, node)
            for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
old_functions, new_functions = functions(before), functions(current)
for name in ['_walk', '_target', 'scan', 'resolve', 'text', '_alternates', 'check', 'record']:
    assert old_functions[name] == new_functions[name], name
checks['untouched_reader_functions'] = {name: digest(new_functions[name].encode())
    for name in ['_walk', '_target', 'scan', 'resolve', 'text', '_alternates', 'check', 'record']}
doc = 'docs/desktop/retention-archive.md'
def differences(text):
    return text.split('### Listed differences', 1)[1].split('## 11. Accounting', 1)[0]
assert differences(old(doc).decode()) == differences((ROOT / doc).read_text())
checks['listed_differences_unchanged'] = True
(DATA / 'byte-audit.json').write_text(json.dumps(checks, indent=2) + '\n')
print(json.dumps(checks, indent=2))
