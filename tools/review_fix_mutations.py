"""Foreground, in-memory assertion mutations for PR #120 round-two fixes.

`--baseline GROUP` runs the new regressions with the actual 7bab0816 modules.
`--only NAME ...` kills targeted mutants without touching production files.
Exit 1 alone is insufficient: a kill must include a failed test assertion.
"""
from __future__ import annotations

import argparse
import importlib
import inspect
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RANK = 'tests/unit/test_weekly_rank_vs_base.py'
BUSY = 'tests/unit/test_timers_busy_read.py'
DOC = 'tests/unit/test_rank_review_report.py'
GROUPS = {
    'differential': [RANK, '-k', 'judged_as_the_base or newly_needs or excludes_no'],
    'ranking': [RANK, '-k', 'not judged_as_the_base and not newly_needs and not excludes_no'],
    'busy': [BUSY, '-k', 'busy_claude_reads_start or busy_pacing_debt or missing_sensor_respects'],
    'report': [DOC],
    'fake': ['tests/fake/test_admission_priority.py'],
}
# module, function (possibly Class.method), old text, replacement, pytest selection
MUTATIONS = {
    'renewal-couples-admission': ('scheduler', 'judge_lane', '"measured": bool(measured_readings)',
        '"measured": ranking["measured"]', [RANK, '-k', 'why_distinguishes']),
    'stopped-window-never-expires': ('scheduler', 'ranking_usage', 'for row in recent)',
        'for row in latest.values())', [RANK, '-k', 'stopped_windows']),
    'renewal-ttl-horizon-omitted': ('capacity', 'lane_horizons', 'note(row["lane_id"], expiry)',
        'None', [RANK, '-k', 'uncertainty_ends_at_its_ttl']),
    'stale-observation-horizon-omitted': ('capacity', 'lane_horizons', 'if observed > instant:',
        'if observed > instant and row.get("label") == "provider":',
        [RANK, '-k', 'stale_labeled_reading_recency']),
    'binding-tie-latest-reset': ('scheduler', 'ranking_usage',
        '_iso(_time(row["resets_at"])) if row.get("resets_at") else "9999"',
        '-_time(row["resets_at"]).timestamp() if row.get("resets_at") else float("inf")',
        [RANK, '-k', 'equal_weekly']),
    'why-hides-renewal': ('cli', '_format_ranking_usage',
        "'pending' if detail.get('reading_renewed') else 'none'", "'none'", [RANK, '-k', 'why_distinguishes']),
    'why-age-uses-stopped-window': ('scheduler', 'ranking_usage',
        '(fresh or recent or latest.values())', 'latest.values()', [RANK, '-k', 'stopped_windows']),
    'busy-reads-inside-idle-hold': ('timers', 'Timers._probe_lane',
        'return lane, None, BUSY', 'return self._busy_read(lane)', [BUSY, '-k', 'busy_claude_reads_start']),
    'busy-pacing-debt-under-idle-hold': ('timers', 'Timers.probe_cycle',
        'self.cancel.wait(wait)', 'None', [BUSY, '-k', 'busy_pacing_debt']),
    'failure-cooldown-bypassed': ('timers', 'Timers._usage_wait',
        'return bool(until and self.now() < instant(until))', 'return False', [BUSY, '-k', 'missing_sensor_respects']),
    'failure-cooldown-not-restored': ('timers', 'Timers.__init__',
        "self._usage_backoff = self._latest('timer.usage-backoff')", 'self._usage_backoff = {}',
        [BUSY, '-k', 'missing_sensor_respects']),
    'failure-cooldown-truncates-deadline': ('timers', 'Timers._probe_result',
        "until.isoformat().replace('+00:00', 'Z')", 'iso(until)',
        [BUSY, '-k', 'respects_fractional_backoff']),
    'failure-fakes-provider-freshness': ('timers', 'Timers._probe_result',
        "self.store.add_event('timer.usage-backoff', lane_id=lane.lane_id, data=data)",
        "with self.store.transaction('mutant.fake-freshness') as tx:\n            tx.execute(\"UPDATE readings SET observed_at=? WHERE lane_id=? AND label='provider'\", (iso(self.now()), lane.lane_id))\n        self.store.add_event('timer.usage-backoff', lane_id=lane.lane_id, data=data)",
        [BUSY, '-k', 'missing_sensor_respects']),
    'report-omits-calibration': ('report', '', '805/3,188', 'unreported', [DOC]),
    'report-overstates-gain': ('report', '', '0.0941 percentage points', 'large improvement', [DOC]),
    'report-claims-claude-freshness': ('report', '', 'no Claude freshness gain', 'Claude freshness gain', [DOC]),
    'report-overstates-five-hour': ('report', '', 'five-hour column covers Claude only', 'five-hour column covers both providers', [DOC]),
}


def replace_report(old: str, new: str):
    report = ROOT / 'docs/reports/2026-10-03-rank-earliest-reset.md'
    read = Path.read_text
    def patched(path, *args, **kwargs):
        text = read(path, *args, **kwargs)
        return text.replace(old, new) if path.resolve() == report else text
    Path.read_text = patched


def run_worker(name: str, baseline: bool = False) -> int:
    import pytest
    sys.path.insert(0, str(ROOT))
    if baseline:
        if name == 'report':
            report = ROOT / 'docs/reports/2026-10-03-rank-earliest-reset.md'
            original = subprocess.check_output(['git', 'show', '7bab0816:' + str(report.relative_to(ROOT))], cwd=ROOT, text=True)
            read = Path.read_text
            Path.read_text = lambda path, *a, **kw: original if path.resolve() == report else read(path, *a, **kw)
        else:
            for part in ('capacity', 'scheduler', 'picker', 'cli', 'timers'):
                module = importlib.import_module('subfleet.' + part)
                source = subprocess.check_output(['git', 'show', f'7bab0816:subfleet/{part}.py'], cwd=ROOT, text=True)
                exec(compile(source, f'<7bab0816:{part}>', 'exec'), module.__dict__)
        selection = GROUPS[name]
    else:
        part, function, old, new, selection = MUTATIONS[name]
        if part == 'report':
            replace_report(old, new)
        else:
            module = importlib.import_module('subfleet.' + part)
            owner, attr = (getattr(module, function.split('.')[0]), function.split('.')[1]) if '.' in function else (module, function)
            import textwrap
            source = textwrap.dedent(inspect.getsource(getattr(owner, attr)))
            if source.count(old) != 1:
                print(f'Mutation no longer applies: {name}')
                return 2
            scope = dict(vars(module))
            exec(compile(source.replace(old, new), f'<review-mutant:{name}>', 'exec'), scope)
            setattr(owner, attr, scope[attr])
    return int(pytest.main(['-q', '-p', 'no:cacheprovider', *selection]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only', nargs='+', choices=list(MUTATIONS), default=list(MUTATIONS))
    parser.add_argument('--worker')
    parser.add_argument('--baseline', choices=list(GROUPS))
    args = parser.parse_args()
    if args.baseline:
        return run_worker(args.baseline, baseline=True)
    if args.worker:
        return run_worker(args.worker)
    failures = []
    for name in args.only:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', name],
                                cwd=ROOT, capture_output=True, text=True)
        assertions = [line for line in result.stdout.splitlines() if line.startswith('FAILED ')]
        killed = result.returncode == 1 and bool(assertions) and 'AssertionError' in result.stdout
        print(f'{"KILLED" if killed else "SURVIVED/ERROR"} {name}: {len(assertions)} failing assertions', flush=True)
        if not killed:
            failures.append(name)
            print(result.stdout[-6000:] + result.stderr[-2000:])
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
