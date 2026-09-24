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
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from . import boot_identity


class InspectionError(RuntimeError):
    """The operating system could not establish process ownership."""


def _read(argv: list[str], *, empty_ok: bool = False) -> str:
    # Match the CLI's rendering of ps lstart; ambient locale/timezone must not
    # make the same live daemon or guardian appear to be a reused pid.
    env = {"LC_ALL": "C", "LANG": "C", "TZ": "UTC",
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InspectionError(f"{os.path.basename(argv[0])} inspection unavailable") from exc
    # BSD ps returns 1 when a valid selector matches no processes.
    if result.returncode and not (
        empty_ok and result.returncode == 1 and not result.stdout.strip()
        and not result.stderr.strip()
    ):
        raise InspectionError(f"{os.path.basename(argv[0])} inspection failed ({result.returncode})")
    return result.stdout


#: The boot-session UUID once read. A process cannot outlive its boot, so the
#: UUID never changes under it; legacy `kern.boottime` seconds can shift with a
#: wall-clock correction and are read again every time (the client's `_BOOT_ID`
#: follows the same rule). Every `liveness` call used to spend one `sysctl`
#: subprocess on this, and the daemon asks about every running guardian (C-5.11).
_BOOT_ID: list[str] = []


def boot_id() -> str:
    """Prefer kern.bootsessionuuid; boottime can move with wall-clock correction."""
    if _BOOT_ID:
        return _BOOT_ID[0]
    def optional_read(argv):
        try:
            return _read(argv)
        except InspectionError:
            return ""
    value = boot_identity.read_identity(optional_read)
    if value:
        if boot_identity.session_uuid(value):
            _BOOT_ID.append(value)
        return value
    raise InspectionError("macOS boot identity is unavailable")


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
    return "alive" if match is True else "dead" if match is False else "unknown"


def same_process(pid: int, boot_id: str, proc_start: str) -> bool:
    """C-5.3: pid reuse, a different boot and zombies never match."""
    return liveness(pid, boot_id, proc_start) == "alive"


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


def _process_table() -> dict[int, tuple[int, int, str]]:
    """pid -> (ppid, pgid, stat) from one `ps -axo pid=,ppid=,pgid=,stat=` snapshot."""
    table: dict[int, tuple[int, int, str]] = {}
    for row in _read(["/bin/ps", "-axo", "pid=,ppid=,pgid=,stat="]).splitlines():
        parts = row.split(None, 3)
        if len(parts) < 4:
            continue
        table[int(parts[0])] = (int(parts[1]), int(parts[2]), parts[3].strip())
    return table


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
                attempt_id: str, root: str | None = None) -> Containment:
    """Collect all three C-5.5 sources; any failed inspection prevents release.

    Identities describe the census, not authority to signal. In particular a
    newly discovered escaped process must remain quarantined unless the caller
    already recorded that process's ownership before the escape.

    The group and the descendant walk are read from one process-table snapshot
    (`ps -axo pid=,ppid=,pgid=,stat=`), so a process cannot be present in one
    source and absent from the other because it exited between two reads. The
    snapshot also gives every live pid a shape (parent, group, state) that the
    census records as evidence; commands and environments are never retained.
    """
    groups: set[int] = set()
    descendants: set[int] = set()
    markers: set[int] = set()
    table: dict[int, tuple[int, int, str]] = {}   # pid -> (ppid, pgid, stat)
    errors: list[str] = []
    try:
        table = _process_table()
        snapshot = True
    except (InspectionError, ValueError):
        snapshot = False
        errors.append("group enumeration unavailable")
        errors.append("descendant enumeration unavailable")

    def live(pid: int) -> bool:
        return pid in table and not table[pid][2].startswith("Z")

    if snapshot:
        if pgid and pgid > 0:
            groups = {pid for pid, (_, group, _) in table.items() if group == pgid and live(pid)}
        # There is no recorded group before setsid. The two remaining sources
        # still enumerate the guardian and any inherited marker.
        roots = {pid for pid in (guardian_pid, child_pid) if pid and pid > 0}
        found = set(roots)
        frontier = roots
        while frontier:
            frontier = {pid for pid, (parent, _, _) in table.items() if parent in frontier and pid not in found}
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
            current = identity(pid)
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
