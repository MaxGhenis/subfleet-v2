"""Check r12's executable scope, preservation requirements and commit trailers."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys

DATA = Path(__file__).resolve().parent
ROOT = DATA.parents[2]
meta = Path(sys.argv[1])
git = ['git', '--git-dir=' + str(meta), '--work-tree=' + str(ROOT)]
source = (ROOT / 'subfleet/dependencies.py').read_text()
before = (DATA / 'input-dependencies.py.txt').read_text()
old = 'return exc.errno in (errno.ELOOP, errno.ENAMETOOLONG)'
assert before.count(old) == 1
expected = before.replace(old, 'return exc.errno == errno.ELOOP')


def executable(text):
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
                node.body.pop(0)
    return ast.dump(tree, include_attributes=False)


assert executable(source) == executable(expected)
changed = subprocess.check_output(git + ['diff', '--name-only', '92a8635a05d6', '--', 'subfleet/'], text=True).splitlines()
assert changed == ['subfleet/dependencies.py'], changed
existing = subprocess.check_output(git + ['diff', '--name-only', '--diff-filter=M', '92a8635a05d6'], text=True).splitlines()
assert set(existing) == {'subfleet/dependencies.py', 'tests/fake/test_retention_dependency_review_r12.py', 'tests/unit/test_retention_location_readable.py', 'docs/desktop/retention-archive.md', 'docs/reports/2026-10-27-retention-dependency-pins.md'}, existing
trail = 'Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>'
messages = subprocess.check_output(git + ['log', '--format=%B%x00', '92a8635a05d6..HEAD'], text=True).split('\0')
assert all(m.strip().endswith(trail) for m in messages if m.strip())
result = dict(executable_change='Only errno tuple membership becomes equality with ELOOP',
              all_other_product_ast_identical=True, modified_existing_files=existing,
              source_sha256=hashlib.sha256(source.encode()).hexdigest(),
              committed_trailers_valid=True)
(DATA / 'product-audit.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
