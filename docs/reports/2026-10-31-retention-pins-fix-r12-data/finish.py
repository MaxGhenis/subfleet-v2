"""Validate completed receipts, clean owned scratch, and render the final report."""
import contextlib
import errno
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
from tables import build

DATA = Path(__file__).resolve().parent
ROOT = DATA.parents[2]
with contextlib.redirect_stdout(io.StringIO()):
    summary = build()
assert not summary['planned_holds_not_completed'], summary['planned_holds_not_completed']
assert not summary['missing_or_unfinished'], summary['missing_or_unfinished']
assert not summary['active'], summary['active']
assert all(row['later_passed'] for row in summary['ordinary_failures'])
assert len(summary['ordinary_failures']) == 4
assert len(summary['mutants']) == 19
assert all(row['result'] == 'killed' for row in summary['mutants'])
assert sum(row['failed_assertions'] for row in summary['mutants']) == 20
assert summary['locked'] and summary['longest_seconds'] < 600
assert summary['peak_rss_mib'] < 8192
assert len(summary['per_file']) == 24
assert summary['differential_comparisons'] == 496
comparison = json.loads((DATA / 'differential-summary.json').read_text())
assert comparison['evidence_matches']
assert comparison['dropped'] == {str(errno.ELOOP): 28}
validation = dict(required_holds_complete=True, missing_collected_nodes=[],
    final_ordinary_failures=[], initial_expectation_failures=4,
    initial_failures_all_corrected=True, locked=True,
    actual_pytest_invocations=summary['completed'],
    distinct_passed=summary['unique_passed'], files=len(summary['per_file']),
    differential_comparisons=496, omitted_errno='ELOOP only',
    mutants_killed=19, named_assertions=20,
    longest_seconds=summary['longest_seconds'], peak_rss_mib=summary['peak_rss_mib'])
(DATA / 'validation.json').write_text(json.dumps(validation, indent=2) + '\n')
subprocess.run([sys.executable, str(DATA / 'cleanup.py')], check=True)
subprocess.run([sys.executable, str(DATA / 'audit_product.py'), str(ROOT / 'sf-r12-git')], check=True)
with contextlib.redirect_stdout(io.StringIO()):
    runpy.run_path(str(DATA / 'write_pr_body.py'), run_name='__main__')
    runpy.run_path(str(DATA / 'write_report.py'), run_name='__main__')
print(json.dumps(validation, indent=2))
