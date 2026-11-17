import json
from pathlib import Path
import re
import subprocess

OUT = Path('/private/tmp/sf-a2-fix1')
CHANGED = {'subfleet.store', 'subfleet.cli', 'subfleet.daemon', 'subfleet.review_subject',
           'subfleet.protocol', 'subfleet.sessions.client', 'subfleet.conversations.service'}
result = subprocess.run(['git', 'grep', '-l', '-E',
    'subfleet.*(store|cli|daemon|review_subject|protocol|sessions|conversations)|from subfleet import',
    '--', 'tests'], text=True, capture_output=True, check=True)
sources = {str(p): p.read_text(errors='replace') for p in Path('tests').rglob('*.py')}
imports = {}
for path, source in sources.items():
    found = set()
    # Import statements only, including multiline parenthesized imports. Templates
    # with embedded Python are included conservatively because children import too.
    for match in re.finditer(r'^\s*from\s+([\w.]+)\s+import\s*(\([^)]*\)|[^\n]+)', source, re.M):
        module, names = match.groups()
        found.add(module)
        names = re.sub(r'#[^\n]*', '', names).strip('() \t\n')
        for item in names.split(','):
            name = item.strip().split(' ')[0]
            found.add(module + '.' + name)
    for match in re.finditer(r'^\s*import\s+([^\n]+)', source, re.M):
        for item in match[1].split(','):
            found.add(item.strip().split(' ')[0])
    imports[path] = found

direct = {path: sorted(found & CHANGED) for path, found in imports.items() if found & CHANGED}
module_path = {path[:-3].replace('/', '.'): path for path in sources}
module_path.update({Path(path).stem: path for path in sources})
closure = set(direct)
while True:
    new = {path for path, found in imports.items() if any(module_path.get(name) in closure for name in found)}
    if new <= closure:
        break
    closure |= new
is_test = lambda path: Path(path).name.startswith('test_')
files = {}
for path in sorted(closure):
    source = sources[path]
    files[path] = {
        'imports_changed': direct.get(path, []),
        'direct_importer': path in direct,
        'test_file': is_test(path),
        'e2e': path.startswith('tests/e2e/'),
        'hazard_candidates': [f'{i}: {line.strip()}' for i, line in enumerate(source.splitlines(), 1)
            if re.search(r'gh.{0,40}(?:POST|PATCH|DELETE|merge|rerun)|git.{0,30}push|real_lsof|real_cwd|launchctl|agent-secret|real process|live state', line)],
    }
summary = {
    'changed_modules': sorted(CHANGED),
    'git_grep_command': result.args,
    'git_grep_paths': sorted(result.stdout.splitlines()),
    'direct_importer_files': sorted(direct),
    'direct_test_files': sorted(path for path in direct if is_test(path)),
    'helper_importer_files': sorted(path for path in direct if not is_test(path)),
    'transitive_test_files': sorted(path for path in closure if is_test(path)),
    'extra_transitive_test_files': sorted(path for path in closure - set(direct) if is_test(path)),
    'files': files,
}
summary['counts'] = {k: len(summary[k]) for k in ('git_grep_paths', 'direct_importer_files', 'direct_test_files',
    'helper_importer_files', 'transitive_test_files', 'extra_transitive_test_files')}
(OUT / 'importer-inventory.json').write_text(json.dumps(summary, indent=2) + '\n')
(OUT / 'importers-direct.txt').write_text(''.join(path + '\n' for path in summary['direct_test_files']))
(OUT / 'importers-transitive.txt').write_text(''.join(path + '\n' for path in summary['transitive_test_files']))
print(json.dumps(summary['counts'], indent=2))
print('Helpers: ' + ', '.join(summary['helper_importer_files']))
print('Extra transitive: ' + ', '.join(summary['extra_transitive_test_files']))
for path, row in files.items():
    if row['hazard_candidates']:
        print(path + ': ' + '\n  '.join(row['hazard_candidates']))
