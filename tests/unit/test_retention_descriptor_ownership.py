"""Bounded checks of explicit cleanup precedence and walk ownership transfers."""
from __future__ import annotations

import ast
import dis
import errno
import os
from pathlib import Path

from hypothesis import HealthCheck, example, given, settings, strategies as st
import pytest

from subfleet import retention_fs as rfs
from tests.unit.test_retention_descriptor_review import raise_at
from tests.unit.test_retention_walk_descriptors import DEEP, tree, unchanged_descriptors


class Cancelled(BaseException):
    pass


def _failure(kind: str) -> BaseException | None:
    factories = {
        "success": lambda: None,
        "oserror": lambda: OSError(errno.EIO, "owned close"),
        "exception": lambda: RuntimeError("operation"),
        "generator": lambda: GeneratorExit("operation"),
        "keyboard": lambda: KeyboardInterrupt("cancel"),
        "system": lambda: SystemExit("cancel"),
        "custom": lambda: Cancelled("cancel"),
    }
    return factories[kind]()


_IN_FLIGHT = (
    "none", "handled_exception", "handled_keyboard", "exception", "generator",
    "keyboard", "system", "custom",
)
_CLOSE_OUTCOMES = ("success", "oserror", "keyboard", "system", "custom")


@settings(max_examples=150, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@example(in_flight="handled_exception", outcomes=["oserror", "keyboard", "success"])
@example(in_flight="handled_keyboard", outcomes=["keyboard", "oserror", "success"])
@example(in_flight="none", outcomes=["oserror", "keyboard", "success"])
@example(in_flight="exception", outcomes=["oserror", "system", "custom"])
@example(in_flight="generator", outcomes=["oserror", "custom", "success"])
@example(in_flight="keyboard", outcomes=["oserror", "system", "success"])
@example(in_flight="system", outcomes=["custom", "oserror", "success"])
@example(in_flight="custom", outcomes=["keyboard", "oserror", "success"])
@given(in_flight=st.sampled_from(_IN_FLIGHT),
       outcomes=st.lists(st.sampled_from(_CLOSE_OUTCOMES), min_size=1, max_size=5))
def test_cleanup_property_attempts_each_owned_close_once_and_preserves_precedence(
        tmp_path, in_flight, outcomes):
    """Handled caller exceptions are ambient; only this operation's exit competes."""
    ambient = (_failure("exception") if in_flight == "handled_exception" else
               _failure("keyboard") if in_flight == "handled_keyboard" else None)
    primary = None if in_flight in ("none", "handled_exception", "handled_keyboard") else _failure(in_flight)
    failures = [_failure(outcome) for outcome in outcomes]
    descriptors, attempted, still_open = [], [], set()

    def close(fd):
        attempted.append(fd)
        assert fd in still_open, "an owned close was retried"
        os.close(fd)
        still_open.remove(fd)
        failure = failures[descriptors.index(fd)]
        if failure is not None:
            raise failure

    def operation():
        with rfs._cleanup(descriptors, close=close):
            if primary is not None:
                raise primary

    try:
        # All examples use real descriptors of one file made by this test.
        for _ in outcomes:
            fd = os.open(tmp_path / "owned", os.O_CREAT | os.O_RDWR, 0o600)
            descriptors.append(fd)
            still_open.add(fd)
        caught = None
        try:
            if ambient is None:
                operation()
            else:
                try:
                    raise ambient
                except BaseException:
                    operation()
        except BaseException as exc:
            caught = exc

        first = next((failure for failure in failures if failure is not None), None)
        cancellation = next((failure for failure in failures
                             if failure is not None and not isinstance(failure, (Exception, GeneratorExit))), None)
        expected = primary
        if primary is None:
            expected = first
        elif isinstance(primary, (Exception, GeneratorExit)) and cancellation is not None:
            expected = cancellation
        assert attempted == descriptors
        assert not still_open
        assert caught is expected
        if caught is not None:
            expected_context = ambient if primary is None else primary if caught is not primary else None
            assert caught.__context__ is expected_context
            assert caught.__cause__ is None
    finally:
        # A failing assertion or a broken cleanup implementation must not leak.
        for fd in still_open:
            os.close(fd)


def _ambient_exception_calls(source: str) -> list[int]:
    """Recognize direct calls and imported or assigned aliases of sys getters."""
    parsed = ast.parse(source)
    sys_aliases = {"sys"}
    getter_aliases = set()
    nodes = list(ast.walk(parsed))
    for node in nodes:
        if isinstance(node, ast.Import):
            sys_aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "sys")
        elif isinstance(node, ast.ImportFrom) and node.module == "sys" and not node.level:
            getter_aliases.update(alias.asname or alias.name for alias in node.names
                                  if alias.name in ("exception", "exc_info"))

    def is_getter(node):
        return ((isinstance(node, ast.Name) and node.id in getter_aliases)
                or (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in sys_aliases and node.attr in ("exception", "exc_info")))

    # Resolve alias chains without depending on the order ast.walk visits them.
    changed = True
    while changed:
        previous = (set(sys_aliases), set(getter_aliases))
        for node in nodes:
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            else:
                continue
            names = {target.id for target in targets if isinstance(target, ast.Name)}
            if isinstance(value, ast.Name) and value.id in sys_aliases:
                sys_aliases.update(names)
            if is_getter(value):
                getter_aliases.update(names)
        changed = previous != (sys_aliases, getter_aliases)
    return [node.lineno for node in nodes if isinstance(node, ast.Call) and is_getter(node.func)]


@pytest.mark.parametrize("filename", ["retention_fs.py", "attempt_tmp.py"])
def test_cleanup_sources_do_not_read_ambient_exception_state(filename):
    source = Path(rfs.__file__).with_name(filename)
    assert not _ambient_exception_calls(source.read_text()), source


@pytest.mark.parametrize("source", [
    "import sys\nsys.exception()",
    "import sys as runtime\nruntime.exc_info()",
    "from sys import exception as active\nactive()",
    "import sys as runtime\nmodule = runtime\ngetter = module.exc_info\naliased = getter\naliased()",
])
def test_ambient_exception_source_guard_recognizes_aliases(source):
    assert _ambient_exception_calls(source)


def _pop_transfer_offsets():
    instructions = list(dis.get_instructions(rfs.walk))
    pop = next(i for i, instruction in enumerate(instructions)
               if instruction.argval == "pop" and instruction.opname in ("LOAD_ATTR", "LOAD_METHOD"))
    pop_call = next(i for i in range(pop, len(instructions)) if instructions[i].opname.startswith("CALL"))
    marker = next(i for i in range(pop_call + 1, len(instructions))
                  if instructions[i].opname == "STORE_FAST" and instructions[i].argval == "close_attempted")
    close_call = next(i for i in range(marker + 1, len(instructions))
                      if instructions[i].opname.startswith("CALL"))
    return [("before-marker", instruction.offset) for instruction in instructions[pop_call + 1:marker + 1]] + [
        ("after-close", instructions[close_call + 1].offset),
    ]


@pytest.mark.parametrize("window,offset", _pop_transfer_offsets())
def test_pop_transfer_injected_instruction_closes_each_child_once(monkeypatch, window, offset):
    """Cover pop return through the marker and close return, not marker-to-call.

    Pure Python cannot atomically set the marker and start os.close. This test
    excludes instructions after the marker executes and before that close call.
    """
    injected = KeyboardInterrupt(f"{window} at {offset}")
    with tree(DEEP) as (_, root_fd), unchanged_descriptors(root_fd):
        original_open, original_close = rfs.open_dir, os.close
        opened, attempted = [], []

        def open_dir(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def close(fd):
            attempted.append(fd)
            original_close(fd)

        with monkeypatch.context() as patch:
            patch.setattr(rfs, "open_dir", open_dir)
            patch.setattr(os, "close", close)
            iterator = rfs.walk(root_fd)
            try:
                for _ in range(4):
                    next(iterator)
                with raise_at(rfs.walk.__code__, offset, injected) as fired:
                    with pytest.raises(KeyboardInterrupt) as caught:
                        list(iterator)
                assert fired and caught.value is injected
                assert len(opened) == 2
                assert sorted(attempted) == sorted(opened)
                assert len(attempted) == len(set(attempted))
                assert root_fd not in attempted
                os.fstat(root_fd)
            finally:
                iterator.close()
