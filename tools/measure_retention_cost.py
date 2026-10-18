#!/usr/bin/env python3
"""Measure #76's retention batches on a disposable, live-shaped store (C-8.4).

Port of #109's cost probe. Drives the real daemon retention worker synchronously,
with real archives and registered git worktrees. No server, provider, timer or
background thread runs. Only the synthetic store is read or written. Holder
checks return empty because no other process uses these synthetic trees.

Idle clock time is fast-forwarded to the production scheduler's next eligible
instant; actual pass wall time and CPU are measured, and skipped waits reported
separately. Seed time is excluded. Bytes scale down; directory/worktree counts do
not. With --max-jobs equal to --jobs, sizes must advance before byte pruning.

Usage: .venv/bin/python tools/measure_retention_cost.py --jobs 3800 --worktrees 890
       --max-passes 3 --pass-s 30 --worktree-bytes 280000
"""

from __future__ import annotations

import argparse
from concurrent.futures import Future
from contextlib import contextmanager
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from subfleet import daemon as daemon_module  # noqa: E402
from subfleet.adapters.registry import register  # noqa: E402
from subfleet.daemon import Daemon  # noqa: E402
from tests.fake.conftest import Harness  # noqa: E402
from tests.fake_adapter import FakeAdapter  # noqa: E402


def cpu(children: bool = False) -> float:
    used = resource.getrusage(resource.RUSAGE_CHILDREN if children else resource.RUSAGE_SELF)
    return used.ru_utime + used.ru_stime


@contextmanager
def _fixture(keep: bool):
    directory = tempfile.mkdtemp(prefix='pr109-retention-', dir='/tmp')
    try:
        yield Path(directory)
    finally:
        if keep:
            print(f'Synthetic fixture retained at {directory}', flush=True)
        else:
            shutil.rmtree(directory)


class _Inline:
    def submit(self, fn, *args):
        future = Future()
        try:
            future.set_result(fn(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, **kwargs):
        pass


class _Clock:
    def __init__(self):
        self.skipped = 0.0

    def monotonic(self):
        return time.monotonic() + self.skipped

    def __getattr__(self, name):
        return getattr(time, name)


def _git(repository: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1', GIT_AUTHOR_NAME='Retention probe',
               GIT_AUTHOR_EMAIL='probe@example.invalid', GIT_COMMITTER_NAME='Retention probe',
               GIT_COMMITTER_EMAIL='probe@example.invalid')
    return subprocess.run(['git', '-C', str(repository), *args], check=True, capture_output=True,
                          text=True, env=env, timeout=60).stdout.strip()


def _seed(service: Daemon, base: Path, jobs: int, turn_jobs: int, worktrees: int,
          files: int, worktree_bytes: int) -> None:
    repository = base / 'source'
    repository.mkdir()
    _git(repository, 'init', '--quiet', '-b', 'synthetic')
    (repository / 'payload.bin').write_bytes(b'x' * worktree_bytes)
    _git(repository, 'add', '.')
    _git(repository, 'commit', '--quiet', '-m', 'Synthetic baseline')
    head = _git(repository, 'rev-parse', 'HEAD')
    # Copy a real Git-created detached registration for each fixture tree,
    # adjusting only its two backlinks. Avoid 890 redundant git/fsync startups
    # during seeding; every archive still reads real HEAD, index and reflog data.
    template = base / 'worktree-template'
    _git(repository, 'worktree', 'add', '--quiet', '--detach', str(template), head)
    template_admin = Path((template / '.git').read_text().removeprefix('gitdir: ').strip())
    # Spread the allocated trees through the entire age-ordered store.
    with service.store.transaction('synthetic.seed'):
        for index in range(jobs):
            identity = f'history-{index:05d}'
            tree = None
            if (index + 1) * worktrees // jobs != index * worktrees // jobs:
                tree = service.root / 'worktrees' / identity
                tree.parent.mkdir(exist_ok=True)
                admin = template_admin.parent / identity
                shutil.copytree(template, tree)
                shutil.copytree(template_admin, admin)
                (tree / '.git').write_text(f'gitdir: {admin}\n')
                (admin / 'gitdir').write_text(f'{tree}/.git\n')
            service.store.add_job(job_id=identity, request_id=identity, payload_digest='synthetic',
                                  kind='turn' if index < turn_jobs else 'dispatch', state='succeeded',
                                  workdir=str(repository), workdir_head=head, worktree=str(tree) if tree else None,
                                  prompt_path='/synthetic/prompt', sandbox='workspace-write' if tree else 'read-only',
                                  created_at=f'2026-01-01T00:00:00.{index:06d}Z', finished_at='2026-01-02T00:00:00Z')
            directory = service.root / 'jobs' / identity / 'a1'
            directory.mkdir(parents=True)
            for number in range(files):
                (directory / f'artifact-{number}').write_bytes(b'output\n' * 16)
    # Remove only this known seed template, avoiding Git's whole-registration
    # scan during fixture setup. Count all registrations and check sampled
    # backlinks/HEADs through Git; retirement validates every selected tree.
    shutil.rmtree(template)
    shutil.rmtree(template_admin)
    registered = sorted(template_admin.parent.iterdir())
    assert len(registered) == worktrees
    for admin in registered[::max(1, worktrees // 3)]:
        tree = Path((admin / 'gitdir').read_text().strip()).parent
        assert _git(tree, 'rev-parse', 'HEAD') == head
    print(f'Seeded {jobs} job directories ({turn_jobs} turns), {worktrees} registered worktrees; '
          f'{worktree_bytes} payload bytes per tree.', flush=True)


def measure(jobs: int = 3800, worktrees: int = 890, files: int = 4, pass_s: float = 30,
            max_passes: int = 3, max_jobs: int = 500, max_bytes: int = 2 * 1024**3,
            turn_jobs: int = 0, measure_s: float = 30, worktree_bytes: int = 280000,
            keep_store: bool = False) -> dict:
    with _fixture(keep_store) as base:
        root = base / 'state'
        root.mkdir()
        Harness(root)
        register('codex', FakeAdapter)
        # A synchronous probe has no daemon process to recover. Give its own
        # lock a synthetic identity; restricted sandboxes cannot inspect boot.
        with patch.object(daemon_module.procs, 'boot_id', lambda: 'synthetic-retention-boot'), \
                patch.object(daemon_module.procs, 'proc_start', lambda pid: 'synthetic-retention-process'):
            service = Daemon(root, desktop_prober=lambda: None)
        service.workers.shutdown(wait=True)
        service.workers = _Inline()
        real_maintenance, real_time, real_transaction = daemon_module.maintenance, daemon_module.time, service.store.transaction
        clock = _Clock()
        rows, ever_sized = [], set()
        first_prune = None
        first_prune_wait = 0.0
        retired = 0
        try:
            _seed(service, base, jobs, turn_jobs, worktrees, files, worktree_bytes=worktree_bytes)
            service.policy = {**service.policy, 'retention': {**service.policy['retention'],
                              'jobs': max_jobs, 'bytes': max_bytes, 'turn_jobs': max_jobs,
                              'turn_bytes': max_bytes, 'turn_keep_days': 0}}
            began_measurement = time.monotonic()
            daemon_module.time = clock

            @contextmanager
            def transaction(kind='state.changed', **options):
                nonlocal first_prune, first_prune_wait
                with real_transaction(kind, **options) as conn:
                    yield conn
                if kind == 'retention.pruned' and first_prune is None:
                    first_prune = time.monotonic() - began_measurement
                    first_prune_wait = clock.skipped

            service.store.transaction = transaction

            def bounded(*args, **kwargs):
                started, own, children = time.monotonic(), cpu(), cpu(True)
                result = real_maintenance(*args, **{**kwargs, 'deadline': started + pass_s,
                                          'measure_s': measure_s, 'holders': lambda *a, **k: {}})
                ever_sized.update(service._retention_state.sizes)
                rows.append({'pass': len(rows) + 1, 'wall_s': time.monotonic() - started,
                             'cpu_s': cpu() - own, 'child_cpu_s': cpu(True) - children,
                             'new_prunes': len(result['pruned']), 'jobs_after': result['jobs_after'],
                             'ever_sized': len(ever_sized), 'progressed': result['progressed'],
                             'interrupted': result.get('interrupted'), 'more': result.get('more'),
                             'errors': result['errors']})
                return result

            daemon_module.maintenance = bounded
            service._last_maintenance = clock.monotonic() - daemon_module.RETENTION_INTERVAL_S
            for _ in range(max_passes):
                next_at = max(service._last_maintenance + daemon_module.RETENTION_INTERVAL_S,
                              service._worker_retry_at.get('retention', 0))
                wait = max(0, next_at - clock.monotonic())
                clock.skipped += wait
                started, own, children = time.monotonic(), cpu(), cpu(True)
                service._schedule('retention', service._retention, paced=True)
                row = rows[-1]
                row.update(wall_s=time.monotonic() - started, cpu_s=cpu() - own,
                           child_cpu_s=cpu(True) - children)
                retired += row['new_prunes']
                row.update(retired=retired, skipped_wait_s=wait)
                assert row['jobs_after'] == jobs - retired, row
                assert not row['errors'], row
                if len(rows) > 1:
                    assert rows[-2]['ever_sized'] <= row['ever_sized']
                    assert rows[-2]['retired'] <= retired
                print(json.dumps(row, sort_keys=True), flush=True)
                if not row['more'] or not row['progressed']:
                    break
            return {'jobs': jobs, 'turn_jobs': turn_jobs, 'worktrees': worktrees,
                    'worktree_bytes': worktree_bytes, 'pass_s': pass_s, 'measure_s': measure_s,
                    'max_jobs': max_jobs, 'max_bytes': max_bytes, 'first_prune_s': first_prune,
                    'first_prune_paced_s': None if first_prune is None else first_prune + first_prune_wait,
                    'retired': retired, 'skipped_wait_s': clock.skipped, 'passes': rows}
        finally:
            daemon_module.maintenance, daemon_module.time = real_maintenance, real_time
            service.store.transaction = real_transaction
            service.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keep-store', action='store_true', help='retain the synthetic fixture for separate inspection/cleanup')
    for name, default, kind in [('jobs', 3800, int), ('turn-jobs', 0, int), ('worktrees', 890, int),
                               ('files', 4, int), ('worktree-bytes', 280000, int), ('max-passes', 3, int),
                               ('pass-s', 30, float), ('measure-s', 30, float), ('max-jobs', 500, int),
                               ('max-bytes', 2 * 1024**3, int)]:
        parser.add_argument('--' + name, default=default, type=kind)
    args = parser.parse_args(argv)
    if not (0 <= args.turn_jobs <= args.jobs and 0 <= args.worktrees <= args.jobs and args.jobs > 0
            and args.files > 0 and args.max_passes > 0 and args.pass_s > 0 and args.measure_s >= 0
            and args.worktree_bytes >= 0 and args.max_jobs >= 0 and args.max_bytes >= 0):
        parser.error('counts, bytes and time budgets must be valid and nonnegative')
    result = measure(**vars(args))
    print('SUMMARY ' + json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
