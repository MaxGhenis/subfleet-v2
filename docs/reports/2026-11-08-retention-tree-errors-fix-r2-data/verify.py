"""Run the W5 iterator mutant in this process without changing production files.

Usage: python verify.py W5 -p sf_rte_safe --basetemp <scratch>/pytest-w5
       tests/unit/test_rte_tmpdir_acl_iterator.py
"""
from __future__ import annotations

import importlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path.cwd()))
mode, *args = sys.argv[1:]
assert mode == "W5", mode
module = importlib.import_module("subfleet.retention_fs")
source = Path(module.__file__).read_text()
before = """                checked(_acl_delete_entry(acl, entry))
                # Deletion may move later entries; restart the native iterator.
                position = _ACL_FIRST_ENTRY
"""
after = """                checked(_acl_delete_entry(acl, entry))
"""
assert source.count(before) == 1, "W5 must replace only the post-deletion restart"
exec(compile(source.replace(before, after, 1), module.__file__, "exec"), module.__dict__)

import pytest

raise SystemExit(pytest.main(args))
