import json
from pathlib import Path
import re
import subprocess

out = Path('/private/tmp/sf-a2-fix1')
data = json.loads((out / 'importer-inventory.json').read_text())
final = {'tests/fake/test_state_contract.py', 'tests/unit/test_test_module_names.py',
         'tests/unit/test_invariants_index.py', 'tests/unit/test_handler_refusals.py'}
changed = {'tests/unit/test_store_review_subject_migration.py', 'tests/unit/test_store_migration_2.py', 'tests/unit/test_review_subject_normalization.py', 'tests/unit/test_review_runs_columns.py', 'tests/unit/test_caller_family_ancestry.py', 'tests/unit/test_writable_fanout.py', 'tests/fake/test_review_subject_resume.py', 'tests/unit/test_review_subject_cli.py', 'tests/unit/test_cli.py', 'tests/unit/test_store_upgrade_slots.py', 'tests/unit/test_review_subject_resume_cli.py'}
skip, first, last, safe = {}, [], [], []
for path in data['direct_test_files']:
    source = Path(path).read_text()
    reason = None
    if path in final:
        last.append(path)
        continue
    if path in changed:
        first.append(path)
        continue
    if path.startswith('tests/e2e/'):
        reason = 'E2E real daemon/provider-adapter/peer ancestry path; reserved for CI under this job brief.'
    elif path.startswith('tests/frontend/'):
        reason = 'Frontend suite excluded per coordinator; browser/Swift application harness.'
    elif path.startswith('tests/process/'):
        reason = 'Real provider or process census/ancestry suite; excluded per coordinator.'
    elif Path(path).name.startswith('test_host_push') or re.search(r'from (?:tests\.unit\.)?test_host_push(?:\w*) import', source):
        reason = 'Host-push fixture or dependent executes local git push, forbidden even with local remotes/stubs.'
    elif Path(path).name in {'test_gate_merge.py', 'test_gate_service.py', 'test_gate_admission.py'} or re.search(r'from (?:tests\.unit\.)?test_gate_merge import', source):
        reason = 'Gate publication fixture/dependent executes gh pr merge through a fake runner.'
    elif Path(path).name in {'test_review_publish_guard.py', 'test_publish_rule_round7.py', 'test_publish_rule_round9.py', 'test_publish_rule_round10.py'} or re.search(r'from (?:tests\.unit\.)?test_(?:review_publish_guard|publish_rule_round(?:7|9|10)) import', source):
        reason = 'Publication shell harness or dependent can execute forbidden git push / gh writes behind stand-ins.'
    if reason:
        skip[path] = reason
    else:
        safe.append(path)
result = {
    'counts': {'direct': len(data['direct_test_files']), 'safe': len(safe), 'skipped_files': len(skip),
               'first_stage_changed': len(first), 'last_stage_required': len(last)},
    'marker_selection_required': 'not real_lsof and not real_cwd',
    'safe_direct_importers': safe,
    'skipped_files': skip,
    'first_stage_changed_files': first,
    'last_stage_required_files': last,
    'remaining_review_notes': [
        'tests/unit/test_hooks.py receives write-shaped commands as guard payload data only, so it stays selected.',
        'Live-store guard tests use traps before filesystem access and synthetic homes, so they stay selected.',
        'real_cwd and real_lsof cases are excluded by the marker expression, including own-child cases.',
        'Other in-process unit/fake tests may launch and reap their own children; they are not existing live-state jobs.',
        'Explicit helper-import closure is inventoried separately; implicit conftest loading is not expanded into the whole suite.',
    ],
}
(out / 'importer-safe-selection.json').write_text(json.dumps(result, indent=2) + '\n')
(out / 'importers-safe-direct.txt').write_text(''.join(path + '\n' for path in safe))
(out / 'importers-skipped.txt').write_text(''.join(path + ': ' + reason + '\n' for path, reason in skip.items()))
(out / 'importers-first-stage.txt').write_text(''.join(path + '\n' for path in first))
print(json.dumps(result['counts']))
print('First stage: ' + ', '.join(first))
print('Last stage: ' + ', '.join(last))
print('Skipped: ' + ', '.join(skip))
