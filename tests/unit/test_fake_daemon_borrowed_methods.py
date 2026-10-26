"""The conversation tests' stand-in daemons borrow real `Daemon` methods, and every
name those methods read must resolve on the stand-in (D-KILL1; C-30.3, C-24.7).

`tests/unit/test_conversation_handoff.py`'s `Daemon` and
`tests/unit/test_conversation_service.py`'s `FakeDaemon` run real main-store
cancellation with no pools or control loop: each takes `_request_cancel` and the
methods it calls from the real `Daemon` as class attributes. A borrowed method that
starts calling one more method fails only in the test that reaches the call. Round 4
of #240 made `_request_cancel` call `_scan_observers` and `_release_ending`, which
neither fake had, and 39 tests in five files failed on CI alone (run 40251224112),
with an AttributeError or with a turn left queued.

This reads each borrowed method's source, collects every `self.<name>` it reads,
follows the names the fake also borrows, and requires each name it reaches to
resolve on the fake: a class attribute, or an attribute its `__init__` assigns. A
name on a branch the fakes' tests never take may stay unborrowed, with its reason,
in `EXEMPT`; an exemption no longer needed fails as well.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from subfleet.daemon import Daemon as RealDaemon
from tests.unit.test_conversation_handoff import Daemon as HandoffDaemon
from tests.unit.test_conversation_service import FakeDaemon

FAKES = {"handoff.Daemon": HandoffDaemon, "service.FakeDaemon": FakeDaemon}

_NO_WORKTREE_LEASE = (
    "read by `_scan_observers`, which `_request_cancel` calls only for a job that holds a "
    "`worktree:` lease with no active attempt: admission takes that lease, these fakes have "
    "none, and no test of theirs writes one")
_NO_FAMILY_WALL = (
    "read by `_request_cancel` only for a family wall cancel (`family=True` with a daemon "
    "`max_wall_s` request): the conversation service never passes `family`")
_TURN_NOTICE = (
    "read by `_notice` only for a job that is not a turn, since it returns first for a turn "
    "(C-26.12): the conversation service cancels turn jobs only")
_NO_EARLIER_ATTEMPT = (
    "read by `_earlier_attempt` only for a job cancelled with no active attempt after an "
    "earlier one ended: these fakes retry nothing, and their tests give an ended attempt only "
    "to a job that is terminal, which `_request_cancel` leaves alone, or is never cancelled")

#: (fake, name): why a name a borrowed method reads may be missing from that fake.
EXEMPT = {
    (HandoffDaemon, "_worktree_observers"): _NO_WORKTREE_LEASE,
    (FakeDaemon, "_worktree_observers"): _NO_WORKTREE_LEASE,
    (HandoffDaemon, "_wall_exited_descendants"): _NO_FAMILY_WALL,
    (FakeDaemon, "_wall_exited_descendants"): _NO_FAMILY_WALL,
    (HandoffDaemon, "_moved_on"): _TURN_NOTICE,
    (FakeDaemon, "_moved_on"): _TURN_NOTICE,
    (HandoffDaemon, "_withdraw_pin_notice"): _TURN_NOTICE,
    (FakeDaemon, "_withdraw_pin_notice"): _TURN_NOTICE,
    (HandoffDaemon, "_salvage_summary"): _NO_EARLIER_ATTEMPT,
    (FakeDaemon, "_salvage_summary"): _NO_EARLIER_ATTEMPT,
    (HandoffDaemon, "_held_line"): _NO_EARLIER_ATTEMPT,
    (FakeDaemon, "_held_line"): _NO_EARLIER_ATTEMPT,
}


def _function(value):
    """The function a class attribute binds: a static or class method's own."""
    return value.__func__ if isinstance(value, (staticmethod, classmethod)) else value


def _real_functions() -> dict:
    """Every function the real `Daemon` defines or inherits, to the name it has there."""
    found = {}
    for klass in reversed(RealDaemon.__mro__[:-1]):
        for name, value in vars(klass).items():
            if inspect.isfunction(_function(value)):
                found[_function(value)] = name
    return found


def _borrowed(fake: type) -> dict:
    """The fake's class attributes that are the real `Daemon`'s functions, by name."""
    real = _real_functions()
    return {name: value for name, value in vars(fake).items() if _function(value) in real}


def _reads(function) -> set[str]:
    """Every `self.<name>` a method reads; a static method has no `self` and reads none."""
    node = ast.parse(textwrap.dedent(inspect.getsource(function))).body[0]
    params = node.args.posonlyargs + node.args.args
    if not params or params[0].arg != "self":
        return set()
    updated = {id(n.target) for n in ast.walk(node) if isinstance(n, ast.AugAssign)}
    return {n.attr for n in ast.walk(node)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self"
            and (isinstance(n.ctx, (ast.Load, ast.Del)) or id(n) in updated)}


def _assigned(fake: type) -> set[str]:
    """Every `self.<name>` the fake's own `__init__` assigns."""
    if "__init__" not in vars(fake):
        return set()
    node = ast.parse(textwrap.dedent(inspect.getsource(vars(fake)["__init__"]))).body[0]
    return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "self" and isinstance(n.ctx, ast.Store)}


def _unresolved(fake: type) -> dict[str, list[str]]:
    """Each name the fake's borrowed methods reach that it lacks, to the borrowed methods reading it."""
    borrowed, assigned = _borrowed(fake), _assigned(fake)
    missing: dict[str, set[str]] = {}
    todo, seen = sorted(borrowed), set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for read in _reads(_function(borrowed[name])):
            if read in borrowed:
                todo.append(read)
            elif not (hasattr(fake, read) or read in assigned):
                missing.setdefault(read, set()).add(name)
    return {name: sorted(by) for name, by in sorted(missing.items())}


@pytest.mark.parametrize("fake", FAKES.values(), ids=FAKES.keys())
def test_every_name_a_borrowed_method_reads_resolves_on_its_fake(fake):
    """D-KILL1 (C-30.3, C-24.7): the fake runs the real cancel path, so every method
    that path reaches through `self` is on the fake, borrowed or its own, unless
    `EXEMPT` says why the fakes' tests never reach it."""
    assert "_request_cancel" in _borrowed(fake)        # the reading below has a root
    missing = {name: by for name, by in _unresolved(fake).items() if (fake, name) not in EXEMPT}
    detail = "; ".join(f"{name} (read by {', '.join(by)})" for name, by in missing.items())
    assert missing == {}, (f"{fake.__module__}.{fake.__qualname__} lacks what its borrowed methods read: "
                           f"{detail}. Borrow each from the real Daemon, or exempt it with its reason")


@pytest.mark.parametrize("fake", FAKES.values(), ids=FAKES.keys())
def test_every_exemption_is_still_needed(fake):
    """D-KILL1 (C-30.3, C-24.7): an exemption names a read the fake lacks; one whose
    name the fake now has, or that no borrowed method reaches, is gone from `EXEMPT`."""
    assert {owner for owner, _ in EXEMPT} <= set(FAKES.values())
    assert all(reason.strip() for reason in EXEMPT.values())
    reached = _unresolved(fake)
    stale = sorted(name for owner, name in EXEMPT if owner is fake and name not in reached)
    assert stale == [], f"{fake.__module__}.{fake.__qualname__}: exemptions no longer needed: {', '.join(stale)}"


@pytest.mark.parametrize("fake", FAKES.values(), ids=FAKES.keys())
def test_a_borrowed_method_keeps_its_binding(fake):
    """D-KILL1 (C-30.3, C-24.7): a static method borrowed bare would take the fake as
    its first argument, which no reading of its source shows (it has no `self`), and
    fail only on the branch that calls it: `_cwd_notice`, for one, runs only when a
    cancelled job's worktree keeps an observer."""
    real = _real_functions()
    wrong = sorted(name for name, value in _borrowed(fake).items()
                   if type(value) is not type(inspect.getattr_static(RealDaemon, real[_function(value)])))
    assert wrong == [], (f"{fake.__module__}.{fake.__qualname__}: borrowed with another binding than "
                         f"Daemon's: {', '.join(wrong)}")
