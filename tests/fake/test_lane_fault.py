"""C-4.5, C-9.3, C-23.44: an `auth-dead` lane is the lane's fault, and the job moves on.

2026-09-30 15:43Z: after 6,742 s with nothing placed, admission put 37 jobs from
about 20 sessions on claude-5, the one Claude lane above its floor. Its
organisation had disabled Claude Code there: every attempt ended in Claude Code's
own placeholder, "Your organization has disabled Claude subscription access for
Claude Code", with no request served, and was classed `auth-dead`. C-4.5 then
allowed no retry, so all 37 ended `failed` with rc 5 after one attempt each,
while the daemon disabled claude-5 (C-23.44).

Now an `auth-dead` attempt of a job with no lane pin, that is no conversation
turn, whose workspace it left as it found it (a read-only job, or a writable one
whose model never answered and whose salvage found its end tree equal to its
start snapshot with HEAD where it was), moves on to the next candidate as a
`limited` one does, and `max_attempts` does not count it. A job moves on once: a
second `auth-dead` ends it and leaves that lane enabled, because two lanes
refusing one job points at the job, so no job disables more lanes than before. A
pinned job, and a job whose workspace changed, end as before. These drive the
daemon's own finalization and admission in-process, no provider run.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json

import pytest
import random

from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import daemon as daemon_module
from subfleet.adapters import registry
from subfleet.adapters.registry import register
from subfleet.contracts import Credential, Outcome, OutcomeClass, Reading, ReadingLabel
from subfleet.daemon import Daemon, after, utcnow
from subfleet.procs import Containment
from tests.fake.conftest import Harness
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401 (a fixture)
from tests.fake.test_workspace_contract import repository
from tests.fake_adapter import FakeAdapter
from tests.unit.test_salvage import git

BLOCK = ("auth-dead: Your organization has disabled Claude subscription access for Claude Code "
         "· Use an Anthropic API key instead, or ask your admin to enable access")


def assertions_only(caught) -> None:
    """A mutation's failure is the property's assertions, one or several (Hypothesis groups
    distinct counterexamples), never an error of another kind."""
    error = caught.value
    found = list(error.exceptions) if isinstance(error, BaseExceptionGroup) else [error]
    assert found and all(isinstance(item, AssertionError) for item in found), found


class LaneRefuses(FakeAdapter):
    """The incident's verdict (C-9.3): the CLI's organisation block, no model answering."""

    def classify(self, attempt_dir, launch, exit_info):
        return Outcome(OutcomeClass.AUTH_DEAD, BLOCK, evidence={"rc": exit_info.rc, "model_answered": False})


class RevokedMidRun(FakeAdapter):
    """The lane's access went while the attempt ran: the model had answered."""

    def classify(self, attempt_dir, launch, exit_info):
        return Outcome(OutcomeClass.AUTH_DEAD, BLOCK, evidence={"rc": exit_info.rc, "model_answered": True})


class Transient(FakeAdapter):
    def classify(self, attempt_dir, launch, exit_info):
        return Outcome(OutcomeClass.TRANSIENT, "transient: stream disconnected",
                       evidence={"rc": exit_info.rc, "model_answered": True})


def add_lane(daemon, harness, lane_id: str, *, measured: bool = False):
    """Another account on its own credential: a lane sharing codex-1's home would be its
    re-enrolment (C-11.2), and a pin on either would follow to the other."""
    first = daemon.store.get_lane("codex-1")
    home = str(harness.root / f"home-{lane_id}")
    daemon.store.put_lane(replace(first, lane_id=lane_id, account_key=f"codex:{lane_id}", enabled=True,
                                  credential=Credential("codex", home, "home"), home=home))
    if measured:
        measure(daemon, lane_id)


def measure(daemon, lane_id: str, utilization: float = .1):
    daemon.store.add_reading(Reading(lane_id, "account", "seven_day", utilization, after(3600),
                                     ReadingLabel.PROVIDER, "fixture", utcnow()))


def finish(daemon, attempt, adapter, *, rc: int = 1):
    """The attempt's provider has exited; finalize it under `adapter`'s verdict."""
    register("codex", adapter)
    adir = daemon.root / "jobs" / attempt["job_id"] / f"a{attempt['seq']}"
    adir.mkdir(mode=0o700, exist_ok=True)
    daemon._pending_launches.discard(attempt["attempt_id"])
    daemon._finalize(receipt_fixture(daemon, attempt, adir, rc=rc))
    register("codex", FakeAdapter)


def attempts(daemon, job_id):
    return daemon.store.list_attempts(job_id)


def notice(daemon, job_id):
    texts = [row["text"] for row in daemon.store.list_notices() if row["job_id"] == job_id]
    assert len(texts) == 1, texts
    return texts[0]


# --- the four cases the incident turns on ---------------------------------------------------------

def test_c4_5_an_unpinned_read_only_job_moves_on_after_its_lane_goes_auth_dead(state_daemon):
    daemon, harness = state_daemon
    add_lane(daemon, harness, "codex-2")
    job_id, first, _ = reserve(daemon, harness)
    assert first["lane_id"] == "codex-1"
    finish(daemon, first, LaneRefuses)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["wait_reason"], job["rc"]) == ("waiting", "capacity", None)
    assert daemon.store.get_lane("codex-1").enabled is False                 # C-23.44, as before
    row = daemon.store.get_attempt(first["attempt_id"])
    assert (row["state"], row["outcome_class"]) == ("failed", "auth-dead")
    fault = json.loads(row["evidence_json"])["lane_fault"]
    assert fault == {"class": "auth-dead", "lane_id": "codex-1", "seq": 1, "model_answered": False,
                     "workspace": "read-only"}
    assert daemon.store.list_notices() == []                                   # not over yet
    daemon._admit()
    second = attempts(daemon, job_id)[-1]
    assert [row["lane_id"] for row in attempts(daemon, job_id)] == ["codex-1", "codex-2"]
    finish(daemon, second, FakeAdapter, rc=0)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"], job["accepted_attempt_id"]) == ("succeeded", 0, second["attempt_id"])
    text = notice(daemon, job_id)
    assert f"{job_id}: succeeded; rc=0" in text.splitlines()[0]
    assert ("attempt a1: lane codex-1 went auth-dead (" + BLOCK + "); it is disabled until `subfleet lanes "
            "enroll` rebinds its credential, and the job moved on to the next lane without counting the attempt") in text


def test_c4_5_an_unpinned_writable_job_whose_tree_is_unchanged_moves_on(state_daemon):
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)                       # codex-1 measured: no admission probe
    head = git(workdir, "rev-parse", "HEAD")
    job_id, first, _ = reserve(daemon, harness, sandbox="workspace-write", in_place=True)
    add_lane(daemon, harness, "codex-2", measured=True)
    finish(daemon, first, LaneRefuses)
    row = daemon.store.get_attempt(first["attempt_id"])
    assert json.loads(row["evidence_json"])["lane_fault"]["workspace"] == "unchanged"
    assert json.loads(row["evidence_json"])["lane_fault"]["head"] == head
    assert not [a for a in daemon.store.list_artifacts(first["attempt_id"]) if a["role"] == "salvage"]
    assert daemon.store.get_job(job_id)["state"] == "waiting"
    daemon._admit()
    assert [row["lane_id"] for row in attempts(daemon, job_id)] == ["codex-1", "codex-2"]
    # The retry starts from the same checkout, at the same head (C-13.3).
    assert json.loads(attempts(daemon, job_id)[-1]["evidence_json"])["baseline_commit"] == head


def test_c4_5_a_pinned_job_fails_on_its_auth_dead_lane_as_before(state_daemon):
    daemon, harness = state_daemon
    add_lane(daemon, harness, "codex-2")
    job_id, first, _ = reserve(daemon, harness, pinned_lane="codex-1")
    finish(daemon, first, LaneRefuses)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 5)
    assert "lane_fault" not in json.loads(daemon.store.get_attempt(first["attempt_id"])["evidence_json"])
    assert daemon.store.get_lane("codex-1").enabled is False
    assert "attempt a1: auth-dead, rc=1: " + BLOCK in notice(daemon, job_id)
    daemon._admit()
    assert len(attempts(daemon, job_id)) == 1


@pytest.mark.parametrize("change", ["edit", "commit"])
def test_c4_5_a_job_whose_workspace_changed_fails_for_reconciliation(state_daemon, change):
    """C-13.3: an edit (a salvage ref holds it) or a commit (HEAD moved, the tree the
    same) is the job's work; the job ends `failed` rc 5 as before, for a person to
    reconcile, and the lane is still disabled."""
    daemon, harness = state_daemon
    workdir = repository(daemon, harness)
    job_id, first, _ = reserve(daemon, harness, sandbox="workspace-write", in_place=True)
    add_lane(daemon, harness, "codex-2", measured=True)
    if change == "edit":
        (workdir / "tracked.txt").write_text("the provider's progress\n")
    else:
        git(workdir, "commit", "--allow-empty", "-m", "an empty commit moves HEAD, not the tree")
    finish(daemon, first, LaneRefuses)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 5)
    assert "lane_fault" not in json.loads(daemon.store.get_attempt(first["attempt_id"])["evidence_json"])
    salvaged = [a for a in daemon.store.list_artifacts(first["attempt_id"]) if a["role"] == "salvage"]
    assert bool(salvaged) is (change == "edit")
    assert daemon.store.get_lane("codex-1").enabled is False
    daemon._admit()
    assert len(attempts(daemon, job_id)) == 1


# --- what it costs and what bounds it -------------------------------------------------------------

def test_c4_5_a_lane_fault_is_not_counted_against_max_attempts(state_daemon):
    """A job allowed one attempt still moves on from an auth-dead lane; its next
    attempt is its first counted one, and a transient end there is its last."""
    daemon, harness = state_daemon
    add_lane(daemon, harness, "codex-2")
    job_id, first, _ = reserve(daemon, harness, max_attempts=1)
    finish(daemon, first, LaneRefuses)
    daemon._admit()
    second = attempts(daemon, job_id)[-1]
    assert second["lane_id"] == "codex-2"
    finish(daemon, second, Transient)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 1)
    text = notice(daemon, job_id)
    assert "attempt a2: transient, rc=1: transient: stream disconnected" in text
    assert "attempt a1: lane codex-1 went auth-dead" in text


def test_c4_5_a_job_moves_on_once_and_a_second_auth_dead_ends_it_leaving_that_lane_enabled(state_daemon):
    """Two lanes refusing one job points at the job (a tool's `invalid_api_key` on
    stderr, a project setting that overrides the lane's credential), so the second
    lane is not disabled on that job's word: no job disables more lanes than before
    this rule. A lane that really is dead is found by the next job, whose first
    `auth-dead` it is."""
    daemon, harness = state_daemon
    for lane_id in ("codex-2", "codex-3"):
        add_lane(daemon, harness, lane_id)
    job_id, first, _ = reserve(daemon, harness)
    finish(daemon, first, LaneRefuses)
    daemon._admit()
    second = attempts(daemon, job_id)[-1]
    assert second["lane_id"] == "codex-2"
    finish(daemon, second, LaneRefuses)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 5)
    assert [lane.enabled for lane in daemon.store.list_lanes()] == [False, True, True]
    evidence = json.loads(daemon.store.get_attempt(second["attempt_id"])["evidence_json"])
    assert evidence["auth_dead_again"] == {"lane_id": "codex-2", "lane_left_enabled": True}
    assert "lane_fault" not in evidence
    text = notice(daemon, job_id)
    assert "attempt a2: auth-dead, rc=1: " + BLOCK in text
    assert "attempt a1: lane codex-1 went auth-dead" in text
    assert ("attempt a2: lane codex-2 answered auth-dead too; two lanes refusing one job points at the job, "
            "so codex-2 was left enabled") in text
    daemon._admit()
    assert len(attempts(daemon, job_id)) == 2
    # codex-2 is dead after all: the next job's first auth-dead disables it, and that job moves on.
    other, attempt, _ = reserve(daemon, harness)
    assert attempt["lane_id"] == "codex-2"
    finish(daemon, attempt, LaneRefuses)
    assert daemon.store.get_lane("codex-2").enabled is False
    daemon._admit()
    assert [row["lane_id"] for row in attempts(daemon, other)] == ["codex-2", "codex-3"]


def test_c4_5_a_read_only_job_moves_on_even_after_its_model_answered(state_daemon):
    """Access revoked mid-run: a read-only job reads again on the next lane."""
    daemon, harness = state_daemon
    add_lane(daemon, harness, "codex-2")
    job_id, first, _ = reserve(daemon, harness)
    finish(daemon, first, RevokedMidRun)
    fault = json.loads(daemon.store.get_attempt(first["attempt_id"])["evidence_json"])["lane_fault"]
    assert fault["model_answered"] is True and fault["workspace"] == "read-only"
    daemon._admit()
    assert [row["lane_id"] for row in attempts(daemon, job_id)] == ["codex-1", "codex-2"]


@pytest.mark.parametrize("adapter", [RevokedMidRun, "unsaid"])
def test_c4_5_a_writable_job_whose_model_answered_does_not_move_on(state_daemon, adapter):
    """A writable attempt runs with permissions skipped: its model may have pushed or
    commented, which an unchanged tree does not show. With an answer, or an adapter
    that cannot say, the job ends for a person, as before; a revive (`max_attempts`
    1: "a retry would be a second continuation") is never run twice this way."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    job_id, first, _ = reserve(daemon, harness, sandbox="workspace-write", in_place=True, max_attempts=1)
    add_lane(daemon, harness, "codex-2", measured=True)
    if adapter == "unsaid":
        class adapter(FakeAdapter):                      # noqa: N801 - an adapter with no `model_answered`
            def classify(self, attempt_dir, launch, exit_info):
                return Outcome(OutcomeClass.AUTH_DEAD, BLOCK, evidence={"rc": exit_info.rc})
    finish(daemon, first, adapter)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 5)
    assert "lane_fault" not in json.loads(daemon.store.get_attempt(first["attempt_id"])["evidence_json"])
    daemon._admit()
    assert len(attempts(daemon, job_id)) == 1


def test_c4_5_a_writable_job_in_a_worktree_admission_cut_moves_on_in_that_worktree(state_daemon):
    """C-6.6: not in place, so admission cuts the job a worktree; the lane fault's tree
    test reads that worktree, and the retry starts in it."""
    daemon, harness = state_daemon
    repository(daemon, harness)
    job_id, first, _ = reserve(daemon, harness, sandbox="workspace-write")
    worktree = daemon.store.get_job(job_id)["worktree"]
    assert worktree and worktree != str(harness.workdir)
    add_lane(daemon, harness, "codex-2", measured=True)
    finish(daemon, first, LaneRefuses)
    assert json.loads(daemon.store.get_attempt(first["attempt_id"])["evidence_json"])["lane_fault"]["workspace"] == "unchanged"
    daemon._admit()
    assert [row["lane_id"] for row in attempts(daemon, job_id)] == ["codex-1", "codex-2"]
    assert daemon.store.get_job(job_id)["worktree"] == worktree


def test_c4_5_a_finalization_replayed_after_a_lane_fault_changes_nothing(state_daemon):
    """C-4.2: finalization is idempotent. The row the first pass read is offered again."""
    daemon, harness = state_daemon
    add_lane(daemon, harness, "codex-2")
    job_id, first, adir = reserve(daemon, harness)
    register("codex", LaneRefuses)
    finalizing = receipt_fixture(daemon, first, adir, rc=1)
    daemon._finalize(finalizing)
    before = (daemon.store.get_job(job_id), attempts(daemon, job_id), len(daemon.store.list_events(job_id)))
    daemon._finalize(finalizing)
    daemon._finalize(daemon.store.get_attempt(first["attempt_id"]))
    register("codex", FakeAdapter)
    assert (daemon.store.get_job(job_id), attempts(daemon, job_id), len(daemon.store.list_events(job_id))) == before
    assert daemon.store.get_job(job_id)["state"] == "waiting"


@pytest.mark.parametrize("max_attempts,state", [(1, "failed"), (2, "queued")])
def test_c4_5_an_attempt_that_never_launched_after_a_lane_fault_is_counted_as_ever(state_daemon, max_attempts, state):
    """`_unlaunched` asks `_attempts_left` too: the lane fault is not counted, the
    attempt after it that never launched is."""
    daemon, harness = state_daemon
    add_lane(daemon, harness, "codex-2")
    job_id, first, _ = reserve(daemon, harness, max_attempts=max_attempts)
    finish(daemon, first, LaneRefuses)
    daemon._admit()
    second = attempts(daemon, job_id)[-1]
    daemon._pending_launches.discard(second["attempt_id"])
    daemon._unlaunched(second, "reserved-no-launch")
    assert daemon.store.get_job(job_id)["state"] == state


def test_c4_5_other_attempts_are_counted_around_a_lane_fault(state_daemon):
    """a1 transient, a2 the lane fault, a3 and a4 transient: three counted attempts,
    which is `max_attempts`, and the fault between them not one of them."""
    daemon, harness = state_daemon
    for lane_id in ("codex-2", "codex-3"):
        add_lane(daemon, harness, lane_id)
    job_id, attempt, _ = reserve(daemon, harness)
    for adapter in (Transient, LaneRefuses, Transient, Transient):
        assert daemon.store.get_job(job_id)["state"] == "running"
        finish(daemon, attempt, adapter)
        daemon.store.update_job(job_id, next_check_at=None) if daemon.store.get_job(job_id)["state"] == "waiting" else None
        daemon._admit()
        attempt = attempts(daemon, job_id)[-1]
    rows = attempts(daemon, job_id)
    assert [row["outcome_class"] for row in rows] == ["transient", "auth-dead", "transient", "transient"]
    assert rows[0]["lane_id"] == rows[1]["lane_id"] == "codex-1"          # C-9.5: the same lane once more
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 1)


def test_c4_5_a_conversation_turn_is_never_a_lane_fault():
    """C-26.7: a turn's conversation decides its failover, so its job does not move."""
    outcome = Outcome(OutcomeClass.AUTH_DEAD, BLOCK, evidence={"model_answered": False})
    job = {"pinned_lane": None, "kind": "turn", "sandbox": "read-only", "workdir_head": None}
    attempt = {"lane_id": "claude-5", "seq": 1, "evidence_json": "{}"}
    assert Daemon._lane_fault(job, attempt, outcome, None, [], {}) is None
    assert Daemon._lane_fault({**job, "kind": "dispatch"}, attempt, outcome, None, [], {})["workspace"] == "read-only"
    for cls in OutcomeClass:
        if cls is not OutcomeClass.AUTH_DEAD:
            assert Daemon._lane_fault({**job, "kind": "dispatch"}, attempt, Outcome(cls, "x"), None, [], {}) is None


# --- the property: a job is never failed by the one dead lane it meets -------------------------------

@contextmanager
def fleet(root, lanes: int):
    root.mkdir(parents=True)
    harness = Harness(root)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
        patch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
        patch.setattr(daemon_module.procs, "same_process", lambda *args: False)
        patch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment())
        patch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
        patch.setattr(registry, "_factories", {**registry._factories, "codex": ByLane})
        service = Daemon(root)
        patch.setattr(service, "_launch", lambda attempt: None)
        # C-11.4's probe, run before a moved-on job may use an unproven lane (C-4.5): a lane
        # the test marked dead (`service.dead_lanes`) refuses it as it refuses every attempt.
        service.dead_lanes, service.probes = set(), []

        def probe(job, lane, model, holder):
            service.probes.append((job["job_id"], lane.lane_id))
            if lane.lane_id in service.dead_lanes:
                return Outcome(OutcomeClass.AUTH_DEAD, BLOCK, evidence={"rc": 1, "model_answered": False})
            return Outcome(OutcomeClass.OK, "admitted", evidence={"rc": 0, "model_answered": True})
        patch.setattr(service, "_execute_probe", probe)
        try:
            for n in range(2, lanes + 1):
                add_lane(service, harness, f"codex-{n}")
            for n in range(1, lanes + 1):
                measure(service, f"codex-{n}")
            yield service, harness
        finally:
            service.close()
    harness.check_notices()


class ByLane(FakeAdapter):
    """`auth-dead` with no model answering where the test wrote DEAD, else `ok`."""

    def classify(self, attempt_dir, launch, exit_info):
        if (attempt_dir / "stdout").read_bytes() == b"DEAD":
            return Outcome(OutcomeClass.AUTH_DEAD, BLOCK, evidence={"rc": exit_info.rc, "model_answered": False})
        return Outcome(OutcomeClass.OK, "fake provider succeeded", evidence={"rc": exit_info.rc})


JOBS = st.lists(st.fixed_dictionaries({"pin": st.none() | st.integers(1, 4)}), min_size=1, max_size=5)


def run_fleet(root, lanes, dead, jobs, writable, prove, order):
    """One example: the fleet, the jobs, attempts ending in `order` until nothing more can be
    placed; then every job as (id, pin, workspace, row, attempts) and the lanes as they ended."""
    dead_lanes = {f"codex-{n}" for n in range(1, lanes + 1) if dead[n - 1]}
    with fleet(root / "state", lanes) as (service, harness):
        service.policy["admission"]["prove_idle_s"] = prove
        service.dead_lanes = dead_lanes
        submitted = []
        for spec in jobs:
            pin = f"codex-{spec['pin']}" if spec["pin"] and spec["pin"] <= lanes else None
            job_id = service.dispatch("submit", harness.submit_args(**({"pinned_lane": pin} if pin else {})))["job_id"]
            submitted.append((job_id, pin, "read-only"))
        if writable:
            repository(service, harness)
            job_id = service.dispatch("submit", harness.submit_args(sandbox="workspace-write", in_place=True))["job_id"]
            submitted.append((job_id, None, writable))
        for _ in range(4 * (len(submitted) + lanes) + 4):
            with service.store.transaction("test.due") as tx:          # every wait due: no clocks in this model
                tx.execute("UPDATE jobs SET next_check_at=NULL WHERE state='waiting'")
            probes = len(service.probes)
            service._admit()
            live = service.store.query("SELECT * FROM attempts WHERE state='reserved'")
            if not live and len(service.probes) == probes:
                break                                 # a pass that placed nothing and probed nothing
            order.shuffle(live)
            for attempt in live:
                job = service.store.get_job(attempt["job_id"])
                if job["sandbox"] == "workspace-write" and writable == "changed":
                    (harness.workdir / "tracked.txt").write_text(f"work of {attempt['attempt_id']}\n")
                adir = service.root / "jobs" / attempt["job_id"] / f"a{attempt['seq']}"
                adir.mkdir(mode=0o700, exist_ok=True)
                service._finalize(receipt_fixture(service, attempt, adir, rc=1 if attempt["lane_id"] in dead_lanes else 0,
                                                  stdout=b"DEAD" if attempt["lane_id"] in dead_lanes else b"done\n"))
        enabled = {lane.lane_id for lane in service.store.list_lanes() if lane.enabled}
        found = [(job_id, pin, workspace, service.store.get_job(job_id), service.store.list_attempts(job_id))
                 for job_id, pin, workspace in submitted]
        probed = list(service.probes)
    return dead_lanes, enabled, found, probed


FLEETS = dict(lanes=st.integers(1, 4), dead=st.lists(st.booleans(), min_size=4, max_size=4), jobs=JOBS,
              writable=st.sampled_from([None, "unchanged", "changed"]), prove=st.sampled_from([None, 900]),
              order=st.randoms(use_true_random=False))


def check_fleet(lanes, dead_lanes, enabled, found, probed, prove):
    alive = {f"codex-{n}" for n in range(1, lanes + 1)} - dead_lanes
    moved = {job_id for job_id, _, _, _, rows in found
             if any(json.loads(row["evidence_json"] or "{}").get("lane_fault") for row in rows)}
    # C-4.5: only a job that moved on waits for a probe, and only of a lane not proven.
    assert {job_id for job_id, _ in probed} <= moved
    if prove is None:
        assert probed == []
    evidence = lambda row: json.loads(row["evidence_json"] or "{}")                    # noqa: E731
    assert alive <= enabled                                   # no lane that works was ever disabled
    disabled_by: dict[str, int] = {}
    for job_id, pin, workspace, job, rows in found:
        faults = [row for row in rows if evidence(row).get("lane_fault")]
        refused = [row for row in rows if row["outcome_class"] == "auth-dead"]
        assert all(row["lane_id"] in dead_lanes for row in refused)
        assert len(faults) <= 1                               # a job moves on once
        assert len(rows) - len(faults) <= job["max_attempts"]  # and that attempt is not counted
        # No job disables more lanes than before the rule: at most one, its first auth-dead's.
        disabling = [row for row in refused if not evidence(row).get("auth_dead_again")]
        assert len(disabling) <= 1 and all(row["lane_id"] not in enabled for row in disabling)
        for row in disabling:
            disabled_by[row["lane_id"]] = disabled_by.get(row["lane_id"], 0) + 1
        if pin is None and workspace != "changed":
            if job["state"] == "failed":
                # Never by the one dead lane it met: only by a second, which is left enabled, and
                # with the hold on never at all: a moved-on job reaches no unproven lane unprobed.
                assert prove is None, (job_id, rows)
                assert [bool(evidence(row).get("lane_fault")) for row in refused] == [True, False], (job_id, rows)
                assert evidence(refused[1])["auth_dead_again"]["lane_left_enabled"] is True
                assert (job["rc"], rows[-1]["attempt_id"]) == (5, refused[1]["attempt_id"])
            else:
                assert job["state"] == ("succeeded" if alive else "waiting"), (job_id, job["state"], rows)
        elif pin is not None:
            assert {row["lane_id"] for row in rows} <= {pin}
            if pin in dead_lanes and rows:
                assert (job["state"], job["rc"]) == ("failed", 5)
        elif rows and rows[0]["lane_id"] in dead_lanes:
            assert (job["state"], job["rc"]) == ("failed", 5) and not faults
    # Every disabled lane was disabled by a job's first auth-dead there, or by a probe.
    assert dead_lanes - enabled == set(disabled_by) | ({lane for _, lane in probed} & dead_lanes)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(**FLEETS)
# The shapes each mutation below must meet, run on every run, not left to the search:
# the incident (one dead lane, then a live one), two dead lanes with the hold off (a job
# moves on once, then ends), and two dead lanes with the hold on (the second is probed).
@example(lanes=2, dead=[True, False, False, False], jobs=[{"pin": None}], writable=None, prove=None,
         order=random.Random(0))
@example(lanes=3, dead=[True, True, False, False], jobs=[{"pin": None}], writable=None, prove=None,
         order=random.Random(0))
@example(lanes=3, dead=[True, True, False, False], jobs=[{"pin": None}], writable=None, prove=900,
         order=random.Random(0))
def test_c4_5_no_unchanged_unpinned_job_is_failed_by_the_one_dead_lane_it_meets(
        tmp_path_factory, lanes, dead, jobs, writable, prove, order):
    """For any fleet of 1 to 4 lanes, some of them dead (every attempt there `auth-dead`
    before a model answers), any jobs (lane-pinned or not, read-only, and at most one
    writable job in the one checkout whose tree its attempts leave unchanged or change),
    with the pilot hold on or off (C-6.14), and attempts ending in any order, run until
    nothing more can be placed:

    - no unpinned job whose workspace is unchanged ends `failed` because of `auth-dead`
      while another enabled lane exists: it moves on, and then with a live lane it
      succeeds, and with none it waits. With the pilot hold on (the default) that is all:
      a moved-on job reaches an unproven lane only after Subfleet's own probe of it, and
      a probe's `auth-dead` disables the lane, so the job never meets a second dead lane
      itself. With the hold off it ends `failed` at a second dead lane, which is left
      enabled, so with one dead lane, as on 2026-09-30, no such job fails either way;
    - only a moved-on job waits for a probe, and only with the hold on;
    - no job disables more than one lane, no lane that works is ever disabled, and every
      disabled lane was a job's first `auth-dead` or a probe's;
    - `max_attempts` counts every attempt but a job's one lane fault;
    - a pinned job never runs elsewhere, and one pinned to a dead lane that it reached
      ends `failed` rc 5; a writable job whose tree changed ends `failed` rc 5 on a dead lane.
    """
    dead_lanes, enabled, found, probed = run_fleet(tmp_path_factory.mktemp("lane-fault"), lanes, dead, jobs,
                                                   writable, prove, order)
    check_fleet(lanes, dead_lanes, enabled, found, probed, prove)
    if prove is not None or len(dead_lanes) <= 1:             # the stated property, exactly
        assert not [job for _, pin, workspace, job, rows in found if pin is None and workspace != "changed"
                    and job["state"] == "failed"]


def test_c4_5_the_property_fails_under_the_rule_of_2026_09_30(tmp_path_factory, monkeypatch):
    """Mutation: with no lane fault (C-4.5 as it stood), the property above finds the
    incident: an unpinned read-only job failed by the one dead lane it met."""
    monkeypatch.setattr(Daemon, "_lane_fault", staticmethod(lambda *args: None))
    with pytest.raises((AssertionError, BaseExceptionGroup)) as caught:
        test_c4_5_no_unchanged_unpinned_job_is_failed_by_the_one_dead_lane_it_meets(tmp_path_factory=tmp_path_factory)
    assertions_only(caught)


def test_c4_5_the_property_fails_when_a_moved_on_job_may_pilot_an_unproven_lane(tmp_path_factory, monkeypatch):
    """Mutation: with a job that moved on allowed to be a lane's pilot (round 2 of the
    review), the second dead lane fails it with the hold on, which the property forbids."""
    monkeypatch.setattr(Daemon, "_moved_on_from", lambda self, job_id: 0)
    with pytest.raises((AssertionError, BaseExceptionGroup)) as caught:
        test_c4_5_no_unchanged_unpinned_job_is_failed_by_the_one_dead_lane_it_meets(tmp_path_factory=tmp_path_factory)
    assertions_only(caught)


def test_c4_5_the_property_fails_when_a_job_may_move_on_without_end(tmp_path_factory, monkeypatch):
    """Mutation: with nothing counting a job's lane faults, one job disables lane after
    lane, which the property's "no job disables more than one lane" catches."""
    monkeypatch.setattr(Daemon, "_uncharged", staticmethod(lambda conn, job_id, *, before: 0))
    with pytest.raises((AssertionError, BaseExceptionGroup)) as caught:
        test_c4_5_no_unchanged_unpinned_job_is_failed_by_the_one_dead_lane_it_meets(tmp_path_factory=tmp_path_factory)
    assertions_only(caught)
