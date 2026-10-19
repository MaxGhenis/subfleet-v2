"""D-KILL2, C-5.5 on the real kernel: a setsid'd lockf that outlives its cancelled attempt.

Incidents 2026-10-16 (pr219-fix4, lockf 14354) and 2026-10-18 (pr223-fix6, lockf
70848): a Codex build's command ran `/usr/bin/lockf -k <suite lock> ...` as the leader
of a session of its own. The attempt was cancelled, lockf (ppid 1, its own group)
outlived it waiting for the lock, and the census of the installed daemon (c69757256,
copied in `tests/reference_census_c697.py`) never listed it, so the attempt was
released while it waited. lockf is an Apple platform binary signed with the hardened
runtime; on macOS 26 such a process's `KERN_PROCARGS2` buffer holds its argv and no
environment (observed 2026-10-18), so the markers it inherited show neither in
`ps -E` nor in the raw buffer: no marker, no group, no parent link. Only C-5.5's
fourth source, responsibility (D-CEN1), names it, and only while the root lives; the
identity the census records then keeps it counted after the root's SIGKILL.

Each test runs a real guardian (`python -m subfleet.guardian`) whose provider is a
shell that starts the lockf, setsid'd, on a scratch lock the test holds (never the
suite lock), with the markers a provider's command inherits. Termination is the kill
protocol's signals, and every census is `Daemon._contain`, as termination takes it.
Every process here is this test's own and is stopped by its recorded identity, on
failure too; lockf waits at most two minutes for its lock.
"""

from __future__ import annotations

import ctypes
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

from subfleet import procs
from subfleet.daemon import Daemon
from tests import reference_census_c697 as installed
from tests.unit.test_census_procargs import Store

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="C-5.5's census reads macOS process state")

REPO = Path(__file__).resolve().parents[2]
#: The provider: a shell that starts lockf in a session of its own, as the incident's
#: command did, publishes its pid, optionally does the same for a python process that
#: shows its markers (`$4`), and stays until the kill protocol's SIGTERM.
PROVIDER = r"""
"$1" -c 'import os, sys; os.setsid(); os.execv("/usr/bin/lockf", ["/usr/bin/lockf", "-k", "-t", "120", sys.argv[1], "/bin/sleep", "60"])' "$2" &
echo $! > "$3/lockf.pid.tmp" && mv "$3/lockf.pid.tmp" "$3/lockf.pid"
if [ -n "$4" ]; then
  "$1" -c 'import os, time; os.setsid(); time.sleep(60)' &
  echo $! > "$3/carrier.pid.tmp" && mv "$3/carrier.pid.tmp" "$3/carrier.pid"
fi
exec /bin/sleep 60
"""


@pytest.fixture(scope="module", autouse=True)
def macos_inspection():
    try:
        if procs.identity(os.getpid()) is None:
            pytest.skip("C-5.3 process identity unavailable")
    except procs.InspectionError:
        pytest.skip("C-5.3 host sandbox denies ps/sysctl; no ownership bypass")


@pytest.fixture
def held_lock(tmp_path):
    """A scratch lock this test holds, so lockf waits as 70848 waited for the suite lock."""
    path = tmp_path / "scratch.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        os.close(fd)


def rooted_only():
    if not procs.disclaim_available():
        pytest.skip("this macOS cannot start a process as its own responsible process")


def executable(pid: int) -> str | None:
    """`proc_pidpath`: the file the kernel runs for `pid` now; None once it is gone."""
    libc = ctypes.CDLL(None, use_errno=True)
    buffer = ctypes.create_string_buffer(4096)
    size = libc.proc_pidpath(ctypes.c_int(pid), buffer, ctypes.c_uint32(len(buffer)))
    return buffer.value.decode() if size > 0 else None


def until(predicate, what: str, timeout: float = 30):
    deadline = time.monotonic() + timeout
    while True:
        found = predicate()
        if found:
            return found
        assert time.monotonic() < deadline, f"{what} did not happen within {timeout} s"
        time.sleep(.02)


def published(path: Path) -> int:
    return int(until(lambda: path.exists() and path.read_text().strip(), f"{path.name} published"))


def stop_identity(identity: procs.ProcessIdentity | None) -> None:
    """SIGKILL a process this test started, only while it is still that process (C-5.3)."""
    if identity is None:
        return
    procs.signal_process(identity, signal.SIGKILL)
    until(lambda: procs.liveness(identity.pid, identity.boot_id, identity.proc_start) != "alive",
          f"pid {identity.pid} to leave after SIGKILL", timeout=10)


def marked(pid: int, attempt_id: str, root: str) -> bool | None:
    """Does `pid`'s raw argument buffer carry the marker? None when the kernel refused it."""
    found = procs.kernel.arguments(pid, ctypes.create_string_buffer(procs._ARGS_BUFFER))
    return None if found is None else procs.procargs_marked(found, *procs.marker_strings(attempt_id, root))


class FakeAttempt:
    """A running attempt without a daemon: a real guardian, rooted as a self-responsible
    daemon starts it or unrooted as Popen starts it, the row `Daemon._contain` reads, and
    the lockf (and optional marker-showing carrier) its provider left."""

    def __init__(self, tmp_path: Path, lock: Path, *, rooted: bool, carrier: bool = False):
        self.state_root = tmp_path / "state"
        self.job_id = f"kill2-{uuid.uuid4().hex[:8]}"
        self.attempt_id = f"{self.job_id}/a1"
        self.directory = self.state_root / "jobs" / self.job_id / "a1"
        self.out = tmp_path / "out"
        work = tmp_path / "work"
        for directory in (self.directory, self.out, work):
            directory.mkdir(parents=True)
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO), "SUBFLEET_ATTEMPT": self.attempt_id,
               "SUBFLEET_JOB": self.job_id, "SUBFLEET_ROOT": str(self.state_root)}
        argv = [sys.executable, "-m", "subfleet.guardian", "--attempt-dir", str(self.directory),
                "--cwd", str(work), "--stdout-path", str(self.out / "stdout"),
                "--stderr-path", str(self.out / "stderr"), "--",
                "/bin/sh", "-c", PROVIDER, "provider", sys.executable, str(lock), str(self.out),
                "1" if carrier else ""]
        self.lockf = self.carrier = self.start = None
        if rooted:
            self.guardian = procs.spawn_disclaimed(argv, env=env, cwd=str(REPO))
        else:
            self.guardian = subprocess.Popen(argv, env=env, cwd=REPO, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.start = json.loads(str(until(lambda: (self.directory / "start.json").exists()
                                              and (self.directory / "start.json").read_text(), "start.json")))
            lockf = published(self.out / "lockf.pid")
            until(lambda: executable(lockf) == "/usr/bin/lockf", "the provider's child to become lockf")
            self.lockf = procs.identity(lockf)
            if carrier:
                self.carrier = procs.identity(published(self.out / "carrier.pid"))
            root = procs.responsibility_root(self.guardian.pid) if rooted else None
            assert (root is not None) == rooted, root
            self.row = {"attempt_id": self.attempt_id, "job_id": self.job_id, "seq": 1,
                        "directory": str(self.directory), "guardian_pid": self.start["guardian_pid"],
                        "pgid": self.start["pgid"], "child_pid": None, "boot_id": self.start["boot_id"],
                        "proc_start": self.start["proc_start"], "started_at": self.start["started_at"],
                        "evidence_json": json.dumps({"responsible_root": root} if root else {})}
            self.core = SimpleNamespace(root=self.state_root, store=Store(self.row), _read_json=self.read_json)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def read_json(path: Path) -> dict | None:
        return json.loads(path.read_text()) if path.exists() else None

    def census(self) -> procs.Containment:
        """Termination's census of this attempt (C-5.5), recorded evidence and all."""
        return Daemon._contain(self.core, self.row)

    def installed_census(self) -> procs.Containment:
        """What the installed daemon's `_contain` asked of its census (c69757256)."""
        receipt = self.read_json(self.directory / "exit.json") or {}
        return installed.containment(self.row["pgid"], self.row["guardian_pid"],
                                     self.row["child_pid"] or receipt.get("child_pid"),
                                     self.attempt_id, root=str(self.state_root))

    def signal(self, sig: int) -> bool:
        """The kill protocol's group signal, sent only while the recorded leader is ours (C-5.4)."""
        return procs.signal_group(self.start["pgid"], sig, boot_id=self.start["boot_id"],
                                  proc_start=self.start["proc_start"])

    def provider_gone(self) -> None:
        """After SIGTERM the provider leaves and the guardian writes its exit receipt."""
        until(lambda: (self.directory / "exit.json").exists(), "the guardian's exit receipt")

    def guardian_gone(self) -> None:
        self.guardian.wait(timeout=30)

    def close(self) -> None:
        stop_identity(self.lockf)
        stop_identity(self.carrier)
        if self.start is not None and self.guardian.poll() is None:
            procs.signal_group(self.start["pgid"], signal.SIGKILL, boot_id=self.start["boot_id"],
                               proc_start=self.start["proc_start"])
        if self.guardian.poll() is None:
            self.guardian.kill()       # this test's own unreaped child: its pid is still its own
        self.guardian.wait(timeout=30)


def emptied(read) -> procs.Containment:
    """Read the census again until it empties: a process SIGKILLed a moment ago can still
    be in the table (launchd reaps it), and the census errs toward holding on."""
    return until(lambda: (lambda census: census if census.verified_empty else None)(read()),
                 "a verified-empty census", timeout=10)


def test_d_kill2_lockf_shows_no_environment_where_python_shows_its_markers(tmp_path, held_lock):
    """The incident's premise, checked on this kernel so the regressions below are the
    incident's: the lockf an attempt's provider leaves carries the attempt's markers
    in its environment, but its argument buffer holds only its argv, so `ps -E` and the
    raw read alike see none; a python process the same provider starts the same way
    shows them in both."""
    attempt = FakeAttempt(tmp_path, held_lock, rooted=False, carrier=True)
    try:
        root = str(attempt.state_root)
        buffer = procs.kernel.arguments(attempt.lockf.pid, ctypes.create_string_buffer(procs._ARGS_BUFFER))
        assert buffer is not None
        strings = procs.procargs_strings(buffer)
        assert b"/usr/bin/lockf" in strings and str(held_lock).encode() in strings
        assert not [string for string in strings if string.startswith(b"SUBFLEET_")], \
            "lockf's buffer shows its environment: the marker source can see it on this macOS"
        assert marked(attempt.carrier.pid, attempt.attempt_id, root) is True
        shown = subprocess.run(["/bin/ps", "-Eww", "-o", "pid=,command=", "-p",
                                f"{attempt.lockf.pid},{attempt.carrier.pid}"],
                               capture_output=True, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}).stdout
        rows = {int(row.split(None, 1)[0]): row.split() for row in shown.decode(errors="replace").splitlines()}
        marker = f"SUBFLEET_ATTEMPT={attempt.attempt_id}"
        assert marker not in rows[attempt.lockf.pid] and marker in rows[attempt.carrier.pid]
    finally:
        attempt.close()


def cancel(attempt: FakeAttempt) -> None:
    """The kill protocol's signals: SIGTERM to the group, then SIGKILL while its recorded
    leader is still ours. The provider leaves at SIGTERM. A rooted guardian ignores it and
    stays while anything names it; an unrooted one writes its receipt and leaves."""
    assert attempt.signal(signal.SIGTERM)
    attempt.provider_gone()
    if attempt.guardian.poll() is None:
        attempt.signal(signal.SIGKILL)       # an unrooted guardian may be leaving by itself
    attempt.guardian_gone()


@pytest.mark.parametrize("rooted", [True, False], ids=["rooted", "unrooted"])
@pytest.mark.parametrize("census", [
    "branch",
    pytest.param("installed-c697", marks=pytest.mark.xfail(strict=True, reason=(
        "D-KILL2: the installed census reads markers with `ps -axEww`, which shows lockf no "
        "environment, has no responsibility source, and keeps no walk member it saw"))),
])
def test_d_kill2_a_setsid_lockf_that_outlives_its_cancelled_attempt_is_counted(tmp_path, held_lock, census,
                                                                              rooted):
    """D-KILL2, C-5.5: the regression. An attempt is cancelled with the kill protocol's
    signals while its provider's lockf waits for a lock in a session of its own, and
    every census termination takes counts the lockf until it is gone. The first,
    before any signal, counts it as the provider's child (and, rooted, as naming the
    guardian) and records it. After SIGTERM the provider is gone and lockf has ppid 1
    and no visible marker; the recorded identity seeds the walk, and keeps doing so
    after the guardian's SIGKILL, when nothing names the guardian any more. The
    installed census (strict xfail) loses it at SIGTERM."""
    if rooted:
        rooted_only()
    attempt = FakeAttempt(tmp_path, held_lock, rooted=rooted)
    read = attempt.census if census == "branch" else attempt.installed_census
    lockf = attempt.lockf.pid
    try:
        first = read()                                        # the kill protocol's first census
        assert lockf in first.live_pids and not first.verified_empty, first.to_dict()
        assert attempt.signal(signal.SIGTERM)
        attempt.provider_gone()
        second = read()
        assert lockf in second.live_pids and not second.verified_empty, second.to_dict()
        assert second.shapes[lockf]["ppid"] == 1 and second.shapes[lockf]["pgid"] == lockf   # the incident's shape
        if rooted:
            assert attempt.signal(signal.SIGKILL)         # it stayed: lockf names it
        attempt.guardian_gone()                           # unrooted, it left at its receipt
        third = read()
        assert lockf in third.live_pids and not third.verified_empty, third.to_dict()
        # Which source saw it: the walk while its parent lived, responsibility while the
        # root lived, then the identity the first census recorded. Never its marker.
        assert lockf in first.descendant_pids
        assert lockf not in first.marker_pids | second.marker_pids | third.marker_pids
        assert lockf in second.descendant_pids and lockf in third.descendant_pids
        assert lockf not in third.responsible_pids
        recorded = json.loads(attempt.row["evidence_json"])
        if rooted:
            assert lockf in first.responsible_pids and lockf in second.responsible_pids
            assert recorded["responsible_identities"][str(lockf)]["proc_start"] == attempt.lockf.proc_start
        else:
            assert not first.responsible_pids
            assert recorded["counted_identities"][str(lockf)]["proc_start"] == attempt.lockf.proc_start
        assert "owned_identities" not in recorded                              # evidence, never authority
        stop_identity(attempt.lockf)
        emptied(read)
    finally:
        attempt.close()


def test_d_kill2_a_lockf_left_before_any_census_is_found_by_responsibility_alone(tmp_path, held_lock):
    """D-CEN1's source is the only one for a lockf whose provider was gone before the
    attempt's first census, as when the provider ends by itself and C-5.9's census
    follows, or the kill protocol's SIGTERM lands first: no marker, no group, ppid 1,
    no earlier record. It names the rooted guardian, which stays while it does; that
    census records it, and after the guardian's SIGKILL the record keeps it counted."""
    rooted_only()
    attempt = FakeAttempt(tmp_path, held_lock, rooted=True)
    lockf = attempt.lockf.pid
    try:
        assert attempt.signal(signal.SIGTERM)
        attempt.provider_gone()
        first = attempt.census()
        assert lockf in first.responsible_pids, first.to_dict()
        assert lockf not in first.marker_pids | first.group_pids | first.descendant_pids
        assert first.shapes[lockf]["ppid"] == 1
        assert attempt.signal(signal.SIGKILL)
        attempt.guardian_gone()
        last = attempt.census()
        assert lockf in last.descendant_pids and not last.verified_empty, last.to_dict()
        stop_identity(attempt.lockf)
        emptied(attempt.census)
    finally:
        attempt.close()


def test_d_kill2_the_installed_census_was_verified_empty_while_lockf_waited(tmp_path, held_lock):
    """D-KILL2 as it happened, on an attempt without a root as the installed daemon starts
    them. The installed census counts lockf while its parent, the provider, lives (the
    walk), never by its hidden marker, and keeps no record of it; after SIGTERM it is
    verified empty while lockf still waits, the answer `kill --confirm-dead` released on.
    This branch's census of the same moment still counts it, from its first census's
    record."""
    attempt = FakeAttempt(tmp_path, held_lock, rooted=False)
    lockf = attempt.lockf.pid
    try:
        seen = attempt.installed_census()
        assert lockf in seen.descendant_pids and lockf not in seen.marker_pids
        attempt.census()                                    # termination's first census records it
        cancel(attempt)
        assert procs.liveness(lockf, attempt.lockf.boot_id, attempt.lockf.proc_start) == "alive"
        assert emptied(attempt.installed_census).verified_empty
        assert lockf in attempt.census().descendant_pids
    finally:
        attempt.close()


def test_d_kill2_an_unrooted_attempt_counts_a_setsid_marker_carrier_by_its_raw_buffer(tmp_path, held_lock):
    """C-5.5's marker source: without a root, a process the provider started in a session
    of its own whose buffer shows the markers is counted by them alone, after the
    provider is gone (ppid 1) and the guardian has left at its receipt, though no census
    saw it before."""
    attempt = FakeAttempt(tmp_path, held_lock, rooted=False, carrier=True)
    carrier = attempt.carrier.pid
    try:
        cancel(attempt)
        census = attempt.census()
        assert carrier in census.marker_pids, census.to_dict()
        assert carrier not in census.group_pids | census.descendant_pids | census.responsible_pids
        assert census.shapes[carrier]["ppid"] == 1 and not census.verified_empty
    finally:
        attempt.close()


@pytest.mark.documented_limitation
def test_d_kill2_a_hidden_lockf_no_census_saw_is_invisible_without_a_root(tmp_path, held_lock):
    """C-5.5's limit: an attempt without a responsibility root (a daemon started under a
    terminal or an app, a macOS without the SPI, or an attempt launched before roots were
    recorded) has three sources. A hidden-environment lockf that left the walk before
    any census of the attempt counted it (here: no census until after SIGTERM) is seen
    by none of them, and the census is verified empty while it waits. A change that
    closes this must change this test."""
    attempt = FakeAttempt(tmp_path, held_lock, rooted=False)
    try:
        cancel(attempt)
        assert emptied(attempt.census).verified_empty
        assert procs.liveness(attempt.lockf.pid, attempt.lockf.boot_id, attempt.lockf.proc_start) == "alive"
    finally:
        attempt.close()


def test_d_kill2_a_failed_responsibility_read_of_the_lockf_is_never_contained(tmp_path, held_lock, monkeypatch):
    """C-5.5: an unverifiable census never counts as contained. The provider is gone before
    termination's first census, so only responsibility can name the lockf, and every
    responsibility read of it fails: no census counts or records it. After the
    guardian's SIGKILL nothing is counted at all, and the census is still not verified
    empty, so termination quarantines instead of releasing."""
    rooted_only()
    attempt = FakeAttempt(tmp_path, held_lock, rooted=True)
    lockf = attempt.lockf.pid
    real = procs.kernel.responsible

    def responsible(pid: int) -> int | None:
        if pid == lockf:
            raise procs.InspectionError(f"responsibility of pid {pid} unavailable")
        return real(pid)
    monkeypatch.setattr(procs.kernel, "responsible", responsible)
    try:
        assert attempt.signal(signal.SIGTERM)
        attempt.provider_gone()
        first = attempt.census()
        assert lockf not in first.live_pids and first.unverifiable, first.to_dict()
        assert attempt.signal(signal.SIGKILL)
        attempt.guardian_gone()
        last = attempt.census()
        assert not last.live_pids, last.to_dict()
        assert f"responsibility read unavailable for pid {lockf}" in last.errors
        assert last.unverifiable and not last.verified_empty
        recorded = json.loads(attempt.row["evidence_json"])
        assert str(lockf) not in {**recorded.get("responsible_identities", {}), **recorded.get("counted_identities", {})}
    finally:
        attempt.close()
