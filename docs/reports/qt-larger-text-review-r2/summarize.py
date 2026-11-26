"""Retain review evidence in the worktree before scratch cleanup."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SCRATCH = Path('/private/tmp/sf-r284r2')
prior = json.loads((ROOT / 'docs/reports/2026-10-30-qt-larger-text-r1b-evidence.json').read_text())


def read(path):
    return json.loads(path.read_text()) if path.exists() else None


runs = []
totals = defaultdict(Counter)
for queued in HERE.glob('*.queued.json'):
    name = queued.name.removesuffix('.queued.json')
    rows = [json.loads(line) for line in (HERE / (name + '.items.jsonl')).read_text().splitlines()] \
        if (HERE / (name + '.items.jsonl')).exists() else []
    counts = defaultdict(Counter)
    for row in rows:
        if row['when'] == 'call' or row['outcome'] in ('failed', 'xfailed'):
            counts[row['nodeid'].split('::')[0]][row['outcome']] += 1
    for file, values in counts.items():
        totals[file].update(values)
    runs.append({
        'name': name,
        **{field: read(HERE / (name + '.' + suffix + '.json')) for field, suffix in [
            ('queued', 'queued'), ('start', 'start'), ('end', 'end'),
            ('process', 'process'), ('process_ended', 'process-ended'),
            ('outside_lock', 'outside-lock'), ('boundary', 'boundary'),
            ('collection', 'collection'),
        ]},
        'counts': dict(counts),
    })
runs.sort(key=lambda row: (row['start'] or row['queued']) .get('start', row['queued']['queued']))
native = []
for directory in (SCRATCH / 'cases').glob('*'):
    if not (HERE / (directory.name + '.process-ended.json')).exists():
        continue
    for path in directory.rglob('*.json'):
        if path.name not in {'result.json', 'resize-result.json', 'probe-failure.json', 'inputs.json', 'schedule.json'}:
            continue
        relative = path.relative_to(SCRATCH / 'cases')
        target = HERE / 'native' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        native.append(str(target.relative_to(ROOT)))
source_hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                 for name in prior['source_sha256']}
assert source_hashes == prior['source_sha256'], 'review must preserve all inherited product/test sources'
evidence = {
    'review_start': '2026-10-31T05:19:50Z',
    'head': '6700b9bbc6268981e3949a09fb3879f53cfecd7a',
    'updated': datetime.now(timezone.utc).isoformat(),
    'scratch': str(SCRATCH), 'darwin_scratch_denied': True,
    'source_sha256': source_hashes, 'inherited_sources_unchanged': True,
    'runs': runs, 'counts_per_file': dict(totals), 'native_files': native,
    'repeat_collection_counts': Counter(read(HERE / 'repeat-larger.collection.json') or []),
    'saved_41_comparison': read(HERE / 'saved-41-comparison.json'),
    'fresh_two_comparison': read(HERE / 'fresh-two-comparison.json'),
    'attempt2_comparison': read(HERE / 'attempt2-comparison.json'),
    'navigation': read(HERE / 'navigation-input.json'),
    'navigation_orderings': {file.name: read(file) for file in HERE.glob('navigation-*.json')
                            if file.name != 'navigation-input.json'},
    'release_mutant_audit': read(HERE / 'release-mutant-audit.json'),
    'review_files_sha256': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in [ROOT / 'tests/frontend/test_review_pr284_r2.py',
                                        HERE / 'diagnostics.py', HERE / 'NavigationInput.swift',
                                        HERE / 'runner.py', HERE / 'remaining.py']},

    'not_run': ['tests/frontend/test_core_live.py (prohibited person-only daemon gate)',
                'fresh CI matrix (hub-owned)', 'Python 3.12', 'CI macOS image'],
    'commits_or_publications': False,
}
(HERE / 'evidence.json').write_text(json.dumps(evidence, indent=2) + '\n')
print(json.dumps({'counts_per_file': dict(totals), 'native_files': len(native)}, indent=2))
