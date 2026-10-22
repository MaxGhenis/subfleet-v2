"""Where a failed test's state root is kept, to be read after the run: under /tmp/sf-failed,
which CI uploads as an artifact.

Each test process keeps its own under /tmp/sf-failed/<pid>/. Two suites can run at once on
one host (the shared suite lock has two slots), and a test that failed in both would
otherwise have one run's rmtree and copytree remove or collide with the other's evidence.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path("/tmp/sf-failed")


def failed_keep_dir(test_name: str) -> Path:
    """The folder a failed test's state root is copied to: one per process and test name.

    Keyed by `request.node.name`, not the full node id, so two modules' tests with the same name share a folder
    within one process (one such pair exists today, and the fake one keeps nothing)."""
    return ROOT / str(os.getpid()) / re.sub(r"[^A-Za-z0-9_.-]", "_", test_name)
