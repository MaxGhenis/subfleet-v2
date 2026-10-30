"""Review #271 r4: mutants of `relay.child_exited` and its bindings beyond fix r4's, each
against its named killer.

Run with pytest from the repository root. As in `fix_r3_mutants.py`, no production file
is edited: each mutant replaces one function, compiled from its source with one change,
or one module attribute, for the length of one test. A mutant counts as killed only when
its killer fails by assertion (or `pytest.fail`), and each killer passes unmutated.
"""

from __future__ import annotations

import ctypes
import errno
import inspect
import os
import textwrap

import pytest

from subfleet import relay as relay_module
from tests.fake import test_turn_relay_exit_peek as peek
from tests.fake import test_turn_relay_exit_r4 as r4

IN_USE = "os" if peek.HAS_OS_WAITID else "libc"
MUTANTS = {
    # The libc peek reads any siginfo as an exit, the zeroed one of a running child too.
    "libc-reads-any-siginfo-as-exit": ("_waitid_libc", (
        "return _Exited(info.si_pid, info.si_status) if info.si_pid else None",
        "return _Exited(info.si_pid, info.si_status)")),
    # The peek asks for no exits: waitid refuses (EINVAL), so no exit is ever read.
    "flags-without-wexited": ("_PEEK_FLAGS", os.WNOHANG | os.WNOWAIT),
    # A pid of 0 is peeked (P_PID 0 is no child of anyone's).
    "pid-zero-peeked": ("child_exited", ("if type(pid) is not int or pid <= 0:", "if type(pid) is not int:")),
    # Every failed peek is logged, not the first.
    "failure-logged-every-time": ("child_exited", ("if not _peek_failure_logged:", "if True:")),
    # The libc call's failure is ignored: ECHILD leaves the zeroed siginfo, read as running.
    "libc-failure-ignored": ("_waitid_libc", (
        "if _libc_waitid(os.P_PID, pid, ctypes.byref(info), _PEEK_FLAGS) != 0:",
        "if _libc_waitid(os.P_PID, pid, ctypes.byref(info), _PEEK_FLAGS) != 0 and False:")),
    # The libc binding without use_errno: get_errno() no longer reads the call's errno.
    "libc-errno-not-kept": ("_libc_waitid", None),
    # Popen's poll() dropped: returncode, then the peek.
    "poll-dropped": ("child_exited", (
        'if getattr(child, "returncode", None) is not None or child.poll() is not None:',
        'if getattr(child, "returncode", None) is not None:')),
}
KILLERS = {
    "libc-reads-any-siginfo-as-exit": lambda tmp_path, patch, capfd:
        peek.test_live_children_never_read_as_exited(patch, "libc", "lock-free"),
    "flags-without-wexited": lambda tmp_path, patch, capfd:
        peek.test_a_peek_never_changes_the_status_the_guardian_records(patch, IN_USE),
    "pid-zero-peeked": lambda tmp_path, patch, capfd:
        peek.test_a_child_without_a_real_pid_is_never_peeked(patch, {"returncode": None, "pid": 0}),
    "failure-logged-every-time": lambda tmp_path, patch, capfd:
        peek.test_another_peek_failure_proves_nothing_and_is_logged_once(patch, capfd, errno.EIO),
    "libc-failure-ignored": lambda tmp_path, patch, capfd:
        peek.test_a_child_the_guardian_reaped_reads_as_exited(patch, "libc"),
    "libc-errno-not-kept": lambda tmp_path, patch, capfd:
        peek.test_a_child_the_guardian_reaped_reads_as_exited(patch, "libc"),
    "poll-dropped": lambda tmp_path, patch, capfd:
        r4.test_the_relay_s_poll_reaps_and_stores_the_status_when_the_wait_lock_is_free(tmp_path),
}


def apply(patch, name):
    target, change = MUTANTS[name]
    if target == "_PEEK_FLAGS":
        patch.setattr(relay_module, target, change)
        return
    if target == "_libc_waitid":
        unkept = ctypes.CDLL(None, use_errno=False).waitid
        unkept.restype, unkept.argtypes = relay_module._libc_waitid.restype, relay_module._libc_waitid.argtypes
        patch.setattr(relay_module, target, unkept)
        ctypes.set_errno(0)                 # nothing stale for get_errno() to find
        return
    before, after = change
    source = textwrap.dedent(inspect.getsource(getattr(relay_module, target)))
    assert source.count(before) == 1, (name, before)
    scope = {}
    exec(compile(source.replace(before, after), f"<review r4 mutant {name}>", "exec"), vars(relay_module), scope)
    patch.setattr(relay_module, target, scope[target])


@pytest.mark.parametrize("name", list(MUTANTS))
def test_the_named_killer_kills_the_mutant(tmp_path, capfd, name):
    with pytest.MonkeyPatch.context() as patch:
        apply(patch, name)
        with pytest.raises((AssertionError, pytest.fail.Exception)):
            KILLERS[name](tmp_path, patch, capfd)


@pytest.mark.parametrize("name", list(MUTANTS))
def test_the_named_killer_passes_unmutated(tmp_path, capfd, name):
    with pytest.MonkeyPatch.context() as patch:
        KILLERS[name](tmp_path, patch, capfd)
