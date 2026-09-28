"""Read-only round-four retention survey. No maintenance or live repository writes.

Use the production checks for conditions 2-6, including hashing every tracked
file without filters. Counts are eventual candidates under budget pressure,
not one-pass throughput or authorization to skip the post-rename recheck.
All live Git calls disable optional locks and lazy fetching. The validator's
fresh comparison index is temporary and outside the surveyed repository.
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
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from subfleet import retention, retention_salvage
from subfleet.store import Store
from subfleet.conversations.turn import TERMINAL_STATES

_VALIDATOR_SHA256 = hashlib.sha256(Path(retention_salvage.__file__).read_bytes()).hexdigest()
os.environ.update(GIT_OPTIONAL_LOCKS='0', GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0',
                  GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='core.fsmonitor', GIT_CONFIG_VALUE_0='false',
                  GIT_CONFIG_KEY_1='core.untrackedCache', GIT_CONFIG_VALUE_1='false')


_budget = threading.local()

def checkpoint():
    if time.monotonic() >= getattr(_budget, "deadline", float("inf")):
        raise TimeoutError("survey candidate deadline exceeded; preservation remains unproved")


def size(path):
    total = 0
    for value in retention._file_sizes(path):
        checkpoint()
        total += value
    return total


def blocker_for(exc):
    if isinstance(exc, (retention._Interrupted, TimeoutError, subprocess.TimeoutExpired)):
        return 'survey-timeout'
    error = str(exc).lower()
    if 'condition ' in error:
        return error.split(':', 1)[0].replace(' ', '-')
    for needles, reason in (
        (('baseline', 'salvage commit', 'allowed named ref', 'caller repository'), 'condition-2-head'),
        (('unmerged', 'staged', 'index'), 'condition-3-index'),
        (('filter', 'attributes', 'conversion', 'tracked file', 'working tree'), 'condition-4-content'),
        (('ignored', 'untracked'), 'condition-5-extra-files'),
        (('nested', 'bare', 'admin', 'worktree ref', 'bisect', 'rebase', 'merge', 'stash'), 'condition-6-git-state'),
    ):
        if any(needle in error for needle in needles):
            return reason
    return 'proof-or-size-error'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, default=Path.home()/'.subfleet')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--candidate-seconds', type=float, default=120)
    parser.add_argument('--workers', type=int, default=2)
    args = parser.parse_args()
    root = args.state.resolve()
    output = args.output.resolve()
    if output.is_relative_to(root):
        parser.error('--output must be outside the surveyed state directory')
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
        print(f"Snapshot: {len(jobs)} jobs, {len(ordinary)} ordinary pins", flush=True)
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
    print("Checking salvage pins and strict worktree conditions", flush=True)

    def inspect(job):
        identity = job['job_id']
        _budget.deadline = time.monotonic() + args.candidate_seconds
        out = {'job_id': identity, 'kind': job['kind'], 'worktree': job.get('worktree'), 'blocker': None, 'worktree_bytes': 0, 'job_bytes': 0, 'worktree_exists': False}
        try:
            tree = retention._owned_worktree(job, root)
            out['worktree_exists'] = tree is not None and tree.exists()
            if identity in ordinary:
                out['blocker'] = 'ordinary-pin'
            else:
                proof = retention_salvage.SalvageReachability(SnapshotEvidence(), root,
                                                             deadline=_budget.deadline)
                if not all(proof(a) for a in by_job.get(identity, [])):
                    out['blocker'] = 'unproved-salvage'
                elif out['worktree_exists']:
                    retention_salvage.prove_worktree_preserved(
                        job, root, by_job.get(identity, []), deadline=_budget.deadline)
            if out['blocker'] is None:
                if out['worktree_exists']:
                    out['worktree_bytes'] = size(tree)
                out['job_bytes'] = size(root/'jobs'/identity)
        except Exception as exc:
            out['detail'] = str(exc)
            out['blocker'] = blocker_for(exc)
        return out
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = []
        for future in as_completed([pool.submit(inspect, job) for job in jobs]):
            results.append(future.result())
            print(f"Checked {len(results)}/{len(jobs)} jobs", flush=True)
            output.with_suffix('.partial.json').write_text(json.dumps(results))
    eligible = [r for r in results if r['blocker'] is None]
    existing = [r for r in eligible if r['worktree_exists']]
    summary = {'job_rows': len(jobs), 'eligible_rows': len(eligible), 'retirable_worktrees': len(existing),
               'retirable_worktree_bytes': sum(r['worktree_bytes'] for r in existing),
               'retirable_total_bytes': sum(r['worktree_bytes']+r['job_bytes'] for r in eligible),
               'blockers': dict(Counter(r['blocker'] for r in results if r['blocker'])),
               'blocked_worktrees': dict(Counter(r['blocker'] for r in results if r['blocker'] and r['worktree_exists'])),
               'existing_owned_worktrees': sum(r['worktree_exists'] for r in results),
               'elapsed_seconds': time.time()-started, 'policy': policy,
               'started_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(started)),
               'candidate_seconds': args.candidate_seconds,
               'workers': args.workers,
               'validator_sha256': _VALIDATOR_SHA256,
               'method': 'read-only eventual eligibility lower bound using production conditions 2-6; every tracked file hashed as raw bytes; timed-out proofs remain blocked; no budget trimming, legacy waiver, or post-rename transaction simulated'}
    output.write_text(json.dumps({'summary': summary, 'jobs': sorted(results, key=lambda r: r['job_id'])}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
