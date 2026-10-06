"""C-5.7: paced, restart-safe release through the real resolver, no processes.

The fake provider fixture uses real stores, admission, git salvage and
conversation events. Only process-table/marker inputs and time are scripted.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
import threading
import uuid

from hypothesis import HealthCheck, example, given, settings, strategies as st
import pytest

from subfleet import daemon as dm, folders, procs, protocol, render
from subfleet.daemon import Daemon, QUARANTINE_RECHECK_BATCH
from tests.fake.test_state_contract import state_daemon, reserve  # noqa: F401
from tests.fake.test_workspace_contract import repository
from tests.unit.test_salvage import git

WRITER = procs.ProcessIdentity(42099, "fixture-boot", "old-start")
LIVE = procs.Containment(marker_pids=frozenset({WRITER.pid}), identities={WRITER.pid: WRITER})
TURN_SETTINGS = {"model": "gpt-6-astra", "effort": None, "fast": False,
                 "permission": "accept-edits", "auto_continue": True}


class Clock:
    def __init__(self, monkeypatch, daemon):
        self.now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        daemon.policy["quarantine_recheck_s"] = 10
        monkeypatch.setattr(dm, "quarantine_time", self.stamp)

    def stamp(self, seconds=0):
        return (self.now + timedelta(seconds=seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def advance(self, seconds=10):
        self.now += timedelta(seconds=seconds)


def quarantined(daemon, harness, *, writable=False):
    if writable:
        repository(daemon, harness)
    job_id, a, adir = reserve(daemon, harness, sandbox="workspace-write" if writable else "read-only",
                              in_place=True, out_path=str(harness.root / f"out-{uuid.uuid4().hex}.md"))
    if writable:
        (harness.workdir / "tracked.txt").write_text("writer progress\n")
    daemon._quarantine(a, LIVE, "writers remain after exit receipt")
    for key, holder in (("native:test", job_id), ("native-session:test", a["attempt_id"]),
                        ("conversation:test", job_id)):
        daemon.store.acquire_lease(key, holder)
    return job_id, daemon.store.get_attempt(a["attempt_id"]), adir


def assert_released(daemon, a):
    assert daemon.store.get_attempt(a["attempt_id"])["state"] in {"lost", "interrupted"}
    assert not [lease for lease in daemon.store.list_leases() if lease["holder"] in {a["attempt_id"], a["job_id"]}]


def scripted_census(monkeypatch, daemon, *, writer="old-start", unverifiable=False):
    # Exercise the production three-source census and its recorded identities.
    monkeypatch.setattr(procs, "snapshot", lambda: procs.ProcessTable(
        {WRITER.pid: (1, WRITER.pid, "S", writer)} if writer is not None else {}, boot_id=WRITER.boot_id))
    def read(argv, **kwargs):
        assert "pid=,command=" in argv
        if unverifiable:
            raise procs.InspectionError("marker enumeration unavailable")
        # A still-owned escaped writer need not have a marker: its previously
        # recorded identity is enough to keep its leases.
        return ""
    monkeypatch.setattr(procs, "_read", read)
    monkeypatch.setattr(procs, "containment", ORIGINAL_CENSUS)


ORIGINAL_CENSUS = procs.containment


def new_turn(daemon, harness):
    service = daemon.conversations
    # No provider is launched: the fixture exercises writable turn snapshots,
    # independently of the deployment's Codex guard certification.
    service._codex_writable = lambda: True
    conversation, _ = service.store.create_conversation(
        provider="codex", workspace=str(harness.workdir), workspace_kind="in-place",
        settings=TURN_SETTINGS, origin="new", lane_id="codex-1")
    cid = conversation["conversation_id"]
    mid = str(uuid.uuid4())
    service.op_message_submit({"conversation_id": cid, "message_id": mid, "text": "continue"}, None)
    service._dispatch()
    message = service.store.message(mid)
    job = service._turn_job(message)
    assert job
    return cid, mid, job["job_id"]


def test_writer_exit_frees_every_lease_saves_salvage_and_admits_waiting_turn(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    job, a, adir = quarantined(daemon, harness, writable=True)
    cid, mid, turn_job = new_turn(daemon, harness)
    daemon._admit_turns()
    assert daemon.store.list_attempts(turn_job) == []
    assert daemon.store.get_job(turn_job)["state"] == "waiting"
    scripted_census(monkeypatch, daemon, writer=None)
    clock.advance(9)
    daemon._recheck_quarantines()
    assert daemon.store.get_attempt(a["attempt_id"])["state"] == "quarantined"
    clock.advance(1)
    daemon._recheck_quarantines()
    assert_released(daemon, a)
    receipt = json.loads((adir / "salvage.json").read_text())
    assert receipt["result"]
    [saved] = [r for r in daemon.store.list_artifacts(a["attempt_id"]) if r["role"] == "salvage"]
    assert git(harness.workdir, "show", f"{saved['path']}:tracked.txt") == "writer progress"
    notices = [r["text"] for r in daemon.store.list_notices() if r["job_id"] == job]
    assert len(notices) == 2 and "released from quarantine" in notices[-1] and saved["path"] in notices[-1]
    event = daemon.store.one("SELECT data_json FROM events WHERE kind='quarantine.self_resolved'")
    evidence = json.loads(event["data_json"])
    assert evidence["operator_note"] is None and evidence["override"] is False
    assert evidence["containment"]["live_pids"] == [] and not evidence["containment"]["unverifiable"]
    daemon._admit_turns()
    assert len(daemon.store.list_attempts(turn_job)) == 1
    key = folders.turn_key(folders.canonical(harness.workdir), turn_job, writable=True)
    assert daemon.store.one("SELECT holder FROM leases WHERE lease_key=?", (key,))["holder"] == turn_job


@pytest.mark.parametrize("unverifiable", [False, True])
def test_live_or_unverifiable_census_never_releases_across_many_paces(state_daemon, monkeypatch, unverifiable):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    _, a, _ = quarantined(daemon, harness)
    scripted_census(monkeypatch, daemon, writer=None if unverifiable else WRITER.proc_start, unverifiable=unverifiable)
    before = daemon.store.list_leases()
    censuses = []
    real = daemon._contain
    def census(row):
        assert daemon.store._lock.held is None or daemon.store._lock.held[0] != threading.get_ident()
        censuses.append(clock.stamp())
        return real(row)
    monkeypatch.setattr(daemon, "_contain", census)
    for _ in range(20):
        clock.advance()
        daemon._recheck_quarantines()
        for _ in range(10):
            daemon._recheck_quarantines()
        assert daemon.store.get_attempt(a["attempt_id"])["state"] == "quarantined"
        assert daemon.store.list_leases() == before
    assert len(censuses) == 20
    assert not daemon.store.one("SELECT 1 FROM events WHERE kind='quarantine.self_resolved'")


def test_pid_reuse_with_different_start_time_counts_as_gone(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    _, a, _ = quarantined(daemon, harness)
    # Also reuse the recorded guardian and its group id. An unrelated process
    # at the same pid must not enter the descendant walk or group census.
    daemon.store.update_attempt(a["attempt_id"], guardian_pid=WRITER.pid, pgid=WRITER.pid,
                                boot_id=WRITER.boot_id, proc_start=WRITER.proc_start)
    scripted_census(monkeypatch, daemon, writer="new-start")
    clock.advance()
    daemon._recheck_quarantines()
    assert_released(daemon, a)


def test_notification_failure_does_not_starve_other_quarantine_checks(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    job, a, _ = quarantined(daemon, harness)
    daemon.store.update_attempt(a["attempt_id"], state="lost", quarantine_notice_pending=1, quarantine_recheck_at="")
    new_job, newer, _ = reserve(daemon, harness)
    daemon._quarantine(newer, LIVE, "writer remains")
    daemon.store.update_attempt(newer["attempt_id"], quarantine_recheck_at="")
    notices = []
    def unavailable(row):
        notices.append(row["attempt_id"])
        raise OSError("conversation store unavailable")
    monkeypatch.setattr(daemon, "_quarantine_turn_notice", unavailable)
    daemon._recheck_quarantines()
    assert_released(daemon, newer)
    assert notices == [a["attempt_id"]]
    daemon._recheck_quarantines()
    assert notices == [a["attempt_id"]]  # the outbox also retries on its pace


@pytest.mark.parametrize("boundary", ["quarantine-saved", "quarantine-released"])
def test_restart_mid_resolution_resumes_without_double_salvage(state_daemon, monkeypatch, boundary):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    _, a, adir = quarantined(daemon, harness, writable=True)
    calls, real_salvage = [], dm.salvage
    def save(*args, **kwargs):
        calls.append(1)
        return real_salvage(*args, **kwargs)
    monkeypatch.setattr(dm, "salvage", save)
    def crash(name, *args):
        if name == boundary:
            raise RuntimeError("simulated daemon crash")
    daemon.crash_hook = crash
    clock.advance()
    daemon._recheck_quarantines()
    assert (adir / "salvage.json").exists() and calls == [1]
    daemon.close()
    restarted = Daemon(harness.root)
    try:
        restarted.policy["quarantine_recheck_s"] = 10
        restarted._recheck_quarantines()  # restart before the durable pace
        clock.advance()
        restarted._recheck_quarantines()
        restarted._resolve_quarantine(a, protocol.KillArgs(a["job_id"], confirm_dead=True))
        assert_released(restarted, a)
        assert calls == [1]
        assert len([e for e in restarted.store.list_events(a["job_id"]) if e["kind"] == "quarantine.self_resolved"]) == 1
        assert len([n for n in restarted.store.list_notices() if n["job_id"] == a["job_id"]]) == 2
    finally:
        restarted.close()


def test_turn_release_records_end_snapshot_and_one_system_line_even_after_restart(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    repository(daemon, harness)
    cid, mid, job = new_turn(daemon, harness)
    daemon._admit_turns()
    [a] = daemon.store.list_attempts(job)
    adir = daemon.root / "jobs" / job / "a1"
    adir.mkdir(exist_ok=True)
    (harness.workdir / "tracked.txt").write_text("turn progress\n")
    daemon._quarantine(a, LIVE, "writers remain after exit receipt")
    record = daemon.conversations.record_quarantine_release
    def crash_after_event(*args, **kwargs):
        record(*args, **kwargs)
        raise RuntimeError("crash after conversation commit")
    monkeypatch.setattr(daemon.conversations, "record_quarantine_release", crash_after_event)
    clock.advance()
    daemon._recheck_quarantines()
    assert_released(daemon, a)
    assert daemon.store.get_attempt(a["attempt_id"])["quarantine_notice_pending"] == 1
    daemon.close()
    restarted = Daemon(harness.root)
    try:
        clock.advance()
        restarted._recheck_quarantines()
        assert restarted.store.get_attempt(a["attempt_id"])["quarantine_notice_pending"] == 0
        events = restarted.conversations.store.events_after(cid, 0)["events"]
        lines = [e for e in events if e["kind"] == "status" and e["data"].get("phase") == "quarantine-released"]
        assert len(lines) == 1 and lines[0]["data"]["author"] == "Subfleet"
        assert "writers are gone" in lines[0]["data"]["message"]
        trees = json.loads((adir / "trees.json").read_text())
        assert trees["end_tree"] and not trees["error"]
        assert git(harness.workdir, "show", f"{trees['end_tree']}:tracked.txt") == "turn progress"
        assert not restarted.store.list_notices()
        assert git(harness.workdir, "for-each-ref", "refs/subfleet-salvage") == ""
    finally:
        restarted.close()


def test_turn_without_a_conversation_manifest_is_released_and_never_left_pending(state_daemon, monkeypatch):
    """C-5.7: a quarantined turn whose manifest names no conversation (a legacy turn,
    or a manifest lost to a crash) is released like any other. Its notice step has no
    one to tell, so it clears the pending mark instead of raising after the release
    committed, which would leave the job pinned from retention for ever."""
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    repository(daemon, harness)
    cid, mid, job = new_turn(daemon, harness)
    daemon._admit_turns()
    [a] = daemon.store.list_attempts(job)
    (daemon.root / "jobs" / job / "a1").mkdir(exist_ok=True)
    daemon._quarantine(a, LIVE, "writers remain after exit receipt")
    manifest = daemon.root / "jobs" / job / "manifest.json"
    data = json.loads(manifest.read_text())
    data.pop("turn", None)
    manifest.write_text(json.dumps(data))
    clock.advance()
    daemon._recheck_quarantines()
    assert_released(daemon, a)
    assert daemon.store.get_attempt(a["attempt_id"])["quarantine_notice_pending"] == 0
    assert daemon.store.one("SELECT 1 FROM events WHERE attempt_id=? AND kind='quarantine.turn_notice_skipped'",
                            (a["attempt_id"],))
    assert not daemon.store.one("SELECT 1 FROM attempts WHERE job_id=? AND quarantine_notice_pending=1", (job,))


def test_300_quarantines_have_bounded_pass_and_tick_cost(state_daemon, monkeypatch, capsys):
    daemon, harness = state_daemon
    Clock(monkeypatch, daemon)
    job, a, _ = quarantined(daemon, harness)
    with daemon.store.transaction("fixture.backlog") as tx:
        columns = list(a)
        for n in range(300):
            row = {**a, "attempt_id": f"{job}/a{n+2}", "seq": n+2, "quarantine_recheck_at": ""}
            tx.execute(f"INSERT INTO attempts({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(row[c] for c in columns))
        tx.execute("DELETE FROM attempts WHERE attempt_id=?", (a["attempt_id"],))
    calls = []
    monkeypatch.setattr(daemon, "_contain", lambda a: calls.append(a["attempt_id"]) or LIVE)
    start = time.perf_counter()
    daemon._recheck_quarantines()
    elapsed = time.perf_counter() - start
    assert len(calls) == 8  # contract bound, independent of the implementation constant
    plan = daemon.store.query("EXPLAIN QUERY PLAN SELECT * FROM attempts WHERE state='quarantined' AND quarantine_recheck_at<=? "
                              "ORDER BY quarantine_recheck_at,attempt_id LIMIT ?", (dm.quarantine_time(), QUARANTINE_RECHECK_BATCH))
    assert any("attempts_quarantine_due" in row["detail"] and "SEARCH" in row["detail"] for row in plan)
    offers = []
    monkeypatch.setattr(daemon, "_schedule", lambda *args, **kwargs: offers.append(args[0]))
    start = time.perf_counter()
    for _ in range(1000):
        daemon._offer_quarantine_recheck()
    tick_elapsed = time.perf_counter() - start
    assert offers == ["quarantine-recheck"]
    print(f"300 attempts: pass={elapsed*1000:.3f}ms, censuses={len(calls)}; 1000 tick offers={tick_elapsed*1000:.3f}ms")


def test_status_names_old_quarantines_age_and_latest_reason(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    _, a, _ = quarantined(daemon, harness)
    daemon.store.update_attempt(a["attempt_id"], finished_at="2026-09-22T12:00:00Z",
                                quarantine_reason=json.dumps({"reason": "writers remain after exit receipt",
                                                              "unverifiable": True, "errors": ["marker enumeration unavailable"]}))
    view = daemon._capacity_view(desktop_in_use={})
    text = render.status(view)
    assert a["attempt_id"] in text and "marker enumeration unavailable" in text and "Age" in text
    assert view["quarantined_attempts"][0]["age_s"] > 600
    assert not render.quarantine_holds({**view, "now": "2026-09-22T12:00:10Z"})


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@example(actions=["tick", "exit0", "unverifiable", "exit1", "restart", "kill", "verifiable", "tick", "restart", "kill"])
@given(actions=st.lists(st.sampled_from(["exit0", "exit1", "reuse0", "reuse1", "unverifiable", "verifiable", "tick", "restart", "kill"]), min_size=1, max_size=35))
def test_property_release_requires_every_recorded_writer_gone_and_occurs_at_most_once(state_daemon, monkeypatch, actions):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    # Fresh job per generated example; use the real store and resolver. Avoid
    # admission/filesystem noise so examples can cover many restart sequences.
    job = f"property-{uuid.uuid4().hex}"
    aid = job + "/a1"
    daemon.store.add_job(job_id=job, request_id=job, payload_digest=job, kind="dispatch",
                         workdir=str(harness.workdir), prompt_path="fixture", sandbox="read-only", state="lost")
    writers = [WRITER, procs.ProcessIdentity(42100, WRITER.boot_id, "second-start")]
    daemon.store.add_attempt(attempt_id=aid, job_id=job, seq=1, lane_id="codex-1", model_requested="astra",
                             state="quarantined", quarantine_reason=json.dumps({"identities": {str(w.pid): {"pid": w.pid, "boot_id": w.boot_id, "proc_start": w.proc_start} for w in writers}}))
    daemon.store.acquire_lease(f"worktree:property:{job}", aid)
    states, unavailable, proofs = ["live", "live"], False, []
    original_read = procs._read
    current = daemon
    def snapshot():
        return procs.ProcessTable({w.pid: (1, w.pid, "S", w.proc_start if state == "live" else "reused-start")
                                   for w, state in zip(writers, states) if state != "gone"}, boot_id=WRITER.boot_id)
    def read(argv, **kwargs):
        if unavailable:
            raise procs.InspectionError("marker enumeration unavailable")
        return ""
    def census(*args, **kwargs):
        result = ORIGINAL_CENSUS(*args, **kwargs)
        proofs.append((result.verified_empty, unavailable, tuple(states)))
        return result
    monkeypatch.setattr(procs, "snapshot", snapshot)
    monkeypatch.setattr(procs, "_read", read)
    monkeypatch.setattr(procs, "containment", census)
    try:
        was_released = False
        for action in actions:
            if action.startswith("exit"):
                states[int(action[-1])] = "gone"
            elif action.startswith("reuse"):
                states[int(action[-1])] = "reused"
            elif action == "unverifiable":
                unavailable = True
            elif action == "verifiable":
                unavailable = False
            elif action == "restart":
                current.close()
                # Construct without real inspection in this deterministic test.
                monkeypatch.setattr(procs, "_read", original_read)
                current = Daemon(harness.root)
                current.policy["quarantine_recheck_s"] = 10
                monkeypatch.setattr(procs, "_read", read)
            elif action == "kill":
                current._resolve_quarantine({"attempt_id": aid}, protocol.KillArgs(job, confirm_dead=True))
            else:
                clock.advance()
                current._recheck_quarantines()
            released = current.store.get_attempt(aid)["state"] != "quarantined"
            events = [e for e in current.store.list_events(job) if e["kind"] in {"quarantine.self_resolved", "quarantine.confirmed_dead"}]
            assert len(events) <= 1
            assert released == bool(events)
            if released:
                if not was_released:
                    verified, unknown, observed = proofs[-1]
                    assert verified and not unknown and all(s != "live" for s in observed)
                assert not current.store.one("SELECT 1 FROM leases WHERE holder=?", (aid,))
                data = json.loads(events[0]["data_json"])
                assert not data["override"] and not data["containment"]["unverifiable"] and not data["containment"]["live_pids"]
            was_released = released
    finally:
        if current is not daemon:
            current.close()
        # The function-scoped fixture spans examples; reopen it for the next
        # generated sequence after testing actual Daemon.close()/construction.
        if daemon._closed:
            daemon.__init__(harness.root)
