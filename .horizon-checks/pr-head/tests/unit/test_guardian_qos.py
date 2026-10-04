"""C-5.1: the guardian starts each provider under a `utility` QoS clamp (2026-09-27).

The daemon runs at the default QoS (launchd `ProcessType` `Interactive`); agent work must
not. These tests pin how the guardian chooses the clamp, that a clamped spawn failure reads
exactly as Popen's own (C-5.2), and where the two spawns knowingly differ. The priorities
the clamp gives real processes are checked in tests/process/test_guardian_qos_process.py and,
through a real daemon, in tests/fake/test_provider_qos.py.
"""

from __future__ import annotations

import errno
import json
import os
import sys

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import guardian

pytestmark = pytest.mark.skipif(not os.access(guardian.TASKPOLICY, os.X_OK),
                                reason="C-5.1's clamp is taskpolicy(8), which ships with macOS")


@pytest.fixture
def guardian_identity(monkeypatch):
    monkeypatch.setattr(guardian.os, "setsid", lambda: None)
    monkeypatch.setattr(guardian.signal, "signal", lambda *args: None)
    monkeypatch.setattr(guardian, "boot_id", lambda: "test-boot")
    monkeypatch.setattr(guardian, "proc_start", lambda pid: "test-start")
    previous = os.umask(0o077)
    yield
    os.umask(previous)


def test_c5_1_the_provider_is_clamped_to_utility_unless_the_operator_says_inherit(monkeypatch):
    """C-5.1: `utility` by default and for any value but `inherit`; `inherit` opts out."""
    monkeypatch.delenv(guardian.PROVIDER_QOS_ENV, raising=False)
    assert guardian.provider_qos() == "utility"
    for value in ("utility", "", "UTILITY", "background", "default"):
        monkeypatch.setenv(guardian.PROVIDER_QOS_ENV, value)
        assert guardian.provider_qos() == "utility", value
    monkeypatch.setenv(guardian.PROVIDER_QOS_ENV, "inherit")
    assert guardian.provider_qos() is None


def test_c5_1_a_host_without_taskpolicy_inherits(monkeypatch):
    """C-5.1: no taskpolicy(8), no clamp: the provider starts as it did before, unwrapped."""
    monkeypatch.delenv(guardian.PROVIDER_QOS_ENV, raising=False)
    real = os.access
    monkeypatch.setattr(guardian.os, "access",
                        lambda path, mode: False if path == guardian.TASKPOLICY else real(path, mode))
    assert guardian.provider_qos() is None
    assert guardian.provider_argv(["codex", "exec"], None) == ["codex", "exec"]


def test_c5_1_the_clamp_wraps_argv_without_touching_it():
    """C-5.1: taskpolicy execs the provider in its own place with argv exactly as given."""
    argv = ["codex", "exec", "--json", "-c", "x=y", "--", "-"]
    assert guardian.provider_argv(argv, "utility") == [guardian.TASKPOLICY, "-c", "utility", *argv]
    assert guardian.provider_argv(argv, None) == argv


@pytest.mark.parametrize("case", ["missing-absolute", "missing-bare", "not-executable", "directory"])
def test_c5_2_a_clamped_spawn_failure_reads_as_popens_own(tmp_path, monkeypatch, guardian_identity, case):
    """C-5.2 differential: rc 127, no child, the same spawn_error and the same (empty) streams,
    clamped or not, so an adapter classifies the failure alike."""
    target = {"missing-absolute": str(tmp_path / "missing-executable"),
              "missing-bare": "subfleet-no-such-provider-for-this-test",
              "not-executable": str(tmp_path / "plain-file"),
              "directory": str(tmp_path / "a-directory")}[case]
    (tmp_path / "plain-file").write_text("not a program\n")
    (tmp_path / "a-directory").mkdir()
    receipts = {}
    for mode in ("utility", "inherit"):
        attempt = tmp_path / mode
        attempt.mkdir()
        monkeypatch.setenv(guardian.PROVIDER_QOS_ENV, mode)
        rc = guardian.run_guardian([target, "--flag"], attempt_dir=attempt, cwd=str(tmp_path), stdin_path=None,
                                   stdout_path=str(attempt / "stdout"), stderr_path=str(attempt / "stderr"))
        receipt = json.loads((attempt / "exit.json").read_bytes())
        assert rc == 127 and receipt["rc"] == 127, (mode, receipt)
        assert receipt["child_pid"] is None and receipt["signal"] is None
        # What an adapter classifies from: the receipt and both streams.
        receipts[mode] = (receipt["spawn_error"], (attempt / "stdout").read_bytes(),
                          (attempt / "stderr").read_bytes())
    assert receipts["utility"] == receipts["inherit"]
    assert repr(target) in receipts["utility"][0]
    assert receipts["utility"][1:] == (b"", b"")


def test_c5_2_a_provider_that_exits_66_is_not_a_spawn_failure(tmp_path, monkeypatch, guardian_identity):
    """C-5.2: only taskpolicy's own line turns exit 66 into a spawn failure."""
    monkeypatch.delenv(guardian.PROVIDER_QOS_ENV, raising=False)
    command = [sys.executable, "-c", "import sys; print('taskpolicy said nothing', file=sys.stderr); sys.exit(66)"]
    rc = guardian.run_guardian(command, attempt_dir=tmp_path, cwd=str(tmp_path), stdin_path=None,
                               stdout_path=str(tmp_path / "stdout"), stderr_path=str(tmp_path / "stderr"))
    receipt = json.loads((tmp_path / "exit.json").read_bytes())
    assert rc == 66 and receipt["rc"] == 66
    assert "spawn_error" not in receipt and receipt["child_pid"] > 0


def test_c5_1_intended_divergence_a_script_without_an_interpreter_line_runs_under_sh(
        tmp_path, monkeypatch, guardian_identity):
    """C-5.1, labelled intended: posix_spawnp runs an executable text file with no `#!` line
    under /bin/sh, as execvp(3) does, where Popen refuses it (ENOEXEC). No provider is one:
    Claude Code and Codex are native binaries or `#!` launchers."""
    script = tmp_path / "no-interpreter-line"
    script.write_text("echo ran-under-sh\n")
    script.chmod(0o700)
    outcomes = {}
    for mode in ("utility", "inherit"):
        attempt = tmp_path / mode
        attempt.mkdir()
        monkeypatch.setenv(guardian.PROVIDER_QOS_ENV, mode)
        rc = guardian.run_guardian([str(script)], attempt_dir=attempt, cwd=str(tmp_path), stdin_path=None,
                                   stdout_path=str(attempt / "stdout"), stderr_path=str(attempt / "stderr"))
        outcomes[mode] = (rc, json.loads((attempt / "exit.json").read_bytes()).get("spawn_error"),
                          (attempt / "stdout").read_text())
    assert outcomes["inherit"][0] == 127 and "Exec format error" in outcomes["inherit"][1]
    assert outcomes["utility"] == (0, None, "ran-under-sh\n")


TEXTS = sorted({os.strerror(number) for number in errno.errorcode})


@settings(max_examples=300, deadline=None)
@given(number=st.sampled_from(sorted(errno.errorcode)),
       name=st.text(st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=40),
       after=st.binary(max_size=60))
def test_c5_2_every_taskpolicy_reason_becomes_popens_words(tmp_path_factory, number, name, after):
    """C-5.2 property: for any errno taskpolicy reports, spawn_error is str(OSError(errno, text, argv0)),
    read from taskpolicy's line alone, whatever follows it."""
    stderr = tmp_path_factory.mktemp("err") / "stderr"
    text = os.strerror(number)
    stderr.write_bytes(guardian.CLAMP_SPAWN_FAILED + text.encode() + b"\n" + after)
    got = guardian.clamp_spawn_error(guardian.CLAMP_SPAWN_FAILED_RC, str(stderr), name)
    mapped = guardian._ERRNO_BY_TEXT[text]
    assert os.strerror(mapped) == text
    assert got == str(OSError(mapped, text, name))


@settings(max_examples=300, deadline=None)
@given(rc=st.integers(-64, 255), head=st.binary(max_size=80))
def test_c5_2_nothing_else_is_read_as_a_spawn_failure(tmp_path_factory, rc, head):
    """C-5.2 property: any other exit status, or any stderr not led by taskpolicy's line, is the provider's."""
    stderr = tmp_path_factory.mktemp("err") / "stderr"
    stderr.write_bytes(head)
    got = guardian.clamp_spawn_error(rc, str(stderr), "codex")
    if rc != guardian.CLAMP_SPAWN_FAILED_RC or not head.split(b"\n", 1)[0].startswith(guardian.CLAMP_SPAWN_FAILED):
        assert got is None
    else:
        assert got is not None and got.endswith(repr("codex"))


def test_c5_2_an_unreadable_stderr_is_no_spawn_failure(tmp_path):
    assert guardian.clamp_spawn_error(guardian.CLAMP_SPAWN_FAILED_RC, str(tmp_path / "absent"), "codex") is None


def test_c5_2_an_unknown_reason_keeps_taskpolicys_words(tmp_path):
    stderr = tmp_path / "stderr"
    stderr.write_bytes(guardian.CLAMP_SPAWN_FAILED + b"Something new\n")
    assert guardian.clamp_spawn_error(66, str(stderr), "codex") == "Something new: 'codex'"


def test_c5_2_taskpolicy_reports_a_spawn_failure_as_the_guardian_expects(tmp_path):
    """C-5.2: pins the host's taskpolicy(8) failure shape the translation relies on."""
    import subprocess
    done = subprocess.run([guardian.TASKPOLICY, "-c", "utility", str(tmp_path / "missing")],
                          capture_output=True)
    assert done.returncode == guardian.CLAMP_SPAWN_FAILED_RC
    assert done.stderr == guardian.CLAMP_SPAWN_FAILED + b"No such file or directory\n"
