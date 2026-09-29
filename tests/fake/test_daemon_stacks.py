"""C-3.6: `subfleet daemon stacks` dumps every thread of a live daemon process.

SIGUSR1 writes a native fallback and wakes the uncapped named-stack worker;
the CLI signals only the identity daemon.lock records and waits for the complete
named dump before printing it.

SIGUSR1's default action ends a process, so the CLI signals only a daemon
whose lock says `"stack_dumps": true`, which a daemon writes after it has
registered the handler and drops before it lets the handler go. A daemon built
before that flag (the installed ad2c208, version 2.0.0a0 like this one) never
receives the signal: the review of 5841d8b showed such a process exits -30.
"""

import faulthandler
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.fake.conftest import REPO

#: A process that records its identity in daemon.lock exactly as a daemon does
#: (C-5.3), with SIGUSR1 at its default action, as every daemon before the flag.
OLD_DAEMON = """
import json, os, signal, sys, time
from subfleet import procs
signal.signal(signal.SIGUSR1, signal.SIG_DFL)      # whatever the parent ignores
root = sys.argv[1]
ident = {"pid": os.getpid(), "boot_id": procs.boot_id(),
         "proc_start": procs.proc_start(os.getpid()), "version": "2.0.0a0"}
with open(os.path.join(root, "daemon.lock"), "w") as lock:
    lock.write(json.dumps(ident, sort_keys=True) + "\\n")
print("ready", flush=True)
time.sleep(120)
"""

#: The same, with the handler a daemon of this version registers, and the flag.
NEW_DAEMON = """
import faulthandler, json, os, signal, sys, time
from subfleet import procs
root = sys.argv[1]
log = open(os.path.join(root, "daemon.log"), "a")
faulthandler.register(signal.SIGUSR1, file=log, all_threads=True, chain=False)
ident = {"pid": os.getpid(), "boot_id": procs.boot_id(),
         "proc_start": procs.proc_start(os.getpid()), "version": "2.0.0a0", "stack_dumps": True}
with open(os.path.join(root, "daemon.lock"), "w") as lock:
    lock.write(json.dumps(ident, sort_keys=True) + "\\n")
print("ready", flush=True)

def parked_where_the_dump_can_find_it():
    time.sleep(120)

parked_where_the_dump_can_find_it()
"""

MANY_THREADS = """
import sys, threading, time
from pathlib import Path
from subfleet.daemon import Daemon
core = Daemon(Path(sys.argv[1]))
park = threading.Event()
ready = threading.Barrier(141)
def parked_where_the_complete_dump_can_find_it():
    ready.wait()
    park.wait()
threads = [threading.Thread(target=parked_where_the_complete_dump_can_find_it,
                            name=f"diagnostic-worker-{i}", daemon=True) for i in range(140)]
for thread in threads:
    thread.start()
ready.wait()
# Hold both application and logging locks across the signal: the dump must use
# neither. The Python signal callback also cannot try to reacquire these.
with core.store.transaction("diagnostic-lock-held"):
    with core._log_handler.lock:
        print("ready", flush=True)
        time.sleep(120)
"""


def stacks(root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "subfleet.cli", "daemon", "stacks", *extra],
        cwd=REPO, env={**os.environ, "SUBFLEET_HOME": str(root), "PYTHONPATH": str(REPO)},
        capture_output=True, text=True, timeout=60)


def spawn(script: str, root: Path) -> subprocess.Popen:
    child = subprocess.Popen([sys.executable, "-c", script, str(root)], cwd=REPO,
                             env={**os.environ, "PYTHONPATH": str(REPO)},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "ready", child.stderr.read()
    return child


@pytest.fixture
def root():
    with tempfile.TemporaryDirectory(prefix="sfs-", dir="/tmp") as directory:
        yield Path(directory)


def test_daemon_stacks_prints_every_thread_of_the_live_daemon(daemon):
    daemon.start()
    assert json.loads((daemon.root / "daemon.lock").read_text())["stack_dumps"] is True
    result = stacks(daemon.root)
    assert result.returncode == 0, result.stderr
    assert "Thread 0x" in result.stdout
    # The accept loop and the control loop are both in the dump.
    assert "serve_forever" in result.stdout and "_control" in result.stdout
    assert "stack dumps:" not in result.stdout           # only what the signal wrote
    assert daemon.call("ping")["pong"] is True             # the daemon lives on
    assert "Thread 0x" in (daemon.root / "daemon.log").read_text(errors="replace")


def test_daemon_stacks_includes_more_than_100_threads_while_store_is_locked(root):
    child = spawn(MANY_THREADS, root)
    try:
        result = stacks(root, "--wait", "15")
        assert result.returncode == 0, result.stderr
        named = result.stdout.split("=== subfleet all-thread stack dump:", 1)[1]
        for index in range(140):
            assert f"name='diagnostic-worker-{index}' " in named
        assert "name='MainThread' " in named
        assert "name='subfleet-stack-dump' " in named
        assert "=== end subfleet all-thread stack dump ===" in named
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(10)


def test_daemon_stacks_without_a_daemon_says_so():
    with tempfile.TemporaryDirectory(prefix="sfs-", dir="/tmp") as directory:
        result = stacks(Path(directory))
    assert result.returncode == 69, (result.returncode, result.stderr)
    assert "not running" in result.stderr


def test_a_daemon_without_the_flag_is_never_signalled(root):
    """Review of 5841d8b, finding 1: SIGUSR1 ends a daemon built before the flag."""
    control = spawn(OLD_DAEMON, root)                     # the fixture is what it claims to be
    os.kill(control.pid, signal.SIGUSR1)
    assert control.wait(10) == -signal.SIGUSR1
    old = spawn(OLD_DAEMON, root)
    try:
        result = stacks(root)
        assert result.returncode == 69, (result.returncode, result.stderr)
        assert f"the daemon at pid {old.pid} (version 2.0.0a0)" in result.stderr
        assert '"stack_dumps": true' in result.stderr and "refusing to signal it" in result.stderr
        assert f"sample {old.pid} 5" in result.stderr        # the way that still works
        assert result.stdout == ""
        time.sleep(.3)
        assert old.poll() is None                            # never signalled: it lives
    finally:
        old.kill()
        old.wait(10)


def test_a_daemon_whose_lock_says_stack_dumps_is_dumped_and_lives(root):
    child = spawn(NEW_DAEMON, root)
    try:
        result = stacks(root)
        assert result.returncode == 0, result.stderr
        assert "parked_where_the_dump_can_find_it" in result.stdout
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(10)


def test_a_lock_that_changes_between_the_two_reads_is_not_signalled(root, monkeypatch, capsys):
    """C-3.6: the CLI reads the lock again just before the signal. A daemon that
    began to stop after the first read has dropped the flag and may have let its
    handler go; the fixture is such a daemon, SIGUSR1 at its default action, whose
    lock said `stack_dumps` when first read. Review of 78a8476: nothing failed
    without the second read."""
    from subfleet import cli
    from subfleet.client import Client
    stopping = spawn(OLD_DAEMON, root)
    try:
        record = json.loads((root / "daemon.lock").read_text())
        reads = iter([{**record, "stack_dumps": True}, record])
        monkeypatch.setattr(Client, "lock_info", lambda self: next(reads))
        monkeypatch.setenv("SUBFLEET_HOME", str(root))
        assert cli.main(["daemon", "stacks"]) == 1
        err = capsys.readouterr().err
        assert "changed while it was being checked; refusing to signal pid" in err and "run it again" in err
        time.sleep(.3)
        assert stopping.poll() is None                       # never signalled: it lives
    finally:
        stopping.kill()
        stopping.wait(10)


def test_a_daemon_writes_the_flag_after_the_handler_and_drops_it_before(tmp_path, monkeypatch):
    """The order that makes the flag true whenever it is read (C-3.6)."""
    from subfleet import daemon as daemon_module
    seen = []
    register, unregister = faulthandler.register, faulthandler.unregister

    def lock_says(root):
        text = (root / "daemon.lock").read_text()
        return json.loads(text).get("stack_dumps") if text.strip() else None

    root = tmp_path / "state"
    monkeypatch.setattr(faulthandler, "register",
                        lambda *a, **k: seen.append(("register", lock_says(root))) or register(*a, **k))
    monkeypatch.setattr(faulthandler, "unregister",
                        lambda *a, **k: seen.append(("unregister", lock_says(root))) or unregister(*a, **k))
    core = daemon_module.Daemon(root)
    try:
        record = json.loads((root / "daemon.lock").read_text())
        assert record["stack_dumps"] is True and record["pid"] == os.getpid()
    finally:
        core.close()
    # The first unregister lets go of any earlier daemon's registration in this
    # process before registering (a no-op here); no call finds the flag set.
    assert seen == [("unregister", None), ("register", None), ("unregister", None)]
    record = json.loads((root / "daemon.lock").read_text())
    assert "stack_dumps" not in record and record["pid"] == os.getpid()   # the identity stays
