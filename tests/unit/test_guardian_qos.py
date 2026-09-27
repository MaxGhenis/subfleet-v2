"""C-5.1: the guardian starts each provider clamped to the `utility` QoS (2026-09-27).

The daemon runs at the default QoS (launchd `ProcessType` `Interactive`); agent work must
not. `subfleet.qos.spawn` starts the provider with `posix_spawn` and the QoS attribute, and
must start it exactly as `subprocess.Popen` did: the same OSError when it cannot, the same
descriptors, signals, directory and environment when it can. The review of 885142a5
(Astra) found the first version's taskpolicy wrapper read a spawn failure out of the
provider's stderr; these tests pin the replacement against Popen itself. The priorities
real processes get are checked in tests/process/test_guardian_qos_process.py and, through a
real daemon, in tests/fake/test_provider_qos.py.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import guardian, qos

pytestmark = pytest.mark.skipif(qos._LIBC is None,
                                reason="C-5.1's clamp is posix_spawn's QoS attribute, which is macOS's")


@pytest.fixture
def guardian_identity(monkeypatch):
    monkeypatch.setattr(guardian.os, "setsid", lambda: None)
    monkeypatch.setattr(guardian.signal, "signal", lambda *args: None)
    monkeypatch.setattr(guardian, "boot_id", lambda: "test-boot")
    monkeypatch.setattr(guardian, "proc_start", lambda pid: "test-start")
    previous = os.umask(0o077)
    yield
    os.umask(previous)


def guard(tmp_path, argv, mode, monkeypatch, *, name=None, cwd=None, stdin_path=None):
    """One guardian run with the clamp on (`utility`) or off (`inherit`); returns rc and its directory."""
    attempt = tmp_path / (name or mode)
    attempt.mkdir(exist_ok=True)
    monkeypatch.setenv(qos.PROVIDER_QOS_ENV, mode)
    rc = guardian.run_guardian(argv, attempt_dir=attempt, cwd=str(cwd or tmp_path), stdin_path=stdin_path,
                               stdout_path=str(attempt / "stdout"), stderr_path=str(attempt / "stderr"))
    return rc, attempt


def run(tmp_path, argv, mode, monkeypatch, *, name=None, cwd=None, stdin_path=None):
    """guard(), then its receipt and both streams."""
    rc, attempt = guard(tmp_path, argv, mode, monkeypatch, name=name, cwd=cwd, stdin_path=stdin_path)
    receipt = json.loads((attempt / "exit.json").read_bytes())
    return rc, receipt, (attempt / "stdout").read_bytes(), (attempt / "stderr").read_bytes()


def test_c5_1_the_provider_is_clamped_unless_the_operator_says_inherit(monkeypatch):
    """C-5.1: `utility` by default and for any value but `inherit`; `inherit` opts out."""
    monkeypatch.delenv(qos.PROVIDER_QOS_ENV, raising=False)
    assert qos.provider_qos() == guardian.provider_qos() == "utility"
    for value in ("utility", "", "UTILITY", "background", "default"):
        monkeypatch.setenv(qos.PROVIDER_QOS_ENV, value)
        assert qos.provider_qos() == "utility", value
    monkeypatch.setenv(qos.PROVIDER_QOS_ENV, "inherit")
    assert qos.provider_qos() is None


def test_c5_1_installers_can_still_check_the_guardian(monkeypatch):
    """The 2.1.8 installer asserts these names before it sets ProcessType Interactive."""
    assert guardian.PROVIDER_QOS == "utility" and os.access(guardian.TASKPOLICY, os.X_OK)


def test_c5_1_a_host_without_the_attribute_inherits(monkeypatch):
    monkeypatch.delenv(qos.PROVIDER_QOS_ENV, raising=False)
    monkeypatch.setattr(qos, "_LIBC", None)
    assert qos.provider_qos() is None
    with pytest.raises(OSError):
        qos.spawn(["true"], cwd="/", stdin=0, stdout=1, stderr=2)


def failure_cases(tmp_path):
    (tmp_path / "plain-file").write_text("not a program\n")
    (tmp_path / "a-directory").mkdir(exist_ok=True)
    script = tmp_path / "no-interpreter-line"
    script.write_text("echo ran-under-sh > ran\n")
    script.chmod(0o700)
    locked = tmp_path / "locked"
    locked.mkdir(exist_ok=True)
    locked.chmod(0o600)                      # no search permission: chdir fails EACCES
    return {"missing-absolute": ([str(tmp_path / "missing-executable")], None),
            "missing-bare": (["subfleet-no-such-provider-for-this-test"], None),
            "not-executable": ([str(tmp_path / "plain-file")], None),
            "directory": ([str(tmp_path / "a-directory")], None),
            "no-interpreter-line": ([str(script)], None),
            "missing-cwd": ([sys.executable, "-c", "pass"], tmp_path / "no-such-directory"),
            "cwd-is-a-file": ([sys.executable, "-c", "pass"], tmp_path / "plain-file"),
            "cwd-not-searchable": ([sys.executable, "-c", "pass"], locked)}


@pytest.mark.parametrize("case", ["missing-absolute", "missing-bare", "not-executable", "directory",
                                  "no-interpreter-line", "missing-cwd", "cwd-is-a-file", "cwd-not-searchable"])
def test_c5_2_a_clamped_spawn_failure_is_popens_own(tmp_path, monkeypatch, guardian_identity, case):
    """C-5.2 differential: rc 127, no child, the same spawn_error and the same (empty) streams,
    clamped or not, so an adapter classifies the failure alike. A script with no `#!` line is
    refused (ENOEXEC) both ways: posix_spawn, unlike posix_spawnp, never falls back to /bin/sh."""
    argv, cwd = failure_cases(tmp_path)[case]
    try:
        outcomes = {mode: run(tmp_path, argv, mode, monkeypatch, cwd=cwd) for mode in ("utility", "inherit")}
    finally:
        (tmp_path / "locked").chmod(0o700)
    for mode, (rc, receipt, stdout, stderr) in outcomes.items():
        assert rc == 127 and receipt["rc"] == 127 and receipt["child_pid"] is None, (mode, receipt)
        assert receipt["signal"] is None and stdout == stderr == b"", mode
    assert outcomes["utility"][1]["spawn_error"] == outcomes["inherit"][1]["spawn_error"]
    assert not (tmp_path / "ran").exists()


def test_c5_2_a_provider_exiting_66_after_a_nested_taskpolicy_failure_is_the_providers(
        tmp_path, monkeypatch, guardian_identity):
    """Astra F1 on 885142a5: the first version read taskpolicy's failure line from the
    provider's stderr, so a provider that ran taskpolicy itself was recorded as never spawned."""
    argv = [sys.executable, "-c", "import subprocess,sys; print('provider-started', flush=True); "
            "sys.exit(subprocess.run(['/usr/sbin/taskpolicy','-c','utility','subfleet-no-such-nested-tool']).returncode)"]
    outcomes = {mode: run(tmp_path, argv, mode, monkeypatch) for mode in ("utility", "inherit")}
    for mode, (rc, receipt, stdout, stderr) in outcomes.items():
        assert rc == 66 and receipt["rc"] == 66 and receipt["child_pid"] > 0, (mode, receipt)
        assert "spawn_error" not in receipt and stdout == b"provider-started\n", mode
        assert stderr.startswith(b"taskpolicy: posix_spawn: "), mode


def test_c5_2_a_provider_that_replaces_its_stderr_with_a_fifo_does_not_hold_the_guardian(
        tmp_path, monkeypatch, guardian_identity):
    """Astra F2 on 885142a5: the guardian opened the stderr path after the provider exited and
    blocked on a FIFO put there. Nothing is read from the provider's streams now."""
    for mode in ("utility", "inherit"):
        attempt = tmp_path / mode
        argv = [sys.executable, "-c", f"import os; p={str(attempt / 'stderr')!r}; os.unlink(p); os.mkfifo(p); "
                "raise SystemExit(66)"]
        result = {}
        thread = threading.Thread(target=lambda: result.update(rc=guard(tmp_path, argv, mode, monkeypatch)[0]),
                                  daemon=True)
        thread.start()
        thread.join(30)
        assert not thread.is_alive(), f"{mode}: the guardian is blocked after its provider exited"
        receipt = json.loads((attempt / "exit.json").read_bytes())
        assert result["rc"] == 66
        assert receipt["rc"] == 66 and receipt["child_pid"] > 0 and "spawn_error" not in receipt, mode
        assert stat.S_ISFIFO(os.lstat(attempt / "stderr").st_mode)


def test_c5_1_the_clamped_provider_starts_as_popen_started_it(tmp_path, monkeypatch, guardian_identity):
    """C-5.1 differential: argv, environment, directory, descriptors, stdin, restored signal
    dispositions and exit status are the same clamped or not."""
    monkeypatch.setenv("SUBFLEET_QOS_CANARY", "canary-value")
    prompt = tmp_path / "prompt"
    prompt.write_bytes(b"stdin \x00 bytes")
    probe = """
import json, os, sys
open_fds = []
for fd in range(3, 256):
    try:
        os.fstat(fd)
        open_fds.append(fd)
    except OSError:
        pass
print(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "env": os.environ.get("SUBFLEET_QOS_CANARY"),
                  "stdin": sys.stdin.buffer.read().hex(), "fds": open_fds,
                  "pgid_is_parent": os.getpgrp() == os.getpgid(os.getppid())}))
sys.stderr.write("err-line\\n")
sys.exit(5)
"""
    # CPython ignores SIGPIPE and SIGXFSZ in itself, and Popen gives the child their defaults
    # back (restore_signals). A shell that signals itself dies of each only if it has it back.
    shells = {name: f"kill -{name} $$; echo survived {name}" for name in ("PIPE", "XFSZ")}
    # An inheritable descriptor open in the guardian: Popen's close_fds=True keeps it from
    # the provider, and so must the clamped spawn (POSIX_SPAWN_CLOEXEC_DEFAULT).
    leak_r, leak_w = os.pipe()
    os.set_inheritable(leak_r, True)
    os.set_inheritable(leak_w, True)
    got = {}
    try:
        for mode in ("utility", "inherit"):
            rc, receipt, stdout, stderr = run(tmp_path, [sys.executable, "-c", probe, "a b", "--flag", "-"], mode,
                                              monkeypatch, stdin_path=str(prompt))
            signalled = {name: run(tmp_path, ["/bin/sh", "-c", shell], mode, monkeypatch, name=f"{mode}-{name}")[:3:2]
                         for name, shell in shells.items()}
            got[mode] = (rc, receipt["rc"], json.loads(stdout), stderr, signalled)
            assert receipt["child_pid"] > 0
    finally:
        os.close(leak_r)
        os.close(leak_w)
    assert got["utility"] == got["inherit"]
    rc, _, report, stderr, signalled = got["utility"]
    assert rc == 5 and report["argv"] == ["a b", "--flag", "-"] and report["env"] == "canary-value"
    assert bytes.fromhex(report["stdin"]) == b"stdin \x00 bytes" and report["fds"] == []
    assert report["pgid_is_parent"] and stderr == b"err-line\n"
    assert signalled == {"PIPE": (-signal.SIGPIPE, b""), "XFSZ": (-signal.SIGXFSZ, b"")}


def test_c5_1_a_signalled_provider_reads_as_popen_reported_it(tmp_path, monkeypatch, guardian_identity):
    for mode in ("utility", "inherit"):
        rc, receipt, _, _ = run(tmp_path, [sys.executable, "-c", "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"],
                                mode, monkeypatch)
        assert rc == -signal.SIGTERM and receipt["signal"] == signal.SIGTERM, mode


def test_c5_1_path_search_matches_popen(tmp_path, monkeypatch):
    """_posixsubprocess's search: a later entry that runs wins over an earlier EACCES; when none
    runs, the first error that is not ENOENT/ENOTDIR wins, else the last; a relative PATH entry
    resolves in the child's directory."""
    bin_a, bin_b, work = tmp_path / "a", tmp_path / "b", tmp_path / "work"
    for directory in (bin_a, bin_b, work / "rel"):
        directory.mkdir(parents=True)
    (bin_a / "tool").write_text("not executable")                       # EACCES first
    good = bin_b / "tool"
    good.write_text("#!/bin/sh\nexit 3\n")
    good.chmod(0o755)
    rel = work / "rel" / "reltool"
    rel.write_text("#!/bin/sh\nexit 4\n")
    rel.chmod(0o755)
    for path, name, expect in ((f"{bin_a}:{bin_b}", "tool", 3), (f"{bin_b}:{bin_a}", "tool", 3),
                               ("rel", "reltool", 4),
                               (f"{bin_a}:{tmp_path / 'nowhere'}", "tool", "[Errno 13] Permission denied: 'tool'"),
                               (str(tmp_path / "nowhere"), "tool", "[Errno 2] No such file or directory: 'tool'")):
        monkeypatch.setenv("PATH", path)
        outcomes = []
        for spawner in ("popen", "qos"):
            try:
                if spawner == "popen":
                    child = subprocess.Popen([name], cwd=work, stdin=subprocess.DEVNULL)
                else:
                    child = qos.spawn([name], cwd=str(work), stdin=os.open(os.devnull, os.O_RDONLY), stdout=1, stderr=2)
                outcomes.append(child.wait())
            except OSError as exc:
                outcomes.append(str(exc))
        assert outcomes == [expect, expect], (path, outcomes)


def test_the_process_handle_polls_waits_and_signals_like_popen(tmp_path, monkeypatch):
    child = qos.spawn([sys.executable, "-c", "import time; time.sleep(30)"], cwd=str(tmp_path),
                      stdin=os.open(os.devnull, os.O_RDONLY), stdout=1, stderr=2)
    assert child.poll() is None and child.returncode is None
    waiter = threading.Thread(target=child.wait)
    waiter.start()
    time.sleep(.2)
    assert child.poll() is None               # a poll during a wait neither blocks nor steals the status
    child.send_signal(signal.SIGKILL)
    waiter.join(10)
    assert child.returncode == -signal.SIGKILL == child.poll() == child.wait()
    sent = []
    monkeypatch.setattr(qos.os, "kill", lambda *args: sent.append(args))
    child.send_signal(signal.SIGKILL)         # after exit: nothing (its pid may be another process's now)
    assert sent == []


@settings(max_examples=60, deadline=None)
@given(argv=st.lists(st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
                             max_size=12), min_size=0, max_size=5))
def test_argv_reaches_the_clamped_provider_unchanged(tmp_path_factory, argv):
    """Property: whatever the arguments, the provider receives exactly them, as Popen passes them."""
    work = tmp_path_factory.mktemp("argv")
    out = work / "out"
    with open(out, "wb") as stream:
        child = qos.spawn([sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *argv],
                          cwd=str(work), stdin=os.open(os.devnull, os.O_RDONLY), stdout=stream.fileno(), stderr=2)
        assert child.wait() == 0
    assert json.loads(out.read_text()) == argv


def test_repository_code_runs_clamped_unless_inherit(monkeypatch):
    monkeypatch.delenv(qos.PROVIDER_QOS_ENV, raising=False)
    argv = ["git", "-C", "/w", "worktree", "add"]
    assert qos.repository_argv(argv) == [qos.TASKPOLICY, "-c", "utility", *argv]
    assert qos.unclamped(qos.repository_argv(argv)) == argv == qos.unclamped(argv)
    monkeypatch.setenv(qos.PROVIDER_QOS_ENV, "inherit")
    assert qos.repository_argv(argv) == argv


def test_salvage_clamps_the_git_commands_that_run_repository_code(monkeypatch):
    """F5 of the review: `add` runs clean filters, `update-ref` the reference-transaction hook;
    the reads a submission waits on keep the daemon's QoS."""
    from subfleet import salvage
    monkeypatch.delenv(qos.PROVIDER_QOS_ENV, raising=False)
    clamped = lambda *args: salvage._argv("/w", args)[0] == qos.TASKPOLICY
    assert clamped("add", "-A") and clamped("update-ref", "r", "c", "0" * 40)
    assert clamped("update-index", "--assume-unchanged", "-z", "--stdin")
    assert not clamped("rev-parse", "--verify", "HEAD") and not clamped("symbolic-ref", "--quiet", "HEAD")
    assert not clamped("-c", "user.name=subfleet", "-c", "user.email=subfleet@localhost", "commit-tree", "t")
    assert not clamped("read-tree", "x") and not clamped("write-tree")
