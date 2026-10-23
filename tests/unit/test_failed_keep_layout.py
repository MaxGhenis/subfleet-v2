"""Only tests/failed_keep.py names the folder a failed test's state root is kept in, every harness
that keeps roots asks it for theirs, and CI uploads that folder (D-KR1, #238 review r1 finding 5).

The scan reads code, not prose: docstrings and comments may name the folder. This file holds no
copy of it either; it searches for `failed_keep.ROOT`'s own name.
"""
from __future__ import annotations

import ast
import functools
from pathlib import Path
import re
from typing import NamedTuple

from tests import failed_keep

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"
HELPER = TESTS / "failed_keep.py"
WORKFLOW = REPO / ".github/workflows/tests.yml"
NEEDLE = failed_keep.ROOT.name
#: The harnesses that keep a failed test's state root today: the fake-daemon and end-to-end
#: fixtures and the app core's live test. Discovery below must still find each of them.
HARNESSES = {"tests/fake/conftest.py", "tests/e2e/conftest.py", "tests/frontend/test_core_live.py"}


class Source(NamedTuple):
    """What the scan needs from one Python file, read in one walk of its tree."""
    literals: list[str]           # string and bytes constants naming the folder, f-string parts included
    reads_report: bool            # reads a test's phase report: `rep_call`, or `"rep_" + when`
    calls: frozenset[str]         # every called name, bare or as an attribute
    imports_helper: bool          # `from tests.failed_keep import failed_keep_dir`


@functools.cache
def sources() -> dict[str, Source]:
    """Every Python file under tests/, by its path from the repository (parsed once for both scans)."""
    return {path.relative_to(REPO).as_posix(): read(path)
            for path in sorted(TESTS.rglob("*.py")) if "__pycache__" not in path.parts}


def read(path: Path) -> Source:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings, literals, calls = set(), [], set()
    reads_report = imports_helper = False
    for node in ast.walk(tree):          # breadth first: a docstring's owner comes before the docstring
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body \
                and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
            docstrings.add(id(node.body[0].value))
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            text = node.value if isinstance(node.value, str) else node.value.decode("latin-1")
            if NEEDLE in text and id(node) not in docstrings:
                literals.append(f"{path.relative_to(REPO)}:{node.lineno}: {text!r}")
            reads_report |= text.startswith("rep_")
        elif isinstance(node, ast.Attribute):
            reads_report |= node.attr.startswith("rep_")
        elif isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
            calls.add(node.func.id if isinstance(node.func, ast.Name) else node.func.attr)
        elif isinstance(node, ast.ImportFrom) and node.module == "tests.failed_keep":
            imports_helper |= any(alias.name == "failed_keep_dir" for alias in node.names)
    return Source(literals, reads_report, frozenset(calls), imports_helper)


def test_only_the_helper_names_the_kept_roots_folder():
    helper = HELPER.relative_to(REPO).as_posix()
    stray = [hit for name, source in sources().items() if name != helper for hit in source.literals]
    # Scripts, probes and data under tests/ are not Python: no mention at all, comments included.
    others = sorted(path for path in TESTS.rglob("*") if path.is_file() and path.suffix not in (".py", ".pyc", ".md"))
    stray += [str(path.relative_to(REPO)) for path in others if NEEDLE.encode() in path.read_bytes()]
    assert stray == [], f"build the kept-root path with tests.failed_keep.failed_keep_dir, not a literal: {stray}"
    assert sources()[helper].literals, "the scan found no literal even in the helper; it is not reading code"


def test_every_harness_that_keeps_roots_asks_the_helper_for_the_path():
    # A harness that keeps roots reads a test's phase report and copies a tree.
    keepers = {name for name, source in sources().items() if source.reads_report and "copytree" in source.calls}
    assert HARNESSES <= keepers, f"discovery lost a known harness: {sorted(HARNESSES - keepers)}"
    missing = sorted(name for name in keepers
                     if "failed_keep_dir" not in sources()[name].calls or not sources()[name].imports_helper)
    assert missing == [], f"these keep a failed test's root without tests.failed_keep.failed_keep_dir: {missing}"


def upload_steps(text: str) -> list[dict]:
    """The workflow's `actions/upload-artifact` steps, each as its `with:` keys (a flat block of scalars)."""
    steps, current = [], None
    for line in text.splitlines():
        item = re.match(r"^(\s*)- (.*)$", line)
        if item and (current is None or len(item.group(1)) <= current["indent"]):
            current = {"indent": len(item.group(1)), "lines": []}
            steps.append(current)
            line = item.group(1) + "  " + item.group(2)
        if current is not None:
            current["lines"].append(line)
    uploads = []
    for step in steps:
        keys, within, depth = {}, {}, None
        for line in step["lines"]:
            key = re.match(r"^(\s*)([\w-]+):\s*(.*?)\s*$", line)
            if not key:
                continue
            indent, name = len(key.group(1)), key.group(2)
            value = re.sub(r"\s+#.*$", "", key.group(3)).strip("\"'")     # a trailing comment, quotes
            if depth is not None and indent > depth:
                within[name] = value
                continue
            depth = indent if name == "with" else None
            keys[name] = value
        if "actions/upload-artifact@" in keys.get("uses", ""):
            uploads.append(within)
    return uploads


def test_ci_uploads_the_folder_the_helper_keeps_roots_in():
    uploads = upload_steps(WORKFLOW.read_text(encoding="utf-8"))
    kept = [step for step in uploads if step.get("name", "").startswith(NEEDLE + "-")]
    assert len(kept) == 1, f"one upload of the kept roots, found {kept} among {uploads}"
    assert kept[0].get("path") == str(failed_keep.ROOT)


def test_the_workflow_reader_reads_upload_steps():
    """The reader above, on a workflow shaped like ours (it must not see a `run` step's keys)."""
    text = """\
    steps:
      - name: Run
        run: pytest
      - name: Upload outcomes
        if: always()
        uses: actions/upload-artifact@abc # v7
        with:
          name: outcomes-x
          path: /tmp/a
      - name: Upload kept
        uses: actions/upload-artifact@abc
        with:
          name: kept-x
          path: "/tmp/b" # quoted, with a comment
          retention-days: 14
      - uses: actions/checkout@abc
    """
    assert upload_steps(text) == [{"name": "outcomes-x", "path": "/tmp/a"},
                                  {"name": "kept-x", "path": "/tmp/b", "retention-days": "14"}]
