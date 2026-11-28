"""Abort before fixture setup if mixed-file selection escapes the audited nodes."""
from pathlib import Path
import pytest

ROOT = Path('/private/tmp/sf-a2-fix1')
ALLOWED = frozenset((ROOT / 'importers-safe-mixed-nodes.txt').read_text().splitlines())


def pytest_collection_modifyitems(config, items):
    bases = {item.nodeid.split('[', 1)[0] for item in items}
    if bases != ALLOWED:
        raise pytest.UsageError('Mixed-file collection differs from the exact audited sixteen selectors')
    for item in items:
        if {'worlds', 'queued_admission_template'} & set(item.fixturenames):
            raise pytest.UsageError('Mixed-file selection requested a forbidden push-fixture dependency')
