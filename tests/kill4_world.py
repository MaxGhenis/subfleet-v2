"""D-KILL4: a simulated process world for the kill protocol and the census.

The daemon's census (`procs.containment`), its kill protocol and its
finalization run unchanged; only the host is simulated. A `World` holds
processes with a parent, a group, a state, a birth, markers (visible or hidden,
as macOS 26 hides an Apple platform binary's environment), a cwd and a uid. It
serves `procs.snapshot`, `procs.kernel` (a `FakeKernel` rebuilt from it on each
read) and the two signalling calls, and it applies each signal as a kernel
would: TERM ends a running process that does not ignore it and stays pending on
a stopped one until CONT, KILL ends any process, CONT resumes. A driver respawns
a marked child whenever it has none. Every signal is recorded, so a test can
check what was signalled, in what order, and that nothing else was.

No real process is inspected or signalled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import signal

from subfleet import procs
from tests.fake_kernel import FakeKernel, procargs

BOOT = "boot"
UID = os.geteuid()


def birth(n: int) -> str:
    """A distinct ps lstart for the n-th birth."""
    return f"Sat Sep  5 {10 + n // 3600 % 12:02d}:{n // 60 % 60:02d}:{n % 60:02d} 2026"


@dataclass
class Proc:
    pid: int
    ppid: int
    pgid: int
    start: str
    marked: bool = False           # carries SUBFLEET_ATTEMPT / SUBFLEET_ROOT of the attempt
    hidden: bool = False           # its environment is invisible (an Apple platform binary)
    ignores_term: bool = False
    stopped: bool = False
    pending_term: bool = False
    cwd: str | None = None
    uid: int = UID
    respawns: bool = False         # a driver loop: always has one marked child
    exits_childless: bool = False  # a guardian: exits once its provider has ended
    alive: bool = True
    ended_by: int | None = None


@dataclass
class World:
    attempt_id: str
    root: str
    procs: dict[int, Proc] = field(default_factory=dict)
    signals: list[tuple[str, int, int]] = field(default_factory=list)   # ("pid"|"group", pid|pgid, sig)
    next_pid: int = 9000
    births: int = 1000
    unsignallable: set[int] = field(default_factory=set)
    respawned: list[int] = field(default_factory=list)
    #: A driver respawns on every n-th table only, so some tables fall between children.
    respawn_every: int = 1
    tables: int = 0

    def add(self, pid: int, ppid: int, pgid: int | None = None, *, start: str | None = None, **options) -> Proc:
        self.births += 1
        proc = Proc(pid, ppid, pgid if pgid is not None else pid, start or birth(self.births), **options)
        self.procs[pid] = proc
        return proc

    def live(self) -> dict[int, Proc]:
        return {pid: proc for pid, proc in self.procs.items() if proc.alive}

    def identity(self, pid: int) -> procs.ProcessIdentity:
        return procs.ProcessIdentity(pid, BOOT, self.procs[pid].start)

    def _end(self, proc: Proc, sig: int) -> None:
        proc.alive, proc.ended_by = False, sig
        for child in self.procs.values():
            if child.alive and child.ppid == proc.pid:
                child.ppid = 1                          # reparented to launchd

    def _respawn(self) -> None:
        for guardian in list(self.procs.values()):
            if guardian.alive and guardian.exits_childless and not guardian.stopped and not any(
                    child.alive and child.ppid == guardian.pid for child in self.procs.values()):
                self._end(guardian, 0)
        self.tables += 1
        for driver in list(self.procs.values()):
            if self.tables % self.respawn_every:
                break
            if driver.alive and driver.respawns and not driver.stopped and not any(
                    child.alive and child.ppid == driver.pid for child in self.procs.values()):
                self.next_pid += 1
                self.add(self.next_pid, driver.pid, driver.pgid, marked=True, cwd=driver.cwd)
                self.respawned.append(self.next_pid)

    # The host reads the daemon's census makes.
    def snapshot(self) -> procs.ProcessTable:
        self._respawn()
        rows = {pid: (proc.ppid, proc.pgid, "T" if proc.stopped else "S", proc.start)
                for pid, proc in self.live().items()}
        return procs.ProcessTable(rows, BOOT)

    def kernel(self) -> FakeKernel:
        markers = [f"SUBFLEET_ATTEMPT={self.attempt_id}", f"SUBFLEET_ROOT={self.root}"]
        live = self.live()
        return FakeKernel({pid: procargs(["proc"], markers if proc.marked and not proc.hidden else [])
                           for pid, proc in live.items()},
                          others=[pid for pid, proc in live.items() if proc.uid != UID],   # EPERM
                          starts={pid: proc.start for pid, proc in live.items()},
                          uids={pid: proc.uid for pid, proc in live.items()},
                          cwds={pid: proc.cwd for pid, proc in live.items() if proc.cwd})

    # The signalling calls (C-5.3's identity check included).
    def signal_process(self, recorded: procs.ProcessIdentity, sig, *, before_signal=None) -> bool:
        proc = self.procs.get(recorded.pid)
        if (proc is None or not proc.alive or proc.start != recorded.proc_start
                or recorded.pid in self.unsignallable or recorded.pid <= 1 or proc.uid != UID):
            return False                              # gone, reborn, refused, or EPERM
        if before_signal is not None:
            before_signal()
        self.signals.append(("pid", recorded.pid, int(sig)))
        self._apply(proc, int(sig))
        return True

    def signal_group(self, pgid: int, sig, *, boot_id: str, proc_start: str, before_signal=None) -> bool:
        leader = self.procs.get(pgid)
        if leader is None or not leader.alive or leader.start != proc_start:
            return False
        if before_signal is not None:
            before_signal()
        self.signals.append(("group", pgid, int(sig)))
        for proc in list(self.procs.values()):
            if proc.alive and proc.pgid == pgid and proc.pid not in self.unsignallable and proc.uid == UID:
                self._apply(proc, int(sig))
        return True

    def _apply(self, proc: Proc, sig: int) -> None:
        if sig == signal.SIGKILL:
            self._end(proc, sig)
        elif sig == signal.SIGTERM:
            if proc.stopped:
                proc.pending_term = True
            elif not proc.ignores_term:
                self._end(proc, sig)
        elif sig == signal.SIGCONT:
            proc.stopped = False
            if proc.pending_term:
                proc.pending_term = False
                if not proc.ignores_term:
                    self._end(proc, signal.SIGTERM)

    def signalled(self) -> set[int]:
        """Every pid a signal reached: individually, or through its group."""
        reached = {pid for kind, pid, _ in self.signals if kind == "pid"}
        for kind, pgid, _ in self.signals:
            if kind == "group":
                reached |= {pid for pid, proc in self.procs.items() if proc.pgid == pgid}
        return reached

    def install(self, monkeypatch) -> "World":
        monkeypatch.setattr(procs, "snapshot", self.snapshot)
        monkeypatch.setattr(procs, "boot_id", lambda: BOOT)
        monkeypatch.setattr(procs, "signal_process", self.signal_process)
        monkeypatch.setattr(procs, "signal_group", self.signal_group)

        class Live:
            """`procs.kernel`, rebuilt from the world on each read."""
            readable = True
            responsibility = True

            def __getattr__(inner, name):
                return getattr(self.kernel(), name)
        monkeypatch.setattr(procs, "kernel", Live())

        def same_process(pid, boot_id, proc_start):
            proc = self.procs.get(pid)
            return bool(proc and proc.alive and proc.start == proc_start)
        monkeypatch.setattr(procs, "same_process", same_process)
        monkeypatch.setattr(procs, "liveness", lambda pid, boot_id, proc_start:
                            "alive" if same_process(pid, boot_id, proc_start) else "dead")

        def identity(pid):
            proc = self.procs.get(pid)
            return procs.ProcessIdentity(pid, BOOT, proc.start) if proc and proc.alive else None
        monkeypatch.setattr(procs, "identity", identity)
        monkeypatch.setattr(procs, "_stat", lambda pid: ("T" if self.procs[pid].stopped else "S")
                            if pid in self.procs and self.procs[pid].alive else None)
        monkeypatch.setattr(procs, "proc_start", lambda pid: self.procs[pid].start
                            if pid in self.procs and self.procs[pid].alive else None)
        return self
