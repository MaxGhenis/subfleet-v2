"""Recorded process identity and conservative macOS containment (C-5).

Process listings containing environments are consumed in memory and discarded;
only pid sets and start identities may become durable evidence.
"""

from __future__ import annotations

from typing import Callable, Iterable, Mapping

import calendar
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

_MONTHS = {name: number for number, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def start_seconds(lstart: str | None) -> int | None:
    """`ps`'s `lstart` as whole seconds since the epoch; None when it does not parse.

    `_read` renders it in the C locale and UTC ("Tue Sep 29 03:14:45 2026", the
    day padded with a space), so the month is read by its English name here
    rather than through `time.strptime`, which follows the process's locale.
    Only an order between two starts is taken from it (C-5.6), never an identity.
    """
    try:
        _, month, day, clock, year = str(lstart).split()
        hour, minute, second = (int(part) for part in clock.split(":"))
        if not (1 <= int(day) <= 31 and 0 <= hour < 24 and 0 <= minute < 60 and 0 <= second <= 60):
            return None
        return calendar.timegm((int(year), _MONTHS[month], int(day), hour, minute, second, 0, 0, 0))
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


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
    #: C-5.12: when the read began (`taken_at` is when it ended). A process born
    #: after `began_at` may be missing; one that died before it cannot be shown.
    began_at: float | None = None
    #: C-5.6: the wall clock (`time.time()`) when the read began. `ps` lists the
    #: pids first and fills each row later, so a process that started at or after
    #: this second is in the table only under a pid that changed hands mid-read,
    #: or is too new to vouch for; ownership never passes through it here.
    began_wall: float | None = None
    _boot: list = field(default_factory=list, init=False, repr=False, compare=False)
    _seconds: list = field(default_factory=list, init=False, repr=False, compare=False)
    _index: list = field(default_factory=list, init=False, repr=False, compare=False)
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

    def links(self) -> tuple[dict[int, tuple[int, ...]], dict[int, tuple[int, ...]]]:
        """(parent -> live children, group -> live members), built once per table.

        Every running attempt's inspection walks the one shared table (C-5.12),
        so its indexes are built on first use and kept for the table's life; two
        threads that build them at once build the same thing."""
        return self._indexes()[:2]

    def has_group(self, pgid: int) -> bool:
        """Does any process, a zombie included, belong to group `pgid`? A zombie
        keeps its group until it is reaped, and while a group has a member POSIX
        reuses neither its id as a pid nor the group (C-5.7b)."""
        return pgid in self._indexes()[2]

    def _indexes(self) -> tuple[dict[int, tuple[int, ...]], dict[int, tuple[int, ...]], frozenset[int]]:
        if not self._index:
            children: dict[int, list[int]] = {}
            members: dict[int, list[int]] = {}
            for pid, (ppid, pgid, _, _) in self.rows.items():
                if self.live(pid):
                    children.setdefault(ppid, []).append(pid)
                    members.setdefault(pgid, []).append(pid)
            self._index.append(({k: tuple(v) for k, v in children.items()},
                                {k: tuple(v) for k, v in members.items()},
                                frozenset(row[1] for row in self.rows.values())))
        return self._index[0]

    def shows(self, recorded: "ProcessIdentity") -> bool:
        """Is the recorded pid live here with its recorded start? No boot identity is
        read: a census counts a match as live whatever the boot (C-5.5, C-5.7b), which
        errs only toward holding on, since a reused pid starts at another time."""
        return self.live(recorded.pid) and self.rows[recorded.pid][3] == recorded.proc_start


def owned_closure(table: ProcessTable, roots: Iterable[ProcessIdentity], *,
                  deferred: set[int] | None = None) -> dict[int, ProcessIdentity]:
    """C-5.6: every process this one table proves the attempt owns, by identity.

    A root is owned when the table shows it alive by C-5.3 (pid, start, and boot,
    a legacy boot timestamp matched as `liveness` would). From the owned set the
    table proves two more kinds of process owned:

    - a live process whose parent in the table is owned, which is not being
      traced, and which started no earlier than that parent. On macOS a parent
      link names the process that forked it (an orphan goes to launchd, and there
      is no subreaper), except that `ptrace` attach lists a traced process under
      its tracer. `ps` marks a traced process `X`, so a stranger the job's
      debugger attached to is never owned through that link; the start order is
      a second check, since a process can never be forked before its parent;
    - a live member of a process group an owned process leads, which started no
      earlier than that leader. A group lies in one session and `setpgid` joins
      only a group of the caller's own session; the guardian's session (it calls
      `setsid`, C-5.1) and every session a process forked in it creates hold
      only processes forked inside them. The start order is needed because `ps`
      reads rows one at a time: a stranger's row can name group G, then G's
      last member leaves, and G's id goes to an owned process that makes a new
      group G before G's own row is read (review of 9d7d4f5b). Such a stranger
      started before that leader did. A process that joined an older owned
      group from within its session is owned through its parent link instead,
      if at all.

    Neither kind passes through a process that started at or after the second
    in which the table's read began (`began_wall`): `ps` lists the pids and then
    fills each row (XNU `proc_iterate`), so such a process is in the table only
    under a pid that changed hands during the read, where a row read earlier can
    name its pid or its group while meaning the old holder's; with a start in
    the same second the start order cannot tell (reviews of 9d7d4f5b and
    4c742703). A process that new is owned on the next table, as soon as it is
    older than that table's read; each one this table would otherwise have
    reached is added to `deferred`, so a caller that must decide now (the kill
    protocol) can read again once the second has turned.

    Returns identities as the table gives them. Raises `InspectionError` when a
    root's boot identity is needed and cannot be read; a root that is absent, a
    zombie, or another process needs none. Nothing here is authority to signal
    by itself: `signal_group` and `signal_process` still confirm the identity
    afresh (C-5.4).
    """
    children, members = table.links()
    cutoff = int(table.began_wall) if table.began_wall is not None else None

    def settled(pid: int) -> bool:
        started = start_seconds(table.rows[pid][3])
        return started is not None and (cutoff is None or started < cutoff)

    owned: dict[int, ProcessIdentity] = {}
    frontier: list[int] = []
    for root in roots:
        if root.pid in owned or not table.is_process(root.pid, root.boot_id, root.proc_start, legacy=True):
            continue
        current = table.identity(root.pid)
        if current is not None:
            owned[root.pid] = current
            frontier.append(root.pid)
    while frontier:
        pid = frontier.pop()
        since = start_seconds(table.rows[pid][3])
        found = [child for child in children.get(pid, ())
                 if since is not None and "X" not in table.rows[child][2]
                 and (start_seconds(table.rows[child][3]) or 0) >= since]
        if table.rows[pid][1] == pid:
            found.extend(member for member in members.get(pid, ())
                         if since is not None and (start_seconds(table.rows[member][3]) or 0) >= since)
        young = not settled(pid)                    # too new to vouch for anyone in this table
        for member in found:
            if member in owned:
                continue
            if young or not settled(member):
                if deferred is not None and table.live(member):
                    deferred.add(member)
                continue
            current = table.identity(member)
            if current is not None:
                owned[member] = current
                frontier.append(member)
    return owned


def snapshot() -> ProcessTable:
    """Read the process table once (C-5.5); raises `InspectionError` when it cannot.

    The boot identity is not read here but when the table first needs it."""
    rows: dict[int, tuple[int, int, str, str]] = {}
    began, began_wall = time.monotonic(), time.time()
    try:
        for row in _read(TABLE_ARGV).splitlines():
            parts = row.split(None, 4)
            if len(parts) < 4:
                continue
            rows[int(parts[0])] = (int(parts[1]), int(parts[2]), parts[3],
                                   parts[4].strip() if len(parts) > 4 else "")
    except ValueError as exc:
        raise InspectionError("ps printed a row that is not a process") from exc
    return ProcessTable(rows, began_at=began, began_wall=began_wall)


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
    # C-5.7b: recorded groups this snapshot proves ended (their id is held by
    # another process), and those it shows vacant (no process in them, their
    # leader gone), which a later snapshot must confirm (`group_state`).
    ended_groups: frozenset[int] = frozenset()
    vacant_groups: frozenset[int] = frozenset()

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
            "ended_groups": sorted(self.ended_groups),
            "vacant_groups": sorted(self.vacant_groups),
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


def group_state(table: ProcessTable, pgid: int, leader: ProcessIdentity | None) -> str:
    """C-5.5, C-5.6, C-5.7b: what one table says of a recorded group.

    `"reused"`: the group's id, which is its leader's pid, is held by a process
    with another start. XNU allocates no pid that is still a group's id
    (`kern_fork.c`), so the recorded group had ended before that process was
    born: proof, from one table, that the group is not the attempt's.
    `"live"`: some process, a zombie included, is in the group, or its recorded
    leader is alive and can make it again with `setpgid(0, 0)`.
    `"vacant"`: neither. One table is not proof: `ps` fills rows after listing
    the pids, so a leader that forks and exits mid-read can leave a member that
    no row shows. A caller persists the group as ended only once a table read
    after this one agrees (reviews of 9d7d4f5b and 4c742703).
    """
    row = table.rows.get(pgid)
    if leader is not None and row is not None and row[3] != leader.proc_start:
        return "reused"
    if table.has_group(pgid):
        return "live"
    if row is not None and not row[2].startswith("Z") and (leader is None or row[3] == leader.proc_start):
        return "live"
    return "vacant"


#: C-5.5: the environment scan. Its output holds every process's environment, so
#: it is consumed in memory and never kept or reported.
MARKER_ARGV = ["/bin/ps", "-axEww", "-o", "pid=,command="]


@dataclass(frozen=True)
class CensusReads:
    """One process table and one environment scan, for several censuses (C-5.7b).

    `table` is None when `ps` failed, and `marked` is None when the scan failed;
    each census given them then reports that source unavailable, as its own read
    would have. `marked` keeps only the scan's rows that carry some attempt's
    marker. Those rows hold environments, so they stay out of `repr` and are
    dropped with the object when the sweep ends.
    """
    table: ProcessTable | None
    marked: tuple[str, ...] | None = field(repr=False)


def census_reads() -> CensusReads:
    """Read the process table and the environment scan once (C-5.5, C-5.7b)."""
    try:
        table: ProcessTable | None = snapshot()
    except InspectionError:
        table = None
    try:
        marked: tuple[str, ...] | None = tuple(
            row for row in _read(MARKER_ARGV).splitlines() if "SUBFLEET_ATTEMPT=" in row)
    except InspectionError:
        marked = None
    return CensusReads(table, marked)


def containment(pgid: int | None, guardian_pid: int | None, child_pid: int | None,
                attempt_id: str, root: str | None = None, *,
                recorded: Iterable[ProcessIdentity] = (),
                groups: Mapping[int, ProcessIdentity | None] | None = None,
                guardian: ProcessIdentity | None = None,
                reads: CensusReads | None = None) -> Containment:
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

    `recorded` are identities the attempt recorded (C-5.6's owned processes;
    for a quarantine, also what its evidence lists, C-5.7b). Each one the snapshot
    shows live with its recorded start is a root of the walk, and when it leads
    its own group, that group's members join the group source: a tool session's
    shell whose environment `ps -E` hides (`/bin/zsh`) is still found by identity.

    `groups` are process groups the attempt recorded, each with the identity of
    the owned process that led it when it was recorded. Their members are
    counted even once that leader is gone, because XNU gives no new process a
    pid that is still a group's id. A group is not counted while the snapshot
    shows its id held by another process (another start): the recorded leader is
    gone and its group ended before the id was reused, so the group is reported
    in `ended_groups`, which is proof. A recorded group the snapshot shows no
    process in at all, zombies included, once its recorded leader is gone too,
    is reported in `vacant_groups`, which is not: a later snapshot must agree
    (`group_state`).

    `guardian`, when the attempt recorded one, makes the recorded pids answer
    only for themselves: the walk starts at the guardian only while the snapshot
    shows it with its recorded start, and the recorded group `pgid` is counted as
    `groups` are, with the guardian as its leader. `child_pid` is a walk root as
    given, with no identity check; callers that recorded the guardian pass None.

    `reads` are the sweep's shared reads (`census_reads`); without them the
    census reads its own.
    """
    members: set[int] = set()
    descendants: set[int] = set()
    markers: set[int] = set()
    ended: set[int] = set()
    vacant: set[int] = set()
    errors: list[str] = []
    if reads is None:
        try:
            seen: ProcessTable | None = snapshot()
        except InspectionError:
            seen = None
    else:
        seen = reads.table
    table = seen.rows if seen is not None else {}
    if seen is None:
        errors.append("group enumeration unavailable")
        errors.append("descendant enumeration unavailable")

    def live(pid: int) -> bool:
        return pid in table and not table[pid][2].startswith("Z")

    if seen is not None:
        alive = {ident.pid for ident in recorded if seen.shows(ident)}
        counted: dict[int, ProcessIdentity | None] = dict(groups or {})
        if pgid and pgid > 0:
            counted[pgid] = guardian if guardian is not None and guardian.pid == pgid else None
        _, by_group = seen.links()
        for group, leader in counted.items():
            state = group_state(seen, group, leader)
            if state == "reused":
                ended.add(group)                      # another process holds the id: the group ended
            elif state == "vacant":
                vacant.add(group)
            else:
                members.update(by_group.get(group, ()))
        for pid in alive:
            if table[pid][1] == pid:
                members.update(by_group.get(pid, ()))
        # There is no recorded group before setsid. The two remaining sources
        # still enumerate the guardian and any inherited marker.
        if guardian is None:
            roots = {pid for pid in (guardian_pid, child_pid) if pid and pid > 0}
        else:
            roots = {guardian.pid} if seen.shows(guardian) else set()
            roots.update(pid for pid in (child_pid,) if pid and pid > 0)
        roots.update(alive)
        found = set(roots)
        frontier = roots
        children, _ = seen.links()
        while frontier:
            frontier = {child for pid in frontier for child in children.get(pid, ()) if child not in found}
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
        if reads is None:
            rows: Iterable[str] = _read(MARKER_ARGV).splitlines()
        elif reads.marked is None:
            raise InspectionError("marker enumeration unavailable")
        else:
            rows = reads.marked
        # Never retain or report these command/environment strings.
        for row in rows:
            pid_text, _, command = row.strip().partition(" ")
            if marker.search(command) and (root_marker is None or root_marker.search(command)):
                pid = int(pid_text)
                state = table[pid][2] if pid in table else _stat(pid)
                if state and not state.startswith("Z"):
                    markers.add(pid)
    except (InspectionError, ValueError):
        errors.append("marker enumeration unavailable")
    identities: dict[int, ProcessIdentity] = {}
    for pid in members | descendants | markers:
        try:
            # The snapshot's own start time when it has one; a pid it could not
            # describe (a marker spawned after the read) is asked about singly.
            current = (seen.identity(pid) if seen is not None else None) or identity(pid)
            if current is not None:
                identities[pid] = current
            else:
                # A process can exit between census and identity capture.
                members.discard(pid)
                descendants.discard(pid)
                markers.discard(pid)
        except InspectionError:
            errors.append(f"identity inspection unavailable for pid {pid}")
    shapes = {pid: {"ppid": table[pid][0], "pgid": table[pid][1], "stat": table[pid][2]}
              for pid in members | descendants | markers if pid in table}
    return Containment(frozenset(members), frozenset(descendants), frozenset(markers),
                       bool(errors), identities, tuple(errors), shapes, frozenset(ended), frozenset(vacant))


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
