"""Keep this round's test allocations and retained failures in its scratch."""
from pathlib import Path
import os
import tempfile

import pytest

SCRATCH = Path(os.environ.get('SF_EFF_TEMP_ROOT') or os.environ['SF_EFF_SCRATCH'])
assert 'sf-eff-fix2' in str(SCRATCH)
tempfile.tempdir = str(SCRATCH)
_mkdtemp = tempfile.mkdtemp


def mkdtemp(suffix=None, prefix=None, dir=None):
    if dir is not None and str(dir).rstrip('/') in ('/tmp', '/private/tmp'):
        dir = str(SCRATCH)
    return _mkdtemp(suffix=suffix, prefix=prefix, dir=dir)


tempfile.mkdtemp = mkdtemp


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    from tests import failed_keep
    failed_keep.ROOT = SCRATCH / 'failed-tests'
