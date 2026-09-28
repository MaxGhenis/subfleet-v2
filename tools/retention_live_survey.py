"""Read-only round-three retention survey. No maintenance, indexes or Git objects.

Counts are conservative eventual candidates under budget pressure, not predicted
one-pass throughput. Raw blob comparisons may conservatively reject filtered
working files. All live Git calls disable optional locks and lazy fetching.
"""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from subfleet import retention, retention_salvage
from subfleet.store import Store
from subfleet.conversations.turn import TERMINAL_STATES

os.environ.update(GIT_OPTIONAL_LOCKS='0', GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0',
                  GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='core.fsmonitor', GIT_CONFIG_VALUE_0='false',
                  GIT_CONFIG_KEY_1='core.untrackedCache', GIT_CONFIG_VALUE_1='false')


_budget = threading.local()

def checkpoint():
    if time.monotonic() >= getattr(_budget, "deadline", float("inf")):
        raise TimeoutError("survey candidate deadline exceeded; preservation remains unproved")


def git(path, *args):
    checkpoint()
    result = subprocess.run(['git', '-C', str(path), '-c', 'core.fsmonitor=false',
                             '-c', 'core.untrackedCache=false', '--no-replace-objects', *args],
                            capture_output=True, timeout=max(.01, min(120, getattr(_budget, "deadline", float("inf"))-time.monotonic())), check=True)
    return result.stdout


def size(path):
    total = 0
    for value in retention._file_sizes(path):
        checkpoint()
        total += value
    return total


def file_blob(path, algorithm):
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        data = os.fsencode(os.readlink(path))
        return '120000', hashlib.new(algorithm, b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError('nonregular tracked entry')
    digest = hashlib.new(algorithm, b'blob ' + str(metadata.st_size).encode() + b'\0')
    with path.open('rb') as file:
        while chunk := file.read(1024*1024):
            checkpoint()
            digest.update(chunk)
    if path.stat().st_mtime_ns != metadata.st_mtime_ns:
        raise ValueError('file changed during survey')
    return ('100755' if metadata.st_mode & 0o111 else '100644'), digest.hexdigest()


def content_preserved(tree, artifacts):
    if git(tree, 'ls-files', '--unmerged'):
        return False, 'unmerged-index'
    if git(tree, 'diff-index', '--cached', '--name-only', 'HEAD', '--'):
        return False, 'staged-index'
    # Match the production proof's stat-cache model. The real index equals
    # HEAD (checked above); only changed, untracked, or hiding-flag paths need
    # independent hashing. This also handles unchanged filtered files without
    # running filters ourselves or creating an index/object.
    actual = {}
    for entry in git(tree, 'ls-tree', '-r', '-z', 'HEAD').split(b'\0'):
        if entry:
            description, name = entry.split(b'\t', 1)
            mode, kind, blob = description.split(b' ')
            actual[name] = (mode.decode(), blob.decode())
    changed = set(git(tree, 'diff-files', '--name-only', '-z', '--').split(b'\0'))
    changed.update(git(tree, 'ls-files', '-o', '--exclude-standard', '-z').split(b'\0'))
    for record in git(tree, 'ls-files', '-v', '-z').split(b'\0'):
        if len(record) >= 3 and (record[:1].islower() or record[:1] in (b'S', b's')):
            changed.add(record[2:])
    algorithm = git(tree, 'rev-parse', '--show-object-format').strip().decode()
    for raw in changed - {b''}:
        path = tree / os.fsdecode(raw)
        if path.exists() or path.is_symlink():
            actual[raw] = file_blob(path, algorithm)
        else:
            actual.pop(raw, None)
    for ref in ['HEAD', *(a['path'] for a in artifacts)]:
        try:
            entries = git(tree, 'ls-tree', '-r', '-z', ref).split(b'\0')
        except subprocess.CalledProcessError:
            continue
        expected = {}
        for entry in entries:
            if not entry:
                continue
            description, name = entry.split(b'\t', 1)
            mode, kind, blob = description.split(b' ')
            expected[name] = (mode.decode(), blob.decode())
        if actual == expected:
            return True, None
    return False, 'working-tree-mismatch'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, default=Path.home()/'.subfleet')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--candidate-seconds', type=float, default=120)
    args = parser.parse_args()
    root = args.state.resolve()
    started = time.time()
    policy = json.loads((root/'policy.json').read_text()).get('retention', {})
    with Store(root/'state.sqlite3', read_only=True) as store:
        jobs = store.list_jobs()
        artifacts = store.query("SELECT r.*,a.job_id FROM artifacts r JOIN attempts a USING(attempt_id) WHERE r.role='salvage'")
        by_job = {}
        for a in artifacts:
            by_job.setdefault(a['job_id'], []).append(a)
        conversation_pins = set()
        cv = root/'conversations.sqlite3'
        if cv.exists():
            with sqlite3.connect(cv.as_uri()+'?mode=ro', uri=True) as db:
                for message_id, identity, state in db.execute('SELECT message_id,job_id,state FROM messages'):
                    if state not in TERMINAL_STATES:
                        if identity:
                            conversation_pins.add(identity)
                        conversation_pins.update(j['job_id'] for j in jobs if j['request_id'].startswith(f'turn:{message_id}:'))
                for (identity,) in db.execute('SELECT conversation_id FROM conversations WHERE blocked_by IS NOT NULL'):
                    conversation_pins.update(j['job_id'] for j in jobs if j['kind']=='turn' and j['name']==f'turn-{identity}')
        ordinary = retention._pins(store, conversation_pins, {a['artifact_id'] for a in artifacts},
                                   turn_keep_s=float(policy.get('turn_keep_days', 14))*86400)
        print(f"Snapshot: {len(jobs)} jobs, {len(ordinary)} ordinary pins; proving salvage", flush=True)
        job_map = {j['job_id']: j for j in jobs}
        attempts = {a['attempt_id']: a for a in store.query('SELECT attempt_id,job_id,seq FROM attempts')}
        class SnapshotEvidence:
            def one(self, sql, params):
                attempt = attempts.get(params[0])
                if attempt is None:
                    return None
                if sql.startswith('SELECT j.*'):
                    return job_map.get(attempt['job_id'])
                if sql.startswith('SELECT seq') and attempt['job_id'] == params[1]:
                    return {'seq': attempt['seq']}
                return None
        proof = retention_salvage.SalvageReachability(SnapshotEvidence(), root)
        def check_salvage(job):
            return job['job_id'], all(proof(a) for a in by_job.get(job['job_id'], []))
        with ThreadPoolExecutor(max_workers=6) as pool:
            salvage_ok = dict(pool.map(check_salvage, (j for j in jobs if j['job_id'] not in ordinary)))

    print("Salvage proofs complete; measuring and checking worktrees", flush=True)

    def inspect(job):
        identity = job['job_id']
        _budget.deadline = time.monotonic() + args.candidate_seconds
        out = {'job_id': identity, 'kind': job['kind'], 'worktree': job.get('worktree'), 'blocker': None, 'worktree_bytes': 0, 'job_bytes': 0, 'worktree_exists': False}
        try:
            tree = retention._owned_worktree(job, root)
            out['worktree_exists'] = tree is not None and tree.exists()
            if identity in ordinary:
                out['blocker'] = 'ordinary-pin'
            elif not salvage_ok[identity]:
                out['blocker'] = 'unproved-salvage'
            elif out['worktree_exists']:
                out['ignored_entries'] = [os.fsdecode(p) for p in git(tree, 'ls-files', '-o', '-i', '--exclude-standard', '--directory', '-z').split(b'\0') if p]
                out['noncache_ignored'] = [p for p in out['ignored_entries'] if not retention_salvage._regenerable_ignored(p)]
                retention_salvage.prove_worktree_preserved(job, root, by_job.get(identity, []), deadline=_budget.deadline)
                ok, reason = content_preserved(tree, by_job.get(identity, []))
                if not ok:
                    out['blocker'] = reason
            if out['blocker'] is None:
                if out['worktree_exists']:
                    out['worktree_bytes'] = size(tree)
                out['job_bytes'] = size(root/'jobs'/identity)
        except Exception as exc:
            error = str(exc)
            out['detail'] = error
            out['blocker'] = ('survey-timeout' if isinstance(exc, (retention._Interrupted, TimeoutError, subprocess.TimeoutExpired)) else
                              'unheld-head' if 'allowed named ref' in error else
                              'noncache-ignored' if 'ignored worktree entry' in error else
                              'nested-git' if 'Git' in error and ('nested' in error or 'bare' in error) else 'proof-or-size-error')
        return out
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = []
        for future in as_completed([pool.submit(inspect, job) for job in jobs]):
            results.append(future.result())
            print(f"Checked {len(results)}/{len(jobs)} jobs", flush=True)
            args.output.with_suffix('.partial.json').write_text(json.dumps(results))
    eligible = [r for r in results if r['blocker'] is None]
    existing = [r for r in eligible if r['worktree_exists']]
    summary = {'job_rows': len(jobs), 'eligible_rows': len(eligible), 'retirable_worktrees': len(existing),
               'retirable_worktree_bytes': sum(r['worktree_bytes'] for r in existing),
               'retirable_total_bytes': sum(r['worktree_bytes']+r['job_bytes'] for r in eligible),
               'blockers': dict(Counter(r['blocker'] for r in results if r['blocker'])),
               'blocked_worktrees': dict(Counter(r['blocker'] for r in results if r['blocker'] and r['worktree_exists'])),
               'existing_owned_worktrees': sum(r['worktree_exists'] for r in results),
               'elapsed_seconds': time.time()-started, 'policy': policy,
               'candidate_seconds': args.candidate_seconds,
               'method': 'read-only stat-aware eventual eligibility lower bound; timed-out proofs remain blocked; no budget trimming or legacy waiver; changed/flagged files compared as raw blobs'}
    args.output.write_text(json.dumps({'summary': summary, 'jobs': results}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
