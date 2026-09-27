"""C-2.5 against the real `subfleetd`: the log rotates while the daemon runs,
and every writer of it follows into the new file.

2026-09-27: the installed daemon's `daemon.log` had reached 68 MB, never
rotated. The daemon here is started as launchd starts it, with stdout and
stderr appended to `daemon.log`, and rotates at 64 KiB.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess

from subfleet.daemonlog import backup_path


GRACE_S = 5.0
SLACK_S = 15.0


def descriptor_inodes(pid: int, fds: tuple[int, ...]) -> dict[int, int]:
    """Each descriptor's inode, as `lsof` reports it."""
    listing = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", ",".join(map(str, fds)), "-F", "fi"],
                             capture_output=True, text=True, timeout=30).stdout.splitlines()
    inodes, fd = {}, None
    for line in listing:
        if line.startswith("f"):
            fd = int(line[1:])
        elif line.startswith("i") and fd is not None:
            inodes[fd] = int(line[1:])
    return inodes


def test_c2_5_a_live_daemon_rotates_and_every_writer_lands_in_the_current_log(e2e):
    """The handler's lines, `daemon stacks` (SIGUSR1, C-3.6), stdout and stderr,
    and the C-5.8a stopping line and dump all reach the log current when they
    are written, not the file the rotation renamed."""
    e2e.policy_update(lambda policy: policy.update(
        daemon_log={"max_bytes": 65536, "backups": 3, "check_s": 0.1}))
    log = e2e.root / "daemon.log"
    e2e.start(output=log, env={
        # The worker that commits `attempt.running` parks (as in
        # test_stop_bound.py), so the stop waits out its grace and dumps.
        "SUBFLEET_E2E_HOLD_AT": "running",
        "SUBFLEET_E2E_STOP_GRACE_S": str(GRACE_S),
        "SUBFLEET_FAKE_RELEASE_PATH": str(e2e.root / "release-provider"),
    })
    submitted = e2e.cli(*e2e.run_args("astra", "-d"))
    assert submitted.rc == 0, submitted.stderr
    e2e.until((e2e.root / "hook-running.json").exists)

    first = os.stat(log)
    assert set(descriptor_inodes(e2e.process.pid, (1, 2)).values()) == {first.st_ino}
    with log.open("ab") as filler:                       # past 64 KiB, as a busy log gets
        filler.write(b"filler line\n" * 6000)
    e2e.until(lambda: os.stat(log).st_ino != first.st_ino
              and "daemon.log: rotated at" in log.read_text(errors="replace"), timeout=20)
    assert os.stat(backup_path(log, 1)).st_ino == first.st_ino
    current = os.stat(log)
    assert current.st_mode & 0o077 == 0
    # launchd's StandardOutPath and StandardErrorPath moved with the handler.
    assert descriptor_inodes(e2e.process.pid, (1, 2)) == {1: current.st_ino, 2: current.st_ino}

    stacks = e2e.cli("daemon", "stacks", timeout=60)
    assert stacks.rc == 0, stacks.stderr
    assert "Thread 0x" in stacks.stdout and " in _run\n" in stacks.stdout      # the rotation thread's loop
    assert "Thread 0x" in log.read_text(errors="replace")
    assert "Thread 0x" not in backup_path(log, 1).read_text(errors="replace")

    e2e.process.send_signal(signal.SIGTERM)
    assert e2e.process.wait(timeout=GRACE_S + SLACK_S) == 1          # C-5.8a: dumped, exit 1
    text = log.read_text(errors="replace")
    assert f"stopping: if this process is still running in {GRACE_S:g} s" in text
    dump = text[text.index("Timeout ("):]
    assert " in hold\n" in dump and " in close\n" in dump
    rotated = backup_path(log, 1).read_text(errors="replace")
    assert "stopping:" not in rotated and "Timeout (" not in rotated
    assert not backup_path(log, 2).exists()                           # one rotation, one file kept
    assert json.loads((e2e.root / "policy.json").read_text())["daemon_log"]["max_bytes"] == 65536
