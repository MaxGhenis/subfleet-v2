"""C-4.7: a provider the host's shutdown ended is retried, and a job's first three are not charged.

Incident: 2026-09-30, the host restarted at 01:44Z. Fourteen running attempts
ended at 01:44:41Z with rc 143 (SIGTERM); the next daemon recorded eleven of them
`unknown: rc 143, no classifying evidence`, which C-4.5 never retries, and three
`limited` or `transient` from the agent's own prose, each on its job's last
attempt. Every one of those jobs ended `failed`.

The in-process cases build a daemon whose boot, and when that boot began, are
synthetic (`procs.boot_id`, `procs.boot_time`), with the `daemon.lock` record of
the last daemon of the boot before, and drive attempts through admission and
finalization with receipts as guardians write them; no process is started. The
end-to-end cases run the daemon, real guardians and a fake provider that exits
143 on SIGTERM, SIGTERM it while the daemon is down, and stage the reboot by
giving the attempt and `daemon.lock` a synthetic earlier boot.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
import pytest

from subfleet import daemon as daemon_module
from subfleet import host_shutdown
from subfleet.adapters.registry import register
from subfleet.contracts import Launch, Outcome, OutcomeClass, Reading, ReadingLabel
from subfleet.daemon import Daemon, after, utcnow
from subfleet.procs import Containment
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter
from tests.unit.test_salvage import git


OLD_BOOT = "75469207-4043-4113-8e1f-b5469953a665"     # the boot the attempts ran in (2026-09-30)
NEW_BOOT = "fc616732-486d-4fb8-a95d-0aae48bfb501"     # the boot after it
BOOT_SECONDS = 1790732697                             # 2026-09-30T01:44:57Z
BOOT_AT = "2026-09-30T01:44:57Z"
ENDED_AT = "2026-09-30T01:44:41Z"
STOPPING_AT = "2026-09-30T01:44:23Z"
LANES = ("codex-1", "codex-2", "codex-3")


def lock_record(boot=OLD_BOOT, stopping_at=STOPPING_AT, **more):
    """`daemon.lock` as the last daemon of `boot` left it."""
    record = {"pid": 2840, "boot_id": boot, "proc_start": "Tue Sep 29 12:00:00 2026", "version": "2.1.9",
              "stack_dumps": False, **more}
    if stopping_at:
        record["stopping_at"] = stopping_at
    return record


def three_lanes(root: Path) -> None:
    rows = []
    for lane in LANES:
        home = root / f"home-{lane}"
        home.mkdir(exist_ok=True)
        rows.append({"lane_id": lane, "provider": "codex", "account_key": f"codex:fake-{lane}",
                     "credential_ref": str(home), "credential_kind": "home", "credential_epoch": 1,
                     "home": str(home), "owner": "v2", "desktop": False, "enabled": True})
    (root / "lanes.json").write_text(json.dumps(rows))


@pytest.fixture
def rebooted(tmp_path, monkeypatch):
    """Build in-process daemons that start in a synthetic boot (C-4.7)."""
    built = []

    def build(*, lock=None, boot=NEW_BOOT, boot_seconds=BOOT_SECONDS, root=None, adapter=FakeAdapter):
        if root is None:
            root = tmp_path / f"state-{len(built)}"
            root.mkdir()
            harness = Harness(root)
            three_lanes(root)
        else:
            harness = next(h for _, h in built if h.root == Path(root).resolve())
        if lock is not None:
            (harness.root / "daemon.lock").write_text(json.dumps(lock))
        monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: boot)
        monkeypatch.setattr(daemon_module.procs, "boot_time", lambda: boot_seconds)
        monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "unit-test-start")
        monkeypatch.setattr(daemon_module.procs, "same_process", lambda *args: False)
        monkeypatch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment())
        register("codex", adapter)
        daemon = Daemon(harness.root)

        def refuse_real_launch(*args):
            raise AssertionError("in-process fixtures must never launch a guardian")
        monkeypatch.setattr(daemon, "_launch", refuse_real_launch)
        built.append((daemon, harness))
        return daemon, harness
    try:
        yield build
    finally:
        for daemon, harness in built:
            daemon.close()
            harness.check_notices()                 # C-15.1


def limit_line() -> bytes:
    return (json.dumps({"event": "rate_limit", "scope": "account", "resets_at": int(time.time()) + 3600})
            + "\n").encode()


def launched(daemon, attempt) -> Path:
    """What `_launch` leaves for finalization, with no guardian started: the
    attempt's directory and its launch (the lane a limit closes is read from it)."""
    daemon._pending_launches.discard(attempt["attempt_id"])
    adir = daemon.root / "jobs" / attempt["job_id"] / f"a{attempt['seq']}"
    adir.mkdir(mode=0o700, exist_ok=True)
    job = daemon.store.get_job(attempt["job_id"])
    daemon._launches[attempt["attempt_id"]] = Launch(
        (), {"SUBFLEET_LANE": attempt["lane_id"]}, (), job.get("worktree") or job["workdir"],
        job["prompt_path"], str(adir / "stdout"), str(adir / "stderr"), None, None)
    return adir


def run_attempt(daemon, job_id, *, rc, boot=OLD_BOOT, ended_at=ENDED_AT, stderr=b"",
                stdout=b"partial work\n", signal_number=None):
    """Admit the job's next attempt and end it as its guardian would have, then
    let the daemon take the receipt and finalize it."""
    daemon._admit()
    attempt = daemon.store.list_attempts(job_id)[-1]
    assert attempt["state"] == "reserved", attempt
    aid = attempt["attempt_id"]
    adir = launched(daemon, attempt)
    daemon.store.update_attempt(aid, state="running", guardian_pid=42000 + attempt["seq"],
                                pgid=42000 + attempt["seq"], boot_id=boot, proc_start="unit-test-start",
                                started_at="2026-09-30T00:36:51Z")
    (adir / "stdout").write_bytes(stdout)
    (adir / "stderr").write_bytes(stderr)
    (adir / "lane.log").write_bytes(b"")
    (adir / "exit.json").write_text(json.dumps({"rc": rc, "signal": signal_number, "wall_s": 4069.6,
                                                "child_pid": 42100 + attempt["seq"], "finished_at": ended_at}))
    daemon._process_attempt(aid)                    # running with a receipt: finalizing (C-4.2)
    daemon._finalize(daemon.store.get_attempt(aid))
    return daemon.store.get_attempt(aid)


def evidence(attempt) -> dict:
    return json.loads(attempt["evidence_json"] or "{}")


def notices(daemon, job_id) -> list[str]:
    return [row["text"] for row in daemon.store.list_notices() if row["job_id"] == job_id]


def started_events(daemon) -> list[dict]:
    return [json.loads(row["data_json"]) for row in daemon.store.query(
        "SELECT data_json FROM events WHERE kind='daemon.started' AND data_json!='{}' ORDER BY event_id")]


# --- in process ---------------------------------------------------------------


def test_c4_7_three_attempts_then_a_reboot_is_retried_not_failed(rebooted):
    """C-4.7, C-4.5: the incident's shape. a1 and a2 hit limits on two lanes; a3,
    the job's third and last attempt, ends on SIGTERM as the host restarts. Before,
    the job ended `failed` with rc 143; now a3 is a host shutdown, uncharged, and
    the job runs a4 and succeeds. The notice says the host restarted."""
    daemon, harness = rebooted(lock=lock_record())
    assert daemon._previous_boot["boot_id"] == OLD_BOOT and daemon._boot_at == BOOT_AT
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    a1 = run_attempt(daemon, job_id, rc=4, stderr=limit_line(), ended_at="2026-09-29T22:24:22Z")
    a2 = run_attempt(daemon, job_id, rc=4, stderr=limit_line(), ended_at="2026-09-30T00:28:19Z")
    assert (a1["outcome_class"], a2["outcome_class"]) == ("limited", "limited")
    a3 = run_attempt(daemon, job_id, rc=143)
    assert len({a1["lane_id"], a2["lane_id"], a3["lane_id"]}) == 3

    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"], job["wait_reason"]) == ("waiting", None, "capacity")
    assert job["next_check_at"] <= utcnow()                 # no lane's fault: no 60 s wait (C-9.5)
    assert (a3["state"], a3["rc"], a3["outcome_class"]) == ("failed", 143, "transient")
    assert a3["outcome_detail"] == host_shutdown.detail(evidence(a3)[host_shutdown.EVIDENCE_KEY])
    assert a3["outcome_detail"].startswith(
        "transient: host shutdown: the provider ended at 2026-09-30T01:44:41Z on SIGTERM (rc 143)")
    found = evidence(a3)
    assert found[host_shutdown.EVIDENCE_KEY] == {
        "ended_at": ENDED_AT, "signal": "SIGTERM", "rc": 143, "boot_id": OLD_BOOT, "next_boot_id": NEW_BOOT,
        "boot_at": BOOT_AT, "daemon_stopping_at": STOPPING_AT, "basis": "next-boot"}
    assert found["provider_verdict"] == {"class": "unknown", "detail": "fake provider failed", "killed_by": None}
    pinned = json.loads((daemon.root / "jobs" / job_id / "a3" / "finalization.json").read_text())
    assert pinned[host_shutdown.EVIDENCE_KEY] == found[host_shutdown.EVIDENCE_KEY]
    assert not daemon.store.list_closures(a3["lane_id"])    # a transient closes no lane
    assert notices(daemon, job_id) == []                     # not terminal: no notice yet (C-15.1)

    a4 = run_attempt(daemon, job_id, rc=0, boot=NEW_BOOT, ended_at=utcnow(), stdout=b"the finished work\n")
    assert a4["lane_id"] == a3["lane_id"]                   # the one lane no limit closed
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"], job["accepted_attempt_id"]) == ("succeeded", 0, a4["attempt_id"])
    [text] = notices(daemon, job_id)
    assert text.splitlines()[0].startswith(f"{job_id}: succeeded; rc=0;")
    assert ("host shutdown: the host shut down or restarted under attempt a3 at 2026-09-30T01:44:41Z; "
            "that was not the work, and it does not count against the job's 3 attempts (C-4.7)") in text
    [started] = started_events(daemon)
    assert started["boot_id"] == NEW_BOOT and started["boot_at"] == BOOT_AT
    assert started["previous_boot"] == {key: lock_record().get(key) for key in host_shutdown.PREVIOUS_BOOT_FIELDS}


def test_c4_7_before_c4_7_the_same_history_failed(rebooted):
    """C-4.5, the differential with the incident: the same three attempts with no
    reboot (the daemon runs in the boot they ran in) end as the store recorded on
    2026-09-30: `unknown`, rc 143, the job `failed`, nothing about the host."""
    daemon, harness = rebooted(boot=OLD_BOOT, lock=None)
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    run_attempt(daemon, job_id, rc=4, stderr=limit_line(), ended_at="2026-09-29T22:24:22Z")
    run_attempt(daemon, job_id, rc=4, stderr=limit_line(), ended_at="2026-09-30T00:28:19Z")
    a3 = run_attempt(daemon, job_id, rc=143)
    assert (a3["outcome_class"], a3["outcome_detail"]) == ("unknown", "fake provider failed")
    assert host_shutdown.EVIDENCE_KEY not in evidence(a3) and "provider_verdict" not in evidence(a3)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", 143)
    [text] = notices(daemon, job_id)
    assert "attempt a3: unknown, rc=143: fake provider failed" in text and "host shutdown" not in text


@pytest.mark.parametrize("receipt", [{"rc": 143}, {"rc": -15, "signal_number": 15}])
@pytest.mark.parametrize("seq_of_sigterm", [1, 3])
def test_c4_7_a_sigterm_without_a_reboot_is_unchanged(rebooted, seq_of_sigterm, receipt):
    """C-4.7: a SIGTERM in the boot the daemon runs in is not the host's, even with
    a stop recorded just before it. The attempt stays `unknown`, is charged, and
    C-4.5 does not retry it."""
    daemon, harness = rebooted(boot=OLD_BOOT, lock=lock_record(boot=OLD_BOOT))
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    for _ in range(seq_of_sigterm - 1):
        run_attempt(daemon, job_id, rc=4, stderr=limit_line())
    last = run_attempt(daemon, job_id, **receipt)
    assert last["outcome_class"] == "unknown" and host_shutdown.EVIDENCE_KEY not in evidence(last)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("failed", receipt["rc"])
    assert len(daemon.store.list_attempts(job_id)) == seq_of_sigterm


def test_c4_7_a_sigterm_long_before_the_reboot_is_unchanged(rebooted):
    """C-4.7: the boot changed, but the provider ended two hours before the next
    boot began, and the last daemon of its boot recorded no stop. Nothing ties
    the signal to the host going down, so the attempt is `unknown` as before."""
    daemon, harness = rebooted(lock=lock_record(stopping_at=None))
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    a1 = run_attempt(daemon, job_id, rc=143, ended_at="2026-09-29T23:44:41Z")
    assert a1["outcome_class"] == "unknown" and host_shutdown.EVIDENCE_KEY not in evidence(a1)
    assert daemon.store.get_job(job_id)["state"] == "failed"


def test_c4_7_an_overnight_shutdown_is_judged_by_the_last_daemons_stop(rebooted):
    """C-4.7: the host shut down at 01:44 and started again eight hours later.
    The next boot is too late to place the end, but the last daemon of the old
    boot wrote in `daemon.lock` that it began to stop at 01:44:23, and the
    provider ended 18 s after that. A one-attempt job (as gate rounds and revives
    are) is retried, since a host shutdown uses none of its budget."""
    eight_hours = BOOT_SECONDS + 8 * 3600
    daemon, harness = rebooted(lock=lock_record(), boot_seconds=eight_hours)
    job_id = daemon.dispatch("submit", harness.submit_args(max_attempts=1))["job_id"]
    a1 = run_attempt(daemon, job_id, rc=143)
    assert a1["outcome_class"] == "transient"
    assert evidence(a1)[host_shutdown.EVIDENCE_KEY]["basis"] == "daemon-stopping"
    assert "it was up again at 2026-09-30T09:44:57Z" in a1["outcome_detail"]
    assert daemon.store.get_job(job_id)["state"] == "waiting"
    a2 = run_attempt(daemon, job_id, rc=0, boot=NEW_BOOT, ended_at=utcnow(), stdout=b"done\n")
    assert daemon.store.get_job(job_id)["state"] == "succeeded" and a2["seq"] == 2


@pytest.mark.parametrize("how", ["operator", "max_wall_s"])
def test_c4_7_subfleet_kill_stays_a_cancel_across_a_reboot(rebooted, how):
    """C-4.7, C-9.2, C-7.2: `subfleet kill` (and the wall limit) records `killed_by`
    before it signals, so its SIGTERM is never the host's, even when everything
    else says reboot: an attempt from the old boot, ended within seconds of the
    next boot and of the last daemon's stop. The job is cancelled, the attempt
    `interrupted` and `unknown`, and nothing is retried."""
    now = int(time.time())
    stopping = datetime.fromtimestamp(now - 30, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    daemon, harness = rebooted(lock=lock_record(stopping_at=stopping), boot_seconds=now - 20)
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon._admit()
    [attempt] = daemon.store.list_attempts(job_id)
    aid = attempt["attempt_id"]
    launched(daemon, attempt)
    daemon.store.update_attempt(aid, state="running", guardian_pid=42001, pgid=42001, boot_id=OLD_BOOT,
                                proc_start="unit-test-start", started_at="2026-09-30T00:00:00Z")
    if how == "operator":
        daemon.dispatch("kill", {"job_id": job_id})
    else:
        daemon.store.update_job(job_id, started_at="2026-09-30T00:00:00Z", max_wall_s=60)
    daemon._process_attempt(aid)                    # the kill protocol: killed_by, SIGTERM, the receipt (C-5.6)
    receipt = json.loads((daemon.root / "jobs" / job_id / "a1" / "exit.json").read_text())
    assert receipt["rc"] == -15 and receipt["signal"] == 15
    # Everything but `killed_by` matches a host shutdown: this receipt's end
    # falls inside both windows, and the attempt ran in the old boot.
    unguarded = host_shutdown.verdict(kind="dispatch", killed_by=None, attempt_boot=OLD_BOOT,
                                      current_boot=NEW_BOOT, boot_at=daemon._boot_at, receipt=receipt,
                                      daemon_stopping_at=stopping)
    assert unguarded is not None
    daemon._finalize(daemon.store.get_attempt(aid))
    a1 = daemon.store.get_attempt(aid)
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("cancelled", 130)
    assert (a1["state"], a1["outcome_class"], a1["killed_by"]) == ("interrupted", "unknown", how)
    assert host_shutdown.EVIDENCE_KEY not in evidence(a1)
    assert len(daemon.store.list_attempts(job_id)) == 1
    [text] = notices(daemon, job_id)
    assert "host shutdown" not in text


def test_c4_7_a_host_shutdown_is_no_lanes_transient(rebooted):
    """C-4.5, C-4.7: a host shutdown is transparent to the lane rules. It does not
    count toward excluding its lane, the same-lane retry it cut off is still owed,
    and a pinned job's ordinary transient after one is still its first. Counted
    as a lane transient, a2 below would have excluded a1's lane (two transients
    there) and stopped the pinned job's retry."""
    daemon, harness = rebooted(lock=lock_record())
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    register("codex", TransientAdapter)
    a1 = run_attempt(daemon, job_id, rc=1, boot=NEW_BOOT, ended_at=utcnow())
    assert a1["outcome_class"] == "transient" and host_shutdown.EVIDENCE_KEY not in evidence(a1)
    daemon.store.update_job(job_id, next_check_at=None)
    a2 = run_attempt(daemon, job_id, rc=143)         # pinned to a1's lane for its one same-lane retry
    assert a2["lane_id"] == a1["lane_id"] and host_shutdown.marked(a2) is not None
    _, exclusions, pin = daemon._retry_pin(daemon.store.get_job(job_id))
    assert exclusions == ()                          # one transient there, not two
    assert pin["pinned_lane"] == a1["lane_id"]       # a1's same-lane retry is still owed (C-9.5)
    daemon.dispatch("kill", {"job_id": job_id})      # out of the way of the next job's admission
    [text] = notices(daemon, job_id)
    assert text.splitlines()[1].startswith("cancelled while waiting to retry")
    assert "host shutdown: the host shut down or restarted under attempt a2" in text

    pinned = daemon.dispatch("submit", harness.submit_args(pinned_lane="codex-2"))["job_id"]
    register("codex", FakeAdapter)
    assert run_attempt(daemon, pinned, rc=143)["outcome_class"] == "transient"
    register("codex", TransientAdapter)
    b2 = run_attempt(daemon, pinned, rc=1, boot=NEW_BOOT, ended_at=utcnow())
    assert b2["outcome_class"] == "transient"
    assert daemon.store.get_job(pinned)["state"] == "waiting"      # still the lane's first transient
    register("codex", FakeAdapter)


def test_c4_7_past_three_host_shutdowns_the_next_are_charged(rebooted):
    """C-4.7: the host shut down under every attempt of a two-attempt job. The
    first `RETRIES` are retried uncharged; the fourth and fifth are charged like
    any transient, so the job ends when its two charged attempts are spent, and
    a job whose own work restarts the host cannot restart it forever."""
    daemon, harness = rebooted(lock=lock_record())
    job_id = daemon.dispatch("submit", harness.submit_args(max_attempts=2))["job_id"]
    free = host_shutdown.RETRIES
    boots = [OLD_BOOT, NEW_BOOT] + [f"0000000{n}-0000-4000-8000-000000000000" for n in range(1, free + 2)]
    for n in range(free + 2):
        # Each attempt ran in its own boot, and the daemon finalizing it in the next.
        daemon._ident["boot_id"], daemon._previous_boot = boots[n + 1], {
            "boot_id": boots[n], "pid": 1, "proc_start": "x", "version": "x", "stopping_at": STOPPING_AT}
        attempt = run_attempt(daemon, job_id, rc=143, boot=boots[n])
        assert attempt["outcome_class"] == "transient", attempt
        assert bool(evidence(attempt)[host_shutdown.EVIDENCE_KEY].get("charged")) == (n >= free)
        assert ("counts against its attempts" in attempt["outcome_detail"]) == (n >= free)
        assert daemon.store.get_job(job_id)["state"] == ("waiting" if n < free + 1 else "failed")
    job = daemon.store.get_job(job_id)
    assert job["rc"] == 143 and host_shutdown.charged(daemon.store.list_attempts(job_id)) == 2
    [text] = notices(daemon, job_id)
    assert "under attempts a1 at" in text
    assert "past the first 3, a4, a5 counted against the job's 2 attempts (C-4.7)" in text


def test_c4_7_in_its_own_boot_a_signalled_attempt_waits_for_the_host(rebooted):
    """C-4.7: the daemon cannot count on being stopped before the providers are
    signalled. A provider that ended on SIGTERM seconds ago, in the boot
    the daemon runs in, is held: finalized now it would be `unknown` for good,
    and the reboot that followed could never be seen. A stopping daemon leaves it
    to the next boot; with no stop within `signal_hold_s` it is finalized as
    before."""
    daemon, harness = rebooted(boot=OLD_BOOT, lock=None)
    assert daemon.signal_hold_s == host_shutdown.SAME_BOOT_HOLD_S
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    held = run_attempt(daemon, job_id, rc=143, ended_at=utcnow())
    adir = daemon.root / "jobs" / job_id / "a1"
    assert held["state"] == "finalizing" and not (adir / "finalization.json").exists()
    assert daemon.store.get_job(job_id)["state"] == "running"
    daemon.stopping.set()                               # the host is going down: left for the next boot
    old = (datetime.now(timezone.utc) - timedelta(seconds=host_shutdown.SAME_BOOT_HOLD_S + 5))
    (adir / "exit.json").write_text(json.dumps({
        "rc": 143, "signal": None, "wall_s": 1.0, "child_pid": 42101,
        "finished_at": old.isoformat(timespec="seconds").replace("+00:00", "Z")}))
    daemon._finalize(daemon.store.get_attempt(held["attempt_id"]))
    assert daemon.store.get_attempt(held["attempt_id"])["state"] == "finalizing"
    daemon.stopping.clear()                             # no stop, and the hold has passed
    daemon._finalize(daemon.store.get_attempt(held["attempt_id"]))
    a1 = daemon.store.get_attempt(held["attempt_id"])
    assert (a1["state"], a1["outcome_class"]) == ("failed", "unknown")
    assert daemon.store.get_job(job_id)["state"] == "failed"


def test_c9_2_an_offline_kill_that_exits_zero_is_not_a_finished_deliverable(rebooted):
    """C-9.2, C-17.5 (review of PR #119): Codex exits 0 after a SIGTERM and leaves
    an interim message. Killed by the daemon, that is never `ok` (`killed_by`);
    killed offline, only `kill.json` says a signal ended it, and it counts the same."""
    daemon, harness = rebooted(boot=OLD_BOOT, lock=None)
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon._admit()
    [attempt] = daemon.store.list_attempts(job_id)
    adir = launched(daemon, attempt)
    (adir / host_shutdown.KILL_MARKER).write_text(json.dumps(
        {"by": "offline-kill", "signal": 15, "pid": 42001, "pgid": 42001, "requested_at": utcnow()}))
    daemon.store.update_attempt(attempt["attempt_id"], state="running", guardian_pid=42001, pgid=42001,
                                boot_id=OLD_BOOT, proc_start="unit-test-start")
    (adir / "stdout").write_bytes(b"I am checking the newer validation code before finalizing\n")
    for name in ("stderr", "lane.log"):
        (adir / name).write_bytes(b"")
    (adir / "exit.json").write_text(json.dumps({"rc": 0, "signal": None, "wall_s": 1.0, "child_pid": 42101,
                                                "finished_at": utcnow()}))
    daemon._process_attempt(attempt["attempt_id"])
    daemon._finalize(daemon.store.get_attempt(attempt["attempt_id"]))
    a1 = daemon.store.get_attempt(attempt["attempt_id"])
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["accepted_attempt_id"]) == ("failed", None)
    assert a1["outcome_class"] == "unknown" and a1["outcome_detail"] == (
        "stopped by offline-kill: exit 0 after the operator's offline kill is not a finished deliverable")
    assert evidence(a1)["provider_verdict"] == {"class": "ok", "detail": "fake provider succeeded",
                                                "killed_by": "offline-kill"}


def test_c4_7_the_hold_must_be_shorter_than_the_stops_lead(tmp_path):
    """C-4.7: a daemon whose hold reached `BEFORE_STOP_S` could hold an attempt
    and then leave it outside the stop's window, so it refuses to start."""
    with pytest.raises(ValueError, match="signal_hold_s"):
        Daemon(tmp_path, signal_hold_s=host_shutdown.BEFORE_STOP_S)
    assert not (tmp_path / "daemon.lock").exists()


def test_c4_7_a_cancel_ends_the_hold(rebooted):
    """C-4.7, C-7.2: a cancelled job is never retried, whatever ended its attempt,
    so its attempt is not held: it is finalized at once and the job is cancelled."""
    daemon, harness = rebooted(boot=OLD_BOOT, lock=None)
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    held = run_attempt(daemon, job_id, rc=143, ended_at=utcnow())
    assert held["state"] == "finalizing"
    daemon.dispatch("kill", {"job_id": job_id})
    daemon._finalize(daemon.store.get_attempt(held["attempt_id"]))
    job = daemon.store.get_job(job_id)
    assert (job["state"], job["rc"]) == ("cancelled", 130)
    assert daemon.store.get_attempt(held["attempt_id"])["state"] == "interrupted"


def test_c4_7_a_held_attempt_is_a_host_shutdown_in_the_next_boot(rebooted):
    """C-4.7: the order the incident did not have. The provider gets SIGTERM, the
    daemon holds the attempt, and 20 s later the daemon is stopped too. The next
    boot began eight hours on. The provider ended before the stop was stamped,
    within `BEFORE_STOP_S` of it, so the next daemon still judges it the host's."""
    daemon, harness = rebooted(lock=lock_record(stopping_at="2026-09-30T01:45:01Z"),
                               boot_seconds=BOOT_SECONDS + 8 * 3600)
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    a1 = run_attempt(daemon, job_id, rc=143)            # ended 01:44:41, 20 s before the stop
    assert a1["outcome_class"] == "transient"
    assert evidence(a1)[host_shutdown.EVIDENCE_KEY]["basis"] == "daemon-stopping"
    assert daemon.store.get_job(job_id)["state"] == "waiting"


def test_c4_7_an_offline_kill_is_never_a_host_shutdown(rebooted):
    """C-4.7, C-17.5: with the daemon down, `subfleet kill` cannot record
    `killed_by`, so it leaves `kill.json` in the attempt's directory before it
    signals. Everything else here says reboot (the old boot, rc 143, both
    windows); the marker decides, and the job is not run again."""
    daemon, harness = rebooted(lock=lock_record())
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    daemon._admit()
    [attempt] = daemon.store.list_attempts(job_id)
    adir = launched(daemon, attempt)
    (adir / host_shutdown.KILL_MARKER).write_text(json.dumps(
        {"by": "offline-kill", "signal": 15, "pid": 42001, "pgid": 42001, "requested_at": "2026-09-30T01:44:40Z"}))
    daemon.store.update_attempt(attempt["attempt_id"], state="running", guardian_pid=42001, pgid=42001,
                                boot_id=OLD_BOOT, proc_start="unit-test-start")
    for name in ("stdout", "stderr", "lane.log"):
        (adir / name).write_bytes(b"")
    receipt = {"rc": 143, "signal": None, "wall_s": 1.0, "child_pid": 42101, "finished_at": ENDED_AT}
    (adir / "exit.json").write_text(json.dumps(receipt))
    assert host_shutdown.verdict(kind="dispatch", killed_by=None, attempt_boot=OLD_BOOT, current_boot=NEW_BOOT,
                                 boot_at=BOOT_AT, receipt=receipt, daemon_stopping_at=STOPPING_AT) is not None
    daemon._process_attempt(attempt["attempt_id"])
    daemon._finalize(daemon.store.get_attempt(attempt["attempt_id"]))
    a1 = daemon.store.get_attempt(attempt["attempt_id"])
    assert (a1["state"], a1["outcome_class"]) == ("failed", "unknown")
    assert host_shutdown.EVIDENCE_KEY not in evidence(a1)
    assert evidence(a1)["offline_kill"]["by"] == "offline-kill"
    assert daemon.store.get_job(job_id)["state"] == "failed"
    assert len(daemon.store.list_attempts(job_id)) == 1


def test_c4_7_a_replay_keeps_the_verdict_finalization_pinned(rebooted, monkeypatch):
    """C-4.2, C-4.7: finalization pins the verdict in `finalization.json` before it
    commits. If the commit is lost and the host restarts again, the replay reaches
    the same class, though this daemon's facts alone would no longer say so."""
    daemon, harness = rebooted(lock=lock_record())
    job_id = daemon.dispatch("submit", harness.submit_args())["job_id"]
    real = host_shutdown.retry_after

    def lost_commit(**kwargs):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(host_shutdown, "retry_after", lost_commit)
    with pytest.raises(sqlite3.OperationalError):
        run_attempt(daemon, job_id, rc=143)
    monkeypatch.setattr(host_shutdown, "retry_after", real)
    [attempt] = daemon.store.list_attempts(job_id)
    assert attempt["state"] == "finalizing"
    # Another restart: a later boot, long after, and a daemon.lock from this one.
    daemon._ident["boot_id"], daemon._boot_at, daemon._previous_boot = (
        "0000000a-0000-4000-8000-000000000000", "2026-10-03T00:00:00Z", None)
    daemon._finalize(daemon.store.get_attempt(attempt["attempt_id"]))
    attempt = daemon.store.get_attempt(attempt["attempt_id"])
    assert attempt["outcome_class"] == "transient"
    assert evidence(attempt)[host_shutdown.EVIDENCE_KEY]["next_boot_id"] == NEW_BOOT
    assert daemon.store.get_job(job_id)["state"] == "waiting"


def test_c4_7_daemon_lock_carries_the_stop_and_the_previous_boot(rebooted):
    """C-4.7, C-5.8: a stopping daemon writes when it began to stop into
    `daemon.lock`; the next daemon, in another boot, takes that record as the
    last daemon of the boot before, and a daemon started later in the same boot
    finds it carried forward. Each start is a `daemon.started` event."""
    first, harness = rebooted(boot=OLD_BOOT, boot_seconds=BOOT_SECONDS - 86400)
    assert first._previous_boot is None
    # The stop's own thread stamps it as soon as `stopping` is set, before close().
    marker = threading.Thread(target=first._watch_stopping)
    marker.start()
    first.stopping.set()
    marker.join(timeout=10)
    stamped = json.loads((harness.root / "daemon.lock").read_text())["stopping_at"]
    first.close()
    assert json.loads((harness.root / "daemon.lock").read_text())["stopping_at"] == stamped   # the first stamp stays
    # Once close() has let the descriptor go, a late mark writes nothing: its
    # number may be another file's by then.
    before = (harness.root / "daemon.lock").read_bytes()
    first._previous_boot = {"boot_id": "late"}
    first._write_lock(stack_dumps=True)
    assert (harness.root / "daemon.lock").read_bytes() == before
    first._previous_boot = None
    old = json.loads((harness.root / "daemon.lock").read_text())
    assert old["boot_id"] == OLD_BOOT and old["stopping_at"] and "stack_dumps" not in old
    second, _ = rebooted(root=harness.root)
    assert second._previous_boot == {key: old.get(key) for key in host_shutdown.PREVIOUS_BOOT_FIELDS}
    live = json.loads((harness.root / "daemon.lock").read_text())
    assert live["boot_id"] == NEW_BOOT and "stopping_at" not in live
    assert live[host_shutdown.PREVIOUS_BOOT_KEY] == second._previous_boot
    second.close()
    third, _ = rebooted(root=harness.root)               # the same boot: carried forward
    assert third._previous_boot == second._previous_boot
    third.close()
    fourth, _ = rebooted(root=harness.root, boot="0000000b-0000-4000-8000-000000000000")
    assert fourth._previous_boot["boot_id"] == NEW_BOOT  # a newer boot: the last daemon of NEW_BOOT
    events = started_events(fourth)
    assert [event["boot_id"] for event in events] == [OLD_BOOT, NEW_BOOT, NEW_BOOT,
                                                       "0000000b-0000-4000-8000-000000000000"]
    assert [(event["previous_boot"] or {}).get("boot_id") for event in events] == [None, OLD_BOOT, OLD_BOOT, NEW_BOOT]


# --- the daemon against a model of the budget ---------------------------------


class ScriptedAdapter(FakeAdapter):
    """rc 75 is an ordinary transient (C-9.5); everything else as the fake classifies it."""

    def classify(self, attempt_dir, launch, exit_info):
        if exit_info.rc == 75:
            return Outcome(OutcomeClass.TRANSIENT, "transient: fixture stream disconnected")
        return super().classify(attempt_dir, launch, exit_info)


@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(outcomes=st.lists(st.sampled_from(["shutdown", "shutdown", "transient", "unknown", "ok"]),
                         min_size=1, max_size=9),
       max_attempts=st.integers(1, 3))
def test_c4_7_property_the_daemon_keeps_the_budget_a_model_keeps(rebooted, outcomes, max_attempts):
    """C-4.5, C-4.7, differential: a job's attempts end as drawn (a host shutdown,
    an ordinary transient, an `unknown`, a success) and go through the daemon's own
    admission and finalization. After each, the job's state, whether the attempt is
    charged, and the charged count are what this model says: the first `RETRIES`
    host shutdowns are free and retried; every other attempt is charged, and the
    job tries again only if that attempt can be retried (a transient or a host
    shutdown) and fewer than `max_attempts` charged attempts have run."""
    daemon, harness = rebooted(lock=lock_record(), adapter=ScriptedAdapter)
    try:
        job_id = daemon.dispatch("submit", harness.submit_args(max_attempts=max_attempts))["job_id"]
        charged = free = 0
        for kind in outcomes:
            daemon.store.update_job(job_id, next_check_at=None)       # past a transient's minute (C-9.5)
            if kind == "shutdown":
                attempt = run_attempt(daemon, job_id, rc=143)
            else:
                attempt = run_attempt(daemon, job_id, rc={"transient": 75, "unknown": 1, "ok": 0}[kind],
                                      boot=NEW_BOOT, ended_at=utcnow(), stdout=b"the work\n")
            is_free = kind == "shutdown" and free < host_shutdown.RETRIES
            if is_free:
                free += 1
                expected = "waiting"
            else:
                charged += 1
                again = kind in ("shutdown", "transient") and charged < max_attempts
                expected = "succeeded" if kind == "ok" else "waiting" if again else "failed"
            assert daemon.store.get_job(job_id)["state"] == expected, (outcomes, max_attempts, kind)
            assert host_shutdown.exempt(attempt) == is_free
            assert (host_shutdown.marked(attempt) is not None) == (kind == "shutdown")
            assert host_shutdown.charged(daemon.store.list_attempts(job_id)) == charged <= max_attempts
            if expected != "waiting":
                break
    finally:
        daemon.close()


# --- a writable job: the retry is an ordinary retry ---------------------------


class TransientAdapter(FakeAdapter):
    """An ordinary transient (C-9.5), for comparison."""

    def classify(self, attempt_dir, launch, exit_info):
        return Outcome(OutcomeClass.TRANSIENT, "transient: fixture stream disconnected")


def writable(daemon, harness):
    workdir = harness.workdir
    git(workdir, "init", "-b", "task/example")
    git(workdir, "config", "user.name", "Test User")
    git(workdir, "config", "user.email", "test@example.invalid")
    (workdir / "tracked.txt").write_text("baseline\n")
    git(workdir, "add", ".")
    git(workdir, "commit", "-m", "baseline")
    for lane in LANES:      # writable admission requires measured capacity (C-11.4)
        daemon.store.add_reading(Reading(lane, "account", "seven_day", .1, after(3600),
                                         ReadingLabel.PROVIDER, "fixture", utcnow()))
    return workdir


def first_attempt_then_retry(rebooted, monkeypatch, *, host: bool, salvage_fails: bool):
    """a1 edits the worktree and ends: as the host restarts (`host`) or on an
    ordinary transient; then a2 is admitted. Returns what a2 starts from."""
    daemon, harness = rebooted(lock=lock_record(), adapter=FakeAdapter if host else TransientAdapter)
    workdir = writable(daemon, harness)
    job_id = daemon.dispatch("submit", harness.submit_args(sandbox="workspace-write", in_place=True))["job_id"]
    daemon._admit()
    [attempt] = daemon.store.list_attempts(job_id)
    adir = launched(daemon, attempt)
    daemon.store.update_attempt(attempt["attempt_id"], state="running", guardian_pid=42001, pgid=42001,
                                boot_id=OLD_BOOT if host else NEW_BOOT, proc_start="unit-test-start")
    (workdir / "tracked.txt").write_text("a1's progress\n")
    (workdir / "new-by-a1.txt").write_text("a1 work\n")
    for name in ("stdout", "stderr", "lane.log"):
        (adir / name).write_bytes(b"")
    (adir / "exit.json").write_text(json.dumps({"rc": 143 if host else 1, "signal": None, "wall_s": 1.0,
                                                "child_pid": 42101, "finished_at": ENDED_AT}))
    daemon._process_attempt(attempt["attempt_id"])
    with monkeypatch.context() as patch:
        if salvage_fails:
            from subfleet.salvage import SalvageError

            def refuse(*args, **kwargs):
                raise SalvageError("git add failed: fatal: adding files failed")
            patch.setattr(daemon_module, "salvage", refuse)
        daemon._finalize(daemon.store.get_attempt(attempt["attempt_id"]))
    a1 = daemon.store.get_attempt(attempt["attempt_id"])
    assert (a1["outcome_class"], host_shutdown.marked(a1) is not None) == ("transient", host)
    assert daemon.store.get_job(job_id)["state"] == "waiting"
    head = git(workdir, "rev-parse", "HEAD")
    daemon.store.update_job(job_id, next_check_at=None)      # an ordinary transient waits 60 s (C-9.5)
    daemon._admit()
    a1, a2 = daemon.store.list_attempts(job_id)
    salvage_refs = [row["path"] for row in daemon.store.list_artifacts(a1["attempt_id"]) if row["role"] == "salvage"]
    held = [row["path"] for row in daemon.store.list_artifacts(a2["attempt_id"]) if row["role"] == "salvage"]
    a2_evidence = evidence(a2)
    shape = {
        "a2": (a2["seq"], a2["state"]),
        "a1_salvaged": [ref.rsplit("-", 2)[0] for ref in salvage_refs],
        "a1_salvage_error": evidence(a1).get("salvage_error"),
        "a2_baseline_commit": a2_evidence.get("baseline_commit") == head,
        "a2_baseline_ref": a2_evidence.get("baseline_ref", "").replace(job_id, "<job>"),
        "a2_held": [ref.replace(job_id, "<job>") for ref in held],
        "a2_starts_from_a1s_tree": a2["baseline_tree"] == daemon_module.working_tree(workdir, head),
    }
    for ref in salvage_refs:
        assert git(workdir, "show", f"{ref}:new-by-a1.txt") == "a1 work"
    for ref in held:
        assert git(workdir, "show", f"{ref}:new-by-a1.txt") == "a1 work"
    return shape


@pytest.mark.parametrize("salvage_fails", [False, True])
def test_c4_7_a_writable_retry_after_a_reboot_starts_as_any_retry_does(rebooted, monkeypatch, salvage_fails):
    """C-4.7, C-13.1, C-13.3, differential: a writable job's retry after a host
    shutdown inherits exactly what a retry after an ordinary transient gets: a1's
    work under a salvage ref, a2 starting from that worktree and HEAD, and, when
    a1's salvage failed, admission holding a2's start snapshot under
    `refs/subfleet-salvage/<job>-a2-baseline`."""
    after_reboot = first_attempt_then_retry(rebooted, monkeypatch, host=True, salvage_fails=salvage_fails)
    after_transient = first_attempt_then_retry(rebooted, monkeypatch, host=False, salvage_fails=salvage_fails)
    assert after_reboot == after_transient
    assert after_reboot["a2"] == (2, "reserved")
    assert after_reboot["a2_baseline_commit"] and after_reboot["a2_starts_from_a1s_tree"]
    if salvage_fails:
        assert after_reboot["a1_salvage_error"] and after_reboot["a1_salvaged"] == []
        assert after_reboot["a2_held"] == ["refs/subfleet-salvage/<job>-a2-baseline"]
    else:
        assert after_reboot["a1_salvaged"] == ["refs/subfleet-salvage/task-example"]
        assert after_reboot["a2_held"] == []


# --- end to end: a daemon, guardians and a real SIGTERM ------------------------


def sysctl_boot_seconds() -> int:
    from subfleet import procs
    return procs.boot_time()


def stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def running_attempt(daemon, job_id, seq, *, timeout=60):
    """The attempt running, its provider past installing its SIGTERM handler: the
    fake prints its ready line after that, and a signal before it would end the
    provider by the default action (rc -15) instead (C-12.8)."""
    adir = daemon.root / "jobs" / job_id / f"a{seq}"

    def found():
        attempts = daemon.attempts(job_id)
        last = attempts[-1] if attempts else None
        try:
            ready = b"fake provider ready" in (adir / "stdout").read_bytes()
        except OSError:
            ready = False
        return last if (last and last["seq"] == seq and last["state"] == "running"
                        and (adir / "start.json").exists() and ready) else None
    return daemon.until(found, timeout=timeout)


def stage_reboot(daemon, job_id, seq, *, basis: str) -> str:
    """Make the daemon's next start a start in a new boot: the attempt and the last
    daemon's `daemon.lock` record get a synthetic earlier boot (C-4.7). With
    `next-boot` the receipt's end is moved to 18 s before this host's real boot
    and the lock has no stop; with `daemon-stopping` the lock says the last daemon
    began to stop 18 s before the provider ended."""
    adir = daemon.root / "jobs" / job_id / f"a{seq}"
    receipt = json.loads((adir / "exit.json").read_text())
    if basis == "next-boot":
        receipt["finished_at"] = stamp(sysctl_boot_seconds() - 18)
        (adir / "exit.json").write_text(json.dumps(receipt))
        stopping = None
    else:
        ended = datetime.fromisoformat(receipt["finished_at"].replace("Z", "+00:00"))
        stopping = stamp(ended.timestamp() - 18)
    with sqlite3.connect(daemon.root / "state.sqlite3") as db:
        db.execute("UPDATE attempts SET boot_id=? WHERE attempt_id=?", (OLD_BOOT, f"{job_id}/a{seq}"))
    (daemon.root / "daemon.lock").write_text(json.dumps(lock_record(stopping_at=stopping)))
    return receipt["finished_at"]


@pytest.mark.parametrize("basis", ["next-boot", "daemon-stopping"])
def test_c4_7_e2e_a_reboot_under_the_third_attempt_is_retried(daemon, basis):
    """C-4.7 end to end. a1 and a2 hit limits on two lanes; a3 runs a provider that,
    like Claude Code, exits 143 on SIGTERM. The daemon goes down, the provider gets
    SIGTERM and its guardian writes the receipt, and the next daemon starts in what
    is, by the attempt's and the lock's record, a new boot. a3 is a host shutdown,
    the job runs a4 and succeeds."""
    three_lanes(daemon.root)
    daemon.start()
    by_attempt = {"1": {"scenario": "rc4-limit-with-clock"}, "2": {"scenario": "rc4-limit-with-clock"},
                  "3": {"scenario": "term-exits-143", "delay_s": 120}}
    args = daemon.submit_args()
    Path(args["prompt_path"]).write_text(json.dumps({"scenario": "ok", "by_attempt": by_attempt}))
    job_id = daemon.call("submit", **args)["job_id"]
    running_attempt(daemon, job_id, 3)
    start = json.loads((daemon.root / "jobs" / job_id / "a3" / "start.json").read_text())
    daemon.crash()
    os.killpg(start["pgid"], signal.SIGTERM)        # as the host does as it goes down
    exit_path = daemon.root / "jobs" / job_id / "a3" / "exit.json"
    daemon.until(exit_path.exists, timeout=30)
    assert json.loads(exit_path.read_text())["rc"] == 143
    ended = stage_reboot(daemon, job_id, 3, basis=basis)
    daemon.start()
    job = daemon.until(
        lambda: (row := daemon.job(job_id))["state"] in {"succeeded", "failed", "cancelled", "lost"} and row,
        timeout=60)
    attempts = daemon.attempts(job_id)
    assert (job["state"], job["rc"]) == ("succeeded", 0), (job, attempts, daemon.log_text())
    a1, a2, a3, a4 = attempts
    assert [a["outcome_class"] for a in attempts] == ["limited", "limited", "transient", "ok"]
    assert a3["rc"] == 143 and a3["outcome_detail"].startswith(
        f"transient: host shutdown: the provider ended at {ended} on SIGTERM (rc 143)")
    assert json.loads(a3["evidence_json"])[host_shutdown.EVIDENCE_KEY]["basis"] == basis
    assert a4["lane_id"] == a3["lane_id"]
    [notice] = daemon.rows("SELECT text FROM notices WHERE job_id=?", (job_id,))
    assert "host shutdown: the host shut down or restarted under attempt a3" in notice["text"]


def test_c4_7_e2e_subfleet_kill_stays_a_cancel_after_a_reboot(daemon):
    """C-4.7, C-7.2 end to end: `subfleet kill` SIGTERMs the provider, which exits
    143; the daemon dies right after taking the receipt; the host "restarts"; the
    next daemon finalizes an attempt from the old boot whose end falls in the
    window. `killed_by` decides: the job is cancelled and nothing is retried."""
    daemon.start("--crash-at", "finalizing")
    args = daemon.submit_args("term-exits-143", delay_s=120)
    job_id = daemon.call("submit", **args)["job_id"]
    running_attempt(daemon, job_id, 1)
    daemon.call("kill", job_id=job_id)
    daemon.until(lambda: daemon.process.poll() is not None, timeout=60)      # crashed at `finalizing`
    [a1] = daemon.attempts(job_id)
    assert (a1["state"], a1["killed_by"]) == ("finalizing", "operator")
    # 143 when the provider exited on the SIGTERM within the fake daemon's 80 ms
    # grace; under load the protocol escalates, and the daemon writes -9.
    receipt = json.loads((daemon.root / "jobs" / job_id / "a1" / "exit.json").read_text())
    assert host_shutdown.ended_by_signal(receipt) is not None, receipt
    stage_reboot(daemon, job_id, 1, basis="daemon-stopping")
    daemon.start()
    job = daemon.until(lambda: (row := daemon.job(job_id))["state"] == "cancelled" and row, timeout=60)
    assert job["rc"] == 130
    [a1] = daemon.attempts(job_id)
    assert (a1["state"], a1["outcome_class"], a1["killed_by"]) == ("interrupted", "unknown", "operator")
    assert host_shutdown.EVIDENCE_KEY not in json.loads(a1["evidence_json"])


def test_c4_7_e2e_a_sigterm_with_no_reboot_is_unchanged(daemon):
    """C-4.7 end to end: a SIGTERM from outside Subfleet while the daemon runs, in
    the same boot, is what it always was: `unknown`, charged, and not retried."""
    daemon.start()
    job_id = daemon.call("submit", **daemon.submit_args("term-exits-143", delay_s=120))["job_id"]
    running_attempt(daemon, job_id, 1)
    start = json.loads((daemon.root / "jobs" / job_id / "a1" / "start.json").read_text())
    os.killpg(start["pgid"], signal.SIGTERM)
    job = daemon.until(lambda: (row := daemon.job(job_id))["state"] == "failed" and row, timeout=60)
    assert job["rc"] == 143
    [a1] = daemon.attempts(job_id)
    assert (a1["outcome_class"], a1["outcome_detail"]) == ("unknown", "fake provider failed")
    assert host_shutdown.EVIDENCE_KEY not in json.loads(a1["evidence_json"])


def test_c4_7_e2e_an_offline_kill_then_a_reboot_is_not_retried(daemon):
    """C-4.7, C-17.5 end to end (review of PR #119): with the daemon down,
    `subfleet kill` signals the provider itself. It leaves `kill.json` first, so
    when the host then restarts and the attempt looks like a host shutdown in
    every other way, the next daemon does not run the job again."""
    from subfleet.offline import Offline
    daemon.start()
    job_id = daemon.call("submit", **daemon.submit_args("term-exits-143", delay_s=120))["job_id"]
    running_attempt(daemon, job_id, 1)
    daemon.crash()
    result = Offline(daemon.root).kill(job_id)
    assert result["action"] == "signalled", result
    adir = daemon.root / "jobs" / job_id / "a1"
    daemon.until((adir / "exit.json").exists, timeout=30)
    assert json.loads((adir / "exit.json").read_text())["rc"] == 143
    assert json.loads((adir / host_shutdown.KILL_MARKER).read_text())["by"] == "offline-kill"
    stage_reboot(daemon, job_id, 1, basis="daemon-stopping")
    daemon.start()
    job = daemon.until(lambda: (row := daemon.job(job_id))["state"] in {"failed", "succeeded", "cancelled"} and row,
                       timeout=60)
    [a1] = daemon.attempts(job_id)
    assert (job["state"], a1["outcome_class"]) == ("failed", "unknown"), (job, a1)
    found = json.loads(a1["evidence_json"])
    assert host_shutdown.EVIDENCE_KEY not in found and found["offline_kill"]["signal"] == 15


def test_c4_7_e2e_a_stopping_daemon_says_so_in_daemon_lock(daemon):
    """C-4.7, C-5.8 end to end: a daemon stopped by SIGTERM leaves `stopping_at` in
    `daemon.lock`, which the next daemon carries as `previous_boot` only when it
    starts in another boot (here it is the same boot, so it carries none)."""
    daemon.start()
    before = stamp(time.time() - 1)
    daemon.process.terminate()
    daemon.process.wait(timeout=60)
    lock = json.loads((daemon.root / "daemon.lock").read_text())
    assert before <= lock["stopping_at"] <= stamp(time.time() + 1)
    daemon.start()
    live = json.loads((daemon.root / "daemon.lock").read_text())
    assert "stopping_at" not in live and host_shutdown.PREVIOUS_BOOT_KEY not in live
