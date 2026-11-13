"""C-6.10, C-6.8: a queue that is only waiting starts no process between its rechecks.

Incident, 2026-09-20: about fifteen processes a second came from the daemon while
twelve jobs waited, among them `git -C <worktree of a waiting job> add -A`: every
evaluable waiting job had its workspace prepared again once a second.

Ported from MaxGhenis/subfleet-v2#38 (5d1e88ff6), adapted to release/217's seams:
a process here starts through `subprocess.Popen` or through `procs.spawn_disclaimed`
(`posix_spawn`, D-CEN1), so both are counted, and so is every launch the
interpreter audits (`subprocess.Popen`, `os.posix_spawn`, `os.exec`, `os.spawn`,
`os.system`). It pins behaviour release/217 already has; it is no evidence for
C-3.8's event names.
"""

import subprocess
import sys

import pytest

from subfleet import procs
from subfleet.adapters.registry import register
from subfleet.contracts import ClockSource, Closure, ClosureReason, Reading, ReadingLabel
from subfleet.daemon import Daemon, after, utcnow
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter
from tests.unit.test_salvage import git

LAUNCH_EVENTS = frozenset({"subprocess.Popen", "os.posix_spawn", "os.exec", "os.spawn", "os.system"})
#: The list the audit hook appends to while a test counts; None the rest of the time.
#: An audit hook cannot be removed, so it is added once and is inert between tests.
_COUNTING: list | None = None
_HOOKED = False


def _audit(event, args):
    if _COUNTING is not None and event in LAUNCH_EVENTS:
        _COUNTING.append((event, repr(args)[:200]))


@pytest.fixture
def spawns(monkeypatch):
    global _COUNTING, _HOOKED
    if not _HOOKED:
        sys.addaudithook(_audit)
        _HOOKED = True
    calls: list = []
    real_popen, real_spawn = subprocess.Popen, procs.spawn_disclaimed

    class CountingPopen(real_popen):
        """`run`, `check_output` and `call` all reach the module's `Popen`."""

        def __init__(self, args, *rest, **kwargs):
            calls.append(("Popen", repr(args)[:200]))
            super().__init__(args, *rest, **kwargs)

    def counting_spawn(argv, **kwargs):
        calls.append(("spawn_disclaimed", repr(argv)[:200]))
        return real_spawn(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", CountingPopen)
    monkeypatch.setattr(procs, "spawn_disclaimed", counting_spawn)
    _COUNTING = calls
    try:
        yield calls
    finally:
        _COUNTING = None


@pytest.fixture
def service(tmp_path, monkeypatch, process_inspection_available):
    root = tmp_path / "state"
    root.mkdir()
    harness = Harness(root)
    register("codex", FakeAdapter)
    daemon = Daemon(harness.root, desktop_prober=lambda: None)
    monkeypatch.setattr(daemon, "_launch", lambda *args: pytest.fail("nothing here may launch a guardian"))
    daemon.store.add_reading(Reading("codex-1", "account", "seven_day", .2, after(86400),
                                     ReadingLabel.PROVIDER, "fixture", utcnow()))
    try:
        yield daemon, harness
    finally:
        daemon.close()


def repository(path):
    path.mkdir(parents=True)
    git(path, "init", "-b", "task/example")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "tracked.txt").write_text("baseline\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "baseline")
    return path


def test_c6_10_a_waiting_queue_starts_no_process_between_its_rechecks(service, spawns, tmp_path):
    """C-6.10, C-6.8 once every job waits on a clock, fifty passes start no git and no ps."""
    daemon, harness = service
    daemon.store.put_closure(Closure("codex-1", "account", after(3600), ClosureReason.PROVIDER_LIMIT,
                                     ClockSource.REPORTED, "fixture"))
    # Three tiers so no job is held behind another; a checkout each, since two
    # writable jobs may not share one; writable ones are what ran `git add -A`.
    jobs = [daemon.dispatch("submit", harness.submit_args(workdir=str(repository(tmp_path / tier)), tier=tier,
                                                          caller_session=None, sandbox=sandbox,
                                                          in_place=in_place))["job_id"]
            for tier, sandbox, in_place in (("easy", "workspace-write", False), ("standard", "workspace-write", True),
                                            ("hard", "read-only", False))]
    daemon._admit()                                   # the first look: each is prepared and waits
    assert [daemon.store.get_job(job)["wait_reason"] for job in jobs] == ["capacity"] * 3
    # Where a wait that keeps its verdict sits once backed off (C-6.10's ceiling):
    # the first recheck is 1 s out, and fifty passes can take longer than that.
    with daemon.store.transaction("test.backed_off") as tx:
        tx.execute("UPDATE jobs SET next_check_at=? WHERE state='waiting'", (after(30),))
    assert spawns, "the first look prepared three workspaces with git, and the seam saw none of it"
    spawns.clear()
    for _ in range(50):
        daemon._admit()
    assert spawns == [], f"{len(spawns)} processes while nothing was due: {sorted(set(spawns))[:6]}"
    assert daemon.store.list_attempts() == []
