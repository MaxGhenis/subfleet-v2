"""#271 fix r3, hub decision 1, NOT BUILT: the kernel exit peek, confined to this harness.

CI runs Python 3.12 and 3.14 on macOS 15. CPython 3.12 leaves `os.waitid` out of its
build on Apple platforms (on this host 3.12.14 reports HAVE_WAITID=1 and has no
`os.waitid`; 3.13.15 and 3.14.7 have it), so the decided helper cannot run on CI's 3.12
job. The brief says to stop and report there rather than fall back, so production code
is unchanged. This harness is what the hub needs to decide:

- `child_exited`, the decided helper, with the peek either through `os.waitid` or,
  where that is missing, through libc's own `waitid` by ctypes (the same system call;
  `BINDING` names the one this interpreter uses);
- the controls: a running child, and one that closed its stdin, read as not exited,
  whether or not the guardian's wait holds Popen's lock;
- the never-reap check: after peeks under a held wait lock, Popen's wait still returns
  the child's real status;
- review r3's wait-lock regression, green with the helper at RelayServer's three exit
  reads (where `os.waitid` exists: the regression itself proves exit with it);
- the brief's two mutants: the helper without the peek, and a peek that reaps.

Run with pytest from the repository root. Each case swaps the three RelayServer methods
for the length of one test; no production file is edited. Every child is started here
and reaped here.
"""

from contextlib import contextmanager
import ctypes
import inspect
import os
import subprocess
import sys
import textwrap
import time

import pytest

from subfleet import relay as relay_module
from tests.fake import test_turn_relay_exit_r3 as r3
from tests.fake import test_turn_relay_exit_recovery as recovery


class _SigInfo(ctypes.Structure):
    """Darwin's `siginfo_t` (sys/signal.h): 104 bytes on arm64 and x86_64."""
    _fields_ = [("si_signo", ctypes.c_int), ("si_errno", ctypes.c_int), ("si_code", ctypes.c_int),
                ("si_pid", ctypes.c_int), ("si_uid", ctypes.c_uint), ("si_status", ctypes.c_int),
                ("si_addr", ctypes.c_void_p), ("si_value", ctypes.c_void_p), ("si_band", ctypes.c_long),
                ("_pad", ctypes.c_ulong * 7)]


def _libc_peek():
    waitid = ctypes.CDLL(None, use_errno=True).waitid
    waitid.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.POINTER(_SigInfo), ctypes.c_int]
    waitid.restype = ctypes.c_int

    def peek(pid: int, flags: int) -> int:
        info = _SigInfo()
        if waitid(os.P_PID, pid, ctypes.byref(info), flags) != 0:
            failure = ctypes.get_errno()
            raise OSError(failure, os.strerror(failure))      # ECHILD is ChildProcessError
        return info.si_pid
    return peek


def _os_peek(pid: int, flags: int) -> int:
    found = os.waitid(os.P_PID, pid, flags)
    return 0 if found is None else found.si_pid


BINDING = "os.waitid" if hasattr(os, "waitid") else "libc waitid (ctypes)"
PEEK = _os_peek if hasattr(os, "waitid") else _libc_peek()
NOT_REAPING = os.WEXITED | os.WNOHANG | os.WNOWAIT
REAPING = os.WEXITED | os.WNOHANG                    # the mutant: WNOWAIT dropped


def child_exited(child, *, flags: int = NOT_REAPING, peek: bool = True) -> bool:
    """Whether the guardian's child has exited: Popen's own evidence, else the kernel's,
    without reaping. ChildProcessError: the guardian's wait reaped it. Any other OSError
    is no evidence."""
    if getattr(child, "returncode", None) is not None or child.poll() is not None:
        return True
    pid = getattr(child, "pid", None)
    if not peek or not isinstance(pid, int):
        return False
    try:
        return PEEK(pid, flags) != 0
    except ChildProcessError:
        return True
    except OSError:
        return False


SITES = (   # (method, before, after): RelayServer's three reads of poll() as exit evidence
    ("status", '"running" if child.poll() is None else "exited"', '"running" if not _exited(child) else "exited"'),
    ("_refusal", "exited = child is not None and child.poll() is not None",
     "exited = child is not None and _exited(child)"),
    ("apply", "if child is not None and child.poll() is None:", "if child is not None and not _exited(child):"),
)


def patch_relay(patch, exited):
    for method, before, after in SITES:
        source = textwrap.dedent(inspect.getsource(getattr(relay_module.RelayServer, method)))
        assert source.count(before) == 1, (method, before)
        scope = {**vars(relay_module), "_exited": exited}
        exec(compile(source.replace(before, after), f"<decision 1 candidate {method}>", "exec"), scope)
        patch.setattr(relay_module.RelayServer, method, scope[method])


def _start(code: str, **kwargs) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", code], **kwargs)


def _until(check, what: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, what
        time.sleep(0.01)


@contextmanager
def _wait_lock_held(child):
    """Hold the lock the guardian's blocking child.wait() holds (`guardian.py`)."""
    child._waitpid_lock.acquire()
    try:
        yield
    finally:
        child._waitpid_lock.release()


def test_the_binding_in_use():
    print("BINDING", sys.version.split()[0], BINDING)
    assert hasattr(os, "WNOWAIT") and hasattr(os, "P_PID") and hasattr(os, "WEXITED")


@pytest.mark.parametrize("held", [False, True], ids=["lock-free", "wait-lock-held"])
def test_live_children_never_read_as_exited(held):
    running = _start("import time; time.sleep(60)")
    read_end, write_end = os.pipe()
    closed = _start("import os, time; os.close(0); time.sleep(60)", stdin=read_end)
    os.close(read_end)
    try:
        time.sleep(0.2)                                  # the second child has closed its stdin
        for child in (running, closed):
            with _wait_lock_held(child) if held else _nothing():
                assert not child_exited(child), child.args
    finally:
        os.close(write_end)
        for child in (running, closed):
            child.kill()
            child.wait(10)


@contextmanager
def _nothing():
    yield


@pytest.mark.parametrize("flags", [NOT_REAPING, REAPING], ids=["candidate", "reaping-peek"])
def test_a_peek_never_changes_the_status_the_guardian_records(flags):
    child = _start("raise SystemExit(3)")
    try:
        with _wait_lock_held(child):
            _until(lambda: PEEK(child.pid, NOT_REAPING) != 0, "the child never exited")
            assert child.poll() is None                  # Popen's cache cannot see it: its lock is held
            assert child_exited(child, flags=flags) and child_exited(child, flags=flags)
        status = child.wait(10)
    finally:
        if child.returncode is None:
            child.kill()
            child.wait(10)
    if flags == REAPING:
        assert status == 0                               # killed: Popen records a status it never saw, not 3
    else:
        assert status == 3


def test_a_child_the_guardian_reaped_reads_as_exited():
    child = _start("raise SystemExit(4)")
    with _wait_lock_held(child):
        _until(lambda: PEEK(child.pid, NOT_REAPING) != 0, "the child never exited")
        assert os.waitpid(child.pid, 0)[0] == child.pid  # the reap the guardian's wait makes
        assert child.poll() is None and child_exited(child)
    assert child.poll() == 0                             # Popen's ECHILD fallback, once its lock is free
    assert child_exited(child, peek=False)


@pytest.mark.skipif(not hasattr(os, "waitid"), reason="review r3's regression proves exit with os.waitid")
@pytest.mark.parametrize("peek", [True, False], ids=["candidate", "peek-removed"])
def test_the_wait_lock_race_settles_as_the_providers_end(tmp_path, peek):
    with pytest.MonkeyPatch.context() as patch:
        patch_relay(patch, lambda child: child_exited(child, peek=peek))
        with contextmanager(recovery.recovery.__wrapped__)(tmp_path, patch) as r:
            if peek:
                r3.test_a_real_exited_fake_is_not_a_relay_failure_while_the_guardian_wait_holds_its_lock(r, patch)
            else:
                with pytest.raises(AssertionError, match="relay-failed"):
                    r3.test_a_real_exited_fake_is_not_a_relay_failure_while_the_guardian_wait_holds_its_lock(
                        r, patch)
