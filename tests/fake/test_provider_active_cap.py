"""C-6.4a: provider admission across lanes and both pools, using the fake daemon."""

from pathlib import Path
import tempfile

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import cli, scheduler
from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner
from subfleet.daemon import after
from tests.fake.test_admission_latency import (add_codex_lanes, fleet_daemon, measure, submit, submit_turn)
from tests.fake.test_admission_liveness import _end, _live
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401


def prepare(service, patch, cap=2):
    service.policy["providers"] = {"codex": {"max_active_attempts": cap}}
    service.policy["caps"]["reading_ttl_s"] = 3600
    add_codex_lanes(service, "codex-2", "codex-3")
    ref = str(service.root / "claude-home")
    service.store.put_lane(Lane("claude-1", "claude", "claude:fake", Credential("claude", ref, "home"),
                                ref, LaneOwner.V2, False))
    for lane in ("codex-1", "codex-2", "codex-3", "claude-1"):
        measure(service, lane)
    patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None, []))
    patch.setattr(service, "_desktop_in_use", lambda: False)


@pytest.fixture
def fleet(routing_state, monkeypatch):
    service, harness = routing_state
    prepare(service, monkeypatch)
    return service, harness


def live_count(service, provider="codex"):
    return service.store.one("SELECT count(*) n FROM attempts a JOIN lanes l USING(lane_id) "
                             "WHERE a.state IN ('reserved','starting','running','finalizing') AND l.provider=?",
                             (provider,))["n"]


def assert_unleased(service, job):
    assert not service.store.list_attempts(job)
    assert not service.store.list_leases(job)
    assert not service.store.query("SELECT 1 FROM leases WHERE holder LIKE ?", (job + "/%",))


@pytest.mark.parametrize("cap", [1, 2, 3])
def test_n_plus_one_jobs_hold_and_resume_in_fifo(fleet, cap):
    service, harness = fleet
    service.policy["providers"]["codex"]["max_active_attempts"] = cap
    jobs = [submit(service, harness, pinned_lane=f"codex-{n % 3 + 1}",
                   pinned_model="astra" if n % 2 == 0 else "terra") for n in range(cap + 2)]
    claude = submit(service, harness, pinned_model="opus")
    service._admit()
    assert [_live(service, job) for job in jobs] == [True] * cap + [False, False]
    assert live_count(service) == cap and _live(service, claude)
    assert service._holds[jobs[cap]]["reason"] == "provider-cap"
    assert service._holds[jobs[cap + 1]]["behind"] == jobs[cap]
    for job in jobs[cap:]:
        assert_unleased(service, job)
        why = service.dispatch("why", {"job_id": job})["text"]
        assert f"codex: {cap} of {cap} running" in why
        assert "providers.codex.max_active_attempts" in why
    status = service.dispatch("daemon.status", {})
    for text in (status["status"], cli.format_status(status)):
        assert f"codex: {cap} of {cap} running (providers.codex.max_active_attempts)" in text
    _end(service, jobs[0])
    service._admit()
    assert _live(service, jobs[cap]) and not _live(service, jobs[cap + 1])
    assert live_count(service) == cap
    _end(service, jobs[1])
    service._admit()
    assert _live(service, jobs[cap + 1])


@pytest.mark.parametrize("first_kind", ["turn", "dispatch"])
@pytest.mark.parametrize("state", ["reserved", "starting", "running", "finalizing"])
def test_turns_count_and_are_held_by_detached_attempts(fleet, first_kind, state):
    service, harness = fleet
    service.policy["providers"]["codex"]["max_active_attempts"] = 1
    first = submit_turn(service, harness, 1) if first_kind == "turn" else submit(service, harness)
    service._admit()
    with service.store.transaction("test.state") as tx:
        tx.execute("UPDATE attempts SET state=? WHERE job_id=?", (state, first))
    waiting = submit(service, harness) if first_kind == "turn" else submit_turn(service, harness, 2)
    service._admit()
    assert_unleased(service, waiting)
    assert service._holds[waiting]["reason"] == "provider-cap"
    assert "codex: 1 of 1 running" in service.dispatch("why", {"job_id": waiting})["text"]
    if first_kind == "dispatch":
        from subfleet.conversations import waits
        assert "codex: 1 of 1 running" in waits.hold_reason(service._holds[waiting], describe=str, who=str)
    _end(service, first)
    service._admit()
    assert _live(service, waiting) and live_count(service) == 1


def test_turn_priority_when_a_provider_slot_frees(fleet):
    service, harness = fleet
    service.policy["providers"]["codex"]["max_active_attempts"] = 1
    active = submit(service, harness)
    service._admit()
    detached = submit(service, harness)
    turn = submit_turn(service, harness, 1)
    service._admit()
    _end(service, active)
    service._admit()
    assert _live(service, turn)
    assert_unleased(service, detached)


@pytest.mark.parametrize("early_kind", ["turn", "dispatch"])
@pytest.mark.parametrize("fills", [True, False])
def test_other_pool_changes_after_evaluation_are_seen_in_transaction(fleet, monkeypatch, early_kind, fills):
    service, harness = fleet
    service.policy["providers"]["codex"]["max_active_attempts"] = 1
    if not fills:
        active = submit(service, harness, pinned_lane="codex-2")
        service._admit()
    target = (submit_turn(service, harness, 1, pinned_lane="codex-1") if early_kind == "turn" else
              submit(service, harness, pinned_lane="codex-1"))
    other = None
    if fills:
        other = (submit(service, harness, pinned_lane="codex-2") if early_kind == "turn" else
                 submit_turn(service, harness, 2, pinned_lane="codex-2"))
    pick, called = service._pick, []
    def pick_then_change(job, **kwargs):
        decision = pick(job, **kwargs)
        if job["job_id"] == target and not called:
            called.append(True)
            if fills:
                service._admit_kind("detached" if early_kind == "turn" else "turn")
                assert _live(service, other)
            else:
                _end(service, active)
        return decision
    evaluate = scheduler.evaluate
    def outside_only(*args, **kwargs):
        assert not service.store._holds_writer()
        return evaluate(*args, **kwargs)
    monkeypatch.setattr(service, "_pick", pick_then_change)
    monkeypatch.setattr(scheduler, "evaluate", outside_only)
    service._admit_kind("turn" if early_kind == "turn" else "detached")
    assert called
    assert _live(service, target) == (not fills)
    if fills:
        assert_unleased(service, target)
        assert service._holds[target]["reason"] == "provider-cap"
    assert live_count(service) == 1


def test_probe_reservations_do_not_consume_provider_slots(fleet):
    service, harness = fleet
    service.policy["providers"]["codex"]["max_active_attempts"] = 1
    # Timer probe reservations are independent leases and remain admissible at
    # the provider cap. Their lane fence still applies as before.
    assert service.store.acquire_lease("lane:codex-2:slot:0", "probe:timer:fixture")
    assert service.store.acquire_lease("lane:codex-3:slot:0", "probe:admission:fixture")
    job = submit(service, harness, pinned_lane="codex-1")
    service._admit()
    assert _live(service, job) and live_count(service) == 1
    service.store.release_leases("probe:timer:fixture")
    holder = service.timers._claim(service.store.get_lane("codex-2"), "probe")
    assert holder and holder.startswith("probe:timer:")
    assert scheduler.provider_live(service._capacity_view())["codex"] == 1
    service.timers._release(holder)


def test_claude_only_cap_is_symmetric(fleet):
    service, harness = fleet
    service.policy["providers"] = {"claude": {"max_active_attempts": 1}}
    jobs = [submit(service, harness, pinned_model="opus") for _ in range(2)]
    codex = [submit(service, harness) for _ in range(3)]
    service._admit()
    assert [_live(service, job) for job in jobs] == [True, False]
    assert all(_live(service, job) for job in codex)
    assert service._holds[jobs[1]]["provider"] == "claude"


def test_codex_waiter_does_not_hold_a_claude_lane_only_pin(fleet):
    service, harness = fleet
    service.policy["providers"]["codex"]["max_active_attempts"] = 1
    active = submit(service, harness)
    service._admit()
    waiting = submit(service, harness, pinned_lane="codex-1")
    claude = submit(service, harness, pinned_model=None, pinned_lane="claude-1")
    service._admit()
    assert_unleased(service, waiting)
    assert _live(service, active) and _live(service, claude)


def test_provider_fifo_respects_lane_only_pins_and_keeps_last_slot(fleet):
    service, harness = fleet
    active = submit(service, harness, pinned_lane="codex-1")
    service._admit()
    out = str(harness.root / "waiting-out.md")
    older = submit(service, harness, pinned_model=None, pinned_lane="codex-1", out_path=out)
    assert service.store.acquire_lease(f"out:{out}", "other-writer")
    later = submit(service, harness, pinned_model="terra", pinned_lane="codex-2")
    claude = submit(service, harness, pinned_model="opus")
    service._admit()
    assert service._holds[older]["reason"] == "lease-held"
    assert service._holds[later]["reason"] == "provider-slot-kept"
    assert service._holds[later]["kept_for"] == older
    assert "codex: 1 of 2 running" in service.dispatch("why", {"job_id": later})["text"]
    assert _live(service, claude)
    assert_unleased(service, later)
    service.store.release_leases("other-writer")
    service._admit()
    assert _live(service, older) and not _live(service, later)
    _end(service, active)
    service._admit()
    assert _live(service, later)


def test_same_provider_fifo_crosses_models_and_lanes_while_count_is_below_cap(fleet):
    service, harness = fleet
    service.store.add_closure(Closure("codex-1", "account", after(3600),
                                      ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "fixture"))
    older = submit(service, harness, pinned_lane="codex-1", pinned_model="astra")
    later = submit(service, harness, pinned_lane="codex-2", pinned_model="terra")
    claude = submit(service, harness, pinned_model="opus")
    service._admit()
    assert service._holds[later]["behind"] == older
    assert_unleased(service, older)
    assert_unleased(service, later)
    assert _live(service, claude)


@pytest.mark.parametrize("claude_open", [True, False])
def test_mixed_provider_chain_keeps_fifo_only_on_the_provider_it_uses(fleet, claude_open):
    service, harness = fleet
    service.store.add_closure(Closure("codex-1", "account", after(3600),
                                      ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "fixture"))
    if not claude_open:
        service.store.add_closure(Closure("claude-1", "account", after(3600),
                                          ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "fixture"))
    older = submit(service, harness, pinned_lane="codex-1", pinned_model="astra")
    mixed = submit(service, harness, pinned_model=None, task="research", tier="standard")
    service._admit()
    assert_unleased(service, older)
    if claude_open:
        assert _live(service, mixed)
        assert service.store.list_attempts(mixed)[0]["lane_id"] == "claude-1"
    else:
        assert_unleased(service, mixed)
        assert service._holds[mixed]["reason"] == "behind-older-job"
        assert service._holds[mixed]["behind"] == older


ACTIONS = st.lists(st.tuples(st.sampled_from(["codex", "turn", "claude", "complete", "state"]),
                            st.integers(0, 10)), min_size=1, max_size=25)


@settings(max_examples=50, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(cap=st.integers(1, 4), actions=ACTIONS)
def test_random_arrivals_and_completions_never_overbook_at_reservation(tmp_path, cap, actions):
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        with fleet_daemon(Path(directory) / "state") as (service, harness, patch):
            prepare(service, patch, cap)
            reserved = []
            def boundary(name, job, attempt):
                if name == "reserved":
                    assert live_count(service) <= cap
                    reserved.append(attempt)
            patch.setattr(service, "_boundary", boundary)
            jobs = []
            # Exercise a filled cap and its release in every example.
            actions = [("codex", n) for n in range(cap + 1)] + list(actions) + [("complete", 0)]
            for action, n in actions:
                if action == "complete":
                    live = [job for job in jobs if _live(service, job)]
                    if live:
                        _end(service, live[n % len(live)])
                elif action == "state":
                    live = [job for job in jobs if _live(service, job)]
                    if live:
                        state = ["reserved", "starting", "running", "finalizing"][n % 4]
                        with service.store.transaction("test.state") as tx:
                            tx.execute("UPDATE attempts SET state=? WHERE job_id=?", (state, live[n % len(live)]))
                elif action == "turn":
                    jobs.append(submit_turn(service, harness, len(jobs) + 1, pinned_lane=f"codex-{n % 3 + 1}"))
                else:
                    model = "opus" if action == "claude" else "astra" if n % 2 else "terra"
                    jobs.append(submit(service, harness, pinned_model=model))
                service._admit()
                assert live_count(service) <= cap
                for job in jobs:
                    if not _live(service, job) and service.store.get_job(job)["state"] in ("queued", "waiting"):
                        assert_unleased(service, job)
            assert reserved
