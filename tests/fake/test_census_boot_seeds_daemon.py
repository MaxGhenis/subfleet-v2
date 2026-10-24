"""D-QUAR-REUSE1, C-5.5, C-5.7: trackb's quarantine, end to end through a real daemon.

`20261014-071003-trackb-d1408-review5/a1` was quarantined on boot A; on boot B its exit
receipt's `child_pid 83026` was an unrelated `tail -40`, and `kill --confirm-dead` left it
quarantined (`quarantine.still_live`) while release/217 kept the stranger's identity in
`counted_identities`. The census, the release and the kill protocol run here as the daemon
runs them, on a synthetic process table; nothing is inspected or signalled on the host.
"""

from __future__ import annotations

from dataclasses import asdict
import json

from subfleet import procs, protocol
from tests.fake.test_state_contract import reserve, state_daemon  # noqa: F401 (a fixture)
from tests.unit.test_procs import BOOT_A, BOOT_B, census

# Save the real census before state_daemon replaces the process seam.
CONTAINMENT = procs.containment
SNAPSHOT = procs.snapshot
GUARDIAN, CHILD, SHELL = 76526, 83026, 83000
GUARDIAN_START = "Wed Oct 14 12:05:49 2026"
STRANGER_START = "Sat Oct 24 15:07:47 2026"
SHELL_START = "Sat Oct 24 15:01:02 2026"
REASON = "different boot identity (attempt's boot)"


def trackb(daemon, harness, monkeypatch, *, now=BOOT_B, extra_rows="", marked=(), quarantined=True, **evidence):
    """An attempt recorded on boot A, its receipt naming 83026, and boot `now`'s table, in which
    the `marked` pids carry its marker pair; quarantined as release/217 left it, by default."""
    job_id, attempt, adir = reserve(daemon, harness)
    recorded = {**json.loads(attempt["evidence_json"] or "{}"), **evidence,
                "responsible_root": {"pid": GUARDIAN, "unique_id": 9000}}
    daemon.store.update_attempt(attempt["attempt_id"], state="running", guardian_pid=GUARDIAN, pgid=GUARDIAN,
                                child_pid=None, boot_id=BOOT_A, proc_start=GUARDIAN_START,
                                started_at="2026-10-14T12:05:49Z", evidence_json=json.dumps(recorded))
    (adir / "start.json").write_text(json.dumps({"guardian_pid": GUARDIAN, "pgid": GUARDIAN, "boot_id": BOOT_A,
                                                 "proc_start": GUARDIAN_START}))
    (adir / "exit.json").write_text(json.dumps({"rc": 0, "signal": None, "child_pid": CHILD,
                                                "finished_at": "2026-10-14T12:28:32Z", "wall_s": 1363}))
    procs.forget_boot_id()
    markers = "".join(f"{pid} pm2 SUBFLEET_ATTEMPT={attempt['attempt_id']} SUBFLEET_ROOT={daemon.root}\n"
                      for pid in marked)
    census(monkeypatch, session=now, markers=markers,
           parents=f"1 0 1 Ss {SHELL_START}\n{SHELL} 1 {SHELL} Ss {SHELL_START}\n"
                   f"{CHILD} {SHELL} {SHELL} S {STRANGER_START}\n" + extra_rows)
    # state_daemon fixes the boot identity at "unit-test-boot": read it from the table's sysctl.
    monkeypatch.setattr(procs, "boot_id", lambda: now)
    monkeypatch.setattr(procs, "containment", CONTAINMENT)
    monkeypatch.setattr(procs, "snapshot", SNAPSHOT)
    attempt = daemon.store.get_attempt(attempt["attempt_id"])
    if not quarantined:
        return job_id, attempt
    daemon._quarantine(attempt, procs.Containment(descendant_pids=frozenset({CHILD}),
                                                  errors=(f"recorded identity unavailable for pid {CHILD}",),
                                                  unverifiable=True),
                       "termination could not verify containment")
    return job_id, daemon.store.get_attempt(attempt["attempt_id"])


def confirmed(daemon) -> dict:
    return json.loads(daemon.store.one("SELECT data_json FROM events WHERE kind='quarantine.confirmed_dead'")
                      ["data_json"])


def test_trackb_confirm_dead_releases_the_attempt(state_daemon, monkeypatch):
    """`kill --confirm-dead` on trackb's shape releases it: the census is verified empty with
    83026 under `recycled` (the attempt's boot), the leases go, and no current-boot identity
    is kept. Release/217 recorded `quarantine.still_live` and adopted the stranger."""
    daemon, harness = state_daemon
    job_id, attempt = trackb(daemon, harness, monkeypatch)
    daemon._resolve_quarantine(attempt, protocol.KillArgs(job_id, confirm_dead=True))
    after = daemon.store.get_attempt(attempt["attempt_id"])
    assert after["state"] == "lost"
    assert not daemon.store.list_leases(attempt["attempt_id"]) and not daemon.store.list_leases(job_id)
    containment = confirmed(daemon)["containment"]
    assert containment["live_pids"] == [] and containment["errors"] == []
    assert containment["recycled"][str(CHILD)]["reason"] == REASON
    assert containment["recycled"][str(CHILD)]["now"]["proc_start"] == STRANGER_START
    evidence = json.loads(after["evidence_json"])
    assert evidence["recycled"][str(CHILD)]["reason"] == REASON
    assert "counted_identities" not in evidence
    shown = json.dumps(daemon.dispatch("show", {"job_id": job_id}))
    assert REASON in shown


def test_trackb_as_release_217_left_it_is_repaired(state_daemon, monkeypatch):
    """trackb's row now: `counted_identities` holds the stranger's current-boot identity
    (`attempt.counted_recorded {"pids": [83026]}`). The census treats it as not the attempt's,
    names it in its evidence (`other_boot`), and releases; the stored history is unchanged."""
    daemon, harness = state_daemon
    adopted = asdict(procs.ProcessIdentity(CHILD, BOOT_B, STRANGER_START))
    job_id, attempt = trackb(daemon, harness, monkeypatch, counted_identities={str(CHILD): adopted})
    daemon._resolve_quarantine(attempt, protocol.KillArgs(job_id, confirm_dead=True))
    after = daemon.store.get_attempt(attempt["attempt_id"])
    assert after["state"] == "lost"
    containment = confirmed(daemon)["containment"]
    assert containment["live_pids"] == [] and containment["other_boot"] == {str(CHILD): [adopted]}
    assert json.loads(after["evidence_json"])["counted_identities"] == {str(CHILD): adopted}


def test_trackb_on_its_own_boot_is_still_held(state_daemon, monkeypatch):
    """The same attempt, its census taken on boot A: the live identity-less child holds it, as
    at 15bd8c172 (`quarantine.still_live`)."""
    daemon, harness = state_daemon
    job_id, attempt = trackb(daemon, harness, monkeypatch, now=BOOT_A)
    daemon._resolve_quarantine(attempt, protocol.KillArgs(job_id, confirm_dead=True))
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "quarantined"
    [held] = [json.loads(row["data_json"]) for row in daemon.store.list_events(job_id)
              if row["kind"] == "quarantine.still_live"]
    assert held["live_pids"] == [CHILD] and held["unverifiable"]


def test_invariant_4_no_signal_reaches_a_counted_or_responsible_identity(state_daemon, monkeypatch):
    """The kill protocol signals by `owned_identities` alone (C-5.4, C-5.6). A cross-boot attempt
    with an adopted counted identity, a stored responsible identity and a live process carrying
    its marker pair sends no signal to any of them, owns nothing new, and is quarantined while
    the marker process lives."""
    daemon, harness = state_daemon
    adopted = asdict(procs.ProcessIdentity(CHILD, BOOT_B, STRANGER_START))
    named = asdict(procs.ProcessIdentity(SHELL, BOOT_B, SHELL_START))
    job_id, attempt = trackb(daemon, harness, monkeypatch, counted_identities={str(CHILD): adopted},
                             responsible_identities={str(SHELL): named},
                             extra_rows=f"90 1 90 S {STRANGER_START}\n", marked=(90,), quarantined=False)
    signalled = []
    monkeypatch.setattr(procs.os, "kill", lambda pid, sig: signalled.append(pid))
    monkeypatch.setattr(procs.os, "killpg", lambda pid, sig: signalled.append(pid))
    daemon.term_grace_s = daemon.kill_settle_s = 0
    daemon._kill_attempt(daemon.store.get_attempt(attempt["attempt_id"]))
    assert signalled == []
    after = daemon.store.get_attempt(attempt["attempt_id"])
    assert after["state"] == "quarantined"
    reason = json.loads(after["quarantine_reason"])
    assert reason["marker_pids"] == [90] and reason["descendant_pids"] == []
    evidence = json.loads(after["evidence_json"])
    assert evidence.get("owned_identities", {}) == {}
    assert evidence["counted_identities"] == {str(CHILD): adopted}           # 90 is not kept from boot B
    assert evidence["responsible_identities"] == {str(SHELL): named}

