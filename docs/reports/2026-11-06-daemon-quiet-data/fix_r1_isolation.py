"""Keep pytest's private homes and failure retention inside this job's scratch."""
from pathlib import Path
import tempfile

SCRATCH = Path('/private/tmp/sf-dq-f1')


def pytest_load_initial_conftests(early_config, parser, args):
    tempfile.tempdir = str(SCRATCH / 'tmp')
    from tests import failed_keep
    failed_keep.ROOT = SCRATCH / 'failed-keep'
