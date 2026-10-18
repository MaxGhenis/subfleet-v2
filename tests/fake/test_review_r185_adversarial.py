"""C-5.5--5.7: review fixture for a departing guardian and escaped writer."""

from dataclasses import asdict
import json
import subprocess

from subfleet import procs, protocol
from subfleet.contracts import Reading, ReadingLabel
from subfleet.daemon import after, utcnow
from tests.fake.test_state_contract import reserve, state_daemon  # noqa: F401
from tests.unit.test_procs import census


CONTAINMENT = procs.containment
SNAPSHOT = procs.snapshot
START = "Sun Oct 11 01:00:00 2026"
CHILD_START = "Sun Oct 11 01:00:01 2026"


def test_confirm_dead_keeps_escaped_writer_when_parent_exits_during_scan(state_daemon, monkeypatch):
    """C-5.5, C-5.7: the fresh census must not release a live escaped writer's lease."""
    daemon, harness = state_daemon
    def git(*args):
        return subprocess.run(["git", "-c", "core.fsmonitor=false", *args], cwd=harness.workdir,
                              capture_output=True, text=True, check=True, timeout=30).stdout.strip()

    git("init", "-b", "feature/review-r185")
    tracked = harness.workdir / "deliverable.txt"
    tracked.write_text("baseline\n")
    git("add", "deliverable.txt")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "baseline")
    daemon.store.add_reading(Reading("codex-1", "account", "seven_day", .1, after(3600),
                                    ReadingLabel.PROVIDER, "fixture", utcnow()))
    job_id, attempt, _ = reserve(daemon, harness, sandbox="workspace-write", in_place=True,
                                 no_preamble=True, max_attempts=1)
    tracked.write_text("writer is still modifying this workspace\n")
    daemon.store.update_attempt(
        attempt["attempt_id"], state="running", guardian_pid=42, pgid=42,
        child_pid=None, boot_id="unit-test-boot", proc_start=START,
        started_at="2026-10-11T01:00:00Z",
        evidence_json=json.dumps({"owned_identities": {
            "43": asdict(procs.ProcessIdentity(43, "unit-test-boot", CHILD_START))}}))
    attempt = daemon.store.get_attempt(attempt["attempt_id"])
    daemon._quarantine(attempt, procs.Containment(descendant_pids=frozenset({43})),
                       "termination could not verify containment")
    assert daemon.store.list_leases(attempt["attempt_id"]) or daemon.store.list_leases(job_id)
    # This is a consistent process table: the old guardian died after 43
    # escaped, 43 was reparented to init, and 42 now belongs to a stranger.
    # The kernel rule permits reuse because the old group/session 42 is gone;
    # it does not imply death of the still-matching owned member in session 43.
    # A restricted executable can hide its environment from the marker read.
    census(monkeypatch, parents="42 1 42 Ss Sun Oct 11 01:16:40 2026\n"
                                 f"43 1 43 Ss {CHILD_START}\n")
    monkeypatch.setattr(procs, "containment", CONTAINMENT)
    monkeypatch.setattr(procs, "snapshot", SNAPSHOT)
    current = daemon.store.get_attempt(attempt["attempt_id"])
    daemon._resolve_quarantine(current, protocol.KillArgs(job_id, confirm_dead=True))
    assert (daemon.store.list_leases(attempt["attempt_id"]) or daemon.store.list_leases(job_id)), \
        "released while PID 43 is still writing"
    assert daemon.store.get_attempt(attempt["attempt_id"])["state"] == "quarantined"
