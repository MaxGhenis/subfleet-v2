"""D-QUAR-START1 (D4), C-4.2, C-5.5: another process's failed marker read does not end a slow start.

2026-10-27, on the shared host at load 94 to 101, `tests/fake/test_quar_start1_daemon.py`
failed 5 of 6 runs, at `c9430763f` and at #270's head alike. In each failure the census
at which the extension ended was unverifiable with one error, "marker read unavailable
for pid N", N a process other than the guardian, and the guardian was KILLed while it was
still starting. Measured the same day at load 19 to 43: every refused read was of a newborn
of this user (EIO or EINVAL, in its first milliseconds, read whole or gone soon after), 20 of
900 whole-host passes met one, and none filled the buffer
(`docs/reports/2026-10-27-quar-start1-census-under-load.md`).

1. The census names the processes whose marker read failed (`markers_unread`) and stays
   unverifiable exactly as before; `only_markers_unread` holds only when those are all its
   errors.
2. Start grace's extension is granted past such a census, and past no other: the guardian's
   own failed read, any other error, another member, a stopped guardian and the bound still
   end it through the existing branch. (2b) The guardian's own identity readers, `sysctl`
   and `ps`, its children in its group for a few milliseconds before `start.json`, are the
   one kind of member admitted beside it.
3. The premise, executed: before `start.json` the guardian starts no process that carries the
   attempt's markers, and none but its identity readers.
4. Mutants of each part fail a named test here.

No real process is inspected or signalled except in 3, where the identity readers of an
in-process guardian run for real.
"""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import sys
import time

from hypothesis import HealthCheck, example, given, settings, strategies as st
import pytest

from subfleet import daemon as daemon_module, guardian as guardian_module, procs
from subfleet.contracts import START_GRACE_S
from subfleet.daemon import Daemon
from tests.kill4_world import BOOT, World
from tests.unit.test_census_round1_mutations import replace_function
from tests.unit.test_daemon_settle import ATTEMPT, JOB, STARTED, attempt, daemon  # noqa: F401
from tests.unit.test_kill4_fix2b import MAX_S, START, abandon_seen_at_each_signal, adir, publish, settle, starting
from tests.unit.test_kill4_terminal_containment import kinds, world  # noqa: F401

KILL = int(signal.SIGKILL)
GUARDIAN = 4242
#: Refused on every table a test reads.
ALWAYS = 10 ** 6


def census_of(world: World) -> procs.Containment:
    """The census `Daemon._contain` takes of the starting attempt: the guardian is its group
    leader, its responsibility root's pid and its one recorded identity."""
    return procs.containment(GUARDIAN, GUARDIAN, None, ATTEMPT, world.root,
                             recorded_identities={GUARDIAN: world.identity(GUARDIAN)}, attempt_boot=BOOT)


def extension(daemon) -> list[dict]:
    return [json.loads(row["data_json"]) for row in daemon.store.list_events(JOB)
            if row["kind"] == "attempt.start_extended"]


# 1. The census names what it could not read.

def test_a_refused_marker_read_is_named_and_leaves_the_census_unverifiable(monkeypatch):
    world = World(ATTEMPT, "/state").install(monkeypatch)
    world.add(GUARDIAN, 1, GUARDIAN, start=STARTED, marked=True)
    world.add(7000, 1, 7000)                            # a newborn stranger, in its first milliseconds
    world.add(7001, 1, 7001)                            # a stranger read whole
    world.fail_reads[7000] = ALWAYS
    census = census_of(world)
    assert census.unverifiable and census.errors == (procs.marker_unread(7000),)
    assert census.markers_unread == {7000} and census.only_markers_unread
    assert census.marker_pids == {GUARDIAN} and census.live_pids == {GUARDIAN}
    assert "markers_unread" not in census.to_dict()     # the evidence a census records is unchanged


def test_a_census_with_any_other_error_is_not_only_markers_unread(monkeypatch):
    world = World(ATTEMPT, "/state").install(monkeypatch)
    world.add(GUARDIAN, 1, GUARDIAN, start=STARTED, marked=True)
    world.add(7000, 1, 7000)
    world.fail_reads[7000] = ALWAYS
    world.fail_tables = 1                               # ps fails once: the group and walk are unread
    census = census_of(world)
    assert census.markers_unread == {7000} and not census.only_markers_unread, census.errors
    assert procs.Containment(unverifiable=True).only_markers_unread is False
    named_elsewhere = procs.Containment(unverifiable=True, errors=(procs.marker_unread(7000),))
    assert not named_elsewhere.only_markers_unread      # a census that does not name its unread pids


@settings(max_examples=120, deadline=None, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(strangers=st.lists(st.tuples(st.sampled_from(["whole", "refused", "cut"]), st.booleans(), st.booleans()),
                         max_size=6),
       guardian_unread=st.booleans(), table_fails=st.booleans())
def test_markers_unread_is_exactly_the_unread_live_readable_processes(strangers, guardian_unread, table_fails):
    """For every world: `markers_unread` is the set of live processes of this user whose
    marker read failed, refused or with a buffer that fills the read (`cut`), the census is
    unverifiable iff it has an error, and `only_markers_unread` holds iff its errors are
    exactly those reads' errors."""
    with pytest.MonkeyPatch.context() as patch:
        world = World(ATTEMPT, "/state").install(patch)
        world.add(GUARDIAN, 1, GUARDIAN, start=STARTED, marked=True)
        refused = set()
        for n, (read, marked, other_user) in enumerate(strangers):
            pid = 7000 + n
            world.add(pid, 1, pid, marked=marked, uid=os.geteuid() + 1 if other_user else os.geteuid())
            if read != "whole":
                if read == "refused":
                    world.fail_reads[pid] = ALWAYS
                else:
                    world.cut_reads.add(pid)
                if not other_user:                      # another user's is skipped (EPERM), never read
                    refused.add(pid)
        if guardian_unread:
            world.fail_reads[GUARDIAN] = ALWAYS
            refused.add(GUARDIAN)
        world.fail_tables = 1 if table_fails else 0
        census = census_of(world)
    assert census.markers_unread == refused, (census.errors, refused)
    assert census.unverifiable == bool(census.errors)
    assert {procs.marker_unread(pid) for pid in refused} <= set(census.errors)
    assert census.only_markers_unread == (bool(refused) and set(census.errors) ==
                                          {procs.marker_unread(pid) for pid in refused})
    assert not (refused - {GUARDIAN}) & census.marker_pids    # an unread marker is never read as found


# 2. Start grace's extension.

@pytest.mark.parametrize("read", ["refused", "cut-short"])
def test_a_strangers_refused_marker_read_does_not_end_a_slow_start(daemon, world, monkeypatch, read):
    """The 2026-10-27 shape: the guardian alone, alive and running, past start grace, while a
    stranger refuses its marker read on every census. A stranger whose buffer fills the
    read (`cut-short`) is a failed marker read too, and is tolerated alike."""
    guardian = world.add(GUARDIAN, 1, GUARDIAN, start=STARTED, marked=True, ignores_term=True, term_handler=True)
    world.add(7000, 1, 7000)
    if read == "refused":
        world.fail_reads[7000] = ALWAYS
    else:
        world.cut_reads.add(7000)
    starting(daemon, expired_s=1)
    for _ in range(4):                                  # four censuses at the census's pacing
        daemon._inspect_next.pop(ATTEMPT, None)
        daemon.__dict__.setdefault("_cwd_wait_next", {}).pop(ATTEMPT, None)
        daemon._process_attempt(ATTEMPT)
        assert attempt(daemon)["state"] == "starting" and not world.signals and guardian.alive
    assert not (adir(daemon) / "abandon.json").exists()
    assert extension(daemon)[0] == {"guardian_pid": GUARDIAN, "start_grace_s": START_GRACE_S,
                                    "start_grace_max_s": MAX_S, "markers_unread": [7000]}
    publish(daemon, "start.json", START)                # its start.json lands within the bound
    daemon._inspect_next.pop(ATTEMPT, None)
    daemon._process_attempt(ATTEMPT)
    assert attempt(daemon)["state"] == "running" and not world.signals


@pytest.mark.parametrize("why", ["guardian-unread", "another-error", "unread-member", "past-bound"])
def test_no_extension_past_any_other_unread_census(daemon, world, monkeypatch, why):
    """The narrowing is exactly D4's: everything else still ends at the grace, unchanged,
    through the group KILL and decision 1's end, with the abandon written first."""
    guardian = world.add(GUARDIAN, 1, GUARDIAN, start=STARTED, marked=True, ignores_term=True, term_handler=True)
    world.add(7000, 1, 7000)
    world.fail_reads[7000] = ALWAYS
    if why == "guardian-unread":
        world.fail_reads[GUARDIAN] = ALWAYS
    if why == "unread-member":
        world.add(4250, 1, GUARDIAN)                    # in the guardian's group, not its child, its read refused
        world.fail_reads[4250] = ALWAYS
    contain, first = daemon._contain, []

    def transient(a):
        """Start grace's census meets the refusals; by the next census the newborns have
        exec'd and read whole, as measured, so the kill protocol can verify its end."""
        census = contain(a)
        if not first:
            first.append(census)
            world.fail_reads.clear()
            if why == "another-error":
                census = dataclasses.replace(census, errors=census.errors + (
                    "responsibility read unavailable for pid 7001",))
        return census
    daemon._contain = transient
    starting(daemon, expired_s=MAX_S - START_GRACE_S + 1 if why == "past-bound" else 1)
    seen = abandon_seen_at_each_signal(daemon, world, monkeypatch)
    a = settle(daemon)
    assert "attempt.start_extended" not in kinds(daemon)
    assert seen and all(seen), seen                      # the abandon precedes every signal
    assert not guardian.alive and guardian.ended_by == KILL
    assert world.signals[0] == ("group", GUARDIAN, KILL), world.signals
    assert 7000 not in world.signalled()                 # the unread stranger is never signalled
    assert (a["state"], a["outcome_detail"]) == ("failed", "starting-no-receipt"), a


ERRORS = {
    "stranger-a": procs.marker_unread(7000), "stranger-b": procs.marker_unread(7001),
    "guardian": procs.marker_unread(GUARDIAN), "responsibility": "responsibility read unavailable for pid 7000",
    "table": "group enumeration unavailable", "identity": f"identity inspection unavailable for pid {GUARDIAN}",
    "process": "process inspection unavailable for pid 7002", "enumeration": "marker enumeration unavailable",
}
PIDS = {"stranger-a": 7000, "stranger-b": 7001, "guardian": GUARDIAN}


@settings(max_examples=200, deadline=None, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(stopped=st.booleans(), member=st.booleans(), elapsed=st.floats(0, 2 * MAX_S, allow_nan=False),
       errors=st.sets(st.sampled_from(sorted(ERRORS))), named=st.booleans(), bare=st.booleans())
@example(stopped=False, member=False, elapsed=1.0, errors={"stranger-a"}, named=True, bare=False)
@example(stopped=False, member=False, elapsed=1.0, errors={"stranger-a", "guardian"}, named=True, bare=False)
@example(stopped=False, member=False, elapsed=1.0, errors={"stranger-a", "table"}, named=True, bare=False)
@example(stopped=False, member=False, elapsed=1.0, errors={"stranger-a", "responsibility"}, named=True, bare=False)
@example(stopped=False, member=False, elapsed=1.0, errors={"stranger-a", "identity"}, named=True, bare=False)
@example(stopped=False, member=False, elapsed=1.0, errors={"stranger-a", "process"}, named=True, bare=False)
@example(stopped=False, member=False, elapsed=1.0, errors={"stranger-a", "enumeration"}, named=True, bare=False)
# Review r1 of #276: D4's shape just inside the bound, at it and just past it (as P3-3 pinned the
# extension property of `test_kill4_fix2b.py`).
@example(stopped=False, member=False, elapsed=MAX_S - 1e-6, errors={"stranger-a"}, named=True, bare=False)
@example(stopped=False, member=False, elapsed=MAX_S, errors={"stranger-a"}, named=True, bare=False)
@example(stopped=False, member=False, elapsed=MAX_S + 1e-6, errors={"stranger-a"}, named=True, bare=False)
def test_extension_is_granted_exactly_while_every_condition_holds_whatever_the_census_could_not_read(
        daemon, stopped, member, elapsed, errors, named, bare):
    """Invariant 3, with D4: no extension passes `START_GRACE_MAX_S`; a stopped guardian,
    another member, or an unverifiable census never extends, unless its only errors are
    failed marker reads of other processes, named as such (`named`). `bare` is an
    unverifiable census with no error at all, which no census makes. Every error kind
    that is not a failed marker read is pinned beside a stranger's (#274 review r1's P3-1).
    `elapsed` runs on a fake clock, so an example lands on the bound exactly, and the answer
    is asserted for every draw (review r1 of #276: a ±0.05 s band went unasserted)."""
    world = World(ATTEMPT, str(daemon.root))
    with pytest.MonkeyPatch.context() as patch:
        world.install(patch)
        world.add(GUARDIAN, 1, GUARDIAN, start=STARTED, marked=True, stopped=stopped)
        daemon.store.update_attempt(ATTEMPT, state="starting", child_pid=None)
        daemon.start_grace_s = START_GRACE_S
        launch = 1000.0                          # the deadline's start
        now = launch + elapsed
        patch.setattr(daemon_module.time, "monotonic", lambda: now)
        census = procs.Containment(group_pids=frozenset({GUARDIAN} | ({4250} if member else set())),
                                   identities={GUARDIAN: world.identity(GUARDIAN)},
                                   shapes={GUARDIAN: {"ppid": 1, "pgid": GUARDIAN, "stat": "T" if stopped else "S"}})
        unread = frozenset(PIDS[name] for name in errors if name in PIDS) if named else frozenset()
        if errors or bare:
            census = dataclasses.replace(census, unverifiable=True, errors=tuple(ERRORS[name] for name in sorted(errors)),
                                         markers_unread=unread)
        extends = daemon._start_extends(attempt(daemon), census, launch + START_GRACE_S)
    only_strangers = bool(errors) and errors <= {"stranger-a", "stranger-b"} and named
    # The clock's own reading decides, so a sum that rounds onto the bound is at it.
    expected = (not stopped and not member and now < launch + MAX_S
                and (not (errors or bare) or only_strangers))
    assert extends == expected, (stopped, member, elapsed, errors, named, bare)


# 2b. The guardian's own identity readers.

READER = 4243


@pytest.mark.parametrize("unread", [False, True])
def test_the_guardians_identity_reader_does_not_end_a_slow_start(daemon, world, monkeypatch, unread):
    """Before `start.json` the guardian runs `sysctl` and `ps` (`procs._read`), each a child
    in its group for a few milliseconds; a census that lands on one sees two members. Taken
    in that child's first milliseconds (`unread`), its marker read can fail as well."""
    guardian = world.add(GUARDIAN, 1, GUARDIAN, start=STARTED, marked=True, ignores_term=True, term_handler=True)
    reader = world.add(READER, GUARDIAN, GUARDIAN)
    if unread:
        world.fail_reads[READER] = ALWAYS
    starting(daemon, expired_s=1)
    for _ in range(3):
        daemon._inspect_next.pop(ATTEMPT, None)
        daemon.__dict__.setdefault("_cwd_wait_next", {}).pop(ATTEMPT, None)
        daemon._process_attempt(ATTEMPT)
        assert attempt(daemon)["state"] == "starting" and not world.signals and guardian.alive
    assert extension(daemon)[0] == {"guardian_pid": GUARDIAN, "start_grace_s": START_GRACE_S,
                                    "start_grace_max_s": MAX_S, "identity_readers": [READER],
                                    **({"markers_unread": [READER]} if unread else {})}
    reader.alive = False                                # the reader exits; start.json lands
    publish(daemon, "start.json", START)
    daemon._inspect_next.pop(ATTEMPT, None)
    daemon._process_attempt(ATTEMPT)
    assert attempt(daemon)["state"] == "running" and not world.signals


@pytest.mark.parametrize("member", ["marked-child", "stopped-child", "grandchild", "own-group", "marked-grandchild"])
def test_no_extension_beside_any_member_but_an_identity_reader(daemon, world, monkeypatch, member):
    """Only an unmarked, running direct child in the guardian's group is admitted beside it:
    any other member still ends start grace through the group KILL, with the abandon first."""
    guardian = world.add(GUARDIAN, 1, GUARDIAN, start=STARTED, marked=True, ignores_term=True, term_handler=True)
    if member in ("grandchild", "marked-grandchild"):
        world.add(READER, GUARDIAN, GUARDIAN)
        world.add(4244, READER, GUARDIAN, marked=member == "marked-grandchild")
    else:
        world.add(READER, GUARDIAN, READER if member == "own-group" else GUARDIAN,
                  marked=member == "marked-child", stopped=member == "stopped-child")
    starting(daemon, expired_s=1)
    seen = abandon_seen_at_each_signal(daemon, world, monkeypatch)
    a = settle(daemon)
    assert "attempt.start_extended" not in kinds(daemon)
    assert seen and all(seen), seen
    assert not guardian.alive and guardian.ended_by == KILL
    assert world.signals[0] == ("group", GUARDIAN, KILL), world.signals
    assert (a["state"], a["outcome_detail"]) == ("failed", "starting-no-receipt"), a


@settings(max_examples=300, deadline=None, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(members=st.lists(st.fixed_dictionaries({
    "child": st.booleans(), "own_group": st.booleans(), "stat": st.sampled_from(["S", "R", "U", "T", "Z", ""]),
    "marked": st.booleans(), "grouped": st.booleans(), "shaped": st.booleans()}), max_size=4),
    held=st.booleans())
@example(members=[{"child": True, "own_group": False, "stat": "S", "marked": False, "grouped": True, "shaped": True}],
         held=False)
@example(members=[{"child": True, "own_group": False, "stat": "", "marked": False, "grouped": True, "shaped": True}],
         held=False)
def test_the_identity_readers_are_exactly_the_unmarked_running_children_in_the_guardians_group(
        daemon, members, held):
    """For every census: `_identity_readers` is the set of live members other than the
    guardian whose shape has the guardian as parent and its group as group, whose state is
    known and neither stopped nor zombie, that are in the group's members and not marked; and the
    extension, every other condition holding, is granted iff every other member is one."""
    world = World(ATTEMPT, str(daemon.root))
    with pytest.MonkeyPatch.context() as patch:
        world.install(patch)
        world.add(GUARDIAN, 1, GUARDIAN, start=STARTED, marked=True)
        daemon.store.update_attempt(ATTEMPT, state="starting", child_pid=None)
        daemon.start_grace_s = START_GRACE_S
        pids = [4300 + n for n in range(len(members))]
        shapes = {GUARDIAN: {"ppid": 1, "pgid": GUARDIAN, "stat": "S"}}
        shapes.update({pid: {"ppid": GUARDIAN if m["child"] else 4299, "pgid": pid if m["own_group"] else GUARDIAN,
                             "stat": m["stat"]} for pid, m in zip(pids, members) if m["shaped"]})
        census = procs.Containment(
            group_pids=frozenset({GUARDIAN} | {pid for pid, m in zip(pids, members) if m["grouped"]}),
            descendant_pids=frozenset(pid for pid, m in zip(pids, members) if not m["grouped"]),
            marker_pids=frozenset({GUARDIAN} | {pid for pid, m in zip(pids, members) if m["marked"]}),
            identities={GUARDIAN: world.identity(GUARDIAN)}, shapes=shapes,
            held_pids=frozenset({4299}) if held else frozenset())
        a = attempt(daemon)
        readers = Daemon._identity_readers(a, census)
        extends = daemon._start_extends(a, census, time.monotonic() - 1 + START_GRACE_S)
    expected = {pid for pid, m in zip(pids, members)
                if m["child"] and not m["own_group"] and m["grouped"] and m["shaped"] and not m["marked"]
                and m["stat"][:1] not in ("", "T", "Z")}
    assert readers == expected, (members, readers)
    assert extends == (not held and set(pids) == expected), (members, held)


# 3. The premise: before start.json, nothing the guardian starts carries the attempt's markers.

MARKERS = ("SUBFLEET_ATTEMPT", "SUBFLEET_ROOT")


@pytest.mark.parametrize("relay", [False, True])
def test_before_its_start_json_the_guardian_starts_nothing_that_carries_the_attempts_markers(
        tmp_path, monkeypatch, root, relay):
    """The guardian runs in this process with its real identity readers (`boot_id`, which
    `sysctl` reads, and `proc_start`, which `ps` reads); only `setsid` and its signal
    handlers are left out, as in `test_guardian.py`. Every process it starts is recorded
    with whether `start.json` was published then and whether its environment holds a
    marker (names only, never a value). With `relay`, the guardian binds its control
    socket and builds a turn job's daemon peer check before `start.json`, as a turn job's
    does (#274 review r1's P3-6); the socket lives under the short `/tmp` root."""
    monkeypatch.setattr(guardian_module.os, "setsid", lambda: None)
    monkeypatch.setattr(guardian_module.signal, "signal", lambda *args: None)
    monkeypatch.setenv("SUBFLEET_ATTEMPT", ATTEMPT)
    monkeypatch.setenv("SUBFLEET_ROOT", str(tmp_path / "state"))
    procs.forget_boot_id()                              # a new guardian has read nothing yet
    spawned: list[tuple[bool, bool, str]] = []
    inside: list[int] = []
    real = subprocess.Popen._execute_child

    def record(self, args, executable, preexec_fn, close_fds, pass_fds, cwd, env, *rest):
        environment = os.environ if env is None else env
        name = os.path.basename(str(args[0] if isinstance(args, (list, tuple)) else args))
        spawned.append(((tmp_path / "start.json").exists(), any(key in environment for key in MARKERS), name))
        inside.append(1)
        try:
            return real(self, args, executable, preexec_fn, close_fds, pass_fds, cwd, env, *rest)
        finally:
            inside.pop()
    monkeypatch.setattr(subprocess.Popen, "_execute_child", record)
    for name in ("fork", "forkpty", "system", "posix_spawn", "posix_spawnp"):
        # Every process must start through subprocess, where it is recorded: subprocess's own
        # posix_spawn passes, and any other start fails the test rather than go unseen.
        if hasattr(os, name):
            def guarded(*args, _name=name, _real=getattr(os, name), **kwargs):
                if not inside:
                    pytest.fail(f"os.{_name} outside subprocess")
                return _real(*args, **kwargs)
            monkeypatch.setattr(os, name, guarded)
    previous = os.umask(0o077)
    try:
        rc = guardian_module.run_guardian([sys.executable, "-c", "pass"], attempt_dir=tmp_path, cwd=str(tmp_path),
                                          stdin_path=None, stdout_path=str(tmp_path / "stdout"),
                                          stderr_path=str(tmp_path / "stderr"), env_pass=MARKERS,
                                          **({"control_socket": str(root / "run" / "c.sock"),
                                              "relay_peer_lock": str(root / "daemon.lock")} if relay else {}))
    finally:
        os.umask(previous)
        procs.forget_boot_id()
    assert rc == 0
    before = [(name, marked) for published, marked, name in spawned if not published]
    after = [(name, marked) for published, marked, name in spawned if published]
    # The identity readers ran, were seen, and are all it starts before start.json: the only
    # children `_identity_readers` admits beside it (2b).
    assert {name for name, _ in before} == {"sysctl", "ps"}, spawned
    assert not any(marked for _, marked in before), before
    assert after and all(marked for _, marked in after), after          # the provider does carry them
    if relay:
        assert json.loads((tmp_path / "start.json").read_text())["control_socket"] == str(root / "run" / "c.sock")


def test_the_identity_readers_environment_is_four_names_and_never_a_marker(monkeypatch):
    seen = []
    monkeypatch.setenv("SUBFLEET_ATTEMPT", ATTEMPT)
    monkeypatch.setenv("SUBFLEET_ROOT", "/state")
    monkeypatch.setattr(procs.subprocess, "run", lambda argv, **kwargs: (
        seen.append(sorted(kwargs["env"])), subprocess.CompletedProcess(argv, 0, "x\n", ""))[1])
    procs._read(["/bin/ps", "-p", "1", "-o", "lstart="])
    assert seen == [["LANG", "LC_ALL", "PATH", "TZ"]]


# 4. Mutation controls: each must fail its named test (strict xfail).

MUTANTS = {
    # Any unverifiable census extends.
    "any-unverifiable": ("extension", "another-error",
                         [(Daemon, "_start_extends", [("(census.unverifiable and (unread is None or guardian in unread))",
                                                       "False")])]),
    # The guardian's own failed read is tolerated as a stranger's is.
    "guardian-unread": ("extension", "guardian-unread",
                        [(Daemon, "_start_extends", [("(unread is None or guardian in unread)", "unread is None")])]),
    # The census does not record what it could not read: D4 is lost.
    "unread-unrecorded": ("stranger", "refused",
                          [(procs, "containment", [("unread = {pid for pid in candidates if marker_unread(pid) in refused}",
                                                    "unread = set()")])]),
    # The guardian's identity readers are not admitted: H1 is lost.
    "no-readers": ("reader", False,
                   [(Daemon, "_start_extends", [("census.live_pids - readers != {guardian}",
                                                 "census.live_pids != {guardian}")])]),
    # Any child in the group is admitted, a marked one too.
    "readers-marked": ("beside", "marked-child",
                       [(Daemon, "_identity_readers", [("and pid not in census.marker_pids)", "and True)")])]),
    # A stopped child is admitted.
    "readers-stopped": ("beside", "stopped-child",
                        [(Daemon, "_identity_readers", [('.startswith(("T", "Z"))', '.startswith("Z")')])]),
    # Any member of the group is admitted, a grandchild too.
    "readers-grandchild": ("beside", "grandchild",
                           [(Daemon, "_identity_readers", [("shape.get(\"ppid\") == guardian and ", "")])]),
    # Any child is admitted, in a group of its own too.
    "readers-own-group": ("beside", "own-group",
                          [(Daemon, "_identity_readers", [("shape.get(\"pgid\") == group and pid in census.group_pids",
                                                           "True")])]),
    # A child with no state is admitted (#274 review r1's P3-5).
    "readers-stateless": ("readers", None,
                          [(Daemon, "_identity_readers", [('(stat := str(shape.get("stat") or "")) and not stat',
                                                           'not (stat := str(shape.get("stat") or ""))')])]),
    # A read cut short is an error under another name, so it is not in `markers_unread`
    # (#275 review r1's OM-A).
    "cut-unnamed": ("stranger", "cut-short",
                    [(procs, "_kernel_sources", [(
                        "# a buffer that may be cut short\n                errors.append(marker_unread(pid))",
                        "# a buffer that may be cut short\n                errors.append("
                        "f\"argument read unavailable for pid {pid}\")")])]),
}


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the named regression must detect this mutant")
@pytest.mark.parametrize("mutant", sorted(MUTANTS))
def test_d4_mutant_fails_its_named_regression(mutant, daemon, world, monkeypatch):
    regression, parameter, replacements = MUTANTS[mutant]
    for owner, name, changes in replacements:
        replace_function(monkeypatch, owner, name, changes)
    if regression == "extension":
        test_no_extension_past_any_other_unread_census(daemon, world, monkeypatch, parameter)
    elif regression == "reader":
        test_the_guardians_identity_reader_does_not_end_a_slow_start(daemon, world, monkeypatch, parameter)
    elif regression == "beside":
        test_no_extension_beside_any_member_but_an_identity_reader(daemon, world, monkeypatch, parameter)
    elif regression == "readers":
        test_the_identity_readers_are_exactly_the_unmarked_running_children_in_the_guardians_group(daemon)
    else:
        test_a_strangers_refused_marker_read_does_not_end_a_slow_start(daemon, world, monkeypatch, parameter)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the property must detect this mutant")
def test_the_any_marker_error_mutant_fails_the_extension_property(daemon, monkeypatch):
    """`only_markers_unread` true whenever any failed marker read is named, other errors aside."""
    monkeypatch.setattr(procs.Containment, "only_markers_unread", property(lambda self: bool(self.markers_unread)))
    test_extension_is_granted_exactly_while_every_condition_holds_whatever_the_census_could_not_read(daemon)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the property must detect this mutant")
@pytest.mark.parametrize("kind", ["responsibility", "table", "identity", "process", "enumeration"])
def test_a_mutant_tolerating_one_more_error_kind_fails_the_extension_property(daemon, monkeypatch, kind):
    """`only_markers_unread` true beside one error that is not a failed marker read. With only
    `table` pinned, review r1's mutant tolerating `enumeration` passed the property."""
    tolerated = ERRORS[kind]
    monkeypatch.setattr(procs.Containment, "only_markers_unread", property(lambda self: bool(self.markers_unread) and (
        set(self.errors) - {tolerated}) == {procs.marker_unread(pid) for pid in self.markers_unread}))
    test_extension_is_granted_exactly_while_every_condition_holds_whatever_the_census_could_not_read(daemon)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="the relay case must detect this mutant")
def test_a_relay_that_starts_a_process_before_start_json_fails_the_premise(tmp_path, monkeypatch, root):
    """The relay's `bind`, which runs before `start.json`, starts `/usr/bin/true` with the
    guardian's marked environment (#275 review r1's OM-B). Only the `relay` case binds."""
    from subfleet.relay import RelayServer
    replace_function(monkeypatch, RelayServer, "bind", [(
        "directory = self.path.parent", "__import__('subprocess').run(['/usr/bin/true']); directory = self.path.parent")])
    test_before_its_start_json_the_guardian_starts_nothing_that_carries_the_attempts_markers(
        tmp_path, monkeypatch, root, True)
