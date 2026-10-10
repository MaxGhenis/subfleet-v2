"""Executable C-5 process world, using the production census and resolvers.

The oracle is the simulated kernel, never the census verdict. Reads take their
own snapshots; queued world transitions run after a selected read. No processes
are spawned or signalled. The safety domain is C-5.7's census-covered lineage;
the invisible-writer residual is demonstrated separately below. Conservative
foreign group evidence must also empty before bounded liveness can apply.
"""
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import atexit
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

from hypothesis import Phase, event, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule
import pytest

from subfleet import procs, protocol
from tests.fake.test_quarantine_self_resolve import Clock
from tests.fake.test_review_pr131_probes import BOOT, ORIGINAL_CENSUS, quarantine
from tests.fake.test_state_contract import reserve, state_daemon
from tests.fake.test_workspace_contract import repository
from tests.fake.quarantine_world_pool import PreparedWorld


PREPARED_WORLD = None


def close_prepared_world():
    global PREPARED_WORLD
    if PREPARED_WORLD is not None:
        prepared, PREPARED_WORLD = PREPARED_WORLD, None
        prepared.close()


atexit.register(close_prepared_world)


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
             "paced-missing-start", "paced-owned-escape", "paced-unowned-escape",
             "missing-leader-owned-escape", "missing-member-unowned-escape",
             "owned-without-shape", "owned-after-census-escape", "paced-owned-table-outage",
             "recorded-identity-restart"]
if os.environ.get("SF_WORLD_SCENARIO"):
    SCENARIOS = [os.environ["SF_WORLD_SCENARIO"]]
CONSUMERS = [os.environ["SF_WORLD_CONSUMER"]] if os.environ.get("SF_WORLD_CONSUMER") else ["attempt", "probe"]


class ModelConnection(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        # Keep real schemas, queries and commits. This kernel-world model does
        # not claim power-loss durability, including while its fixtures migrate.
        if sql.replace(" ", "").upper() == "PRAGMASYNCHRONOUS=FULL":
            sql = "PRAGMA synchronous=NORMAL"
        return super().execute(sql, *args, **kwargs)


@contextmanager
def fixture_io(patch):
    connect = sqlite3.connect
    def model_connect(*args, **kwargs):
        kwargs.setdefault("factory", ModelConnection)
        return connect(*args, **kwargs)
    with patch.context() as scope:
        scope.setattr(os, "fsync", lambda fd: None)
        scope.setattr(sqlite3, "connect", model_connect)
        yield


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
        self.ownership_candidates = set()
        # Read visibility varies independently of the kernel's real identity.
        self.missing_starts = set()
        self.partial_starts = set()
        self.omit_shapes = False
        self.term_members = set()

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
            # A fresh scalar confirmation of the original leader can verify
            # this group even when its table start is unavailable. Only full
            # table member identities qualify; later samples alone do not.
            if table.boot_id == BOOT:
                self.ownership_candidates.update((pid, row[3]) for pid, row in table.rows.items()
                                                 if row[1] == 100 and not row[2].startswith("Z") and row[3])
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
        match = self.confirmable(pid, boot, start)
        if match:
            self.signal_checks[pid] = (boot, start)
            if pid == 100 and self.capturing_ownership:
                self.owned.update(self.ownership_candidates)
        return match

    def confirmable(self, pid, boot, start):
        """Kernel truth plus scalar visibility, independently of census output."""
        p = self.processes.get(pid)
        return (p is not None and not p.zombie and bool(start) and p.start == start and boot == BOOT
                and "identity" not in self.failures and ("identity", pid) not in self.missing_starts
                and ("identity", pid) not in self.partial_starts)

    def census(self, *args, **kwargs):
        census = ORIGINAL_CENSUS(*args, **kwargs)
        # Legacy containment fixtures/receipts may have full identities without
        # shapes. This hides reported topology, never the kernel's identity.
        return replace(census, shapes={}) if self.omit_shapes else census

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
        leader = self.processes.get(100)
        original_group = leader is None or leader.start == "guardian-start"
        assert not self.resolving, "resolvers must never signal"
        assert p is not None and not p.zombie and (
            (p.pgid == 100 and original_group) or (pid, p.start) in self.owned), (
            "S2 stray signal", pid, self.rows(), self.trace)
        authority = self.processes.get(100) if via_group else p
        assert authority is not None and self.signal_checks.get(authority.pid) == (BOOT, authority.start), (
            "S2 unconfirmed signal identity", pid, self.rows(), self.trace)
        self.signals.append((pid, p.start, int(sig)))

    def signal_group(self, pgid, sig):
        assert pgid == 100, ("S2 stray group signal", pgid)
        if int(sig) == 15:
            self.term_members.update((p.pid, p.start) for p in self.processes.values()
                                     if p.pgid == pgid and not p.zombie)
        for p in list(self.processes.values()):
            if p.pgid == pgid and not p.zombie:
                self.signal(p.pid, sig, via_group=True)
        self.after("signal_group")


class ProcessWorldMachine(RuleBasedStateMachine):
    def __init__(self, *, prepared=False):
        global PREPARED_WORLD
        super().__init__()
        self.patch = pytest.MonkeyPatch()
        self.prepared = None
        if prepared and PREPARED_WORLD is not None:
            self.prepared = PREPARED_WORLD
            self.setup_prepared_world()
            return
        self.directory = tempfile.TemporaryDirectory(prefix="sf-process-world-", dir=os.environ["TMPDIR"])
        # This model has no power-loss invariant. Omit only fixture setup's
        # file durability waits; real content and SQL commits remain exercised.
        with fixture_io(self.patch):
            self.setup_world()
        if prepared:
            PREPARED_WORLD = self.prepared = PreparedWorld(self)

    @staticmethod
    def close_pool():
        close_prepared_world()

    def setup_prepared_world(self):
        prepared = self.prepared
        self.directory, self.harness, self.fixture = prepared.directory, prepared.harness, prepared.fixture
        self.attempts = deepcopy(prepared.attempts)
        self.protected_leases = deepcopy(prepared.leases)
        self.protected_workspaces = deepcopy(prepared.workspaces)
        try:
            with fixture_io(self.patch):
                prepared.restore_databases()
                self.patch.setattr(procs, "boot_id", lambda: "unit-test-boot")
                self.patch.setattr(procs, "proc_start", lambda pid: "unit-test-start")
                self.daemon = prepared.daemon_type(self.harness.root, term_grace_s=0, kill_settle_s=0)
            self.clock = Clock(self.patch, self.daemon)
            self.daemon.store.connection.execute("PRAGMA synchronous=NORMAL")
            def refuse_real_launch(*args):
                raise AssertionError("process-world fixtures must never launch a guardian")
            self.patch.setattr(self.daemon, "_launch", refuse_real_launch)
            self.bind_world()
            prepared.check_reset(self.daemon)
            prepared.attach(self)
        except BaseException:
            if hasattr(self, "daemon"):
                self.daemon.close()
            self.patch.undo()
            prepared.poisoned = True
            close_prepared_world()
            raise

    def setup_world(self):
        self.fixture = state_daemon.__wrapped__(Path(self.directory.name), self.patch)
        self.daemon, self.harness = next(self.fixture)
        # This kernel model exercises committed state, not power-loss recovery.
        self.daemon.store.connection.execute("PRAGMA synchronous=NORMAL")
        self.clock = Clock(self.patch, self.daemon)
        self.attempts = [quarantine(self.daemon, self.harness)]
        repository(self.daemon, self.harness)
        _, writable, _ = reserve(self.daemon, self.harness, sandbox="workspace-write")
        self.daemon.store.update_attempt(writable["attempt_id"], guardian_pid=100, pgid=100,
                                         boot_id=BOOT, proc_start="guardian-start")
        self.daemon._quarantine(self.daemon.store.get_attempt(writable["attempt_id"]),
                                procs.Containment(), "writers remain after exit receipt")
        self.attempts.append(self.daemon.store.get_attempt(writable["attempt_id"]))
        for a in self.attempts:
            self.daemon.store.acquire_lease("native:" + a["attempt_id"], a["attempt_id"])
        self.protected_leases = {}
        self.protected_workspaces = {}
        for a in self.attempts:
            self.protected_leases[a["attempt_id"]] = frozenset(
                (lease["lease_key"], lease["holder"]) for lease in self.daemon.store.list_leases()
                if lease["holder"] in {a["attempt_id"], a["job_id"]})
            job = self.daemon.store.get_job(a["job_id"])
            self.protected_workspaces[a["attempt_id"]] = {Path(job["workdir"])}
            if job["worktree"]:
                self.protected_workspaces[a["attempt_id"]].add(Path(job["worktree"]))
        self.bind_world()

    def bind_world(self):
        self.world = World()
        # The probe fixture is seeded once from the attempt's current recorded
        # evidence and independent oracle, so both consumers start with equal
        # inputs. Subsequent ownership and census observations stay separate.
        self.probe_record = None
        self.probe_owned = None
        self.probe_observed = None
        self.probe_groups = None
        self.recorded_only = False
        self.last_verdicts = [False, False]
        self.daemon.term_grace_s = self.daemon.kill_settle_s = 0
        self.patch.setattr(procs, "containment", lambda *a, **kw: self.world.census(*a, **kw))
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

    def record_identity_only_writers(self):
        """Legacy holds can begin with identities and no launch/group evidence."""
        self.world.exit(100)
        for pid in (200, 201):
            self.world.spawn(pid, marked=False)
            self.world.observed.add((pid, self.world.processes[pid].start))
        identities = {str(pid): asdict(procs.ProcessIdentity(pid, BOOT, self.world.processes[pid].start))
                      for pid in (200, 201)}
        for a in self.attempts:
            self.daemon.store.update_attempt(a["attempt_id"], guardian_pid=None, child_pid=None, pgid=None,
                boot_id=None, proc_start=None, evidence_json="{}",
                quarantine_reason=json.dumps({"identities": identities}))
        self.recorded_only = True

    def restart_daemon(self):
        with fixture_io(self.patch):
            self.daemon.close()
            self.daemon = type(self.daemon)(self.harness.root, term_grace_s=0, kill_settle_s=0)
        self.daemon.policy["quarantine_recheck_s"] = 10
        self.daemon.store.connection.execute("PRAGMA synchronous=NORMAL")
        def refuse_real_launch(*args):
            raise AssertionError("process-world fixtures must never launch a guardian")
        self.patch.setattr(self.daemon, "_launch", refuse_real_launch)
        if self.prepared is not None:
            self.daemon.publish_hook = self.prepared.track_publication
        if self.probe_record is not None:
            restored = self.daemon._probe_record(self.probe_record["holder"])
            assert restored is not None, "probe authority was not durable across restart"
            self.probe_record = restored
        self.world.trace.append(("restart",))

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
        if scenario == "recorded-identity-restart":
            self.record_identity_only_writers()
            self.pair(resolve=True)
            self.restart_daemon()
            self.world.exit(200)
            self.world.spawn(200, marked=False, writer=False)
            self.pair(resolve=True)
            return
        if scenario == "paced-owned-table-outage":
            self.world.fork(100, 200)
            self.ownership_pace()
            self.world.change("setsid", 200)
            self.world.processes[200].marked = self.world.processes[200].cwd = False
            self.world.failures.add("table")
            self.kill(consumer)
            return
        if scenario in {"owned-without-shape", "owned-after-census-escape"}:
            self.world.fork(100, 200)
            if scenario == "owned-without-shape":
                self.world.omit_shapes = True
                self.world.hooks.append(("signal_group", 1, lambda: self.world.change("setsid", 200)))
            else:
                # The initial table proved group ownership. A later marker
                # bracket observes its escape and overwrites the census shape.
                self.world.processes[200].marked = source == "marker"
                self.world.processes[200].cwd = source == "cwd"
                self.world.hooks.append(("table", 1, lambda: self.world.change("setsid", 200)))
            self.kill(consumer)
            assert self.world.processes[200].pgid == 200
            return
        if scenario in {"missing-leader-owned-escape", "missing-member-unowned-escape"}:
            self.world.fork(100, 200)
            self.world.missing_starts.add(("table", 100))
            complete_member = scenario == "missing-leader-owned-escape"
            if not complete_member:
                self.world.missing_starts.add(("table", 200))
                self.world.processes[200].marked = source == "marker"
                self.world.processes[200].cwd = source == "cwd"
            # A child may handle SIGTERM and escape before its first SIGKILL.
            self.world.hooks.append(("signal_group", 1, lambda: self.world.change("setsid", 200)))
            self.kill(consumer)
            assert self.world.processes[200].pgid == 200
            assert any(pid == 200 and sig == 9 for pid, _, sig in self.world.signals) == complete_member
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
    @rule()
    def daemon_restart(self):
        self.restart_daemon()

    @precondition(lambda self: self.active() and self.recorded_only)
    @rule(pid=st.sampled_from([200, 201]))
    def recorded_writer_pid_reuse(self, pid):
        # A reused PID belongs to another process; the original writer is gone.
        self.world.exit(pid)
        self.world.spawn(pid, marked=False, writer=False)

    @precondition(lambda self: self.active())
    @rule(consumer=st.sampled_from(["attempt", "probe"]))
    def kill(self, consumer):
        a = self.attempts[0]
        first_signal = len(self.world.signals)
        self.world.term_members.clear()
        self.world.marker = a["attempt_id"]
        self.world.read_counts = {}
        self.world.confirming.clear()
        self.world.capturing_ownership = True
        self.world.ownership_candidates.clear()
        attempt_owned, attempt_observed, attempt_groups = (
            self.world.owned, self.world.observed, self.world.groups)
        if consumer == "probe" and self.probe_record is None:
            evidence = json.loads(self.daemon.store.get_attempt(a["attempt_id"])["evidence_json"])
            self.probe_record = {**evidence, "holder": a["attempt_id"], "job_id": a["job_id"],
                                 "lane_id": a["lane_id"], "directory": str(self.harness.workdir),
                                 "guardian_pid": 100, "pgid": 100, "boot_id": BOOT,
                                 "proc_start": "guardian-start", "state": "running"}
            self.probe_owned = set(attempt_owned)
            self.probe_observed = set(attempt_observed)
            self.probe_groups = set(attempt_groups)
        if consumer == "probe":
            self.world.owned, self.world.observed, self.world.groups = (
                self.probe_owned, self.probe_observed, self.probe_groups)
        try:
            if consumer == "attempt":
                self.daemon._kill_attempt(self.daemon.store.get_attempt(a["attempt_id"]))
            else:
                # This action initiates a new kill, as the original fixture
                # did. A passive recheck of an already quarantined probe does
                # not initiate C-5.6, so only its state is reset here; all its
                # own recorded identities and lineage survive previous kills.
                self.probe_record["state"] = "running"
                self.daemon._contain_probe(self.probe_record)
                self.probe_record = self.daemon._probe_record(a["attempt_id"])
                assert self.probe_record is not None, "probe kill authority was not persisted"
            # Both K1 and the kernel signal seam's S2 use this consumer's
            # authority before the attempt scope is restored below.
            self.kill_liveness(first_signal)
        finally:
            self.world.capturing_ownership = False
            if consumer == "probe":
                self.probe_owned, self.probe_observed, self.probe_groups = (
                    self.world.owned, self.world.observed, self.world.groups)
                self.world.owned, self.world.observed, self.world.groups = (
                    attempt_owned, attempt_observed, attempt_groups)
        # Kill evidence may have changed only one twin; copy the durable roots
        # to keep future parity checks about equal inputs.
        actual = self.daemon.store.get_attempt(a["attempt_id"])
        self.daemon.store.update_attempt(self.attempts[1]["attempt_id"], evidence_json=actual["evidence_json"],
                                         quarantine_reason=actual["quarantine_reason"])

    def kill_liveness(self, first_signal):
        """K1: a confirmed owned survivor must receive the kill protocol."""
        sent = self.world.signals[first_signal:]
        for pid, start in self.world.owned:
            if not self.world.confirmable(pid, BOOT, start):
                continue
            deliveries = [(index, sig) for index, (target, identity, sig) in enumerate(sent)
                          if (target, identity) == (pid, start)]
            assert any(sig == 9 for _, sig in deliveries), (
                "K1 owned survivor not signalled", pid, start, self.world.rows(), self.world.trace, sent)
            terms = [index for index, sig in deliveries if sig == 15]
            kills = [index for index, sig in deliveries if sig == 9]
            assert not terms or min(terms) < min(kills), (
                "K1 escalation precedes SIGTERM", pid, deliveries, self.world.trace)
            if (pid, start) in self.world.term_members:
                assert terms, ("K1 owned group member misses SIGTERM", pid, deliveries, self.world.trace)

    @invariant()
    def safety(self):
        alive = [p for p in self.world.processes.values() if p.writer and not p.zombie]
        for a in self.attempts:
            actual = self.daemon.store.get_attempt(a["attempt_id"])
            leases = {(l["lease_key"], l["holder"]) for l in self.daemon.store.list_leases()
                      if l["holder"] in {a["attempt_id"], a["job_id"]}}
            held = actual["state"] == "quarantined"
            assert (not alive or held) and (not held or (
                self.protected_leases[a["attempt_id"]] <= leases
                and all(path.exists() for path in self.protected_workspaces[a["attempt_id"]]))), (
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
        if self.prepared is not None:
            try:
                with fixture_io(self.patch):
                    self.prepared.finish(self)
            finally:
                if self.prepared.poisoned:
                    close_prepared_world()
            return
        try:
            with fixture_io(self.patch):
                self.daemon.close()
                self.fixture.close()
        finally:
            self.patch.undo()
            self.directory.cleanup()


class ProofProcessWorldMachine(ProcessWorldMachine):
    def __init__(self):
        super().__init__(prepared=os.environ.get("SF_WORLD_POOL") == "1")


TestProcessWorld = ProofProcessWorldMachine.TestCase
TestProcessWorld.settings = settings(max_examples=int(os.environ.get("SF_WORLD_EXAMPLES", "100")),
    stateful_step_count=int(os.environ.get("SF_WORLD_STEPS", "25")),
    deadline=int(os.environ.get("SF_WORLD_DEADLINE_MS", "30000")), database=None,
    phases=[phase for phase in Phase if phase != Phase.explain])


def test_prepared_world_matches_fresh_and_restores_both_stores_and_receipts():
    def exercise(*, prepared, contaminate=False):
        machine = ProcessWorldMachine(prepared=prepared)
        machine.patch.setattr(sys.modules[__name__], "event", lambda *args: None)
        try:
            for connection in (machine.daemon.store.connection, machine.daemon.conversations.store._db):
                assert not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='world_pool_mutation'").fetchone()
            receipt = machine.daemon.root / "jobs" / machine.attempts[0]["job_id"] / "a1" / "exit.json"
            assert not receipt.exists()
            machine.initial("marker", "owned-after-census-escape", "attempt")
            machine.quiesce_and_check_liveness()
            machine.restart_daemon()
            signature = (machine.world.rows(), machine.world.signals, machine.world.trace,
                         machine.last_verdicts,
                         [machine.daemon.store.get_attempt(a["attempt_id"])["state"] for a in machine.attempts],
                         [len([l for l in machine.daemon.store.list_leases()
                               if l["holder"] in {a["attempt_id"], a["job_id"]}]) for a in machine.attempts])
            if contaminate:
                for connection in (machine.daemon.store.connection, machine.daemon.conversations.store._db):
                    connection.execute("CREATE TABLE world_pool_mutation (value TEXT)")
                    connection.execute("INSERT INTO world_pool_mutation VALUES ('previous world')")
                machine.daemon._publish("pool-test", receipt, b"{}\n")
            return deepcopy(signature)
        finally:
            machine.teardown()
    fresh = exercise(prepared=False)
    try:
        assert exercise(prepared=True, contaminate=True) == fresh
        assert exercise(prepared=True) == fresh
    finally:
        close_prepared_world()


@pytest.mark.parametrize("change", ["new-file", "new-ref", "changed-file"])
def test_prepared_world_retires_after_workspace_or_ref_changes(change):
    machine = ProcessWorldMachine(prepared=True)
    directory = Path(machine.directory.name)
    try:
        if change == "new-file":
            (machine.harness.workdir / "previous-world-file").write_text("unexpected\n")
        elif change == "new-ref":
            refs = next(iter(machine.prepared.ref_paths))
            head = machine.daemon.store.get_job(machine.attempts[1]["job_id"])["workdir_head"]
            (refs / "previous-world-ref").write_text(head + "\n")
        else:
            (machine.harness.workdir / "tracked.txt").write_text("unexpected\n")
        with pytest.raises(AssertionError, match="pooled (workspace paths|Git refs|protected workspace)"):
            machine.teardown()
        assert PREPARED_WORLD is None and not directory.exists()
        replacement = ProcessWorldMachine(prepared=True)
        try:
            assert (replacement.harness.workdir / "tracked.txt").read_text() == "baseline\n"
            assert not (replacement.harness.workdir / "previous-world-file").exists()
            assert all(not (refs / "previous-world-ref").exists() for refs in replacement.prepared.ref_paths)
        finally:
            replacement.teardown()
    finally:
        close_prepared_world()


@pytest.mark.parametrize("consumer", ["attempt", "probe"])
def test_k1_owned_survivor_without_reported_shape_is_signalled(consumer):
    machine = ProcessWorldMachine()
    machine.patch.setattr(sys.modules[__name__], "event", lambda *args: None)
    try:
        machine.initial("marker", "owned-without-shape", consumer)
        assert any(pid == 200 and sig == 9 for pid, _, sig in machine.world.signals)
        machine.safety()
    finally:
        machine.teardown()


@pytest.mark.parametrize("consumer", ["attempt", "probe"])
def test_k1_owned_child_escaping_during_census_is_signalled(consumer):
    machine = ProcessWorldMachine()
    machine.patch.setattr(sys.modules[__name__], "event", lambda *args: None)
    try:
        machine.initial("marker", "owned-after-census-escape", consumer)
        assert any(pid == 200 and sig == 9 for pid, _, sig in machine.world.signals)
        machine.safety()
    finally:
        machine.teardown()


@pytest.mark.parametrize("consumer", ["attempt", "probe"])
def test_k1_owned_child_is_signalled_during_table_outage(consumer):
    machine = ProcessWorldMachine()
    machine.patch.setattr(sys.modules[__name__], "event", lambda *args: None)
    try:
        machine.initial("marker", "paced-owned-table-outage", consumer)
        assert any(pid == 200 and sig == 9 for pid, _, sig in machine.world.signals)
        machine.safety()
    finally:
        machine.teardown()


def test_probe_ownership_does_not_grant_attempt_signal_authority():
    machine = ProcessWorldMachine()
    machine.patch.setattr(sys.modules[__name__], "event", lambda *args: None)
    try:
        machine.initial("marker", "missing-leader-owned-escape", "probe")
        child = (200, machine.world.processes[200].start)
        assert child in machine.probe_owned and child not in machine.world.owned
        assert machine.probe_record["owned_identities"]["200"]["proc_start"] == child[1]
        evidence = json.loads(machine.daemon.store.get_attempt(machine.attempts[0]["attempt_id"])["evidence_json"])
        assert "200" not in evidence.get("owned_identities", {})
        first_signal = len(machine.world.signals)
        machine.kill("attempt")
        assert all(pid != 200 for pid, _, _ in machine.world.signals[first_signal:])
        assert child in machine.probe_owned and child not in machine.world.owned
        assert machine.world.same_process(200, BOOT, child[1])
        with pytest.raises(AssertionError, match="S2 stray signal"):
            machine.world.signal(200, 9)
        machine.safety()
    finally:
        machine.teardown()


def test_repeated_probe_kill_retains_owned_identity_after_restart_and_table_outage():
    machine = ProcessWorldMachine()
    machine.patch.setattr(sys.modules[__name__], "event", lambda *args: None)
    try:
        machine.initial("marker", "missing-leader-owned-escape", "probe")
        child = (200, machine.world.processes[200].start)
        machine.restart_daemon()
        assert machine.probe_record["owned_identities"]["200"]["proc_start"] == child[1]
        machine.world.failures.add("table")
        first_signal = len(machine.world.signals)
        machine.kill("probe")
        assert (200, child[1], 9) in machine.world.signals[first_signal:]
        assert child in machine.probe_owned and child not in machine.world.owned
        machine.safety()
    finally:
        machine.teardown()


def test_probe_census_does_not_expand_attempt_coverage():
    machine = ProcessWorldMachine()
    machine.patch.setattr(sys.modules[__name__], "event", lambda *args: None)
    try:
        machine.initial("marker", "missing-leader-owned-escape", "probe")
        child = (200, machine.world.processes[200].start)
        assert child in machine.probe_owned and child in machine.probe_observed
        assert child not in machine.world.owned and child not in machine.world.observed
        assert 200 in machine.probe_groups and 200 not in machine.world.groups
        assert machine.world.covered()
        before = deepcopy(machine.world.processes)
        machine.world.change("exit", 100)
        assert machine.world.processes == before and machine.world.covered()
        # Bypassing the generated-domain guard leaves the attempt's invisible
        # residual. Probe observations do not make that an S1-covered attempt;
        # the unconditional residual's release is demonstrated separately.
        machine.world.exit(100)
        assert not machine.world.covered()
        assert machine.world.processes[200].writer
    finally:
        machine.teardown()


def test_k1_oracle_rejects_omitted_owned_survivor_signal():
    machine = ProcessWorldMachine()
    machine.patch.setattr(sys.modules[__name__], "event", lambda *args: None)
    try:
        machine.world.fork(100, 200)
        machine.world.record_ownership(machine.world.snapshot())
        machine.world.same_process(100, BOOT, "guardian-start")
        machine.world.signal_group(100, 15)
        machine.world.change("setsid", 200)
        machine.world.signal_group(100, 9)
        with pytest.raises(AssertionError, match="K1 owned survivor not signalled"):
            machine.kill_liveness(0)
    finally:
        machine.teardown()


def test_s1_recorded_identities_survive_restart_and_reuse():
    machine = ProcessWorldMachine()
    machine.patch.setattr(sys.modules[__name__], "event", lambda *args: None)
    try:
        machine.initial("marker", "recorded-identity-restart", "attempt")
        assert not any(machine.last_verdicts)
        machine.world.missing_starts.add(("table", 201))
        machine.pair(resolve=True)
        assert not any(machine.last_verdicts)
        machine.restart_daemon()
        machine.world.missing_starts.clear()
        machine.world.exit(201)
        machine.pair(resolve=True)
        # The reused PID now heads an unrelated group. Its retained numeric
        # group remains conservative evidence until that group is empty too.
        assert not any(machine.last_verdicts)
        machine.world.exit(200)
        machine.pair(resolve=True)
        assert all(machine.last_verdicts)
    finally:
        machine.teardown()


def test_s1_oracle_rejects_release_with_recorded_writer():
    machine = ProcessWorldMachine()
    machine.patch.setattr(sys.modules[__name__], "event", lambda *args: None)
    try:
        machine.record_identity_only_writers()
        machine.daemon.store.update_attempt(machine.attempts[0]["attempt_id"], state="lost")
        with pytest.raises(AssertionError, match="S1 premature release"):
            machine.safety()
    finally:
        machine.teardown()


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
