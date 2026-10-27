"""Standalone fake daemon; all fault injection stays in the test harness.

It never outlives the test process that started it (`ParentWatch`).
"""

from __future__ import annotations

import argparse
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import threading
import time

#: A fake daemon a test started must not outlive that test (#250 fix round 2c). On
#: 2026-10-27 round 2b's provider exited during a hold at load 205 and its pytest went
#: with it, before any teardown ran. The fake daemon it had started for
#: `test_quar_start1_daemon.py`, in a session of its own (`isolated_group`), ran on with
#: ppid 1 until the hub stopped it by pid. It looks at its parent this often, and once
#: the parent is gone it has this long to stop before it ends itself.
PARENT_POLL_S = 0.2
PARENT_GONE_EXIT_S = 10.0
EX_PARENT_GONE = 75


def end_attempt_groups(root: Path) -> None:
    """KILL the group of every attempt guardian recorded under `root` (the store's rows
    and the guardians' `start.json` receipts) that is still alive by its recorded
    identity (pid, boot, start), as the test harness's teardown does: the test that
    started them is over. A starting guardian (no `start.json`) is ended too."""
    from subfleet.procs import same_process
    found: set[tuple] = set()
    with suppress(sqlite3.Error, OSError):
        with sqlite3.connect(f"file:{root / 'state.sqlite3'}?mode=ro", uri=True) as db:
            found.update(db.execute("SELECT guardian_pid, pgid, boot_id, proc_start FROM attempts "
                                    "WHERE guardian_pid IS NOT NULL AND pgid IS NOT NULL").fetchall())
    for receipt in root.glob("jobs/*/a*/start.json"):
        with suppress(OSError, ValueError, KeyError, TypeError):
            start = json.loads(receipt.read_text())
            found.add((start["guardian_pid"], start["pgid"], start["boot_id"], start["proc_start"]))
    for guardian, pgid, boot, started in found:
        with suppress(OSError, ValueError, TypeError):
            if same_process(int(guardian), boot, started):
                os.killpg(int(pgid), signal.SIGKILL)


class ParentWatch:
    """Stop this daemon when the process that started it is gone.

    `parent` is the pid the harness passed (`--parent-pid`, its own) or, without it, this
    process's parent when it began. The kernel reparents an orphan, so once `os.getppid()`
    differs the parent has exited, whatever its pid is used for later; a harness that died
    before this process could look is caught the same way, as its pid was passed. The
    daemon is then stopped as SIGTERM stops it, and `end_attempt_groups` runs after its
    close. A stop that has not ended the process within `PARENT_GONE_EXIT_S` (a worker that
    will not finish, a hold hook nobody releases) ends the groups and the process."""

    def __init__(self, parent: int, root: Path):
        self.parent, self.root = parent, root
        self.daemon = None
        self.gone = threading.Event()
        threading.Thread(target=self._watch, name="fake-daemon-parent-watch", daemon=True).start()

    def _watch(self) -> None:
        while os.getppid() == self.parent:
            time.sleep(PARENT_POLL_S)
        self.gone.set()
        daemon = self.daemon
        if daemon is None:
            os._exit(EX_PARENT_GONE)                   # gone before the daemon existed: nothing launched
        with suppress(Exception):
            daemon.log.warning("fake daemon: parent pid %d is gone; stopping", self.parent)
        daemon.stopping.set()
        time.sleep(PARENT_GONE_EXIT_S)                 # a clean stop has ended the process by now
        end_attempt_groups(self.root)
        os._exit(EX_PARENT_GONE)


def audit_publication(path: Path) -> None:
    """Record actual fsync/rename calls without changing their behavior."""
    sync = os.fsync
    rename = os.rename
    replace = os.replace
    lock = threading.Lock()

    def record(data: dict) -> None:
        with lock, path.open("a") as stream:
            stream.write(json.dumps(data) + "\n")

    def fsync(fd: int) -> None:
        info = os.fstat(fd)
        sync(fd)
        record({"op": "fsync", "ino": info.st_ino,
                "directory": stat.S_ISDIR(info.st_mode)})

    def publish(original, source, target, *args, **kwargs):
        info = os.stat(source)
        directory = os.stat(Path(target).parent)
        result = original(source, target, *args, **kwargs)
        record({"op": "rename", "ino": info.st_ino,
                "dir_ino": directory.st_ino, "target": str(target)})
        return result

    os.fsync = fsync
    os.rename = lambda src, dst, *a, **kw: publish(rename, src, dst, *a, **kw)
    os.replace = lambda src, dst, *a, **kw: publish(replace, src, dst, *a, **kw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--crash-at")
    parser.add_argument("--hold-at")
    parser.add_argument("--missing-start", action="store_true")
    parser.add_argument("--start-delay", type=float, default=0)
    parser.add_argument("--publication-audit", action="store_true")
    parser.add_argument("--gate-peer", action="store_true")
    parser.add_argument("--max-connections", type=int)
    parser.add_argument("--connection-idle-s", type=float)
    parser.add_argument("--inspection-witness", type=Path)
    parser.add_argument("--inspect-interval-s", type=float, default=1.0)
    # D-KILL4 fix 1: a start grace longer than a short --start-delay, and a TERM grace
    # long enough to observe a TERM-ignoring escaper between the TERM and the KILL.
    parser.add_argument("--start-grace-s", type=float, default=.65)
    parser.add_argument("--term-grace-s", type=float, default=.08)
    # D-QUAR-START1 (a): the bound on start grace's extension for a live, running guardian.
    parser.add_argument("--start-grace-max-s", type=float)
    parser.add_argument("--hide-environment-of", type=Path, action="append", default=[],
                        help="a file a test's process writes its pid to; the census reads that "
                             "pid's argument buffer without its environment (D-KILL2)")
    parser.add_argument("--parent-pid", type=int,
                        help="the test process that started this daemon (default: its parent at start); "
                             "the daemon stops once that process is gone (ParentWatch)")
    args = parser.parse_args()
    root = args.state_root
    watch = ParentWatch(args.parent_pid or os.getppid(), root)
    if args.publication_audit:
        audit_publication(root / "publication.jsonl")

    from subfleet import descriptors
    from subfleet.adapters.registry import register
    from subfleet.daemon import Daemon, DaemonUnavailable, log_open_file_limit
    from tests.fake import profile as fake_profile
    from tests.fake_adapter import FakeAdapter

    if args.gate_peer:
        from tests.fake.gate_peer import FakeGateAdapter
        register("codex", FakeGateAdapter)
    else:
        register("codex", FakeAdapter)
    # C-10.6, C-10.3: the profile endpoint is a fixture and the desktop app's
    # keychain item is never read — this harness runs under a real HOME.
    fake_profile.install()
    # C-6.13: nor is the host's load, which belongs to whatever else it runs:
    # the machine guard reads a quiet machine unless a test stages a reading.
    import subfleet.machine
    staged = root / "machine.json"
    subfleet.machine.read = lambda: (json.loads(staged.read_text()) if staged.exists() else
                                     {"load1": 0.5, "load5": 0.5, "cpus": 8, "memory_pressure": 1, "observed_at": 0.0})
    if args.hide_environment_of:
        # D-KILL2: the incident's hidden markers on any macOS. GitHub's macOS 15.7.9
        # runner shows lockf's environment, which macOS 26.6.2 never returns.
        from subfleet import procs
        from tests.hidden_environment import HidingKernel, published
        procs.kernel = HidingKernel(procs.kernel, published(args.hide_environment_of))

    def hook(boundary: str, job_id: str, attempt_id: str) -> None:
        if boundary not in {args.crash_at, args.hold_at}:
            return
        (root / f"hook-{boundary}.json").write_text(json.dumps(
            {"job_id": job_id, "attempt_id": attempt_id}))
        if boundary == args.hold_at:
            while not (root / "release-hook").exists():
                time.sleep(.01)
        if boundary == args.crash_at:
            if args.missing_start:
                with sqlite3.connect(f"file:{root / 'state.sqlite3'}?mode=ro", uri=True) as db:
                    guardian = db.execute(
                        "SELECT guardian_pid FROM attempts WHERE attempt_id=?", (attempt_id,)
                    ).fetchone()[0]
                os.kill(guardian, signal.SIGKILL)
            os.kill(os.getpid(), signal.SIGKILL)
            # kill() can return before the kernel has torn down every thread.
            # This thread must never get past the boundary it simulates a crash
            # at: returning let `reserved` record the pending launch, and the
            # dying daemon launched the attempt about a quarter of the time.
            while True:
                time.sleep(1)

    # As `subfleet.daemon.main` does (C-16.6): raise the open-file limit first,
    # so the connection cap is derived from what the process can really open.
    limits = descriptors.raise_open_file_limit()
    connections = {key: value for key, value in (
        ("max_connections", args.max_connections),
        ("connection_idle_s", args.connection_idle_s)) if value is not None}
    try:
        daemon = Daemon(root, tick_s=.02, start_grace_s=args.start_grace_s, term_grace_s=args.term_grace_s,
                        kill_settle_s=1.0, exit_settle_s=1.0,
                        guardian_start_delay_s=args.start_delay, crash_hook=hook,
                        inspect_interval_s=args.inspect_interval_s,
                        **connections)
    except DaemonUnavailable:
        return 69
    log_open_file_limit(daemon.log, *limits)
    if args.start_grace_max_s is not None:
        daemon.start_grace_max_s = args.start_grace_max_s
    if args.inspection_witness:
        original = daemon._record_owned

        def witnessed(attempt, table):
            original(attempt, table)
            try:
                pid = int(args.inspection_witness.read_text())
            except (OSError, ValueError):
                return
            if table.live(pid):
                Path(str(args.inspection_witness) + ".inspected").touch()
        daemon._record_owned = witnessed
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: daemon.stopping.set())
    watch.daemon = daemon
    if watch.gone.is_set():
        daemon.stopping.set()                          # the watch saw no daemon yet; it may be exiting
    try:
        daemon.serve_forever()
    finally:
        daemon.close()
        if watch.gone.is_set():
            end_attempt_groups(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
