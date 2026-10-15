"""Recorded process identity and conservative macOS containment (C-5).

Process listings containing environments are consumed in memory and discarded;
only pid sets and start identities may become durable evidence.
"""

from __future__ import annotations

from typing import Callable, ContextManager, Sequence

import ctypes
import errno
import fcntl
import os
import signal
import struct
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field, replace
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
    state = _stat(pid)
    if not state or state.startswith("Z"):
        return None
    if not started:
        raise InspectionError("process start identity is unavailable")
    return ProcessIdentity(pid, boot_id(), started)


def liveness(pid: int | None, boot_id: str | None, proc_start: str | None) -> str:
    """C-5.3 with three answers: "alive" (the recorded identity), "dead" (absent,
    a zombie, or a different process at that pid), or "unknown" (inspection
    failed). A caller that would act on death must treat "unknown" as no
    evidence at all and look again later; only `same_process`, which gates
    signals, collapses "unknown" into "not the same" (C-5.4)."""
    if not pid or pid <= 0:
        return "dead"
    for fresh in (False, True):
        try:
            current = identity(pid)
        except InspectionError:
            return "unknown"
        if current is None:
            return "dead"
        if not boot_id or not proc_start:
            return "unknown"
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
    _confirmed_boot: list = field(default_factory=list, init=False, repr=False, compare=False)
    _seconds: list = field(default_factory=list, init=False, repr=False, compare=False)
    _reading: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)
    _reading_seconds: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    _groups: dict[int, frozenset[int]] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        groups: dict[int, set[int]] = {}
        for pid, row in self.rows.items():
            if not row[2].startswith("Z"):
                groups.setdefault(row[1], set()).add(pid)
        object.__setattr__(self, "_groups", {pgid: frozenset(pids) for pgid, pids in groups.items()})

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

    def boot_matches(self, known: str) -> bool | None:
        """C-5.12: share one fresh confirmation before proving another boot."""
        match = boot_identity.matches(known, self.boot(), self.legacy_seconds)
        if match is not False:
            return match
        with self._reading:
            if not self._confirmed_boot:
                try:
                    forget_boot_id()
                    self._confirmed_boot.append(boot_id())
                except InspectionError as exc:
                    self._confirmed_boot.append(exc)
        found = self._confirmed_boot[0]
        if isinstance(found, InspectionError):
            raise InspectionError(str(found)) from found
        return boot_identity.matches(known, found, self.legacy_seconds)

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

    def identity_gone(self, known: ProcessIdentity) -> bool:
        """A missing/zombie/reused identity is proved gone; incomplete reads hold."""
        if not self.live(known.pid):
            return True
        started = self.rows[known.pid][3]
        # A positive start mismatch disproves this incarnation independently
        # of boot inspection. Missing or matching starts still need boot proof.
        if started and known.proc_start and started != known.proc_start:
            return True
        if not known.boot_id:
            return False
        try:
            if boot_identity.session_uuid(known.boot_id) and self.boot_matches(known.boot_id) is False:
                return True
            if not started or not known.proc_start:
                return False
            return self.boot_matches(known.boot_id) is False
        except InspectionError:
            return False

    def root_resolved(self, known: CensusRoot) -> bool:
        return self.identity_gone(known.identity) and not self.group(known.pgid)

    def group(self, pgid: int | None) -> frozenset[int]:
        if not pgid or pgid <= 0:
            return frozenset()
        return self._groups.get(pgid, frozenset())


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
            match = table.boot_matches(known.boot_id) if known.boot_id else None
        except InspectionError:
            match = None
        if match is not False:
            protected.add(known.pid)
    return protected


def identities_overlap(left: ProcessIdentity, right: ProcessIdentity) -> bool:
    """Whether coarse/legacy identities can describe the same incarnation.

    Missing identity components and unavailable legacy boot inspection cannot
    disprove a collision. This grants containment protection, never authority.
    """
    if left.pid != right.pid:
        return False
    if left.proc_start and right.proc_start and left.proc_start != right.proc_start:
        return False
    if not left.boot_id or not right.boot_id:
        return True
    try:
        return boot_identity.matches(left.boot_id, right.boot_id,
                                     lambda: boot_identity.boot_seconds(_read)) is not False
    except InspectionError:
        return True


def _qualified_identity_matches(known: ProcessIdentity, current: ProcessIdentity) -> bool:
    """Full start qualification with the existing legacy boot compatibility."""
    return (known.pid == current.pid and bool(known.proc_start) and bool(current.proc_start)
            and known.proc_start == current.proc_start and bool(known.boot_id) and bool(current.boot_id)
            and boot_identity.matches(known.boot_id, current.boot_id,
                                      lambda: boot_identity.boot_seconds(_read)) is True)


@dataclass(frozen=True)
class ForeignOwnership:
    """Other attempts' recorded processes, never inferred from shared cwd."""
    identities: tuple[ProcessIdentity, ...] = ()
    groups: tuple[CensusRoot, ...] = ()
    launches: tuple[ProcessIdentity, ...] = ()
    # All attempts' claims, including terminal histories, can invalidate signal
    # authority. Only the active identities/groups above can exclude a process.
    claims: tuple[ProcessIdentity, ...] = ()
    collisions: tuple[ProcessIdentity, ...] = ()

    generation: int | None = field(default=None, compare=False)
    signal_candidates: frozenset[int] | None = field(default=None, compare=False, repr=False)
    # Missing active launch receipts can appear after a cached census. They
    # may suppress delivery, but never enlarge its signal candidates.
    receipt_candidates: tuple[tuple[str, str, int], ...] = field(default=(), repr=False, compare=False)
    claims_by_pid: dict[int, tuple[ProcessIdentity, ...]] = field(init=False, compare=False, repr=False)

    def __post_init__(self):
        buckets: dict[int, set[ProcessIdentity]] = {}
        for known in (*self.claims, *self.identities):
            buckets.setdefault(known.pid, set()).add(known)
        object.__setattr__(self, "claims_by_pid", {pid: tuple(values) for pid, values in buckets.items()})

    def claimed(self, current: ProcessIdentity) -> bool:
        return any(identities_overlap(known, current) for known in self.claims_by_pid.get(current.pid, ()))

    def ambiguous(self, current: ProcessIdentity) -> bool:
        return any(identities_overlap(known, current) for known in self.collisions)

    def observes(self, current: ProcessIdentity, *, ambiguous: Sequence[ProcessIdentity] = ()) -> bool:
        """Fresh foreign group proof, including before its next ownership record."""
        # A colliding leader cannot authenticate either attempt's whole group.
        groups = tuple(known for known in self.groups
                       if not any(identities_overlap(value, known.identity) for value in ambiguous))
        if not groups or identity(current.pid) != current:
            return False
        group = process_group(current.pid)
        members = tuple(known for known in self.identities
                        if not any(identities_overlap(value, known) for value in ambiguous))
        return (group is not None and replace(self, groups=groups, identities=members).owns(current, group)
                and identity(current.pid) == current)

    def local_descendants(self, table: ProcessTable, roots: Sequence[int], *,
                          group_pids: Sequence[int] = ()) -> frozenset[int]:
        """Local launch/group proof wins collisions; independent launches stop it.

        Historical foreign descendants cannot override current local ancestry.
        Only verified foreign launch receipts form boundaries in that walk.
        """
        local = set(roots)
        boundaries = set()
        for known in self.launches:
            try:
                if table.is_process(known.pid, known.boot_id, known.proc_start, legacy=True):
                    boundaries.add(known.pid)
            except InspectionError:
                pass
        # A stale foreign launch can reuse a member of the authenticated local
        # group within the same lstart second. Retain that collision; independent
        # foreign groups still delimit nested dispatches.
        boundaries.difference_update(local | set(group_pids))
        local.update(set(group_pids) - boundaries)
        return table.descendants(tuple(local), excluded=boundaries)

    def pids(self, table: ProcessTable, *, protected: Sequence[int] = ()) -> frozenset[int]:
        blocked = set(protected) | protected_pids(table, self.collisions)
        roots = set()
        for known in self.identities:
            if known.pid in blocked:
                continue
            try:
                if table.is_process(known.pid, known.boot_id, known.proc_start, legacy=True):
                    roots.add(known.pid)
            except InspectionError:
                pass  # Unproved foreign ownership cannot discharge a writer.
        for known in self.groups:
            if known.pid in blocked:
                continue
            try:
                if (table.is_process(known.pid, known.boot_id, known.proc_start, legacy=True)
                    and table.rows[known.pid][1] == known.pgid) or (
                    any(table.rows[pid][1] == known.pgid for pid in roots)
                ):
                    roots.update(table.group(known.pgid) - blocked)
            except InspectionError:
                pass
        # Stop at this attempt's verified launch identities: a child dispatch
        # can start inside its parent's tree but has its own guardian/group.
        return table.descendants(tuple(roots), excluded=blocked)

    def owns(self, current: ProcessIdentity, group: int | None = None, *,
             ambiguous: Sequence[ProcessIdentity] = ()) -> bool:
        blocked = (*self.collisions, *ambiguous)
        if any(identities_overlap(known, current) for known in blocked):
            return False
        members = tuple(known for known in self.identities
                        if not any(identities_overlap(value, known) for value in blocked))
        if any(_qualified_identity_matches(known, current) for known in members):
            return True
        # A late cwd/marker match may not have appeared in the shared table.
        # Its fresh identity brackets the group lookup at the call site. Check
        # the foreign leader afresh too, so a reused PGID is never excluded.
        for known in self.groups:
            if any(identities_overlap(value, known.identity) for value in blocked):
                continue
            if (group != known.pgid or known.pid != known.pgid
                    or boot_identity.matches(known.boot_id, current.boot_id,
                                             lambda: boot_identity.boot_seconds(_read)) is not True):
                continue
            leader = identity(known.pid)
            if leader is not None and _qualified_identity_matches(known.identity, leader):
                if process_group(known.pid) == known.pgid and identity(known.pid) == leader:
                    return True
                continue  # A guardian cannot leave its original session group.
            if leader is not None:
                continue  # A live reused leader proves the old group emptied.
            # An absent leader alone cannot prove foreign ownership: its old
            # group may have emptied and been reused on this same boot. A
            # still-identical recorded member must anchor a leaderless group.
            for member in members:
                if boot_identity.matches(member.boot_id, current.boot_id,
                                         lambda: boot_identity.boot_seconds(_read)) is not True:
                    continue
                observed = identity(member.pid)
                if (observed is not None and _qualified_identity_matches(member, observed)
                        and process_group(member.pid) == group and identity(member.pid) == observed):
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


# C-5.5's source reads, named as a census records their failure. A census whose
# only errors are these, and that shows no live process and no ambiguous identity,
# saw nothing at all: it is blind, and decides nothing on its own (C-5.9).
GROUP_UNAVAILABLE = "group enumeration unavailable"
DESCENDANT_UNAVAILABLE = "descendant enumeration unavailable"
MARKER_UNAVAILABLE = "marker enumeration unavailable"
CWD_UNAVAILABLE = "cwd enumeration unavailable"
ENUMERATION_ERRORS = frozenset({GROUP_UNAVAILABLE, DESCENDANT_UNAVAILABLE, MARKER_UNAVAILABLE, CWD_UNAVAILABLE, "responsibility enumeration unavailable"})


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
    # Collisions hold leases/quarantine, but are never new signal ownership.
    ambiguous_identities: tuple[ProcessIdentity, ...] = ()

    foreign_ownership: ForeignOwnership = field(default_factory=ForeignOwnership, repr=False, compare=False)
    resolved_lineage_roots: tuple[CensusRoot, ...] = ()
    resolved_identities: tuple[ProcessIdentity, ...] = ()
    # Proven recycled roots/leaders, including identities excluded from the
    # live sets. No command or environment may enter this evidence (C-5.5).
    recycled: dict[int, dict[str, Any]] = field(default_factory=dict)
    # C-5.5's fourth source (D-CEN1): the live processes whose responsible
    # process is the attempt's recorded responsibility root.
    responsible_pids: frozenset[int] = frozenset()

    @property
    def live_pids(self) -> frozenset[int]:
        return self.group_pids | self.descendant_pids | self.marker_pids | self.cwd_pids | self.responsible_pids

    @property
    def verified_empty(self) -> bool:
        return not self.unverifiable and not self.errors and not self.live_pids and not self.ambiguous_identities

    @property
    def blind(self) -> bool:
        """C-5.9: not verified empty only because a source read failed (a `ps` or
        the cwd `lsof` timed out or failed), with no process or ambiguity seen."""
        return (bool(self.errors) and set(self.errors) <= ENUMERATION_ERRORS
                and not self.live_pids and not self.ambiguous_identities)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolved_lineage_roots": [asdict(value) for value in self.resolved_lineage_roots],
            "resolved_identities": [asdict(value) for value in self.resolved_identities],
            "cwd_pids": sorted(self.cwd_pids),
            "lineage_roots": [asdict(value) for value in self.lineage_roots],
            "provider_identities": [asdict(value) for value in self.provider_identities],
            "descendant_identities": [asdict(value) for value in self.descendant_identities],
            "ambiguous_identities": [asdict(value) for value in self.ambiguous_identities],
            "group_pids": sorted(self.group_pids),
            "descendant_pids": sorted(self.descendant_pids),
            "marker_pids": sorted(self.marker_pids),
            "responsible_pids": sorted(self.responsible_pids),
            "live_pids": sorted(self.live_pids),
            "unverifiable": self.unverifiable,
            "identities": {str(pid): asdict(value) for pid, value in self.identities.items()},
            "errors": list(self.errors),
            "shapes": {str(pid): dict(value) for pid, value in sorted(self.shapes.items())},
            "lineage_boot_ids": list(self.lineage_boot_ids),
            "recycled": {str(pid): dict(value) for pid, value in sorted(self.recycled.items())},
        }


def group_members(pgid: int) -> dict[int, str]:
    """pid -> `lstart` for the live, non-zombie members of process group `pgid`,
    from one `ps -axo pid=,pgid=,stat=,lstart=` snapshot.

    This is C-5.5's first source alone. It exists for the steady-state record
    of which group members a running attempt or probe owns, which keeps only
    group members: the full census also reads every process's argument buffer
    (megabytes on a busy machine) for markers, and every process's responsible
    process, which that record never uses. The start column tells a recorded pid's later incarnation from the
    process recorded under it; it is a change detector only, and an identity is
    still captured by `identity` (C-5.3). Containment decisions (release, kill,
    lost, quarantine) still take the full `containment`, including responsibility
    for attempts that recorded a root. Raises
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


# C-5.5's in-process reads (D-CEN1). `ps -E` stops a process's environment at the
# first empty string, and a title written over the argv area (node's
# `process.title`) leaves that area full of NULs, so it showed no marker for
# vitest 19935 and 21910 on 2026-10-13. The census reads the same buffer itself,
# through `sysctl` `KERN_PROCARGS2`, and looks at every string in it.
_CTL_KERN, _KERN_ARGMAX, _KERN_PROCARGS2 = 1, 8, 49
_PROC_ALL_PIDS = 1
#: `struct proc_bsdinfowithuniqid` (sys/proc_info.h): a 136-byte `proc_bsdinfo`
#: (flags, status, xstatus, pid, ...), then `proc_uniqidentifierinfo`, whose
#: 64-bit `p_uniqueid` follows a 16-byte UUID.
_PROC_PIDT_BSDINFOWITHUNIQID = 18
_BSDINFO_SIZE = 136
_BSDINFO_WITH_UNIQID_SIZE = _BSDINFO_SIZE + 56
_UNIQUEID_OFFSET = _BSDINFO_SIZE + 16
_PROC_FLAG_INEXIT = 4                  # pbi_flags: the process has begun to exit
_SZOMB = 5                             # pbi_status: a zombie
#: Initial allocation; a full read retries up to the kernel's own KERN_ARGMAX.
#: A read still filling that maximum cannot establish that all markers were read.
try:
    _ARGS_BUFFER = os.sysconf("SC_ARG_MAX") if sys.platform == "darwin" else 1 << 20
except (ValueError, OSError):
    _ARGS_BUFFER = 1 << 20
_ATTEMPT_PREFIX, _ROOT_PREFIX = b"SUBFLEET_ATTEMPT=", b"SUBFLEET_ROOT="


@dataclass(frozen=True)
class KernelProcess:
    """What `proc_pidinfo` says of a live pid this process may inspect: whether it
    has begun to exit and its unique id, which the kernel gives each new process
    from one counter for the life of a boot. No command, environment or name."""
    pid: int
    exiting: bool
    unique_id: int


@dataclass(frozen=True)
class ResponsibleRoot:
    """A process started with responsibility disclaimed (`spawn_disclaimed`): it is
    its own responsible process, and every process it starts, and they start, is
    reported with it as theirs whatever they do to their session, group, parent,
    title or environment (D-CEN1). `unique_id` orders it among the processes of
    its boot (`boot_id`): one created before it, at a pid that once had another
    holder, is not its."""
    pid: int
    boot_id: str | None
    unique_id: int | None


def procargs_strings(buffer: bytes) -> list[bytes]:
    """The NUL-terminated strings of a `KERN_PROCARGS2` buffer after the executable
    path: the argv area, the environment and what the kernel appends, empty strings
    included. The buffer is `argc` (4 bytes), the executable path, NUL padding, then
    those strings; a string the buffer ends inside of is not whole and is not
    returned, so a buffer cut short can make a string missing, never a different one."""
    if len(buffer) <= 4:
        return []
    body = buffer[4:]
    end = body.find(b"\0")
    if end < 0:
        return []                          # not even the executable path is whole
    strings = body[end + 1:].split(b"\0")
    strings.pop()                          # after the last NUL: unterminated, or nothing
    return strings


def marker_strings(attempt_id: str, root: str | None) -> tuple[bytes, bytes | None]:
    """The exact strings C-5.5's marker source looks for; `ValueError` for an id that
    no environment string could carry whole."""
    if not attempt_id or any(char.isspace() for char in attempt_id) or "\0" in attempt_id:
        raise ValueError("invalid attempt marker")
    if root is not None and "\0" in root:
        raise ValueError("invalid state root marker")
    return (_ATTEMPT_PREFIX + os.fsencode(attempt_id),
            _ROOT_PREFIX + os.fsencode(root) if root else None)


def procargs_marked(buffer: bytes, attempt: bytes, root: bytes | None) -> bool:
    """C-5.5: does some whole string after the executable path equal `attempt`, and,
    when the caller has a state root, another equal `root`? Over-inclusion is the
    safe side, so a string in the argv area counts as one in the environment."""
    strings = procargs_strings(buffer)
    return attempt in strings and (root is None or root in strings)


class Darwin:
    """C-5.5's in-process reads of the process table: libproc's pid list and
    `proc_pidinfo`, `sysctl` `KERN_PROCARGS2`, and the responsibility SPI. They
    start no process, so a census is one `ps` and these (C-5.12). What they read
    is kept in memory only for the match; a census records pids, shapes and
    identities, never a command or an environment."""

    def __init__(self) -> None:
        try:
            libc = ctypes.CDLL(None, use_errno=True)
        except OSError:
            libc = None

        def bind(name, restype, *argtypes):
            function = getattr(libc, name, None) if libc is not None else None
            if function is not None:
                function.restype, function.argtypes = restype, list(argtypes)
            return function

        size_p = ctypes.POINTER(ctypes.c_size_t)
        self._sysctl = bind("sysctl", ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
                            ctypes.c_void_p, size_p, ctypes.c_void_p, ctypes.c_size_t)
        self._listpids = bind("proc_listpids", ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
                              ctypes.c_void_p, ctypes.c_int)
        self._pidinfo = bind("proc_pidinfo", ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                             ctypes.c_void_p, ctypes.c_int)
        self._responsible = bind("responsibility_get_pid_responsible_for_pid", ctypes.c_int, ctypes.c_int)
        self.readable = None not in (self._sysctl, self._listpids, self._pidinfo)
        #: The responsibility SPI resolved (D-CEN1); without it no root is recorded.
        self.responsibility = self.readable and self._responsible is not None
        self._argument_max: int | None = None

    def pids(self) -> frozenset[int]:
        """Every pid now, from `proc_listpids`; `InspectionError` when it cannot say."""
        if not self.readable:
            raise InspectionError("process listing unavailable")
        for _ in range(3):
            needed = self._listpids(_PROC_ALL_PIDS, 0, None, 0)
            if needed <= 0:
                break
            listed = (ctypes.c_int * (needed // ctypes.sizeof(ctypes.c_int) + 512))()
            got = self._listpids(_PROC_ALL_PIDS, 0, listed, ctypes.sizeof(listed))
            if got <= 0:
                break
            if got < ctypes.sizeof(listed):      # room to spare: nothing was cut off
                return frozenset(pid for pid in listed[:got // ctypes.sizeof(ctypes.c_int)] if pid > 0)
        raise InspectionError("process listing unavailable")

    def process(self, pid: int) -> KernelProcess | None:
        """`pid`'s state, or None when there is no live process there to read: it is
        gone or a zombie (ESRCH), or another user's (EPERM, which `ps -E` meets too
        and skips). Any other failure is `InspectionError`."""
        if not self.readable:
            raise InspectionError("process inspection unavailable")
        info = ctypes.create_string_buffer(_BSDINFO_WITH_UNIQID_SIZE)
        ctypes.set_errno(0)
        got = self._pidinfo(pid, _PROC_PIDT_BSDINFOWITHUNIQID, 0, info, len(info))
        failure = ctypes.get_errno()
        if got == len(info):
            flags, status, _, shown = struct.unpack_from("=IIII", info.raw)
            if shown != pid:
                raise InspectionError(f"process inspection of pid {pid} described pid {shown}")
            if status == _SZOMB:
                return None
            return KernelProcess(pid, bool(flags & _PROC_FLAG_INEXIT),
                                 struct.unpack_from("=Q", info.raw, _UNIQUEID_OFFSET)[0])
        if got <= 0 and failure in (errno.ESRCH, errno.EPERM):
            return None
        raise InspectionError(f"process inspection of pid {pid} unavailable")

    def arguments(self, pid: int, buffer) -> bytes | None:
        """`pid`'s raw `KERN_PROCARGS2` buffer, or None when the kernel refused it. It
        refuses one reason as another (EINVAL for a gone, zombie or other user's
        process alike), so the caller asks `process` again to tell them apart. A
        full read or ENOMEM retries with KERN_ARGMAX when it exceeds `buffer`.
        A read still filling that maximum may be cut short: `InspectionError`."""
        if not self.readable:
            raise InspectionError("argument inspection unavailable")
        name = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
        for retry in (False, True):
            size = ctypes.c_size_t(len(buffer))
            ctypes.set_errno(0)
            rc = self._sysctl(name, 3, buffer, ctypes.byref(size), None, 0)
            if rc == 0 and size.value < len(buffer):
                return ctypes.string_at(buffer, size.value)
            if rc != 0 and ctypes.get_errno() != errno.ENOMEM:
                return None
            if not retry:
                maximum = self.argument_max()
                if maximum > len(buffer):
                    buffer = ctypes.create_string_buffer(maximum)
                    continue
            raise InspectionError(f"argument buffer of pid {pid} fills the read")

    def argument_max(self) -> int:
        """The kernel's allocation ceiling for PROCARGS2, read in process, once."""
        if self._argument_max is None:
            name = (ctypes.c_int * 2)(_CTL_KERN, _KERN_ARGMAX)
            value = ctypes.c_int()
            size = ctypes.c_size_t(ctypes.sizeof(value))
            if (self._sysctl(name, 2, ctypes.byref(value), ctypes.byref(size), None, 0) != 0
                    or size.value != ctypes.sizeof(value) or value.value <= 0):
                raise InspectionError("kernel argument maximum unavailable")
            self._argument_max = value.value
        return self._argument_max

    def responsible(self, pid: int) -> int | None:
        """The pid of `pid`'s responsible process, or None when it is gone or another
        user's (ESRCH, EPERM); `InspectionError` for anything else."""
        if not self.responsibility:
            raise InspectionError("responsibility inspection unavailable")
        ctypes.set_errno(0)
        found = self._responsible(pid)
        failure = ctypes.get_errno()
        if found > 0:
            return found
        if failure in (errno.ESRCH, errno.EPERM):
            return None
        raise InspectionError(f"responsibility of pid {pid} unavailable")


#: The reader every census uses; tests replace it.
kernel = Darwin()
RESPONSIBILITY_ROUNDS = 8


def responsible_for(root: int) -> frozenset[int]:
    """Every other live process whose responsible process is `root`: what a guardian
    started as a responsibility root waits on after its exit receipt (D-CEN1).
    Re-list after reading: a member can fork and exit between its listing and its
    read. Its child was born before that read and appears in the next listing.
    `InspectionError` on failed reads or continuous births exhausting the bounded
    rounds; neither proves emptiness."""
    read = {root}
    for _ in range(RESPONSIBILITY_ROUNDS):
        fresh = kernel.pids() - read
        if not fresh:
            return frozenset()
        named = set()
        for pid in sorted(fresh):
            if kernel.responsible(pid) == root:
                named.add(pid)
        if named:
            return frozenset(named)
        read.update(fresh)
    raise InspectionError("responsibility listing did not settle")


def _kernel_sources(kernel_reader, candidates: list[int], marker: tuple[bytes, bytes | None] | None,
                    root: ResponsibleRoot | None, ours: Callable[[KernelProcess], bool],
                    marker_observer: Callable[[int, bytes], bool] | None = None
                    ) -> tuple[set[int], set[int], list[str]]:
    """One pass over `candidates` for C-5.5's marker and responsibility sources:
    (marked pids, pids whose responsible process is `root`, errors). `marker` None
    skips the first, `root` None the second; `ours` says whether a process is
    recent enough to be the root's (`ResponsibleRoot`)."""
    marked: set[int] = set()
    responsible: set[int] = set()
    errors: list[str] = []
    buffer = ctypes.create_string_buffer(_ARGS_BUFFER) if marker is not None else None
    # Read the holder before its purported members without adding another read.
    # A reused pid can name itself and newly born strangers as responsible.
    ceiling = None
    for pid in sorted(candidates, key=lambda pid: (root is None or pid != root.pid, pid)):
        try:
            current = kernel_reader.process(pid)
        except InspectionError:
            errors.append(f"process inspection unavailable for pid {pid}")
            continue
        if current is None:
            continue                       # gone, a zombie, or another user's
        if root is not None and pid == root.pid and root.unique_id is not None:
            if current.unique_id != root.unique_id:
                ceiling = current.unique_id
        if marker is not None:
            try:
                found = kernel_reader.arguments(pid, buffer)
            except InspectionError:                # a buffer that may be cut short
                errors.append(f"marker read unavailable for pid {pid}")
            else:
                if found is not None:
                    if (marker_observer(pid, found) if marker_observer else procargs_marked(found, *marker)):
                        marked.add(pid)
                else:
                    # The kernel's refusal does not say why: ask again. A process
                    # that has since gone, become a zombie or begun to exit (its
                    # arguments are torn down with it, and it runs no more code)
                    # is no writer; one still there is a read that failed.
                    try:
                        again = kernel_reader.process(pid)
                    except InspectionError:
                        again = current
                    if again is not None and not again.exiting:
                        errors.append(f"marker read unavailable for pid {pid}")
        if root is not None:
            try:
                boss = kernel_reader.responsible(pid)
            except InspectionError:
                errors.append(f"responsibility read unavailable for pid {pid}")
                continue
            if (boss == root.pid and ours(current)
                    and (ceiling is None or root.unique_id <= current.unique_id < ceiling)):
                responsible.add(pid)
    return marked, responsible, errors


def containment(pgid: int | None, guardian_pid: int | None, child_pid: int | None,
                attempt_id: str, root: str | None = None, *,
                recorded: dict[int, ProcessIdentity | Sequence[ProcessIdentity]] | None = None,
                owned_identities: Sequence[ProcessIdentity] = (),
                ambiguous_identities: Sequence[ProcessIdentity] = (),
                launch_boot_id: str | None = None,
                lineage_boot_ids: Sequence[str] = (),
                child_unrecorded: bool = False,
                workdir: str | None = None,
                lineage_roots: Sequence[CensusRoot] = (),
                lineage_overflow_boot: str | None = None,
                guardian_identity: ProcessIdentity | None = None,
                child_identity: ProcessIdentity | None = None,
                foreign_ownership: ForeignOwnership | Callable[[ProcessTable | None], ForeignOwnership] = ForeignOwnership(),
                recorded_identities: dict[int, ProcessIdentity] | None = None,
                started_at: str | None = None, finished_at: str | None = None,
                responsible_root: ResponsibleRoot | None = None) -> Containment:
    """Collect C-5.5 sources; failed sources or needed identity reads prevent release.

    General identities describe the census, not authority to signal. Only
    group_identities preserves complete original-group table observations;
    callers must confirm the leader and member before recording ownership.
    Newly discovered escaped identities remain conservative census evidence.

    The group and the descendant walk are read from one process-table snapshot
    (`ps -axo pid=,ppid=,pgid=,stat=,lstart=`), so a process cannot be present
    in one source and absent from the other because it exited between two reads.
    The snapshot also gives every live pid a shape (parent, group, state) that
    the census records as evidence, and the start time that is its identity.
    The marker source reads each process's raw argument buffer in this process
    (`Darwin`), and so does the fourth source, every process whose responsible
    process is `responsible_root` (D-CEN1), for an attempt that recorded one;
    alongside one bounded cwd `lsof` and fresh per-PID identities bracketing
    late matches (C-5.12).
    Commands and environments are never retained.
    """
    groups: set[int] = set()
    descendants: set[int] = set()
    markers: set[int] = set()
    cwds: set[int] = set()
    providers: tuple[ProcessIdentity, ...] = ()
    excluded_identities: set[ProcessIdentity] = set()
    ambiguous: set[ProcessIdentity] = set()
    responsible: set[int] = set()
    errors: list[str] = []
    recycled: dict[int, dict[str, Any]] = {}
    checked: dict[int, bool] = {}
    roots_rebooted = False
    # Both APIs contribute observations; a newer observation never overwrites
    # an older one, and census evidence never becomes signal authority.
    recorded = {pid: ([values] if isinstance(values, ProcessIdentity) else list(values))
                for pid, values in (recorded or {}).items()}
    for known in (*(recorded_identities or {}).values(), guardian_identity, child_identity):
        if known is not None and known not in recorded.setdefault(known.pid, []):
            recorded[known.pid].append(known)
    records_by_pid = {pid: list(values) for pid, values in recorded.items()}
    for known in lineage_roots:
        if known.identity not in records_by_pid.setdefault(known.pid, []):
            records_by_pid[known.pid].append(known.identity)
    try:
        seen: ProcessTable | None = snapshot()
        table = seen.rows
    except InspectionError:
        seen, table = None, {}
        errors.append(GROUP_UNAVAILABLE)
        errors.append(DESCENDANT_UNAVAILABLE)

    def live(pid: int) -> bool:
        return pid in table and not table[pid][2].startswith("Z")

    def compare_root(pid: int) -> bool:
        """Prune only when every recorded incarnation is comparably different.

        Absent/zombie roots still anchor parent links. Receipt timestamps are
        accepted for compatibility, but wall-clock bounds cannot prove reuse.
        """
        if pid not in table or seen is None:
            return False
        began = table[pid][3]
        observations = records_by_pid.get(pid, ())
        needs_comparison = live(pid) or (pid == pgid and bool(seen.group(pgid)))
        reasons = []
        for known in observations:
            if (roots_rebooted and boot_identity.session_uuid(known.boot_id) is not None
                    and boot_identity.session_uuid(known.boot_id) == boot_identity.session_uuid(launch_boot_id)):
                reasons.append("different boot identity")
                continue
            if not known.proc_start:
                if needs_comparison:
                    errors.append(f"recorded identity unavailable for pid {pid}")
                return False
            if not began:
                if needs_comparison:
                    errors.append(f"start identity unavailable for root pid {pid}")
                return False
            if began != known.proc_start:
                reasons.append("different start identity")
                continue
            if not known.boot_id:
                if needs_comparison:
                    errors.append(f"recorded identity unavailable for pid {pid}")
                return False
            if not needs_comparison:
                return False
            try:
                match = seen.boot_matches(known.boot_id)
                if match is None:
                    errors.append(f"boot identity unknown for root pid {pid}")
                    return False
                if match is True:
                    return False
                reasons.append("different boot identity")
            except InspectionError:
                errors.append(f"boot identity unavailable for root pid {pid}")
                return False
        if not observations:
            if needs_comparison and not roots_rebooted:
                errors.append(f"recorded identity unavailable for pid {pid}")
            return False
        try:
            now_boot = seen.boot()
        except InspectionError:
            now_boot = None  # A comparable start mismatch alone proves reuse.
        recycled[pid] = {
            "recorded": asdict(observations[0]),
            "now": {"pid": pid, "boot_id": now_boot, "proc_start": began},
            "reason": reasons[0],
        }
        return True

    def is_recycled(pid: int) -> bool:
        if pid not in checked:
            checked[pid] = compare_root(pid)
        return checked[pid]

    local_seen: dict[int, ProcessIdentity] = {}
    proven_roots = set()
    genuine_descendants = ()
    resolved_roots = tuple(known for known in lineage_roots if seen.root_resolved(known)) if seen is not None else ()
    resolved_set = set(resolved_roots)
    lineage_roots = tuple(known for known in lineage_roots if known not in resolved_set)
    resolved_identities = tuple(known for values in (recorded or {}).values()
                                for known in ([values] if isinstance(values, ProcessIdentity) else values)
                                if seen.identity_gone(known)) if seen is not None else ()
    if callable(foreign_ownership):
        foreign_ownership = foreign_ownership(seen)
    if seen is not None:
        protected = protected_pids(seen, (*owned_identities, *ambiguous_identities))
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
                        break
                except InspectionError:
                    pass
        # A retained census observation at a reused launch PID is a hold,
        # not proof that the current incarnation belongs to this attempt.
        for known in (guardian_identity, child_identity):
            if known is not None:
                try:
                    if seen.is_process(known.pid, known.boot_id, known.proc_start, legacy=True):
                        proven_roots.add(known.pid)
                except InspectionError:
                    pass
        guardian_verified = False
        if guardian_identity is not None:
            try:
                guardian_verified = seen.is_process(guardian_pid, guardian_identity.boot_id,
                                                     guardian_identity.proc_start, legacy=True)
            except InspectionError:
                errors.append("guardian launch identity inspection unavailable")
        local_group = seen.group(pgid) if guardian_verified and table[guardian_pid][1] == pgid else ()
        genuine_descendants = foreign_ownership.local_descendants(seen, tuple(proven_roots),
                                                                 group_pids=local_group)
        protected.update(genuine_descendants)
        launch_boundaries = tuple(known.pid for known in (guardian_identity, child_identity)
                                  if known is not None and known.pid in proven_roots
                                  and table[known.pid][1] == pgid)
        claimed_local = tuple(pid for pid in protected if foreign_ownership.claimed(seen.census_root(pid).identity))
        observed_foreign = foreign_ownership.pids(seen, protected=(*launch_boundaries, *claimed_local))
        # The original group can outlive its guardian before paced recording.
        # Its observed members also hold on a foreign claim, without becoming
        # authenticated ownership or overriding a live reused group leader.
        leaderless_group = set()
        if pgid and not seen.live(pgid) and launch_boot_id:
            try:
                if boot_identity.matches(launch_boot_id, seen.boot(), seen.legacy_seconds) is not False:
                    leaderless_group.update(seen.group(pgid))
            except InspectionError:
                leaderless_group.update(seen.group(pgid))
        for pid in protected | leaderless_group | protected_pids(seen, foreign_ownership.collisions):
            current = seen.census_root(pid).identity
            if (pid in observed_foreign or foreign_ownership.claimed(current) or any(identities_overlap(known, current)
                                                        for known in ambiguous_identities)):
                ambiguous.add(current)
                protected.add(pid)
        safe_roots = tuple(pid for pid in proven_roots if seen.census_root(pid).identity not in ambiguous)
        safe_group = local_group if guardian_identity is not None and guardian_identity.pid not in {value.pid for value in ambiguous} else ()
        authenticated_descendants = foreign_ownership.local_descendants(seen, safe_roots, group_pids=safe_group)
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
                if current is not None and previous != current:
                    return seen.boot_matches(known) is False
            except InspectionError:
                return False
            return current is not None and previous != current

        roots_rebooted = rebooted(launch_boot_id)
        # C-5.3/C-5.7: a PID's new incarnation is not a recorded writer.
        # Read all recorded writers from this same snapshot, including escaped
        # writers whose parent links and markers no longer identify them.
        gone, owned, uncertain_roots = set(), set(), set()
        for pid, observations in records_by_pid.items():
            if pid in excluded:
                continue
            try:
                if is_recycled(pid) or not live(pid):
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
                            match = seen.boot_matches(known.boot_id)
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
        if lineage_overflow_boot is not None and not rebooted(lineage_overflow_boot):
            errors.append("lineage root limit exceeded")
        # A verified guardian's direct children establish the legacy provider
        # identities before signals or finalization can erase the parent link.
        if guardian_verified and guardian_pid not in {value.pid for value in ambiguous}:
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
        if pgid and is_recycled(pgid):
            groups.clear()
        # A matching identity also observes its current group immediately.
        # This keeps the census stable after that observation is persisted and
        # follows a writer that changed groups since the earlier inspection.
        recorded_groups = set()
        for pid in owned | uncertain_roots:
            recorded_groups.update(seen.group(table[pid][1]))
        # A sampled group can belong to a different incarnation than its
        # retained identity after a failed bracket. Leader identity/reuse
        # cannot discharge it; hold until the group is empty or rebooted.
        for known in lineage_roots:
            if rebooted(known.boot_id):
                continue
            # Sampled groups seed the same descendant walk as groups learned
            # from recorded writers. Persisting a root must not move unchanged
            # processes into the original launch-group diagnostic on recheck.
            recorded_groups.update(seen.group(known.pgid))
        # There is no recorded group before setsid. The two remaining sources
        # still enumerate the guardian and any inherited marker.
        roots = ({pid for pid in {*records_by_pid, guardian_pid, child_pid}
                  if pid and pid > 0 and not is_recycled(pid) and not roots_rebooted}
                  | owned | groups | uncertain_roots | recorded_groups)
        groups.difference_update(excluded)
        descendants = set(seen.descendants(tuple(roots), excluded=excluded))
    else:
        foreign_seen = {}
        # An outage cannot turn a known collision into signal authority.
        ambiguous.update(known for known in owned_identities if foreign_ownership.claimed(known))
        ambiguous.update(ambiguous_identities)
        if foreign_ownership.groups:
            for known in owned_identities:
                try:
                    current = identity(known.pid)
                    if (current is not None and identities_overlap(known, current)
                            and foreign_ownership.observes(current, ambiguous=tuple(ambiguous))):
                        ambiguous.add(current)
                except InspectionError:
                    pass  # The census outage already holds containment.
    # PID sets describe sources only. Durable observations are keyed by the
    # full identity, since multiple incarnations can appear in one census.
    identities: dict[int, ProcessIdentity] = {}
    group_identities: dict[int, ProcessIdentity] = {}
    observed_groups: dict[ProcessIdentity, set[int]] = {}
    incomplete_roots: list[CensusRoot] = []
    shapes: dict[int, dict[str, Any]] = {}
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
                if pid in authenticated_descendants and current not in ambiguous:
                    proven_descendants.append(current)
                if row[1] == pgid:
                    if current not in ambiguous:
                        group_identities[pid] = current
        except InspectionError:
            errors.append(f"identity inspection unavailable for pid {pid}")
            incomplete_roots.append(seen.census_root(pid))

    def observe_later(pid: int, source: set[int], *, foreign: ProcessIdentity | None = None,
                      local_marker: bool = False) -> None:
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
            collision = (any(identities_overlap(known, current) for known in (*ambiguous_identities, *ambiguous))
                         or foreign_ownership.ambiguous(current)
                         or ((local or local_marker) and foreign_ownership.claimed(current)))
            if collision:
                ambiguous.add(current)
                excluded_identities.discard(current)
                local = True
            if current == foreign and not local:
                source.discard(pid)
                return
            group = process_group(pid)
            confirmed = identity(pid)
            if confirmed != current or group is None:
                if confirmed is not None and confirmed != current:
                    retain(confirmed, 0)
                raise InspectionError("process changed during group inspection")
            # The current identity already brackets this group read. Foreign
            # group proof can use it without consuming another scalar capture.
            if local_marker and foreign_ownership.owns(
                    current, group, ambiguous=(*ambiguous_identities, *ambiguous)):
                ambiguous.add(current)
                excluded_identities.discard(current)
                local = True
            if not local and (current == foreign_seen.get(pid) or foreign_ownership.owns(
                    current, group, ambiguous=(*ambiguous_identities, *ambiguous))):
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
        # C-5.5: attempt ids are a timestamp and a slug, so two daemons (or two
        # test state roots) can mint the same id in the same second. The state
        # root is the second half of the marker whenever the caller has one.
        wanted: tuple[bytes, bytes | None] | None = marker_strings(attempt_id, root)
    except ValueError:
        wanted = None
        errors.append("marker enumeration unavailable")
    attempt_marked: set[int] = set()

    def marked(pid: int, raw: bytes) -> bool:
        strings = procargs_strings(raw)
        attempts = [value for value in strings if value.startswith(_ATTEMPT_PREFIX)
                    and value != _ATTEMPT_PREFIX]
        exact = wanted is not None and wanted[0] in strings
        root_match = wanted is not None and wanted[1] is not None and wanted[1] in strings
        local = exact and (wanted[1] is None or root_match)
        if local:
            attempt_marked.add(pid)
        foreign_marker = bool(attempts) and not local
        if foreign_marker:
            try:
                earlier = seen.identity(pid) if seen is not None and pid in table else None
                current = identity(pid) if earlier is not None or workdir is not None else None
                if current is not None and current == earlier:
                    if current in owned_identities or current == local_seen.get(pid):
                        ambiguous.add(current)
                    else:
                        foreign_attempts[pid] = current
                elif current is not None and earlier is not None:
                    ambiguous.add(current)
                elif current is not None and pid not in table and workdir is not None:
                    # A late waiter needs a second raw marker read bracketed by
                    # identities before its shared cwd can be excluded.
                    confirmed = kernel.arguments(pid, ctypes.create_string_buffer(_ARGS_BUFFER))
                    values = procargs_strings(confirmed) if confirmed is not None else []
                    other = any(value.startswith(_ATTEMPT_PREFIX) and value != _ATTEMPT_PREFIX
                                for value in values)
                    own = ((wanted[0] in values and (wanted[1] is None or wanted[1] in values))
                           or (wanted[1] is not None and wanted[1] in values and not other))
                    if other and not own and identity(pid) == current:
                        foreign_attempts[pid] = current
                    elif own:
                        if wanted[0] in values:
                            attempt_marked.add(pid)
                        return True
            except InspectionError:
                pass                 # Failed foreign proof can never exclude cwd.
            return False
        return local or (root_match and not attempts)

    order: list[bool | None] = []

    def ours(process: KernelProcess) -> bool:
        """D-CEN1: is a process reporting the root as responsible young enough to be
        its? Unique ids order a boot's processes, so one created before the root is
        another process's, at a pid the root's later took. Only a proven other boot
        excludes the rest (none of the root's processes outlives its boot); an order
        that cannot be read keeps them (C-5.3)."""
        if responsible_root is None:
            return True
        if not order:
            match = None
            if responsible_root.boot_id:
                recorded = str(responsible_root.boot_id)
                seconds = seen.legacy_seconds if seen is not None else lambda: boot_identity.boot_seconds(_read)
                try:
                    match = boot_identity.matches(recorded, seen.boot() if seen is not None else boot_id(),
                                                  seconds)
                    if match is False:
                        forget_boot_id()     # C-5.12: a cached mismatch is read afresh first
                        match = boot_identity.matches(recorded, boot_id(), seconds)
                except InspectionError:
                    match = None
            order.append(match)
        if order[0] is False:
            return False
        return (order[0] is None or responsible_root.unique_id is None
                or process.unique_id >= responsible_root.unique_id)

    # The table's start/boot comparison is stronger than unique-id ordering, and
    # also protects legacy roots whose unique id was unavailable at launch.
    source_root = responsible_root
    if (source_root is not None and source_root.pid in records_by_pid
            and is_recycled(source_root.pid)):
        source_root = None
    if wanted is not None or source_root is not None:
        try:
            # The snapshot's pids and a listing taken after it, so a process born
            # since the snapshot is read too; a pid gone by then reads as gone.
            candidates = sorted(kernel.pids() | {pid for pid in table if live(pid)})
        except InspectionError:
            candidates = []
            if wanted is not None:
                errors.append("marker enumeration unavailable")
            if source_root is not None:
                errors.append("responsibility enumeration unavailable")
        # Never retain or report what these reads return but the pids.
        found_marked, found_responsible, failed = _kernel_sources(
            kernel, candidates, wanted, source_root, ours, marked)
        # Preserve a known snapshot boot for diagnostics even if the positive
        # raw match exits before identity capture. Do not read a boot solely
        # for a process already gone.
        if (found_marked or found_responsible) and seen is not None and seen.boot_id:
            observed_boots.add(seen.boot_id)
        for found, source, name in ((found_marked, markers, "marker"),
                                    (found_responsible, responsible, "responsibility")):
            for pid in sorted(found):
                if pid not in table:
                    try:
                        state = _stat(pid)
                    except InspectionError:
                        errors.append(f"{name} enumeration unavailable")
                        # The positive raw observation must still retain an
                        # identity/group root if its fresh inspection fails.
                    else:
                        if not state or state.startswith("Z"):
                            continue
                # A shared root alone does not contradict verified ownership
                # by another attempt. Exact attempt markers and responsibility
                # members still retain collisions as conservative holds.
                observe_later(pid, source, local_marker=pid in attempt_marked or source is responsible)
        observed_writer = observed_writer or bool(markers | responsible)
        errors.extend(failed)
    if workdir is not None:
        try:
            for pid in cwd_pids(workdir):
                observe_later(pid, cwds, foreign=foreign_attempts.get(pid))
        except InspectionError:
            errors.append(CWD_UNAVAILABLE)
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
    if seen is not None:
        safe_roots = tuple(pid for pid in proven_roots if not any(
            identities_overlap(seen.census_root(pid).identity, value) for value in ambiguous))
        guardian_ambiguous = guardian_verified and any(
            identities_overlap(seen.census_root(guardian_pid).identity, value) for value in ambiguous)
        if guardian_ambiguous:
            providers = ()
        authenticated_descendants = foreign_ownership.local_descendants(
            seen, safe_roots, group_pids=local_group if guardian_verified and not guardian_ambiguous else ())
        proven_descendants = [value for value in proven_descendants if value.pid in authenticated_descendants]
    providers = tuple(value for value in providers if value not in ambiguous)
    group_identities = {pid: value for pid, value in group_identities.items() if value not in ambiguous}
    return Containment(frozenset(groups), frozenset(descendants), frozenset(markers),
                       bool(errors), identities, tuple(errors), shapes, tuple(sorted(observed_boots)),
                       frozenset(cwds), captured, providers, group_identities, tuple(excluded_identities),
                       tuple(value for value in proven_descendants if value not in ambiguous),
                       tuple(sorted(ambiguous, key=lambda value: (value.pid, value.boot_id, value.proc_start))),
                       foreign_ownership, resolved_roots, resolved_identities, recycled, frozenset(responsible))


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


def signal_process(recorded: ProcessIdentity, sig: int | signal.Signals, *,
                   authorize: Callable[[], ContextManager[bool]] | None = None) -> bool:
    """Signal a survivor only with previously recorded ownership (C-5.6)."""
    if recorded.pid <= 1 or recorded.pid == os.getpid():
        return False
    if not same_process(recorded.pid, recorded.boot_id, recorded.proc_start):
        return False
    try:
        if authorize is not None:
            # Finish scalar reads before entering the claim-publication gate.
            # Group membership grants no authority to signal other members.
            os.getpgid(recorded.pid)
            with authorize() as allowed:
                if not allowed:
                    return False
                # Authorization can import receipts. Reconfirm after that I/O
                # while publications remain excluded through the syscall.
                if not same_process(recorded.pid, recorded.boot_id, recorded.proc_start):
                    return False
                os.kill(recorded.pid, sig)
        else:
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


class _Spawn:
    """libSystem's `posix_spawn` and the attributes `spawn_disclaimed` sets, or None
    for each that this macOS does not have. `available` needs them all: without one
    the guardian is started as before, with no responsibility root."""

    def __init__(self) -> None:
        try:
            libc = ctypes.CDLL(None, use_errno=True)
        except OSError:
            libc = None
        pointer = ctypes.c_void_p
        self.functions = {}
        for name, argtypes in (
                ("posix_spawn", [ctypes.POINTER(ctypes.c_int), ctypes.c_char_p, pointer, pointer,
                                 ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_char_p)]),
                ("posix_spawnattr_init", [pointer]),
                ("posix_spawnattr_destroy", [pointer]),
                ("posix_spawnattr_setflags", [pointer, ctypes.c_short]),
                ("posix_spawnattr_setsigdefault", [pointer, ctypes.POINTER(ctypes.c_uint32)]),
                ("responsibility_spawnattrs_setdisclaim", [pointer, ctypes.c_bool]),
                ("posix_spawn_file_actions_init", [pointer]),
                ("posix_spawn_file_actions_destroy", [pointer]),
                ("posix_spawn_file_actions_addopen", [pointer, ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                                                     ctypes.c_uint16]),
                ("posix_spawn_file_actions_addinherit_np", [pointer, ctypes.c_int]),
                ("posix_spawn_file_actions_addchdir_np", [pointer, ctypes.c_char_p])):
            function = getattr(libc, name, None) if libc is not None else None
            if function is not None:
                function.restype, function.argtypes = ctypes.c_int, argtypes
            self.functions[name] = function
        self.available = (sys.platform == "darwin" and kernel.responsibility
                          and None not in self.functions.values())

    def __getattr__(self, name: str):
        functions = self.__dict__.get("functions", {})
        if name not in functions:
            raise AttributeError(name)
        return functions[name]


_spawn = _Spawn()
#: posix_spawn.h (macOS): reset these signals to default, and close every
#: descriptor no file action names.
_POSIX_SPAWN_SETSIGDEF = 0x0004
_POSIX_SPAWN_CLOEXEC_DEFAULT = 0x4000
_POSIX_SPAWN_SETSID = 0x0400


def disclaim_available() -> bool:
    """Can guardians be started as responsibility roots here (D-CEN1)?"""
    return bool(_spawn.available)


def guardian_disclaim_allowed() -> bool:
    """Disclaim only from a self-responsible daemon. A daemon started by a
    terminal must preserve that terminal's responsibility and privacy grants.
    An unreadable identity cannot authorize changing that attribution."""
    if not disclaim_available():
        return False
    try:
        return kernel.responsible(os.getpid()) == os.getpid()
    except InspectionError:
        return False


class Spawned:
    """A process `spawn_disclaimed` started: the part of `subprocess.Popen` the daemon
    and its tests use (`pid`, `args`, `returncode`, `poll`, `wait`, `send_signal`,
    `terminate`, `kill`), reaped with `os.waitpid` as Popen reaps. One dropped while it
    still runs is kept and reaped by a later `spawn_disclaimed`, as Popen keeps one."""

    def __init__(self, args: list[str], pid: int):
        self.args, self.pid, self.returncode = args, pid, None
        self._waitpid_lock = threading.Lock()

    def _reaped(self, waited: int, status: int) -> None:
        if waited == self.pid:
            self.returncode = os.waitstatus_to_exitcode(status)

    def poll(self) -> int | None:
        if self.returncode is None and self._waitpid_lock.acquire(False):
            try:
                if self.returncode is None:
                    try:
                        self._reaped(*os.waitpid(self.pid, os.WNOHANG))
                    except ChildProcessError:
                        self.returncode = 0          # reaped elsewhere, as Popen says
            finally:
                self._waitpid_lock.release()
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if timeout is None:
            with self._waitpid_lock:
                while self.returncode is None:
                    try:
                        self._reaped(*os.waitpid(self.pid, 0))
                    except ChildProcessError:
                        self.returncode = 0
            return self.returncode
        deadline, delay = time.monotonic() + timeout, .0005
        while self.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(min(delay, remaining, .05))
            delay *= 2
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.poll()
        if self.returncode is None:
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                pass

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)

    def __del__(self) -> None:
        try:
            if self.returncode is None:
                _abandoned.append(self)        # still running: reaped by a later spawn
        except Exception:                      # interpreter shutdown
            pass


_abandoned: list[Spawned] = []


def spawn_disclaimed(argv: list[str], *, env: dict[str, str], cwd: str,
                     pass_fds: tuple[int, ...] = ()) -> Spawned | subprocess.Popen:
    """Start `argv` as its own responsible process (D-CEN1): what Popen does for a
    guardian (`stdin`, `stdout` and `stderr` on /dev/null, every descriptor closed but
    `pass_fds`, `cwd`, exactly `env`, SIGPIPE and SIGXFSZ at their defaults) with
    `posix_spawn`, which also never forks the daemon (C-5.12), and
    `responsibility_spawnattrs_setdisclaim`, which Popen cannot set. Every process the
    guardian starts, and they start, then names it as responsible however it leaves
    the group, the parent links or its own environment, and C-5.5's fourth source
    finds them by it.

    Without the attribute (`disclaim_available()` is False) it is Popen, as before,
    and the caller records no root. Raises OSError, as Popen does, when the
    executable cannot be started."""
    for child in list(_abandoned):
        if child.poll() is not None:
            try:
                _abandoned.remove(child)
            except ValueError:             # another spawn reaped it first
                pass
    if not _spawn.available:
        return subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, pass_fds=pass_fds, close_fds=True, cwd=cwd, start_new_session=True)
    args = [os.fsencode(part) for part in argv]
    envp = []
    for key, value in env.items():
        key_b, value_b = os.fsencode(key), os.fsencode(value)
        if not key_b or b"=" in key_b:
            raise ValueError("illegal environment variable name")
        envp.append(key_b + b"=" + value_b)
    if any(b"\0" in part for part in (*args, *envp, os.fsencode(cwd))):
        raise ValueError("embedded null byte")

    def check(rc: int) -> None:
        if rc:
            raise OSError(rc, os.strerror(rc))
    attr, actions = ctypes.c_void_p(), ctypes.c_void_p()
    check(_spawn.posix_spawnattr_init(ctypes.byref(attr)))
    try:
        check(_spawn.posix_spawn_file_actions_init(ctypes.byref(actions)))
        try:
            check(_spawn.posix_spawnattr_setflags(ctypes.byref(attr),
                                                  _POSIX_SPAWN_CLOEXEC_DEFAULT | _POSIX_SPAWN_SETSIGDEF | _POSIX_SPAWN_SETSID))
            # sigset_t is a 32-bit mask on macOS; signal n is bit n - 1.
            defaults = ctypes.c_uint32((1 << (signal.SIGPIPE - 1)) | (1 << (signal.SIGXFSZ - 1)))
            check(_spawn.posix_spawnattr_setsigdefault(ctypes.byref(attr), ctypes.byref(defaults)))
            check(_spawn.responsibility_spawnattrs_setdisclaim(ctypes.byref(attr), True))
            for fd, flags in ((0, os.O_RDONLY), (1, os.O_WRONLY), (2, os.O_WRONLY)):
                check(_spawn.posix_spawn_file_actions_addopen(ctypes.byref(actions), fd, b"/dev/null", flags, 0))
            for fd in pass_fds:
                check(_spawn.posix_spawn_file_actions_addinherit_np(ctypes.byref(actions), fd))
            check(_spawn.posix_spawn_file_actions_addchdir_np(ctypes.byref(actions), os.fsencode(cwd)))
            pid = ctypes.c_int()
            argv_c = (ctypes.c_char_p * (len(args) + 1))(*args, None)
            envp_c = (ctypes.c_char_p * (len(envp) + 1))(*envp, None)
            rc = _spawn.posix_spawn(ctypes.byref(pid), args[0], ctypes.byref(actions), ctypes.byref(attr),
                                    argv_c, envp_c)
            if rc:
                raise OSError(rc, os.strerror(rc), argv[0])
        finally:
            _spawn.posix_spawn_file_actions_destroy(ctypes.byref(actions))
    finally:
        _spawn.posix_spawnattr_destroy(ctypes.byref(attr))
    return Spawned(list(argv), pid.value)


def responsibility_root(pid: int) -> dict[str, int | None] | None:
    """What the daemon records for a guardian `spawn_disclaimed` started (D-CEN1):
    `{"pid", "unique_id"}` when the kernel names `pid` as its own responsible
    process, else None (a guardian Popen started, or the SPI is missing), and then
    the census keeps its three sources for that attempt. A unique id that cannot be
    read is recorded as None, which keeps every process naming the root (C-5.3).
    A failed responsibility read raises: it does not prove the child unrooted."""
    if not kernel.responsibility:
        return None
    if kernel.responsible(pid) != pid:
        return None
    try:
        found = kernel.process(pid)
    except InspectionError:
        found = None
    return {"pid": pid, "unique_id": found.unique_id if found is not None else None}
