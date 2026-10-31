"""Run the supplied coverage batches in the foreground, resuming only unfinished nodes."""
from __future__ import annotations
import collections
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

DATA = Path(__file__).resolve().parent
CUTOFF = datetime(2026, 10, 28, 11, 46, tzinfo=timezone.utc)


def completed():
    done = set()
    for file in DATA.glob('c1c-remaining-*-results.jsonl'):
        reports = collections.defaultdict(dict)
        for line in file.read_text().splitlines():
            row = json.loads(line)
            reports[row['nodeid']][row['when']] = row['outcome']
        done.update(node for node, phases in reports.items()
                    if phases.get('teardown') == 'passed'
                    and (phases.get('call') in ('passed', 'skipped') or phases.get('setup') == 'skipped')
                    and 'failed' not in phases.values())
    return done


def main(numbers):
    batches = json.loads((DATA / 'c1c-batches.json').read_text())
    for number in numbers:
        nodes = batches[number - 1]
        attempt = 0
        while remaining := sorted(set(nodes) - completed()):
            if datetime.now(timezone.utc) >= CUTOFF:
                print('No more holds: four-hour cutoff.', flush=True)
                return 2
            attempt += 1
            label = f'c1c-remaining-{number:02}' + (f'-resume-{attempt}' if attempt > 1 else '')
            ledger = DATA / 'c1c-holds.jsonl'
            ended = {json.loads(line)['label'] for line in ledger.read_text().splitlines()} if ledger.exists() else set()
            while label in ended:
                attempt += 1
                label = f'c1c-remaining-{number:02}-resume-{attempt}'
            path = DATA / f'{label}-nodes.txt'
            path.write_text(''.join(n + '\n' for n in remaining))
            result = subprocess.run([sys.executable, str(DATA / 'c1c-hold.py'), label, '--nodes', str(path)])
            now_remaining = set(nodes) - completed()
            if result.returncode not in (0, 2):
                print(f'{label}: stopped for review, rc={result.returncode}, unfinished={len(now_remaining)}', flush=True)
                return result.returncode
            if len(now_remaining) >= len(remaining):
                print(f'{label}: no completed original node in this slice; leaving {len(now_remaining)} unfinished and continuing other slices', flush=True)
                break
    return 0


if __name__ == '__main__':
    raise SystemExit(main([int(n) for n in sys.argv[1:]]))
