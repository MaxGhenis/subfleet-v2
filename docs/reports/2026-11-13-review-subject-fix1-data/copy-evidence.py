"""Copy reviewed small fix-round evidence; never delete or execute source files.

Prepared for the coordinator. Run after final ledger rendering and owned tests
have ended. After cleanup appends cleanup.jsonl, run the copied script with
--manifest-only to refresh hashes without requiring the removed scratch tree.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import stat

S = Path('/private/tmp/sf-a2-fix1')
W = Path('/Users/maxghenis/.subfleet/worktrees/20261115-180648-rv-a2-fix-r1b')
E = W / 'docs/reports/2026-11-13-review-subject-fix1-data'
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 12 * 1024 * 1024
# No recursive source discovery. These are reviewed data/script/action files,
# never database/cache/archive/provider output or full test/progress logs.
REQUIRED = {
    'ledger.jsonl': 'test-commands.jsonl',
    'ledger-table.md': 'commands.md',
    'ledger-aggregate.json': 'ledger-aggregate.json',
    'ledger-summary.md': 'ledger-summary.md',
    'batch-selections.jsonl': 'batch-selections.jsonl',
    'batch-results.jsonl': 'batch-results.jsonl',
    'importer-batches.json': 'importer-batches.json',
    'importer-inventory.json': 'importer-inventory.json',
    'importer-refinement.json': 'importer-refinement.json',
    'skipped23-audit.json': 'skipped23-audit.json',
    'importer-safe-selection.json': 'importer-safe-selection.json',
    'importers-run.txt': 'importers-run.txt',
    'importer-extras.txt': 'importer-extras.txt',
    'importers-safe-mixed-nodes.txt': 'importers-safe-mixed-nodes.txt',
    'stage1-files.txt': 'stage1-files.txt',
    'active-selection.txt': 'active-selection-snapshot.txt',
    'root-actions.md': 'root-actions.md',
    'schema-ledger.md': 'schema-actions.md',
    'cli-actions.md': 'cli-actions.md',
    'subject-actions.md': 'subject-actions.md',
    'safety-audit.md': 'safety-audit.md',
    'guard-proof-data.py': 'guard-proof-data.py',
    'guard-installations.jsonl': 'guard-installations.jsonl',
    'guard-skips.jsonl': 'guard-skips.jsonl',
    'outside.jsonl': 'outside-paths.jsonl',
    'run.py': 'run.py',
    'run4.py': 'run4.py',
    'run_historical_cli.py': 'run_historical_cli.py',
    'run-importer-batches.py': 'run-importer-batches.py',
    'run-importer-extras.py': 'run-importer-extras.py',
    'run-safe-mixed-importers.py': 'run-safe-mixed-importers.py',
    'sf_fix1_safety.py': 'sf_fix1_safety.py',
    'sf_fix1_mixed.py': 'sf_fix1_mixed.py',
    'sitecustomize-proposal.py': 'sitecustomize-proposal.py',
    'sitecustomize.py': 'sitecustomize.py',
    'install_guard.py': 'install_guard.py',
    'guard-removal.py': 'guard-removal.py',
    'final-cleanup.py': 'final-cleanup.py',
    'render-ledger.py': 'render-ledger.py',
    'make-schema-fixtures.py': 'make-schema-fixtures.py',
    'importer_inventory_fast.py': 'importer_inventory_fast.py',
    'finalize-importers.py': 'finalize-importers.py',
    'frontend-conditional-plugin.py.txt': 'frontend-conditional-plugin.py.txt',
    'frontend-scratch-patch.py.txt': 'frontend-scratch-patch.py.txt',
    'copy-evidence.py': 'copy-evidence.py',
}
OPTIONAL = {
    'cli-report-notes.md': 'cli-report-notes.md',
    'importer-inventory-summary.txt': 'importer-inventory-summary.txt',
    'importers-direct.txt': 'importers-direct.txt',
    'importers-transitive.txt': 'importers-transitive.txt',
    'importers-safe-direct.txt': 'importers-safe-direct.txt',
    'importers-first-stage.txt': 'importers-first-stage.txt',
    'importers-restore-three.txt': 'importers-restore-three.txt',
    'importers-skipped.txt': 'importers-skipped.txt',
    'interrupted-batch10-counts.json': 'interrupted-batch10-counts.json',
    'root-state.json': 'root-state-snapshot.json',
    'guard-removals.jsonl': 'guard-removals.jsonl',
    'guard-proof-result.json': 'guard-proof-result.json',
    'guard-proof-results.json': 'guard-proof-results.json',
    'guard-proof.json': 'guard-proof.json',
    'guard-proof-data.json': 'guard-proof-data.json',
    'guard-proof-data-result.json': 'guard-proof-data-result.json',
    'guard-proof-results-v1.json': 'guard-proof-results-v1.json',
    'guard-proof-results-v2.json': 'guard-proof-results-v2.json',
    'guard-v1.py': 'guard-v1.py',
    'prepare-guard-exclusions.py': 'prepare-guard-exclusions.py',
    'importer-batch-11-observed-failures.json': 'importer-batch-11-observed-failures.json',
    'frontend-approved-binaries.jsonl': 'frontend-approved-binaries.jsonl',
    'sf_fix1_graphql.py': 'sf_fix1_graphql.py',
    'graphql-callback-proof.py': 'graphql-callback-proof.py',
    'graphql-callback-proof-results.json': 'graphql-callback-proof-results.json',
    'graphql-callback-proof.json': 'graphql-callback-proof.json',
    'graphql-callbacks.jsonl': 'graphql-callbacks.jsonl',
    'NO_ACTIVE_TESTS.json': 'NO_ACTIVE_TESTS.json',
    'extras-direct.sh': 'extras-direct.sh',
    'mixed-direct.sh': 'mixed-direct.sh',
    'final-direct.sh': 'final-direct.sh',
    'final-direct-02.sh': 'final-direct-02.sh',
    'guard-case-results.json': 'guard-case-results.json',
}


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def within_evidence(path):
    if path != E and E not in path.parents:
        raise RuntimeError('Evidence target escapes its fixed directory')
    current = path
    while current != W:
        if current.is_symlink():
            raise RuntimeError('Refusing a symlink in evidence ancestors')
        if current == current.parent:
            raise RuntimeError('Evidence ancestor check escaped the workspace')
        current = current.parent


def read_small(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError('Evidence source/target is not a regular file: ' + path.name)
    limit = 8 * 1024 * 1024 if path == E / 'cleanup.jsonl' else MAX_FILE_BYTES
    if info.st_size > limit:
        raise RuntimeError('Evidence file exceeds reviewed small-file limit: ' + path.name)
    payload = path.read_bytes()
    if len(payload) > limit:
        raise RuntimeError('Evidence file grew beyond small-file limit: ' + path.name)
    return payload


def records(payload):
    return [json.loads(line) for line in payload.decode().splitlines() if line.strip()]


def validate(name, payload):
    if name.endswith('.json'):
        json.loads(payload)
    elif name.endswith('.jsonl'):
        entries = records(payload)
        if re.fullmatch(r'guard-denials-[0-9]+\.jsonl', name):
            for entry in entries:
                if set(entry) != {'reason', 'at', 'frames'} or not isinstance(entry['reason'], str):
                    raise RuntimeError('Guard denial is not a reviewed static-reason record')
                for frame in entry['frames']:
                    if set(frame) != {'file', 'line', 'function'}:
                        raise RuntimeError('Guard denial contains unexpected frame data')
        elif name == 'guard-skips.jsonl':
            for entry in entries:
                if not set(entry) <= {'nodeid', 'reason', 'basetemp', 'pid'}:
                    raise RuntimeError('Guard skip contains unexpected data fields')
                if not str(entry.get('reason', '')).startswith('sf-a2-fix1 safety guard:'):
                    raise RuntimeError('Guard skip is not a static guard refusal')
        elif name == 'guard-installations.jsonl':
            for entry in entries:
                keys = set(entry)
                if keys == {'path', 'sha256'}:
                    continue
                if (keys != {'path', 'sha256', 'previous_sha256', 'version', 'status'}
                        or entry['version'] != 'sf-a2-fix1-strict-v2'
                        or entry['status'] != 'write planned; old hash verified'
                        or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])
                        or entry['previous_sha256'] is not None
                        and not re.fullmatch(r'[0-9a-f]{64}', entry['previous_sha256'])):
                    raise RuntimeError('Guard installation contains unexpected fields')


def transform(name, payload):
    # commands.md sits inside the data directory; renderer links are relative to
    # docs/reports because the same table can be embedded in the main report.
    if name in {'ledger-table.md', 'ledger-summary.md'}:
        text = payload.decode()
        text = text.replace('](2026-11-13-review-subject-fix1-data/', '](')
        return text.encode()
    return payload


def manifest():
    within_evidence(E)
    rows = []
    for path in sorted(E.rglob('*')):
        within_evidence(path)
        if path.name == 'sha256-manifest.json' or path.is_dir():
            continue
        payload = read_small(path)
        rows.append({'path': path.relative_to(E).as_posix(), 'bytes': len(payload), 'sha256': sha(payload)})
    payload = (json.dumps({'algorithm': 'sha256', 'files': rows,
                          'excluded_self': 'sha256-manifest.json'}, indent=2) + '\n').encode()
    (E / 'sha256-manifest.json').write_bytes(payload)
    return rows


def copy():
    within_evidence(E)
    old = {}
    audit_path = E / 'copy-audit.json'
    if audit_path.exists():
        old_audit = json.loads(read_small(audit_path))
        old = {item['destination']: item['destination_sha256'] for item in old_audit['copies']}
    names = dict(REQUIRED)
    missing = [name for name in REQUIRED if not (S / name).exists()]
    if missing:
        raise RuntimeError('Required evidence is missing: ' + ', '.join(missing))
    missing_optional = []
    for name, target in OPTIONAL.items():
        if (S / name).exists():
            names[name] = target
        else:
            missing_optional.append(name)
    # The reviewed denial writer emits only a fixed reason and code locations,
    # never argv/environment/locals. Match top-level filenames, not subtrees.
    for path in S.iterdir():
        if re.fullmatch(r'guard-denials-[0-9]+\.jsonl', path.name):
            names[path.name] = path.name
    pending, copied = [], []
    total = 0
    for name, target_name in sorted(names.items()):
        source = S / name
        payload = read_small(source)
        validate(name, payload)
        transformed = transform(name, payload)
        target = E / target_name
        within_evidence(target)
        if target.exists():
            existing = read_small(target)
            if existing != transformed and old.get(target_name) != sha(existing):
                raise RuntimeError('Preserving an unowned/edited evidence file: ' + target_name)
        total += len(transformed)
        if total > MAX_TOTAL_BYTES:
            raise RuntimeError('Evidence exceeds reviewed total size limit')
        pending.append((target, transformed))
        copied.append({'source': name, 'destination': target_name,
                       'source_bytes': len(payload), 'source_sha256': sha(payload),
                       'destination_bytes': len(transformed), 'destination_sha256': sha(transformed),
                       'transform': 'relative Markdown links' if transformed != payload else None})
    E.mkdir(parents=True, exist_ok=True)
    for target, payload in pending:
        target.write_bytes(payload)
    audit = {
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'copy_command': '.venv/bin/python /private/tmp/sf-a2-fix1/copy-evidence.py',
        'scratch_source': str(S), 'workspace': str(W),
        'copies': copied, 'missing_optional_files': missing_optional,
        'excluded_source_categories': ['databases', 'cache trees', 'archives/extracted trees',
            'environment dumps', 'full test logs', 'per-test progress logs',
            'cleanup-dryrun.json (mixed output; cleanup.jsonl will be written by cleanup helper)'],
        'scope': 'Snapshot of reviewed evidence only; selections/actions may describe intermediate stages.',
    }
    audit_path.write_text(json.dumps(audit, indent=2) + '\n')
    rows = manifest()
    print(json.dumps({'copied_files': len(copied), 'copied_bytes': total,
                      'manifest_files': len(rows), 'destination': str(E)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest-only', action='store_true',
                        help='Refresh hashes after cleanup; does not read scratch sources')
    options = parser.parse_args()
    if options.manifest_only:
        if not E.is_dir():
            raise RuntimeError('Evidence directory is missing')
        print(json.dumps({'manifest_files': len(manifest()), 'destination': str(E)}))
    else:
        copy()


if __name__ == '__main__':
    main()
