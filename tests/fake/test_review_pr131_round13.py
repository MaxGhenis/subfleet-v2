"""Review round 13 of #131 (head d284f15cf): executed witnesses.

Fake process tables and synthetic censuses only; no real process is signalled.
Tests named `test_r13_finding_*` assert the behaviour the review asks for and
fail on d284f15cf; tests named `test_r13_check_*` pin behaviour the review
confirms (merge, push, #144 waiters, D-CK1, D-PR1 pacing).
"""

import dataclasses
import json
import time

import pytest

from subfleet import daemon as dm, procs, protocol
from subfleet.daemon import Daemon
from tests.fake.test_review_pr131_probes import BOOT, NEXT_BOOT, script_table
from tests.fake.test_quarantine_self_resolve import Clock
from tests.fake.test_state_contract import receipt_fixture, reserve, state_daemon  # noqa: F401

D_ST2_BOUND = 4 * 1024 * 1024            # tests/fake/test_status_payload.py's reply bound


def census(pids, round_=0, *, boot=BOOT):
    """A full census of `pids`, shaped as procs.containment returns one."""
    pids = list(pids)
    half = len(pids) // 2
    ids = {pid: procs.ProcessIdentity(pid, boot, f"Sat Oct 11 {round_ % 24:02d}:{pid // 60 % 60:02d}:{pid % 60:02d} 2026")
           for pid in pids}
    shapes = {pid: {"ppid": pid - 1, "pgid": pids[0] if i < half else pid, "stat": "S"} for i, pid in enumerate(pids)}
    return procs.Containment(
        group_pids=frozenset(pids[:half]), descendant_pids=frozenset(pids[half:]), unverifiable=False,
        identities=ids, shapes=shapes, lineage_boot_ids=(boot,),
        lineage_roots=tuple(procs.CensusRoot(pid, boot, ids[pid].proc_start, shapes[pid]["pgid"]) for pid in pids),
        provider_identities=(ids[pids[0]],), descendant_identities=tuple(ids[pid] for pid in pids[half:]))


def status_bytes(daemon, monkeypatch):
    monkeypatch.setattr(Daemon, "_desktop_identity", lambda self: None)
    monkeypatch.setattr(Daemon, "_desktop_in_use", lambda self: False)
    monkeypatch.setattr(Daemon, "_priority_callers_status", lambda self: [])
    result = daemon.dispatch("daemon.status", {})
    return len(protocol.encode(protocol.Response("r13", True, result=result))), result


def clone_quarantined(daemon, row, n):
    """`n` more quarantined attempts carrying `row`'s quarantine_reason."""
    with daemon.store.transaction("fixture.r13.clone") as tx:
        job = dict(tx.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone())
        for i in range(n):
            j = f"r13-clone-{i:04d}"
            tx.execute("INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,sandbox,"
                       "created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (j, j, "x", "dispatch", "lost", job["workdir"], job["prompt_path"], "read-only",
                        job["created_at"], "2026-10-01T00:00:00.000000Z"))
            tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,reserved_at,"
                       "finished_at,quarantine_reason,quarantine_recheck_at,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (j + "/a1", j, 1, row["lane_id"], row["model_requested"], "quarantined", row["reserved_at"],
                        "2026-10-01T00:00:00.000000Z", row["quarantine_reason"], row["quarantine_recheck_at"], "{}"))


# --- item 3: the status reply ---------------------------------------------------

@pytest.mark.parametrize("live_pids,rechecks,rows", [(300, 0, 40), (300, 14, 8), (4096, 1, 3)])
def test_r13_finding_status_reply_stays_bounded_with_large_quarantines(state_daemon, monkeypatch,
                                                                       live_pids, rechecks, rows):
    """One attempt is quarantined through `_quarantine`, then rechecked through
    `_resolve_quarantine_once` with a census of fresh identities each time
    (process churn), growing `identity_history` to LINEAGE_ROOT_LIMIT. Its
    `quarantine_reason` is the daemon's own; the other rows carry the same text.
    """
    daemon, harness = state_daemon
    _, a, _ = reserve(daemon, harness)
    pids = range(40000, 40000 + live_pids)
    daemon._quarantine(a, census(pids, 0), "termination could not verify containment")
    if rechecks:
        # The quadratic merge (next test) makes each real recheck cost seconds to
        # minutes at the cap; the reason it writes is built the same way here.
        previous = json.loads(daemon.store.get_attempt(a["attempt_id"])["quarantine_reason"])
        history = {}
        for r in range(rechecks):
            for ident in census(pids, r + 1).identities.values():
                history.setdefault(str(ident.pid), []).append(dataclasses.asdict(ident))
        flat = [value for values in history.values() for value in values][-dm.LINEAGE_ROOT_LIMIT:]
        history = {}
        for value in flat:
            history.setdefault(str(value["pid"]), []).append(value)
        current = census(pids, rechecks + 1).to_dict()
        detail = {**previous, **current, "identity_history": history}
        daemon.store.update_attempt(a["attempt_id"], quarantine_reason=json.dumps(detail))
    row = daemon.store.get_attempt(a["attempt_id"])
    clone_quarantined(daemon, row, rows - 1)
    size, result = status_bytes(daemon, monkeypatch)
    reasons = sum(len(r["quarantine_reason"] or "") for r in result["attempts"])
    print(f"\nR13 status: {rows} quarantined rows, {live_pids} live pids, history={rechecks > 0}: "
          f"reason {len(row['quarantine_reason']):,} B/row, quarantine_reason total {reasons:,} B, reply {size:,} B")
    assert size < D_ST2_BOUND, f"daemon.status reply {size:,} bytes with {rows} quarantined rows"
    for row in result["attempts"]:
        summary = json.loads(row["quarantine_reason"])
        assert summary["live_pid_count"] == live_pids
        assert len(summary["live_pids"]) <= dm.QUARANTINE_PID_PREVIEW
        assert not {"identities", "lineage_roots", "identity_history"} & summary.keys()


def test_r13_finding_recent_census_identities_is_not_quadratic(monkeypatch):
    """A recheck at the history cap merges 4,096 + 300 + 300 identities."""
    history = {}
    for i in range(dm.LINEAGE_ROOT_LIMIT):
        pid = 40000 + i % 300
        history.setdefault(str(pid), []).append({"pid": pid, "boot_id": BOOT, "proc_start": f"h{i}"})
    previous = {str(pid): {"pid": pid, "boot_id": BOOT, "proc_start": "p"} for pid in range(40000, 40300)}
    current = {str(pid): {"pid": pid, "boot_id": BOOT, "proc_start": "c"} for pid in range(40000, 40300)}
    calls = [0]
    original = procs.ProcessIdentity.__eq__

    def counted(self, other):
        calls[0] += 1
        return original(self, other)
    monkeypatch.setattr(procs.ProcessIdentity, "__eq__", counted)
    began = time.perf_counter()
    dm._recent_census_identities(history, previous, current)
    elapsed = time.perf_counter() - began
    print(f"\nR13 merge: {calls[0]:,} identity comparisons, {elapsed:.2f} s")
    assert calls[0] < 200_000, f"{calls[0]:,} comparisons ({elapsed:.1f} s) for one recheck's merge"


# --- item 5 / liveness: dead lineage roots ----------------------------------------

def _running(daemon, harness, **overrides):
    job_id, a, adir = reserve(daemon, harness, **overrides)
    daemon.store.update_attempt(a["attempt_id"], state="running", guardian_pid=100, pgid=100,
                                boot_id=BOOT, proc_start="guardian")
    # Current guardians publish their child (C-5.5); without it a legacy start
    # holds as "guardian child publication unavailable" until a receipt exists.
    (adir / "start.json").write_text(json.dumps({"guardian_pid": 100, "boot_id": BOOT,
                                               "proc_start": "guardian", "pgid": 100, "child_pid": 101,
                                               "child_identity": {"pid": 101, "boot_id": BOOT,
                                                                  "proc_start": "provider"}}))
    return job_id, daemon.store.get_attempt(a["attempt_id"]), adir


def test_r13_finding_dead_lineage_roots_do_not_turn_a_long_successful_run_lost(state_daemon, monkeypatch):
    """A long run's paced inspections see 4,100 short-lived tool processes, each
    in its own session (as Codex runs commands), none alive at the end. The
    provider exits 0 and every process is gone: nothing can still be writing."""
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    job_id, a, adir = _running(daemon, harness)
    per = 100
    for r in range(dm.LINEAGE_ROOT_LIMIT // per + 1):
        rows = {100: (1, 100, "Ss", "guardian"), 101: (100, 100, "S", "provider")}
        rows.update({pid: (101, pid, "Ss", f"tool-{pid}") for pid in range(1000 + r * per, 1000 + (r + 1) * per)})
        script_table(monkeypatch, rows)
        daemon._record_owned(daemon.store.get_attempt(a["attempt_id"]), procs.snapshot())
    evidence = json.loads(daemon.store.get_attempt(a["attempt_id"])["evidence_json"])
    print(f"\nR13 overflow premise: {len(evidence['lineage_roots'])} roots kept, "
          f"overflow={evidence.get('lineage_overflow_boot')}")
    script_table(monkeypatch, {})                      # the run is over; nothing lives
    now = [0.0]
    monkeypatch.setattr(dm.time, "monotonic", lambda: now[0])
    a = receipt_fixture(daemon, daemon.store.get_attempt(a["attempt_id"]), adir, rc=0)
    daemon._finalize(a)
    now[0] = daemon.exit_settle_s + 1
    daemon._finalize(daemon.store.get_attempt(a["attempt_id"]))
    actual = daemon.store.get_attempt(a["attempt_id"])
    for _ in range(3):                                 # three automatic paces later
        clock.advance()
        daemon._recheck_quarantines()
    actual = daemon.store.get_attempt(a["attempt_id"])
    job = daemon.store.get_job(job_id)
    leases = [row["lease_key"] for row in daemon.store.list_leases() if row["holder"] in (job_id, a["attempt_id"])]
    print(f"R13 overflow outcome: job {job['state']} rc={job['rc']}; attempt {actual['state']}; leases {leases}; "
          f"reason {json.loads(actual['quarantine_reason'] or '{}').get('errors')}")
    assert (job["state"], job["rc"]) == ("succeeded", 0), (actual["state"], actual["quarantine_reason"][:300])


# --- item 4: D-CK1, D-QC1, D-PR1 ---------------------------------------------------

def _record_signals(monkeypatch):
    sent = []
    monkeypatch.setattr(procs, "signal_group", lambda pgid, sig, **kw: sent.append(("group", pgid, int(sig))) or False)
    monkeypatch.setattr(procs, "signal_process", lambda ident, sig: sent.append(("pid", ident.pid, int(sig))) or False)
    return sent


def test_r13_check_d_ck1_own_session_marker_child_is_held_unsignalled_then_self_resolves(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    job_id, a, adir = _running(daemon, harness, out_path=str(harness.root / "out-ck1.md"))
    marker = f"300 uv sync --frozen SUBFLEET_ATTEMPT={a['attempt_id']} SUBFLEET_ROOT={daemon.root}\n"
    # The provider has just started `uv sync` in a session of its own.
    script_table(monkeypatch, {100: (1, 100, "Ss", "guardian"), 101: (100, 100, "S", "provider"),
                               300: (101, 300, "Ss", "uv-start")}, markers=marker)
    daemon._record_owned(a, procs.snapshot())
    held_before = sorted(row["lease_key"].split(":")[0] for row in daemon.store.list_leases()
                         if row["holder"] in (job_id, a["attempt_id"]))
    # Cancel: the provider dies first; uv is reparented to launchd and keeps running.
    daemon.store.update_job(job_id, cancel_requested_at=dm.utcnow())
    script_table(monkeypatch, {300: (1, 300, "Ss", "uv-start")}, markers=marker)
    sent = _record_signals(monkeypatch)
    monkeypatch.setattr(procs, "same_process", lambda *args: False)
    daemon.term_grace_s = daemon.kill_settle_s = 0
    daemon._kill_attempt(daemon.store.get_attempt(a["attempt_id"]))
    actual = daemon.store.get_attempt(a["attempt_id"])
    evidence = json.loads(actual["evidence_json"])
    reason = json.loads(actual["quarantine_reason"])
    print(f"\nR13 D-CK1: signals {sent}; held {reason['reason']!r} live {reason['live_pids']}; "
          f"300 owned={'300' in evidence.get('owned_identities', {})} "
          f"descendant={any(v['pid'] == 300 for v in evidence.get('descendant_identities', []))}; leases {held_before}")
    assert actual["state"] == "quarantined" and 300 in reason["live_pids"]
    assert not any(target == 300 for _, target, _ in sent), "a lineage-only descendant gets no signal (C-5.6)"
    assert {"out", "worktree"} & set(held_before)
    still = [row for row in daemon.store.list_leases() if row["holder"] in (job_id, a["attempt_id"])]
    assert still, "the quarantine keeps the job's leases while uv lives"
    clock.advance()
    daemon._recheck_quarantines()                     # uv still alive: held
    assert daemon.store.get_attempt(a["attempt_id"])["state"] == "quarantined"
    script_table(monkeypatch, {})                     # uv exits by itself
    clock.advance()
    daemon._recheck_quarantines()
    actual = daemon.store.get_attempt(a["attempt_id"])
    left = [row["lease_key"] for row in daemon.store.list_leases() if row["holder"] in (job_id, a["attempt_id"])]
    notices = [row["text"] for row in daemon.store.query("SELECT text FROM notices WHERE job_id=?", (job_id,))]
    print(f"R13 D-CK1 after exit: attempt {actual['state']}, leases {left}, notices {[n.splitlines()[-1][:80] for n in notices]}")
    assert actual["state"] == "interrupted" and not left
    assert any("released from quarantine" in text for text in notices)


def test_r13_finding_d_qc1_caller_is_told_when_the_recheck_happens(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    Clock(monkeypatch, daemon)
    job_id, a, _ = _running(daemon, harness)
    daemon._quarantine(a, census([42099]), "writers remain after exit receipt")
    actual = daemon.store.get_attempt(a["attempt_id"])
    notice = daemon.store.one("SELECT text FROM notices WHERE job_id=?", (job_id,))["text"]
    why = daemon.dispatch("why", {"job_id": job_id})["text"]
    when = actual["quarantine_recheck_at"]
    print(f"\nR13 D-QC1: next recheck {when}; notice mentions it: {when in notice}; "
          f"why mentions quarantine: {'quarantin' in why}; why text: {why.splitlines()[:3]}")
    command = f"subfleet kill {job_id} --confirm-dead"
    assert when in notice and command in notice, notice[:400]
    assert "quarantin" in why.lower() and when in why and command in why, why
    daemon.store.update_attempt(a["attempt_id"], finished_at="2026-10-01T00:00:00Z")
    _, status = status_bytes(daemon, monkeypatch)
    assert "Next check" in status["text"] and when in status["text"]


def test_r13_check_d_pr1_recycled_group_hold_is_paced_and_only_status_tells(state_daemon, monkeypatch):
    """The attempt's retained root 243 (group 243) died; macOS gave 243 to an
    unrelated daemon that leads its own new group 243 (distnoted in D-PR1)."""
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    job_id, a, _ = _running(daemon, harness)
    root = procs.CensusRoot(243, BOOT, "writer-start", 243)
    daemon.store.update_attempt(a["attempt_id"], evidence_json=json.dumps(
        {"lineage_roots": [dataclasses.asdict(root)]}))
    script_table(monkeypatch, {243: (1, 243, "Ss", "distnoted-start")})
    a = daemon.store.get_attempt(a["attempt_id"])
    daemon._quarantine(a, daemon._contain(a), "writers remain after exit receipt")
    calls = [0]
    contain = daemon._contain

    def counted(row):
        calls[0] += 1
        return contain(row)
    monkeypatch.setattr(daemon, "_contain", counted)
    paces = 6
    for _ in range(paces):
        for _ in range(5):                            # five passes inside one pace
            daemon._recheck_quarantines()
        clock.advance()
    daemon._recheck_quarantines()
    notices = daemon.store.one("SELECT count(*) n FROM notices WHERE job_id=?", (job_id,))["n"]
    size, result = status_bytes(daemon, monkeypatch)
    holds = result["quarantined_attempts"]
    print(f"\nR13 D-PR1: {calls[0]} censuses over {paces} paces (31 passes); notices {notices}; "
          f"state {daemon.store.get_attempt(a['attempt_id'])['state']}; status holds {holds}")
    assert calls[0] == paces
    assert daemon.store.get_attempt(a["attempt_id"])["state"] == "quarantined"
    assert notices == 1


# --- item 2: the merge --------------------------------------------------------------

def test_r13_check_quarantined_push_job_never_pushes_and_frees_its_leases(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    job_id, a, _ = _running(daemon, harness)
    daemon.store.update_job(job_id, push_branch="feat/r13", push_remote="origin", push_default_branch="main")
    published = []
    monkeypatch.setattr(daemon, "_publish_bundle", lambda job: published.append(job["job_id"]) or "0" * 40)
    script_table(monkeypatch, {42099: (1, 42099, "Ss", "writer")},
                 markers=f"42099 w SUBFLEET_ATTEMPT={a['attempt_id']}\n")
    a = daemon.store.get_attempt(a["attempt_id"])
    daemon._quarantine(a, daemon._contain(a), "writers remain after exit receipt")
    # Only accepted jobs are exported (`PENDING_EXPORTS`, `_finalize`), and only
    # an export schedules the push thread; `_push_job` is called directly too.
    exports = [row["job_id"] for row in daemon.store.query(dm.PENDING_EXPORTS)]
    daemon._push_job(job_id)
    leases = [row["lease_key"] for row in daemon.store.list_leases() if row["holder"] in (job_id, a["attempt_id"])]
    assert job_id not in exports and not any(key.startswith("push:") for key in leases)
    script_table(monkeypatch, {})
    clock.advance()
    daemon._recheck_quarantines()
    assert job_id not in [row["job_id"] for row in daemon.store.query(dm.PENDING_EXPORTS)]
    daemon._push_job(job_id)
    job = daemon.store.get_job(job_id)
    left = [row["lease_key"] for row in daemon.store.list_leases() if row["holder"] in (job_id, a["attempt_id"])]
    pushes = daemon.store.query("SELECT * FROM job_pushes WHERE job_id=?", (job_id,))
    print(f"\nR13 push: before release {leases}; after {left}; job {job['state']} accepted={job['accepted_attempt_id']}; "
          f"pushes {pushes}; published {published}")
    assert not published and not pushes and not left and job["accepted_attempt_id"] is None


def test_r13_check_redispatch_to_a_quarantined_out_path_is_accepted_after_self_resolve(state_daemon, monkeypatch):
    """D-CK1's reporter: a re-dispatch to the same -o path was refused while the
    quarantine stood. Submit refuses (no lease waiter forms for an -o path); the
    paced recheck releases the lease and the same submit is accepted and placed."""
    from subfleet.adapters.base import AdapterError
    daemon, harness = state_daemon
    clock = Clock(monkeypatch, daemon)
    out = str(harness.root / "out-shared.md")
    job_a, a, _ = _running(daemon, harness, out_path=out)
    script_table(monkeypatch, {42099: (1, 42099, "Ss", "writer")},
                 markers=f"42099 w SUBFLEET_ATTEMPT={a['attempt_id']}\n")
    a = daemon.store.get_attempt(a["attempt_id"])
    daemon._quarantine(a, daemon._contain(a), "writers remain after exit receipt")
    with pytest.raises(AdapterError, match="output path is held") as refusal:
        daemon.dispatch("submit", harness.submit_args(out_path=out))
    next_check = daemon.store.get_attempt(a["attempt_id"])["quarantine_recheck_at"]
    assert next_check in refusal.value.fix
    assert f"subfleet kill {job_a} --confirm-dead" in refusal.value.fix
    script_table(monkeypatch, {})
    clock.advance()
    daemon._recheck_quarantines()
    job_b = daemon.dispatch("submit", harness.submit_args(out_path=out))["job_id"]
    daemon._admit()
    print(f"\nR13 re-dispatch: A {daemon.store.get_attempt(a['attempt_id'])['state']}; "
          f"B {[row['state'] for row in daemon.store.list_attempts(job_b)]}")
    assert daemon.store.list_attempts(job_b)


# --- item 5 / cost: the round-12 foreign ownership read ------------------------------

def test_r13_finding_inspection_cost_does_not_grow_with_terminal_history(state_daemon, monkeypatch):
    """C-5.11: a paced inspection costs the same however much history is kept.
    300 finished attempts, each with the 50 descendants it once recorded; one
    running attempt with 20 tool processes. Count identity comparisons and
    receipt reads in one `_record_owned` (every running attempt, every 1 s)."""
    daemon, harness = state_daemon
    _, a, adir = _running(daemon, harness)
    lane = a["lane_id"]
    with daemon.store.transaction("fixture.r13.ledger") as tx:
        job = dict(tx.execute("SELECT * FROM jobs WHERE job_id=?", (a["job_id"],)).fetchone())
        for i in range(300):
            j, base = f"r13-done-{i:04d}", 1000 + i * 60
            evidence = {"owned_identities": {str(base): {"pid": base, "boot_id": BOOT, "proc_start": f"g{i}"}},
                        "descendant_identities": [{"pid": base + 1 + k, "boot_id": BOOT, "proc_start": f"d{i}-{k}"}
                                                  for k in range(50)]}
            tx.execute("INSERT INTO jobs(job_id,request_id,payload_digest,kind,state,workdir,prompt_path,sandbox,"
                       "created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (j, j, "x", "dispatch", "succeeded", job["workdir"], job["prompt_path"], "read-only",
                        job["created_at"], job["created_at"]))
            tx.execute("INSERT INTO attempts(attempt_id,job_id,seq,lane_id,model_requested,state,reserved_at,"
                       "finished_at,guardian_pid,pgid,boot_id,proc_start,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (j + "/a1", j, 1, lane, a["model_requested"], "succeeded", a["reserved_at"], a["reserved_at"],
                        base, base, BOOT, f"g{i}", json.dumps(evidence)))
    rows = {100: (1, 100, "Ss", "guardian"), 101: (100, 100, "S", "provider")}
    rows.update({pid: (101, pid, "Ss", f"tool-{pid}") for pid in range(500, 520)})
    script_table(monkeypatch, rows)
    comparisons, reads = [0], [0]
    overlap, read_json = procs.identities_overlap, daemon._read_json

    def counted_overlap(left, right):
        comparisons[0] += 1
        return overlap(left, right)

    def counted_read(path):
        reads[0] += 1
        return read_json(path)
    monkeypatch.setattr(procs, "identities_overlap", counted_overlap)
    monkeypatch.setattr(daemon, "_read_json", counted_read)
    began = time.perf_counter()
    daemon._record_owned(daemon.store.get_attempt(a["attempt_id"]), procs.snapshot())
    elapsed = time.perf_counter() - began
    print(f"\nR13 inspection: {comparisons[0]:,} identity comparisons, {reads[0]} receipt reads, {elapsed:.2f} s "
          f"for one _record_owned with 300 finished attempts")
    assert comparisons[0] < 5_000 and reads[0] < 10, (comparisons[0], reads[0], elapsed)


def test_r13_finding_a_new_foreign_claim_can_only_suppress_cached_signal_authority(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    _, ours, _ = _running(daemon, harness)
    _, other, _ = reserve(daemon, harness)
    script_table(monkeypatch, {100: (1, 100, "Ss", "guardian"),
                             202: (1, 202, "S", "writer")})
    target = procs.ProcessIdentity(202, BOOT, "writer")
    foreign = daemon._foreign_ownership(ours, procs.snapshot())
    observed = procs.Containment(identities={202: target}, group_pids=frozenset({202}))
    assert daemon._unambiguous_signal_identity(ours["attempt_id"], target, observed, {}, foreign)
    daemon.store.update_attempt(other["attempt_id"], state="succeeded", evidence_json=json.dumps(
        {"owned_identity_history": {"202": [dataclasses.asdict(target)]}}))
    assert not daemon._unambiguous_signal_identity(ours["attempt_id"], target, observed, {}, foreign)
    refreshed = daemon._foreign_ownership(ours, procs.snapshot())
    assert refreshed.claimed(target) and not refreshed.identities


def test_r13_finding_status_guard_accepts_legacy_plain_text(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    _, a, _ = reserve(daemon, harness)
    daemon.store.update_attempt(a["attempt_id"], state="quarantined", quarantine_reason="legacy hold",
                                finished_at="2026-10-01T00:00:00Z")
    _, result = status_bytes(daemon, monkeypatch)
    [shown] = result["attempts"]
    summary = json.loads(shown["quarantine_reason"])
    assert summary["reason"] == "legacy hold" and summary["live_pid_count"] == 0
    assert summary["live_pids"] == []


def test_r13_finding_status_never_uses_a_census_as_reason_fallback(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    _, a, _ = reserve(daemon, harness)
    daemon.store.update_attempt(a["attempt_id"], state="quarantined",
                                quarantine_reason=json.dumps(census([42099]).to_dict()),
                                finished_at="2026-10-01T00:00:00Z")
    _, result = status_bytes(daemon, monkeypatch)
    [shown] = result["attempts"]
    summary = json.loads(shown["quarantine_reason"])
    assert summary["reason"] == "awaiting a verified-empty census"
    assert summary["live_pid_count"] == 1
    assert set(summary) == {"reason", "errors", "unverifiable", "live_pid_count", "live_pids"}


def test_r13_finding_legacy_finished_child_claim_is_imported_once(state_daemon, monkeypatch):
    daemon, harness = state_daemon
    _, a, adir = reserve(daemon, harness)
    child = {"pid": 202, "boot_id": BOOT, "proc_start": "child-start"}
    (adir / "start.json").write_text(json.dumps({"child_identity": child}))
    daemon.store.update_attempt(a["attempt_id"], state="succeeded", child_pid=None)
    with daemon.store.transaction("fixture.legacy.import") as tx:
        tx.execute("UPDATE process_claim_receipts SET seeded=0 WHERE singleton=1")
    daemon._seed_process_claim_receipts()
    script_table(monkeypatch, {202: (1, 202, "Ss", "child-start")})
    foreign = daemon._foreign_ownership({"attempt_id": "another"}, procs.snapshot())
    assert foreign.claimed(procs.ProcessIdentity(**child))
    assert not foreign.identities
    monkeypatch.setattr(daemon, "_read_json", lambda path: pytest.fail("receipt import repeated"))
    daemon._seed_process_claim_receipts()
    assert daemon._foreign_ownership({"attempt_id": "another"}, procs.snapshot()).claimed(procs.ProcessIdentity(**child))
