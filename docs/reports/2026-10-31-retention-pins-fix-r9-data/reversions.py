"""One generated-only property per locked hold in an isolated source copy."""
import ast
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from batch import call_waited

DATA = Path(__file__).resolve().parent
ROOT = DATA.parents[2]
SCRATCH = Path('/private/tmp/sf-fix272r9')
source = (ROOT / 'subfleet/dependencies.py').read_text()
definitions = json.loads((DATA / 'mutants.json').read_text())
manifest = {}
relative = 'tests/unit/test_retention_location_spelling.py'
property_name = 'test_spelling_inputs_keep_the_places_real_git_traverses'

for name, mutation in definitions.items():
    if not name.startswith('r9-'):
        continue
    assert datetime.datetime.now(datetime.UTC) < datetime.datetime.fromisoformat('2026-10-31T13:56:57+00:00')
    copy = SCRATCH / ('reversion-' + name)
    try:
        copy.mkdir()
        for folder in ('subfleet', 'tests'):
            shutil.copytree(ROOT / folder, copy / folder,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copy2(ROOT / 'pyproject.toml', copy / 'pyproject.toml')
        changed = source
        for edit in mutation['edits']:
            assert changed.count(edit['old']) == 1
            changed = changed.replace(edit['old'], edit['new'])
        (copy / 'subfleet/dependencies.py').write_text(changed)
        original = (ROOT / relative).read_bytes()
        assert original == (copy / relative).read_bytes()
        tree = ast.parse(original)
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == property_name)
        assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'example'
                       for n in function.decorator_list)
        manifest[name] = dict(edits=mutation['edits'], explicit_examples=0,
                              property_byte_identical=True,
                              property_sha256=hashlib.sha256(original).hexdigest(),
                              product_sha256=hashlib.sha256(changed.encode()).hexdigest())
        (DATA / 'reversions-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        env = dict(os.environ, SF_R8_SOURCE=str(copy))
        rc = call_waited([sys.executable, str(DATA / 'run.py'), 'revert-' + name,
                          '--linked', relative + '::' + property_name], env=env)
        manifest[name]['exit'] = rc
        print(name, rc, flush=True)
    finally:
        assert '/sf-' in str(copy)
        shutil.rmtree(copy, ignore_errors=True)
    (DATA / 'reversions-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
