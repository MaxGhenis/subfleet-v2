"""Prepared only: run the sixteen audited safe nodes after batch workers stop."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path('/private/tmp/sf-a2-fix1')
BASE = Path('/private/var/folders/9l/_wztzgbx7mgc7l1r0416cy7m0000gn/T/subfleet-attempt-20261115-180648-rv-a2-fix-r1b-a1-3hqfxg5e/sf-a2-fix1')
DEADLINE = datetime(2026, 11, 16, 2, 25, tzinfo=timezone.utc).timestamp()
SELECTORS = (
    'tests/unit/test_host_push_metadata.py::test_both_layouts_read_as_data',
    'tests/unit/test_host_push_metadata.py::test_each_metadata_path_refuses_promptly',
    'tests/unit/test_host_push_store_completion_pr183.py::test_generated_preparation_pass_completion_cancel_restart_interleavings',
    'tests/unit/test_host_push_store_interleavings.py::test_busy_store_completion_cancel_pass_restart_interleavings',
    'tests/unit/test_host_push_storage_retry_budget.py::test_completion_storage_fault_retries_atomic_ownership_once',
    'tests/unit/test_host_push_storage_retry_budget.py::test_completion_does_not_retry_unknown_or_permanent_storage_codes',
    'tests/unit/test_host_push_storage_retry_budget.py::test_cancellation_clears_pending_storage_budget',
    'tests/unit/test_host_push_storage_retry_budget.py::test_completion_retries_last_the_window_not_just_the_count',
    'tests/unit/test_host_push_storage_retry_budget.py::test_completion_fails_once_window_and_count_are_both_spent',
    'tests/unit/test_host_push_storage_retry_budget.py::test_a_completion_write_failure_s_notice_names_its_class_only',
    'tests/unit/test_host_push_build_budget.py::test_production_gate_never_queues_two_unconsumed_builds',
    'tests/unit/test_host_push_build_budget.py::test_mutation_removing_global_preparation_gate_is_caught',
    'tests/unit/test_host_push_build_budget.py::test_fast_completions_still_offer_only_one_build_per_pass',
    'tests/unit/test_host_push_build_budget.py::test_mutation_removing_per_pass_offer_limit_is_caught',
    'tests/unit/test_host_push_build_budget.py::test_mutation_forgetting_prior_pass_completion_spends_offer_is_caught',
    'tests/unit/test_host_push_build_budget.py::test_midpass_failed_future_is_retained_until_startup_classifies_it',
)


def stop_own_child(child):
    if child.poll() is not None:
        return
    child.send_signal(signal.SIGINT)
    try:
        child.wait(timeout=30)
    except subprocess.TimeoutExpired:
        child.terminate()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=15)


def main():
    if sys.argv[1:] != ['--after-batches-stopped']:
        raise SystemExit('Manually run with --after-batches-stopped after current batch workers have ended')
    if time.time() >= DEADLINE:
        raise SystemExit('Importer deadline reached; write the report instead')
    listed = (ROOT / 'importers-safe-mixed-nodes.txt').read_text().splitlines()
    if tuple(listed) != SELECTORS or len(set(listed)) != 16:
        raise RuntimeError('Mixed selection differs from the exact reviewed sixteen-node list')
    modules = sorted({node.split('::', 1)[0] for node in SELECTORS})
    if not all(module.startswith('tests/unit/') and Path(module).is_file() for module in modules):
        raise RuntimeError('Mixed selection must name existing unit modules only')
    basetemp = BASE / 'pytest-importer-mixed-01'
    if basetemp.exists():
        raise RuntimeError('Fresh mixed basetemp already exists; review and choose a new distinct name')
    BASE.mkdir(parents=True, exist_ok=True)
    (ROOT / 'active-selection.txt').write_text('\n'.join(modules) + '\n')
    args = [
        'uv', 'run', '--no-sync', '--with', 'pytest-xdist>=3.6',
        'python', str(ROOT / 'run4.py'), '-p', 'sf_fix1_mixed',
        '-q', '-n', '4', '--dist', 'worksteal', '--timeout=900',
        '-m', 'not real_lsof and not real_cwd', *SELECTORS,
        f'--basetemp={basetemp}',
    ]
    env = dict(os.environ)
    env['TMPDIR'] = subprocess.check_output(['getconf', 'DARWIN_USER_TEMP_DIR'], text=True).strip()
    env['UV_CACHE_DIR'] = str(ROOT / 'uv-cache')
    began = time.monotonic()
    print('MIXED START', len(SELECTORS), len(modules), flush=True)
    child = None
    try:
        with (ROOT / 'importer-mixed-01.log').open('w') as log:
            child = subprocess.Popen(args, env=env, stdout=log, stderr=subprocess.STDOUT)
            while child.poll() is None:
                try:
                    child.wait(timeout=min(15, max(1, DEADLINE - time.time())))
                except subprocess.TimeoutExpired:
                    if time.time() >= DEADLINE:
                        stop_own_child(child)
    except BaseException:
        if child is not None:
            stop_own_child(child)
        raise
    record = {
        'stage': 'importer-mixed-01', 'files': modules, 'selectors': list(SELECTORS),
        'args': args, 'returncode': child.returncode,
        'seconds': round(time.monotonic() - began, 2),
    }
    with (ROOT / 'batch-results.jsonl').open('a') as result:
        result.write(json.dumps(record) + '\n')
    print('MIXED DONE', child.returncode, record['seconds'], flush=True)
    return child.returncode


if __name__ == '__main__':
    raise SystemExit(main())
