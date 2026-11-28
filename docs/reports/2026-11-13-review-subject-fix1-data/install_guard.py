"""Install this job's reviewed guard before any pytest or test imports."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import sysconfig

ROOT = Path('/private/tmp/sf-a2-fix1')
PROJECT = Path('/Users/maxghenis/.subfleet/worktrees/20261115-180648-rv-a2-fix-r1b')
VERSION = 'sf-a2-fix1-strict-v2'


def _allowed(path):
    return (path in (ROOT / 'sitecustomize.py', PROJECT / 'sitecustomize.py')
            or (path.name == 'sitecustomize.py' and path.parent.name == 'site-packages'
                and (path.is_relative_to(PROJECT / '.venv' / 'lib')
                     or path.is_relative_to(ROOT / 'uv-cache'))))


def install(*, upgrade=False):
    source = (ROOT / 'sitecustomize-proposal.py').read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    purelib = Path(sysconfig.get_path('purelib'))
    if not (purelib.is_relative_to(ROOT / 'uv-cache')
            or purelib.is_relative_to(PROJECT / '.venv')):
        raise RuntimeError('guard installation refused outside the job interpreters')
    targets = [ROOT / 'sitecustomize.py', PROJECT / 'sitecustomize.py',
               purelib / 'sitecustomize.py']
    targets.extend(path / 'sitecustomize.py' for path in
                   (PROJECT / '.venv' / 'lib').glob('python*/site-packages'))
    manifest = ROOT / 'guard-installations.jsonl'
    previous = {}
    if manifest.exists():
        for line in manifest.read_text().splitlines():
            entry = json.loads(line)
            path = Path(entry['path'])
            if not _allowed(path):
                raise RuntimeError('unexpected installation manifest path')
            previous[path] = entry['sha256']
    if upgrade:
        targets.extend(path for path in previous if path.parent.is_dir())
    targets = list(dict.fromkeys(targets))
    for target in targets:
        if not _allowed(target) or target.is_symlink():
            raise RuntimeError('refusing an unexpected guard target')
        if target.exists():
            current = hashlib.sha256(target.read_bytes()).hexdigest()
            if current != digest and not (upgrade and previous.get(target) == current):
                raise RuntimeError('refusing to replace an existing sitecustomize file: ' + str(target))
    existing = sys.modules.get('sitecustomize')
    if not upgrade and existing is not None and getattr(existing, '_GUARD_VERSION', None) != VERSION:
        raise RuntimeError('refusing to replace an already loaded sitecustomize module')
    for target in targets:
        current = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
        if current != digest:
            # Record the intended hash chain before changing any installed file.
            with manifest.open('a') as stream:
                stream.write(json.dumps({'path': str(target), 'sha256': digest,
                                         'previous_sha256': current, 'version': VERSION,
                                         'status': 'write planned; old hash verified'}) + '\n')
        if current is None:
            # Exclusive creation also refuses a concurrently introduced file.
            with target.open('xb') as stream:
                stream.write(source)
        elif current != digest:
            assert upgrade and previous.get(target) == current
            staging = target.with_name('.sitecustomize.sf-a2-fix1-upgrade')
            with staging.open('xb') as stream:
                stream.write(source)
            staging.replace(target)
    if upgrade:
        # Existing audit hooks cannot be removed. Exit this preparation process
        # and start a fresh interpreter to use V2 without retaining the V1 hook.
        return targets
    if existing is None:
        spec = importlib.util.spec_from_file_location('sitecustomize', ROOT / 'sitecustomize.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules['sitecustomize'] = module
        spec.loader.exec_module(module)
    if getattr(sys.modules['sitecustomize'], '_GUARD_VERSION', None) != VERSION:
        raise RuntimeError('job guard did not initialize')
    return targets


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upgrade', action='store_true')
    options = parser.parse_args()
    installed = install(upgrade=options.upgrade)
    print(json.dumps({'guard': VERSION, 'installed_paths': list(map(str, installed))}))
