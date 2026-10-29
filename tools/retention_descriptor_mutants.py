"""Opt-in pytest plugin for the nine named D-WALK-FD1 mutations.

Run under the suite hold, for example::

    pytest -p tools.retention_descriptor_mutants --retention-descriptor-mutant=MH \
        tests/unit/test_retention_descriptor_review.py::test_descent_close_failure_owns_new_child

Only the imported module is mutated; production source stays unchanged.
M1/M2/MO/ML/MI/MP/MC repeat the build/review mutants. MH drops the two
handoff protections, and MD makes the shared cleanup drain stop on an error.
"""
from __future__ import annotations

import ast
from pathlib import Path
import subprocess


MUTANTS = ("M1", "M2", "MO", "ML", "MI", "MP", "MC", "MH", "MD")


def pytest_addoption(parser):
    parser.addoption("--retention-descriptor-mutant", choices=MUTANTS)


def _definition(source: str, name: str) -> tuple[int, int]:
    nodes = ast.parse(source).body
    for component in name.split("."):
        node = next(node for node in nodes if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                    and node.name == component)
        nodes = node.body
    return node.lineno - 1, node.end_lineno


def _replace_definition(source: str, name: str, original: str) -> str:
    lines, old_lines = source.splitlines(keepends=True), original.splitlines(keepends=True)
    start, end = _definition(source, name)
    old_start, old_end = _definition(original, name)
    lines[start:end] = old_lines[old_start:old_end]
    return "".join(lines)


def _replace_once(source: str, old: str, new: str) -> str:
    assert source.count(old) == 1, old
    return source.replace(old, new, 1)


def _at_revision(revision: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "show", f"{revision}:subfleet/retention_fs.py"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True,
    ).stdout


def pytest_configure(config):
    mutant = config.getoption("--retention-descriptor-mutant")
    if mutant is None:
        return
    from subfleet import retention_fs as rfs

    source = Path(rfs.__file__).read_text()
    if mutant == "M1":
        source = _replace_once(
            source,
            "                except BaseException:\n                    os.close(child)\n                    raise\n",
            "                except BaseException:\n                    raise\n",
        )
    elif mutant == "M2":
        source = _at_revision("bc108d3ad")
    elif mutant in ("MO", "ML", "MP", "MC"):
        name = {"MO": "open_dir", "ML": "_layout", "MP": "_open_path",
                "MC": "Reclaim._conflict_dir"}[mutant]
        source = _replace_definition(source, name, _at_revision("bc108d3ad"))
    elif mutant == "MI":
        source = _replace_once(
            source,
            "                try:\n                    opened.append(fd)\n"
            "                except BaseException:\n                    os.close(fd)\n                    raise\n",
            "                opened.append(fd)\n",
        )
    elif mutant == "MH":
        original = _at_revision("b1996dcf6bed")
        for name in ("_open_path", "Reclaim._conflict_dir"):
            source = _replace_definition(source, name, original)
    elif mutant == "MD":
        original = ("def _close_all(descriptors: Iterable[int]) -> None:\n"
                    "    for fd in descriptors:\n        os.close(fd)\n")
        source = _replace_definition(source, "_close_all", original)
    exec(compile(source, f"<retention descriptor mutant {mutant}>", "exec"), rfs.__dict__)
