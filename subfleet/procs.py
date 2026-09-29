"""Recorded process identity and conservative macOS containment (C-5).

Process listings containing environments are consumed in memory and discarded;
only pid sets and start identities may become durable evidence.
"""

from __future__ import annotations

from typing import Callable

import fcntl
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
        # the 10 s cap, so the most that follows is a close seen that much
        # later. This package hands down two descriptors on purpose (`pass_fds`),
        # both from `pipe_above_stdio`: the guardian's launch gate, which the
        # guardian closes before it inspects anything, and the catalog run's
        # fence, which never reaches this function (the run's own `ps` reads
        # close descriptors). A third must do one or the other.
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
    alive and nothing else: a pid it does not show, and a boot identity it cannot
    match (a legacy timestamp record, unless the caller asks for C-5.3's legacy
    match), are left to `liveness`, and it never grants authority to signal,
    which `signal_group` and `signal_process` still read afresh (C-5.4).

    Its boot identity is read the first time it builds a live pid's identity,
    and once, whatever the answer: a table that shows no live pid of interest,
    as an empty census does, needs no `sysctl`, and a table shared by every
    running attempt costs one read however many ask. The daemon's shared table
    has it read by `boot()` before any attempt is given it, so that only the
    inspection that read the table waits for it.
    """
    rows: dict[int, tuple[int, int, str, str]]   # pid -> (ppid, pgid, stat, lstart)
    boot_id: str | None = None                   # None: read on first need, by `boot`
    taken_at: float = field(default_factory=time.monotonic)
    _boot: list = field(default_factory=list, init=False, repr=False, compare=False)
    _seconds: list = field(default_factory=list, init=False, repr=False, compare=False)
    _reading: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)
    _reading_seconds: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

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
            raise InspectionError(str(found)) from found
        return found

    def legacy_seconds(self) -> str | None:
        """`kern.boottime` seconds for matching a legacy record (C-5.3); `InspectionError`
        when they cannot be read.

        Read the first time a legacy record needs them, once, and kept for the
        table's life, failed or not, so every legacy record asked about the table
        shares one `sysctl`; one that asks while it runs waits for it (C-5.12).
        The lock is this read's own, so no one waiting for `boot()` waits for it."""
        if not self._seconds:
            with self._reading_seconds:
                if not self._seconds:
                    try:
                        self._seconds.append(boot_identity.boot_seconds(_read))
                    except InspectionError as exc:
                        self._seconds.append(exc)
        found = self._seconds[0]
        if isinstance(found, InspectionError):
            raise InspectionError(str(found)) from found
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
        this boot counts as well, at the cost of one `sysctl` per table
        (`legacy_seconds`), and says "alive" only where `liveness` would. Raises
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
        return legacy and boot_identity.matches(str(boot_id), current, self.legacy_seconds) is True

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
    # The recorded guardian, child or group-leader pids another process now
    # holds (C-5.5): exited roots and an emptied group, never writers by pid.
    reused_pids: frozenset[int] = frozenset()

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
            "identities": {str(pid): asdict(value) for pid, value in sorted(self.identities.items())},
            "errors": list(self.errors),
            "shapes": {str(pid): dict(value) for pid, value in sorted(self.shapes.items())},
            "reused_pids": sorted(self.reused_pids),
        }


def group_members(pgid: int) -> dict[int, str]:
    """pid -> `lstart` for the live, non-zombie members of process group `pgid`,
    from one `ps -axo pid=,pgid=,stat=,lstart=` snapshot.

    This is C-5.5's first source alone. It exists for the steady-state record
    of which group members a running attempt or probe owns, which keeps only
    group members: the full census also reads every process's environment
    (`ps -axEww`, megabytes on a busy machine) for markers that record never
    uses. The start column tells a recorded pid's later incarnation from the
    process recorded under it; it is a change detector only, and an identity is
    still captured by `identity` (C-5.3). Containment decisions (release, kill,
    lost, quarantine) still take the full three-source `containment`. Raises
    `InspectionError` when `ps` fails.
    """
    if not pgid or pgid <= 0:
        return {}
    members: dict[int, str] = {}
    try:
        for row in _read(["/bin/ps", "-axo", "pid=,pgid=,stat=,lstart="]).splitlines():
            parts = row.split(None, 3)
            if len(parts) < 4:
                continue
            if int(parts[1]) == pgid and not parts[2].startswith("Z"):
                members[int(parts[0])] = parts[3].strip()
    except ValueError as exc:
        raise InspectionError("group enumeration unavailable") from exc
    return members


def containment(pgid: int | None, guardian_pid: int | None, child_pid: int | None,
                attempt_id: str, root: str | None = None, *,
                recorded_identities: dict[int, ProcessIdentity] | None = None) -> Containment:
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

    Identity decides only what a recorded pid would otherwise decide (C-5.3).
    The guardian and the child seed the parent walk only when the snapshot
    shows, at their pids, the processes recorded for them; a pid another
    process now holds is an exited root, reported in `reused_pids`, never a
    writer. The walk then follows the kernel's parent links, which need no
    recorded identity: a live child of a verified process is the attempt's,
    even at a pid an earlier process of the attempt once held. The recorded
    group is the attempt's while its leader pid holds the recorded leader
    (live or a zombie), or holds nothing on the recorded boot: POSIX does not
    reuse a pid while a group with that id has members, so the id is still
    the attempt's. Another process at the leader pid proves that the old
    group emptied and a new one took the id; none of it is attributed. The
    attempt markers count whatever the identity. Identity evidence one of
    these decisions needs that is missing or cannot be compared prevents
    release.
    """
    groups: set[int] = set()
    descendants: set[int] = set()
    markers: set[int] = set()
    errors: list[str] = []
    recorded = recorded_identities or {}
    reused: set[int] = set()
    try:
        seen: ProcessTable | None = snapshot()
        table = seen.rows
    except InspectionError:
        seen, table = None, {}
        errors.append("group enumeration unavailable")
        errors.append("descendant enumeration unavailable")

    def live(pid: int) -> bool:
        return pid in table and not table[pid][2].startswith("Z")

    boots: dict[str, bool] = {}
    verdicts: dict[int, bool] = {}

    def same_boot(recorded_boot: str) -> bool:
        """Is a recorded boot this table's (C-5.3)? `InspectionError` when unknown."""
        if recorded_boot not in boots:
            match = boot_identity.matches(recorded_boot, seen.boot(), seen.legacy_seconds)
            if match is False:
                # C-5.12: the table's boot identity may be `BOOT_ID_TTL_S` old.
                # Only a fresh read may say "another boot" about a recorded
                # process, as in `liveness`.
                forget_boot_id()
                match = boot_identity.matches(recorded_boot, boot_id(), seen.legacy_seconds)
            if match is None:
                raise InspectionError("recorded boot identity cannot be compared")
            boots[recorded_boot] = match
        return boots[recorded_boot]

    def recorded_here(pid: int) -> bool:
        """Is the row at `pid`, a zombie's too, the process recorded for it? A
        different start alone proves another process and needs no boot read."""
        if pid not in verdicts:
            known = recorded.get(pid)
            try:
                if not known or known.pid != pid or not known.boot_id or not known.proc_start:
                    raise InspectionError("recorded identity is unavailable")
                if not table[pid][3]:
                    raise InspectionError("snapshot start identity is unavailable")
                verdicts[pid] = table[pid][3] == known.proc_start and same_boot(known.boot_id)
                if not verdicts[pid]:
                    reused.add(pid)
            except InspectionError:
                errors.append(f"identity inspection unavailable for pid {pid}")
                verdicts[pid] = False
        return verdicts[pid]

    if seen is not None:
        members = seen.group(pgid)
        if members:
            if pgid in table:
                owned_group = recorded_here(pgid)
            else:
                leader = recorded.get(pgid)
                try:
                    if not leader or leader.pid != pgid or not leader.boot_id or not leader.proc_start:
                        raise InspectionError("recorded group leader identity is unavailable")
                    owned_group = same_boot(leader.boot_id)
                except InspectionError:
                    errors.append("group leader identity inspection unavailable")
                    owned_group = False
            if owned_group:
                groups = set(members)
        # There is no recorded group before setsid. The two remaining sources
        # still enumerate the guardian and any inherited marker.
        roots = {pid for pid in (guardian_pid, child_pid)
                 if pid and pid > 0 and pid in table and recorded_here(pid)}
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
    for pid in sorted(groups | descendants | markers):
        try:
            # A snapshot row is authoritative for this census: one without a
            # start is an inspection failure, not leave to replace it with a
            # later read that could drop a writer or describe a reused pid.
            # Only a marker born after the table is asked about singly.
            current = seen.identity(pid) if pid in table else identity(pid)
            if current is None and pid in table:
                raise InspectionError("snapshot start identity is unavailable")
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
                       bool(errors), identities, tuple(dict.fromkeys(errors)), shapes,
                       frozenset(reused))


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


def pipe_above_stdio() -> tuple[int, int]:
    """A new pipe whose read end a child inherits by `pass_fds`: (read, write), both
    above 2 and close-on-exec in this process.

    `os.pipe()` returns the lowest free descriptors. In a process with 0, 1 or 2
    closed, a read end there is replaced in the child by the /dev/null standard
    stream it is started with, so the child reads end-of-file although the byte was
    written; a write end there takes whatever this process writes to that stream.
    `subprocess` keeps its error pipe's write end above 2 for the same reason. During
    the call the raw ends from `os.pipe()` may sit at 0, 1 or 2, so another thread's
    output to that stream can land in the pipe; the call empties it before returning
    it. The daemon's guardian launch gates (C-5.1) and the catalog run's fence (C-30.1)
    come from here."""
    raw = os.pipe()
    moved: list[int] = []
    try:
        try:
            for fd in raw:
                moved.append(fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3))
        finally:
            for fd in raw:
                os.close(fd)
        os.set_blocking(moved[0], False)
        try:
            while os.read(moved[0], 65536):
                pass
        except BlockingIOError:
            pass
        os.set_blocking(moved[0], True)
    except BaseException:
        for fd in moved:
            os.close(fd)
        raise
    return moved[0], moved[1]
