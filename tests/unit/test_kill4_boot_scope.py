"""D-KILL4 with D-QUAR-REUSE1 (#250 fix round 2a; review r2 of #250, check 4): one rule.

#250 (D-KILL4) makes the census the kill protocol's signal authority (C-5.4, C-5.6) and
the recorded group a signal source only with its leader verified. #259 (D-QUAR-REUSE1)
scopes every recorded seed to the attempt's boot, the boot its guardian identity
records: once that boot is over, no recorded pid and not the recorded group counts a
process of this one. The merge with #240 at 8fe3413ef brought #259 here. The combined
census takes #259's "the attempt's boot is over" branch first and #250's leader check in
its else branch, and #259's boot filter also covers every key #250 stores:
`chain_identities`, `held_parents`, `held_marked` and the taint in
`unverified_identities`. A held parent is held again only when its boot matches as well
as its start.

Each case runs the daemon's census, kill protocol, finalization and kill request
unchanged on the simulated host (`tests/kill4_world.py`), on a current boot the test
sets (`BootWorld`). C-5.3's identity checks, `same_process` and `liveness`, are the real
ones over that world, so a signal reaches a process only when its captured identity,
boot included, is the live one's. Nothing on the host is inspected or signalled.

Invariants, executed by the property at the end:
1. No stranger is signalled, on any boot: nothing stored on another boot than the
   attempt's, or recorded on another boot than the current one, seeds, holds or targets.
2. #259 cannot add a signal: when the attempt's boot is over, the group, walk and
   responsibility sources are empty.
3. #250 cannot weaken #259: no key #250 stores receives an identity of another boot than
   the attempt's.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import signal
import uuid

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import procs, protocol
from subfleet.contracts import attempt_dir
from subfleet.daemon import Daemon
from tests.kill4_world import World
from tests.unit.test_daemon_settle import ATTEMPT, JOB, STARTED, add_running, attempt, daemon  # noqa: F401
from tests.unit.test_daemon_settle import publish_receipt, with_launch
from tests.unit.test_kill4_terminal_containment import owned_worktree

TERM, KILL = int(signal.SIGTERM), int(signal.SIGKILL)
BOOT_A = "11111111-1111-4111-8111-111111111111"     # the attempt's boot
BOOT_B = "22222222-2222-4222-8222-222222222222"     # a later boot
BOOT_C = "33333333-3333-4333-8333-333333333333"     # another
SECONDS = "100"           # `kern.boottime` seconds the world's sysctl answers (a legacy boot)
REASON = "different boot identity (attempt's boot)"
GROUP_ERROR = "boot identity unavailable for group 4242"
#: C-5.3's real checks, saved before any fixture replaces them.
SAME_PROCESS, LIVENESS = procs.same_process, procs.liveness
#: The keys an attempt stores identities under, #259's and #250's.
SEED_KEYS = ("owned_identities", "responsible_identities", "counted_identities")
D_KILL4_KEYS = ("chain_identities", "held_parents", "held_marked", "unverified_identities")


@dataclass
class BootWorld(World):
    """`World` on a boot the test sets. `boot` is the current boot's identity: a
    boot-session UUID, `kern.boottime` seconds after a failed UUID read, or None when it
    cannot be read. `tried` records every signal asked for, delivered or refused."""
    boot: str | None = BOOT_A
    tried: list = field(default_factory=list)

    def snapshot(self) -> procs.ProcessTable:
        self._respawn()
        rows = {pid: (proc.ppid, proc.pgid, "T" if proc.stopped else "S", proc.start)
                for pid, proc in self.live().items()}
        return procs.ProcessTable(rows)          # its boot is read through `procs.boot_id`

    def read_boot(self) -> str:
        if self.boot is None:
            raise procs.InspectionError("macOS boot identity is unavailable")
        return self.boot

    def identity(self, pid: int) -> procs.ProcessIdentity:
        return procs.ProcessIdentity(pid, self.boot, self.procs[pid].start)

    def current(self, pid: int) -> procs.ProcessIdentity | None:
        proc = self.procs.get(pid)
        return procs.ProcessIdentity(pid, self.read_boot(), proc.start) if proc and proc.alive else None

    def sysctl(self, argv, empty_ok=False, **kwargs) -> str:
        if argv[-1] == "kern.boottime":
            return "{ sec = %s, usec = 0 } Sat Sep  5 09:00:00 2026" % SECONDS
        raise procs.InspectionError("the simulated host runs no " + argv[0])

    def signal_process(self, recorded, sig, *, before_signal=None) -> bool:
        self.tried.append(("pid", recorded.pid, int(sig)))
        if not procs.same_process(recorded.pid, recorded.boot_id, recorded.proc_start):
            return False                                   # C-5.3, as `procs.signal_process`
        return super().signal_process(recorded, sig, before_signal=before_signal)

    def signal_group(self, pgid, sig, *, boot_id, proc_start, before_signal=None) -> bool:
        self.tried.append(("group", pgid, int(sig)))
        if not procs.same_process(pgid, boot_id, proc_start):
            return False                                   # C-5.4, as `procs.signal_group`
        return super().signal_group(pgid, sig, boot_id=boot_id, proc_start=proc_start,
                                    before_signal=before_signal)

    def asked(self) -> set[int]:
        """Every pid a signal was asked for, one by one or as a group's number."""
        return {pid for _, pid, _ in self.tried}

    def install(self, monkeypatch) -> "BootWorld":
        super().install(monkeypatch)
        monkeypatch.setattr(procs, "boot_id", self.read_boot)
        monkeypatch.setattr(procs, "identity", self.current)
        monkeypatch.setattr(procs, "_read", self.sysctl)
        monkeypatch.setattr(procs, "same_process", SAME_PROCESS)
        monkeypatch.setattr(procs, "liveness", LIVENESS)
        return self


@pytest.fixture
def world(daemon, monkeypatch):
    w = BootWorld(ATTEMPT, str(daemon.root)).install(monkeypatch)
    daemon.exit_settle_s = 0
    daemon._salvage = lambda job, a, **kwargs: ([], None, {})    # a quarantine's release salvages nothing
    with_launch(daemon, monkeypatch)
    daemon.store.update_attempt(ATTEMPT, boot_id=BOOT_A)     # the attempt ran on boot A
    procs.forget_boot_id()
    return w


def inline(daemon, monkeypatch) -> None:
    """Run the kill request's scheduled work at once, as its worker would."""
    monkeypatch.setattr(daemon, "_schedule", lambda key, fn, *args, **kw: fn(*args))


def finished(daemon, state: str = "succeeded") -> None:
    daemon.store.update_attempt(ATTEMPT, state=state)
    daemon.store.update_job(JOB, state=state)


def evidence(daemon) -> dict:
    return json.loads(attempt(daemon)["evidence_json"] or "{}")


def store(daemon, **keys) -> None:
    daemon.store.update_attempt(ATTEMPT, evidence_json=json.dumps({**evidence(daemon), **keys}))


def kept(ident: procs.ProcessIdentity) -> dict:
    return asdict(ident)


def stranger_group(world, member: int = 5600):
    """r1's P1 world: a process took pid 4242 (the recorded guardian and pgid), called
    setsid, forked a sleeping member and exited, leaving a leaderless group 4242."""
    return world.add(member, 1, 4242)


def quarantined(daemon, census: procs.Containment) -> None:
    daemon._quarantine(attempt(daemon), census, "termination could not verify containment")
    assert attempt(daemon)["state"] == "quarantined"


# 1. A leaderless group at the recorded pgid, the attempt's own boot: held, never signalled.

@pytest.mark.parametrize("path", ["census", "kill", "dead-guardian"])
def test_case_1_a_leaderless_group_on_the_attempts_boot_holds_and_is_never_signalled(daemon, world, monkeypatch,
                                                                                    path):
    member = stranger_group(world)
    if path == "census":
        census = daemon._contain(attempt(daemon))
        assert member.pid in census.group_pids and not census.leader_verified, census.to_dict()
        assert census.to_dict()["group_leader"] == "not verified"
        assert not census.containment_empty and 4242 not in census.recycled
        assert daemon._signal_targets(attempt(daemon), census) == {}
    elif path == "kill":
        finished(daemon)
        inline(daemon, monkeypatch)
        # Fix round 2b, decision 2 (review r2's P3-2): nothing in the census may be signalled,
        # so no protocol runs; the answer names the member as held, never signalled.
        answer = daemon.kill(protocol.KillArgs(job_id=JOB))
        assert answer["status"] == "already finished" and "held, never signalled" in answer["detail"], answer
        assert "recontainment" not in evidence(daemon)
    else:
        daemon.store.update_job(JOB, max_attempts=1)
        daemon._process_attempt(ATTEMPT)                         # running, its guardian dead
        assert attempt(daemon)["state"] == "quarantined"
    assert member.alive and member.pid not in world.signalled() and member.pid not in world.asked()
    assert str(member.pid) not in evidence(daemon).get("counted_identities", {})


# 2. The same group with the attempt's boot over: no source, recycled once, released.

def test_case_2_with_the_attempts_boot_over_the_group_is_no_source_and_confirm_dead_releases(daemon, world,
                                                                                            monkeypatch):
    member = stranger_group(world)
    world.boot = BOOT_B
    census = daemon._contain(attempt(daemon))
    assert census.verified_empty and not census.group_pids and not census.leader_verified, census.to_dict()
    assert census.recycled[4242]["reason"] == REASON
    assert census.recycled[4242]["now"] == {"pid": 4242, "boot_id": BOOT_B, "proc_start": None}
    assert "group of another boot" not in json.dumps(census.to_dict())
    daemon._contain(attempt(daemon))
    recycled = [row for row in daemon.store.list_events(JOB) if row["kind"] == "attempt.processes_recycled"]
    assert len(recycled) == 1, recycled                                  # recorded once, for one reason
    assert evidence(daemon)["recycled"]["4242"]["reason"] == REASON
    # As a quarantine left before the merge, `--confirm-dead` releases it, and nothing is signalled.
    quarantined(daemon, procs.Containment(group_pids=frozenset({member.pid}), unverifiable=True,
                                          errors=(GROUP_ERROR,)))
    inline(daemon, monkeypatch)
    assert daemon.kill(protocol.KillArgs(job_id=JOB, confirm_dead=True))["status"] == "resolution requested"
    assert attempt(daemon)["state"] == "lost"
    confirmed = [json.loads(row["data_json"]) for row in daemon.store.list_events(JOB)
                 if row["kind"] == "quarantine.confirmed_dead"]
    assert confirmed and confirmed[-1]["containment"]["live_pids"] == []
    assert member.alive and not world.tried


# 3. A legacy or unreadable boot: the group's members hold the attempt, never signalled.

@pytest.mark.parametrize(("attempt_boot", "now"), [("99", BOOT_A), (BOOT_A, None), (BOOT_A, SECONDS)],
                         ids=["legacy-attempt-boot", "unreadable-current-boot", "legacy-current-boot"])
def test_case_3_a_boot_that_cannot_be_compared_holds_the_groups_members_never_signalled(daemon, world, monkeypatch,
                                                                                        attempt_boot, now):
    member = stranger_group(world)
    daemon.store.update_attempt(ATTEMPT, boot_id=attempt_boot)
    world.boot = now
    census = daemon._contain(attempt(daemon))
    assert census.unverifiable and GROUP_ERROR in census.errors, census.to_dict()
    assert member.pid in census.group_pids and not census.leader_verified
    assert 4242 not in census.recycled
    assert daemon._signal_targets(attempt(daemon), census) == {}
    finished(daemon)
    inline(daemon, monkeypatch)
    # Review r3 of #250, P3-1: no read, verifiable or not, attributes a member only a
    # leaderless group names, so no protocol runs although the census is unverifiable.
    answer = daemon.kill(protocol.KillArgs(job_id=JOB))
    assert answer["status"] == "already finished" and "held, never signalled" in answer["detail"], answer
    assert "recontainment" not in evidence(daemon) and not world.tried, world.tried
    assert member.alive and member.pid not in world.signalled()
    assert ("pid", member.pid, TERM) not in world.tried and ("pid", member.pid, KILL) not in world.tried


def kill_force_release_kill_kill(daemon, monkeypatch) -> tuple[list[dict], list[str]]:
    """Review r2's sequence on a finished job, each `kill`'s scheduled work run at once;
    the answers, and the keys of the work scheduled."""
    daemon.term_grace_s, daemon.kill_settle_s = 2.0, 1.0
    scheduled: list[str] = []
    monkeypatch.setattr(daemon, "_schedule", lambda key, fn, *args, **kw: (scheduled.append(key), fn(*args)))
    answers = [daemon.kill(protocol.KillArgs(job_id=JOB)),
               daemon.kill(protocol.KillArgs(job_id=JOB, force_release=True)),
               daemon.kill(protocol.KillArgs(job_id=JOB)),
               daemon.kill(protocol.KillArgs(job_id=JOB))]
    return answers, scheduled


def kinds(daemon) -> set[str]:
    return {row["kind"] for row in daemon.store.list_events(JOB)}


@pytest.mark.parametrize(("attempt_boot", "now"), [("99", BOOT_A), (BOOT_A, None)],
                         ids=["legacy-attempt-boot", "unreadable-current-boot"])
def test_case_3_kill_force_release_kill_kill_on_an_unverifiable_census_runs_no_protocol(
        daemon, world, monkeypatch, attempt_boot, now):
    """Review r3 of #250, P3-1, its executed input: case 3's world through review r2's
    sequence. An absent leader never verifies, so every census of this attempt is
    unverifiable, and its only member is one a leaderless group names, which no read
    attributes. Each `kill` answered `containment requested` and ran a full protocol
    (a TERM grace and the settle window) that could signal nothing, appending a
    `terminal_containment` round and a `recontainment` result; "the next `kill` reports
    the last result" never happened."""
    member = stranger_group(world)
    daemon.store.update_attempt(ATTEMPT, boot_id=attempt_boot)
    world.boot = now
    finished(daemon)
    answers, scheduled = kill_force_release_kill_kill(daemon, monkeypatch)
    assert [answer["status"] for answer in answers] == ["already finished"] * 4, answers
    for answer in (answers[0], answers[2], answers[3]):
        assert f"pid {member.pid}" in answer["detail"] and "held, never signalled" in answer["detail"], answer
    assert not scheduled, scheduled
    after = evidence(daemon)
    assert "terminal_containment" not in after and "recontainment" not in after, after
    assert "attempt.recontainment_started" not in kinds(daemon)
    assert member.alive and not world.tried, world.tried
    assert attempt(daemon)["state"] == "succeeded"


def walk_seed(daemon, world, *, tainted: bool) -> tuple:
    """A counted seed (5800) with a child (5801): walk members. `tainted`: an unverifiable
    census first recorded the seed (`unverified_identities`), so no census, verifiable or
    not, attributes it or anything below it (C-5.4, `_tainted`)."""
    seed = world.add(5800, 1, 5800)
    child = world.add(5801, 5800, 5800)
    record = kept(procs.ProcessIdentity(5800, BOOT_A, seed.start))
    store(daemon, counted_identities={"5800": record}, **({"unverified_identities": {"5800": record}}
                                                          if tainted else {}))
    return seed, child


@pytest.mark.parametrize("members", ["listing-failed", "walk-member"])
def test_an_unverifiable_census_a_later_read_could_attribute_still_runs_the_protocol(daemon, world, monkeypatch,
                                                                                    members):
    """The control for review r3's P3-1 fix: an unverifiable census still runs the protocol
    when a verifiable read could attribute a member. `listing-failed`: the process table
    could not be read, so the census has no members at all and could have missed any.
    `walk-member`: a seed's marker read failed once, so the census that found the seed and
    its child is unverifiable; the protocol's own censuses read it and TERM the seed."""
    stranger_group(world)
    if members == "listing-failed":
        def unavailable():
            raise procs.InspectionError("process table unavailable")
        monkeypatch.setattr(procs, "snapshot", unavailable)
    else:
        seed, child = walk_seed(daemon, world, tainted=False)
        world.fail_reads = {5801: world.tables + 1}          # the kill's own census only
    finished(daemon)
    daemon.term_grace_s, daemon.kill_settle_s = 0.2, 0.1
    inline(daemon, monkeypatch)
    census = daemon._contain(attempt(daemon)) if members == "listing-failed" else None
    if census is not None:
        assert census.unverifiable and not census.live_pids and not census.held_pids, census.to_dict()
    answer = daemon.kill(protocol.KillArgs(job_id=JOB))
    assert answer["status"] == "containment requested", answer
    assert "attempt.recontainment_started" in kinds(daemon)
    assert evidence(daemon)["recontainment"], evidence(daemon)
    if members == "walk-member":
        assert not seed.alive and seed.ended_by == TERM, world.tried


def test_an_unverifiable_census_whose_walk_members_are_tainted_runs_no_protocol(daemon, world, monkeypatch):
    """Review r3's P3-1 rule read as written, "a verifiable read could attribute a member":
    a walk member an unverifiable census first recorded is tainted, and so is every member
    below it, so a verifiable read does not attribute it either, and the protocol would
    spend a TERM grace on every `kill` with nothing it may signal, as in case 3 (a legacy
    attempt boot: no census of it verifies). A walk or responsibility member whose identity
    this census could not read still runs the protocol, unless a tainted census parent taints it:
    `_tainted` matches a member's own record by its identity, but follows parent links without one."""
    member = stranger_group(world)
    seed, child = walk_seed(daemon, world, tainted=True)
    daemon.store.update_attempt(ATTEMPT, boot_id="99")
    finished(daemon)
    census = daemon._contain(attempt(daemon))
    assert census.unverifiable and {5800, 5801} <= census.descendant_pids, census.to_dict()
    answers, scheduled = kill_force_release_kill_kill(daemon, monkeypatch)
    assert [answer["status"] for answer in answers] == ["already finished"] * 4, answers
    assert not scheduled and not world.tried, (scheduled, world.tried)
    assert "recontainment" not in evidence(daemon)
    assert member.alive and seed.alive and child.alive


# 4. A live process at the guardian pid with the recorded start, after a reboot.

@pytest.mark.parametrize("path", ["census", "kill", "dead-guardian"])
def test_case_4_a_process_at_the_guardian_pid_with_its_start_on_a_later_boot_is_excluded(daemon, world,
                                                                                         monkeypatch, path):
    leader = world.add(4242, 1, 4242, start=STARTED)      # the recorded start, by chance, on boot B
    member = world.add(4250, 4242, 4242)
    world.boot = BOOT_B
    census = daemon._contain(attempt(daemon))
    assert not census.leader_verified and not census.group_pids and census.verified_empty, census.to_dict()
    assert census.recycled[4242]["reason"] == "different boot identity"   # its birth decides it
    if path == "kill":
        finished(daemon)
        inline(daemon, monkeypatch)
        assert daemon.kill(protocol.KillArgs(job_id=JOB))["status"] == "already finished"
    elif path == "dead-guardian":
        daemon.store.update_job(JOB, max_attempts=1)
        daemon._process_attempt(ATTEMPT)
        assert attempt(daemon)["state"] == "lost", attempt(daemon)
    assert leader.alive and member.alive and not world.signalled()
    assert not [kind for kind, pid, _ in world.tried if kind == "pid"]   # a group signal is refused by C-5.4


# 5 and 6. A stored owned, counted or responsible identity from another boot: never a seed or a target.

@pytest.mark.parametrize("key", SEED_KEYS)
def test_cases_5_and_6_a_stored_identity_from_another_boot_never_seeds_or_targets(daemon, world, monkeypatch, key):
    """Recorded on boot B, the current one, but not on the attempt's boot A: a census on a
    later boot wrote it (release/217 kept such identities; an owned one cannot be written
    today, `_record_owned` needs the guardian alive on its boot). #250 alone seeds it,
    walks to its child, and signals both as recorded processes of a verifiable census."""
    world.boot = BOOT_B
    stranger = world.add(5700, 1, 5700)
    child = world.add(5701, 5700, 5700)
    store(daemon, **{key: {"5700": kept(world.identity(5700))}})
    finished(daemon)
    inline(daemon, monkeypatch)
    status = daemon.kill(protocol.KillArgs(job_id=JOB))["status"]
    assert not world.tried and stranger.alive and child.alive, world.tried
    assert status == "already finished"
    census = daemon._contain(attempt(daemon))
    assert not census.live_pids and census.verified_empty, census.to_dict()
    assert census.other_boot == {5700: [kept(world.identity(5700))]}
    for written in SEED_KEYS[1:]:
        assert "5701" not in evidence(daemon).get(written, {})


def test_case_5_an_owned_identity_of_another_boot_attributes_no_member(daemon, world):
    """The attribution itself (`_signal_reasons`): a member a leaderless group names is a target
    only by another source or an owned identity, and an owned identity of another boot than the
    attempt's is not one, even when it is the member's own current identity."""
    world.boot = BOOT_B
    member = world.add(5700, 1, 4242)
    store(daemon, owned_identities={"5700": kept(world.identity(5700))})
    census = procs.Containment(group_pids=frozenset({5700}), identities={5700: world.identity(5700)},
                               shapes={5700: {"ppid": 1, "pgid": 4242, "stat": "S"}})
    assert daemon._signal_targets(attempt(daemon), census) == {}
    daemon.store.update_attempt(ATTEMPT, boot_id=BOOT_B)               # the attempt's own boot: owned
    assert daemon._signal_targets(attempt(daemon), census) == {5700: world.identity(5700)}
    assert member.alive


# 7. A stored chain ancestor or held parent from another boot: never re-added, never held.

def test_case_7_a_chain_ancestor_stored_on_another_boot_is_never_re_added(daemon, world, monkeypatch):
    tree = owned_worktree(daemon, monkeypatch, world)
    world.boot = BOOT_B
    ancestor = world.add(5900, 1, 5900, cwd=str(tree))
    record = kept(world.identity(5900))
    store(daemon, chain_identities={"5900": record},
          chain_reasons={"5900": "pid 5900: ancestor of marked pid 5901, cwd in worktree"})
    finished(daemon)
    inline(daemon, monkeypatch)
    daemon.kill(protocol.KillArgs(job_id=JOB))
    assert ancestor.alive and not world.tried, world.tried
    census = daemon._contain(attempt(daemon))
    assert not census.chain_pids and census.containment_empty, census.to_dict()
    assert census.other_boot == {5900: [record]}


@pytest.mark.parametrize(("recorded_on", "keys"), [
    (BOOT_A, ("held_parents",)), (BOOT_A, ("held_marked",)), (BOOT_A, ("held_parents", "held_marked")),
    (BOOT_B, ("held_marked",)), (BOOT_C, ("held_parents",)),
], ids=["attempt-boot-parents", "attempt-boot-marked", "attempt-boot-both", "current-boot-marked",
        "third-boot-parents"])
def test_case_7_a_held_parent_from_another_boot_is_never_held(daemon, world, recorded_on, keys):
    """Recorded on the attempt's boot A (`held_parents`, or `held_marked` naming it), a parent
    is held again by its birth while a marked process names it. On boot B a process at that
    pid with that start, by chance, is another process: only the boot tells (#250 compared
    the start alone). One recorded on another boot than the attempt's is not the attempt's."""
    parent = world.add(6000, 1, 6000)
    world.boot = BOOT_B
    record = asdict(procs.ProcessIdentity(6000, recorded_on, parent.start))
    store(daemon, **{key: {"6000": record} for key in keys})
    census = daemon._contain(attempt(daemon))
    assert not census.held_pids and census.verified_empty, census.to_dict()
    if recorded_on != BOOT_A:
        assert census.other_boot == {6000: [record]}
    assert parent.alive and not world.tried


@pytest.mark.parametrize("now", [BOOT_A, None, SECONDS], ids=["same-boot", "unreadable", "legacy-seconds"])
def test_case_7_a_held_parent_on_the_attempts_boot_or_one_not_comparable_is_still_held(daemon, world, now):
    """The guard: on the attempt's own boot the held parent is held as #250 held it, and a
    boot that cannot be compared keeps holding (a hold never signals)."""
    parent = world.add(6002, 1, 6002)
    store(daemon, held_marked={"6002": asdict(procs.ProcessIdentity(6002, BOOT_A, parent.start))})
    world.boot = now
    census = daemon._contain(attempt(daemon))
    assert census.held_pids == {6002}, census.to_dict()
    assert daemon._signal_targets(attempt(daemon), census) == {}


# 8. A marked process on a later boot: a member and a target; nothing about it is kept.

def test_case_8_a_marked_process_on_a_later_boot_is_a_target_and_nothing_is_kept(daemon, world, monkeypatch):
    """#259's stated exception: the marker source matches the attempt's environment, so a
    later-boot process carrying its exact marker pair is counted. Under #250 it is a signal
    target, and so is the worktree ancestor the chain rule adds for it in that census; the
    first ancestor outside the worktree is held. Pinned: none of the three is persisted,
    not as a counted identity, a chain identity, a held parent or `held_marked`."""
    tree = owned_worktree(daemon, monkeypatch, world)
    world.boot = BOOT_B
    shell = world.add(6098, 1, 6098, cwd="/Users/operator")
    driver = world.add(6099, 6098, 6098, cwd=str(tree), marked=True, hidden=True)   # a shell: markers hidden
    marked = world.add(6100, 6099, 6098, marked=True, cwd=str(tree))
    census = daemon._contain(attempt(daemon))
    assert census.marker_pids == {6100} and census.chain_pids == {6099} and census.held_pids == {6098}
    assert not census.group_pids and not census.descendant_pids and not census.responsible_pids
    assert set(daemon._signal_targets(attempt(daemon), census)) == {6099, 6100}
    finished(daemon)
    inline(daemon, monkeypatch)
    assert daemon.kill(protocol.KillArgs(job_id=JOB))["status"] == "containment requested"
    assert not marked.alive and marked.ended_by == TERM and not driver.alive and driver.ended_by == TERM
    assert shell.alive and shell.pid not in world.asked()
    after = evidence(daemon)
    for key in (*SEED_KEYS, *D_KILL4_KEYS):
        assert not ({"6098", "6099", "6100"} & set(after.get(key, {}))), (key, after.get(key))
    rounds = after["terminal_containment"]
    assert {signal_["pid"] for signal_ in rounds[-1]["signals"]} == {6099, 6100}
    census = daemon._contain(attempt(daemon))       # the protocol's carry ended with it: nothing holds
    assert census.verified_empty and shell.alive, census.to_dict()


# 9. A taint record from another boot never matches.

@pytest.mark.parametrize(("record_boot", "member_boot", "attempt_boot", "matches"), [
    (BOOT_A, BOOT_B, BOOT_A, False),     # recorded on the attempt's boot; the member is of boot B
    (BOOT_B, BOOT_B, BOOT_A, False),     # recorded on boot B, not the attempt's
    (BOOT_C, BOOT_B, None, False),       # the member's boot is not the record's
    (BOOT_A, BOOT_A, BOOT_A, True),      # the attempt's own boot: taint holds as before
    (SECONDS, BOOT_A, BOOT_A, True),     # legacy seconds compare by start, as before
    ("", BOOT_B, BOOT_A, True),          # no boot recorded: by start, as before
], ids=["attempt-boot-record", "other-boot-record", "member-other-boot", "own-boot", "legacy", "unrecorded"])
def test_case_9_a_taint_record_from_another_boot_never_matches(record_boot, member_boot, attempt_boot, matches):
    start = "Sat Sep  5 11:00:00 2026"
    found = procs.Containment(descendant_pids=frozenset({7000, 7001}),
                              identities={7000: procs.ProcessIdentity(7000, member_boot, start),
                                          7001: procs.ProcessIdentity(7001, member_boot, start)},
                              shapes={7000: {"ppid": 1, "pgid": 7000, "stat": "S"},
                                      7001: {"ppid": 7000, "pgid": 7000, "stat": "S"}})
    stored = {"unverified_identities": {"7000": {"pid": 7000, "boot_id": record_boot, "proc_start": start}}}
    assert Daemon._tainted(stored, found, attempt_boot) == ({7000, 7001} if matches else set())


def test_case_9_the_taint_write_receives_no_identity_of_another_boot(daemon, world):
    """A marked parent recorded as tainted on boot B and its marked child counted on boot B
    (both by a census on that later boot, before the boot filter): #250 alone wrote the child
    into `unverified_identities` as tainted from boot B's record."""
    world.boot = BOOT_B
    parent = world.add(7099, 1, 7099, marked=True)
    child = world.add(7100, 7099, 7099, marked=True)
    store(daemon, unverified_identities={"7099": kept(world.identity(7099))},
          counted_identities={"7100": kept(world.identity(7100))})
    before = evidence(daemon)["unverified_identities"]
    census = daemon._contain(attempt(daemon))
    assert census.marker_pids == {7099, 7100}
    assert evidence(daemon)["unverified_identities"] == before
    assert set(daemon._signal_targets(attempt(daemon), census)) == {7099, 7100}   # markers, any boot
    assert parent.alive and child.alive


# 10. #259's trackb incident through #250's protocol.

def trackb(daemon, world) -> procs.Proc:
    """`20261014-071003-trackb-d1408-review5/a1`'s shape: the guardian recorded on boot A (its
    row and `start.json`), its receipt naming child 4243 with no identity, and on boot B pid
    4243 an unrelated `tail -40` under an operator's shell."""
    adir = attempt_dir(daemon.root, JOB, 1)
    (adir / "start.json").write_text(json.dumps({"guardian_pid": 4242, "pgid": 4242, "boot_id": BOOT_A,
                                                 "proc_start": STARTED}))
    publish_receipt(daemon, rc=0)                                       # names child_pid 4243
    world.boot = BOOT_B
    world.add(4300, 1, 4300)
    return world.add(4243, 4300, 4300)


def test_case_10_trackb_is_released_by_confirm_dead_and_never_signalled(daemon, world, monkeypatch):
    stranger = trackb(daemon, world)
    daemon.store.update_attempt(ATTEMPT, child_pid=None, state="finalizing", rc=0)
    quarantined(daemon, procs.Containment(descendant_pids=frozenset({4243}), unverifiable=True,
                                          errors=("recorded identity unavailable for pid 4243",)))
    inline(daemon, monkeypatch)
    assert daemon.kill(protocol.KillArgs(job_id=JOB, confirm_dead=True))["status"] == "resolution requested"
    a = attempt(daemon)
    assert a["state"] == "lost", a
    assert json.loads(a["evidence_json"])["recycled"]["4243"]["reason"] == REASON
    assert "counted_identities" not in json.loads(a["evidence_json"])
    assert daemon.kill(protocol.KillArgs(job_id=JOB))["status"] == "already finished"
    assert stranger.alive and not world.tried


def test_case_10_trackb_finalizes_from_its_receipt_with_nothing_signalled(daemon, world):
    stranger = trackb(daemon, world)
    daemon.store.update_attempt(ATTEMPT, child_pid=None, state="finalizing", rc=0)
    daemon._finalize(attempt(daemon))
    a = attempt(daemon)
    assert a["state"] == "succeeded" and not a["quarantine_reason"], a
    assert "attempt.containment_started" not in [row["kind"] for row in daemon.store.list_events(JOB)]
    assert stranger.alive and not world.tried


# 11. r1's six P1 shapes, on the attempt's boot and after a reboot.

def shape_kill(daemon, world, monkeypatch, reboot):
    finished(daemon)
    inline(daemon, monkeypatch)
    return daemon.kill(protocol.KillArgs(job_id=JOB))["status"]


def shape_confirm_dead_then_kill(daemon, world, monkeypatch, reboot):
    finished(daemon, "lost")
    inline(daemon, monkeypatch)
    first = daemon.kill(protocol.KillArgs(job_id=JOB, confirm_dead=True))["status"]
    return first, daemon.kill(protocol.KillArgs(job_id=JOB))["status"]


def shape_dead_guardian(daemon, world, monkeypatch, reboot):
    daemon.store.update_job(JOB, max_attempts=1)
    daemon._process_attempt(ATTEMPT)
    return attempt(daemon)["state"]


def shape_recorded_boot(daemon, world, monkeypatch, reboot):
    """r1's "another recorded boot": the attempt's row names a boot that is not this one. As
    a boot-session UUID (after a reboot) the boot scope drops the group; as a string that is
    no UUID (#250's own test) the leader check does."""
    daemon.store.update_attempt(ATTEMPT, boot_id=BOOT_C if reboot else "another-boot")
    finished(daemon)
    inline(daemon, monkeypatch)
    census = daemon._contain(attempt(daemon))
    return census.recycled[4242]["reason"], daemon.kill(protocol.KillArgs(job_id=JOB))["status"]


def shape_unverifiable(daemon, world, monkeypatch, reboot):
    finished(daemon)
    inline(daemon, monkeypatch)

    def unlisted():
        raise procs.InspectionError("pid listing unavailable")
    monkeypatch.setattr(procs.kernel, "pids", unlisted)
    census = daemon._contain(attempt(daemon))
    assert census.unverifiable and not census.leader_verified
    daemon.kill(protocol.KillArgs(job_id=JOB))
    return sorted(census.group_pids)


def shape_another_attempts_provider(daemon, world, monkeypatch, reboot):
    other = add_running(daemon, JOB + "-b", 4242)
    daemon.store.update_attempt(other, proc_start="Mon Oct 19 09:00:00 2026", boot_id=world.boot)
    finished(daemon)
    inline(daemon, monkeypatch)
    daemon.kill(protocol.KillArgs(job_id=JOB))
    return attempt(daemon)["state"]


SHAPES = {
    # Fix round 2b, decision 2: a census of held members only runs no protocol on the same boot either.
    "kill": (shape_kill, "already finished", "already finished"),
    "confirm-dead-then-kill": (shape_confirm_dead_then_kill, ("not quarantined", "already finished"),
                               ("already finished", "already finished")),
    "dead-guardian": (shape_dead_guardian, "quarantined", "lost"),
    "recorded-boot": (shape_recorded_boot, ("group of another boot", "already finished"),
                      (REASON, "already finished")),
    "unverifiable": (shape_unverifiable, [5600], []),
    "another-attempts-provider": (shape_another_attempts_provider, "succeeded", "succeeded"),
}


@pytest.mark.parametrize("reboot", [False, True], ids=["same-boot", "after-reboot"])
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_case_11_r1s_p1_shapes_never_signal_the_stranger_on_either_boot(daemon, world, monkeypatch, shape, reboot):
    run, same, rebooted = SHAPES[shape]
    member = stranger_group(world)
    if reboot:
        world.boot = BOOT_B
    assert run(daemon, world, monkeypatch, reboot) == (rebooted if reboot else same)
    assert member.alive and member.pid not in world.signalled()
    assert not [pid for kind, pid, _ in world.tried if kind == "pid"], world.tried
    recycled = evidence(daemon).get("recycled", {}).get("4242", {}).get("reason")
    if reboot:
        assert recycled == REASON                         # the boot scope, once, before the leader check
    elif shape != "recorded-boot":
        assert recycled is None


# 12. The property: attempt boot x current boot x every stored key.

PROPERTY = settings(max_examples=60, deadline=None, derandomize=True, database=None,
                    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
STRANGERS = [5001, 5002, 5003, 5004]
STARTS = ["Sat Sep  5 11:00:00 2026", "Sat Sep  5 11:00:01 2026"]
#: Attempt boots: a boot-session UUID, legacy seconds the world's sysctl matches, and none.
ATTEMPT_BOOTS = [BOOT_A, SECONDS, None]
#: Current boots: the attempt's ("same"), another UUID, legacy seconds after a failed UUID
#: read, and one that cannot be read.
CURRENT_BOOTS = ["same", BOOT_B, SECONDS, None]
RECORDED_BOOTS = [BOOT_A, BOOT_B, BOOT_C, SECONDS, ""]
STORED_KEYS = (*SEED_KEYS, *D_KILL4_KEYS)


@st.composite
def worlds(draw):
    attempt_boot = draw(st.sampled_from(ATTEMPT_BOOTS))
    now = draw(st.sampled_from(CURRENT_BOOTS))
    procs_ = {pid: draw(st.sampled_from(STARTS)) for pid in draw(st.sets(st.sampled_from(STRANGERS)))}
    births = st.tuples(st.sampled_from(RECORDED_BOOTS), st.sampled_from(STARTS))
    stored = {key: draw(st.dictionaries(st.sampled_from(STRANGERS), births, max_size=2)) for key in STORED_KEYS}
    return {"attempt_boot": attempt_boot, "now": attempt_boot if now == "same" else now, "procs": procs_,
            "stored": stored, "orphan_group": draw(st.booleans())}


def provably_other(first: str | None, second: str | None) -> bool:
    """The test's own statement of "another boot" (C-5.3): two boot-session UUIDs that
    differ, and nothing else (legacy seconds, a missing or unreadable boot decide nothing)."""
    def session(value):
        try:
            return str(uuid.UUID(str(value or "").strip()))
        except ValueError:
            return None
    return bool(session(first) and session(second) and session(first) != session(second))


def foreign(world_: dict, pid: int) -> bool:
    """Every stored identity at `pid` provably names another boot than the attempt's or the
    current one: nothing stored is its."""
    entries = [boot for key in STORED_KEYS for at, (boot, _) in world_["stored"][key].items() if at == pid]
    return all(provably_other(boot, world_["attempt_boot"]) or provably_other(boot, world_["now"])
               for boot in entries)


@PROPERTY
@given(case=worlds())
def test_case_12_property_nothing_of_another_boot_seeds_holds_or_targets(daemon, world, monkeypatch, case):
    """For every attempt boot, current boot and stored key: a stranger every stored identity of
    which names another boot is never a census member, never held and never signalled
    (invariant 1); with the attempt's boot over, the group, walk and responsibility sources are
    empty (invariant 2); and no stored key gains an identity of another boot than the
    attempt's (invariant 3). A leaderless group at the recorded pgid is never signalled."""
    for kept_ in (world.procs, world.signals, world.delivered, world.tried):
        kept_.clear()
    for pid, start in case["procs"].items():
        world.add(pid, 1, pid, start=start)
    if case["orphan_group"]:
        stranger_group(world)
    world.boot = case["now"]
    procs.forget_boot_id()
    daemon.__dict__.pop("_containment_rounds", None)
    stored = {key: {str(pid): {"pid": pid, "boot_id": boot, "proc_start": start}
                    for pid, (boot, start) in entries.items()}
              for key, entries in case["stored"].items() if entries}
    daemon.store.update_attempt(ATTEMPT, state="succeeded", boot_id=case["attempt_boot"],
                                evidence_json=json.dumps(stored))
    daemon.store.update_job(JOB, state="succeeded")
    censuses = []
    with monkeypatch.context() as patch:
        patch.setattr(daemon, "_schedule", lambda key, fn, *args, **kw: fn(*args))
        real = Daemon._contain
        patch.setattr(daemon, "_contain", lambda a: censuses.append(real(daemon, a)) or censuses[-1])
        daemon._contain(attempt(daemon))
        daemon._contain(attempt(daemon))
        daemon.kill(protocol.KillArgs(job_id=JOB))
    over = provably_other(case["attempt_boot"], case["now"])
    strangers = {pid for pid in case["procs"] if foreign(case, pid)}
    for census in censuses:
        assert not (strangers & (census.live_pids | census.held_pids)), (strangers, census.to_dict())
        if over:
            assert not (census.group_pids | census.descendant_pids | census.responsible_pids), census.to_dict()
    assert not (strangers & world.asked()), world.tried
    if case["orphan_group"]:
        assert 5600 not in world.signalled() and ("pid", 5600, TERM) not in world.tried
    after = json.loads(attempt(daemon)["evidence_json"] or "{}")
    for key in STORED_KEYS:
        gained = {pid: value for pid, value in after.get(key, {}).items() if stored.get(key, {}).get(pid) != value}
        assert not [value for value in gained.values()
                    if provably_other(value["boot_id"], case["attempt_boot"])], (key, gained)
