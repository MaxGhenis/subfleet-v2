"""Run the eleven reviewed extra importers after the batch driver has stopped.

Prepared only; creation/compilation does not execute tests. Manually invoke with
--after-batches-stopped only after the coordinator has ended the current batch
worker session. The driver never inspects other processes to establish that.
"""
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
WHITELIST = {
    'tests/frontend/test_core_client.py',
    'tests/frontend/test_core_live_steer.py',
    'tests/frontend/test_core_outbox.py',
    'tests/frontend/test_core_protocol.py',
    'tests/frontend/test_core_steer.py',
    'tests/frontend/test_core_suggestions.py',
    'tests/frontend/test_disk_ruling.py',
    'tests/unit/test_gate_admission.py',
    'tests/unit/test_host_push_cli.py',
    'tests/unit/test_host_push_quarantine_restart.py',
    'tests/fake/test_review_launch_permissions.py',
}


def install_frontend_cache_wrapper() -> None:
    plugin = ROOT / 'sf_fix1_safety.py'
    source = plugin.read_text()
    helper = (ROOT / 'frontend-conditional-plugin.py.txt').read_text()
    hook_line = '        os.environ.pop(name, None)\n'
    call_line = '    _install_frontend_scratch_cache(config)\n'
    if call_line not in source:
        if source.count(hook_line) != 1:
            raise RuntimeError('Safety plugin configure hook changed; review before installing wrapper')
        source = source.replace(hook_line, hook_line + call_line, 1)
    if 'def _install_frontend_scratch_cache(config):' not in source:
        source += '\n\n' + helper
    compile(source, str(plugin), 'exec')
    plugin.write_text(source)


def stop_own_child(child: subprocess.Popen) -> None:
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


def main() -> int:
    if sys.argv[1:] != ['--after-batches-stopped']:
        raise SystemExit('Manually run with --after-batches-stopped after the current batch driver and workers have ended')
    if time.time() >= DEADLINE:
        raise SystemExit('Importer deadline reached; write the report instead')
    paths = (ROOT / 'importer-extras.txt').read_text().splitlines()
    if len(paths) != len(WHITELIST) or set(paths) != WHITELIST:
        raise RuntimeError('Extra importer list differs from the reviewed exact eleven-file whitelist')
    if not all(Path(path).is_file() for path in paths):
        raise RuntimeError('A selected test file is missing from the assigned workspace')
    basetemp = BASE / 'pytest-importer-extras-01'
    if basetemp.exists():
        raise RuntimeError('Fresh extras basetemp already exists; review and choose a new distinct name')
    BASE.mkdir(parents=True, exist_ok=True)
    install_frontend_cache_wrapper()
    (ROOT / 'active-selection.txt').write_text('\n'.join(paths) + '\n')
    args = [
        'uv', 'run', '--no-sync', '--with', 'pytest-xdist>=3.6',
        'python', str(ROOT / 'run4.py'), '-q', '-n', '4', '--dist', 'worksteal',
        '--timeout=900', '-m', 'not real_lsof and not real_cwd',
        'tests/unit', 'tests/fake', 'tests/frontend', f'--basetemp={basetemp}',
    ]
    env = dict(os.environ)
    env['TMPDIR'] = subprocess.check_output(['getconf', 'DARWIN_USER_TEMP_DIR'], text=True).strip()
    env['UV_CACHE_DIR'] = str(ROOT / 'uv-cache')
    began = time.monotonic()
    print('EXTRAS START', len(paths), flush=True)
    child = None
    try:
        with (ROOT / 'importer-extras-01.log').open('w') as log:
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
        'stage': 'importer-extras-01', 'files': paths, 'args': args,
        'returncode': child.returncode,
        'seconds': round(time.monotonic() - began, 2),
    }
    with (ROOT / 'batch-results.jsonl').open('a') as result:
        result.write(json.dumps(record) + '\n')
    print('EXTRAS DONE', child.returncode, record['seconds'], flush=True)
    return child.returncode


if __name__ == '__main__':
    raise SystemExit(main())
