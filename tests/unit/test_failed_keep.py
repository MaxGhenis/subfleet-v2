"""A failed test's kept state root is under /tmp/sf-failed (CI uploads it), one folder per process."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.failed_keep import ROOT, failed_keep_dir

REPO = Path(__file__).resolve().parents[2]


def test_the_kept_root_is_under_the_uploaded_folder_and_this_process():
    keep = failed_keep_dir("test_x[param/a b]")
    assert keep.parent == ROOT / str(os.getpid())
    assert keep.name == "test_x_param_a_b_"
    # That ROOT is the folder CI uploads: test_failed_keep_layout.py reads the workflow.


def test_two_processes_keep_the_same_test_in_different_folders():
    code = "from tests.failed_keep import failed_keep_dir; print(failed_keep_dir('test_same'))"
    other = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True)
    assert Path(other.stdout.strip()) != failed_keep_dir("test_same")
    assert Path(other.stdout.strip()).name == failed_keep_dir("test_same").name
