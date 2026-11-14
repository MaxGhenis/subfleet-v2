"""Keep explicit /tmp test fixtures and failed-state pruning in this job's scratch."""
import json
from pathlib import Path
import tempfile

SCRATCH = Path('/private/tmp/sf-sbh-b1')
_mkdtemp = tempfile.mkdtemp


def mkdtemp(suffix=None, prefix=None, dir=None):
    if dir is not None and str(dir) in ('/tmp', '/private/tmp'):
        dir = str(SCRATCH)
    if dir is not None and not Path(dir).resolve().is_relative_to(SCRATCH):
        prefix = 'sf-sbh-b1-' + (prefix or tempfile.gettempprefix())
    path = _mkdtemp(suffix=suffix, prefix=prefix, dir=dir)
    with (SCRATCH / 'temp-directories.jsonl').open('a') as record:
        record.write(json.dumps(str(path)) + '\n')
    return path


tempfile.mkdtemp = mkdtemp


def pytest_load_initial_conftests(early_config, parser, args):
    import tests.failed_keep
    tests.failed_keep.ROOT = SCRATCH / 'failed'
