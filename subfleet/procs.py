"""Recorded process identity and conservative macOS containment (C-5).

Process listings containing environments are consumed in memory and discarded;
only pid sets and start identities may become durable evidence.
"""

from __future__ import annotations

from typing import Callable

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

    def descendants(self, roots: set[int]) -> frozenset[int]:
        """Live walk members, retaining absent and zombie parent-link anchors.

        Build the parent index once, so walking a deep tree costs one pass over
        the shared table rather than one pass per generation (C-5.12).
        """
        children: dict[int, list[int]] = {}
        for pid, row in self.rows.items():
            children.setdefault(row[0], []).append(pid)
        found = set(roots)
        pending = list(roots)
        while pending:
            for pid in children.get(pending.pop(), ()):
                if pid not in found:
                    found.add(pid)
                    pending.append(pid)
        return frozenset(pid for pid in found if self.live(pid))


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
    # Proven recycled roots/leaders, including identities excluded from the
    # live sets. No command or environment may enter this evidence (C-5.5).
    recycled: dict[int, dict[str, Any]] = field(default_factory=dict)
    # C-5.5's fourth source (D-CEN1): the live processes whose responsible
    # process is the attempt's recorded responsibility root.
    responsible_pids: frozenset[int] = frozenset()

    # Hold only: observers never become walk seeds or signal targets.
    cwd_pids: frozenset[int] = frozenset()

    @property
    def live_pids(self) -> frozenset[int]:
        return self.group_pids | self.descendant_pids | self.marker_pids | self.responsible_pids

    @property
    def verified_empty(self) -> bool:
        return not self.unverifiable and not self.live_pids and not self.cwd_pids

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_pids": sorted(self.group_pids),
            "descendant_pids": sorted(self.descendant_pids),
            "marker_pids": sorted(self.marker_pids),
            "responsible_pids": sorted(self.responsible_pids),
            "live_pids": sorted(self.live_pids),
            "cwd_holders": [f"pid {pid}: cwd in worktree" for pid in sorted(self.cwd_pids)],
            "unverifiable": self.unverifiable,
            "identities": {str(pid): asdict(value) for pid, value in self.identities.items()},
            "errors": list(self.errors),
            "shapes": {str(pid): dict(value) for pid, value in sorted(self.shapes.items())},
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
_PROC_PIDVNODEPATHINFO = 9
_VNODE_INFO_SIZE = 152
_MAXPATHLEN = 1024
_VNODE_PATHINFO_SIZE = 2 * (_VNODE_INFO_SIZE + _MAXPATHLEN)
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
    has begun to exit, its birth rendered as ps lstart, and its unique id, which
    the kernel gives each new process from one counter for the life of a boot.
    No command, environment or name."""
    pid: int
    exiting: bool
    unique_id: int | None
    proc_start: str | None = None


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
            # Birth and unique id belong to this very record. A later ps of a
            # responsibility member might describe a new holder of its pid.
            seconds = struct.unpack_from("=Q", info.raw, 120)[0]  # pbi_start_tvsec
            born = time.gmtime(seconds)
            weekday = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[born.tm_wday]
            month = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
                     "Oct", "Nov", "Dec")[born.tm_mon - 1]
            # ps runs with LC_ALL=C and TZ=UTC; ambient locale must not change
            # these identities. Keep its space-padded day and second precision.
            started = (f"{weekday} {month} {born.tm_mday:2d} {born.tm_hour:02d}:"
                       f"{born.tm_min:02d}:{born.tm_sec:02d} {born.tm_year}") if seconds else None
            return KernelProcess(pid, bool(flags & _PROC_FLAG_INEXIT),
                                 struct.unpack_from("=Q", info.raw, _UNIQUEID_OFFSET)[0], started)
        if got <= 0 and failure in (errno.ESRCH, errno.EPERM):
            return None
        raise InspectionError(f"process inspection of pid {pid} unavailable")

    def cwd(self, pid: int) -> str | None:
        """Kernel cwd via PROC_PIDVNODEPATHINFO; gone/denied processes have none.

        Darwin's sys/proc_info.h defines two 1176-byte vnode_info_path records,
        each a 152-byte vnode_info followed by MAXPATHLEN (1024) path bytes.
        No command, environment, subprocess or lsof read is involved.
        """
        if self._pidinfo is None:
            raise InspectionError("cwd inspection unavailable")
        info = ctypes.create_string_buffer(_VNODE_PATHINFO_SIZE)
        ctypes.set_errno(0)
        got = self._pidinfo(pid, _PROC_PIDVNODEPATHINFO, 0, info, len(info))
        failure = ctypes.get_errno()
        if got == len(info):
            path = info.raw[_VNODE_INFO_SIZE:_VNODE_INFO_SIZE + _MAXPATHLEN]
            end = path.find(b"\0")
            if end < 0:
                raise InspectionError(f"cwd inspection of pid {pid} truncated")
            return os.fsdecode(path[:end]) or None
        if got <= 0 and failure in (errno.ESRCH, errno.EPERM):
            return None
        raise InspectionError(f"cwd inspection of pid {pid} unavailable")

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
                    root: ResponsibleRoot | None, ours: Callable[[KernelProcess], bool]
                    ) -> tuple[set[int], dict[int, KernelProcess], list[str]]:
    """One pass over `candidates` for C-5.5's marker and responsibility sources:
    (marked pids, records whose responsible process is `root`, errors). `marker` None
    skips the first, `root` None the second; `ours` says whether a process is
    recent enough to be the root's (`ResponsibleRoot`)."""
    marked: set[int] = set()
    responsible: dict[int, KernelProcess] = {}
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
                    if procargs_marked(found, *marker):
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
                    and (ceiling is None or current.unique_id is None
                         or root.unique_id <= current.unique_id < ceiling)):
                responsible[pid] = current
    return marked, responsible, errors


def containment(pgid: int | None, guardian_pid: int | None, child_pid: int | None,
                attempt_id: str, root: str | None = None, *,
                recorded_identities: dict[int, ProcessIdentity] | None = None,
                recorded_alternatives: dict[int, list[ProcessIdentity]] | None = None,
                started_at: str | None = None, finished_at: str | None = None,
                responsible_root: ResponsibleRoot | None = None) -> Containment:
    """Collect C-5.5 sources; failed sources or needed identity reads prevent release.

    Receipt timestamps are accepted for caller compatibility, but cannot prove
    reuse: a wall-clock correction can put a genuine birth outside those bounds.

    Identities describe the census, not authority to signal. In particular a
    newly discovered escaped process must remain quarantined unless the caller
    already recorded that process's ownership before the escape.
    Alternative recorded births at one pid are census seeds too: a match of
    either ownership or responsibility evidence must survive the other birth's
    recycling. Neither alternative grants signal authority.

    The group and the descendant walk are read from one process-table snapshot
    (`ps -axo pid=,ppid=,pgid=,stat=,lstart=`), so a process cannot be present
    in one source and absent from the other because it exited between two reads.
    The snapshot also gives every live pid a shape (parent, group, state) that
    the census records as evidence, and the start time that is its identity.
    The marker source reads each process's raw argument buffer in this process
    (`Darwin`), and so does the fourth source, every process whose responsible
    process is `responsible_root` (D-CEN1), for an attempt that recorded one.
    Responsibility members retain the birth identity from their kernel record;
    other identities come from the snapshot. A root without a recorded unique
    id needs a matching start-time read before the member scan. Every scanned
    root's current holder is read in process afterwards (C-5.12).
    Commands and environments are never retained.
    """
    groups: set[int] = set()
    descendants: set[int] = set()
    markers: set[int] = set()
    responsible: set[int] = set()
    responsible_records: dict[int, KernelProcess] = {}
    errors: list[str] = []
    recycled: dict[int, dict[str, Any]] = {}
    checked: set[int] = set()
    recorded_identities = recorded_identities or {}
    recorded_alternatives = recorded_alternatives or {}
    try:
        seen: ProcessTable | None = snapshot()
        table = seen.rows
    except InspectionError:
        seen, table = None, {}
        errors.append("group enumeration unavailable")
        errors.append("descendant enumeration unavailable")

    def live(pid: int) -> bool:
        return pid in table and not table[pid][2].startswith("Z")

    def is_recycled(pid: int) -> bool:
        """Prune only when every recorded birth at this pid is provably different."""
        if pid in checked:
            return pid in recycled
        if pid not in table or seen is None:
            return False
        checked.add(pid)
        began = table[pid][3]
        needs_comparison = live(pid) or (pid == pgid and bool(groups))
        choices = []
        for known in [recorded_identities.get(pid), *recorded_alternatives.get(pid, ())]:
            if known is not None and known not in choices:
                choices.append(known)
        if not choices:
            if needs_comparison:
                errors.append(f"recorded identity unavailable for pid {pid}")
            return False
        reason = None
        uncertain = False
        failed_comparisons: list[str] = []
        for known in choices:
            if not known.boot_id or not known.proc_start:
                # Wall-clock receipt bounds cannot repair an incomplete birth.
                uncertain = True
                if needs_comparison:
                    failed_comparisons.append(f"recorded identity unavailable for pid {pid}")
            elif not began:
                uncertain = True
                if needs_comparison:
                    failed_comparisons.append(f"start identity unavailable for root pid {pid}")
            elif began != known.proc_start:
                reason = reason or "different start identity"
            elif not needs_comparison:
                # A zombie alone contributes no live PID. Compare its boot only
                # when needed to decide ownership of surviving group members.
                return False
            else:
                try:
                    current_boot = seen.boot()
                    match = boot_identity.matches(known.boot_id, current_boot, seen.legacy_seconds)
                    if match is False:
                        # C-5.12: a cached UUID mismatch needs a fresh read before
                        # it can establish death, just as in liveness().
                        forget_boot_id()
                        match = boot_identity.matches(known.boot_id, boot_id(), seen.legacy_seconds)
                    if match is True:
                        return False
                    if match is None:
                        uncertain = True
                        failed_comparisons.append(f"boot identity unknown for root pid {pid}")
                    else:
                        reason = reason or "different boot identity"
                except InspectionError:
                    uncertain = True
                    failed_comparisons.append(f"boot identity unavailable for root pid {pid}")
        if uncertain or reason is None:
            errors.extend(dict.fromkeys(failed_comparisons))
            return False
        try:
            now_boot = seen.boot()
        except InspectionError:
            now_boot = None                 # start mismatch alone already proves reuse
        recycled[pid] = {
            "recorded": asdict(choices[0]),
            "now": {"pid": pid, "boot_id": now_boot, "proc_start": began},
            "reason": reason,
        }
        if len(choices) > 1:
            recycled[pid]["also_recorded"] = [asdict(known) for known in choices[1:]]
        return True

    if seen is not None:
        # XNU forkproc rejects PIDs reserved by a process group or session:
        # https://github.com/apple-oss-distributions/xnu/blob/xnu-12377.1.9/bsd/kern/kern_fork.c#L970-L972
        # Thus a different process at pid == pgid proves the old group is gone.
        # An absent leader does NOT: retain its surviving group members.
        groups = set(seen.group(pgid))
        if pgid and groups and is_recycled(pgid):
            groups.clear()
        # There is no recorded group before setsid. The other sources
        # still enumerate the guardian and any inherited marker.
        # The snapshot is not atomic: an absent/zombie parent may still have
        # live child rows. Keep its links, and seed every recorded survivor so
        # an escaped writer reparented to init survives exclusion of its root.
        # These are census seeds only; signal ownership is checked separately.
        roots = {pid for pid in {*recorded_identities, *recorded_alternatives, guardian_pid, child_pid}
                 if pid and pid > 0 and not is_recycled(pid)}
        found = set(roots)
        frontier = roots
        while frontier:
            frontier = {pid for pid, row in table.items() if row[0] in frontier and pid not in found}
            found.update(frontier)
        descendants = {pid for pid in found if live(pid)}
    try:
        # C-5.5: attempt ids are a timestamp and a slug, so two daemons (or two
        # test state roots) can mint the same id in the same second. The state
        # root is the second half of the marker whenever the caller has one.
        wanted: tuple[bytes, bytes | None] | None = marker_strings(attempt_id, root)
    except ValueError:
        wanted = None
        errors.append("marker enumeration unavailable")
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
        return (order[0] is None or responsible_root.unique_id is None or process.unique_id is None
                or process.unique_id >= responsible_root.unique_id)

    # The table's start/boot comparison is stronger than unique-id ordering, and
    # also protects legacy roots whose unique id was unavailable at launch.
    source_root = responsible_root
    if (source_root is not None and source_root.pid in recorded_identities
            and is_recycled(source_root.pid)):
        source_root = None
    if source_root is not None and source_root.unique_id is None:
        # Without a kernel sequence, the snapshot alone cannot bound a new
        # holder's members. Reject an already-unavailable root before reading
        # its members; the final kernel holder read bounds later reuse.
        known = recorded_identities.get(source_root.pid)
        try:
            root_matches = (live(source_root.pid) and known is not None and bool(known.proc_start)
                            and table[source_root.pid][3] == known.proc_start
                            and proc_start(source_root.pid) == known.proc_start)
        except InspectionError:
            root_matches = False
        if not root_matches:
            errors.append(f"responsibility root identity unavailable for pid {source_root.pid}")
            source_root = None
    if wanted is not None or source_root is not None:
        listed = True
        try:
            # The snapshot's pids and a listing taken after it, so a process born
            # since the snapshot is read too; a pid gone by then reads as gone.
            candidates = sorted(kernel.pids() | {pid for pid in table if live(pid)})
        except InspectionError:
            listed = False
            candidates = []
            if wanted is not None:
                errors.append("marker enumeration unavailable")
            if source_root is not None:
                errors.append("responsibility enumeration unavailable")
        # Keep only pids and birth records; argument buffers never become evidence.
        found_marked, found_responsible, failed = _kernel_sources(
            kernel, candidates, wanted, source_root, ours)
        if source_root is not None and listed and order != [False]:
            # A successor cannot acquire the pid until R is reaped. Every real
            # member read naming R therefore predates that successor. Apply
            # this ceiling after the scan too, for known and unknown root ids.
            holder_failed = False
            try:
                holder = kernel.process(source_root.pid)
            except InspectionError:
                holder = None
                holder_failed = True
            root_matches = holder is not None and (
                holder.unique_id == source_root.unique_id if source_root.unique_id is not None
                else holder.proc_start == recorded_identities[source_root.pid].proc_start)
            if not root_matches:
                if (holder is not None and holder.unique_id is not None
                        and (source_root.unique_id is not None or holder.proc_start is not None)):
                    found_responsible = {pid: member for pid, member in found_responsible.items()
                                         if member.unique_id is None or member.unique_id < holder.unique_id}
                # Without a readable successor ceiling, retaining a member
                # only holds the attempt. It never grants signal authority.
                # A known root already gone with no provisional observations
                # is still a valid empty source; do not make it sticky.
                if (source_root.unique_id is None or holder_failed
                        or (holder is None and found_responsible)
                        or (holder is not None and holder.unique_id is None)):
                    failed.append(f"responsibility root identity unavailable for pid {source_root.pid}")
        markers |= found_marked
        responsible.update(found_responsible)
        responsible_records.update(found_responsible)
        errors.extend(failed)
    for pid in sorted(markers):
        # Responsibility records already establish live, non-zombie state.
        # An older zombie row or a later pid holder cannot undo that read.
        if pid in responsible_records:
            continue
        try:
            state = table[pid][2] if pid in table else _stat(pid)
        except InspectionError:
            errors.append("marker enumeration unavailable")
            continue
        if not state or state.startswith("Z"):
            markers.discard(pid)
    identities: dict[int, ProcessIdentity] = {}
    for pid in groups | descendants | markers | responsible:
        try:
            # A missing snapshot start still prevents verification. A kernel
            # responsibility record keeps its own birth for later censuses.
            missing_snapshot_start = seen is not None and seen.live(pid) and not table[pid][3]
            if missing_snapshot_start and pid not in responsible_records:
                raise InspectionError("snapshot start identity is unavailable")
            if pid in responsible_records:
                member = responsible_records[pid]
                if not member.proc_start:
                    raise InspectionError("responsibility member start identity is unavailable")
                if pid in groups and member.proc_start != table[pid][3]:
                    # The snapshot saw another incarnation in this group. A
                    # new responsibility identity cannot inherit its signal
                    # authority; only this source now establishes membership.
                    groups.discard(pid)
                current = ProcessIdentity(pid, seen.boot() if seen is not None else boot_id(), member.proc_start)
            else:
                current = (seen.identity(pid) if seen is not None else None) or identity(pid)
            if current is not None:
                identities[pid] = current
                if missing_snapshot_start:
                    errors.append(f"identity inspection unavailable for pid {pid}")
            else:
                # A process can exit between census and identity capture.
                groups.discard(pid)
                descendants.discard(pid)
                markers.discard(pid)
                responsible.discard(pid)
        except InspectionError:
            errors.append(f"identity inspection unavailable for pid {pid}")
    shapes = {pid: {"ppid": table[pid][0], "pgid": table[pid][1], "stat": table[pid][2]}
              for pid in groups | descendants | markers | responsible if pid in table
              and (pid not in responsible_records or responsible_records[pid].proc_start == table[pid][3])}
    return Containment(frozenset(groups), frozenset(descendants), frozenset(markers),
                       bool(errors), identities, tuple(errors), shapes, recycled,
                       frozenset(responsible))


def worktree_cwd_pids(worktree: str | os.PathLike) -> frozenset[int]:
    """Live processes with cwd inside a resolved job-owned worktree (hold only).

    The caller establishes ownership. Resolve both spellings, including /tmp
    and /private/tmp, and compare whole path components. Cache repeated cwd
    spellings within this one scan; nothing about observers is persisted.
    Other users denied by libproc are skipped, as in the census.
    """
    target = os.path.realpath(worktree)
    prefix = target.rstrip(os.sep) + os.sep
    resolved: dict[str, str] = {}
    found = set()
    for pid in kernel.pids():
        cwd = kernel.cwd(pid)
        if cwd is None:
            continue
        if cwd not in resolved:
            resolved[cwd] = os.path.realpath(cwd)
        current = resolved[cwd]
        if current == target or current.startswith(prefix):
            found.add(pid)
    return frozenset(found)


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
                                stderr=subprocess.DEVNULL, pass_fds=pass_fds, close_fds=True, cwd=cwd)
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
                                                  _POSIX_SPAWN_CLOEXEC_DEFAULT | _POSIX_SPAWN_SETSIGDEF))
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
