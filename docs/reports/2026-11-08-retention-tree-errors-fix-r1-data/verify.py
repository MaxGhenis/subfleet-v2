"""Process-local baseline and guard mutations; never alter production files."""
import ast
import importlib
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path.cwd()))
mode, *args = sys.argv[1:]
plugins = []

def old_source(path):
    return subprocess.check_output(["git", "show", f"3e0b0c36b:{path}"], text=True)

def replace_module(name, source):
    module = importlib.import_module(name)
    exec(compile(source, module.__file__, "exec"), module.__dict__)

if mode == "baseline-acl":
    for name in ("retention_fs", "attempt_tmp"):
        replace_module(f"subfleet.{name}", old_source(f"subfleet/{name}.py"))
elif mode == "baseline-bundle":
    replace_module("subfleet.retention_archive", old_source("subfleet/retention_archive.py"))
elif mode == "baseline-patterns":
    tree = ast.parse(old_source("tests/unit/test_attempt_tmpdir_mutations.py"))
    value = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "MUTATIONS" for target in node.targets))
    patterns = ast.literal_eval(value)
    class OldPatterns:
        def pytest_collection_modifyitems(self, items):
            changed = set()
            for item in items:
                if item.module.__file__.endswith("test_attempt_tmpdir_mutations.py"):
                    item.module.MUTATIONS = patterns
                    changed.add(item.module.__name__)
            assert changed, "the baseline table must reach the collected module"
    plugins.append(OldPatterns())
else:
    replacements = {
        "R1": ("attempt_tmp", "found is not None and found.st_uid == os.getuid()", "found is not None"),
        "R1-parent": ("attempt_tmp", "if os.fstat(parent).st_uid == os.getuid():", "if True:"),
        "R2": ("retention_archive", "age = _wall_now() - found.st_mtime", "age = abs(_wall_now() - found.st_mtime)"),
        "R3": ("attempt_tmp", "_delete(os.rmdir, parent, tomb, parent_inside=False)", "_delete(os.rmdir, parent, tomb, parent_inside=True)"),
        "walk-stat-raw": ("retention_fs", '                # Listing needs read, this lstat needs search on the parent.\n                raise TreeError("unreadable", f"{path}: {exc.strerror}") from None', '                raise'),
    }
    name, before, after = replacements[mode]
    source = Path(f"subfleet/{name}.py").read_text()
    assert source.count(before) == 1, (mode, source.count(before))
    replace_module(f"subfleet.{name}", source.replace(before, after, 1))

import pytest
raise SystemExit(pytest.main(args, plugins=plugins))
