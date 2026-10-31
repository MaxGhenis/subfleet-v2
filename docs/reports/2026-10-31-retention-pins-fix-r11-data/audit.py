"""Final source boundary and complete-node coverage audit, read-only."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess

from tables import receipts

DATA = Path(__file__).resolve().parent
ROOT = DATA.parents[2]


def committed(revision, path):
    return subprocess.check_output(['git', 'show', revision + ':' + path], cwd=ROOT)


paths = ['docs/acceptance-contract.md', 'tests/unit/test_retention_location_inputs.py',
         'tests/unit/test_retention_location_spelling.py', 'tests/unit/test_retention_pointer_inputs.py']
audit = {}
for path in paths:
    current = (ROOT / path).read_bytes()
    assert current == committed('ced02912b', path), path
    audit[path] = dict(unchanged=True, sha256=hashlib.sha256(current).hexdigest())
snapshot = ROOT / 'docs/reports/2026-10-31-retention-pins-fix-r10-data/base-dependencies.py.txt'
assert snapshot.read_bytes() == committed('475f63278', 'subfleet/dependencies.py')
assert (DATA / 'ced-dependencies.py.txt').read_bytes() == committed('ced02912b', 'subfleet/dependencies.py')
audit['reader_snapshots_match_commits'] = True
path = 'tests/fake/test_retention_dependency_review_r11.py'
mark = "@pytest.mark.xfail(strict=True, reason='R11-F1: return filter drops a readable case alias without incomplete evidence')\n"
assert (ROOT / path).read_text() == committed('ced02912b', path).decode().replace(mark, '')
audit['r11_only_strict_mark_removed'] = True
before = (DATA / 'ced-dependencies.py.txt').read_text()
after = (ROOT / 'subfleet/dependencies.py').read_text()


def functions(source):
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == '_Aliases')
    return {n.name: ast.get_source_segment(source, n) for n in cls.body if isinstance(n, ast.FunctionDef)}


old, new = functions(before), functions(after)
for name in ('_walk', '_target', 'resolve', 'text', 'scan', 'check'):
    assert old[name] == new[name], name
    audit[name] = dict(unchanged=True, sha256=hashlib.sha256(new[name].encode()).hexdigest())
for source in (before, after):
    tree = ast.parse(source)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_spelled_location')
    if source == before:
        old_equations = ast.get_source_segment(source, function)
    else:
        assert old_equations == ast.get_source_segment(source, function)
audit['spelled_equations_unchanged'] = True
normal, linked = set(), set()
for run in receipts():
    if run.get('exit') != 0:
        continue
    destination = linked if run['name'].startswith('linked-') else normal if run['name'].startswith('green-') else None
    if destination is not None:
        destination.update(p['node'] for p in run['phases'] if p['phase'] == 'call' and p['outcome'] == 'passed')
audit['new_file_normal_nodes'] = sum(n.startswith('tests/unit/test_retention_location_readable.py::') for n in normal)
audit['new_file_linked_nodes'] = sum(n.startswith('tests/unit/test_retention_location_readable.py::') for n in linked)
assert audit['new_file_normal_nodes'] == audit['new_file_linked_nodes'] == 36
audit['first_nine_normal_nodes'] = len(normal)
assert len(normal) == 112
(DATA / 'final-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
print('Final source audit passed; 36 new normal and linked nodes, 112 first-nine green nodes')
