"""D-TREE-THRASH1: a fresh detached tree is cut for placement, not for waiting.

The differential oracle is the frozen actual `_admit_pass` at a8624a489,
hash-checked and compiled into the current daemon's namespace. It exercises the
old scheduling code without requiring Git history. Every daemon and repository
is isolated.
"""
from __future__ import annotations

import ast
import functools
import hashlib
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import types

import pytest
from hypothesis import HealthCheck, event, given, settings, strategies as st

from subfleet import daemon as daemon_module, folders, scheduler
from subfleet.adapters.base import AdapterError
from subfleet.contracts import ClockSource, Closure, ClosureReason
from subfleet.daemon import Daemon, utcnow
from tests.fake.conftest import Harness
from tests.fake.test_admission_latency import add_codex_lanes, measure
from tests.fake.test_state_contract import state_daemon  # noqa: F401
from tests.fake.test_unrun_worktrees import repo, submit as submit_writer, swept  # noqa: F401
from tests.unit.test_salvage import git

def submit(service, harness, repository, **changes):
    return submit_writer(service, harness, repository, caller_session=None, **changes)


BASE = "a8624a4899a2"
BASE_METHOD_SHA256 = "a894326e79493fd6dc79b8fea75fd392add53d8283f123f4e49e328ee4afbcf5"
FUTURE = "2099-12-31T00:00:00Z"
WAITING_METADATA = scheduler.waiting_metadata


@functools.lru_cache
def base_admit_pass():
    """Load the byte-for-byte base method, including in shallow/archive checkouts."""
    source = (Path(__file__).parents[1] / "fixtures" / f"tree_thrash_{BASE}.py").read_bytes()
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Daemon")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_admit_pass")
    original_lines = b"".join(source.splitlines(keepends=True)[method.lineno - 1:method.end_lineno])
    assert hashlib.sha256(original_lines).hexdigest() == BASE_METHOD_SHA256
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    namespace = dict(vars(daemon_module))
    exec(compile(module, f"{BASE}/subfleet/daemon.py", "exec"), namespace)
    return namespace["_admit_pass"]


@pytest.fixture
def immutable_source(repo, monkeypatch):  # noqa: F811
    """Cache submit's inspection of a source that a scheduling scenario never edits.

    A submit normally reads HEAD, the top, common directory and prefix with four
    Git processes. This fixture's simple repository has a loose task/example
    branch and no redirects; read that exact fixture input once instead. Only
    this source path is cached: allocated worktrees, their snapshots, registration
    and the safety sweep remain real. The moving-HEAD examples/properties
    deliberately do not request this fixture.
    """
    source = os.path.realpath(repo)
    dotgit = repo / ".git"
    assert dotgit.is_dir() and (dotgit / "HEAD").read_text().strip() == "ref: refs/heads/task/example"
    assert not (dotgit / "commondir").exists()
    head = (dotgit / "refs" / "heads" / "task" / "example").read_text().strip()
    assert len(head) in (40, 64) and all(character in "0123456789abcdef" for character in head)
    answers = {"git_head": head, "git_toplevel": source,
               "git_common_dir": os.path.realpath(dotgit), "_git_prefix": "."}
    for name, answer in answers.items():
        original = getattr(daemon_module, name)

        @functools.wraps(original)
        def cached(path, *args, _original=original, _answer=answer, **kwargs):
            if os.path.realpath(path) != source:
                return _original(path, *args, **kwargs)
            return _answer

        monkeypatch.setattr(daemon_module, name, cached)


@pytest.fixture
def fleet_factory(state_daemon, tmp_path, monkeypatch):  # noqa: F811
    """Reuse the state fixture's process stubs; each scenario gets a fresh store."""
    services = []

    def make(*, base=False, slots=2, cap=None, lanes=1, synthetic=False):
        # Hypothesis reuses this fixture: do not accumulate daemon worker pools.
        for prior, prior_harness in services:
            if not prior._closed:
                prior.close()
                prior_harness.check_notices()
        root = tmp_path / f"fleet-{len(services)}"
        root.mkdir()
        harness = Harness(root)
        service = Daemon(root)
        services.append((service, harness))
        service._schedule = lambda *args, **kwargs: None
        # The loaded host can take over 60 s for a tiny Git read. These fixture
        # caps spare setup from that noise; failure tests inject their own errors.
        service.policy["caps"].update(max_active_attempts=cap, max_in_flight_per_lane=slots,
                                      reading_ttl_s=3600, workspace_git_timeout_s=300,
                                      worktree_add_timeout_s=300)
        if lanes > 1:
            add_codex_lanes(service, *(f"codex-{i}" for i in range(2, lanes + 1)))
        for i in range(1, lanes + 1):
            measure(service, f"codex-{i}")
        if base:
            oracle = base_admit_pass()
            # The real daemon globals include this scenario's clock patches.
            method = types.FunctionType(oracle.__code__, vars(daemon_module),
                                        oracle.__name__, oracle.__defaults__, oracle.__closure__)
            method.__kwdefaults__ = oracle.__kwdefaults__
            service._admit_pass = types.MethodType(method, service)
        cuts = []
        workspace = service._workspace

        def cut(job):
            path = service.root / "worktrees" / job["job_id"]
            fresh = not path.exists()
            if synthetic:
                path.mkdir(parents=True, exist_ok=True)
                result = str(path), job["workdir_head"], None, []
            else:
                result = workspace(job)
            if fresh:
                cuts.append(job["job_id"])
            return result

        service._workspace = cut
        service._launch = lambda *args: pytest.fail("a state scenario never launches a process")
        return service, harness, cuts

    try:
        yield make
    finally:
        for service, harness in services:
            service.close()
            harness.check_notices()


def close(service, lane="codex-1"):
    service.store.put_closure(Closure(lane, "account", FUTURE, ClosureReason.OPERATOR_HOLD,
                                      ClockSource.REPORTED, "tree-thrash-test"))


def open_lanes(service):
    with service.store.transaction("test.release") as tx:
        tx.execute("UPDATE closures SET released_at=? WHERE released_at IS NULL", (utcnow(),))
        tx.execute("UPDATE jobs SET next_check_at=NULL WHERE state IN ('queued','waiting')")


def due_waiters(service):
    with service.store.transaction("test.due_waiters") as tx:
        tx.execute("UPDATE jobs SET next_check_at=NULL WHERE state='waiting'")


def finish(service):
    """End the reserved batch and release exactly its holders, without launching it."""
    with service.store.transaction("test.batch_ended") as tx:
        for row in tx.execute("SELECT attempt_id,job_id FROM attempts WHERE state='reserved'").fetchall():
            tx.execute("DELETE FROM leases WHERE holder IN (?,?)", (row[0], row[1]))
            tx.execute("UPDATE attempts SET state='succeeded' WHERE attempt_id=?", (row[0],))
            tx.execute("UPDATE jobs SET state='succeeded' WHERE job_id=?", (row[1],))


def reservations(service, jobs):
    index = {job: i for i, job in enumerate(jobs)}
    return [(index[row["job_id"]], row["lane_id"]) for row in service.store.query(
        "SELECT job_id,lane_id FROM attempts ORDER BY rowid")]


def measured_shape(make, repository, *, base):
    service, harness, cuts = make(base=base, slots=None)
    jobs = [submit(service, harness, repository) for _ in range(10)]
    reserve = service._disk.reserve
    batch = [0]

    def reserved(*args, **kwargs):
        reserve(*args, **kwargs)
        batch[0] += 1
        if batch[0] == 2:
            close(service)

    service._disk.reserve = reserved
    given_back = 0
    for round_no in range(20):
        batch[0] = 0
        open_lanes(service)
        before = len(service.store.list_attempts())
        service._admit()
        assert len(service.store.list_attempts()) - before == 2
        # A fresh pass observes the controller's hold and C-6.20 sweeps leftovers.
        due_waiters(service)
        service._admit()
        given_back += swept(service)
        finish(service)
        if round_no < 19:
            jobs.extend(submit(service, harness, repository) for _ in range(2))
    # This script closes after reservation, never in evaluation -> reservation.
    lane_closed_between_evaluation_and_reservation = 0
    return {"cuts": len(cuts), "reservations": len(service.store.list_attempts()),
            "give_backs": given_back, "closure_race_cuts": lane_closed_between_evaluation_and_reservation,
            "trace": reservations(service, jobs)}


def test_measured_shape_twenty_two_start_rounds_cut_only_the_forty_placed_trees(
        fleet_factory, repo, monkeypatch, immutable_source):  # noqa: F811
    """I1/I2: 10 waiters and 20 controller releases; no lane slot cap.

    The source is immutable and clean throughout these rounds. Cache its HEAD
    and clean baseline reads to keep this scheduling test affordable under load;
    every worktree add, registration, and safe give-back is real. Separate I3
    examples/properties exercise the complete Git snapshot with moving HEADs.
    """
    head = git(repo, "rev-parse", "HEAD").strip()
    baseline = git(repo, "rev-parse", "HEAD^{tree}").strip()
    with monkeypatch.context() as fixed_clean_source:
        fixed_clean_source.setattr(daemon_module, "git_head", lambda *args, **kwargs: head)
        fixed_clean_source.setattr(daemon_module, "working_tree", lambda *args, **kwargs: baseline)
        old = measured_shape(fleet_factory, repo, base=True)
        new = measured_shape(fleet_factory, repo, base=False)
    assert old == {"cuts": 200, "reservations": 40, "give_backs": 160,
                   "closure_race_cuts": 0, "trace": old["trace"]}
    assert new["cuts"] == new["reservations"] + new["closure_race_cuts"] == 40
    assert new["give_backs"] == 0
    assert new["trace"] == old["trace"]


def test_a_hold_after_the_second_reservation_stops_later_cuts_in_this_pass(
        fleet_factory, repo, immutable_source):  # noqa: F811
    service, harness, cuts = fleet_factory(slots=None)
    jobs = [submit(service, harness, repo) for _ in range(10)]
    reserve = service._disk.reserve

    def reserved(*args, **kwargs):
        reserve(*args, **kwargs)
        if len(service.store.list_attempts()) == 2:
            close(service)

    service._disk.reserve = reserved
    service._admit()
    assert cuts == jobs[:2]
    assert reservations(service, jobs) == [(0, "codex-1"), (1, "codex-1")]
    assert all(service._holds[job]["reason"] == "closed:account" for job in jobs[2:])


def test_a_lane_closed_after_evaluation_is_rechecked_before_cutting_a_tree(
        fleet_factory, repo, immutable_source):  # noqa: F811
    """I1/I4: even the first look cuts nothing if its first check sees a hold."""
    service, harness, cuts = fleet_factory(slots=None, synthetic=True)
    jobs = [submit(service, harness, repo) for _ in range(3)]
    pick = service._pick
    evaluated = []

    def close_after_evaluation(row, **options):
        decision = pick(row, **options)
        evaluated.append(row["job_id"])
        if len(evaluated) == 1:
            assert decision.chosen_lane == "codex-1"
            close(service)
        return decision

    service._pick = close_after_evaluation
    service._admit()
    assert evaluated[0] == jobs[0]
    assert cuts == [] and not service.store.list_attempts()
    assert all(service._holds[job]["reason"] == "closed:account" for job in jobs)
    assert all(not (service.root / "worktrees" / job).exists() for job in jobs)


def test_the_one_cut_whose_lane_closes_between_evaluation_and_reservation_is_given_back(
        fleet_factory, repo, immutable_source):  # noqa: F811
    """I1's labelled exception is one racing look; the next looks cut nothing."""
    service, harness, cuts = fleet_factory()
    jobs = [submit(service, harness, repo) for _ in range(5)]
    workspace = service._workspace
    raced = []

    def close_after_cut(job):
        answer = workspace(job)
        close(service)
        raced.append(job["job_id"])
        return answer

    service._workspace = close_after_cut
    service._admit()
    lane_closed_between_evaluation_and_reservation = len(raced)
    assert lane_closed_between_evaluation_and_reservation == 1
    assert len(cuts) == len(service.store.list_attempts()) + lane_closed_between_evaluation_and_reservation
    assert not service.store.list_attempts()
    assert service._holds[jobs[0]].get("unprepared")
    assert swept(service) == 1
    assert all(not (service.root / "worktrees" / job).exists() for job in jobs)
    events = service.store.query("SELECT data_json FROM events WHERE kind='job.worktree_given_back'")
    assert len(events) == 1 and json.loads(events[0]["data_json"])["why"] == "no lane"


def test_a_reopened_lane_before_the_sweep_keeps_its_unused_tree(
        fleet_factory, repo, immutable_source):  # noqa: F811
    """A stale `unprepared` hold cannot discard a tree its lane can now use."""
    service, harness, cuts = fleet_factory()
    job = submit(service, harness, repo)
    workspace = service._workspace

    def close_while_preparing(row):
        result = workspace(row)
        close(service)
        return result

    service._workspace = close_while_preparing
    service._admit()
    tree = service.root / "worktrees" / job
    assert cuts == [job] and (tree / ".git").is_file()
    assert not service.store.list_attempts(job)
    assert service._holds[job].get("unprepared")
    open_lanes(service)
    # No intervening admission pass may clear the stale hold for the sweep.
    assert service._holds[job].get("unprepared")
    assert swept(service) == 0
    assert tree.is_dir() and (tree / ".git").is_file()
    assert not service.store.query("SELECT 1 FROM events WHERE job_id=? AND kind='job.worktree_given_back'", (job,))


def test_the_contract_no_git_for_a_waiter_covers_a_lease_arriving_during_preparation(
        fleet_factory, repo, immutable_source):  # noqa: F811
    """I4 covers provisional rejection: a later lease can leave a prepared tree."""
    service, harness, cuts = fleet_factory()
    output = service.root / "answer.md"
    job = submit(service, harness, repo, out_path=str(output))
    workspace = service._workspace

    def lease_while_preparing(row):
        result = workspace(row)
        assert (Path(result[0]) / ".git").is_file()
        assert service.store.acquire_lease(f"out:{output}", "arrived-during-git")
        return result

    service._workspace = lease_while_preparing
    service._admit()
    assert cuts == [job] and not service.store.list_attempts(job)
    assert (service.root / "worktrees" / job / ".git").is_file()
    assert service._holds[job]["reason"] == "lease-held"
    assert service._holds[job]["leases"] == [f"out:{folders.identity(str(output))}"]
    row = service.store.get_job(job)
    assert (row["state"], row["wait_reason"], row["worktree"]) == ("waiting", "capacity", None)


def test_i1_two_pinned_lanes_can_each_lose_a_prepared_tree_in_one_pass(
        fleet_factory, repo, immutable_source):  # noqa: F811
    """I1 counts racing looks: two pinned lanes can each close during preparation."""
    service, harness, cuts = fleet_factory(lanes=2, slots=None, synthetic=True)
    jobs = [submit(service, harness, repo, pinned_lane=f"codex-{index}") for index in (1, 2)]
    workspace = service._workspace
    racing_looks = []

    def close_while_preparing(row):
        result = workspace(row)
        close(service, row["pinned_lane"])
        racing_looks.append(row["job_id"])
        return result

    service._workspace = close_while_preparing
    service._admit()
    assert cuts == racing_looks == jobs
    assert not service.store.list_attempts()
    assert len(cuts) == len(service.store.list_attempts()) + len(racing_looks) == 2
    assert all(cuts.count(job) == 1 and service._holds[job].get("unprepared") for job in jobs)


def test_reopening_the_original_lane_during_preparation_reuses_its_original_evaluation(
        fleet_factory, repo, immutable_source):  # noqa: F811
    """The basis stays paired with its original decision across both checks (I2)."""
    service, harness, cuts = fleet_factory(lanes=2, slots=None, synthetic=True)
    job = submit(service, harness, repo)
    evaluations = []
    pick = service._pick

    def picked(row, **options):
        decision = pick(row, **options)
        evaluations.append(decision.chosen_lane)
        # The evaluation's read snapshot has ended: commit the controller's
        # closure off the writer lock, before its first reservation check.
        if len(evaluations) == 1:
            close(service, "codex-1")
        return decision

    service._pick = picked
    stands = service._route_stands
    checks = []

    def checked(basis, decision):
        checks.append(True)
        return stands(basis, decision)

    service._route_stands = checked
    workspace = service._workspace

    def reopen_while_preparing(row):
        result = workspace(row)
        open_lanes(service)
        return result

    service._workspace = reopen_while_preparing
    service._admit()
    (attempt,) = service.store.list_attempts(job)
    assert len(checks) == 2 and evaluations == ["codex-1"]
    assert cuts == [job] and attempt["lane_id"] == "codex-1"


def test_a_closure_race_with_a_desktop_lane_never_refreshes_the_registry_under_the_store_lock(
        fleet_factory, repo, immutable_source):  # noqa: F811
    service, harness, cuts = fleet_factory(lanes=2, synthetic=True)
    service.store.update_lane("codex-2", desktop=1)
    close(service, "codex-2")
    job = submit(service, harness, repo)
    desktop = service._desktop_in_use
    refreshed = []

    def outside_the_lock():
        assert not service.store._holds_writer(), "registry refresh under the store write lock"
        refreshed.append(True)
        return desktop()

    service._desktop_in_use = outside_the_lock
    workspace = service._workspace

    def close_after_cut(row):
        result = workspace(row)
        close(service)
        return result

    service._workspace = close_after_cut
    service._admit()
    assert refreshed and cuts == [job]
    assert not service.store.list_attempts(job)
    assert service._holds[job].get("unprepared")


@pytest.mark.parametrize("reason", ["no-slot", "lease", "disk", "retention"])
def test_a_fresh_waiter_runs_no_git_and_writes_no_tree(
        fleet_factory, repo, monkeypatch, immutable_source, reason):  # noqa: F811
    """I4/I5: capacity, a path lease, the door, and retention precede tree work."""
    service, harness, cuts = fleet_factory(slots=1, synthetic=True)
    extra = {}
    if reason == "no-slot":
        running = submit(service, harness, repo)
        service._admit()
        assert service.store.list_attempts(running)
        cuts.clear()
    elif reason == "lease":
        output = service.root / "answer.md"
        extra["out_path"] = str(output)
    elif reason == "disk":
        from tests.fake.test_unrun_worktrees import disk
        disk(service, monkeypatch, 39)
    elif reason == "retention":
        service.store.acquire_lease(folders.exclusive_key(str(repo)), "retention:test")
    job = submit(service, harness, repo, **extra)
    if reason == "lease":
        service.store.acquire_lease(f"out:{output}", "another-job")
    run = subprocess.run
    calls = []

    def no_git(command, *args, **kwargs):
        if Path(str(command[0])).name == "git":
            calls.append(command)
            pytest.fail(f"waiting fresh job ran git: {command}")
        return run(command, *args, **kwargs)

    # Caching submit's immutable source inspection must not hide an accidental
    # Git inspection in the waiting look itself, even before a subprocess runs.
    for name in ("git_head", "git_toplevel", "git_common_dir", "_git_prefix"):
        monkeypatch.setattr(daemon_module, name, lambda *args, _name=name, **kwargs: pytest.fail(
            f"waiting fresh job invoked {_name}"))
    monkeypatch.setattr(subprocess, "run", no_git)
    service._admit()
    assert not cuts and not calls and not service.store.list_attempts(job)
    assert not (service.root / "worktrees" / job).exists()
    assert service._holds[job]["reason"] == {
        "no-slot": "no-slot", "lease": "lease-held", "disk": "disk", "retention": "lease-held"}[reason]


def test_the_placement_reuses_one_evaluation_and_pins_the_submitted_head(fleet_factory, repo):  # noqa: F811
    """I3: moving the submitter's HEAD never changes the tree or its baseline."""
    service, harness, cuts = fleet_factory()
    job = submit(service, harness, repo)
    submitted_head = service.store.get_job(job)["workdir_head"]
    (repo / "tracked.txt").write_text("later commit\n")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "caller moved HEAD while waiting")
    evaluations = []
    pick = service._pick

    def picked(row, **kwargs):
        evaluations.append(row["job_id"])
        return pick(row, **kwargs)

    service._pick = picked
    slot_lease = service._slot_lease
    slot_selections = []

    def select_prepared_slot(tx, lane_id, turn):
        # Provisional checks verify capacity without selecting a slot number.
        # Only the final reservation, after the real tree exists, searches it.
        assert cuts == [job] and (service.root / "worktrees" / job / ".git").is_file()
        slot_selections.append((lane_id, turn))
        return slot_lease(tx, lane_id, turn)

    service._slot_lease = select_prepared_slot
    service._admit()
    (attempt,) = service.store.list_attempts(job)
    tree = service.store.get_job(job)["worktree"]
    assert cuts == [job] and evaluations == [job]
    assert slot_selections == [("codex-1", False)]
    assert git(tree, "rev-parse", "HEAD").strip() == submitted_head
    assert (Path(tree) / "tracked.txt").read_text() == "baseline\n"
    assert attempt["baseline_tree"] == git(repo, "rev-parse", f"{submitted_head}^{{tree}}").strip()
    evidence = json.loads(attempt["evidence_json"])
    assert evidence["baseline_commit"] == submitted_head and evidence["run_folder"] == tree
    # The provisional placement is rolled back before Git: only the final
    # reservation's audit event and its three leases may survive.
    assert len(service.store.query("SELECT 1 FROM events WHERE kind='attempt.reserved' AND job_id=?", (job,))) == 1
    leased = service.store.query("SELECT lease_key,holder FROM leases WHERE holder IN (?,?)",
                                 (job, attempt["attempt_id"]))
    assert len(leased) == 3
    assert {row["lease_key"] for row in leased if row["holder"] == job} == {
        folders.exclusive_key(tree), folders.turn_key(tree, job, writable=False)}


@pytest.mark.parametrize("transient", [False, True])
def test_a_failing_workspace_reaches_the_job_when_capacity_first_allows_placement(
        fleet_factory, repo, immutable_source, transient):  # noqa: F811
    """I6: workspace errors are postponed until placement, then retain C-6.8."""
    service, harness, cuts = fleet_factory(slots=1, synthetic=True)
    occupant = submit(service, harness, repo)
    service._admit()
    job = submit(service, harness, repo)
    calls = []

    def fail(row):
        calls.append(row["job_id"])
        if transient:
            raise subprocess.TimeoutExpired("git worktree add", 1)
        raise AdapterError("cannot prepare tree", code=7, reason="workspace-prepare")

    service._workspace = fail
    service._admit()
    assert calls == [] and service.store.get_job(job)["wait_reason"] == "capacity"
    finish(service)
    service._admit()                    # freed lease brings the clock forward
    assert calls == [job]
    row = service.store.get_job(job)
    if transient:
        assert (row["state"], row["wait_reason"]) == ("waiting", "workspace")
        assert service._workspace_deferrals[job] == 1
    else:
        assert (row["state"], row["rc"]) == ("failed", 7)
    assert not service.store.list_attempts(job)


SCENARIOS = st.tuples(st.integers(1, 3), st.integers(0, 3), st.one_of(st.none(), st.integers(1, 5)),
                     st.lists(st.tuples(st.sampled_from(["astra", "terra"]),
                                       st.sampled_from([None, "codex-1", "codex-2"]), st.booleans()),
                              min_size=1, max_size=7))


@settings(max_examples=24, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(scenario=SCENARIOS)
def test_i2_i5_generated_reservations_holds_and_capacity_clocks_equal_the_base(
        fleet_factory, repo, monkeypatch, immutable_source, scenario):  # noqa: F811
    """Static lanes: exact base ordering, FIFO, path leases, clocks and demand."""
    slots, closed_bits, cap, specifications = scenario
    frozen = "2026-11-06T09:00:00Z"
    instant = datetime.fromisoformat(frozen.replace("Z", "+00:00"))
    monkeypatch.setattr(daemon_module, "utcnow", lambda: frozen)
    monkeypatch.setattr(daemon_module, "after", lambda seconds: (instant + timedelta(seconds=seconds)).isoformat(
        timespec="seconds").replace("+00:00", "Z"))
    monkeypatch.setattr(scheduler, "waiting_metadata", lambda decision, **kwargs: WAITING_METADATA(
        decision, now=instant, **kwargs))
    results = []
    for base in (True, False):
        service, harness, cuts = fleet_factory(base=base, slots=slots, cap=cap, lanes=2, synthetic=True)
        # Bit 0/1 chooses closed lanes; the generated pins may name either one.
        for i in (1, 2):
            if closed_bits & (1 << (i - 1)):
                close(service, f"codex-{i}")
        jobs = []
        for i, (model, pin, leased) in enumerate(specifications):
            output = service.root / f"contested-{i}.md"
            jobs.append(submit(service, harness, repo, pinned_model=model, pinned_lane=pin,
                               **({"out_path": str(output)} if leased else {})))
            if leased:
                service.store.acquire_lease(f"out:{output}", "external-owner")
        service._admit()
        # A second due look checks C-6.10's unchanged-verdict backoff too.
        due_waiters(service)
        service._admit()
        ids = {job: f"job-{i}" for i, job in enumerate(jobs)}

        def normalized(value):
            encoded = json.dumps(value, sort_keys=True).replace(str(service.root), "<root>")
            encoded = encoded.replace(folders.fold(str(service.root)), "<root>")
            for job in sorted(ids, key=len, reverse=True):
                encoded = encoded.replace(job, ids[job])
            return json.loads(encoded)

        results.append({"trace": reservations(service, jobs),
                        "holds": normalized(service._holds),
                        "clocks": [(service.store.get_job(job)["wait_reason"],
                                    service.store.get_job(job)["next_check_at"]) for job in jobs],
                        "waits": normalized(service._capacity_waits),
                        "demand": [ids[row["job_id"]] for row in service._reset_demand()]})
    assert results[0] == results[1]


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(jobs_per_round=st.integers(1, 7), changes=st.lists(st.tuples(st.booleans(), st.integers(1, 3),
                                                               st.booleans()), min_size=1, max_size=7))
def test_i1_generated_lane_changes_bound_cuts_by_reservations_and_the_labelled_race(
        fleet_factory, repo, monkeypatch, immutable_source, jobs_per_round, changes):  # noqa: F811
    """Arbitrary opens/holds and one forced closure race per pass: I1 and I4."""
    service, harness, cuts = fleet_factory(slots=3, synthetic=True)
    race_job = [None]
    workspace = service._workspace
    labelled = []

    def racing_workspace(job):
        fresh = not os.path.lexists(service.root / "worktrees" / job["job_id"])
        result = workspace(job)
        if fresh and race_job[0] == job["job_id"]:
            close(service)
            labelled.append(job["job_id"])
        return result

    service._workspace = racing_workspace
    total_reserved = total_races = 0
    for opened, slots, race in changes:
        finish(service)
        open_lanes(service)
        if not opened:
            close(service)
        service.policy["caps"]["max_in_flight_per_lane"] = slots
        jobs = [submit(service, harness, repo) for _ in range(jobs_per_round)]
        waiting = service.store.query("SELECT job_id FROM jobs WHERE state IN ('queued','waiting') ORDER BY created_at,rowid")
        race_job[0] = waiting[0]["job_id"] if race and opened else None
        before_cuts, before_reserved, before_races = len(cuts), len(service.store.list_attempts()), len(labelled)
        run = subprocess.run

        def no_git(command, *args, **kwargs):
            if Path(str(command[0])).name == "git":
                pytest.fail(f"synthetic admission unexpectedly ran git: {command}")
            return run(command, *args, **kwargs)

        # Submission above deliberately uses real Git. These looks' synthetic
        # placements require none, so this also checks I4 on every waiter.
        with monkeypatch.context() as guard:
            guard.setattr(subprocess, "run", no_git)
            service._admit()
        reserved = len(service.store.list_attempts()) - before_reserved
        lane_closed_between_evaluation_and_reservation = len(labelled) - before_races
        assert lane_closed_between_evaluation_and_reservation <= 1
        assert len(cuts) - before_cuts <= reserved + lane_closed_between_evaluation_and_reservation
        total_reserved += reserved
        total_races += lane_closed_between_evaluation_and_reservation
    event(f"labelled closure races: {total_races}; reservations: {total_reserved}")
    assert len(cuts) <= total_reserved + total_races


@settings(max_examples=8, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(moves=st.integers(0, 3))
def test_i3_generated_head_movements_keep_the_submitted_tree_and_baseline(fleet_factory, repo, moves):  # noqa: F811
    service, harness, cuts = fleet_factory()
    job = submit(service, harness, repo)
    submitted = service.store.get_job(job)["workdir_head"]
    for index in range(moves):
        (repo / "tracked.txt").write_text(f"{job} caller movement {index}\n")
        git(repo, "add", "tracked.txt")
        git(repo, "commit", "-m", f"caller movement {index}")
    service._admit()
    (attempt,) = service.store.list_attempts(job)
    tree = service.store.get_job(job)["worktree"]
    assert cuts == [job] and Path(tree).is_dir()
    assert git(tree, "rev-parse", "HEAD").strip() == submitted
    assert attempt["baseline_tree"] == git(repo, "rev-parse", f"{submitted}^{{tree}}").strip()
    assert json.loads(attempt["evidence_json"])["baseline_commit"] == submitted


@settings(max_examples=6, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(transient=st.booleans(), retry_limit=st.integers(1, 3))
def test_i6_generated_workspace_failures_keep_the_original_retry_limit(
        fleet_factory, repo, immutable_source, transient, retry_limit):  # noqa: F811
    service, harness, _ = fleet_factory(slots=1, synthetic=True)
    service.policy["caps"]["workspace_retry_max"] = retry_limit
    submit(service, harness, repo)
    service._admit()
    job = submit(service, harness, repo)
    calls = []

    def broken(row):
        calls.append(row["job_id"])
        if transient:
            raise subprocess.TimeoutExpired("git", 1)
        raise OSError("permanent workspace error")

    service._workspace = broken
    service._admit()
    assert calls == []
    finish(service)
    for _ in range(retry_limit + 1 if transient else 1):
        due_waiters(service)
        service._admit()
    row = service.store.get_job(job)
    assert row["state"] == "failed" and not service.store.list_attempts(job)
    assert len(calls) == (retry_limit + 1 if transient else 1)
    event(f"workspace failure transient={transient}, retry_limit={retry_limit}")
