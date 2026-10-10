"""C-6.9, C-26.9: a job waiting for a lease holds no later job back; it keeps the last slot.

Review of fenced-turn-precheck (0812d5c5), P3-4, measured 2026-10-06 with
`~/reviews/subfleet-2110/lease-waiter-slot/measure_lease_waiter_holdback.py`. With
`conversations.max_active_turns` 5, an older turn waiting `lease-held` on retention's
fence, on a detached writer's checkout, on its conversation or on its native session
held a later turn of its model `behind-older-job` on each of four looks, with 0 or 1
of 5 turn slots live. With `caps.max_active_attempts` 5 an older detached job waiting
for an output path did the same to a later detached job. With no cap each later job
was placed at once.

These run the daemon's own admission, in-process, on the fake providers. The pure
pass model's properties are in `tests/unit/test_admission_lease_waiters.py`; the
last test here checks the daemon against that model.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from hypothesis import HealthCheck, event, given, settings, strategies as st

from subfleet import folders, protocol
from subfleet.daemon import after
from tests.admission_model import run_pass
from tests.fake.test_admission_latency import fleet_daemon, measure, submit
from tests.fake.test_admission_liveness import CODEX, _checkout, _end, _live, _turn_in

KINDS = ("fence", "detached-writer", "conversation", "native", "detached-out")
SID = "0199aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"


def _native_turn(service, harness, n, *, workdir):
    """A turn of a conversation that continues native session `SID` (C-26.3)."""
    prompt = harness.root / f"turn-{n}.md"
    prompt.write_text("turn")
    args = protocol.SubmitArgs(request_id=f"turn:message-{n}:0", kind="turn", workdir=str(workdir),
                               prompt_path=str(prompt), sandbox="workspace-write", pinned_model="astra",
                               name="turn-resumed", in_place=True, independent=True, no_preamble=True,
                               max_attempts=1, allow_tmp=True)
    return service.submit(args, turn={"conversation_id": "resumed", "message_id": f"message-{n}",
                                      "provider": "codex", "digest": f"d{n}", "native_session_id": SID})["job_id"]


def _hold(kind, service, harness):
    """Another holder takes a lease of `kind` (a turn's): its key, and how it is let go."""
    if kind in ("fence", "detached-writer"):
        key = folders.exclusive_key(folders.canonical(str(harness.workdir)))
        if kind == "fence":
            # Retention's fence on the checkout the turn writes in (C-8.4): the look finds it.
            assert service.store.acquire_lease(key, "retention:retired")
            return key, lambda: service.store.release_leases("retention:retired")
        writer = submit(service, harness, sandbox="workspace-write", in_place=True)
        service._admit()
        assert _live(service, writer)
        return key, lambda: _end(service, writer)
    if kind == "conversation":
        first = _turn_in(service, harness, 0, workdir=harness.workdir, conversation="shared")
        service._admit_turns()
        assert _live(service, first)
        return "conversation:shared", lambda: _end(service, first)
    assert kind == "native"
    assert service.store.acquire_lease(f"native:codex:{SID}", "a-resume-of-it")      # C-26.3
    return f"native:codex:{SID}", lambda: service.store.release_leases("a-resume-of-it")


def _waiter(kind, service, harness):
    """The turn that needs the lease `_hold` took."""
    if kind == "native":
        return _native_turn(service, harness, 1, workdir=harness.workdir)
    return _turn_in(service, harness, 1, workdir=harness.workdir,
                    conversation="shared" if kind == "conversation" else None)


def _older(kind, service, harness):
    """The older job, waiting for a lease of `kind`, and how that lease is let go. A
    turn for every kind but `detached-out`."""
    if kind == "detached-out":
        out = str(harness.root / "shared-out.md")
        older = submit(service, harness, out_path=out)
        assert service.store.acquire_lease(f"out:{out}", "someone-else")   # submit refuses a held path
        return older, lambda: service.store.release_leases("someone-else")
    _, let_go = _hold(kind, service, harness)
    return _waiter(kind, service, harness), let_go


def _later(kind, service, harness, n, tmp_path, *, model="astra"):
    """A later job that competes with the older one (its model), in a folder of its own."""
    if kind == "detached-out":
        return submit(service, harness)
    folder = tmp_path / f"later-{n}"
    folder.mkdir(exist_ok=True)
    return _turn_in(service, harness, 10 + n, workdir=folder, sandbox="read-only", model=model)


def _cap(service, patch, kind, cap):
    if kind == "detached-out":
        patch.setitem(service.policy, "caps", {**service.policy["caps"], "max_active_attempts": cap})
    else:
        patch.setitem(service.policy, "conversations", {**(service.policy.get("conversations") or {}),
                                                         "max_active_turns": cap})


def _pool_live(service, kind):
    return service.store.one("SELECT count(*) AS n FROM attempts a JOIN jobs j USING(job_id) WHERE a.state IN "
                             "('reserved','starting','running','finalizing') AND (j.kind='turn')=?",
                             (kind != "detached-out",))["n"]


@pytest.fixture
def world(tmp_path):
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        _checkout(harness)
        for lane_id in CODEX:
            measure(service, lane_id)
        real = service._workspace
        patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None, []))
        yield service, harness, patch, real


@pytest.mark.parametrize("kind", KINDS)
def test_c6_9_a_job_waiting_for_a_lease_holds_no_later_job_back_under_a_cap(world, tmp_path, kind):
    """The measured shape, with a cap of 5: the later job is placed by the pass that
    looks at it, while the older one waits for its lease, and the older one is placed
    by the first pass after the lease is let go. On 0812d5c5 the later job was held
    `behind-older-job` behind it for as long as the lease was held."""
    service, harness, patch, _ = world
    older, let_go = _older(kind, service, harness)
    _cap(service, patch, kind, 5)
    later = _later(kind, service, harness, 0, tmp_path)
    admit = service._admit if kind == "detached-out" else service._admit_turns
    admit()
    assert service._holds[older]["reason"] == "lease-held", service._holds.get(older)
    assert _live(service, later), service._holds.get(later)
    let_go()
    admit()
    assert _live(service, older), service._holds.get(older)


@pytest.mark.parametrize("kind", KINDS)
def test_c6_9_the_last_slot_is_kept_for_a_job_waiting_for_a_lease(world, tmp_path, kind):
    """Three later jobs that compete with the older one, and a cap three above what is
    live: two are placed, the third is held `slot-kept` for the older job, which is
    placed by the first pass after its lease is let go, into the slot kept for it. The
    third stays out (the pool is full), except where the lease's holder was a turn of
    the pool (`conversation`), whose end frees a slot of its own."""
    service, harness, patch, _ = world
    older, let_go = _older(kind, service, harness)
    cap = _pool_live(service, kind) + 3
    _cap(service, patch, kind, cap)
    later = [_later(kind, service, harness, n, tmp_path) for n in range(3)]
    admit = service._admit if kind == "detached-out" else service._admit_turns
    admit()
    assert service._holds[older]["reason"] == "lease-held", service._holds.get(older)
    assert [_live(service, job) for job in later] == [True, True, False], {j: service._holds.get(j) for j in later}
    hold = service._holds[later[2]]
    assert (hold["reason"], hold["kept_for"], hold["live"]) == ("slot-kept", older, cap - 1), hold
    let_go()
    service.store.update_job(later[2], next_check_at=None)
    admit()
    assert _live(service, older), service._holds.get(older)
    assert _pool_live(service, kind) == cap
    assert _live(service, later[2]) is (kind == "conversation"), service._holds.get(later[2])


@pytest.mark.parametrize("clocked", [False, True], ids=["look", "clock"])
def test_c6_16_a_priority_lease_waiter_passes_later_tiers_and_keeps_its_slot(world, clocked):
    """Release's cross-tier priority queue keeps #144's lease-wait distinction."""
    service, harness, patch, _ = world
    older, let_go = _older("detached-out", service, harness)
    service.store.update_job(older, caller_session="chosen", tier="hard", pinned_model="astra")
    patch.setitem(service.policy["admission"], "priority_callers", ["chosen"])
    _cap(service, patch, "detached-out", 3)
    if clocked:
        service._admit()
        assert service._holds[older]["reason"] == "lease-held"
    later = [submit(service, harness, tier="trivial", caller_session="other", pinned_model="astra")
             for _ in range(3)]
    service._admit()
    assert service._holds[older]["reason"] == "lease-held", service._holds.get(older)
    assert [_live(service, job) for job in later] == [True, True, False], service._holds
    hold = service._holds[later[2]]
    assert (hold["reason"], hold["kept_for"], hold["live"]) == ("slot-kept", older, 2), hold
    let_go()
    service._admit()
    assert _live(service, older), service._holds.get(older)


def test_c26_9_a_lease_freed_mid_pass_goes_to_the_older_turn_even_against_one_that_competes(world):
    """C-26.9's queue keeps a lease's order with a cap too, now that C-6.9's hold-back no
    longer does it for a competing turn. An older turn waits for its conversation, which
    a running turn of it holds; that turn ends after the pass has looked at the older
    one, and a newer turn of the same conversation and model, which competes with it
    under the cap, is looked at next. The newer turn is held `lease-held`, queued behind
    the older one, which takes the lease on the next pass. On 0812d5c5 the newer turn
    was held `behind-older-job` instead: the queue was reached only by turns that did
    not compete (the parametrised liveness test chose such a model for that reason)."""
    service, harness, patch, _ = world
    patch.setitem(service.policy, "conversations", {**(service.policy.get("conversations") or {}),
                                                     "max_active_turns": 50})
    first = _turn_in(service, harness, 0, workdir=harness.workdir, conversation="shared")
    service._admit_turns()
    older = _turn_in(service, harness, 1, workdir=harness.workdir, conversation="shared")
    service._admit_turns()
    assert service._holds[older]["reason"] == "lease-held"
    newer = _turn_in(service, harness, 2, workdir=harness.workdir, conversation="shared")
    service.store.update_job(older, next_check_at=after(3600))
    stub = service._workspace

    def first_ends_now(job):
        if job["job_id"] == newer:
            _end(service, first)
        return stub(job)
    patch.setattr(service, "_workspace", first_ends_now)
    service._admit_turns()
    hold = service._holds[newer]
    assert not _live(service, newer), hold
    assert hold["reason"] == "lease-held" and hold["queued"] == ["conversation:shared"]
    assert hold["queued_behind"] == [older]
    patch.setattr(service, "_workspace", stub)
    service._admit_turns()
    assert _live(service, older) and not _live(service, newer), service._holds.get(older)


@pytest.mark.parametrize("path", ["look", "clock"])
def test_c6_9_a_turn_queued_behind_another_for_a_lease_holds_no_later_turn_back(world, tmp_path, path):
    """A lease kept for an older turn (`queued`, the key itself free) is a lease wait as
    much as a key another holder has: the turn queued for it holds no later turn of
    another conversation back under a cap, whether the reserving transaction just
    queued it (`look`) or it is passed on its clock with that hold recorded (`clock`).
    With only the held keys counted as its wait, each path made it a slot waiter."""
    service, harness, patch, _ = world
    patch.setitem(service.policy, "conversations", {**(service.policy.get("conversations") or {}),
                                                     "max_active_turns": 50})
    if path == "look":
        first = _turn_in(service, harness, 0, workdir=harness.workdir, conversation="shared")
        service._admit_turns()
        older = _turn_in(service, harness, 1, workdir=harness.workdir, conversation="shared")
        service._admit_turns()
        queued = _turn_in(service, harness, 2, workdir=harness.workdir, conversation="shared")
        service.store.update_job(older, next_check_at=after(3600))
        stub = service._workspace

        def first_ends_now(job):
            if job["job_id"] == queued:
                _end(service, first)
            return stub(job)
        patch.setattr(service, "_workspace", first_ends_now)
    else:
        queued = _turn_in(service, harness, 2, workdir=harness.workdir, conversation="shared")
        key = "conversation:shared"
        service._capacity_wait(queued, "lease-held:" + key,
                               {"reason": "lease-held", "leases": [], "queued": [key], "queued_behind": ["gone"]})
        with service.store.transaction("fixture.wait") as tx:
            tx.execute("UPDATE jobs SET state='waiting',wait_reason='capacity',next_check_at=? WHERE job_id=?",
                       (after(3600), queued))
        service._admit_turns()                       # the lease snapshot: nothing is freed on the next pass
    later = _later("conversation", service, harness, 0, tmp_path)
    service._admit_turns()
    hold = service._holds[queued]
    assert hold["reason"] == "lease-held" and hold["queued"] == ["conversation:shared"] and not hold["leases"], hold
    assert _live(service, later), service._holds.get(later)


# --- the daemon against the pass model ------------------------------------------------------------

DIFFERENTIAL = settings(max_examples=30, deadline=None, derandomize=True,
                        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture,
                                               HealthCheck.data_too_large])


@DIFFERENTIAL
@given(kind=st.sampled_from(KINDS[:4]), clocked=st.booleans(), cap=st.sampled_from([None, 1, 2, 3, 5]),
       ahead=st.lists(st.sampled_from(["astra", "terra"]), max_size=2),
       later=st.lists(st.sampled_from(["astra", "terra"]), min_size=1, max_size=4))
def test_c6_9_the_turn_pass_takes_the_pass_model_s_decisions_around_a_lease_waiter(tmp_path_factory, kind, clocked,
                                                                                    cap, ahead, later):
    """Differential, the daemon's turn pass against `tests/admission_model.run_pass` on the
    view the daemon evaluates on: turns live or queued before the older one, the older
    turn waiting for a lease of `kind` (looked at by the transaction or retention's fence
    look, or passed on its clock), and later turns of its model or another's, under a
    turn cap or none. Each job's outcome agrees: placed, or held for the same reason,
    behind or keeping a slot for the same job."""
    tmp_path = tmp_path_factory.mktemp("differential")
    with fleet_daemon(tmp_path / "state") as (service, harness, patch):
        _checkout(harness)
        for lane_id in CODEX:
            measure(service, lane_id)
        patch.setattr(service, "_workspace", lambda job: (job["workdir"], None, None, []))
        if cap is not None:
            _cap(service, patch, kind, cap)
        key, _ = _hold(kind, service, harness)
        before = [_later(kind, service, harness, 20 + n, tmp_path, model=model) for n, model in enumerate(ahead)]
        service._admit_turns()
        older = _waiter(kind, service, harness)
        service._admit_turns()
        lease_held = (service._holds.get(older) or {}).get("reason") == "lease-held"
        jobs = [_later(kind, service, harness, n, tmp_path, model=model) for n, model in enumerate(later)]
        on_clock = clocked and lease_held
        event(f"older: {'on its clock' if on_clock else 'looked at'}, {'lease-held' if lease_held else 'not'}")
        for job_id in [*before, *jobs, older]:
            service.store.update_job(job_id, next_check_at=None)      # looked at, as the model looks at each
        if on_clock:
            service.store.update_job(older, next_check_at=after(3600))
        holder = service.store.one("SELECT holder FROM leases WHERE lease_key=?", (key,))
        assert holder and holder["holder"] != older
        queued = service.store.query("SELECT * FROM jobs WHERE kind='turn' AND state IN ('queued','waiting') "
                                     "ORDER BY created_at,rowid")
        view = service._capacity_view(service._desktop_identity(), service._capacity_rows(route=True),
                                      now=datetime.now(timezone.utc))
        rows = [dict(row, lease_wait=frozenset({key}),
                     lease_look="clock" if on_clock else "fence" if kind == "fence" else "transaction")
                if row["job_id"] == older else dict(row) for row in queued]
        service._admit_turns()
        model = run_pass(service.policy, view, rows).by_job()
        for row in queued:
            job_id = row["job_id"]
            hold = service._holds.get(job_id) or {}
            daemon_said = ("placed" if _live(service, job_id) else hold.get("reason"),
                           hold.get("behind") or hold.get("kept_for"))
            outcome = model[job_id]
            model_said = ("placed" if outcome.placed else outcome.hold, outcome.waits_for)
            assert daemon_said == model_said, (job_id, "older" if job_id == older else "", daemon_said, model_said,
                                               {j: service._holds.get(j) for j in model})
