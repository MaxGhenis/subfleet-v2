"""Render evidence tables for the final report and handoff."""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent
s = json.loads((DATA / 'summary.json').read_text())
lines = ['| File | Passed | Start UTC (2026-10-31) | Load 1/5/15 |', '|---|---:|---|---|']
for row in s['files']:
    name = row['file'].replace('tests/fake/', 'F/').replace('tests/unit/', 'U/')
    if name.startswith('docs/'):
        name = 'Data/' + Path(name).name
    lines.append('| ' + name + ' | ' + str(row['passed']) + ' | ' + row['start'][11:19] + ' | '
                 + ' / '.join(f'{v:.2f}' for v in row['load']) + ' |')
(DATA / 'file-table.md').write_text('\n'.join(lines) + '\n')
lines = ['| Common pointer | Own store | Original /100 | Focused /40 |', '|---|---|---:|---:|']
for common in ('absent', 'empty', 'lf', 'crlf', 'target'):
    for own in ('absent', 'plain', 'symlink', 'alternate'):
        lines.append(f"| {common} | {own} | {s['cells']['original'][common][own]} | {s['cells']['focused'][common][own]} |")
(DATA / 'class-table.md').write_text('\n'.join(lines) + '\n')
lines = ['| Dimension | Class | Original /100 | Focused /40 |', '|---|---|---:|---:|']
for key in ('layout', 'prefix', 'target', 'common', 'own', 'named', 'padding', 'pad_file', 'parent'):
    before = s['dimensions']['original'].get(key, {})
    after = s['dimensions']['focused'].get(key, {})
    for value in dict.fromkeys([*before, *after]):
        lines.append(f'| {key} | {value} | {before.get(value, 0 if before else "—")} | {after.get(value, 0 if after else "—")} |')
(DATA / 'dimension-table.md').write_text('\n'.join(lines) + '\n')
lines = ['| Mutant | Named killer | Killed | Same assertion as r6 |', '|---|---|---|---|']
for m in s['mutants']:
    tests = [test for test in m['tests'] if '::' in test]
    tests = [test.replace('tests/fake/test_retention_dependency_review_', '').replace('.py::', '::')
             .replace('tests/unit/test_retention_pointer_whitespace::', 'W::')
             .replace('tests/fake/test_retention_pointer_failures::', 'PF::')
             .replace('tests/fake/test_retention_parent_hint::', 'PH::') for test in tests]
    tests = ['P::' + test.split('::', 1)[1] if '/probes::' in test else test for test in tests]
    same = m['same_assertion_as_r6']
    lines.append(f"| {m['name']} | {'; '.join(tests)} | {'Yes' if m['killed'] else 'No'} | {'New' if same is None else ('Yes' if same else 'No')} |")
(DATA / 'mutant-table.md').write_text('\n'.join(lines) + '\n')
