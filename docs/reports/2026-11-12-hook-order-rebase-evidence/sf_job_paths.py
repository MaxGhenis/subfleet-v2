"""Job-only pytest isolation; no repository test code is changed."""
import os
from pathlib import Path
import shutil
import tempfile

SCRATCH = Path('/private/tmp/sf-hor-b1')
_original_mkdtemp = tempfile.mkdtemp
_original_rmtree = shutil.rmtree
_external = set()


def job_mkdtemp(suffix=None, prefix=None, dir=None):
    if dir is not None and Path(dir).resolve() == Path('/private/tmp'):
        dir = str(SCRATCH)
    if dir is not None and not Path(dir).resolve().is_relative_to(SCRATCH):
        prefix = 'sf-hor-b1-' + (prefix or '')
    result = _original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)
    resolved = Path(result).resolve()
    if not resolved.is_relative_to(SCRATCH):
        _external.add(str(resolved))
        with (SCRATCH / ('outside-' + str(os.getpid()) + '.txt')).open('a') as stream:
            stream.write(str(resolved) + '\n')
    return result


def job_rmtree(path, *args, **kwargs):
    resolved = str(Path(path).resolve())
    if resolved in _external:
        assert 'sf-hor-b1' in resolved
    return _original_rmtree(path, *args, **kwargs)


tempfile.mkdtemp = job_mkdtemp
shutil.rmtree = job_rmtree
from tests import failed_keep
failed_keep.ROOT = SCRATCH / 'failed-keep'
