"""Limit this job's test census to children of its still-running test runner.

This report-only harness changes no installed Subfleet code.  Unlike filtering
an all-host listing, proc_listchildpids asks the kernel only for one known
parent's children.  The tree is rebuilt on every read; no PID survives in an
allowlist after leaving the runner's tree.
"""
from __future__ import annotations

import ctypes
import functools
from importlib.machinery import PathFinder
import os
import sys

_installed = False


def install(owner_pid: int | None = None) -> None:
    """Install before tests/daemons run, retaining their explicit monkeypatches."""
    global _installed
    if _installed:
        return
    if owner_pid is None and "SF_EFF_OWNER_PID" not in os.environ:
        return
    owner = int(os.environ["SF_EFF_OWNER_PID"] if owner_pid is None else owner_pid)
    if owner <= 1:
        raise ValueError("SF_EFF_OWNER_PID must name this job's test runner")

    # Loading a system library neither enumerates processes nor reads config.
    # If unavailable, the wrappers below fail closed instead of falling back
    # to Darwin.pids()'s all-host proc_listpids call.
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        children = library.proc_listchildpids
        children.restype = ctypes.c_int
        children.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
    except (AttributeError, OSError):
        children = None

    def owned_pids(procs) -> frozenset[int]:
        if children is None:
            raise procs.InspectionError("job-scoped child process listing unavailable")
        found = {owner}
        pending = [owner]
        while pending:
            parent = pending.pop()
            for capacity in (256, 1024, 4096, 16384):
                buffer = (ctypes.c_int * capacity)()
                count = children(parent, buffer, ctypes.sizeof(buffer))
                if count < 0:
                    raise procs.InspectionError("job-scoped child process listing failed")
                if count < ctypes.sizeof(buffer):
                    for pid in buffer[:count // ctypes.sizeof(ctypes.c_int)]:
                        if pid > 1 and pid not in found:
                            found.add(pid)
                            pending.append(pid)
                    break
            else:
                raise procs.InspectionError("job-scoped child process listing truncated")
        return frozenset(found)

    def patch(procs):
        read = getattr(procs, "_read", None)
        if read is not None and not getattr(read, "_sf_eff_b1_scoped", False):
            @functools.wraps(read)
            def scoped_read(argv, *, empty_ok=False):
                if (len(argv) >= 3 and argv[0] == "/bin/ps" and argv[1] == "-axo"):
                    pids = owned_pids(procs)
                    if not pids:
                        return ""
                    return read([argv[0], "-p", ",".join(map(str, sorted(pids))), "-o", *argv[2:]],
                                empty_ok=True)
                # Preserve direct identity reads, sysctl boot identity and test doubles.
                return read(argv, empty_ok=empty_ok)

            scoped_read._sf_eff_b1_scoped = True
            procs._read = scoped_read

        darwin = getattr(procs, "Darwin", None)
        if darwin is not None:
            def scoped_kernel_pids(self):
                return owned_pids(procs)

            darwin.pids = scoped_kernel_pids

    class ScopedLoader:
        def __init__(self, original):
            self.original = original

        def __getattr__(self, name):
            return getattr(self.original, name)

        def create_module(self, spec):
            create = getattr(self.original, "create_module", None)
            return create(spec) if create is not None else None

        def exec_module(self, module):
            self.original.exec_module(module)
            patch(module)

    class ScopedFinder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "subfleet.procs":
                return None
            # Delegate using the package's eventual __path__. In particular,
            # an archive comparison must select its old package before this
            # harness touches procs; importing Subfleet from a .pth would pin
            # the current checkout prematurely.
            spec = PathFinder.find_spec(fullname, path, target)
            if spec is not None and spec.loader is not None:
                spec.loader = ScopedLoader(spec.loader)
            return spec

    present = sys.modules.get("subfleet.procs")
    if present is not None:
        patch(present)
    sys.meta_path.insert(0, ScopedFinder())
    _installed = True
