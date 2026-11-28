import json
import os
from pathlib import Path
import subprocess
import tarfile

root = Path.cwd()
scratch = Path('/private/tmp/sf-a2-fix1')
fixtures = root / 'tests/fixtures/store/review-upgrade'
revisions = {'release-217-v10': 'a4aded8859fca77678f59e10261ad701160aefb8', 'wake-caller-307-v11': '02ae7d1eda99bbfc7a44bc85b98218cc3a960219', 'review-subject-old-v11': '5b06c1826bc0641f65b1293197a505b8c8a4843a'}
code = '''
import json
from pathlib import Path
import sqlite3
import sys
from subfleet.store import MIGRATIONS, Store
path, fixture, commit = map(str, sys.argv[1:])
with Store(path) as store:
    store.add_job(job_id='historic-review', request_id='historic-request', payload_digest='synthetic-digest', kind='dispatch', task='review', workdir='/synthetic/work', prompt_path='/synthetic/prompt', sandbox='read-only')
    store.add_notice('historic-review', 'synthetic notice', 'synthetic-session')
    if commit.startswith('5b06c'):
        store.update_job('historic-review', caller_family='codex', review_subject='https://github.com/example/project/pull/17', review_round=2)
with sqlite3.connect(path) as conn:
    conn.execute("UPDATE schema_version SET applied_at='2026-11-01T00:00:00Z'")
    conn.execute("UPDATE jobs SET created_at='2026-11-01T00:00:00Z'")
    conn.execute("UPDATE notices SET created_at='2026-11-01T00:00:00Z'")
    conn.execute("UPDATE events SET ts='2026-11-01T00:00:00Z'")
    conn.commit()
    Path(fixture).write_text('-- Created by the unmodified Store from git archive ' + commit + '\\n' + '\\n'.join(conn.iterdump()) + '\\n')
    if commit.startswith('02ae7'):
        payload = {'commit': commit, 'migration': MIGRATIONS[11], 'schema': {name: conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0] for name in ['notices', 'caller_wake_sessions', 'notices_caller_wake']}}
        Path(fixture).with_name('wake-caller-307-ddl.json').write_text(json.dumps(payload, indent=2) + '\\n')
'''
for name, commit in revisions.items():
    tree = scratch / 'schema-archives' / name
    tree.mkdir()
    archive = tree.with_suffix('.tar')
    with archive.open('wb') as output:
        subprocess.run(['git', 'archive', commit, 'subfleet'], check=True, stdout=output)
    with tarfile.open(archive) as source:
        source.extractall(tree, filter='data')
    env = {'HOME': str(scratch / 'archive-home'), 'PATH': os.defpath, 'PYTHONPATH': str(tree), 'TMPDIR': str(scratch)}
    subprocess.run([str(root / '.venv/bin/python'), '-c', code, str(tree / 'historic.sqlite3'), str(fixtures / (name + '.sql')), commit], cwd=tree, env=env, check=True)
    print(name, commit)
