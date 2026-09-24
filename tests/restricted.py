"""C-5.5 host facts the containment tests depend on.

xnu's sysctl_procargsx (KERN_PROCARGS2, which `ps -E` reads) withholds the
environment of a CS_RESTRICT process unless System Integrity Protection permits
unrestricted DTrace or the kernel is a development build. So whether the marker
can see `/bin/sleep` is a property of the host, and a test that expects it
hidden asks this module first.
"""

from __future__ import annotations

import ctypes
import functools
import os
import signal
import subprocess
import sys
import time
import uuid

from subfleet import procs

CS_RESTRICT = 0x800   # xnu osfmk/kern/cs_blobs.h


def cs_restricted(pid: int) -> bool:
    """The live CS_RESTRICT bit, read with csops(CS_OPS_STATUS) as any same-user process may."""
    flags = ctypes.c_uint32()
    if ctypes.CDLL(None, use_errno=True).csops(pid, 0, ctypes.byref(flags), ctypes.sizeof(flags)):
        raise OSError(ctypes.get_errno(), f"csops({pid})")
    return bool(flags.value & CS_RESTRICT)


@functools.lru_cache(maxsize=None)
def kernel_hides_restricted_environments() -> bool:
    """Whether the marker census misses a CS_RESTRICT process that carries the marker on this host.

    A Python process with the same environment is the control: if the census
    cannot see it either, the host denies process inspection and the answer
    is unknown (RuntimeError).
    """
    marker = f"restricted-probe-{uuid.uuid4().hex}/a1"
    root = f"/restricted-probe/{os.getpid()}"
    env = {**os.environ, "SUBFLEET_ATTEMPT": marker, "SUBFLEET_ROOT": root}
    started = [subprocess.Popen(command, env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
               for command in (["/bin/sleep", "30"], [sys.executable, "-c", "import time; time.sleep(30)"])]
    sleeper, control = started
    try:
        until = time.monotonic() + 5
        census = procs.containment(None, None, None, marker, root=root)
        while control.pid not in census.marker_pids and time.monotonic() < until:
            time.sleep(.05)
            census = procs.containment(None, None, None, marker, root=root)
        if control.pid not in census.marker_pids:
            raise RuntimeError("the marker census does not see an unrestricted process on this host")
        return cs_restricted(sleeper.pid) and sleeper.pid not in census.marker_pids
    finally:
        for process in started:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
