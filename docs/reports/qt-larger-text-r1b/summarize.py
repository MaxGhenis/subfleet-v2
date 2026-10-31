"""Rebuild durable evidence from incremental records without rerunning tests."""
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SCRATCH = Path('/private/tmp/sf-fix284r1b')
OUT = ROOT / 'docs/reports/2026-10-30-qt-larger-text-r1b-evidence.json'


def read(path):
    return json.loads(path.read_text()) if path.exists() else None


def build():
    previous = read(OUT) or {}
    runs = []
    for queued in sorted(HERE.glob('*.queued.json')):
        name = queued.name.removesuffix('.queued.json')
        items = HERE / f'{name}.items.jsonl'
        records = [json.loads(s) for s in items.read_text().splitlines()] if items.exists() else []
        results = [e for e in records if e['when'] == 'call'
                   or (e['when'] != 'call' and e['outcome'] == 'failed')]
        counts = defaultdict(Counter)
        for item in results:
            outcome = 'error' if item['when'] != 'call' else item['outcome']
            counts[item['nodeid'].split('::')[0]][outcome] += 1
        runs.append({
            'name': name, 'queued': read(queued),
            'start': read(HERE / f'{name}.start.json'),
            'end': read(HERE / f'{name}.end.json'),
            'boundary': read(HERE / f'{name}.boundary.json'),
            'outside_lock': read(HERE / f'{name}.outside-lock.json'),
            'process': read(HERE / f'{name}.process.json'),
            'process_ended': read(HERE / f'{name}.process-ended.json'),
            'collection': read(HERE / f'{name}.collection.json'),
            'counts': dict(counts), 'items': results,
        })
    sources = [
        'app/Sources/ConversationOpeningPin.swift', 'app/Sources/UIWindow.swift',
        'tests/frontend/ReviewPR284PinProbe.swift',
        'tests/frontend/ReviewR219Fix8ViewProbe.swift',
        'tests/frontend/test_review_pr284_pin.py',
        'tests/frontend/test_review_pr284_resizes.py',
        'tests/frontend/test_review_r219_fix10_view.py',
        'tests/frontend/test_review_r219_fix8_mutations.py',
        'tests/frontend/test_review_r219_fix8_view.py',
        'tests/fixtures/qt-flake/pr284-landed-replay.json',
        'tests/fixtures/qt-flake/pr284-pending-attempt2.json',
    ]
    hashes = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sources}
    initial = HERE / 'initial-source-hashes.json'
    if not initial.exists():
        initial.write_text(json.dumps(hashes, indent=2) + '\n')
    assert hashes == read(initial), 'source changed during validation'
    native = []
    for path in sorted((SCRATCH / 'cases').glob('**/result.json')):
        if path.parent.is_symlink():
            continue
        result = read(path)
        if not isinstance(result, dict):
            continue
        summary = {k: result[k] for k in ['kind', 'layout', 'resizes'] if k in result}
        if 'steps' in result:
            summary['step_reveals'] = {s['step']: s['reveals'] for s in result['steps'] if 'reveals' in s}
        if 'trace' in result:
            suspended = False
            releases = []
            for entry in result['trace']:
                if 'suspended' not in entry:
                    continue
                if suspended and not entry['suspended']:
                    releases.append({'at': entry.get('at'), 'event': entry['event']})
                suspended = entry['suspended']
            summary['real_releases'] = releases
        native.append({'path': str(path.relative_to(SCRATCH)),
                       'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), **summary})
    pin_inputs = {str(p.relative_to(SCRATCH)): read(p)
                  for p in (SCRATCH / 'cases').glob('**/review-pin*/inputs.json')}
    native_by_path = {item['path']: item for item in previous.get('native_results', [])}
    native_by_path.update({item['path']: item for item in native})
    native = list(native_by_path.values())
    pin_inputs = {**previous.get('pin_inputs', {}), **pin_inputs}
    evidence = {
        'job_start': '2026-10-31T02:34:34Z',
        'inherited_head': '98924e35c1fdacac5471c974d8063214a6d165db',
        'red_head': '26ee5d6f2', 'scratch': str(SCRATCH),
        'darwin_scratch_denied': True, 'shared_git_metadata_denied': True,
        'private_git_metadata': '.git-local',
        'bundle': 'docs/reports/qt-larger-text-217-r1b.bundle',
        'source_sha256': hashes, 'runs': runs, 'native_results': native,
        'pin_inputs': pin_inputs,
        'navigation': read(HERE / 'navigation-input.json'),
        'two_reveal_input': read(HERE / 'two-reveal-input.json'),
        'two_reveal_comparison': read(HERE / 'two-reveal-comparison.json'),
        'attempt2_comparison': read(HERE / 'attempt2-comparison.json'),
        'mutation_kill_messages': [
            {'run': path.stem, 'message': line}
            for path in HERE.glob('round*.log')
            for line in path.read_text().splitlines() if ' killed by ' in line
        ],
        'resource': {'current_load': os.getloadavg(),
                     'max_reported_child_rss_bytes': max(
                         (r['end']['max_child_rss_bytes'] for r in runs if r['end']), default=0)},
    }
    for key in ['cleanup', 'resource_checkpoint_bytes', 'delivery']:
        if key in previous:
            evidence[key] = previous[key]
    OUT.write_text(json.dumps(evidence, indent=2) + '\n')
    for run in runs:
        print(run['name'], 'queued' if not run['start'] else run['start']['start'],
              run['counts'], 'done' if run['end'] else 'unfinished')
    return evidence


if __name__ == '__main__':
    build()
