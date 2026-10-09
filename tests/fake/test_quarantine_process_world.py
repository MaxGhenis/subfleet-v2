"""Executable C-5 process world, using the production census and resolvers.

The oracle is the simulated kernel, never the census verdict. Reads take their
own snapshots; queued world transitions run after a selected read. No processes
are spawned or signalled. The safety domain is C-5.7's census-covered lineage;
the invisible-writer residual is demonstrated separately below. Conservative
foreign group evidence must also empty before bounded liveness can apply.
"""
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile

from hypothesis import Phase, event, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule
import pytest

from subfleet import procs, protocol
from tests.fake.test_quarantine_self_resolve import Clock
from tests.fake.test_review_pr131_probes import BOOT, ORIGINAL_CENSUS, quarantine
from tests.fake.test_state_contract import state_daemon


@dataclass
class Process:
    pid: int
    start: str
    ppid: int
    pgid: int
    sid: int
    marked: bool = True
    cwd: bool = False
    writer: bool = True
    zombie: bool = False


PIDS = st.sampled_from([99, 200, 201, 300, 700, 900])
PHASES = st.sampled_from(["table", "markers", "cwd", "identity", "group", "confirm"])
OPERATIONS = st.sampled_from(["spawn", "fork", "exit", "zombie", "reap", "reparent",
                             "setsid", "group", "chdir", "scrub", "reuse_pid", "reuse_pgid"])
SCENARIOS = ["ordinary", "failed-bracket-child", "reused-before-group", "retained-authority",
             "paced-missing-start", "paced-owned-escape", "paced-unowned-escape"]
if os.environ.get("SF_WORLD_SCENARIO"):
    SCENARIOS = [os.environ["SF_WORLD_SCENARIO"]]
CONSUMERS = [os.environ["SF_WORLD_CONSUMER"]] if os.environ.get("SF_WORLD_CONSUMER") else ["attempt", "probe"]


class World:
    def __init__(self):
        self.processes = {100: Process(100, "guardian-start", 1, 100, 100, marked=False, writer=False)}
        self.serial = 0
        self.failures = set()
        self.hooks = []
        self.read_counts = {}
        self.observed = {(100, "guardian-start")}
        self.groups = {100}
        self.marker = ""
        self.trace = []
        self.signals = []
        self.signal_checks = {}
        self.resolving = False
        self.confirming = set()
        # Signal ownership is independent of conservative census roots. Only
        # complete identities observed in the original, verified group count.
        self.owned = {(100, "guardian-start")}
        self.capturing_ownership = False
        # Read visibility varies independently of the kernel's real identity.
        self.missing_starts = set()
        self.partial_starts = set()

    def free(self, pid):
        # XNU reserves live group/session numbers, including leaderless groups.
        return pid not in self.processes and all(
            pid not in (p.pgid, p.sid) for p in self.processes.values())

    def spawn(self, pid, *, pgid=None, parent=1, marked=True, cwd=False, writer=True):
        if not self.free(pid):
            return
        self.serial += 1
        group = pgid or pid
        sid = self.processes[parent].sid if parent in self.processes else group
        self.processes[pid] = Process(pid, f"start-{self.serial}", parent, group, sid,
                                      marked, cwd, writer)
        self.trace.append(("spawn", pid, f"start-{self.serial}", group, writer))

    def fork(self, parent, child):
        p = self.processes.get(parent)
        if p is None or p.zombie or not self.free(child):
            return
        self.spawn(child, pgid=p.pgid, parent=parent, marked=p.marked, cwd=p.cwd,
                   writer=p.writer or parent == 100)

    def exit(self, pid):
        self.processes.pop(pid, None)
        self.trace.append(("exit", pid))
        for p in self.processes.values():
            if p.ppid == pid:
                p.ppid = 1

    def covered(self):
        roots = {p.pid for p in self.processes.values() if not p.zombie and
                 (p.marked or p.cwd or (p.pid, p.start) in self.observed or p.pgid in self.groups)}
        while True:
            more = {p.pid for p in self.processes.values() if p.ppid in roots}
            if more <= roots:
                break
            roots |= more
        return all(not p.writer or p.zombie or p.pid in roots for p in self.processes.values())

    def change(self, operation, pid, other=300, *, covered=True):
        before = deepcopy(self.processes)
        p = self.processes.get(pid)
        if operation == "spawn":
            self.spawn(pid, marked=True)
        elif operation == "fork":
            self.fork(pid, other)
        elif operation == "exit":
            self.exit(pid)
        elif operation == "zombie" and p:
            p.zombie = True
        elif operation == "reap" and p and p.zombie:
            self.exit(pid)
        elif operation == "reparent" and p:
            p.ppid = 1
        elif operation == "setsid" and p and p.pid != p.pgid and all(
                pid not in (q.pgid, q.sid) for q in self.processes.values()):
            p.pgid = p.sid = pid
        elif operation == "group" and p and p.pid != p.sid and other in self.processes:
            q = self.processes[other]
            if q.sid == p.sid:
                p.pgid = q.pgid
        elif operation == "chdir" and p:
            p.cwd = False
        elif operation == "scrub" and p:
            p.marked = False
        elif operation == "reuse_pid":
            old_group = p.pgid if p else other
            self.exit(pid)
            self.spawn(pid, pgid=old_group if other != pid else None)
        elif operation == "reuse_pgid" and all(q.pgid != pid for q in self.processes.values()):
            self.spawn(pid, writer=False, marked=False)
        if covered and not self.covered():
            self.processes = before
            event("excluded=C-5.7 invisible-writer residual")
        else:
            self.trace.append((operation, pid, other))

    def after(self, phase):
        count = self.read_counts.get(phase, 0) + 1
        self.read_counts[phase] = count
        self.trace.append(("read", phase, count))
        for hook in list(self.hooks):
            at, occurrence, callback = hook
            if at == phase and count == occurrence:
                self.hooks.remove(hook)
                callback()

    def rows(self):
        return {pid: (p.ppid, p.pgid, "Z" if p.zombie else "S", p.start)
                for pid, p in self.processes.items()}

    def snapshot(self):
        if "table" in self.failures:
            raise procs.InspectionError("world ps unavailable")
        rows = {pid: (*row[:3], "" if ("table", pid) in self.missing_starts else row[3])
                for pid, row in self.rows().items()}
        table = procs.ProcessTable(rows, BOOT)
        if self.capturing_ownership:
            self.record_ownership(table)
        roots = [p.pid for p in self.processes.values() if
                 (p.pid, p.start) in self.observed or p.pgid in self.groups]
        for pid in table.descendants(roots):
            p = self.processes[pid]
            self.observed.add((pid, p.start))
            self.groups.add(p.pgid)
        self.after("table")
        return table

    def record_ownership(self, table):
        leader = table.rows.get(100)
        if table.boot_id != BOOT or not leader or leader[1:] != (100, "S", "guardian-start"):
            return
        self.owned.update((pid, row[3]) for pid, row in table.rows.items()
                          if row[1] == 100 and not row[2].startswith("Z") and row[3])

    def identity(self, pid):
        phase = "confirm" if pid in self.confirming else "identity"
        self.confirming.discard(pid)
        if phase in self.failures:
            self.after(phase)
            raise procs.InspectionError(f"world {phase} unavailable")
        p = self.processes.get(pid)
        if p and not p.zombie and (phase, pid) in self.missing_starts:
            self.after(phase)
            raise procs.InspectionError("world missing process start identity")
        value = procs.ProcessIdentity(pid, BOOT, p.start) if p and not p.zombie else None
        if value and (phase, pid) in self.partial_starts:
            value = procs.ProcessIdentity(pid, BOOT, "")
        if value:
            self.observed.add((pid, p.start))
        self.after(phase)
        return value

    def group(self, pid):
        if "group" in self.failures:
            raise procs.InspectionError("world group unavailable")
        p = self.processes.get(pid)
        value = p.pgid if p else None
        if value:
            self.groups.add(value)
        self.confirming.add(pid)
        self.after("group")
        return value

    def same_process(self, pid, boot, start):
        p = self.processes.get(pid)
        match = (p is not None and not p.zombie and bool(start) and p.start == start and boot == BOOT
                 and "identity" not in self.failures and ("identity", pid) not in self.missing_starts
                 and ("identity", pid) not in self.partial_starts)
        if match:
            self.signal_checks[pid] = (boot, start)
        return match

    def read(self, argv, **kwargs):
        assert "pid=,command=" in argv, argv
        if "markers" in self.failures:
            raise procs.InspectionError("world marker ps unavailable")
        value = "".join(f"{p.pid} writer SUBFLEET_ATTEMPT={self.marker}\n"
                        for p in self.processes.values() if p.marked)
        self.after("markers")
        return value

    def cwd(self, workdir):
        if "cwd" in self.failures:
            raise procs.InspectionError("world lsof unavailable")
        value = {p.pid for p in self.processes.values() if p.cwd}
        self.after("cwd")
        return value

    def signal(self, pid, sig, *, via_group=False):
        p = self.processes.get(pid)
        assert not self.resolving, "resolvers must never signal"
        assert p is not None and not p.zombie and (
            p.pgid == 100 or (pid, p.start) in self.owned), (
            "S2 stray signal", pid, self.rows(), self.trace)
        authority = self.processes.get(100) if via_group else p
        assert authority is not None and self.signal_checks.get(authority.pid) == (BOOT, authority.start), (
            "S2 unconfirmed signal identity", pid, self.rows(), self.trace)
        self.signals.append((pid, p.start, int(sig)))

    def signal_group(self, pgid, sig):
        assert pgid == 100, ("S2 stray group signal", pgid)
        for p in list(self.processes.values()):
            if p.pgid == pgid and not p.zombie:
                self.signal(p.pid, sig, via_group=True)


class ProcessWorldMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.directory = tempfile.TemporaryDirectory(prefix="sf-process-world-", dir=os.environ["TMPDIR"])
        self.patch = pytest.MonkeyPatch()
        self.fixture = state_daemon.__wrapped__(Path(self.directory.name), self.patch)
        self.daemon, self.harness = next(self.fixture)
        self.clock = Clock(self.patch, self.daemon)
        self.attempts = [quarantine(self.daemon, self.harness) for _ in range(2)]
        for a in self.attempts:
            self.daemon.store.acquire_lease("native:" + a["attempt_id"], a["attempt_id"])
        self.world = World()
        self.last_verdicts = [False, False]
        self.daemon.term_grace_s = self.daemon.kill_settle_s = 0
        self.patch.setattr(procs, "containment", ORIGINAL_CENSUS)
        for name, method in (("snapshot", "snapshot"), ("identity", "identity"),
                             ("process_group", "group"), ("_read", "read"), ("cwd_pids", "cwd")):
            self.patch.setattr(procs, name, lambda *a, _method=method, **kw: getattr(self.world, _method)(*a, **kw))
        self.patch.setattr(procs, "_stat", lambda pid: self.world.rows().get(pid, (0, 0, None, ""))[2])
        self.patch.setattr(procs, "same_process", lambda pid, boot, start: self.world.same_process(pid, boot, start))
        # Kernel signal seams preserve production signal helper checks.
        self.patch.setattr(procs.os, "getpgid", lambda pid: self.world.processes[pid].pgid)
        self.patch.setattr(procs.os, "kill", lambda pid, sig: self.world.signal(pid, sig))
        self.patch.setattr(procs.os, "killpg", lambda pgid, sig: self.world.signal_group(pgid, sig))

    def active(self):
        return not any(self.last_verdicts)

    def ownership_pace(self):
        try:
            table = self.world.snapshot()
        except procs.InspectionError:
            event("ownership pace=table unavailable")
            return
        self.world.record_ownership(table)
        for a in self.attempts:
            self.daemon._record_owned(self.daemon.store.get_attempt(a["attempt_id"]), table)

    def pair(self, *, resolve=False):
        baseline = deepcopy(self.world)
        worlds, verdicts, censuses = [], [], []
        self.clock.advance()
        for operator, a in zip((False, True), self.attempts):
            self.world = deepcopy(baseline)
            self.world.marker = a["attempt_id"]
            self.world.read_counts = {}
            self.world.confirming.clear()
            self.world.resolving = resolve
            if resolve:
                self.daemon.store.update_attempt(a["attempt_id"], quarantine_recheck_at="")
                if operator:
                    self.daemon._resolve_quarantine(a, protocol.KillArgs(a["job_id"], confirm_dead=True))
                else:
                    # Only this twin is due, so the automatic batch cannot read
                    # the operator twin against the wrong marker world.
                    other = self.attempts[1]
                    self.daemon.store.update_attempt(other["attempt_id"], quarantine_recheck_at=self.clock.stamp(10))
                    self.daemon._recheck_quarantines()
                verdicts.append(self.daemon.store.get_attempt(a["attempt_id"])["state"] != "quarantined")
            else:
                censuses.append(self.daemon._contain(a).to_dict())
            self.world.resolving = False
            worlds.append(deepcopy(self.world))
        assert worlds[0].rows() == worlds[1].rows(), "P1 unequal read schedules"
        if resolve:
            assert verdicts[0] == verdicts[1], ("P1 resolver parity", verdicts, baseline.trace)
            self.last_verdicts = verdicts
        else:
            assert censuses[0] == censuses[1], "P1 census parity"
        self.world = worlds[0]
        self.safety()

    @initialize(source=st.sampled_from(["marker", "cwd"]), scenario=st.sampled_from(SCENARIOS),
                consumer=st.sampled_from(CONSUMERS))
    def initial(self, source, scenario, consumer):
        event("scenario=" + scenario)
        if scenario == "ordinary":
            return
        if scenario.startswith("paced-"):
            self.world.fork(100, 200)
            if scenario == "paced-missing-start":
                self.world.missing_starts.add(("table", 200))
                self.ownership_pace()
                self.world.change("setsid", 200)
                self.world.exit(100)
                self.pair(resolve=True)
                self.world.missing_starts.clear()
                self.pair(resolve=True)
            else:
                if scenario == "paced-owned-escape":
                    self.ownership_pace()
                self.world.change("setsid", 200)
                if scenario == "paced-unowned-escape":
                    self.ownership_pace()
                self.kill(consumer)
            return
        if scenario != "retained-authority":
            self.world.exit(100)
        # These are small, generated compositions of kernel transitions. The
        # census's first table misses the late writer; subsequent reads do not.
        def late_spawn():
            self.world.spawn(99, pgid=700, marked=source == "marker", cwd=source == "cwd")
        self.world.hooks.append(("table", 1, late_spawn))
        if scenario == "reused-before-group":
            def replace():
                self.world.exit(99)
                self.world.spawn(99, marked=False)
                self.world.fork(99, 300)
                self.world.failures.add("confirm")
            self.world.hooks.append(("identity", 1, replace))
        else:
            def fork_and_exit():
                self.world.fork(99, 200)
                self.world.processes[200].marked = self.world.processes[200].cwd = False
                self.world.exit(99)
            self.world.hooks.append(("group", 1, fork_and_exit))
        self.pair(resolve=True)
        self.world.failures.clear()
        if scenario == "retained-authority":
            self.world.processes[200].marked = source == "marker"
            self.world.processes[200].cwd = source == "cwd"
            self.world.failures.add("confirm")
            self.kill(consumer)
        else:
            self.pair(resolve=True)

    @precondition(lambda self: self.active())
    @rule(pid=PIDS, marked=st.booleans())
    def spawn_writer(self, pid, marked):
        self.world.spawn(pid, marked=marked, cwd=not marked)

    @precondition(lambda self: self.active())
    @rule(operation=OPERATIONS, pid=st.one_of(PIDS, st.just(100)), other=PIDS)
    def transition(self, operation, pid, other):
        event("operation=" + operation)
        self.world.change(operation, pid, other)

    @precondition(lambda self: self.active())
    @rule(phase=PHASES, operation=OPERATIONS, pid=PIDS, other=PIDS)
    def interleave(self, phase, operation, pid, other):
        self.world.hooks.append((phase, 1, lambda: self.world.change(operation, pid, other)))
        self.pair()
        self.world.hooks.clear()

    @precondition(lambda self: self.active())
    @rule(failure=st.sampled_from(["table", "markers", "cwd", "group", "identity", "confirm"]), fail=st.booleans())
    def fail_inspection(self, failure, fail):
        (self.world.failures.add if fail else self.world.failures.discard)(failure)

    @precondition(lambda self: self.active())
    @rule(pid=st.one_of(PIDS, st.just(100)), phase=st.sampled_from(["table", "identity", "confirm"]),
          missing=st.booleans())
    def start_column_visibility(self, pid, phase, missing):
        event("start visibility=" + phase + (" missing" if missing else " complete"))
        (self.world.missing_starts.add if missing else self.world.missing_starts.discard)((phase, pid))

    @precondition(lambda self: self.active())
    @rule(pid=PIDS, phase=st.sampled_from(["identity", "confirm"]), partial=st.booleans())
    def partial_start_identity(self, pid, phase, partial):
        event("partial identity=" + phase + str(partial))
        (self.world.partial_starts.add if partial else self.world.partial_starts.discard)((phase, pid))

    @precondition(lambda self: self.active())
    @rule()
    def paced_ownership_capture(self):
        self.ownership_pace()

    @precondition(lambda self: self.active())
    @rule()
    def census_pace(self):
        self.pair()

    @precondition(lambda self: self.active())
    @rule()
    def resolver_call(self):
        self.pair(resolve=True)

    @precondition(lambda self: self.active())
    @rule(consumer=st.sampled_from(["attempt", "probe"]))
    def kill(self, consumer):
        a = self.attempts[0]
        self.world.marker = a["attempt_id"]
        self.world.read_counts = {}
        self.world.confirming.clear()
        self.world.capturing_ownership = True
        if consumer == "attempt":
            self.daemon._kill_attempt(self.daemon.store.get_attempt(a["attempt_id"]))
        else:
            evidence = json.loads(self.daemon.store.get_attempt(a["attempt_id"])["evidence_json"])
            record = {**evidence, "holder": a["attempt_id"], "job_id": a["job_id"],
                      "lane_id": a["lane_id"], "directory": str(self.harness.workdir),
                      "guardian_pid": 100, "pgid": 100, "boot_id": BOOT,
                      "proc_start": "guardian-start", "state": "running"}
            self.daemon._contain_probe(record)
        self.world.capturing_ownership = False
        # Kill evidence may have changed only one twin; copy the durable roots
        # to keep future parity checks about equal inputs.
        actual = self.daemon.store.get_attempt(a["attempt_id"])
        self.daemon.store.update_attempt(self.attempts[1]["attempt_id"], evidence_json=actual["evidence_json"],
                                         quarantine_reason=actual["quarantine_reason"])

    @invariant()
    def safety(self):
        alive = [p for p in self.world.processes.values() if p.writer and not p.zombie]
        for a in self.attempts:
            actual = self.daemon.store.get_attempt(a["attempt_id"])
            leases = [l for l in self.daemon.store.list_leases() if l["holder"] in {a["attempt_id"], a["job_id"]}]
            assert not alive or (actual["state"] == "quarantined" and leases and self.harness.workdir.exists()), (
                "S1 premature release", actual["state"], self.world.rows(), self.world.trace)
        assert self.last_verdicts[0] == self.last_verdicts[1], "P1 parity"

    @rule()
    def quiesce_and_check_liveness(self):
        self.world.processes.clear()
        self.world.failures.clear()
        self.world.hooks.clear()
        self.pair(resolve=True)
        assert all(self.last_verdicts), "L1 all evidence empty must release within one pace"
        for a in self.attempts:
            assert not [l for l in self.daemon.store.list_leases() if l["holder"] in {a["attempt_id"], a["job_id"]}]

    def teardown(self):
        try:
            self.fixture.close()
        finally:
            self.patch.undo()
            self.directory.cleanup()


TestProcessWorld = ProcessWorldMachine.TestCase
TestProcessWorld.settings = settings(max_examples=int(os.environ.get("SF_WORLD_EXAMPLES", "100")),
    stateful_step_count=25, deadline=None, database=None,
    phases=[phase for phase in Phase if phase != Phase.explain])


def test_unconditional_s1_has_the_documented_invisible_writer_counterexample():
    """This passes by demonstrating the residual, not by claiming S1 holds."""
    machine = ProcessWorldMachine()
    try:
        machine.world.spawn(99)
        machine.world.fork(99, 200)
        machine.world.change("setsid", 200, covered=False)
        machine.world.change("scrub", 200, covered=False)
        machine.world.change("chdir", 200, covered=False)
        machine.world.exit(99)
        machine.world.exit(100)
        assert not machine.world.covered()
        with pytest.raises(AssertionError, match="S1 premature release"):
            machine.pair(resolve=True)
    finally:
        machine.teardown()
