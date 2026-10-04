"""C-6.14: no burst onto an unproven lane.

2026-09-30 15:43Z: after 6,742 s with nothing placed, claude-5 came above its floor
while every other Claude lane was limited, held or below-floor, and admission put
37 jobs from about 20 sessions on it within 40 s. Its organisation had disabled
Claude Code: its usage readings looked healthy, and no attempt got a model to
answer. All 37 had launched before the first one failed and C-23.44 disabled it.

A lane that has shown no model answering for `admission.prove_idle_s` (900 s by
default; null turns the hold off) is proven again by one detached attempt, its
pilot. While the pilot is in flight and has not answered, the lane is `no-slot`
to every other detached job (`slot_block` `proving:<attempt id>`, as a probe's
lease is `probe:...`), so those jobs go to other lanes or wait; the moment the
pilot's stream shows its model answering, or it ends, the hold is gone. A turn is
never held. These run the daemon's own admission, the attempt worker's stream
read and finalization in-process; no provider runs.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os

import pytest
import random

from hypothesis import HealthCheck, example, given, settings, strategies as st

from subfleet import capacity, scheduler
from subfleet import daemon as daemon_module
from subfleet.adapters import claude_stream, codex
from subfleet.adapters.registry import register
from subfleet.daemon import Daemon, ANSWER_EVENT
from tests.fake.test_lane_fault import LaneRefuses, add_lane, assertions_only, finish, fleet
from tests.fake.test_state_contract import receipt_fixture, state_daemon  # noqa: F401 (a fixture)
from tests.fake_adapter import FakeAdapter

IDLE = 900
#: A Codex model's first answer, as `codex exec --json` writes it (a real stream, 2026-10-03).
ANSWER = b'{"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "On it."}}\n'
STARTED = b'{"type": "thread.started", "thread_id": "01a1021e-b232-7963-a071-e2abf93a9619"}\n{"type": "turn.started"}\n'


class Streams(FakeAdapter):
    """The fake provider, reading its stream as Codex's adapter does (C-6.14)."""

    def model_answered(self, event):
        return codex.model_answered(event)


def hold_on(daemon, idle=IDLE):
    daemon.policy["admission"]["prove_idle_s"] = idle
    register("codex", Streams)


def submit_many(daemon, harness, n, **changes):
    return [daemon.dispatch("submit", harness.submit_args(**changes))["job_id"] for _ in range(n)]


def placed(daemon, jobs=None):
    rows = daemon.store.query("SELECT * FROM attempts WHERE state IN ('reserved','starting','running','finalizing') "
                              "ORDER BY attempt_id")
    return [row for row in rows if jobs is None or row["job_id"] in jobs]


def due(daemon):
    """Every wait due now: what the passes after a freed slot or an answer look at."""
    with daemon.store.transaction("test.due") as tx:
        tx.execute("UPDATE jobs SET next_check_at=NULL WHERE state='waiting'")


def stream(daemon, attempt, data: bytes):
    adir = daemon.root / "jobs" / attempt["job_id"] / f"a{attempt['seq']}"
    adir.mkdir(mode=0o700, exist_ok=True)
    with open(adir / "stdout", "ab") as out:
        out.write(data)
    return adir


def run(daemon, monkeypatch, attempt):
    """The attempt's provider started (C-4.2 `running`); one worker pass over it, the
    process inspection stubbed (this suite runs no provider)."""
    daemon.store.update_attempt(attempt["attempt_id"], state="running")
    monkeypatch.setattr(daemon, "_inspect_running", lambda *args: True)
    daemon._answer_reads.get(attempt["attempt_id"], {}).update(next=0.0)        # not paced in a test
    daemon._process_attempt(attempt["attempt_id"])


# --- the burst ---------------------------------------------------------------------------------

def test_c6_14_a_burst_onto_an_unproven_lane_places_one_then_the_rest_once_it_answers(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    hold_on(daemon)
    jobs = submit_many(daemon, harness, 6)
    daemon._admit()
    pilot, = placed(daemon)
    assert pilot["job_id"] == jobs[0] and pilot["lane_id"] == "codex-1"
    for job_id in jobs[1:]:
        assert daemon.store.get_job(job_id)["state"] == "waiting"
        assert daemon._holds[job_id]["reason"] == "lane-proving"
        decision = daemon.dispatch("why", {"job_id": job_id})["decision"]
        rejection = decision["evaluations"][0]["rejections"][0]
        assert rejection["reasons"] == ["no-slot"] and rejection["slot_block"] == f"proving:{pilot['attempt_id']}"
    # The CLI's own events are no answer: the hold stands.
    stream(daemon, pilot, STARTED)
    run(daemon, monkeypatch, pilot)
    daemon._admit()
    assert len(placed(daemon)) == 1
    # The model's first item: the lane is proven, and the next pass places the rest at
    # once, though their backed-off clocks have not come round (C-6.10).
    stream(daemon, pilot, ANSWER)
    run(daemon, monkeypatch, pilot)
    assert pilot["attempt_id"] in daemon._attempt_answers
    # Recorded once (`Store.add_event`'s own transaction adds its bare audit row beside it).
    events = [row for row in daemon.store.list_events(pilot["job_id"])
              if row["kind"] == ANSWER_EVENT and json.loads(row["data_json"])]
    assert [(row["lane_id"], row["attempt_id"], json.loads(row["data_json"])) for row in events] == \
        [("codex-1", pilot["attempt_id"], {"source": "stream"})]
    daemon._admit()
    assert sorted(row["job_id"] for row in placed(daemon)) == sorted(jobs)


def probes_answer(daemon, monkeypatch, dead=()):
    """C-11.4's probe, stubbed: `auth-dead` on the lanes named dead, else `ok`; who probed where."""
    from subfleet.contracts import Outcome, OutcomeClass
    probed = []

    def probe(job, lane, model, holder):
        probed.append((job["job_id"], lane.lane_id))
        if lane.lane_id in dead:
            return Outcome(OutcomeClass.AUTH_DEAD, "auth-dead: organization has disabled", evidence={"rc": 1})
        return Outcome(OutcomeClass.OK, "admitted", evidence={"rc": 0})
    monkeypatch.setattr(daemon, "_execute_probe", probe)
    return probed


def test_c6_14_auth_dead_on_the_pilot_disables_the_lane_and_the_burst_stays_queued(state_daemon, monkeypatch):
    """The incident with the hold: one attempt reaches the dead lane; it is disabled
    (C-23.44), the pilot's job moves on (C-4.5), and the other jobs are still queued,
    not failed. When a lane comes, the moved-on job is no pilot of it: Subfleet probes
    it first (C-11.4), and its answer proves the lane for the whole burst."""
    daemon, harness = state_daemon
    hold_on(daemon)
    probed = probes_answer(daemon, monkeypatch)
    jobs = submit_many(daemon, harness, 5)
    daemon._admit()
    pilot, = placed(daemon)
    stream(daemon, pilot, STARTED)
    run(daemon, monkeypatch, pilot)
    finish(daemon, daemon.store.get_attempt(pilot["attempt_id"]), LaneRefuses)
    register("codex", Streams)
    assert daemon.store.get_lane("codex-1").enabled is False
    due(daemon)
    daemon._admit()
    assert placed(daemon) == [] and probed == []
    assert {daemon.store.get_job(job_id)["state"] for job_id in jobs} == {"waiting"}
    assert daemon.store.list_notices() == []
    add_lane(daemon, harness, "codex-2")
    due(daemon)
    daemon._admit()
    assert probed == [(jobs[0], "codex-2")]                   # the moved-on job asked Subfleet's probe
    assert "codex-2" in daemon._lane_answers
    assert sorted(row["job_id"] for row in placed(daemon)) == sorted(jobs)


def test_c6_14_c4_5_two_dead_lanes_fail_no_job_of_a_burst(state_daemon, monkeypatch):
    """Round 2 of the review: with two dead lanes, a job that moved on from the first and
    reached the second as its pilot would fail there, leave it enabled (C-23.44), and the
    next moved-on job would do the same. A moved-on job is no pilot: the second lane is
    probed, the probe's `auth-dead` disables it, and the burst waits, every job queued."""
    daemon, harness = state_daemon
    hold_on(daemon)
    add_lane(daemon, harness, "codex-2")
    probed = probes_answer(daemon, monkeypatch, dead={"codex-2"})
    jobs = submit_many(daemon, harness, 5)
    daemon._admit()
    flying = placed(daemon)
    assert sorted(row["lane_id"] for row in flying) == ["codex-1", "codex-2"]     # one pilot each
    for attempt in flying:                                   # both lanes are dead
        finish(daemon, attempt, LaneRefuses)
    register("codex", Streams)
    for _ in range(4):
        due(daemon)
        daemon._admit()
        assert placed(daemon) == []
    assert [lane.enabled for lane in daemon.store.list_lanes()] == [False, False]
    assert probed == []                                      # nothing left to probe: both found by pilots
    assert {daemon.store.get_job(job_id)["state"] for job_id in jobs} == {"waiting"}
    assert daemon.store.list_notices() == []
    add_lane(daemon, harness, "codex-3")                     # dead too: its probe says so
    probes_answer(daemon, monkeypatch, dead={"codex-3"})
    due(daemon)
    daemon._admit()
    assert daemon.store.get_lane("codex-3").enabled is False and placed(daemon) == []
    assert {daemon.store.get_job(job_id)["state"] for job_id in jobs} == {"waiting"}


def test_c4_5_a_probe_that_answers_auth_dead_sends_the_job_on_at_once(state_daemon, monkeypatch):
    """Round 3 of the review: a moved-on job's probe that answers `auth-dead` has disabled
    its lane (C-23.44), so the job's next evaluation goes on in the same pass, as after a
    `limited` probe, rather than after the 60 s an inconclusive probe waits."""
    daemon, harness = state_daemon
    hold_on(daemon)
    job_id = submit_many(daemon, harness, 1)[0]
    daemon._admit()
    first, = placed(daemon)
    finish(daemon, first, LaneRefuses)                       # codex-1 dead; the job moves on
    register("codex", Streams)
    add_lane(daemon, harness, "codex-2")
    add_lane(daemon, harness, "codex-3")
    probed = probes_answer(daemon, monkeypatch, dead={"codex-2"})
    due(daemon)
    daemon._admit()                                           # one pass
    assert probed == [(job_id, "codex-2"), (job_id, "codex-3")]
    assert daemon.store.get_lane("codex-2").enabled is False
    retry, = placed(daemon)
    assert (retry["job_id"], retry["lane_id"]) == (job_id, "codex-3")


def test_c6_14_a_pilot_that_ends_ok_proves_its_lane(state_daemon):
    """An adapter whose stream the daemon cannot read is proven by an attempt that ends
    `ok` (`Daemon._answered`): before the attempt leaves flight, so no second pilot."""
    daemon, harness = state_daemon
    daemon.policy["admission"]["prove_idle_s"] = IDLE
    jobs = submit_many(daemon, harness, 3)
    daemon._admit()
    pilot, = placed(daemon)
    finish(daemon, pilot, FakeAdapter, rc=0)
    assert daemon.store.get_job(pilot["job_id"])["state"] == "succeeded"
    daemon._admit()
    assert sorted(row["job_id"] for row in placed(daemon)) == sorted(jobs[1:])


def test_c6_14_a_pilot_that_fails_hands_the_hold_to_the_next_job(state_daemon):
    """A pilot that ends without an answer proves nothing: the lane is still unproven,
    and the next job is its pilot, alone."""
    daemon, harness = state_daemon
    hold_on(daemon)
    jobs = submit_many(daemon, harness, 4, pinned_lane="codex-1", max_attempts=1)
    daemon._admit()
    pilot, = placed(daemon)

    class Unknown(FakeAdapter):
        def classify(self, attempt_dir, launch, exit_info):
            from subfleet.contracts import Outcome, OutcomeClass
            return Outcome(OutcomeClass.UNKNOWN, "no answer", evidence={"rc": 1, "model_answered": False})
    finish(daemon, pilot, Unknown)
    register("codex", Streams)
    due(daemon)
    daemon._admit()
    following, = placed(daemon)
    assert following["job_id"] == jobs[1]


def test_c6_14_a_pilot_that_never_answers_holds_its_lane_for_prove_wait_s_and_no_longer(state_daemon, monkeypatch,
                                                                                       caplog):
    """A pilot that hangs before its first answer (a slow hook, an MCP server that never
    starts) would hold its lane until `max_wall_s`. After `admission.prove_wait_s` (300 s)
    it is a pilot no longer: the lane takes one more attempt, the next pilot, and
    `daemon.log` says so once. Null waits for the pilot however long."""
    daemon, harness = state_daemon
    hold_on(daemon)
    jobs = submit_many(daemon, harness, 4)
    daemon._admit()
    pilot, = placed(daemon)
    daemon.store.update_attempt(pilot["attempt_id"], reserved_at=daemon_module.after(-240))
    due(daemon)
    daemon._admit()
    assert len(placed(daemon)) == 1                            # within the wait: still its lane's pilot
    daemon.store.update_attempt(pilot["attempt_id"], reserved_at=daemon_module.after(-360))
    daemon.policy["admission"]["prove_wait_s"] = None
    due(daemon)
    daemon._admit()
    assert len(placed(daemon)) == 1                            # null: however long
    daemon.policy["admission"]["prove_wait_s"] = 300
    due(daemon)
    daemon._admit()
    flying = placed(daemon)
    assert sorted(row["job_id"] for row in flying) == sorted(jobs[:2])     # one more, and it is the pilot now
    following, = [row for row in flying if row["attempt_id"] != pilot["attempt_id"]]
    assert daemon._holds[jobs[2]]["reason"] == "lane-proving"
    now = daemon._pick(daemon.store.get_job(jobs[2]))          # as a look now judges it
    assert now.evaluations[0]["rejections"][0]["slot_block"] == f"proving:{following['attempt_id']}"
    stream(daemon, pilot, STARTED)
    with caplog.at_level("WARNING"):
        run(daemon, monkeypatch, daemon.store.get_attempt(pilot["attempt_id"]))
        run(daemon, monkeypatch, daemon.store.get_attempt(pilot["attempt_id"]))
    said = [record.getMessage() for record in caplog.records if "no longer holds the lane" in record.getMessage()]
    assert said == [f"attempt {pilot['attempt_id']} on lane codex-1 has shown no model answering for 300 s; it no "
                    "longer holds the lane, which takes one more attempt (C-6.14)"]


def test_c6_14_an_ok_attempt_proves_its_lane_whatever_its_stream_showed(state_daemon):
    """A stream shape the predicate does not know (`model_answered` False) with a
    verdict of `ok`: the lane is proven, or it would serve one attempt at a time for good."""
    from subfleet.contracts import Outcome, OutcomeClass
    daemon, harness = state_daemon
    daemon.policy["admission"]["prove_idle_s"] = IDLE
    jobs = submit_many(daemon, harness, 3)
    daemon._admit()
    pilot, = placed(daemon)

    class UnknownShape(FakeAdapter):
        def classify(self, attempt_dir, launch, exit_info):
            return Outcome(OutcomeClass.OK, "ok", evidence={"rc": 0, "model_answered": False})
    finish(daemon, pilot, UnknownShape, rc=0)
    assert "codex-1" in daemon._lane_answers
    daemon._admit()
    assert sorted(row["job_id"] for row in placed(daemon)) == sorted(jobs[1:])


def test_c6_14_only_an_answer_on_an_unproven_lane_wakes_admission(state_daemon):
    """C-6.10: a proven lane's every start answers too; a look at every backed-off wait
    for each would be the cost C-6.10 keeps timer probes out of."""
    daemon, harness = state_daemon
    hold_on(daemon)
    daemon._record_answer("codex-1", "probe")                 # unproven until now: jobs may have waited on it
    assert daemon._take_answer_news() is True
    daemon._record_answer("codex-1", "keepalive")             # proven: nothing was held for it
    assert daemon._take_answer_news() is False
    daemon._lane_answers = {"codex-1": datetime.now(timezone.utc).timestamp() - IDLE - 1}
    daemon._record_answer("codex-1", "probe")
    assert daemon._take_answer_news() is True
    daemon.policy["admission"]["prove_idle_s"] = None         # no hold, so nothing to wake
    daemon._lane_answers = {}
    daemon._record_answer("codex-1", "probe")
    assert daemon._take_answer_news() is False


# --- which lanes are held ----------------------------------------------------------------------

def test_c6_14_a_lane_that_answered_within_the_window_takes_the_whole_burst(state_daemon):
    daemon, harness = state_daemon
    hold_on(daemon)
    daemon._record_answer("codex-1", "probe")                  # C-11.4's probe is a model turn
    jobs = submit_many(daemon, harness, 6)
    daemon._admit()
    assert sorted(row["job_id"] for row in placed(daemon)) == sorted(jobs)


@pytest.mark.parametrize("idle_for,held", [(IDLE - 60, False), (IDLE + 60, True)])
def test_c6_14_a_lane_idle_past_the_window_is_proven_again(state_daemon, idle_for, held):
    daemon, harness = state_daemon
    hold_on(daemon)
    daemon._lane_answers = {"codex-1": datetime.now(timezone.utc).timestamp() - idle_for}
    jobs = submit_many(daemon, harness, 4)
    daemon._admit()
    assert len(placed(daemon)) == (1 if held else 4)


def test_c6_14_null_turns_the_hold_off(state_daemon):
    daemon, harness = state_daemon
    hold_on(daemon, idle=None)
    jobs = submit_many(daemon, harness, 4)
    daemon._admit()
    assert len(placed(daemon)) == 4


def test_c6_14_other_lanes_are_unaffected(state_daemon):
    """codex-1 unproven, codex-2 proven: one attempt goes to codex-1 as its pilot and
    the rest to codex-2, in C-11.3's load bands (`admission.lane_spread`); nobody waits."""
    daemon, harness = state_daemon
    hold_on(daemon)
    add_lane(daemon, harness, "codex-2")
    daemon._record_answer("codex-2", "keepalive")
    jobs = submit_many(daemon, harness, 5)
    daemon._admit()
    lanes = [row["lane_id"] for row in placed(daemon)]
    assert sorted(lanes) == ["codex-1", "codex-2", "codex-2", "codex-2", "codex-2"]
    assert all(daemon.store.get_job(job_id)["state"] == "running" for job_id in jobs)


# --- C-6.3: the reservation's check sees what an evaluation now would -------------------------

def test_c6_3_c6_14_the_reservation_check_and_a_fresh_evaluation_agree_on_a_pilot(state_daemon):
    """Differential: a pilot placed after a job's early evaluation, and an answer heard
    after it, change that lane in the reservation's check (`_route_rows`, `still_stands`)
    exactly as an evaluation now (`_pick`) sees them."""
    daemon, harness = state_daemon
    hold_on(daemon)
    first, second = submit_many(daemon, harness, 2)
    job = daemon.store.get_job(second)
    basis = {}
    early = daemon._pick(job, basis=basis)
    assert early.chosen_lane == "codex-1"
    aid = f"{first}/a1"
    with daemon.store.transaction("test.pilot") as tx:            # another pass placed `first` meanwhile
        tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,evidence_json,reserved_at)"
                   " VALUES(?,?,1,'codex-1','gpt-6-astra','reserved','{}',?)", (aid, first, daemon_module.utcnow()))
    why, judged, now = daemon._route_stands(basis, early)
    fresh = daemon._pick(job)
    assert why is None and judged >= 1
    assert now.chosen_lane is None is fresh.chosen_lane
    assert scheduler.verdict_signature(now) == scheduler.verdict_signature(fresh)
    assert now.evaluations[0]["rejections"][0]["slot_block"] == f"proving:{aid}"
    # And back: the pilot answers after the next early evaluation.
    basis = {}
    early = daemon._pick(job, basis=basis)
    daemon._record_answer("codex-1", "stream", attempt=daemon.store.get_attempt(aid))
    why, judged, now = daemon._route_stands(basis, early)
    fresh = daemon._pick(job)
    assert why is None and now.chosen_lane == "codex-1" == fresh.chosen_lane
    assert scheduler.verdict_signature(now) == scheduler.verdict_signature(fresh)


def test_c6_14_the_view_and_the_check_lay_the_same_marks(state_daemon):
    """`_capacity_view` and `_route_rows` lay the same pilot marks over the same rows."""
    daemon, harness = state_daemon
    hold_on(daemon)
    add_lane(daemon, harness, "codex-2")
    submit_many(daemon, harness, 4)
    daemon._admit()
    basis = {}
    daemon._pick(daemon.store.list_jobs()[-1], basis=basis)
    view_marks = {lane: mark for lane, mark in basis["view"]["unavailable_lanes"].items()
                  if capacity.pilot_block(mark)}
    rows = daemon._route_rows(basis, datetime.now(timezone.utc))
    assert view_marks and view_marks == {lane: mark for lane, mark in rows["unavailable"].items()
                                         if capacity.pilot_block(mark)}


# --- what is remembered -------------------------------------------------------------------------

def test_c6_14_a_restart_keeps_what_was_proven(tmp_path):
    with fleet(tmp_path / "state", 2) as (service, harness):
        service.policy["admission"]["prove_idle_s"] = IDLE
        jobs = submit_many(service, harness, 2)
        service._admit()
        pilot = placed(service)[0]
        service._record_answer(pilot["lane_id"], "stream", attempt=pilot)
        service._record_answer("codex-2", "keepalive")
        before = dict(service._lane_answers)
        service.close()
        again = Daemon(service.root)
        try:
            assert set(again._lane_answers) == {pilot["lane_id"], "codex-2"}
            assert all(abs(again._lane_answers[lane] - when) <= 1 for lane, when in before.items())
            assert set(again._attempt_answers) == {pilot["attempt_id"]}         # still in flight
        finally:
            again.close()


# --- the stream reader ---------------------------------------------------------------------------

class ReadsClaude(FakeAdapter):
    def model_answered(self, event):
        return claude_stream.model_answered(event)


INCIDENT = [
    b'{"type":"system","subtype":"hook_started","hook_name":"SessionStart:startup"}\n',
    b'{"type":"system","subtype":"init","model":"claude-opus-5-5","apiKeySource":"none"}\n',
    b'{"type":"assistant","error":"oauth_org_not_allowed","is_api_error_message":true,"message":{"model":"<synthetic>",'
    b'"role":"assistant","type":"message","content":[{"type":"text","text":"Your organization has disabled Claude '
    b'subscription access for Claude Code"}],"usage":{"input_tokens":0,"output_tokens":0,'
    b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n',
    b'{"type":"result","subtype":"success","is_error":true,"duration_api_ms":0,"usage":{"input_tokens":0,'
    b'"output_tokens":0}}\n',
]
THINKING = b'{"type":"system","subtype":"thinking_tokens","estimated_tokens":50,"estimated_tokens_delta":50}\n'


def reader(daemon, harness, monkeypatch, adapter):
    monkeypatch.setattr(daemon_module, "ANSWER_READ_INTERVAL_S", 0.0)
    register("codex", adapter)
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon._admit()
    attempt = daemon.store.list_attempts(job_id)[-1]
    adir = daemon.root / "jobs" / job_id / "a1"
    adir.mkdir(mode=0o700, exist_ok=True)
    return attempt, adir


def test_c6_14_the_incident_stream_never_proves_its_lane(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    attempt, adir = reader(daemon, harness, monkeypatch, ReadsClaude)
    daemon._read_answer(attempt, adir)                         # nothing written yet
    for line in INCIDENT:
        with open(adir / "stdout", "ab") as out:
            out.write(line)
        daemon._read_answer(attempt, adir)
    assert attempt["attempt_id"] not in daemon._attempt_answers and daemon._lane_answers == {}
    with open(adir / "stdout", "ab") as out:
        out.write(THINKING)
    daemon._read_answer(attempt, adir)
    assert attempt["attempt_id"] in daemon._attempt_answers and "codex-1" in daemon._lane_answers


def test_c6_14_a_fifo_where_the_stream_belongs_is_never_waited_on(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    attempt, adir = reader(daemon, harness, monkeypatch, ReadsClaude)
    os.mkfifo(adir / "stdout")
    daemon._read_answer(attempt, adir)                         # returns at once: not a regular file
    assert attempt["attempt_id"] not in daemon._attempt_answers


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(lines=st.lists(st.sampled_from(INCIDENT + [THINKING, b"not json\n", b"\n", b"x" * 300 + b"\n"]),
                      min_size=1, max_size=8),
       cuts=st.lists(st.integers(0, 2000), max_size=6), chunk=st.integers(1, 64))
def test_c6_14_the_incremental_reader_agrees_with_reading_the_whole_stream(state_daemon, monkeypatch, lines, cuts,
                                                                            chunk):
    """Differential: written in any pieces and read in any chunk size, the reader
    records an answer exactly when a complete line of the stream answers, as a parse
    of the whole stream says: never early for half a line, never missing one that
    arrived across reads."""
    daemon, harness = state_daemon
    daemon._attempt_answers, daemon._lane_answers, daemon._answer_reads = {}, {}, {}
    monkeypatch.setattr(daemon_module, "ANSWER_READ_CHUNK", chunk)
    monkeypatch.setattr(daemon_module, "ANSWER_READ_INTERVAL_S", 0.0)
    register("codex", ReadsClaude)
    rows = daemon.store.list_attempts()
    if rows:
        attempt = rows[-1]
    else:
        job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
        daemon._admit()
        attempt = daemon.store.list_attempts(job_id)[-1]
    adir = daemon.root / "jobs" / attempt["job_id"] / "a1"
    adir.mkdir(mode=0o700, exist_ok=True)
    data = b"".join(lines)
    (adir / "stdout").write_bytes(b"")
    points = sorted({min(cut, len(data)) for cut in cuts} | {len(data)})
    start = 0
    for point in points:
        with open(adir / "stdout", "ab") as out:
            out.write(data[start:point])
        start = point
        for _ in range(len(data) // chunk + 2):
            daemon._read_answer(attempt, adir)
    expected = any(claude_stream.model_answered(json.loads(line)) for line in lines if line.startswith(b"{"))
    assert (attempt["attempt_id"] in daemon._attempt_answers) is expected
    if expected:                                               # never before the answering line was whole
        assert data.find(b"\n", data.find(THINKING)) < len(data)


def test_c6_14_a_line_longer_than_the_reader_holds_is_skipped_and_reading_goes_on(state_daemon, monkeypatch):
    """A line still incomplete past `ANSWER_LINE_MAX` (a `system/init` listing every tool
    and skill) is dropped, the rest of it skipped to its newline, and the next line read."""
    daemon, harness = state_daemon
    attempt, adir = reader(daemon, harness, monkeypatch, ReadsClaude)
    monkeypatch.setattr(daemon_module, "ANSWER_LINE_MAX", 64)
    monkeypatch.setattr(daemon_module, "ANSWER_READ_CHUNK", 50)
    long_init = b'{"type":"system","subtype":"init","tools":["' + b"T" * 400 + b'"]}\n'
    (adir / "stdout").write_bytes(long_init + THINKING)
    for _ in range(20):
        daemon._read_answer(attempt, adir)
    assert attempt["attempt_id"] in daemon._attempt_answers
    assert daemon._answer_reads == {}                          # done with this attempt's stream


# --- the invariant, over any order of events --------------------------------------------------

OPS = st.lists(st.sampled_from(["submit", "submit3", "admit", "admit", "answer", "finish", "finish-dead",
                                "age", "warm"]), min_size=1, max_size=30)


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(lanes=st.integers(1, 3), ops=OPS, pick=st.randoms(use_true_random=False))
@example(lanes=1, ops=["submit3", "admit"], pick=random.Random(0))     # the burst; its mutation meets it every run
def test_c6_14_a_cold_lane_never_takes_a_second_unanswered_attempt(tmp_path_factory, lanes, ops, pick):
    """For any order of submissions, passes, answers, ends (on a lane that works or one
    that has gone auth-dead) and lanes going idle past the window:

    - a pass never places an attempt on a lane that was unproven at the pass and already
      had a detached attempt in flight that had not answered, and places at most one on
      an unproven lane that had none;
    - no job is held `lane-proving` unless some lane it could use has a pilot in flight
      (the hold always has an end: that pilot's answer or its end);
    - once nothing is in flight and every lane still enabled has answered, every job
      that can run is placed: the hold leaves nothing behind.
    """
    root = tmp_path_factory.mktemp("lane-pilot")
    with fleet(root / "state", lanes) as (service, harness):
        service.policy["admission"]["prove_idle_s"] = IDLE

        def snapshot():
            now = datetime.now(timezone.utc).timestamp()
            cold = {lane.lane_id for lane in service.store.list_lanes()
                    if now - service._lane_answers.get(lane.lane_id, -1e12) >= IDLE}
            flying = placed(service)
            unanswered = {}
            for row in flying:
                if row["attempt_id"] not in service._attempt_answers:
                    unanswered[row["lane_id"]] = unanswered.get(row["lane_id"], 0) + 1
            return cold, {row["attempt_id"] for row in flying}, unanswered

        def admit():
            due(service)
            cold, before, unanswered = snapshot()
            service._admit()
            new: dict[str, int] = {}
            for row in placed(service):
                if row["attempt_id"] not in before:
                    new[row["lane_id"]] = new.get(row["lane_id"], 0) + 1
            for lane_id in cold:
                assert new.get(lane_id, 0) <= (0 if unanswered.get(lane_id) else 1), (lane_id, new, unanswered)
            pilots = {row["lane_id"] for row in placed(service) if row["attempt_id"] not in service._attempt_answers}
            for job_id, hold in service._holds.items():
                if hold.get("reason") == "lane-proving":
                    assert pilots, (job_id, hold)

        for op in ops:
            flying = placed(service)
            if op in ("submit", "submit3"):
                submit_many(service, harness, 1 if op == "submit" else 3)
            elif op == "admit":
                admit()
            elif op == "answer" and flying:
                attempt = pick.choice(flying)
                service._record_answer(attempt["lane_id"], "stream", attempt=attempt)
            elif op in ("finish", "finish-dead") and flying:
                attempt = pick.choice(flying)
                adir = service.root / "jobs" / attempt["job_id"] / f"a{attempt['seq']}"
                adir.mkdir(mode=0o700, exist_ok=True)
                service._finalize(receipt_fixture(service, attempt, adir, rc=1 if op == "finish-dead" else 0,
                                                  stdout=b"DEAD" if op == "finish-dead" else b"done\n"))
            elif op == "age":
                lane_id = f"codex-{pick.randint(1, lanes)}"
                service._lane_answers = {**service._lane_answers,
                                         lane_id: datetime.now(timezone.utc).timestamp() - IDLE - 1}
            elif op == "warm":
                service._record_answer(f"codex-{pick.randint(1, lanes)}", "keepalive")
        # Drain: every attempt answers and ends well; the passes place the rest.
        for _ in range(4 * len(service.store.list_jobs()) + 4):
            flying = placed(service)
            for attempt in flying:
                service._record_answer(attempt["lane_id"], "stream", attempt=attempt)
                adir = service.root / "jobs" / attempt["job_id"] / f"a{attempt['seq']}"
                adir.mkdir(mode=0o700, exist_ok=True)
                service._finalize(receipt_fixture(service, attempt, adir, rc=0, stdout=b"done\n"))
            admit()
            if not placed(service):
                break
        if any(lane.enabled for lane in service.store.list_lanes()):
            assert {job["state"] for job in service.store.list_jobs()} <= {"succeeded", "failed"}
        assert not any(hold.get("reason") == "lane-proving" for hold in service._holds.values())


# --- the tests above can fail -----------------------------------------------------------------

def test_c6_14_the_invariant_fails_with_no_pilot_marks(tmp_path_factory, monkeypatch):
    """Mutation: with no marks laid (admission before C-6.14), the invariant finds a second
    unanswered attempt placed on an unproven lane."""
    monkeypatch.setattr(Daemon, "_pilot_marks", lambda self, attempts, instant: {})
    with pytest.raises((AssertionError, BaseExceptionGroup)) as caught:
        test_c6_14_a_cold_lane_never_takes_a_second_unanswered_attempt(tmp_path_factory=tmp_path_factory)
    assertions_only(caught)


def test_c6_3_the_differential_fails_with_marks_only_in_the_early_view(state_daemon, monkeypatch):
    """Mutation: with the reservation's check laying no marks, it keeps a lane the early
    view chose although a pilot was placed there since, and the differential says so."""
    rows = Daemon._route_rows

    def without_pilots(self, basis, now):
        monkeypatch.setattr(self, "_pilot_marks", lambda attempts, instant: {})
        try:
            return rows(self, basis, now)
        finally:
            monkeypatch.undo()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Daemon, "_route_rows", without_pilots)
        with pytest.raises((AssertionError, BaseExceptionGroup)) as caught:
            test_c6_3_c6_14_the_reservation_check_and_a_fresh_evaluation_agree_on_a_pilot(state_daemon)
        assertions_only(caught)
