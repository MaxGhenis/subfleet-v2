"""Review probes for PR #131 (C-5.7 self-resolution). Not part of the PR.

Every probe uses the real `procs.containment` and the real resolver; only the
process table, the marker scan and time are scripted. Names say what a probe
expects of a safe implementation; a failure is a finding.
"""
import json

import pytest

from subfleet import daemon as dm, procs, protocol
from tests.fake.test_state_contract import state_daemon  # noqa: F401

ORIGINAL_CENSUS = procs.containment
BOOT = "6F1C0F2E-1111-4222-8333-944455556666"
NEXT_BOOT = "7F1C0F2E-1111-4222-8333-944455556666"


def ident(pid, start, boot=BOOT):
    return {"pid": pid, "boot_id": boot, "proc_start": start}


def script_table(monkeypatch, rows, *, markers="", boot=BOOT, table_fails=False, markers_fail=False):
    """rows: pid -> (ppid, pgid, stat, lstart). `markers` is the `ps -axEww` text."""
    def snapshot():
        if table_fails:
            raise procs.InspectionError("ps failed")
        return procs.ProcessTable(dict(rows), boot_id=boot)

    def read(argv, **kwargs):
        if "pid=,command=" in argv:
            if markers_fail:
                raise procs.InspectionError("marker enumeration unavailable")
            return markers
        if "stat=" in argv:          # `_stat` for a marker pid the table lacks
            pid = int(argv[argv.index("-p") + 1])
            return rows[pid][2] if pid in rows else ""
        raise AssertionError(argv)

    monkeypatch.setattr(procs, "snapshot", snapshot)
    monkeypatch.setattr(procs, "_read", read)
    monkeypatch.setattr(procs, "containment", ORIGINAL_CENSUS)


def quarantine(daemon, harness, *, guardian=100, held=None, owned=None):
    """A quarantined read-only attempt whose guardian led group `guardian`."""
    from tests.fake.test_state_contract import reserve
    job_id, a, adir = reserve(daemon, harness)
    daemon.store.update_attempt(a["attempt_id"], guardian_pid=guardian, pgid=guardian,
                                boot_id=BOOT, proc_start="guardian-start",
                                evidence_json=json.dumps({"owned_identities": owned or {}}))
    a = daemon.store.get_attempt(a["attempt_id"])
    census = procs.Containment(marker_pids=frozenset(int(p) for p in (held or {})),
                               identities={int(p): procs.ProcessIdentity(**v) for p, v in (held or {}).items()})
    daemon._quarantine(a, census, "writers remain after exit receipt")
    return daemon.store.get_attempt(a["attempt_id"])


def confirm_dead(daemon, a):
    daemon._resolve_quarantine(a, protocol.KillArgs(a["job_id"], confirm_dead=True))
    return daemon.store.get_attempt(a["attempt_id"])["state"]


# --- 1. Safety: a live process in the attempt's own process group -------------

def test_a_live_group_member_on_a_recycled_recorded_pid_keeps_the_quarantine(state_daemon, monkeypatch):
    """Guardian 100 led group 100 and has exited. While the attempt ran, the daemon
    recorded group member 200 (`owned_identities`, start "w-old"), which also exited.
    A later writer in group 100 (its parent, another writer, has since exited) was
    given pid 200 again after the pid space wrapped. It has no marker.

    Group 100 still has a live member, so C-5.5's group source is not empty. XNU
    cannot hand out pid 100 while group 100 has members, so this process is ours."""
    daemon, harness = state_daemon
    a = quarantine(daemon, harness, owned={"200": ident(200, "w-old")})
    script_table(monkeypatch, {200: (1, 100, "S", "w-new")})
    census = daemon._contain(a)
    assert census.group_pids == {200}, census.to_dict()
    assert not census.verified_empty
    assert confirm_dead(daemon, a) == "quarantined"


def test_automatic_pass_also_releases_with_that_group_member_live(state_daemon, monkeypatch):
    """The same scenario through the paced pass: the leases go while pid 200 writes."""
    daemon, harness = state_daemon
    a = quarantine(daemon, harness, owned={"200": ident(200, "w-old")})
    daemon.store.update_attempt(a["attempt_id"], quarantine_recheck_at="")
    script_table(monkeypatch, {200: (1, 100, "S", "w-new")})
    daemon._recheck_quarantines()
    assert daemon.store.get_attempt(a["attempt_id"])["state"] == "quarantined"



def test_the_same_census_miss_reaches_a_running_attempts_finalization(state_daemon, monkeypatch):
    """`_contain` serves `_finalize` (C-5.9) and the kill protocol (C-5.6) too: a running
    attempt's `owned_identities` are passed as recorded writers there as well."""
    from tests.fake.test_state_contract import reserve
    daemon, harness = state_daemon
    job_id, a, adir = reserve(daemon, harness)
    daemon.store.update_attempt(a["attempt_id"], state="running", guardian_pid=100, pgid=100, boot_id=BOOT,
                                proc_start="guardian-start",
                                evidence_json=json.dumps({"owned_identities": {"200": ident(200, "w-old")}}))
    script_table(monkeypatch, {200: (1, 100, "S", "w-new")})
    assert not daemon._contain(daemon.store.get_attempt(a["attempt_id"])).verified_empty

# --- 2. Safety: a writer recorded by the quarantine census itself --------------

def test_a_writer_the_quarantine_census_recorded_holds_even_if_an_older_owned_identity_shares_its_pid(
        state_daemon, monkeypatch):
    """While running, group member 300 (start "p1") was recorded as owned and exited.
    Pid 300 was reused by a descendant P2 (start "p2"), which daemonised (setsid) and
    was live at quarantine, so the quarantine census recorded 300 -> "p2". P2's parent
    has exited and P2 carries no marker. Only its recorded identity names it.

    `_contain` merges `{**held, **owned}`, so the stale owned "p1" wins and P2,
    a writer the quarantine itself recorded, is treated as gone."""
    daemon, harness = state_daemon
    a = quarantine(daemon, harness, held={"300": ident(300, "p2")}, owned={"300": ident(300, "p1")})
    script_table(monkeypatch, {300: (1, 300, "Ss", "p2")})
    census = daemon._contain(a)
    assert 300 in census.live_pids, census.to_dict()
    assert confirm_dead(daemon, a) == "quarantined"


# --- 3. The cases the brief names, which should hold or release ---------------

def test_pid_reuse_with_the_same_start_string_holds(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    a = quarantine(daemon, harness, held={"42099": ident(42099, "old-start")})
    script_table(monkeypatch, {42099: (1, 42099, "S", "old-start")})   # cannot tell: treated as ours
    assert not daemon._contain(a).verified_empty
    assert confirm_dead(daemon, a) == "quarantined"


@pytest.mark.parametrize("table_fails,markers_fail", [(True, False), (False, True)])
def test_a_partly_unverifiable_census_holds(state_daemon, monkeypatch, table_fails, markers_fail):
    daemon, harness = state_daemon
    a = quarantine(daemon, harness, held={"42099": ident(42099, "old-start")})
    script_table(monkeypatch, {}, table_fails=table_fails, markers_fail=markers_fail)
    census = daemon._contain(a)
    assert census.unverifiable and not census.verified_empty
    assert confirm_dead(daemon, a) == "quarantined"


def test_an_unknown_legacy_boot_identity_on_a_matching_start_holds(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    a = quarantine(daemon, harness, held={"42099": ident(42099, "old-start", boot="1700000000")})
    script_table(monkeypatch, {42099: (1, 42099, "S", "old-start")}, boot="1700000123")
    monkeypatch.setattr(procs.ProcessTable, "legacy_seconds", lambda self: "1700000123")
    census = daemon._contain(a)
    assert census.errors and not census.verified_empty


def test_after_a_reboot_recorded_pids_are_gone_and_a_reused_leader_group_is_ignored(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    a = quarantine(daemon, harness, held={"42099": ident(42099, "old-start")})
    # New boot: pid 100 leads an unrelated group, 42099 is an unrelated process.
    script_table(monkeypatch, {100: (1, 100, "Ss", "new-boot-a"), 101: (100, 100, "S", "new-boot-b"),
                               42099: (1, 42099, "S", "new-boot-c")}, boot=NEXT_BOOT)
    assert daemon._contain(a).verified_empty
    assert confirm_dead(daemon, a) == "lost"


def test_after_a_reboot_a_marked_process_still_holds(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    a = quarantine(daemon, harness, held={"42099": ident(42099, "old-start")})
    marker = f"SUBFLEET_ATTEMPT={a['attempt_id']} SUBFLEET_ROOT={daemon.root}"
    script_table(monkeypatch, {555: (1, 555, "S", "new")}, markers=f"555 node {marker}\n", boot=NEXT_BOOT)
    assert daemon._contain(a).marker_pids == {555}
    assert confirm_dead(daemon, a) == "quarantined"


def test_a_child_forked_by_a_live_recorded_writer_is_found_through_it(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    a = quarantine(daemon, harness, held={"42099": ident(42099, "old-start")})
    # The escaped recorded writer (its own session) forked 43000 after it escaped.
    script_table(monkeypatch, {42099: (1, 42099, "Ss", "old-start"), 43000: (42099, 42099, "S", "kid")})
    census = daemon._contain(a)
    assert {42099, 43000} <= census.live_pids


# --- 4. Liveness: an unrecorded child pid now used by an unrelated process ----

def test_an_unrelated_process_on_the_child_pid_does_not_hold_for_ever(state_daemon, monkeypatch):
    """The provider exited before quarantine. Its launch receipt proves that
    the unrelated long-lived process at its PID has a different identity.
    Without that evidence, calling a bare PID unrelated cannot be safe."""
    daemon, harness = state_daemon
    a = quarantine(daemon, harness, held={"42099": ident(42099, "old-start")})
    daemon.store.update_attempt(a["attempt_id"], child_pid=500)
    (daemon.root / "jobs" / a["job_id"] / "a1" / "start.json").write_text(json.dumps({
        "child_pid": 500, "child_identity": ident(500, "provider-start")}))
    a = daemon.store.get_attempt(a["attempt_id"])
    script_table(monkeypatch, {500: (1, 500, "Ss", "unrelated")})
    census = daemon._contain(a)
    # A changed PID identity says nothing about unobserved descendants.
    assert not census.live_pids and not census.verified_empty, census.to_dict()


# --- 5. Salvage on automatic release: a worktree whose admin dir was pruned ---

def test_a_pruned_worktree_is_released_with_its_salvage_error_and_left_on_disk(state_daemon, monkeypatch):
    import shutil
    from tests.fake.test_state_contract import reserve
    from tests.fake.test_workspace_contract import repository
    from tests.unit.test_salvage import git
    daemon, harness = state_daemon
    repository(daemon, harness)
    linked = harness.root / "linked-wt"
    git(harness.workdir, "worktree", "add", "-b", "task/linked", str(linked))
    job_id, a, adir = reserve(daemon, harness, sandbox="workspace-write", in_place=True,
                              out_path=str(harness.root / "out-pruned.md"))
    daemon.store.update_job(job_id, worktree=str(linked))
    (linked / "tracked.txt").write_text("unsaved writer progress\n")
    shutil.rmtree(harness.workdir / ".git" / "worktrees" / "linked-wt")      # the admin dir, pruned
    census = procs.Containment(marker_pids=frozenset({42099}),
                               identities={42099: procs.ProcessIdentity(42099, BOOT, "old-start")})
    daemon._quarantine(daemon.store.get_attempt(a["attempt_id"]), census, "writers remain after exit receipt")
    daemon.store.update_attempt(a["attempt_id"], quarantine_recheck_at="")
    script_table(monkeypatch, {}, boot=NEXT_BOOT)
    daemon._recheck_quarantines()
    row = daemon.store.get_attempt(a["attempt_id"])
    assert row["state"] == "lost"
    assert not [l for l in daemon.store.list_leases() if l["holder"] in {job_id, a["attempt_id"]}]
    error = json.loads(row["evidence_json"])["salvage_error"]
    assert "not a git repository" in error, error
    assert (linked / "tracked.txt").read_text() == "unsaved writer progress\n"
    [*_, text] = [n["text"] for n in daemon.store.list_notices() if n["job_id"] == job_id]
    print("\nNOTICE:\n" + text)
    assert "released from quarantine" in text and "salvage failed" in text
    assert f"the worktree is kept: {linked}" in text and "salvage saved" not in text


# --- 6. A quarantine reason that is not JSON (the PR's own migration fixture) ---

def test_a_plain_text_quarantine_reason_does_not_wedge_resolution(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    a = quarantine(daemon, harness)
    daemon.store.update_attempt(a["attempt_id"], quarantine_reason="held", quarantine_recheck_at="")
    script_table(monkeypatch, {}, boot=NEXT_BOOT)
    daemon._recheck_quarantines()
    assert daemon.store.get_attempt(a["attempt_id"])["state"] == "lost"


# --- 7. The notice-pending pin: archived, deleted, and a failing conversation store ---

@pytest.mark.parametrize("fate", ["archived", "deleted", "store-fails-then-recovers"])
def test_a_released_turns_notice_never_stays_pending(state_daemon, monkeypatch, fate):
    from subfleet.conversations.store import utcnow as conv_now
    from tests.fake.test_quarantine_self_resolve import (Clock, LIVE, assert_released, new_turn,
                                                         scripted_census)
    from tests.fake.test_workspace_contract import repository
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    repository(daemon, harness)
    cid, mid, job = new_turn(daemon, harness)
    daemon._admit_turns()
    [a] = daemon.store.list_attempts(job)
    (daemon.root / "jobs" / job / "a1").mkdir(exist_ok=True)
    daemon._quarantine(a, LIVE, "writers remain after exit receipt")
    conv = daemon.conversations.store
    if fate == "archived":
        conv.update_conversation(cid, archived_at=conv_now())
    elif fate == "deleted":
        with conv._lock:
            conv._db.execute("PRAGMA foreign_keys=OFF")
            conv._db.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
            conv._db.execute("DELETE FROM conversations WHERE conversation_id=?", (cid,))
            conv._db.execute("PRAGMA foreign_keys=ON")
    calls = []
    if fate == "store-fails-then-recovers":
        real = conv.append_events
        def flaky(**kwargs):
            calls.append(1)
            if len(calls) <= 2:
                raise OSError("disk I/O error")
            return real(**kwargs)
        monkeypatch.setattr(conv, "append_events", flaky)
    scripted_census(monkeypatch, daemon, writer=None)
    clock.advance()
    daemon._recheck_quarantines()
    assert_released(daemon, a)
    for _ in range(3):
        clock.advance()
        daemon._recheck_quarantines()
    assert daemon.store.get_attempt(a["attempt_id"])["quarantine_notice_pending"] == 0
    lines = [e for e in conv.query("SELECT data_json FROM events WHERE attempt_id=? AND position='cmd:quarantine-release'",
                                   (a["attempt_id"],))]
    assert len(lines) == 1
    if calls:
        assert len(calls) == 3


# --- 8. The quarantined turn's own conversation after the release ------------

def test_the_conversation_of_a_released_turn_dispatches_its_next_message(state_daemon, monkeypatch):
    """Turn 1 is quarantined. The person sends message 2, which waits: turn 1's job
    still holds its leases (C-24.5). The writers exit and the daemon releases turn 1
    by itself, then writes "its leases are free" into this conversation. Message 2
    must now get its turn, with the conversation not blocked."""
    import uuid
    from tests.fake.test_quarantine_self_resolve import (Clock, LIVE, assert_released, new_turn,
                                                         scripted_census)
    from tests.fake.test_workspace_contract import repository
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    repository(daemon, harness)
    service = daemon.conversations
    cid, mid, job = new_turn(daemon, harness)
    daemon._admit_turns()
    [a] = daemon.store.list_attempts(job)
    (daemon.root / "jobs" / job / "a1").mkdir(exist_ok=True)
    daemon._quarantine(a, LIVE, "writers remain after exit receipt")
    second = str(uuid.uuid4())
    service.op_message_submit({"conversation_id": cid, "message_id": second, "text": "next", "after_message_id": mid}, None)
    for _ in range(3):
        service.tick()
    print("\nmessage 1:", service.store.message(mid)["state"], service.store.message(mid)["state_reason"],
          "| message 2:", service.store.message(second)["state"], "| job 1:", daemon.store.get_job(job)["state"])
    blocked = service.store.conversation(cid)["blocked_by"]
    print("\nblocked_by before release:", blocked)
    assert service._turn_job(service.store.message(second)) is None         # waits on turn 1's leases
    scripted_census(monkeypatch, daemon, writer=None)
    clock.advance()
    daemon._recheck_quarantines()
    assert_released(daemon, a)
    for _ in range(3):
        service.tick()
    conversation = service.store.conversation(cid)
    print("blocked_by after release:", conversation["blocked_by"],
          "| message 2:", service.store.message(second)["state"], "| job:", service._turn_job(service.store.message(second)))
    assert conversation["blocked_by"] is None and service._turn_job(service.store.message(second))


# --- 9. An operator `kill --confirm-dead` racing the automatic pass ------------

@pytest.mark.parametrize("first", ["automatic", "operator"])
def test_operator_and_automatic_resolution_race_resolves_once_and_salvages_once(state_daemon, monkeypatch, first):
    import threading
    from tests.fake.test_quarantine_self_resolve import Clock, quarantined, scripted_census
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    job, a, adir = quarantined(daemon, harness, writable=True)
    scripted_census(monkeypatch, daemon, writer=None)
    clock.advance()
    salvages, real_salvage = [], dm.salvage
    monkeypatch.setattr(dm, "salvage", lambda *args, **kwargs: salvages.append(1) or real_salvage(*args, **kwargs))
    inside, go = threading.Event(), threading.Event()
    real_contain, calls = daemon._contain, []
    def contain(row):
        calls.append(threading.current_thread().name)
        if len(calls) == 1:
            inside.set()
            assert go.wait(30)
        return real_contain(row)
    monkeypatch.setattr(daemon, "_contain", contain)
    run_auto = lambda: daemon._recheck_quarantines()
    run_operator = lambda: daemon._resolve_quarantine(a, protocol.KillArgs(job, confirm_dead=True))
    one, two = (run_auto, run_operator) if first == "automatic" else (run_operator, run_auto)
    t1 = threading.Thread(target=one, name="first"); t1.start()
    assert inside.wait(30)
    t2 = threading.Thread(target=two, name="second"); t2.start()
    t2.join(2)
    # An operator's request waits on the lock; an automatic pass after an operator's
    # claim finds the attempt not due (its pace was claimed first) and returns.
    assert t2.is_alive() == (first == "automatic")
    go.set(); t1.join(30); t2.join(30)
    assert calls == ["first"]                # the second re-read the row and did nothing
    kinds = [e["kind"] for e in daemon.store.list_events(job)
             if e["kind"] in ("quarantine.self_resolved", "quarantine.confirmed_dead") and e["data_json"] != "{}"]
    assert kinds == (["quarantine.self_resolved"] if first == "automatic" else ["quarantine.confirmed_dead"])
    assert salvages == [1]
    # Base behaviour: an operator release whose salvage fully succeeded adds no notice.
    assert len([n for n in daemon.store.list_notices() if n["job_id"] == job]) == (2 if first == "automatic" else 1)


# --- 10. One attempt's failure in a pass never starves the others ----------

def test_a_raising_resolution_does_not_starve_the_batch_and_keeps_its_pace(state_daemon, monkeypatch):
    from tests.fake.test_quarantine_self_resolve import Clock, LIVE, scripted_census
    from tests.fake.test_state_contract import reserve
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    rows = []
    for _ in range(3):
        _, a, _ = reserve(daemon, harness, out_path=str(harness.root / f"out-{len(rows)}.md"))
        daemon._quarantine(a, LIVE, "writers remain after exit receipt")
        rows.append(daemon.store.get_attempt(a["attempt_id"]))
    scripted_census(monkeypatch, daemon, writer=None)
    real, broken, calls = daemon._contain, rows[1]["attempt_id"], []
    def contain(row):
        calls.append(row["attempt_id"])
        if row["attempt_id"] == broken:
            raise KeyError("unexpected evidence shape")
        return real(row)
    monkeypatch.setattr(daemon, "_contain", contain)
    clock.advance()
    daemon._recheck_quarantines()
    states = [daemon.store.get_attempt(r["attempt_id"])["state"] for r in rows]
    assert states == ["lost", "quarantined", "lost"]
    daemon._recheck_quarantines()                     # same instant: not retried before its pace
    assert calls.count(broken) == 1
    clock.advance()
    daemon._recheck_quarantines()
    assert calls.count(broken) == 2
