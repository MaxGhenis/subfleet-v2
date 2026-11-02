#!/usr/bin/env python3
"""D-RET-WALK1: measure the repository walk with synthetic data and one worker.

Use TMPDIR=/private/tmp/sf-repo-walk (create it first). The reusable fixture
contains 18,000 job rows, 5,140 distinct source workdirs, 2,300 existing empty
source folders under three Git repositories, and three owned registrations.
All other owned trees are absent, like the old jobs which exposed the stall.
Only this fixture is read or written; no daemon, live store, or worker runs.

The old implementation is loaded from the task's base commit, including its
owned_worktree function. Both implementations use retention's real Git helper
and QoS policy. Fixture creation is excluded. Each mode prints JSON timings;
`new` also measures the first loop by clearing every workdir in copied rows.

Usage: .venv/bin/python tools/measure_retention_repo_walk.py old
       .venv/bin/python tools/measure_retention_repo_walk.py new
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
SCRATCH = Path(os.path.realpath(os.environ.get('TMPDIR', '/private/tmp/sf-repo-walk')))
BASELINE = '18aa2a69562fc454241a0bfde5a0d32536c63680'
sys.path.insert(0, str(REPO))
from subfleet import retention_archive as archive  # noqa: E402
from subfleet import retention_git as rgit  # noqa: E402


def baseline():
    source = subprocess.run(['git', 'show', f'{BASELINE}:subfleet/retention_archive.py'],
                            cwd=REPO, capture_output=True, text=True, check=True).stdout
    tree = ast.parse(source)
    names = {'owned_worktree', 'known_repositories'}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    scope = dict(archive.__dict__)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), f'{BASELINE}:retention_archive.py', 'exec'), scope)
    return scope['owned_worktree'], scope['known_repositories']


def fixture():
    fixture_root = SCRATCH / 'scale-fixture'
    state = fixture_root / 'state'
    allocated = state / 'worktrees'
    allocated.mkdir(parents=True, exist_ok=True)
    repositories = [fixture_root / f'repo-{i}' for i in range(3)]
    for repository in repositories:
        repository.mkdir(exist_ok=True)
        if not (repository / '.git').is_dir():
            subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', 'init', '-q', str(repository)], check=True)
    workdirs = []
    for i in range(5140):
        workdir = repositories[i % 3] / f'source-{i:05d}'
        if i < 2300:
            workdir.mkdir(exist_ok=True)
        workdirs.append(str(workdir))
    # A few real-shaped registrations exercise that branch without making
    # thousands of fake Git admin files or running worktree-add processes.
    for i, repository in enumerate(repositories):
        tree = allocated / f'job-{i:05d}'
        tree.mkdir(exist_ok=True)
        common = repository / '.git'
        admin = common / 'worktrees' / tree.name
        admin.mkdir(parents=True, exist_ok=True)
        (tree / '.git').write_text(f'gitdir: {admin}\n')
        (admin / 'commondir').write_text('../..\n')
        (admin / 'gitdir').write_text(f'{tree / ".git"}\n')
        (admin / 'HEAD').write_text('ref: refs/heads/main\n')
    jobs = [dict(job_id=f'job-{i:05d}', sandbox='workspace-write', in_place=0,
                 worktree=str(allocated / f'job-{i:05d}'), workdir=workdirs[i % len(workdirs)],
                 workdir_head='a' * 40) for i in range(18000)]
    return state, jobs


def first_loop(jobs, root, owned):
    found = {}
    registrations = 0
    for job in jobs:
        try:
            tree = owned(job, root)
        except ValueError:
            continue
        if tree is not None and os.path.lexists(tree):
            try:
                reg, _ = rgit.registration(tree)
                registrations += 1
            except OSError:
                continue
            if reg is not None:
                found.setdefault(str(reg.common), reg.common)
    return found, registrations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('old', 'new', 'first-loop'))
    args = parser.parse_args()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    state, jobs = fixture()
    old_owned, old_walk = baseline()
    expected = sorted(str(SCRATCH / 'scale-fixture' / f'repo-{i}' / '.git') for i in range(3))
    print(json.dumps(dict(stage='fixture', jobs=len(jobs), unique_workdirs=5140,
                          existing_workdirs=2300, existing_owned_trees=3, repositories=3,
                          load_avg=os.getloadavg())), flush=True)
    if args.mode in ('old', 'first-loop'):
        started = time.perf_counter()
        found, registrations = first_loop(jobs, state, old_owned)
        result = dict(stage='old-first-loop', seconds=time.perf_counter() - started,
                      registration_calls=registrations, repositories=len(found))
        (SCRATCH / 'scale-old-first-loop.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result), flush=True)
    if args.mode == 'first-loop':
        return
    if args.mode == 'new':
        jobs_without_workdirs = [dict(job, workdir=None) for job in jobs]
        started = time.perf_counter()
        started_cpu = time.process_time()
        found = archive.known_repositories(jobs_without_workdirs, state)
        result = dict(stage='new-first-loop', seconds=time.perf_counter() - started,
                      process_cpu_seconds=time.process_time() - started_cpu,
                      repositories=len(found))
        (SCRATCH / 'scale-new-first-loop.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result), flush=True)
    original_common = rgit.common_dir
    count = 0
    def counted_common(*args, **kwargs):
        nonlocal count
        count += 1
        return original_common(*args, **kwargs)
    rgit.common_dir = counted_common
    started = time.perf_counter()
    found = (old_walk if args.mode == 'old' else archive.known_repositories)(jobs, state)
    elapsed = time.perf_counter() - started
    assert sorted(map(str, found)) == expected, found
    result = dict(stage=args.mode, seconds=elapsed, subprocess_common_dir_calls=count,
                  repositories=len(found), jobs=len(jobs), distinct_workdirs=5140,
                  existing_workdirs=2300, existing_owned_trees=3)
    (SCRATCH / f'scale-{args.mode}.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
