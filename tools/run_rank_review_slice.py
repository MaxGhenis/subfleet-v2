"""Run a foreground pytest slice and retain exact counts/timing for PR #120.

Example: python tools/run_rank_review_slice.py scheduler tests/unit/test_scheduler.py
For long 1,500-example laws, use three --examples 500 --seed N slices. The
collection plugin changes only the per-slice example budget, never assertions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'docs/reports/2026-10-04-rank-review-test-results.json'


class Capture:
    def __init__(self, examples):
        self.examples = examples
        self.results = {}

    def pytest_collection_modifyitems(self, items):
        if self.examples is not None:
            from hypothesis import settings
            for item in items:
                old = getattr(item.obj, '_hypothesis_internal_use_settings', None)
                if old is not None:
                    item.obj._hypothesis_internal_use_settings = settings(old, max_examples=self.examples)

    def pytest_runtest_logreport(self, report):
        if report.when == 'call' or (report.when == 'setup' and report.outcome != 'passed'):
            self.results[report.nodeid] = report.outcome
        elif report.when == 'teardown' and report.failed:
            self.results[report.nodeid] = 'failed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--examples', type=int)
    parser.add_argument('--seed', type=int)
    parser.add_argument('name')
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    import pytest
    plugin = Capture(args.examples)
    selection = ['-q', '-p', 'no:cacheprovider', *args.pytest_args]
    if args.seed is not None:
        selection += [f'--hypothesis-seed={args.seed}']
    start = time.monotonic()
    code = pytest.main(selection, plugins=[plugin])
    duration = time.monotonic() - start
    results = json.loads(RESULTS.read_text()) if RESULTS.exists() else []
    row = dict(slice=args.name, arguments=selection, examples=args.examples, seed=args.seed,
               seconds=round(duration, 3), exit_code=code,
               passed=sum(v == 'passed' for v in plugin.results.values()),
               failed=sum(v == 'failed' for v in plugin.results.values()),
               skipped=sum(v == 'skipped' for v in plugin.results.values()),
               tests=plugin.results)
    results.append(row)
    RESULTS.write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps({k: v for k, v in row.items() if k != 'tests'}), flush=True)
    return code or (1 if duration >= 600 else 0)


if __name__ == '__main__':
    raise SystemExit(main())
