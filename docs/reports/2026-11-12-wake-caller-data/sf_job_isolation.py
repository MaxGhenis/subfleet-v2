"""Keep this job's failed-test evidence and compiler caches in its own scratch."""
from pathlib import Path
import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session):
    from tests import failed_keep
    failed_keep.ROOT = Path('/private/tmp/sf-wc-b1/failed-keep')
