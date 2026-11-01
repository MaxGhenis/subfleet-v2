"""Plan only unresolved requested checks, including the known archive path trap."""
import json
from pathlib import Path

from tables import build, receipts

DATA = Path(__file__).resolve().parent
summary = build()
assert not summary['active'], summary['active']
known = {
    'test_round_trip_dirty_unpushed_detached_job_restores_every_byte_and_commit',
    'test_blob_only_in_a_local_branch_is_archived_not_omitted',
    'test_local_path_remote_is_not_a_remote',
    'test_borrowed_or_shallow_object_store_is_scratch[alternates]',
    'test_borrowed_or_shallow_object_store_is_scratch[shallow]',
    'test_survey_is_read_only_and_predicts_the_pass',
}
archive = sorted({r['node'] for r in summary['ordinary_failures']
    if not r['later_passed'] and r['node'].startswith('tests/unit/test_retention_archive.py::')
    and r['node'].split('::')[-1] in known})
plan = [['archive-path-controls', ['--archive-path', *archive]]] if archive else []
scheduled = set(archive)
for run in receipts():
    if run.get('exit') != 2 or run['name'].startswith(('red-', 'mutant-', 'retry-')):
        continue
    remaining = [n for n in run.get('collected', []) if n in summary['missing_or_unfinished'] and n not in scheduled]
    scheduled.update(remaining)
    # Split guarded whole-file holds into single named tests. A lone property
    # gets one fresh guarded attempt at its original settings.
    for index, node in enumerate(remaining):
        plan.append(['retry-' + run['name'] + '-' + str(index), [node]])
(DATA / 'retry-plan.json').write_text(json.dumps(plan, indent=2) + '\n')
unknown = sorted(set(summary['missing_or_unfinished']) - scheduled)
(DATA / 'unresolved-before-retries.json').write_text(json.dumps(dict(
    known_archive=archive, retry_plan=plan, other_unresolved=unknown), indent=2) + '\n')
print('Planned retries:', len(plan), 'Other unresolved:', unknown)
