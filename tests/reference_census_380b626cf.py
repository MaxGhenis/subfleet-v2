"""The census of #250 at 380b626cf (D-KILL4, before D-QUAR-REUSE1 met it), kept as a reference.

`containment` below is copied verbatim from `subfleet/procs.py` at 380b626cf, lines
808-1172. It is the census #259's boot scope (D-QUAR-REUSE1) changes on this line: release/217's
census as D-KILL4 left it, with the parent-chain rule and the recorded group's leader check
but no `attempt_boot`. So it takes `parent_chain` and not `attempt_boot`. The differential
tests in `tests/unit/test_census_boot_seeds.py` compare today's census with it, as they
compared #259's with 15bd8c172's (`tests/reference_census_15bd8c172.py`): any input on which
the two differ must be one whose attempt's boot is over, or that stores an identity recorded
on another boot than the attempt's.

The function is rebound to `subfleet.procs`'s globals, as the 15bd8c172 copy is, so every
name its body reads (`snapshot`, `kernel`, `boot_id`, `_parent_chain`, ...) is looked up
there when it runs. `_parent_chain` is therefore today's, which differs from 380b626cf's
only for a stored held parent of another boot or one the kill protocol carries
(`ParentChain.held`, `.carried`): the generated worlds store neither, and the tests assert so.
Nothing outside tests imports this module.
"""

from __future__ import annotations

import types

from subfleet import procs
from subfleet.procs import Containment, ParentChain, ProcessIdentity, ResponsibleRoot

__all__ = ["containment"]


def containment(pgid: int | None, guardian_pid: int | None, child_pid: int | None,
                attempt_id: str, root: str | None = None, *,
                recorded_identities: dict[int, ProcessIdentity] | None = None,
                recorded_alternatives: dict[int, list[ProcessIdentity]] | None = None,
                started_at: str | None = None, finished_at: str | None = None,
                responsible_root: ResponsibleRoot | None = None,
                parent_chain: ParentChain | None = None) -> Containment:
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
    With `parent_chain`, a marked member whose parent the census lacks has its
    parent chain followed (`_parent_chain`, D-KILL4).
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

    def leader_boot(pid: int) -> bool | None:
        """Does the recorded group leader's boot match this table's? None: unknown."""
        known = recorded_identities.get(pid)
        if known is None or not known.boot_id:
            return None
        try:
            match = boot_identity.matches(str(known.boot_id), seen.boot(), seen.legacy_seconds)
            if match is False:
                # C-5.12: a cached UUID mismatch needs a fresh read before it proves another boot.
                forget_boot_id()
                match = boot_identity.matches(str(known.boot_id), boot_id(), seen.legacy_seconds)
        except InspectionError:
            return None
        return match

    leader_verified = False
    if seen is not None:
        # XNU forkproc rejects PIDs reserved by a process group or session:
        # https://github.com/apple-oss-distributions/xnu/blob/xnu-12377.1.9/bsd/kern/kern_fork.c#L970-L972
        # Thus a different process at pid == pgid proves the old group is gone.
        # An absent leader does NOT: retain its surviving group members.
        groups = set(seen.group(pgid))
        compared = len(errors)
        if pgid and groups and is_recycled(pgid):
            groups.clear()
        if pgid and groups and pgid in recorded_identities:
            # D-KILL4 fix 1 (P1, C-5.4, C-5.5): the group is a signal source only when
            # it is provably the attempt's. A leader this table shows alive with its
            # recorded birth and boot verifies it (killpg reaches it anyway). An absent
            # leader leaves its members in the census, holding the attempt, but they
            # are signal targets only when another source or an owned identity
            # attributes them: the leader may have exited long ago and its pgid been
            # taken by a stranger's leaderless group. A recorded boot that is provably
            # not this one drops the group source (no process outlives its boot); one
            # that cannot be compared leaves its members holding, never signalled, and
            # makes the census unverifiable. A present leader was compared by
            # `is_recycled` just now (a failed comparison appended its error), so only
            # an absent one is compared here, with C-5.12's fresh read of a mismatch.
            known = recorded_identities[pgid]
            if pgid in table:
                match = None if len(errors) > compared else True
            else:
                match = leader_boot(pgid)
                if match is None:
                    errors.append(f"boot identity unavailable for group {pgid}")
            if match is False:
                recycled.setdefault(pgid, {"recorded": asdict(known), "now": {"pid": pgid},
                                           "reason": "group of another boot"})
                groups.clear()
            elif match is True:
                leader_verified = bool(live(pgid) and known.proc_start and table[pgid][3] == known.proc_start)
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
    chain: dict[int, str] = {}
    held: dict[int, str] = {}
    fresh: set[int] = set()
    if parent_chain is not None and seen is not None:
        chain, held, fresh = _parent_chain(seen, markers, groups | descendants | markers | responsible,
                                           parent_chain, errors)
    identities: dict[int, ProcessIdentity] = {}
    for pid in groups | descendants | markers | responsible | chain.keys():
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
                chain.pop(pid, None)
        except InspectionError:
            errors.append(f"identity inspection unavailable for pid {pid}")
    shapes = {pid: {"ppid": table[pid][0], "pgid": table[pid][1], "stat": table[pid][2]}
              for pid in groups | descendants | markers | responsible | chain.keys() if pid in table
              and (pid not in responsible_records or responsible_records[pid].proc_start == table[pid][3])}
    held = {pid: why for pid, why in held.items() if pid not in identities}
    held_identities: dict[int, ProcessIdentity] = {}
    for pid in sorted(held):
        try:
            found = seen.identity(pid) if seen is not None else None
        except InspectionError:
            found = None
            errors.append(f"identity inspection unavailable for pid {pid}")
        if found is not None:
            held_identities[pid] = found
    return Containment(frozenset(groups), frozenset(descendants), frozenset(markers),
                       bool(errors), identities, tuple(errors), shapes, recycled,
                       frozenset(responsible), chain_pids=frozenset(chain), chain_reasons=chain,
                       held_pids=frozenset(held), held_reasons=held, held_identities=held_identities,
                       held_fresh=frozenset(fresh & held.keys()),
                       leader_verified=leader_verified and pgid in groups)


# Rebind to the live module namespace (see the docstring): nested functions created when
# it runs inherit these globals too.
containment = types.FunctionType(containment.__code__, vars(procs), containment.__name__,
                                 containment.__defaults__, containment.__closure__)
containment.__kwdefaults__ = {"recorded_identities": None, "recorded_alternatives": None,
                              "started_at": None, "finished_at": None, "responsible_root": None,
                              "parent_chain": None}
