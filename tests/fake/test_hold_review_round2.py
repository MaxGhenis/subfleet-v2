"""C-6.9, C-6.11, C-10.3, C-26.2: regressions found by the second review round of the fair-holds change.

The admission lens of that round (PR #92) wrote each case to show a defect; they stay as tests of the
fixes. A wait that is not for capacity holds nobody. A job waiting on a lease keeps, as a waiter, the
lease set it waits for. A clocked hold keeps naming the rival its look found. No capacity view is
built inside a reservation. A turn is judged with its affinity lane at the top of the pass, as `_pick`
judges it. And `pick` keeps off the desktop login while its hint cannot be read."""

import pytest

import tests.fake.test_routing_end_to_end as R
import tests.fake.test_tier_hold_by_demand as T
from subfleet import daemon as daemon_module, protocol
from subfleet.contracts import Credential, Lane, LaneOwner, Reading, ReadingLabel
from subfleet.daemon import after, utcnow

routing_state = R.routing_state
fleet = T.fleet
three_codex = T.three_codex
submit, wait_on_capacity, close_astra, lane_of = T.submit, T.wait_on_capacity, T.close_astra, T.lane_of


def _workspace_wait(service, job_id, seconds=30):
    service.store.update_job(job_id, state="waiting", wait_reason="workspace", next_check_at=after(seconds))


@pytest.mark.parametrize("with_older", [False, True])
def test_c6_9_a_workspace_wait_holds_no_newer_job(fleet, with_older):
    """C-6.9: only a job waiting for capacity is a waiter. X waits on its workspace (terra or astra);
    J (terra only) is placed whether or not an older astra job W waits for capacity ahead of both,
    because X's wait is not one a lane could end, and W could never take terra's lane."""
    service, harness = fleet
    older = submit(service, harness, pinned_model="astra") if with_older else None
    workspace = submit(service, harness, pinned_model=None, task="sweep", tier="standard")
    newer = submit(service, harness, pinned_model="terra")
    if older:
        wait_on_capacity(service, older)
    _workspace_wait(service, workspace)
    service._admit()
    assert lane_of(service, newer) == ["codex-1"], service._holds.get(newer)


def test_c6_9_a_clocked_lease_waiter_never_holds_the_lease_holder(three_codex):
    """C-6.9: a job is never held behind a job waiting for a lease the first job holds itself. J waits
    for a lease K holds and is also held at the top of the pass (behind W), so it is registered from
    the hold; it must keep its lease set there, or K would be held behind J forever."""
    service, harness = three_codex
    close_astra(service, "codex-1")
    w = submit(service, harness, pinned_model="astra", exclusions=["codex-3"])
    j = submit(service, harness, pinned_model="astra")
    k = submit(service, harness, pinned_model="astra", exclusions=["codex-2"])
    wait_on_capacity(service, w)
    key = "out:/tmp/hold-review-lease"
    with service.store.transaction("fixture.lease") as tx:
        tx.execute("INSERT INTO leases(lease_key,holder,acquired_at) VALUES(?,?,?)", (key, k, utcnow()))
    service.store.update_job(j, state="waiting", wait_reason="capacity", next_check_at=after(30))
    service._capacity_wait(j, "lease-held:" + key, {"reason": "lease-held", "leases": [key]})
    service._admit()
    assert service._holds.get(k, {}).get("behind") != j, service._holds.get(k)


def test_c6_9_a_clocked_hold_keeps_naming_the_rival_its_look_found(three_codex, monkeypatch):
    """C-6.9: the hold a look found on codex-2 names W2, the older job that could run there. While the
    newer job is on its clock the next pass must not rename it to W1, which can only use closed codex-1
    and so could never take the lane the newer job would."""
    service, harness = three_codex
    close_astra(service, "codex-1")
    close_astra(service, "codex-2")
    stale = service._route_view(service._desktop_identity())[2]
    with service.store.transaction("fixture.reopen") as tx:
        tx.execute("UPDATE closures SET released_at=? WHERE lane_id='codex-2'", (utcnow(),))
    monkeypatch.setattr(service, "_hold_view", lambda desktop: stale)
    w1 = submit(service, harness, pinned_model="astra", exclusions=["codex-2", "codex-3"])
    w2 = submit(service, harness, pinned_model="astra", exclusions=["codex-3"])
    newer = submit(service, harness, pinned_model="astra")
    wait_on_capacity(service, w1)
    wait_on_capacity(service, w2)
    service._admit()
    first = dict(service._holds[newer])
    assert (first.get("behind"), first.get("lane")) == (w2, "codex-2")
    service._admit()
    second = dict(service._holds[newer])
    assert second.get("behind") == w2, f"renamed to {second.get('behind')} (w1={w1})"
    assert w2 in service.dispatch("why", {"job_id": newer})["text"]


def test_c6_11_the_reservation_recheck_builds_no_view_inside_the_transaction(three_codex, monkeypatch):
    """C-6.11: the reservation re-checks the hold on the view the job was judged on. With the shared
    hold view expired (HOLD_VIEW_TTL_S 0, a slow pass) it must still not build a capacity view while
    the reservation transaction is open."""
    service, harness = three_codex
    service.policy["caps"]["max_active_attempts_per_parent"] = 5
    close_astra(service, "codex-1")
    close_astra(service, "codex-2")
    parent = submit(service, harness, pinned_model="terra")
    service.store.update_job(parent, state="waiting", wait_reason="approval")
    w = submit(service, harness, pinned_model="astra", exclusions=["codex-3"], parent_job_id=parent)
    newer = submit(service, harness, pinned_model="astra")
    wait_on_capacity(service, w)
    inside = []
    real = service._route_view

    def spy(*args, **kwargs):
        inside.append(service.store.conn.in_transaction)
        return real(*args, **kwargs)
    monkeypatch.setattr(service, "_route_view", spy)
    monkeypatch.setattr(daemon_module, "HOLD_VIEW_TTL_S", 0)
    service._admit()
    assert inside and True not in inside, inside
    assert lane_of(service, newer) == ["codex-3"]


def _turn(service, harness, n, *, affinity=None, exclusions=()):
    prompt = harness.root / f"turn-{n}.md"
    prompt.write_text("turn")
    args = protocol.SubmitArgs(request_id=f"turn:message-{n}:0", kind="turn", workdir=str(harness.workdir),
                               prompt_path=str(prompt), sandbox="read-only", pinned_model="astra",
                               name=f"turn-conversation-{n}", exclusions=list(exclusions), in_place=True,
                               independent=True, no_preamble=True, max_attempts=1, allow_tmp=True)
    turn = {"conversation_id": f"conversation-{n}", "message_id": f"message-{n}", "provider": "codex",
            "digest": f"digest-{n}", "affinity_lane": affinity}
    return service.submit(args, turn=turn)["job_id"]


def test_c26_2_a_turn_is_held_only_on_the_lane_its_affinity_takes(three_codex):
    """C-26.2 with C-6.9: the newer turn's conversation last ran on codex-3, so `_pick` would place it
    there. The older turn excludes codex-3, so it cannot hold the newer one back; judging the newer
    turn without its affinity lane (codex-1) would wrongly find it behind the older."""
    service, harness = three_codex
    service.policy["conversations"].update(max_active_turns=5)
    older = _turn(service, harness, 0, exclusions=["codex-3"])
    newer = _turn(service, harness, 1, affinity="codex-3")
    wait_on_capacity(service, older)
    service._admit_turns()
    assert lane_of(service, newer) == ["codex-3"], service._holds.get(newer)


def test_c10_3_pick_keeps_off_the_desktop_login_while_its_hint_is_unreadable(fleet, monkeypatch):
    """C-10.3: `pick` judges the desktop lane by the hint admission kept, so a moment when ~/.claude.json
    cannot be read never names the real desktop login (claude-9) while a recorded flag sits on claude-1."""
    service, harness = fleet
    for lane_id, email, flag in (("claude-1", "max@optiqal.ai", True), ("claude-9", "max@thesisinstitute.org", False)):
        service.store.put_lane(Lane(lane_id, "claude", f"claude:{email}", Credential("claude", f"/fake/{lane_id}", "home"),
                                    f"/fake/{lane_id}", LaneOwner.V2, flag, label=email))
    service.store.add_reading(Reading("claude-9", "account", "seven_day", .1, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))
    service.store.add_reading(Reading("claude-1", "account", "seven_day", .5, after(86400),
                                      ReadingLabel.PROVIDER, "fixture", utcnow()))
    hint = {"value": "max@thesisinstitute.org"}
    monkeypatch.setattr("subfleet.daemon.capacity.read_desktop_account", lambda: hint["value"])
    service._admit()
    readable = service.dispatch("pick", {"family": "claude", "model": "opus"})
    hint["value"] = None
    service._admit()
    unreadable = service.dispatch("pick", {"family": "claude", "model": "opus"})
    for answer in (readable, unreadable):
        assert [row["lane_id"] for row in answer["excluded"]] == ["claude-9"]
        assert answer["best"] == "max@optiqal.ai"


def test_c6_4_the_hold_check_counts_a_family_once_per_view_not_once_per_rival(three_codex, monkeypatch):
    """C-6.4 with C-6.9: with a parent cap set, whether a rival's own family is full is read from counts
    built once per view. The first cut asked `scheduler._parent_blocks` (a walk of every attempt) for each
    rival of each held job: 421 calls and about 600 ms a pass with 40 jobs waiting over 500 finished ones.
    A pass may call it no more often than it evaluates a job: at most once per waiting job."""
    service, harness = three_codex
    service.policy["caps"]["max_active_attempts_per_parent"] = 50
    parent = submit(service, harness, pinned_model="terra")
    service.store.update_job(parent, state="waiting", wait_reason="approval")
    waiting = [submit(service, harness, pinned_model="astra", parent_job_id=parent) for _ in range(20)]
    waiting += [submit(service, harness, pinned_model="astra") for _ in range(20)]
    for job_id in waiting:
        wait_on_capacity(service, job_id, 600)
    calls = [0]
    real = daemon_module.scheduler._parent_blocks

    def counted(*args, **kwargs):
        calls[0] += 1
        return real(*args, **kwargs)
    monkeypatch.setattr(daemon_module.scheduler, "_parent_blocks", counted)
    service._admit()
    calls[0] = 0
    for _ in range(3):
        service._admit()
    assert calls[0] <= 3 * len(waiting), calls[0]
