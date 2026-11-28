"""Run one retention bundle mutant in memory with the existing killer tests."""

import inspect
from pathlib import Path
import sys


sys.path.insert(0, str(Path.cwd()))
from subfleet import retention_archive
import pytest


mode, *args = sys.argv[1:]
module_path = Path(retention_archive.__file__)
source = module_path.read_text()
if mode == "2":
    helper = inspect.getsource(retention_archive._Builder._clear_bundle_leftovers)
    lock_handling = '        lock = temporary.with_name(temporary.name + ".lock")\n'
    before = helper[helper.index(lock_handling):]
    after = "        temporary.unlink(missing_ok=True)\n"
    assert before.endswith(after)
    description = "_clear_bundle_leftovers only unlinks commits.bundle.tmp"
else:
    before = "stale = not directory and age >= self.BUNDLE_LOCK_STALE_S"
    after = {
        "7": "stale = True",
        "7b": "stale = not directory and age > self.BUNDLE_LOCK_STALE_S",
    }[mode]
    description = after

assert source.count(before) == 1
replacement = source.replace(before, after, 1)
exec(compile(replacement, str(module_path), "exec"), retention_archive.__dict__)
print(f"Mutant {mode} loaded in memory: {description}", flush=True)
raise SystemExit(pytest.main(args))
