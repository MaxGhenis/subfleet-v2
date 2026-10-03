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
whose salvage found its end tree equal to its start snapshot with HEAD where it
was), moves on to the next candidate as a `limited` one does, and `max_attempts`
does not count it. A pinned job, and a job whose workspace changed, end as before.
These drive the daemon's own finalization and admission in-process, no provider run.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

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


class LaneRefuses(FakeAdapter):
    """The incident's verdict (C-9.3): the CLI's organisation block, no model answering."""

    def classify(self, attempt_dir, launch, exit_info):
        return Outcome(OutcomeClass.AUTH_DEAD, BLOCK, evidence={"rc": exit_info.rc, "model_answered": False})


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


def test_c4_5_a_job_moves_on_from_at_most_every_lane_and_then_waits_never_failing(state_daemon):
    """Each lane fault disables its lane (C-23.44), so a job moves on from at most as
    many as there are; with none left it waits, held for good (C-11.8), not failed."""
    daemon, harness = state_daemon
    for lane_id in ("codex-2", "codex-3"):
        add_lane(daemon, harness, lane_id)
    job_id, attempt, _ = reserve(daemon, harness)
    for _ in range(3):
        finish(daemon, attempt, LaneRefuses)
        daemon._admit()
        attempt = attempts(daemon, job_id)[-1]
    assert [row["lane_id"] for row in attempts(daemon, job_id)] == ["codex-1", "codex-2", "codex-3"]
    assert all(not lane.enabled for lane in daemon.store.list_lanes())
    job = daemon.store.get_job(job_id)
    assert job["state"] == "waiting" and job["rc"] is None
    assert daemon._holds[job_id].get("for_good") == ["disabled"]
    assert daemon.store.list_notices() == []


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


# --- the property: an unchanged unpinned job never fails for a dead lane while another is open ----

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


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(lanes=st.integers(1, 4), dead=st.lists(st.booleans(), min_size=4, max_size=4), jobs=JOBS,
       writable=st.sampled_from([None, "unchanged", "changed"]), prove=st.sampled_from([None, 900]),
       order=st.randoms(use_true_random=False))
def test_c4_5_no_unchanged_unpinned_job_fails_for_auth_dead_while_another_lane_is_open(
        tmp_path_factory, lanes, dead, jobs, writable, prove, order):
    """For any fleet of 1 to 4 lanes, some of them dead (every attempt there `auth-dead`
    before a model answers), any jobs (lane-pinned or not, read-only, and at most one
    writable job in the one checkout whose tree its attempts leave unchanged or change),
    with the pilot hold on or off (C-6.14), and attempts ending in any order, run until
    nothing more can be placed:

    - no unpinned job whose workspace is unchanged ends `failed` because of `auth-dead`
      while another enabled lane exists: with a live lane it succeeds, and with none it
      waits, never failed;
    - each lane fault disabled its lane, and no job moved on from more of them than there
      were lanes; `max_attempts` counted none of them;
    - a pinned job never runs elsewhere, and one pinned to a dead lane that it reached
      ends `failed` rc 5; a writable job whose tree changed ends `failed` rc 5 on a dead lane.
    """
    root = tmp_path_factory.mktemp("lane-fault")
    dead_lanes = {f"codex-{n}" for n in range(1, lanes + 1) if dead[n - 1]}
    with fleet(root / "state", lanes) as (service, harness):
        service.policy["admission"]["prove_idle_s"] = prove
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
            service._admit()
            live = service.store.query("SELECT * FROM attempts WHERE state='reserved'")
            if not live:
                break
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
        alive = {f"codex-{n}" for n in range(1, lanes + 1)} - dead_lanes
        assert enabled == alive | (dead_lanes - {row["lane_id"] for row in service.store.list_attempts()})
        for job_id, pin, workspace in submitted:
            job = service.store.get_job(job_id)
            rows = service.store.list_attempts(job_id)
            faults = [row for row in rows if json.loads(row["evidence_json"] or "{}").get("lane_fault")]
            assert all(row["lane_id"] in dead_lanes and row["outcome_class"] == "auth-dead" for row in faults)
            assert len(faults) <= lanes and len({row["lane_id"] for row in faults}) == len(faults)
            assert len(rows) - len(faults) <= job["max_attempts"]
            if pin is None and workspace != "changed":
                # The property: never failed for auth-dead while another lane was open.
                assert not (job["state"] == "failed" and rows and rows[-1]["outcome_class"] == "auth-dead")
                assert job["state"] == ("succeeded" if alive else "waiting"), (job_id, job["state"], rows)
            elif pin is not None:
                assert {row["lane_id"] for row in rows} <= {pin}
                if pin in dead_lanes and rows:
                    assert (job["state"], job["rc"]) == ("failed", 5)
            elif rows and rows[0]["lane_id"] in dead_lanes:
                assert (job["state"], job["rc"]) == ("failed", 5) and not faults
