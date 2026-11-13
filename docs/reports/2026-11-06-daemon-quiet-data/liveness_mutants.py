"""Run the five D-DAEMON-QUIET1 liveness mutants in isolated production copies.

Run from the repository through its synced development environment, for example:
    uv run --no-sync python docs/reports/2026-11-06-daemon-quiet-data/liveness_mutants.py
Only the temporary production copies are changed. Tests are shared read-only.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def replace_once(source, old, new):
    assert source.count(old) == 1, (old, source.count(old))
    return source.replace(old, new, 1)


def mutants(source):
    gate = ("and (a['attempt_id'] not in writes\n"
            "                     or observed_at - writes[a['attempt_id']] >= LIVENESS_WRITE_S)")
    yield ('no_pace', replace_once(source, gate, 'and True'),
           'test_liveness_writes_at_the_minute_boundary_and_keeps_writing')
    yield ('never_after_first', replace_once(source, gate, "and a['attempt_id'] not in writes"),
           'test_liveness_age_stays_within_one_minute_plus_inspection_interval')
    yield ('failed_write_counted', replace_once(
        source, "            with self.store.transaction('attempt.observed_alive'",
        "            writes[a['attempt_id']] = time.monotonic()\n"
        "            with self.store.transaction('attempt.observed_alive'"),
        'test_failed_liveness_commit_retries_the_same_table_and_observation')
    yield ('repeated_table_moves_saved', replace_once(
        source, 'observed = utcnow_ms() if previous_table is not table else None',
        'observed = utcnow_ms()'),
        'test_repeated_table_cannot_advance_saved_liveness_even_after_minute')
    finished = replace_once(source, "'_counted_tables', '_last_observed_alive_write',",
                            "'_counted_tables',")
    finished, removed = re.subn(
        r"^\s*self\.__dict__\.get\('_last_observed_alive_write', \{\}\)\.pop\([^\n]*\n",
        '', finished, flags=re.MULTILINE)
    assert removed == 5, removed  # Terminal worker and all four ending paths.
    yield ('never_drop_finished', finished, 'test_ended_attempts_leave_no_liveness_pacing_entry')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path,
                        default=Path(__file__).resolve().parent / 'liveness-mutants')
    parser.add_argument('--temp-root', type=Path,
                        default=Path(os.environ.get('TMPDIR', '/private/tmp')))
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[3]
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = (repository / 'subfleet/daemon.py').read_text()
    results = []
    for name, mutated, test in mutants(source):
        with tempfile.TemporaryDirectory(prefix=f'daemon-quiet-{name}-', dir=args.temp_root) as folder:
            location = Path(folder).resolve()
            shutil.copytree(repository / 'subfleet', location / 'subfleet',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            (location / 'tests').symlink_to(repository / 'tests', target_is_directory=True)
            shutil.copyfile(repository / 'pyproject.toml', location / 'pyproject.toml')
            (location / 'subfleet/daemon.py').write_text(mutated)
            selector = 'tests/unit/test_liveness_write_pacing.py::' + test
            pytest_args = ['-q', selector, '--tb=short', '--basetemp', str(location / 'run')]
            launcher = (
                'import pathlib, subfleet.daemon as d; '
                f'assert pathlib.Path(d.__file__).is_relative_to({str(location)!r}), d.__file__; '
                'import pytest; '
                f'raise SystemExit(pytest.main({pytest_args!r}))')
            command = [sys.executable, '-c', launcher]
            run = subprocess.run(command, cwd=location, env=os.environ.copy(),
                                 text=True, capture_output=True, timeout=180)
            log = run.stdout + run.stderr
            (output / f'{name}.log').write_text(log)
            count = re.search(r'(\d+) failed(?:,| in)', log)
            result = dict(mutant=name, test=test, returncode=run.returncode,
                          killed=run.returncode == 1 and ('FAILED ' + selector) in log,
                          failed_cases=int(count.group(1)) if count else None,
                          log=f'liveness-mutants/{name}.log')
            results.append(result)
            (output / 'verified-results.json').write_text(json.dumps(results, indent=2) + '\n')
            print(json.dumps(result), flush=True)
    if not all(result['killed'] for result in results):
        raise SystemExit('A mutant survived or failed outside its target test.')


if __name__ == '__main__':
    main()
