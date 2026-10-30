"""Opt-in pytest plugin for the seventeen named D-WALK-FD1 mutations.

Run under the suite hold, for example::

    pytest -p tools.retention_descriptor_mutants --retention-descriptor-mutant=MH \
        tests/unit/test_retention_descriptor_review.py::test_descent_close_failure_owns_new_child

Only the imported module is mutated; production source stays unchanged.
M1/M2/MO/ML/MI/MP/MC repeat the build/review mutants. MH drops the two
handoff protections, and MD makes the shared cleanup drain stop on an error.
MR retries a released parent, MA ignores registration ownership, ME replaces
an active exception, and MX discards a cancellation raised by cleanup.
MAH restores ambient exception inference, MSR the plain size root close, MPO
the unprotected pop, and MGF implicit walk finalization in record_complete.
"""
from __future__ import annotations

import ast
from pathlib import Path
import subprocess


MUTANTS = ("M1", "M2", "MO", "ML", "MI", "MP", "MC", "MH", "MD", "MR", "MA", "ME", "MX", "MAH", "MSR", "MPO", "MGF")


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


def _unwrap_owner(source: str, definition: str, resource: str) -> str:
    """Drop only the specified production ownership context."""
    lines = source.splitlines(keepends=True)
    start, end = _definition(source, definition)
    node = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.With)
                and start < node.lineno <= end
                and any(isinstance(item.context_expr, ast.Call)
                        and ast.get_source_segment(source, item.context_expr.args[0]) == f"({resource},)"
                        for item in node.items))
    lines[node.lineno - 1:node.end_lineno] = [line[4:] for line in lines[node.lineno:node.end_lineno]]
    return "".join(lines)


def _plain_root_close(source: str) -> str:
    lines = source.splitlines(keepends=True)
    start, end = _definition(source, "AttemptTemp.size")
    node = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.With)
                and start < node.lineno <= end
                and ast.get_source_segment(source, node.items[0].context_expr) == "rfs._cleanup((fd,))")
    indent = " " * node.col_offset
    lines[node.lineno - 1:node.end_lineno] = ([indent + "try:\n"]
        + lines[node.lineno:node.end_lineno]
        + [indent + "finally:\n", indent + "    os.close(fd)\n"])
    return "".join(lines)


def pytest_configure(config):
    mutant = config.getoption("--retention-descriptor-mutant")
    if mutant is None:
        return
    from subfleet import retention_fs as rfs

    source = Path(rfs.__file__).read_text()
    if mutant == "M1":
        source = _replace_once(
            source,
            "                except BaseException as exc:\n"
            "                    if not stack or stack[-1][0] != child:\n"
            "                        _close_all((child,), exc)\n                    raise\n",
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
            "                    try:\n                        opened.append(fd)\n"
            "                    except BaseException as exc:\n"
            "                        if not opened or opened[-1] != fd:\n"
            "                            _close_all((fd,), exc)\n                        raise\n",
            "                    opened.append(fd)\n",
        )
    elif mutant == "MH":
        original = _at_revision("b1996dcf6bed")
        for name in ("_open_path", "Reclaim._conflict_dir"):
            source = _replace_definition(source, name, original)
    elif mutant == "MD":
        original = ("def _close_all(descriptors: Iterable[Any], pending: BaseException | None, *,\n"
                    "               close: Callable[[Any], Any] | None = None) -> None:\n"
                    "    if close is None:\n        close = os.close\n"
                    "    for fd in descriptors:\n        close(fd)\n")
        source = _replace_definition(source, "_close_all", original)
    elif mutant == "MR":
        source = _replace_once(
            source,
            "            if parent != dir_fd:\n                os.close(parent)\n",
            "            if parent != dir_fd:\n                try:\n                    os.close(parent)\n"
            "                except BaseException:\n                    os.close(parent)\n                    raise\n",
        )
    elif mutant == "MA":
        for guard, descriptor, indent in (("not stack or stack[-1][0] != child", "child", " " * 20),
                                          ("not opened or opened[-1] != fd", "fd", " " * 24)):
            source = _replace_once(
                source, f"{indent}if {guard}:\n{indent}    _close_all(({descriptor},), exc)\n",
                f"{indent}_close_all(({descriptor},), exc)\n",
            )
    elif mutant == "ME":
        source = _replace_once(
            source,
            "    if pending is None:\n        if first is not None:\n            raise first\n"
            "    elif isinstance(pending, (Exception, GeneratorExit)) and cancelled is not None:\n"
            "        raise cancelled\n",
            "    if first is not None:\n        raise first\n",
        )
    elif mutant == "MX":
        source = _replace_once(
            source,
            "    elif isinstance(pending, (Exception, GeneratorExit)) and cancelled is not None:\n"
            "        raise cancelled\n", "",
        )
    elif mutant == "MAH":
        source = _replace_once(
            source, "    if close is None:\n        close = os.close\n",
            "    pending = __import__('sys').exception()\n"
            "    if close is None:\n        close = os.close\n",
        )
    elif mutant == "MPO":
        source = _replace_once(
            source,
            "                close_attempted = False\n"
            "                try:\n"
            "                    stack.pop()\n"
            "                    if fd != root_fd:\n"
            "                        # Transfer from the stack to one close attempt. Python\n"
            "                        # cannot make this marker and the call below atomic.\n"
            "                        close_attempted = True\n"
            "                        os.close(fd)\n"
            "                except BaseException as exc:\n"
            "                    if not close_attempted and fd != root_fd and (not stack or stack[-1][0] != fd):\n"
            "                        _close_all((fd,), exc)\n"
            "                    raise\n",
            "                stack.pop()\n"
            "                if fd != root_fd:\n                    os.close(fd)\n",
        )
    elif mutant == "MGF":
        source = _unwrap_owner(source, "record_complete", "iterator")
    exec(compile(source, f"<retention descriptor mutant {mutant}>", "exec"), rfs.__dict__)
    if mutant == "MSR":
        from subfleet import attempt_tmp
        source = _plain_root_close(Path(attempt_tmp.__file__).read_text())
        # Replace just this production method, preserving the imported module.
        start, end = _definition(source, "AttemptTemp.size")
        import textwrap
        namespace = dict(attempt_tmp.__dict__)
        exec(compile(textwrap.dedent("".join(source.splitlines(keepends=True)[start:end])),
                     "<retention descriptor mutant MSR>", "exec"), namespace)
        attempt_tmp.AttemptTemp.size = namespace["size"]
