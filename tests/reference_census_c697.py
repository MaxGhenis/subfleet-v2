"""The census the installed daemon ran on 2026-10-18 (D-KILL2), kept as a reference.

`containment` below is copied verbatim from `subfleet/procs.py` at c69757256 (release
2026-10-10, installed at ~/.local/share/subfleet/current; the installed file and the
commit's are byte-identical). Its marker source is one `ps -axEww -o pid=,command=`
read, and it has three sources: group, parent walk and markers. Tests run it against
real processes to show what that daemon could and could not see; nothing outside
tests imports it. Its helpers are today's: they have the same names and contracts.
"""

from __future__ import annotations

import re

from subfleet.procs import Containment, InspectionError, ProcessIdentity, ProcessTable, _read, _stat, identity, snapshot

__all__ = ["containment"]


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

