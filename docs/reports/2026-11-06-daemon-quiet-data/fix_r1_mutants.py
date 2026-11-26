"""Run r1's mutants individually in scratch and byte-verify each restoration."""

from __future__ import annotations

import argparse
import hashlib
from importlib.util import cache_from_source
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def replace_once(source, old, new):
    assert source.count(old) == 1, (old, source.count(old))
    return source.replace(old, new, 1)


def mutants(source):
    assert source.count("writes[a['attempt_id']] = observed_at") == 2
    yield (
        'commit_completion_pace',
        source.replace("writes[a['attempt_id']] = observed_at",
                       "writes[a['attempt_id']] = time.monotonic()"),
        'test_liveness_pace_starts_at_observation_before_a_slow_successful_commit',
    )
    floorless = replace_once(
        source,
        "                if observed is not None and (not current.get('last_observed_alive_at')\n"
        "                                            or _earlier(current['last_observed_alive_at'], observed)):\n",
        "                if observed is not None:\n",
    )
    floorless = replace_once(
        floorless,
        "                if (not current.get('last_observed_alive_at')\n"
        "                        or _earlier(current['last_observed_alive_at'], observed)):\n"
        "                    current['last_observed_alive_at'] = observed\n",
        "                current['last_observed_alive_at'] = observed\n",
    )
    yield ('drop_timestamp_floor', floorless,
           'test_liveness_wall_clock_rollback_never_moves_saved_observation_backwards')
    early = replace_once(
        source,
        '            if not found and not dropping and not age_changed:\n'
        '                continue\n'
        '            with self.store.transaction(kind, job_id=a["job_id"], attempt_id=a["attempt_id"],\n',
        '            if not found and not dropping and not age_changed:\n'
        '                continue\n'
        "            if observed is not None:\n"
        "                writes[a['attempt_id']] = observed_at\n"
        '            with self.store.transaction(kind, job_id=a["job_id"], attempt_id=a["attempt_id"],\n',
    )
    early = replace_once(
        early,
        '            a["evidence_json"] = encoded\n'
        "            if observed is not None:\n"
        "                writes[a['attempt_id']] = observed_at\n"
        '            wrote = True\n',
        '            a["evidence_json"] = encoded\n'
        '            wrote = True\n',
    )
    yield ('ownership_pace_before_commit', early,
           'test_failed_identity_commit_does_not_consume_pace')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    scratch = args.scratch.resolve()
    assert 'sf-dq-f1' in str(scratch), scratch
    repository = Path(__file__).resolve().parents[3]
    original = repository / 'subfleet/daemon.py'
    pristine = original.read_bytes()
    location = scratch / 'mutants'
    location.mkdir()
    shutil.copytree(repository / 'subfleet', location / 'subfleet',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (location / 'tests').symlink_to(repository / 'tests', target_is_directory=True)
    shutil.copyfile(repository / 'pyproject.toml', location / 'pyproject.toml')
    target = location / 'subfleet/daemon.py'
    assert target.read_bytes() == pristine
    results = []
    try:
        for name, mutated, test in mutants(pristine.decode()):
            assert target.read_bytes() == pristine
            selector = 'tests/unit/test_liveness_write_pacing.py::' + test
            pytest_args = ['-q', selector, '--tb=short', '--basetemp', str(scratch / name)]
            launcher = (
                'import pathlib, subfleet.daemon as d; '
                f'assert pathlib.Path(d.__file__).is_relative_to({str(location)!r}), d.__file__; '
                'import pytest; '
                f'raise SystemExit(pytest.main({pytest_args!r}))')
            command = [sys.executable, '-c', launcher]
            try:
                target.write_text(mutated)
                Path(cache_from_source(str(target))).unlink(missing_ok=True)
                # Remove cached bytecode even on interpreters using coarse mtimes.
                cache = location / 'subfleet/__pycache__'
                if cache.exists():
                    assert 'sf-dq-f1' in str(cache)
                    shutil.rmtree(cache)
                run = subprocess.run(command, cwd=location, env=os.environ.copy(),
                                     text=True, capture_output=True, timeout=300)
                log = run.stdout + run.stderr
                (scratch / f'{name}.log').write_text(log)
                result = dict(mutant=name, killer=test, command=command, cwd=str(location),
                              returncode=run.returncode,
                              killed=run.returncode == 1 and ('FAILED ' + selector) in log,
                              log=log)
            finally:
                target.write_bytes(pristine)
                assert target.read_bytes() == pristine
                assert original.read_bytes() == pristine
            result['restored_sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
            results.append(result)
            args.output.write_text(json.dumps(results, indent=2) + '\n')
            print(json.dumps({k: v for k, v in result.items() if k not in ('log', 'command')}), flush=True)
        assert all(result['killed'] for result in results), 'A mutant survived or failed outside its test.'
    finally:
        assert target.read_bytes() == pristine
        assert 'sf-dq-f1' in str(location)
        shutil.rmtree(location)


if __name__ == '__main__':
    main()
