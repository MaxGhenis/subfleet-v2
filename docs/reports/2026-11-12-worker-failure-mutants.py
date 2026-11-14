#!/usr/bin/env python3
"""Run C-5.10 mutations separately, restoring exact daemon.py bytes each time.

Run only after the ordinary tests finish and while daemon.py has no other writer.
The runner refuses to start if its expected pristine snippets are absent.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

WORKTREE = Path(__file__).resolve().parents[2]
SCRATCH = Path('/private/tmp/sf-wcp-b1')
SOURCE = WORKTREE / 'subfleet/daemon.py'
CAUSE_TESTS = 'tests/unit/test_worker_failure_cause.py'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def replace_once(source: str, old: str, new: str) -> str:
    count = source.count(old)
    if count != 1:
        raise AssertionError(f'Expected one mutation anchor, found {count}: {old!r}')
    return source.replace(old, new, 1)


ONE_SHOT = ('                    self.log.error("worker %s failed: %s", key, '
            'type(exc).__name__, exc_info=True)')
MUTANTS = [
    {
        'number': 1,
        'name': 'cause-on-every-paced-failure',
        'old': '                cause = count % WORKER_CAUSE_EVERY == 1          # 1, 33, 65, ...',
        'new': '                cause = True  # MUTANT: every paced failure gets its cause',
        'test': f'{CAUSE_TESTS}::test_paced_cause_and_exact_summary_cadence',
    },
    {
        'number': 2,
        'name': 'cause-rendering-raises-out-of-done',
        'old': ONE_SHOT,
        'new': '                    str(exc)  # MUTANT: render before the protected handler\n' + ONE_SHOT,
        'test': f'{CAUSE_TESTS}::test_hostile_exception_cause_never_escapes_or_skips_handler',
    },
    {
        'number': 3,
        'name': 'retention-structured-error-contains-message',
        'old': '                    self.timers.mark("retention", error=type(exc).__name__, next_due=after(delay))',
        'new': '                    self.timers.mark("retention", error=str(exc), next_due=after(delay))',
        'test': f'{CAUSE_TESTS}::test_retention_error_is_type_only_and_deferred_keeps_count',
    },
    {
        'number': 4,
        'name': 'control-signatures-never-clear-on-success',
        'old': '                self._control_traced.clear()',
        'new': '                pass  # MUTANT: retain control causes after completed iterations',
        'test': f'{CAUSE_TESTS}::test_control_cause_deduplication_resets_after_completed_iteration',
    },
    {
        'number': 5,
        'name': 'remembered-set-grows-past-256',
        'old': '        if len(traced) >= TRACED_CAUSES_MAX:\n            traced.clear()',
        'new': '        if False:  # MUTANT: the remembered set never restarts\n            traced.clear()',
        'test': f'{CAUSE_TESTS}::test_first_trace_holds_at_most_256_and_starts_again_when_full',
    },
    {
        'number': 6,
        'name': 'summary-text-changes',
        'old': '                    self.log.error("worker %s failed: %s (%d in a row, next try in %g s)",',
        'new': '                    self.log.error("worker %s failed: %s (%d consecutive, retry after %g s)",',
        'test': f'{CAUSE_TESTS}::test_paced_cause_and_exact_summary_cadence',
    },
    {
        'number': 7,
        'name': 'cause-bypasses-scrubbing-formatter',
        'old': ONE_SHOT,
        'new': '                    print(exc, file=sys.stderr)  # MUTANT: raw cause bypass\n' + ONE_SHOT,
        'test': f'{CAUSE_TESTS}::test_exception_message_notes_chain_and_source_are_scrubbed',
    },
]


def main() -> int:
    assert SCRATCH.name == 'sf-wcp-b1'
    assert SOURCE.resolve().is_relative_to(WORKTREE.resolve())
    SCRATCH.mkdir(exist_ok=True)
    pristine = SOURCE.read_bytes()
    pristine_hash = sha(pristine)
    pristine_text = pristine.decode('utf-8')
    # Build every mutation before touching the source, so an outdated anchor stops safely.
    variants = [(spec, replace_once(pristine_text, spec['old'], spec['new']).encode('utf-8'))
                for spec in MUTANTS]
    snapshot = SCRATCH / 'daemon-pristine-mutants.py'
    snapshot.write_bytes(pristine)
    assert snapshot.read_bytes() == pristine
    run_tag = str(time.time_ns())
    session = SCRATCH / f'mutants-{run_tag}'
    session.mkdir()
    env = os.environ.copy()
    darwin_tmp = subprocess.check_output(['getconf', 'DARWIN_USER_TEMP_DIR'], text=True).strip()
    env['TMPDIR'] = darwin_tmp
    env['UV_CACHE_DIR'] = str(SCRATCH / 'uv-cache')
    results = []
    try:
        for spec, variant in variants:
            assert SOURCE.read_bytes() == pristine, 'Source changed between mutations'
            test_temp = session / f"pytest-{spec['number']}"
            command = [
                'uv', '--cache-dir', env['UV_CACHE_DIR'], 'run', '--no-sync',
                '--with', 'pytest-xdist>=3.6', 'pytest', spec['test'], '-n', '4',
                '--basetemp', str(test_temp), '-q',
            ]
            started = time.monotonic()
            stdout = ''
            returncode = None
            error = None
            SOURCE.write_bytes(variant)
            try:
                assert SOURCE.read_bytes() == variant
                completed = subprocess.run(
                    command, cwd=WORKTREE, env=env, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300,
                )
                stdout, returncode = completed.stdout, completed.returncode
            except BaseException as exc:
                error = f'{type(exc).__name__}: {exc}'
                if isinstance(exc, subprocess.TimeoutExpired):
                    stdout = exc.stdout or ''
                    if isinstance(stdout, bytes):
                        stdout = stdout.decode('utf-8', 'replace')
                raise
            finally:
                SOURCE.write_bytes(pristine)
                restored = SOURCE.read_bytes()
                assert restored == pristine and sha(restored) == pristine_hash
                output_path = session / f"{spec['number']}-{spec['name']}.txt"
                output_path.write_text(stdout, encoding='utf-8')
                result = {
                    'number': spec['number'], 'name': spec['name'], 'test': spec['test'],
                    'command': command, 'returncode': returncode,
                    'killed': returncode == 1, 'seconds': time.monotonic() - started,
                    'output': str(output_path), 'error': error,
                    'restored_sha256': sha(restored),
                }
                results.append(result)
                (session / 'results.json').write_text(json.dumps({
                    'pristine_sha256': pristine_hash, 'snapshot': str(snapshot),
                    'TMPDIR': env['TMPDIR'], 'UV_CACHE_DIR': env['UV_CACHE_DIR'],
                    'results': results,
                }, indent=2) + '\n', encoding='utf-8')
                print(json.dumps(result), flush=True)
            # Exit codes 2/3/4/5 indicate infrastructure/collection failures, not kills.
            if returncode != 1:
                print(f"Mutation {spec['number']} was not killed by an assertion failure", file=sys.stderr)
                return 1
    finally:
        SOURCE.write_bytes(pristine)
        assert SOURCE.read_bytes() == pristine
        assert sha(SOURCE.read_bytes()) == pristine_hash
    print(f'All seven mutants killed. Source restored: {pristine_hash}. Results: {session}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
