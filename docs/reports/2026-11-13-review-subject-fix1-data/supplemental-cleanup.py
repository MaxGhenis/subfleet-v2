"""Remove two generated test-cache roots omitted from the initial inventory."""

import json
from pathlib import Path
import runpy
import subprocess

W = Path('/Users/maxghenis/.subfleet/worktrees/20261115-180648-rv-a2-fix-r1b')
E = W / 'docs/reports/2026-11-13-review-subject-fix1-data'
DISPOSE = W / 'sf-a2-fix1-extra-cleanup'
ROOTS = {'.hypothesis': {'constants', 'unicode_data', '.gitignore', 'examples'},
         'build': {'frontend-probes'}}

assert Path.cwd() == W
assert not DISPOSE.exists() and not DISPOSE.is_symlink()
helpers = runpy.run_path(str(E / 'final-cleanup.py'), run_name='sf_extra_cleanup_helpers')
tracked = subprocess.run(['git', '-c', 'core.fsmonitor=false', 'ls-files', '-z', '--', *ROOTS],
                         capture_output=True, check=True).stdout
assert not tracked, 'refusing to remove tracked files'
for name, allowed in ROOTS.items():
    path = W / name
    assert path.is_dir() and not path.is_symlink()
    assert {child.name for child in path.iterdir()} <= allowed

with (E / 'supplemental-cleanup.jsonl').open('x') as journal:
    def log(event, **values):
        journal.write(json.dumps({'event': event, **values}) + '\n')
        journal.flush()

    for name in ROOTS:
        log('inventory', path=str(W / name), category='generated-test-cache')
    log('inventory', path=str(DISPOSE), category='disposal-directory')
    DISPOSE.mkdir()
    for name in ROOTS:
        path, target = W / name, DISPOSE / name
        log('move-start', original=str(path), disposal=str(target))
        path.rename(target)
        log('delete-start', original=str(path), disposal=str(target))
        assert 'sf-a2-fix1' in str(target)
        helpers['guarded_remove'](target)
        log('delete-done', original=str(path), exists=path.exists(), disposal_exists=target.exists())
    log('delete-start', original=str(DISPOSE))
    assert 'sf-a2-fix1' in str(DISPOSE)
    helpers['guarded_remove'](DISPOSE)
    log('delete-done', original=str(DISPOSE), exists=DISPOSE.exists())
print(json.dumps({'generated_cache_roots_removed': len(ROOTS), 'disposal_removed': not DISPOSE.exists()}))
