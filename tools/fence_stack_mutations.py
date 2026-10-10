"""Reproduce fence-stack mutants, one pytest at a time under an outer lockf.

Run with TMPDIR and caches isolated, e.g. lockf -k <suite-lock> python
 tools/fence_stack_mutations.py 140. Logs stay in untracked build/.
A mutation is killed only by test assertions, never collection/setup errors.
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
import subprocess
import sys

DAEMON = 'subfleet/daemon.py'
FOLDERS = 'subfleet/folders.py'
PINS = 'tests/fake/test_detached_cwd_pins.py'
WRITER = 'tests/fake/test_writer_retention_fence.py'
DETACHED = 'tests/fake/test_detached_retention_fence.py'
POLICY = 'tests/fake/test_policy_sandbox.py'


def edit(source, old, new, method=None):
    lines = source.splitlines(keepends=True)
    if method:
        nodes = [n for n in ast.walk(ast.parse(source))
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == method]
        assert len(nodes) == 1, (method, len(nodes))
        node = nodes[0]
        start, end = node.lineno - 1, node.end_lineno
    else:
        start, end = 0, len(lines)
    part = ''.join(lines[start:end])
    assert part.count(old) == 1, (method, old, part.count(old))
    return ''.join(lines[:start]) + part.replace(old, new) + ''.join(lines[end:])


def mutants(head):
    # (name, file, method, before, after, test file, selection)
    yield ('no-resolved-reader-row', DAEMON, '_admit_pass',
           'if job["kind"] != "turn" or run_folder != (write_target or read_folder):',
           'if job["kind"] == "turn" and run_folder != (write_target or read_folder):',
           PINS, '(entire_lease and reserved) or any_detached_cwd_lifetime')
    yield ('reader-row-uses-raw-workspace', DAEMON, '_admit_pass',
           'folders.turn_key(run_folder, job["job_id"], writable=False)',
           'folders.turn_key(workspace, job["job_id"], writable=False)',
           PINS, 'entire_lease and reserved')
    yield ('launch-ignores-resolved-cwd', DAEMON, '_launch',
           'if job["kind"] != "turn" and evidence.get("run_folder"):',
           'if False and evidence.get("run_folder"):', PINS, 'another_swap')
    yield ('quarantine-drops-cwd-row', DAEMON, '_quarantine',
           'state, rc = ("cancelled", 130) if job["cancel_requested_at"] else ("lost", 125)',
           'tx.execute("DELETE FROM leases WHERE holder=? AND lease_key LIKE \'worktree-read:%\'", (a["job_id"],))\n            state, rc = ("cancelled", 130) if job["cancel_requested_at"] else ("lost", 125)',
           PINS, 'entire_lease and quarantined')
    if head == '140':
        writer_guard = 'blockers.extend(key for key in folders.retiring(read, write_target) if key != own)'
        yield ('writer-no-ancestor-fence', DAEMON, '_admit_pass', writer_guard, 'pass',
               PINS, 'writer_git_target')
        yield ('ordinary-holder-above', DAEMON, '_admit_pass', writer_guard,
               'blockers.extend(folders.exclusive_key(each) for each in folders.above(write_target))',
               WRITER, 'held_by_anything_but_retention')
        yield ('read-workdir-not-target', DAEMON, '_admit_pass', writer_guard,
               'blockers.extend(key for key in folders.retiring(read, job["workdir"]) if key != own)',
               PINS, 'writer_git_target')
        yield ('submit-raw-workdir', DAEMON, None,
               'recorded_workdir = folders.canonical(workdir)', 'recorded_workdir = workdir',
               WRITER, 'queued_on_the_tree_under_a_case_alias or data_firmlink')
        yield ('clock-queues-blocked-fence', DAEMON, '_admit_pass',
               'blocked = set(known.get("blocked") or ())', 'blocked = set()',
               WRITER, 'on_its_clock_never_queues')
        yield ('resume-realpath', DAEMON, '_launch_dir',
               'folders.canonical(workspace["worktree"]) != folders.canonical(root)',
               'os.path.realpath(workspace["worktree"]) != os.path.realpath(root)',
               POLICY, 'source_started_under_a_state_root_typed_in_other_capitals')
    else:
        yield ('no-source-fence', DAEMON, '_detached_folders',
               'return [(recorded, False)] + ([(now, False)] if now != recorded else [])',
               'return []', DETACHED, 'submitted_under_retentions_fence')
        yield ('no-early-look', DAEMON, '_admit_pass',
               'if any(self._fence_hold(job, folder, holds, waiters, tier, models, lanes, fold=fold,',
               'if False and any(self._fence_hold(job, folder, holds, waiters, tier, models, lanes, fold=fold,',
               DETACHED, 'no_git_runs_for_an_in_place_writer')
        yield ('no-transaction-fence', DAEMON, '_admit_pass',
               'if job_folders:', 'if False and job_folders:', DETACHED,
               'fence_taken_after_the_early_look')
        yield ('tx-no-fold', DAEMON, '_admit_pass',
               '(folders.retiring_folded if fold else folders.retiring)(read, folder)',
               'folders.retiring(read, folder)', DETACHED, 'transaction_reads_a_legacy_jobs_fence_folded')
        yield ('folded-no-nfc', FOLDERS, 'folded',
               'unicodedata.normalize("NFC", folder).casefold()', 'folder.casefold()',
               DETACHED, 'retiring_folded_is_retention')
        yield ('no-current-folder', DAEMON, '_detached_folders',
               'return [(recorded, False)] + ([(now, False)] if now != recorded else [])',
               'return [(recorded, False)]', DETACHED, 'replaced_by_a_symlink')
        yield ('fence-not-seen', DAEMON, '_seen_held', 'if held:', 'if False and held:',
               DETACHED, 'fence_found_after_the_census')
        yield ('priority-waiter-removed', DAEMON, '_fence_hold',
               'priority_waiters.append(waiter)', 'pass', DETACHED, 'fenced_priority_job_keeps_its_place')


def run(name, tests, expression, logs):
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', tests, '-k', expression],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (logs / (name + '.log')).write_text(result.stdout)
    summary = [line for line in result.stdout.splitlines() if ' in ' in line and
               any(word in line for word in ('passed', 'failed', 'error', 'skipped'))]
    killed = (result.returncode == 1 and 'AssertionError' in result.stdout and
              'ERROR collecting' not in result.stdout and 'ERROR at setup' not in result.stdout)
    print(f'{name}: {"KILLED" if killed else "NOT KILLED"}; exit={result.returncode}; {summary[-1:]!r}', flush=True)
    return killed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('head', choices=('140', '142'))
    parser.add_argument('--only', nargs='+')
    parser.add_argument('--baseline', help='Git revision before the cwd pin, tested against new regressions')
    args = parser.parse_args()
    logs = Path('build/fence-fix/mutants-' + args.head)
    logs.mkdir(parents=True, exist_ok=True)
    failures = []
    if args.baseline:
        path = Path(DAEMON)
        original = path.read_bytes()
        try:
            command = ['git', '--git-dir=.git-local', 'show', args.baseline + ':' + DAEMON]
            path.write_bytes(subprocess.check_output(command))
            if not run('pre-fix-review-probe', PINS, 'entire_lease and reserved', logs):
                failures.append('pre-fix-review-probe')
        finally:
            path.write_bytes(original)
    else:
        for name, filename, method, before, after, tests, expression in mutants(args.head):
            if args.only and name not in args.only:
                continue
            path = Path(filename)
            original = path.read_bytes()
            try:
                changed = edit(original.decode(), before, after, method)
                compile(changed, filename, 'exec')
                path.write_text(changed)
                if not run(name, tests, expression, logs):
                    failures.append(name)
            finally:
                path.write_bytes(original)
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
