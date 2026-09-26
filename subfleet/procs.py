"""Recorded process identity and conservative macOS containment (C-5).

Process listings containing environments are consumed in memory and discarded;
only pid sets and start identities may become durable evidence.
"""

from __future__ import annotations

from typing import Callable

import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from . import boot_identity

#: C-5.12: how long one boot-identity read is reused. Only a boot session UUID
#: is: `kern.bootsessionuuid` is fixed for a boot, while the `kern.boottime`
#: seconds that a failed UUID read falls back to would make every process
#: recorded with the UUID compare as unknown (C-5.3) for as long as they were
#: kept. A mismatch is read again before `liveness` calls a process dead.
BOOT_ID_TTL_S = 5.0


class InspectionError(RuntimeError):
    """The operating system could not establish process ownership."""


def _read(argv: list[str], *, empty_ok: bool = False) -> str:
    # Match the CLI's rendering of ps lstart; ambient locale/timezone must not
    # make the same live daemon or guardian appear to be a reused pid.
    env = {"LC_ALL": "C", "LANG": "C", "TZ": "UTC",
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    try:
        # C-5.12: `close_fds=False` is what lets CPython start the reader with
        # `posix_spawn` on macOS; with the default it forks, and a fork of the
        # daemon costs in proportion to its memory (measured 2026-09-21 at 1 GB
        # resident and 40 threads: 8.2 ms of daemon CPU per fork, 0.17 ms per
        # spawn). CPython on macOS has no `POSIX_SPAWN_CLOSEFROM`, so closing
        # descriptors means forking. What the reader can inherit: every
        # descriptor Python opens is close-on-exec (PEP 446), but macOS has no
        # `pipe2` and no `SOCK_CLOEXEC`, so a pipe or socket that another thread
        # is creating is inheritable for an instant, and a spawn in that instant
        # hands it down. Only a `ps` or `sysctl` holds it, until it exits within
        # the 10 s cap, so the most that follows is an end-of-file seen that
        # much later. The one descriptor this package hands down on purpose, the
        # guardian's launch gate (`pass_fds`), is closed by the guardian before
        # it inspects anything; a second one must be too.
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10, env=env,
                                close_fds=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InspectionError(f"{os.path.basename(argv[0])} inspection unavailable") from exc
    # BSD ps returns 1 when a valid selector matches no processes.
    if result.returncode and not (
        empty_ok and result.returncode == 1 and not result.stdout.strip()
        and not result.stderr.strip()
    ):
        raise InspectionError(f"{os.path.basename(argv[0])} inspection failed ({result.returncode})")
    return result.stdout


_boot_lock = threading.Lock()
_boot_cache: tuple[float, str] | None = None     # (monotonic read time, boot id)


def boot_id() -> str:
    """Prefer kern.bootsessionuuid; boottime can move with wall-clock correction.

    One read of the UUID serves `BOOT_ID_TTL_S` (C-5.12). Nothing else is kept:
    not a failed read, and not the boottime seconds returned in its place.
    """
    global _boot_cache
    with _boot_lock:
        cached = _boot_cache
        if cached and time.monotonic() - cached[0] <= BOOT_ID_TTL_S:
            return cached[1]

    def optional_read(argv):
        try:
            return _read(argv)
        except InspectionError:
            return ""
    value = boot_identity.read_identity(optional_read)
    if not value:
        raise InspectionError("macOS boot identity is unavailable")
    if boot_identity.session_uuid(value):
        with _boot_lock:
            _boot_cache = (time.monotonic(), value)
    return value


def forget_boot_id() -> None:
    """Drop the cached boot identity; the next `boot_id()` reads `sysctl` again."""
    global _boot_cache
    with _boot_lock:
        _boot_cache = None


def proc_start(pid: int) -> str | None:
    """Read exactly ps's lstart value; an absent process returns None."""
    if pid <= 0:
        return None
    return _read(["/bin/ps", "-p", str(pid), "-o", "lstart="], empty_ok=True).strip() or None


def proc_start_retry(pid: int, *, tries: int = 6, delay_s: float = 0.25,
                     alive: Callable[[], bool] | None = None) -> str | None:
    """`proc_start` with a bounded retry (C-5.3 hardening for C-4.2 `starting`).

    One `ps` pass can miss or time out on a just-spawned pid under load, and a
    single failed read must not cost the caller an attempt. Retries stop early
    when `alive()` says the process is gone. The last inspection error, if
    every try raised, is re-raised so the caller still sees "unavailable".
    """
    last_error: InspectionError | None = None
    for i in range(max(1, tries)):
        try:
            started = proc_start(pid)
        except InspectionError as exc:
            last_error, started = exc, None
        if started:
            return started
        if alive is not None and not alive():
            break
        if i + 1 < tries:
            time.sleep(delay_s)
    if last_error is not None:
        raise last_error
    return None


def _stat(pid: int) -> str | None:
    return _read(["/bin/ps", "-p", str(pid), "-o", "stat="], empty_ok=True).strip() or None


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    boot_id: str
    proc_start: str


def identity(pid: int) -> ProcessIdentity | None:
    """Capture a live, non-zombie process without command or environment data."""
    if pid <= 0:
        return None
    started = proc_start(pid)
    state = _stat(pid) if started else None
    if not started or not state or state.startswith("Z"):
        return None
    return ProcessIdentity(pid, boot_id(), started)


def liveness(pid: int | None, boot_id: str | None, proc_start: str | None) -> str:
    """C-5.3 with three answers: "alive" (the recorded identity), "dead" (absent,
    a zombie, or a different process at that pid), or "unknown" (inspection
    failed). A caller that would act on death must treat "unknown" as no
    evidence at all and look again later; only `same_process`, which gates
    signals, collapses "unknown" into "not the same" (C-5.4)."""
    if not pid or pid <= 0 or not boot_id or not proc_start:
        return "dead"
    for fresh in (False, True):
        try:
            current = identity(pid)
        except InspectionError:
            return "unknown"
        if current is None:
            return "dead"
        if current.proc_start != proc_start:
            return "dead"
        try:
            match = boot_identity.matches(str(boot_id), current.boot_id,
                                          lambda: boot_identity.boot_seconds(_read))
        except InspectionError:
            return "unknown"
        if match is not False or fresh:
            break
        # C-5.12: the boot identity compared may be `BOOT_ID_TTL_S` old. Only a
        # fresh read may say "another boot" about a process whose start matches.
        forget_boot_id()
    return "alive" if match is True else "dead" if match is False else "unknown"


def same_process(pid: int, boot_id: str, proc_start: str) -> bool:
    """C-5.3: pid reuse, a different boot and zombies never match."""
    return liveness(pid, boot_id, proc_start) == "alive"


#: C-5.5: the one read of the process table. `lstart` is last because it is the
#: only column that contains spaces.
TABLE_ARGV = ["/bin/ps", "-axo", "pid=,ppid=,pgid=,stat=,lstart="]


@dataclass(frozen=True)
class ProcessTable:
    """Every process at one instant: parent, group, state and start (C-5.5).

    A table answers the C-5.3 identity question for any number of pids from the
    one `ps` that produced it (C-5.12). It can say that a recorded process is
    alive and nothing else: a boot identity it cannot match exactly (a legacy
    timestamp record) and a pid it does not show are left to `liveness`, and it
    never grants authority to signal, which `signal_group` and `signal_process`
    still read afresh (C-5.4).

    Its boot identity is read the first time it builds a live pid's identity,
    and once, whatever the answer: a table that shows no live pid of interest,
    as an empty census does, needs no `sysctl`, and a table shared by every
    running attempt costs one read however many ask.
    """
    rows: dict[int, tuple[int, int, str, str]]   # pid -> (ppid, pgid, stat, lstart)
    boot_id: str | None = None                   # None: read on first need, by `boot`
    taken_at: float = field(default_factory=time.monotonic)
    _boot: list = field(default_factory=list, init=False, repr=False, compare=False)
    _reading: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def boot(self) -> str:
        """The boot identity of every row; `InspectionError` when it cannot be read."""
        if self.boot_id is not None:
            return self.boot_id
        with self._reading:
            if not self._boot:
                try:
                    self._boot.append(boot_id())     # the module's reader, and its cache
                except InspectionError as exc:
                    self._boot.append(exc)
        found = self._boot[0]
        if isinstance(found, InspectionError):
            raise InspectionError(str(found))
        return found

    def live(self, pid: int) -> bool:
        return pid in self.rows and not self.rows[pid][2].startswith("Z")

    def identity(self, pid: int) -> ProcessIdentity | None:
        """The live, non-zombie process at `pid`, or None when the table cannot say.

        Raises `InspectionError` when the boot identity cannot be read."""
        if not self.live(pid) or not self.rows[pid][3]:
            return None
        return ProcessIdentity(pid, self.boot(), self.rows[pid][3])

    def is_process(self, pid: int | None, boot_id: str | None, proc_start: str | None, *,
                   legacy: bool = False) -> bool:
        """Was the recorded identity live, exactly, when the table was read (C-5.3)?

        With `legacy`, a recorded `kern.boottime` timestamp that C-5.3 matches to
        this boot counts as well, at the cost of one `sysctl`. Raises
        `InspectionError` when the boot identity is needed and cannot be read,
        which includes a UUID record against a table whose UUID read fell back
        to seconds; a pid that is absent, a zombie or another process needs no
        boot identity."""
        if not pid or pid <= 0 or not boot_id or not proc_start:
            return False
        if not self.live(pid) or self.rows[pid][3] != proc_start:
            return False
        current = self.boot()
        if current == str(boot_id):
            return True
        if boot_identity.session_uuid(str(boot_id)) and not boot_identity.session_uuid(current):
            # This table's UUID read fell back to `kern.boottime` seconds, against
            # which a UUID record can only be unknown (C-5.3): a failed read of the
            # boot identity, not an answer about the process.
            raise InspectionError("macOS boot session identity is unavailable")
        return legacy and boot_identity.matches(str(boot_id), current,
                                                lambda: boot_identity.boot_seconds(_read)) is True

    def group(self, pgid: int | None) -> frozenset[int]:
        if not pgid or pgid <= 0:
            return frozenset()
        return frozenset(pid for pid, row in self.rows.items() if row[1] == pgid and self.live(pid))


def snapshot() -> ProcessTable:
    """Read the process table once (C-5.5); raises `InspectionError` when it cannot.

    The boot identity is not read here but when the table first needs it."""
    rows: dict[int, tuple[int, int, str, str]] = {}
    try:
        for row in _read(TABLE_ARGV).splitlines():
            parts = row.split(None, 4)
            if len(parts) < 4:
                continue
            rows[int(parts[0])] = (int(parts[1]), int(parts[2]), parts[3],
                                   parts[4].strip() if len(parts) > 4 else "")
    except ValueError as exc:
        raise InspectionError("ps printed a row that is not a process") from exc
    return ProcessTable(rows)


@dataclass(frozen=True)
class Containment:
    group_pids: frozenset[int] = frozenset()
    descendant_pids: frozenset[int] = frozenset()
    marker_pids: frozenset[int] = frozenset()
    unverifiable: bool = False
    identities: dict[int, ProcessIdentity] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    # pid -> {"ppid", "pgid", "stat"} for every live pid: the shape of what the
    # census saw, without commands or environments (C-5.5 evidence).
    shapes: dict[int, dict[str, Any]] = field(default_factory=dict)

    @property
    def live_pids(self) -> frozenset[int]:
        return self.group_pids | self.descendant_pids | self.marker_pids

    @property
    def verified_empty(self) -> bool:
        return not self.unverifiable and not self.live_pids

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_pids": sorted(self.group_pids),
            "descendant_pids": sorted(self.descendant_pids),
            "marker_pids": sorted(self.marker_pids),
            "live_pids": sorted(self.live_pids),
            "unverifiable": self.unverifiable,
            "identities": {str(pid): asdict(value) for pid, value in self.identities.items()},
            "errors": list(self.errors),
            "shapes": {str(pid): dict(value) for pid, value in sorted(self.shapes.items())},
        }


def containment(pgid: int | None, guardian_pid: int | None, child_pid: int | None,
                attempt_id: str, root: str | None = None) -> Containment:
    """Collect all three C-5.5 sources; any failed inspection prevents release.

    Identities describe the census, not authority to signal. In particular a
    newly discovered escaped process must remain quarantined unless the caller
    already recorded that process's ownership before the escape.

    The group and the descendant walk are read from one process-table snapshot
    (`ps -axo pid=,ppid=,pgid=,stat=,lstart=`), so a process cannot be present
    in one source and absent from the other because it exited between two reads.
    The snapshot also gives every live pid a shape (parent, group, state) that
    the census records as evidence, and the start time that is its identity, so
    a census is two `ps` reads however many processes it finds (C-5.12);
    commands and environments are never retained.
    """
    groups: set[int] = set()
    descendants: set[int] = set()
    markers: set[int] = set()
    errors: list[str] = []
    try:
        seen: ProcessTable | None = snapshot()
        table = seen.rows
    except InspectionError:
        seen, table = None, {}
        errors.append("group enumeration unavailable")
        errors.append("descendant enumeration unavailable")

    def live(pid: int) -> bool:
        return pid in table and not table[pid][2].startswith("Z")

    if seen is not None:
        groups = set(seen.group(pgid))
        # There is no recorded group before setsid. The two remaining sources
        # still enumerate the guardian and any inherited marker.
        roots = {pid for pid in (guardian_pid, child_pid) if pid and pid > 0}
        found = set(roots)
        frontier = roots
        while frontier:
            frontier = {pid for pid, row in table.items() if row[0] in frontier and pid not in found}
            found.update(frontier)
        descendants = {pid for pid in found if live(pid)}
    try:
        if not attempt_id or any(char.isspace() for char in attempt_id):
            raise ValueError("invalid attempt marker")
        marker = re.compile(r"(?:^|\s)SUBFLEET_ATTEMPT=" + re.escape(attempt_id) + r"(?=\s|$)")
        # C-5.5: attempt ids are a timestamp and a slug, so two daemons (or two
        # test state roots) can mint the same id in the same second. The state
        # root is the second half of the marker whenever the caller has one.
        root_marker = (re.compile(r"(?:^|\s)SUBFLEET_ROOT=" + re.escape(root) + r"(?=\s|$)")
                       if root else None)
        # Never retain or report these command/environment strings.
        for row in _read(["/bin/ps", "-axEww", "-o", "pid=,command="]).splitlines():
            pid_text, _, command = row.strip().partition(" ")
            if marker.search(command) and (root_marker is None or root_marker.search(command)):
                pid = int(pid_text)
                state = table[pid][2] if pid in table else _stat(pid)
                if state and not state.startswith("Z"):
                    markers.add(pid)
    except (InspectionError, ValueError):
        errors.append("marker enumeration unavailable")
    identities: dict[int, ProcessIdentity] = {}
    for pid in groups | descendants | markers:
        try:
            # The snapshot's own start time when it has one; a pid it could not
            # describe (a marker spawned after the read) is asked about singly.
            current = (seen.identity(pid) if seen is not None else None) or identity(pid)
            if current is not None:
                identities[pid] = current
            else:
                # A process can exit between census and identity capture.
                groups.discard(pid)
                descendants.discard(pid)
                markers.discard(pid)
        except InspectionError:
            errors.append(f"identity inspection unavailable for pid {pid}")
    shapes = {pid: {"ppid": table[pid][0], "pgid": table[pid][1], "stat": table[pid][2]}
              for pid in groups | descendants | markers if pid in table}
    return Containment(frozenset(groups), frozenset(descendants), frozenset(markers),
                       bool(errors), identities, tuple(errors), shapes)


def signal_group(pgid: int, sig: int | signal.Signals, *, boot_id: str,
                 proc_start: str) -> bool:
    """Signal only a recorded, still-identical group leader (C-5.4)."""
    if pgid <= 1 or pgid == os.getpgrp() or not same_process(pgid, boot_id, proc_start):
        return False
    try:
        if os.getpgid(pgid) != pgid:
            return False
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def signal_process(recorded: ProcessIdentity, sig: int | signal.Signals) -> bool:
    """Signal a survivor only with previously recorded ownership (C-5.6)."""
    if recorded.pid <= 1 or recorded.pid == os.getpid():
        return False
    if not same_process(recorded.pid, recorded.boot_id, recorded.proc_start):
        return False
    try:
        os.kill(recorded.pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False
