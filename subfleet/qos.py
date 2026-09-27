"""C-5.1: agent work, and the repository code the daemon runs for it, at the `utility` QoS.

The daemon runs at the default QoS (launchd `ProcessType` `Interactive`), so nothing it
starts may inherit that when the work is an agent's or a repository's own code. A QoS
clamp is the one setting every process the clamped one starts inherits and none can
raise. Measured on 2026-09-27: a thread's own QoS (`pthread_set_qos_class_self_np`)
and `nice` reach no child that execs (docs/reports/2026-09-27-daemon-qos.md).

- A provider is spawned with `posix_spawn` and `posix_spawnattr_set_qos_class_np`
  (`spawn`). A failed exec comes back as posix_spawn's own errno, and is raised as the
  OSError Popen would raise. No text is read from the provider's streams, and an
  executable with no `#!` line is refused (ENOEXEC) as Popen refuses it; `posix_spawnp`
  and `execvp` would run it under /bin/sh.
- A `git` command that can run repository code (a hook, a filter) is started under
  `taskpolicy -c utility` (`repository_argv`), so its whole tree is clamped too.

The clamp caps QoS classes and timeshare priority. A thread can still ask for the
realtime policy (`THREAD_TIME_CONSTRAINT_POLICY`) or be boosted by priority donation,
as it can under launchd `Standard`'s clamp.
"""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import sys
import threading

TASKPOLICY = "/usr/sbin/taskpolicy"
PROVIDER_QOS = "utility"
PROVIDER_QOS_ENV = "SUBFLEET_PROVIDER_QOS"    # `inherit`: nothing is clamped
QOS_CLASS_UTILITY = 0x11
POSIX_SPAWN_SETSIGDEF = 0x0004
POSIX_SPAWN_CLOEXEC_DEFAULT = 0x4000          # every descriptor not named is closed, as close_fds=True
# Popen's restore_signals: what CPython ignores at start is default again in the child.
RESTORED_SIGNALS = tuple(getattr(signal, name) for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ") if hasattr(signal, name))


def _libc():
    if sys.platform != "darwin":
        return None
    try:
        lib = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    if not hasattr(lib, "posix_spawnattr_set_qos_class_np"):
        return None
    pointer = ctypes.POINTER(ctypes.c_void_p)
    for name, args in (("posix_spawnattr_init", [pointer]), ("posix_spawnattr_destroy", [pointer]),
                       ("posix_spawnattr_setflags", [pointer, ctypes.c_short]),
                       ("posix_spawnattr_setsigdefault", [pointer, ctypes.POINTER(ctypes.c_uint32)]),
                       ("posix_spawnattr_set_qos_class_np", [pointer, ctypes.c_uint]),
                       ("posix_spawn_file_actions_init", [pointer]), ("posix_spawn_file_actions_destroy", [pointer]),
                       ("posix_spawn_file_actions_adddup2", [pointer, ctypes.c_int, ctypes.c_int]),
                       ("posix_spawn_file_actions_addinherit_np", [pointer, ctypes.c_int])):
        function = getattr(lib, name)
        function.argtypes, function.restype = args, ctypes.c_int
    chdir = getattr(lib, "posix_spawn_file_actions_addchdir", None) or lib.posix_spawn_file_actions_addchdir_np
    chdir.argtypes, chdir.restype = [pointer, ctypes.c_char_p], ctypes.c_int
    lib.subfleet_addchdir = chdir
    lib.posix_spawn.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_char_p, pointer, pointer,
                                ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_char_p)]
    lib.posix_spawn.restype = ctypes.c_int
    return lib


_LIBC = _libc()


def provider_qos() -> str | None:
    """The QoS the provider is clamped to, or None when it inherits the guardian's.

    Only `inherit` opts out. A host without `posix_spawnattr_set_qos_class_np` (not
    macOS) inherits too."""
    if os.environ.get(PROVIDER_QOS_ENV, PROVIDER_QOS) == "inherit" or _LIBC is None:
        return None
    return PROVIDER_QOS


def repository_argv(argv: list[str]) -> list[str]:
    """argv for a command that can run repository code, clamped as a provider is."""
    if os.environ.get(PROVIDER_QOS_ENV, PROVIDER_QOS) == "inherit" or not os.access(TASKPOLICY, os.X_OK):
        return list(argv)
    return [TASKPOLICY, "-c", PROVIDER_QOS, *argv]


def unclamped(argv: list[str] | tuple) -> list:
    """argv without `repository_argv`'s prefix (to name the command it runs)."""
    argv = list(argv)
    return argv[3:] if argv[:3] == [TASKPOLICY, "-c", PROVIDER_QOS] else argv


class Process:
    """What the guardian and its relay use of a Popen: pid, returncode, poll, wait, send_signal.

    One lock guards reaping, as Popen's does: a poll that finds a wait under way says
    the child still runs rather than racing it for the status."""

    def __init__(self, pid: int):
        self.pid, self.returncode = pid, None
        self._lock = threading.Lock()

    def _reap(self, flags: int) -> None:
        pid, status = os.waitpid(self.pid, flags)
        if pid == self.pid:
            self.returncode = os.waitstatus_to_exitcode(status)

    def poll(self) -> int | None:
        if self.returncode is None and self._lock.acquire(False):
            try:
                if self.returncode is None:
                    self._reap(os.WNOHANG)
            except ChildProcessError:
                pass
            finally:
                self._lock.release()
        return self.returncode

    def wait(self) -> int:
        with self._lock:
            while self.returncode is None:
                self._reap(0)
        return self.returncode

    def send_signal(self, sig: int) -> None:
        if self.poll() is None:
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                pass


def _candidates(name: str) -> list[str]:
    """Popen's executable list: the name itself when it has a directory part, else each
    PATH entry joined to it (relative ones resolve in the child's cwd, as Popen's do)."""
    if os.path.dirname(name):
        return [name]
    return [os.path.join(directory, name) for directory in os.get_exec_path()]


def _check(rc: int, what: str) -> None:
    if rc:
        raise OSError(rc, f"{what}: {os.strerror(rc)}")


def spawn(argv: list[str], *, cwd: str, stdin: int, stdout: int, stderr: int, qos: str = PROVIDER_QOS) -> Process:
    """Start argv in cwd with descriptors 0-2 from stdin/stdout/stderr and every other one
    closed, clamped to `qos`, in this process's session and group; raise the OSError
    `subprocess.Popen(argv, cwd=cwd, ...)` would when it cannot be started."""
    if _LIBC is None or qos != PROVIDER_QOS:
        raise OSError(errno.ENOSYS, "no QoS clamp on this host")
    # Popen names the directory when its chdir fails; posix_spawn's errno would not say which.
    os.stat(cwd)
    for ok, number in ((os.path.isdir(cwd), errno.ENOTDIR), (os.access(cwd, os.X_OK), errno.EACCES)):
        if not ok:
            raise OSError(number, os.strerror(number), cwd)
    lib = _LIBC
    attr, actions = ctypes.c_void_p(), ctypes.c_void_p()
    check = _check
    check(lib.posix_spawnattr_init(ctypes.byref(attr)), "posix_spawnattr_init")
    try:
        check(lib.posix_spawn_file_actions_init(ctypes.byref(actions)), "posix_spawn_file_actions_init")
        try:
            check(lib.posix_spawnattr_set_qos_class_np(ctypes.byref(attr), QOS_CLASS_UTILITY), "set_qos_class")
            mask = ctypes.c_uint32(0)
            for signum in RESTORED_SIGNALS:
                mask.value |= 1 << (signum - 1)
            check(lib.posix_spawnattr_setsigdefault(ctypes.byref(attr), ctypes.byref(mask)), "setsigdefault")
            check(lib.posix_spawnattr_setflags(ctypes.byref(attr), POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_CLOEXEC_DEFAULT),
                  "setflags")
            check(lib.subfleet_addchdir(ctypes.byref(actions), os.fsencode(cwd)), "addchdir")
            for source, target in ((stdin, 0), (stdout, 1), (stderr, 2)):
                if source == target:
                    check(lib.posix_spawn_file_actions_addinherit_np(ctypes.byref(actions), target), "addinherit")
                else:
                    check(lib.posix_spawn_file_actions_adddup2(ctypes.byref(actions), source, target), "adddup2")
            args = [os.fsencode(part) for part in argv]
            c_argv = (ctypes.c_char_p * (len(args) + 1))(*args, None)
            env = [key + b"=" + value for key, value in os.environb.items()]
            c_env = (ctypes.c_char_p * (len(env) + 1))(*env, None)
            pid = ctypes.c_int()
            # _posixsubprocess's PATH search: report the first error that is not
            # ENOENT/ENOTDIR, else the last, always naming argv[0].
            first = last = 0
            for executable in _candidates(argv[0]):
                rc = lib.posix_spawn(ctypes.byref(pid), os.fsencode(executable), ctypes.byref(actions),
                                     ctypes.byref(attr), c_argv, c_env)
                if rc == 0:
                    return Process(pid.value)
                last = rc
                if rc not in (errno.ENOENT, errno.ENOTDIR) and not first:
                    first = rc
            number = first or last or errno.ENOENT
            raise OSError(number, os.strerror(number), argv[0])
        finally:
            lib.posix_spawn_file_actions_destroy(ctypes.byref(actions))
    finally:
        lib.posix_spawnattr_destroy(ctypes.byref(attr))
