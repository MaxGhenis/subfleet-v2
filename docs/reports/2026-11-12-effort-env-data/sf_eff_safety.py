"""Scratch containment used only for this job's requested test runs."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

from sf_eff_processes import install

install()

SCRATCH = Path('/private/tmp/sf-eff-b1')
_original_mkdtemp = tempfile.mkdtemp


def _scratch_mkdtemp(suffix=None, prefix=None, dir=None):
    if dir is not None and os.fsdecode(dir).rstrip('/') in ('/tmp', '/private/tmp'):
        dir = bytes(SCRATCH) if isinstance(dir, bytes) else str(SCRATCH)
    return _original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)


tempfile.mkdtemp = _scratch_mkdtemp


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    from tests import failed_keep
    failed_keep.ROOT = SCRATCH / 'failed-tests'


def pytest_collection_modifyitems(session, config, items):
    # The e2e harness supplies its own sitecustomize and replaces PYTHONPATH.
    # Extend that existing observer after collection, before E2E builds it;
    # all original observers remain in the same order and retain their scope.
    module = sys.modules.get('tests.e2e.conftest')
    if module is not None:
        helper_directory = str(Path(__file__).resolve().parent)
        module.OBSERVERS += (
            '\nimport sys\n'
            f'sys.path.insert(0, {helper_directory!r})\n'
            'from sf_eff_processes import install as _sf_eff_install\n'
            '_sf_eff_install()\n'
        )
