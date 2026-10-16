"""A stand-in for `procs.Darwin`, C-5.5's in-process reads (D-CEN1), for tests that
describe the process table rather than read the host's.

Each process is a raw `KERN_PROCARGS2` buffer (`procargs`), and optionally the pid
of its responsible process and its unique id. Every read is recorded in `calls`.
"""

from __future__ import annotations

from collections.abc import Iterable

from subfleet import procs


def procargs(argv: Iterable[str | bytes], env: Iterable[str | bytes] = (), *,
             executable: str | bytes | None = None, padding: int = 3) -> bytes:
    """The buffer `sysctl` `KERN_PROCARGS2` returns: argc, the executable path, NUL
    padding, the argv strings, the environment strings, then what the kernel adds."""
    def raw(part):
        return part if isinstance(part, bytes) else part.encode()
    args = [raw(part) for part in argv]
    path = raw(executable) if executable is not None else b"/fixture/bin/" + (args[0] if args else b"x")
    body = path + b"\0" * (1 + padding) + b"".join(part + b"\0" for part in args)
    body += b"".join(raw(part) + b"\0" for part in env)
    body += b"\0executable_path=" + path + b"\0ptr_munge=\0"
    return len(args).to_bytes(4, "little") + body


def from_ps_rows(text: str) -> dict[int, bytes]:
    """`pid command [env...]` rows, as tests wrote `ps -axEww -o pid=,command=` output
    before D-CEN1, as buffers: each whitespace-separated word after the command is one
    environment string, which is how the old reader matched them."""
    found = {}
    for row in text.splitlines():
        words = row.split()
        if len(words) >= 2:
            found[int(words[0])] = procargs([words[1]], words[2:])
    return found


class FakeKernel:
    """`processes` maps a live, readable pid to its buffer, or to None for a read the
    kernel refuses. `others` are other users' pids (EPERM: listed, never readable),
    `exiting` pids have begun to exit, `responsible` maps a pid to its responsible
    process's pid (absent: its own), `unique` to its unique id (absent: the pid).
    `fail` names reads that raise `InspectionError`: "pids", or ("process" |
    "responsible", pid)."""

    readable = True

    def __init__(self, processes: dict[int, bytes | None] | None = None, *, others: Iterable[int] = (),
                 exiting: Iterable[int] = (), responsible: dict[int, int] | None = None,
                 unique: dict[int, int] | None = None, fail: Iterable = (), responsibility: bool = True,
                 gone_after_process: Iterable[int] = ()):
        self.processes = dict(processes or {})
        self.others = set(others)
        self.exiting = set(exiting)
        self.boss = dict(responsible or {})
        self.unique = dict(unique or {})
        self.fail = set(fail)
        self.responsibility = responsibility
        self.gone_after_process = set(gone_after_process)
        self.calls: list[tuple] = []

    def pids(self) -> frozenset[int]:
        self.calls.append(("pids",))
        if "pids" in self.fail:
            raise procs.InspectionError("process listing unavailable")
        return frozenset(self.processes) | frozenset(self.others)

    def process(self, pid: int) -> procs.KernelProcess | None:
        self.calls.append(("process", pid))
        if ("process", pid) in self.fail:
            raise procs.InspectionError(f"process inspection of pid {pid} unavailable")
        if pid not in self.processes or pid in self.others:
            return None
        if pid in self.gone_after_process and sum(1 for call in self.calls if call == ("process", pid)) > 1:
            return None
        return procs.KernelProcess(pid, pid in self.exiting, self.unique.get(pid, pid))

    def arguments(self, pid: int, buffer=None) -> bytes | None:
        self.calls.append(("arguments", pid))
        return self.processes.get(pid)

    def responsible(self, pid: int) -> int | None:
        self.calls.append(("responsible", pid))
        if ("responsible", pid) in self.fail or not self.responsibility:
            raise procs.InspectionError(f"responsibility of pid {pid} unavailable")
        if pid not in self.processes or pid in self.others:
            return None
        return self.boss.get(pid, pid)

    def scans(self) -> list[tuple]:
        """The reads only a deciding census makes: the listing and each buffer."""
        return [call for call in self.calls if call[0] in ("pids", "arguments", "responsible")]


class ScriptedMarkerKernel(FakeKernel):
    """Replay a test's scripted marker rows through the raw-buffer interface.

    The callback is always a fixture reader, never the host reader. Listing and
    late per-pid confirmation keep their original interleaving boundaries.
    """

    def __init__(self, read):
        super().__init__(responsibility=False)
        self.read = read
        self.argument_reads = {}

    def pids(self):
        self.processes = from_ps_rows(self.read(["/bin/ps", "-axEww", "-o", "pid=,command="]))
        self.argument_reads.clear()
        return super().pids()

    def arguments(self, pid, buffer=None):
        self.argument_reads[pid] = self.argument_reads.get(pid, 0) + 1
        if self.argument_reads[pid] == 1:
            return super().arguments(pid, buffer)
        command = self.read(["/bin/ps", "-p", str(pid), "-Eww", "-o", "command="], empty_ok=True)
        # The scalar command has no pid column; generic fixture readers may
        # still return a complete row.
        if command.split()[:1] != [str(pid)]:
            command = f"{pid} {command}"
        return from_ps_rows(command).get(pid, procargs(["gone"]))
