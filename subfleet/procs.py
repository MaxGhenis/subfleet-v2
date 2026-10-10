"""Recorded process identity and conservative macOS containment (C-5).

Process listings containing environments are consumed in memory and discarded;
only pid sets and start identities may become durable evidence.
"""

from __future__ import annotations

from typing import Callable, Sequence

import fcntl
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from . import boot_identity, folders

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

        Raises `InspectionError` when a listed live process has no start time
        or the boot identity cannot be read. Neither failure proves absence."""
        if not self.live(pid):
            return None
        if not self.rows[pid][3]:
            raise InspectionError("missing process start identity")
        return ProcessIdentity(pid, self.boot(), self.rows[pid][3])

    def census_root(self, pid: int) -> CensusRoot:
        """Retain a listed process, including unknown identity components."""
        try:
            boot = self.boot()
        except InspectionError:
            boot = ""
        row = self.rows[pid]
        return CensusRoot(pid, boot, row[3] or "", row[1])

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


    def descendants(self, roots: Sequence[int], *, excluded: Sequence[int] = ()) -> frozenset[int]:
        """Walk parent links once, including children of retained group members."""
        blocked = set(excluded)
        found = set(roots) - blocked
        frontier = found
        while frontier:
            frontier = {pid for pid, row in self.rows.items()
                        if row[0] in frontier and pid not in found and pid not in blocked}
            found.update(frontier)
        return frozenset(pid for pid in found if self.live(pid))


@dataclass(frozen=True)
class CensusRoot:
    """An observed identity and group, used only to hold containment."""
    pid: int
    boot_id: str
    proc_start: str
    pgid: int

    @property
    def identity(self) -> ProcessIdentity:
        return ProcessIdentity(self.pid, self.boot_id, self.proc_start)


def protected_pids(table: ProcessTable, records: Sequence[ProcessIdentity]) -> set[int]:
    """Foreign history cannot override a live or ambiguous local observation."""
    protected = set()
    for known in records:
        if not table.live(known.pid):
            continue
        started = table.rows[known.pid][3]
        if started and known.proc_start and started != known.proc_start:
            continue
        try:
            match = boot_identity.matches(known.boot_id, table.boot(), table.legacy_seconds)
        except InspectionError:
            match = None
        if match is not False:
            protected.add(known.pid)
    return protected


@dataclass(frozen=True)
class ForeignOwnership:
    """Other attempts' recorded processes, never inferred from shared cwd."""
    identities: tuple[ProcessIdentity, ...] = ()
    groups: tuple[CensusRoot, ...] = ()

    def pids(self, table: ProcessTable, *, protected: Sequence[int] = ()) -> frozenset[int]:
        roots = set()
        for known in self.identities:
            try:
                if table.is_process(known.pid, known.boot_id, known.proc_start, legacy=True):
                    roots.add(known.pid)
            except InspectionError:
                pass  # Unproved foreign ownership cannot discharge a writer.
        for known in self.groups:
            try:
                if (table.is_process(known.pid, known.boot_id, known.proc_start, legacy=True)
                    and table.rows[known.pid][1] == known.pgid) or (
                    any(table.rows[pid][1] == known.pgid for pid in roots)
                ):
                    roots.update(table.group(known.pgid))
            except InspectionError:
                pass
        # Stop at this attempt's verified launch identities: a child dispatch
        # can start inside its parent's tree but has its own guardian/group.
        return table.descendants(tuple(roots), excluded=protected)

    def owns(self, current: ProcessIdentity, group: int | None = None) -> bool:
        if current in self.identities:
            return True
        # A late cwd/marker match may not have appeared in the shared table.
        # Its fresh identity brackets the group lookup at the call site. Check
        # the foreign leader afresh too, so a reused PGID is never excluded.
        for known in self.groups:
            if group != known.pgid or known.pid != known.pgid or current.boot_id != known.boot_id:
                continue
            leader = identity(known.pid)
            if leader == known.identity:
                return True
            # An absent leader alone cannot prove foreign ownership: its old
            # group may have emptied and been reused on this same boot. A
            # still-identical recorded member must anchor a leaderless group.
            for member in self.identities:
                if member.boot_id != current.boot_id:
                    continue
                if identity(member.pid) == member and process_group(member.pid) == group and identity(member.pid) == member:
                    return True
        return False


def process_group(pid: int) -> int | None:
    """The group of a late census observation; no signal authority."""
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return None
    except OSError as exc:
        raise InspectionError("process group inspection unavailable") from exc


CWD_ARGV = ["/usr/sbin/lsof", "-nP", "-d", "cwd", "-F0pn"]


def cwd_pids(workdir: str) -> frozenset[int]:
    """One 10-second-bounded cwd scan; canonical paths and component boundaries.

    NUL fields preserve spaces and newlines in paths. A failed or malformed
    listing proves nothing. Only PIDs are returned; cwd paths are discarded.
    """
    def canonical(path: str) -> str:
        if sys.platform != "darwin":
            return os.path.realpath(path)
        spelled, problem = folders.spelling(path)
        if problem is not None:
            raise InspectionError("cwd canonical spelling unavailable")
        return spelled

    target = canonical(workdir)
    found: set[int] = set()
    pid = None
    named = False
    output = _read(CWD_ARGV)
    if not output.strip():
        raise InspectionError("cwd enumeration empty")
    for field in output.split("\0"):
        field = field.lstrip("\n")
        if not field:
            continue
        if field.startswith("p"):
            if pid is not None and not named:
                raise InspectionError("cwd enumeration incomplete")
            named = False
            try:
                pid = int(field[1:])
                if pid <= 0:
                    raise ValueError("invalid pid")
            except ValueError as exc:
                raise InspectionError("cwd enumeration malformed") from exc
        elif field.startswith("n"):
            if pid is None or not os.path.isabs(field[1:]):
                raise InspectionError("cwd enumeration malformed")
            named = True
            directory = canonical(field[1:])
            if os.path.commonpath((target, directory)) == target:
                found.add(pid)
        elif field != "fcwd":
            raise InspectionError("cwd enumeration malformed")
    if pid is None or not named:
        raise InspectionError("cwd enumeration incomplete")
    return frozenset(found)


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
    # Diagnostic boot observations, including a marker gone before PID capture.
    # They never require a reboot before a verified-empty census can release.
    lineage_boot_ids: tuple[str, ...] = ()

    cwd_pids: frozenset[int] = frozenset()
    lineage_roots: tuple[CensusRoot, ...] = ()
    provider_identities: tuple[ProcessIdentity, ...] = ()
    # Complete identities from the original group's table rows. Later marker
    # reads may change/drop shapes; these observations must keep their identity.
    # The caller must confirm its leader and each member before recording them
    # as signal authority. Retained groups never populate this mapping.
    group_identities: dict[int, ProcessIdentity] = field(default_factory=dict)
    # Transient ownership exclusions let the daemon remove old foreign roots,
    # including group members not yet present in their owner's paced record.
    # They are deliberately absent from the persisted census representation.
    excluded_identities: tuple[ProcessIdentity, ...] = ()
    # Proven ancestry below local launch/ownership roots survives reparenting.
    # Unlike general census roots, these can exclude a foreign observation;
    # like all detached descendants, they confer no authority to signal.
    descendant_identities: tuple[ProcessIdentity, ...] = ()

    @property
    def live_pids(self) -> frozenset[int]:
        return self.group_pids | self.descendant_pids | self.marker_pids | self.cwd_pids

    @property
    def verified_empty(self) -> bool:
        return not self.unverifiable and not self.errors and not self.live_pids

    def to_dict(self) -> dict[str, Any]:
        return {
            "cwd_pids": sorted(self.cwd_pids),
            "lineage_roots": [asdict(value) for value in self.lineage_roots],
            "provider_identities": [asdict(value) for value in self.provider_identities],
            "descendant_identities": [asdict(value) for value in self.descendant_identities],
            "group_pids": sorted(self.group_pids),
            "descendant_pids": sorted(self.descendant_pids),
            "marker_pids": sorted(self.marker_pids),
            "live_pids": sorted(self.live_pids),
            "unverifiable": self.unverifiable,
            "identities": {str(pid): asdict(value) for pid, value in self.identities.items()},
            "errors": list(self.errors),
            "shapes": {str(pid): dict(value) for pid, value in sorted(self.shapes.items())},
            "lineage_boot_ids": list(self.lineage_boot_ids),
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
                recorded: dict[int, ProcessIdentity | Sequence[ProcessIdentity]] | None = None,
                owned_identities: Sequence[ProcessIdentity] = (),
                launch_boot_id: str | None = None,
                lineage_boot_ids: Sequence[str] = (),
                child_unrecorded: bool = False,
                workdir: str | None = None,
                lineage_roots: Sequence[CensusRoot] = (),
                lineage_overflow_boot: str | None = None,
                guardian_identity: ProcessIdentity | None = None,
                foreign_ownership: ForeignOwnership = ForeignOwnership()) -> Containment:
    """Collect C-5.5 group, lineage, cwd and marker sources; failures hold.

    General identities describe the census, not authority to signal. Only
    group_identities preserves complete original-group table observations;
    callers must confirm the leader and member before recording ownership.
    Newly discovered escaped identities remain conservative census evidence.

    The group and the descendant walk are read from one process-table snapshot
    (`ps -axo pid=,ppid=,pgid=,stat=,lstart=`), so a process cannot be present
    in one source and absent from the other because it exited between two reads.
    The snapshot also gives every live pid a shape (parent, group, state) that
    the census records as evidence, and the start time that is its identity, so
    a census uses two listing `ps` reads and one bounded cwd `lsof` (C-5.12),
    plus fresh per-PID identities bracketing the group lookup for every later
    marker or cwd match. Commands and environments are never retained.
    """
    groups: set[int] = set()
    descendants: set[int] = set()
    markers: set[int] = set()
    cwds: set[int] = set()
    providers: tuple[ProcessIdentity, ...] = ()
    excluded_identities: set[ProcessIdentity] = set()
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

    local_seen: dict[int, ProcessIdentity] = {}
    proven_roots = set()
    if seen is not None:
        protected = protected_pids(seen, owned_identities)
        for known in owned_identities:
            try:
                if seen.is_process(known.pid, known.boot_id, known.proc_start, legacy=True):
                    proven_roots.add(known.pid)
            except InspectionError:
                pass
        # Launch and locally recorded ownership stop foreign ancestor walks.
        # Census-only roots may themselves be contaminated and do not do so.
        for pid in (guardian_pid, child_pid):
            values = (recorded or {}).get(pid, ())
            values = [values] if isinstance(values, ProcessIdentity) else values
            if pid == guardian_pid and guardian_identity is not None:
                values = [*values, guardian_identity]
            for known in values:
                try:
                    if seen.is_process(pid, known.boot_id, known.proc_start, legacy=True):
                        protected.add(pid)
                        proven_roots.add(pid)
                        break
                except InspectionError:
                    pass
        local_seen = {pid: seen.census_root(pid).identity for pid in protected}
        excluded = foreign_ownership.pids(seen, protected=protected)
        foreign_seen = {pid: seen.census_root(pid).identity for pid in excluded}
        excluded_identities.update(foreign_seen.values())
        def rebooted(known: str | None) -> bool:
            # Only distinct kernel boot-session UUIDs prove every old writer
            # dead. Legacy wall-clock seconds and malformed IDs cannot do so.
            previous = boot_identity.session_uuid(known)
            if previous is None:
                return False
            try:
                current = boot_identity.session_uuid(seen.boot())
            except InspectionError:
                return False
            return current is not None and previous != current

        # C-5.3/C-5.7: a PID's new incarnation is not a recorded writer.
        # Read all recorded writers from this same snapshot, including escaped
        # writers whose parent links and markers no longer identify them.
        gone, owned, uncertain_roots = set(), set(), set()
        records_by_pid = {pid: ([values] if isinstance(values, ProcessIdentity) else list(values))
                          for pid, values in (recorded or {}).items()}
        for known in lineage_roots:
            records_by_pid.setdefault(known.pid, []).append(known.identity)
        for pid, observations in records_by_pid.items():
            if pid in excluded:
                continue
            try:
                if not live(pid):
                    gone.add(pid)
                elif not table[pid][3]:
                    raise InspectionError("missing process start identity")
                else:
                    # No observation overrides another. Ownership needs one
                    # match; death needs every observation proved different.
                    records = (observations,) if isinstance(observations, ProcessIdentity) else observations
                    uncertain = False
                    for known in records:
                        if rebooted(known.boot_id):
                            continue
                        if not known.proc_start:
                            uncertain = True
                            continue
                        if table[pid][3] != known.proc_start:
                            continue
                        if not known.boot_id:
                            uncertain = True
                            continue
                        try:
                            match = boot_identity.matches(known.boot_id, seen.boot(), seen.legacy_seconds)
                        except InspectionError:
                            match = None
                        if match is True:
                            owned.add(pid)
                            break
                        if match is None:
                            uncertain = True
                    if pid not in owned:
                        if uncertain or not records:
                            raise InspectionError("unknown boot identity")
                        gone.add(pid)
            except InspectionError:
                errors.append(f"recorded identity inspection unavailable for pid {pid}")
                if live(pid):
                    uncertain_roots.add(pid)
        roots_rebooted = rebooted(launch_boot_id)
        if lineage_overflow_boot is not None and not rebooted(lineage_overflow_boot):
            errors.append("lineage root limit exceeded")
        # A verified guardian's direct children establish the legacy provider
        # identities before signals or finalization can erase the parent link.
        guardian_verified = False
        if guardian_identity is not None:
            try:
                guardian_verified = seen.is_process(guardian_pid, guardian_identity.boot_id,
                                                     guardian_identity.proc_start, legacy=True)
            except InspectionError:
                errors.append("guardian launch identity inspection unavailable")
        if guardian_verified:
            found_providers = []
            for pid, row in table.items():
                if row[0] == guardian_pid and live(pid) and pid not in excluded:
                    try:
                        found_providers.append(seen.identity(pid))
                    except InspectionError:
                        errors.append(f"provider identity inspection unavailable for pid {pid}")
            providers = tuple(value for value in found_providers if value is not None)
        if child_unrecorded and not providers and not roots_rebooted:
            # A legacy guardian may have executed a child without publishing
            # its PID. Its own disappearance cannot establish that child's death.
            errors.append("guardian child publication unavailable")
        groups = set() if roots_rebooted else set(seen.group(pgid))
        # A live reused group leader heads an unrelated group; an absent leader
        # can still leave its original group members behind.
        # Only the leader's pid says so: XNU never hands out a pid that names a
        # live group or session, so any other live member of the group is ours,
        # whatever pid it was given (a recycled one included).
        if pgid in gone and live(pgid):
            groups.clear()
        # A matching identity also observes its current group immediately.
        # This keeps the census stable after that observation is persisted and
        # follows a writer that changed groups since the earlier inspection.
        for pid in owned | uncertain_roots:
            groups.update(seen.group(table[pid][1]))
        # A sampled group can belong to a different incarnation than its
        # retained identity after a failed bracket. Leader identity/reuse
        # cannot discharge it; hold until the group is empty or rebooted.
        for known in lineage_roots:
            if rebooted(known.boot_id):
                continue
            groups.update(seen.group(known.pgid))
        # There is no recorded group before setsid. The two remaining sources
        # still enumerate the guardian and any inherited marker.
        roots = ({pid for pid in (guardian_pid, child_pid)
                  if pid and pid > 0 and pid not in gone and not roots_rebooted}
                 | owned | groups | uncertain_roots)
        groups.difference_update(excluded)
        descendants = set(seen.descendants(tuple(roots), excluded=excluded))
    else:
        foreign_seen = {}
    # PID sets describe sources only. Durable observations are keyed by the
    # full identity, since multiple incarnations can appear in one census.
    identities: dict[int, ProcessIdentity] = {}
    group_identities: dict[int, ProcessIdentity] = {}
    observed_groups: dict[ProcessIdentity, set[int]] = {}
    incomplete_roots: list[CensusRoot] = []
    shapes: dict[int, dict[str, Any]] = {}
    genuine_descendants = seen.descendants(tuple(proven_roots), excluded=excluded) if seen is not None else ()
    proven_descendants = []

    def retain(current: ProcessIdentity, group: int) -> None:
        identities[current.pid] = current
        observed_groups.setdefault(current, set()).add(group)

    for pid in sorted(groups | descendants):
        row = table[pid]
        shapes[pid] = {"ppid": row[0], "pgid": row[1], "stat": row[2]}
        try:
            current = seen.identity(pid)
            if current is not None:
                retain(current, row[1])
                if pid in genuine_descendants:
                    proven_descendants.append(current)
                if row[1] == pgid:
                    group_identities[pid] = current
        except InspectionError:
            errors.append(f"identity inspection unavailable for pid {pid}")
            incomplete_roots.append(seen.census_root(pid))

    def observe_later(pid: int, source: set[int], *, foreign: ProcessIdentity | None = None) -> None:
        source.add(pid)
        current = None
        group = None
        try:
            # A later positive match never borrows the earlier incarnation's
            # zombie verdict, start identity, or group. Bracket the group read
            # with full identities so reuse during capture cannot mix them.
            current = identity(pid)
            if current is None:
                state = _stat(pid)
                if not state or state.startswith("Z"):
                    source.discard(pid)
                    return
                raise InspectionError("missing process start identity")
            # A shared cwd does not make another attempt our writer. Exclude
            # only the same incarnation observed with its different marker.
            local = current in owned_identities or current == local_seen.get(pid)
            if current == foreign and not local:
                source.discard(pid)
                return
            group = process_group(pid)
            confirmed = identity(pid)
            if confirmed != current or group is None:
                if confirmed is not None and confirmed != current:
                    retain(confirmed, 0)
                raise InspectionError("process changed during group inspection")
            if not local and (current == foreign_seen.get(pid) or foreign_ownership.owns(current, group)):
                excluded_identities.add(current)
                source.discard(pid)
                return
            retain(current, group)
            # Shapes from a different incarnation must not cause the durable
            # merge to attach its group to this newly observed identity.
            if (seen is not None and table.get(pid, (None, None, None, None))[3] == current.proc_start
                    and seen.boot() == current.boot_id):
                row = table[pid]
                shapes[pid] = {"ppid": row[0], "pgid": group, "stat": "S"}
            else:
                shapes.pop(pid, None)
        except InspectionError:
            errors.append(f"identity inspection unavailable for pid {pid}")
            shapes.pop(pid, None)
            # The failed later read has no entitlement to the old table row.
            # A sampled group remains conservative evidence if confirmation
            # fails; even an unreadable group leaves the identity as a root.
            # These roots never grant signal authority (C-5.3).
            incomplete_roots.append(CensusRoot(pid, current.boot_id if current else "",
                                                current.proc_start if current else "", group or 0))

    observed_writer = bool(groups | descendants)
    foreign_attempts: dict[int, ProcessIdentity] = {}
    observed_boots = set(lineage_boot_ids)
    try:
        if not attempt_id or any(char.isspace() for char in attempt_id):
            raise ValueError("invalid attempt marker")
        marker = re.compile(r"(?:^|\s)SUBFLEET_ATTEMPT=" + re.escape(attempt_id) + r"(?=\s|$)")
        # An explicit different attempt belongs to another run (including
        # a parent turn waiting for this one). Fall back to the state root only
        # when no nonempty attempt marker is visible; partial markers still hold.
        attempt_marker = re.compile(r"(?:^|\s)SUBFLEET_ATTEMPT=\S+")
        root_marker = (re.compile(r"(?:^|\s)SUBFLEET_ROOT=" + re.escape(root) + r"(?=\s|$)")
                       if root else None)
        # Never retain or report these command/environment strings.
        for row in _read(["/bin/ps", "-axEww", "-o", "pid=,command="]).splitlines():
            pid_text, _, command = row.strip().partition(" ")
            if attempt_marker.search(command) and not marker.search(command):
                # Qualify cwd exclusions by full identity, never by bare PID.
                # An unverified incarnation remains a cwd census root.
                pid = int(pid_text)
                try:
                    earlier = seen.identity(pid) if seen is not None and pid in table else None
                    current = identity(pid) if earlier is not None or workdir is not None else None
                    if current is not None and current == earlier:
                        foreign_attempts[pid] = current
                    elif current is not None and workdir is not None:
                        # A background waiter can start after the table. Read
                        # its marker again between fresh identities, so excluding
                        # it cannot instead exclude a reused PID's cwd writer.
                        command = _read(["/bin/ps", "-p", str(pid), "-Eww", "-o", "command="],
                                        empty_ok=True)
                        if (attempt_marker.search(command) and not marker.search(command)
                                and identity(pid) == current):
                            foreign_attempts[pid] = current
                        elif marker.search(command) or (root_marker is not None and root_marker.search(command)
                                                       and not attempt_marker.search(command)):
                            observed_writer = True
                            observe_later(pid, markers)
                except InspectionError:
                    pass
                continue
            if marker.search(command) or (root_marker is not None and root_marker.search(command)):
                observed_writer = True
                pid = int(pid_text)
                observe_later(pid, markers)
    except (InspectionError, ValueError):
        errors.append("marker enumeration unavailable")
    if workdir is not None:
        try:
            for pid in cwd_pids(workdir):
                observe_later(pid, cwds, foreign=foreign_attempts.get(pid))
        except InspectionError:
            errors.append("cwd enumeration unavailable")
    observed_writer = observed_writer or bool(cwds)
    if observed_writer:
        try:
            observed_boots.add(seen.boot() if seen is not None else boot_id())
        except InspectionError:
            # Retain the failed observation for diagnostics only.
            observed_boots.add("")
    captured = tuple(CensusRoot(ident.pid, ident.boot_id, ident.proc_start, group)
                     for ident, groups_seen in observed_groups.items()
                     for group in sorted(groups_seen)) + tuple(incomplete_roots)
    return Containment(frozenset(groups), frozenset(descendants), frozenset(markers),
                       bool(errors), identities, tuple(errors), shapes, tuple(sorted(observed_boots)),
                       frozenset(cwds), captured, providers, group_identities, tuple(excluded_identities),
                       tuple(proven_descendants))


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
