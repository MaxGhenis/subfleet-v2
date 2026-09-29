"""C-5.5, C-5.6, C-5.7b: what one process table proves an attempt owns, and what a census counts.

The property tests run a small model of the macOS process table: `fork`, `setsid`,
`setpgid` within a session, `exit` (orphans go to launchd, pid 1), reaping, `ptrace`
attach (the tracee is listed under its tracer and marked `X`), and pid reuse, which
POSIX forbids while a group or session with that id exists. Processes the guardian's
tree forked are "ours"; everything else is a stranger. `(pid, start)` names a process
(C-5.3): the model never reuses a pid within the second it was freed.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import procs
from subfleet.procs import ProcessIdentity, ProcessTable, containment, owned_closure, start_seconds

BOOT = "75469207-4043-4113-8e1f-b5469953a665"
T0 = "Tue Sep 29 03:14:45 2026"
T1 = "Tue Sep 29 03:14:46 2026"
T2 = "Tue Sep 29 03:15:00 2026"


def table(*rows: tuple[int, int, int, str, str]) -> ProcessTable:
    """(pid, ppid, pgid, stat, lstart) rows, with the boot identity already read."""
    return ProcessTable({pid: (ppid, pgid, stat, start) for pid, ppid, pgid, stat, start in rows}, BOOT)


def ident(pid: int, start: str = T0) -> ProcessIdentity:
    return ProcessIdentity(pid, BOOT, start)


def test_start_seconds_reads_ps_lstart_in_utc_and_refuses_anything_else():
    """C-5.6: only an order is taken from `lstart`; a day padded with a space parses too."""
    assert start_seconds("Sat Sep  5 10:00:00 2026") == 1788602400
    assert start_seconds(T0) < start_seconds(T1) < start_seconds(T2)
    for bad in ("", None, "garbage", "Tue Sep 29 25:00:00 2026", "Tue Foo 29 03:14:45 2026", "unit-test-start"):
        assert start_seconds(bad) is None


def test_c5_6_a_tool_session_under_the_provider_is_owned_and_a_stranger_is_not():
    """C-5.6: the recorded group, the provider's tool session (a `/bin/zsh` session leader
    and the command in its group), and nothing of a stranger's, even in a group or under a
    parent that is not ours."""
    shown = table(
        (100, 1, 100, "Ss", T0),       # guardian, leads the recorded group
        (101, 100, 100, "S", T0),      # provider
        (200, 101, 200, "Ss", T1),     # Bash tool: zsh in a session of its own
        (201, 200, 200, "S", T1),      # uv run pytest
        (202, 1, 200, "S", T1),        # orphaned member of the zsh's group
        (300, 1, 300, "Ss", T0),       # a stranger's shell
        (301, 300, 300, "S", T1),      # and its child
    )
    owned = owned_closure(shown, [ident(100)])
    assert set(owned) == {100, 101, 200, 201, 202}
    assert owned[201] == ProcessIdentity(201, BOOT, T1)


def test_c5_6_a_traced_or_older_child_is_not_owned_through_its_parent_link():
    """C-5.6: `ptrace` attach lists a stranger under its tracer; `X` and the start order keep it out."""
    shown = table(
        (100, 1, 100, "Ss", T1),
        (101, 100, 100, "S", T1),       # the job's debugger
        (400, 101, 400, "SX", T2),      # a stranger it attached to, started later: traced
        (401, 101, 401, "S", T0),       # a stranger listed under it but started before it
        (402, 400, 400, "S", T2),       # the traced stranger's own child (its group is not ours)
    )
    assert set(owned_closure(shown, [ident(100, T1)])) == {100, 101}


def test_c5_6_roots_count_only_with_their_recorded_start_zombies_never():
    """C-5.3, C-5.6: a recorded pid now held by another process (another start) proves nothing."""
    shown = table((100, 1, 100, "Ss", T2), (101, 100, 100, "S", T2), (102, 1, 102, "Z", T0))
    assert owned_closure(shown, [ident(100, T0), ident(102, T0)]) == {}
    assert set(owned_closure(shown, [ident(100, T2)])) == {100, 101}


def test_c5_6_an_orphaned_recorded_process_still_proves_its_children():
    """C-5.6: a recorded shell reparented to launchd is still a root, by identity."""
    shown = table((200, 1, 200, "Ss", T1), (201, 200, 200, "S", T2), (202, 1, 202, "Ss", T2))
    assert set(owned_closure(shown, [ident(200, T1)])) == {200, 201}


def test_c5_6_a_root_whose_boot_identity_cannot_be_read_raises():
    """C-5.3: a start that matches needs the boot identity, and a failed read is no answer."""
    shown = ProcessTable({100: (1, 100, "Ss", T0)})

    def failing():
        raise procs.InspectionError("boot identity unavailable")

    original = procs.boot_id
    procs.boot_id = failing
    try:
        with pytest.raises(procs.InspectionError):
            owned_closure(shown, [ident(100)])
        assert owned_closure(shown, [ident(100, T1)]) == {}     # absent or reused needs no boot read
    finally:
        procs.boot_id = original


# --- the process model ---------------------------------------------------------------

POOL = range(1000, 1012)          # small, so pids are reused often


class Model:
    """A process table under fork, setsid, setpgid, exit, reap, attach and pid reuse."""

    def __init__(self):
        self.procs: dict[int, dict] = {}
        self.clock = 0
        self.freed: dict[int, int] = {}          # pid -> the second it was freed
        self.next = POOL.start
        guardian = self._spawn(ppid=1, ours=True)
        self.procs[guardian].update(pgid=guardian, sid=guardian)
        self.guardian = guardian
        stranger = self._spawn(ppid=1, ours=False)
        self.procs[stranger].update(pgid=stranger, sid=stranger)

    def lstart(self, second: int) -> str:
        minute, sec = divmod(second, 60)
        return f"Tue Sep 29 03:{minute:02d}:{sec:02d} 2026"

    def _free(self, pid: int) -> bool:
        if pid in self.procs or self.freed.get(pid) == self.clock:
            return False
        return all(p["pgid"] != pid and p["sid"] != pid for p in self.procs.values())

    def _spawn(self, ppid: int, ours: bool, pgid: int = 0, sid: int = 0) -> int | None:
        for _ in range(len(POOL)):
            pid = self.next
            self.next = POOL.start + (self.next - POOL.start + 1) % len(POOL)
            if self._free(pid):
                self.procs[pid] = {"ppid": ppid, "pgid": pgid, "sid": sid, "start": self.clock,
                                   "zombie": False, "ours": ours, "traced": False, "tracer_of": None}
                return pid
        return None

    def live(self) -> list[int]:
        return sorted(pid for pid, p in self.procs.items() if not p["zombie"])

    def step(self, op: int, i: int, j: int) -> None:
        live = self.live()
        if not live:
            return
        a = live[i % len(live)]
        b = live[j % len(live)]
        pa = self.procs[a]
        if op == 0:                                          # fork
            self._spawn(ppid=a, ours=pa["ours"], pgid=pa["pgid"], sid=pa["sid"])
        elif op == 1 and pa["pgid"] != a and pa["sid"] != a:  # setsid (not a group leader)
            pa.update(pgid=a, sid=a)
        elif op == 2 and self.procs[b]["sid"] == pa["sid"] and pa["sid"] != a:
            pa["pgid"] = self.procs[b]["pgid"]                # setpgid into a group of its own session
        elif op == 3 and pa["sid"] != a:
            pa["pgid"] = a                                   # setpgid(0, 0): a new group, same session
        elif op == 4 and a != self.guardian or op == 4 and len(live) > 3:
            self._exit(a)
        elif op == 5:                                        # reap a zombie child of `a`
            for pid in [pid for pid, p in self.procs.items() if p["zombie"] and p["ppid"] == a]:
                self._remove(pid)
        elif op == 6 and a != b and not self.procs[b]["traced"] and b != self.guardian:
            pb = self.procs[b]                                # ptrace attach: b listed under a
            pb.update(traced=True, tracer_of=pb["ppid"], ppid=a)
        elif op == 7:
            self.clock += 1

    def _remove(self, pid: int) -> None:
        del self.procs[pid]
        self.freed[pid] = self.clock

    def _exit(self, pid: int) -> None:
        for p in self.procs.values():
            if p["ppid"] == pid:
                p["ppid"] = 1                                 # orphans go to launchd
        parent = self.procs[pid]["ppid"]
        if parent != 1 and parent in self.procs and not self.procs[parent]["zombie"]:
            self.procs[pid]["zombie"] = True
        else:
            self._remove(pid)

    def table(self) -> ProcessTable:
        rows = {}
        for pid, p in self.procs.items():
            stat = "Z" if p["zombie"] else ("S" + ("s" if p["sid"] == pid else "") + ("X" if p["traced"] else ""))
            rows[pid] = (p["ppid"], p["pgid"], stat, self.lstart(p["start"]))
        return ProcessTable(rows, BOOT)

    def identity(self, pid: int) -> ProcessIdentity:
        return ProcessIdentity(pid, BOOT, self.lstart(self.procs[pid]["start"]))


def reference_closure(shown: ProcessTable, roots) -> set[int]:
    """C-5.6 read literally, as a fixpoint over every pair: slow, and independent of
    `owned_closure`'s indexes and frontier."""
    rows = shown.rows

    def live(pid):
        return pid in rows and not rows[pid][2].startswith("Z")

    owned = {r.pid for r in roots if live(r.pid) and rows[r.pid][3] == r.proc_start}
    while True:
        more = set()
        for pid in rows:
            if pid in owned or not live(pid):
                continue
            ppid, pgid, stat, start = rows[pid]
            by_parent = (ppid in owned and "X" not in stat
                         and start_seconds(start) >= start_seconds(rows[ppid][3]))
            by_group = pgid in owned and rows[pgid][1] == pgid
            if by_parent or by_group:
                more.add(pid)
        if not more:
            return owned
        owned |= more


steps = st.lists(st.tuples(st.integers(0, 7), st.integers(0, 50), st.integers(0, 50)), min_size=30, max_size=120)


@settings(max_examples=500, deadline=None, derandomize=True, database=None)
@given(steps=steps, records=st.dictionaries(st.integers(0, 119), st.integers(0, 50), min_size=2, max_size=10))
def test_c5_6_ownership_never_reaches_a_stranger_and_matches_the_reference(steps, records):
    """C-5.6: whatever the process table did (forks, new sessions and groups, exits and
    orphans, traces, pid reuse), `owned_closure` proves owned only processes the guardian's
    tree forked, and exactly the ones C-5.6's rules reach (the reference fixpoint).

    The roots are the guardian as recorded at birth plus identities recorded along the
    way (at the steps `records` names), some of whose pids a stranger may hold by the end."""
    model = Model()
    recorded = [model.identity(model.guardian)]
    for n, (op, i, j) in enumerate(steps):
        model.step(op, i, j)
        if n in records:
            live = [pid for pid in model.live() if model.procs[pid]["ours"]]
            if live:
                recorded.append(model.identity(live[records[n] % len(live)]))
    shown = model.table()
    owned = owned_closure(shown, recorded)
    strangers = {pid for pid in owned if not model.procs[pid]["ours"]}
    assert not strangers, (strangers, shown.rows)
    assert all(not model.procs[pid]["zombie"] for pid in owned)
    assert set(owned) == reference_closure(shown, recorded)
    for pid, found in owned.items():
        assert found == ProcessIdentity(pid, BOOT, shown.rows[pid][3])


@settings(max_examples=200, deadline=None, derandomize=True, database=None)
@given(steps=steps)
def test_c5_6_every_untraced_process_forked_under_a_live_guardian_is_owned(steps):
    """C-5.6 completeness: while the chain of parents from the guardian is intact (no
    orphaning, no trace), every process forked under it is owned, whatever sessions and
    groups it made on the way."""
    model = Model()
    for op, i, j in steps:
        if op not in (4, 6):                     # no exits, no traces: every chain stays intact
            model.step(op, i, j)
    shown = model.table()
    owned = owned_closure(shown, [model.identity(model.guardian)])
    ours = {pid for pid, p in model.procs.items() if p["ours"] and not p["zombie"]}
    assert set(owned) == ours


# --- the census ----------------------------------------------------------------------

ATTEMPT = "20260929-000000-census/a1"
ROOT = "/state root/with space"


def marked(pid: int, attempt: str = ATTEMPT, root: str = ROOT) -> str:
    return f"{pid} /usr/bin/python3 -c x SUBFLEET_ATTEMPT={attempt} SUBFLEET_ROOT={root} HOME=/Users/x"


class Reads:
    """`procs._read` over one fixed process table and environment scan."""

    def __init__(self, shown: ProcessTable, lines: list[str], *, fail_table=False, fail_scan=False):
        self.shown, self.lines, self.fail_table, self.fail_scan = shown, lines, fail_table, fail_scan
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, empty_ok=False):
        self.calls.append(list(argv))
        if argv == procs.TABLE_ARGV:
            if self.fail_table:
                raise procs.InspectionError("ps inspection failed (1)")
            return "".join(f"{pid} {ppid} {pgid} {stat} {start}\n"
                           for pid, (ppid, pgid, stat, start) in self.shown.rows.items())
        if argv == procs.MARKER_ARGV:
            if self.fail_scan:
                raise procs.InspectionError("ps inspection failed (1)")
            return "".join(line + "\n" for line in self.lines)
        if argv[:2] == ["/bin/ps", "-p"]:
            row = self.shown.rows.get(int(argv[2]))
            column = {"lstart=": 3, "stat=": 2}[argv[4]]
            return (row[column] + "\n") if row else ""
        raise procs.InspectionError("unexpected read " + " ".join(argv))


@pytest.fixture
def reads(monkeypatch):
    def install(shown, lines=(), **faults):
        fake = Reads(shown, list(lines), **faults)
        monkeypatch.setattr(procs, "_read", fake)
        monkeypatch.setattr(procs, "boot_id", lambda: BOOT)
        return fake
    return install


def test_c5_5_a_census_counts_a_recorded_shell_whose_environment_ps_does_not_show(reads):
    """C-5.5: an orphaned `/bin/zsh` (no marker visible) is found by its identity, its
    group's members through it, and a recorded pid another process now holds is not."""
    reads(table((200, 1, 200, "Ss", T1), (201, 1, 200, "S", T1), (300, 1, 300, "S", T2)))
    found = containment(100, 100, None, ATTEMPT, ROOT, recorded=[ident(200, T1), ident(300, T0)])
    assert found.live_pids == {200, 201} and not found.verified_empty
    assert containment(100, 100, None, ATTEMPT, ROOT).verified_empty


def test_c5_7b_the_quarantine_census_lets_recorded_pids_answer_only_for_themselves(reads):
    """C-5.7b: after pid reuse, the guardian's pid and the recorded group id belong to a
    stranger, whose process and group the census does not count; the guardian with its
    own start is counted as before."""
    guardian = ident(100, T0)
    stranger = table((100, 1, 100, "Ss", T2), (101, 100, 100, "S", T2), (102, 7, 100, "S", T2))
    reads(stranger)
    assert containment(100, 100, None, ATTEMPT, ROOT, guardian=guardian).verified_empty
    assert not containment(100, 100, None, ATTEMPT, ROOT).verified_empty      # the running census holds on
    reads(table((100, 1, 100, "Ss", T0), (101, 100, 100, "S", T0)))
    assert containment(100, 100, None, ATTEMPT, ROOT, guardian=guardian).live_pids == {100, 101}
    # The group's leader is gone and its pid free: a member left in it may be the attempt's.
    reads(table((102, 1, 100, "S", T0)))
    assert containment(100, 100, None, ATTEMPT, ROOT, guardian=guardian).live_pids == {102}


def test_c5_5_a_marker_needs_both_halves_and_a_failed_read_is_unverifiable(reads):
    """C-5.5: the attempt id and the state root, both; `ps` failing is never "empty"."""
    shown = table((500, 1, 500, "S", T0), (501, 1, 501, "S", T0), (502, 1, 502, "Z", T0))
    reads(shown, [marked(500), marked(501, root="/other root"), marked(502), marked(503)])
    assert containment(None, None, None, ATTEMPT, ROOT).live_pids == {500}
    reads(shown, [], fail_scan=True)
    assert containment(None, None, None, ATTEMPT, ROOT).unverifiable
    reads(shown, [], fail_table=True)
    assert containment(None, None, None, ATTEMPT, ROOT).unverifiable


row = st.tuples(st.integers(1, 12), st.integers(1, 12), st.sampled_from(["S", "Ss", "R", "Z", "SX"]),
                st.sampled_from([T0, T1, T2]))


@settings(max_examples=300, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(rows=st.dictionaries(st.integers(1, 12), row, max_size=12),
       markers=st.lists(st.integers(1, 14), max_size=5),
       recorded=st.lists(st.tuples(st.integers(1, 14), st.sampled_from([T0, T1, T2])), max_size=4),
       guardian=st.one_of(st.none(), st.tuples(st.integers(1, 12), st.sampled_from([T0, T1, T2]))),
       pgid=st.integers(1, 12), child=st.one_of(st.none(), st.integers(1, 12)),
       fail_table=st.booleans(), fail_scan=st.booleans())
def test_c5_7b_a_census_from_shared_reads_equals_one_from_its_own_reads(
        reads, rows, markers, recorded, guardian, pgid, child, fail_table, fail_scan):
    """C-5.7b differential: one sweep's `census_reads` give every attempt the census its
    own reads of the same process table would have given it."""
    shown = ProcessTable({pid: (ppid, g, stat, start) for pid, (ppid, g, stat, start) in rows.items()}, BOOT)
    lines = [marked(pid) for pid in markers] + [marked(1, attempt="20260929-000000-other/a1")]
    fake = reads(shown, lines, fail_table=fail_table, fail_scan=fail_scan)
    ids = [ProcessIdentity(pid, BOOT, start) for pid, start in recorded]
    lead = ProcessIdentity(guardian[0], BOOT, guardian[1]) if guardian else None
    args = (pgid, lead.pid if lead else pgid, child, ATTEMPT, ROOT)
    own = containment(*args, recorded=ids, guardian=lead)
    shared_reads = procs.census_reads()
    before = len(fake.calls)
    shared = containment(*args, recorded=ids, guardian=lead, reads=shared_reads)
    assert shared == own
    assert not [argv for argv in fake.calls[before:] if argv in (procs.TABLE_ARGV, procs.MARKER_ARGV)]


def test_c5_7b_census_reads_keep_only_marked_rows_and_never_show_them():
    """C-5.5: environments are consumed in memory; `repr` never prints the rows kept."""
    kept = procs.CensusReads(table((1, 0, 1, "Ss", T0)), (marked(9),))
    assert "SUBFLEET_ATTEMPT" not in repr(kept) and "HOME=" not in repr(kept)
    assert re.search(r"marked", repr(kept)) is None
