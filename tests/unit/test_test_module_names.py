"""Every test module has a basename of its own.

Only `tests/` is a package; its test directories are not, so pytest imports each
test module by its basename. Two modules with one basename stop a whole-suite run
at collection ("import file mismatch": nothing runs), while a run of either file
alone passes. That hid tests/e2e/test_conversation_titles.py beside
tests/unit/test_conversation_titles.py from every file-by-file run.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]


def test_every_test_module_has_a_basename_of_its_own():
    paths = {path for pattern in ("test_*.py", "*_test.py")      # pytest's default python_files
             for path in TESTS.rglob(pattern) if "__pycache__" not in path.parts}
    modules: dict[str, list[str]] = defaultdict(list)
    for path in sorted(paths):
        modules[path.name].append(path.relative_to(TESTS).as_posix())
    assert {name: found for name, found in modules.items() if len(found) > 1} == {}
