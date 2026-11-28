"""Keep explicit /tmp test fixtures and failed-state pruning in this job's scratch."""
import json
from pathlib import Path
import tempfile
import shutil

SCRATCH = Path('/private/tmp/sf-sbh-c1')
_mkdtemp = tempfile.mkdtemp
_rmtree = shutil.rmtree
_external = set()


def mkdtemp(suffix=None, prefix=None, dir=None):
    if dir is not None and str(dir) in ('/tmp', '/private/tmp'):
        dir = str(SCRATCH)
    if dir is not None and not Path(dir).resolve().is_relative_to(SCRATCH):
        prefix = 'sf-sbh-c1-' + (prefix or tempfile.gettempprefix())
    path = _mkdtemp(suffix=suffix, prefix=prefix, dir=dir)
    if not Path(path).resolve().is_relative_to(SCRATCH):
        _external.add(str(Path(path).resolve()))
    with (SCRATCH / 'temp-directories.jsonl').open('a') as record:
        record.write(json.dumps(str(path)) + '\n')
    return path


tempfile.mkdtemp = mkdtemp


def pytest_load_initial_conftests(early_config, parser, args):
    import tests.failed_keep
    tests.failed_keep.ROOT = SCRATCH / 'failed'


def rmtree(path, *args, **kwargs):
    resolved = str(Path(path).resolve())
    if resolved in _external:
        assert 'sf-sbh-c1' in resolved
        with (SCRATCH / 'external-deletions.jsonl').open('a') as record:
            record.write(json.dumps(resolved) + '\n')
    return _rmtree(path, *args, **kwargs)


shutil.rmtree = rmtree
