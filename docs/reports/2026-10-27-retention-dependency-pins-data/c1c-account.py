"""Summarize complete per-phase coverage, including interrupted holds honestly."""
from __future__ import annotations
import collections
import json
import hashlib
from pathlib import Path

DATA = Path(__file__).resolve().parent


def outcomes(path):
    nodes = collections.defaultdict(dict)
    for line in path.read_text().splitlines():
        row = json.loads(line)
        nodes[row['nodeid']][row['when']] = row
    complete, counts, failed, partial = set(), collections.Counter(), [], []
    for node, phases in nodes.items():
        result = phases.get('call', {})
        if not result and phases.get('setup', {}).get('outcome') == 'skipped':
            result = phases['setup']
        if any(row['outcome'] == 'failed' for row in phases.values()):
            counts['failed'] += 1
            failed.append(node)
        elif phases.get('teardown', {}).get('outcome') == 'passed' and result.get('outcome') in ('passed', 'skipped'):
            complete.add(node)
            counts['xfailed' if result.get('xfail') else result['outcome']] += 1
        else:
            counts['incomplete'] += 1
            partial.append(node)
    return complete, dict(counts), failed, partial


def summarize():
    initial = set((DATA / 'remaining-nodes.txt').read_text().splitlines())
    start = set((DATA / 'c1c-start-remaining.txt').read_text().splitlines())
    reused = set()
    for file in DATA.glob('c1b-*-results.jsonl'):
        reused.update(outcomes(file)[0])
    assert len(initial) == 759 and len(start) == 732
    assert initial - reused == start
    completed, holds, failures = set(), [], []
    records = [json.loads(line) for line in (DATA / 'c1c-holds.jsonl').read_text().splitlines()]
    for record in records:
        result = DATA / (record['label'] + '-results.jsonl')
        done, counts, failed, partial = outcomes(result) if result.exists() else (set(), {}, [], [])
        coverage = record['label'].startswith('c1c-remaining-')
        if coverage:
            completed.update(done)
            failures.extend(failed)
        holds.append({**record, 'counts': counts, 'partial': partial, 'coverage': coverage})
    # In-progress results are useful but only complete phases count.
    for file in DATA.glob('c1c-remaining-*-results.jsonl'):
        done, _, failed, _ = outcomes(file)
        completed.update(done)
        failures.extend(failed)
    remaining = sorted(start - completed)
    summary = {'production_checkpoint': '6ca8f4b0bc36eca4142b7183a2de5c3eab24e300',
               'oracle_sha256': hashlib.sha256(Path('tests/fake/test_detached_retention_fence.py').read_bytes()).hexdigest(),
               'planned_unique': 1104, 'lost_salvage_remaining': 759, 'reused_replacement': len(initial & reused),
               'process_start_remaining': len(start), 'completed_this_process': len(start & completed),
               'remaining': len(remaining), 'unresolved_failures': sorted(set(failures) - completed), 'holds': holds}
    (DATA / 'c1c-current-remaining.txt').write_text(''.join(n + '\n' for n in remaining))
    (DATA / 'c1c-accounting.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps({key: value for key, value in summary.items() if key != 'holds'}, indent=2))
    for hold in holds:
        print(hold['label'], hold['counts'], 'outside' if hold['outside_lock'] else 'locked',
              'load', hold.get('admitted', {}).get('load', [None])[0] if hold.get('admitted') else None,
              'rc', hold['rc'])
    return summary


if __name__ == '__main__':
    summarize()
