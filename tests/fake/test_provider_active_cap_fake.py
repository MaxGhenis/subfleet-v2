"""C-6.4a: provider admission across lanes and both pools, using the fake daemon."""

from pathlib import Path
import json
import tempfile

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import cli, folders, protocol, scheduler
from subfleet.contracts import ClockSource, Closure, ClosureReason, Credential, Lane, LaneOwner, attempt_dir
from subfleet.daemon import after, utcnow
from subfleet.procs import Containment
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
    """Independent oracle: ordinary attempts plus unresolved containment evidence."""
    rows = service.store.query("SELECT a.state,a.quarantine_reason FROM attempts a JOIN lanes l USING(lane_id) "
                               "WHERE l.provider=?", (provider,))
    total = 0
    for row in rows:
        if row["state"] in {"reserved", "starting", "running", "finalizing"}:
            total += 1
        elif row["state"] == "quarantined":
            try:
                census = json.loads(row["quarantine_reason"])
                contained = isinstance(census, dict) and census.get("live_pids") == [] and census.get("unverifiable") is False
            except (ValueError, TypeError):
                contained = False
            total += not contained
    return total


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
    view = service._capacity_view()
    assert scheduler.provider_live(view)["codex"] == 1
    assert view["provider_caps"][0]["probes"] == 2
    for text in (service.dispatch("daemon.status", {})["status"], cli.format_status(service.dispatch("daemon.status", {}))):
        assert "2 exempt probe reservations" in text
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


ACTIONS = st.lists(st.tuples(st.sampled_from(["codex", "turn", "claude", "complete", "state",
                                            "quarantine-live", "quarantine-empty", "quarantine-unknown",
                                            "release", "probe", "probe-end"]),
                            st.integers(0, 10)), min_size=1, max_size=25)


@pytest.mark.parametrize("survivor", ["live", "unknown", "empty"])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("first_pool", ["detached", "turn"])
@pytest.mark.parametrize("waiting_pool", ["detached", "turn"])
def test_quarantine_retains_provider_slot_until_contained_or_released(
        fleet, monkeypatch, survivor, force, first_pool, waiting_pool):
    service, harness = fleet
    # Keep the wait's clock ahead even on a slow host: only freed capacity can
    # bring its next look forward, rather than the one-second recheck expiring.
    now = utcnow()
    monkeypatch.setattr("subfleet.daemon.utcnow", lambda: now)
    service.policy["providers"]["codex"]["max_active_attempts"] = 1
    first = submit_turn(service, harness, 1) if first_pool == "turn" else submit(service, harness)
    service._admit()
    attempt = service.store.list_attempts(first)[0]
    # Admission alone never launches the fake. Production creates this before
    # starting the guardian, including when a late start receipt quarantines it.
    attempt_dir(service.root, first, attempt["seq"]).mkdir(parents=True, exist_ok=True)
    census = (Containment(marker_pids=frozenset({123456})) if survivor == "live" else
              Containment(unverifiable=True) if survivor == "unknown" else Containment())
    service._quarantine(attempt, census, "fixture containment")
    second = submit_turn(service, harness, 2) if waiting_pool == "turn" else submit(service, harness)
    service._admit()
    assert _live(service, second) == census.verified_empty
    assert live_count(service) == 1
    assert scheduler.provider_live(service._capacity_view())["codex"] == 1
    if not census.verified_empty:
        assert_unleased(service, second)
        assert service._holds[second]["live"] == 1
        assert service.store.get_job(second)["next_check_at"] > now
        # Both pools have already observed the quarantine's lane lease gone.
        # Resolution must wake them even with no remaining leases to release.
        assert not service.store.list_leases(attempt["attempt_id"])
        if first_pool == "detached":
            # C-8.4 (#140): a detached job keeps its job-held reader row on its run
            # folder for the whole lease lifetime, quarantine included. Only that
            # pin may remain; no slot or write lease does.
            assert all(row["lease_key"].startswith(folders.READER)
                       for row in service.store.list_leases(first))
        assert "codex: 1 of 1 running" in service.dispatch("daemon.status", {})["status"]
        monkeypatch.setattr(service, "_contain", lambda a: census)
        service._resolve_quarantine(attempt, protocol.KillArgs(first, confirm_dead=True))
        assert live_count(service) == 1
        assert service.store.get_attempt(attempt["attempt_id"])["state"] == "quarantined"
        service._admit()
        assert not _live(service, second)
    monkeypatch.setattr(service, "_contain", lambda a: census if force else Containment())
    service._resolve_quarantine(attempt, protocol.KillArgs(
        first, confirm_dead=not force, force_release=force, operator_note="fixture release"))
    assert service.store.get_attempt(attempt["attempt_id"])["state"] == "lost"
    service._admit()
    assert _live(service, second) and live_count(service) == 1


@pytest.mark.parametrize("pool", ["detached", "turn"])
@pytest.mark.parametrize("survivor", ["live", "unknown"])
def test_verified_empty_quarantine_census_wakes_provider_waiter(fleet, monkeypatch, pool, survivor):
    """C-6.4a: recorded containment frees the count even before state resolution."""
    service, harness = fleet
    now = utcnow()
    monkeypatch.setattr("subfleet.daemon.utcnow", lambda: now)
    service.policy["providers"]["codex"]["max_active_attempts"] = 1
    first = submit(service, harness)
    service._admit()
    attempt = service.store.list_attempts(first)[0]
    census = (Containment(marker_pids=frozenset({123456})) if survivor == "live" else
              Containment(unverifiable=True))
    service._quarantine(attempt, census, "fixture containment")
    waiting = submit_turn(service, harness, 1) if pool == "turn" else submit(service, harness)
    service._admit()
    assert_unleased(service, waiting)
    assert service._holds[waiting]["reason"] == "provider-cap"
    assert service.store.get_job(waiting)["next_check_at"] > now
    with service.store.transaction("test.contained") as tx:
        tx.execute("UPDATE attempts SET quarantine_reason=? WHERE attempt_id=?",
                   (json.dumps(Containment().to_dict()), attempt["attempt_id"]))
    service._admit()
    assert service.store.get_attempt(attempt["attempt_id"])["state"] == "quarantined"
    assert _live(service, waiting) and live_count(service) == 1


@pytest.mark.parametrize("pool", ["turn", "detached"])
@pytest.mark.parametrize("releases", [False, True])
def test_quarantine_census_change_is_revalidated_before_reservation(fleet, monkeypatch, pool, releases):
    service, harness = fleet
    service.policy["providers"]["codex"]["max_active_attempts"] = 1
    first = submit(service, harness)
    service._admit()
    attempt = service.store.list_attempts(first)[0]
    survivor = Containment(marker_pids=frozenset({123456}))
    service._quarantine(attempt, survivor if releases else Containment(), "fixture census")
    target = submit_turn(service, harness, 1) if pool == "turn" else submit(service, harness)
    pick = service._pick
    changed = []

    def pick_then_change(job, **options):
        decision = pick(job, **options)
        if job["job_id"] == target and not changed:
            changed.append(True)
            if releases:
                monkeypatch.setattr(service, "_contain", lambda a: Containment())
                service._resolve_quarantine(attempt, protocol.KillArgs(first, confirm_dead=True))
            else:
                with service.store.transaction("test.census") as tx:
                    tx.execute("UPDATE attempts SET quarantine_reason=? WHERE attempt_id=?",
                               (json.dumps(survivor.to_dict()), attempt["attempt_id"]))
        return decision

    monkeypatch.setattr(service, "_pick", pick_then_change)
    service._admit()
    assert changed
    assert _live(service, target) == releases
    assert live_count(service) == 1
    if not releases:
        assert_unleased(service, target)
        assert service._holds[target]["reason"] == "provider-cap"


@pytest.mark.parametrize("key,value", [("max_in_flight_per_lane", 2),
                                      ("max_active_attempts", 3),
                                      ("max_active_attempts_per_parent", 1)])
def test_existing_caps_preserve_fifo_for_the_selected_provider(fleet, key, value):
    service, harness = fleet
    service.policy["caps"][key] = value
    service.store.add_closure(Closure("codex-1", "account", after(3600),
                                      ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "fixture"))
    older = submit(service, harness, pinned_lane="codex-1", pinned_model="astra")
    later = submit(service, harness, pinned_lane="codex-2", pinned_model="terra")
    service._admit()
    assert_unleased(service, later)
    assert service._holds[later]["behind"] == older
    assert service._holds[later]["provider"] == "codex"


@settings(max_examples=60, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(lane_cap=st.booleans(), pool_cap=st.booleans(), parent_cap=st.booleans(),
       related=st.booleans(), selected=st.sampled_from(["codex", "claude"]),
       waiter_count=st.integers(1, 3))
def test_mixed_chains_wait_only_for_selected_provider_with_existing_caps(
        tmp_path, lane_cap, pool_cap, parent_cap, related, selected, waiter_count):
    """Provider FIFO cannot add an older peer from a provider routing did not select."""
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        with fleet_daemon(Path(directory) / "state") as (service, harness, patch):
            prepare(service, patch, 2)
            service.policy["providers"]["claude"] = {"max_active_attempts": 2}
            if lane_cap:
                service.policy["caps"]["max_in_flight_per_lane"] = 2
            if pool_cap:
                service.policy["caps"]["max_active_attempts"] = 3
            if parent_cap:
                service.policy["caps"]["max_active_attempts_per_parent"] = 1
            # Terminal parent jobs keep these siblings in the same queue class.
            parents = [submit(service, harness) for _ in range(2)]
            for parent in parents:
                _end(service, parent)
            blocked_lane = "codex-1" if selected == "claude" else "claude-1"
            blocked_model = "astra" if selected == "claude" else "opus"
            service.store.add_closure(Closure(blocked_lane, "account", after(3600),
                                              ClosureReason.PROVIDER_LIMIT, ClockSource.REPORTED, "fixture"))
            older = [submit(service, harness, pinned_lane=blocked_lane, pinned_model=blocked_model,
                            parent_job_id=parents[0]) for _ in range(waiter_count)]
            mixed = submit(service, harness, pinned_model=None, task="research", tier="standard",
                           parent_job_id=parents[0 if related else 1])
            decision = service._pick(service.store.get_job(mixed))
            assert service.policy["models"][decision.chosen_model]["provider"] == selected
            service._admit()
            assert all(not _live(service, job) for job in older)
            assert _live(service, mixed), service._holds
            attempt = service.store.list_attempts(mixed)[0]
            assert service.store.get_lane(attempt["lane_id"]).provider == selected


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
            actions = ([("codex", n) for n in range(cap + 1)] +
                       [("quarantine-live", 0), ("probe", 0), ("probe-end", 0), ("release", 1),
                        ("quarantine-empty", 0), ("codex", 0),
                        ("quarantine-unknown", 0), ("release", 0)] +
                       list(actions) + [("complete", 0)])
            for action, n in actions:
                if action.startswith("quarantine-"):
                    live = [job for job in jobs if _live(service, job)]
                    if live:
                        attempt = service.store.list_attempts(live[n % len(live)])[-1]
                        # Model launch's directory setup before a turn can be
                        # quarantined and its release publishes trees.json.
                        attempt_dir(service.root, attempt["job_id"], attempt["seq"]).mkdir(
                            parents=True, exist_ok=True)
                        census = (Containment(marker_pids=frozenset({123456})) if action == "quarantine-live" else
                                  Containment(unverifiable=True) if action == "quarantine-unknown" else Containment())
                        service._quarantine(attempt, census, "generated containment")
                elif action == "release":
                    rows = service.store.query("SELECT * FROM attempts WHERE state='quarantined'")
                    if rows:
                        attempt = rows[n % len(rows)]
                        with patch.context() as release_patch:
                            release_patch.setattr(service, "_contain", lambda a: Containment() if n % 2 == 0 else
                                                  Containment(marker_pids=frozenset({123456})))
                            service._resolve_quarantine(attempt, protocol.KillArgs(
                                attempt["job_id"], confirm_dead=n % 2 == 0, force_release=n % 2 == 1,
                                operator_note="generated release"))
                elif action == "probe":
                    service.store.acquire_lease(f"lane:codex-{n % 3 + 1}:slot:0",
                                                f"probe:{'timer' if n % 2 else 'admission'}:generated-{n}")
                elif action == "probe-end":
                    for row in service.store.query("SELECT holder FROM leases WHERE holder LIKE 'probe:%'"):
                        service.store.release_leases(row["holder"])
                elif action == "complete":
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
                assert live_count(service) <= cap
                service._admit()
                assert live_count(service) <= cap
                assert scheduler.provider_live(service._capacity_view())["codex"] == live_count(service)
                assert scheduler.provider_live(service._capacity_view(rows=service._capacity_rows(route=True)))["codex"] == live_count(service)
                for job in jobs:
                    if not _live(service, job) and service.store.get_job(job)["state"] in ("queued", "waiting"):
                        assert_unleased(service, job)
            assert reserved
