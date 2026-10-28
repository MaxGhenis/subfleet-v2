"""C-5.1, D-QOS-BOOKED1: the daemon's per-launch `--provider-qos`, as the guardian takes it.

The guardian clamps every provider to `utility` (tests/unit/test_guardian_qos.py). Since
D-QOS-BOOKED1 the daemon may ask, for one launch, that the provider start at the guardian's
own QoS instead (`--provider-qos inherit`): it asks only for a priority caller's job that
requested `run --qos default`. The operator's `SUBFLEET_PROVIDER_QOS=inherit` still unclamps
every launch; neither can clamp what the other lets inherit. `start.json` records the QoS
the provider was given, never what was asked.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from subfleet import guardian

pytestmark = pytest.mark.skipif(not os.access(guardian.TASKPOLICY, os.X_OK),
                                reason="C-5.1's clamp is taskpolicy(8), which ships with macOS")


@pytest.fixture
def guardian_identity(monkeypatch):
    monkeypatch.setattr(guardian.os, "setsid", lambda: None)
    monkeypatch.setattr(guardian.signal, "signal", lambda *args: None)
    monkeypatch.setattr(guardian, "boot_id", lambda: "test-boot")
    monkeypatch.setattr(guardian, "proc_start", lambda pid: "test-start")
    monkeypatch.setattr(guardian, "linger", lambda: None)
    previous = os.umask(0o077)
    yield
    os.umask(previous)


@pytest.mark.parametrize("request_, env, given", [
    ("utility", None, "utility"), ("inherit", None, None),
    ("utility", "inherit", None), ("inherit", "inherit", None),
    # Only `inherit` opts out, from either side; any other operator value clamps.
    ("utility", "utility", "utility"), ("inherit", "utility", None),
    ("utility", "", "utility"), ("utility", "INHERIT", "utility"),
])
def test_d_qos_booked1_either_side_unclamps_and_neither_clamps_what_the_other_lets_inherit(
        monkeypatch, request_, env, given):
    if env is None:
        monkeypatch.delenv(guardian.PROVIDER_QOS_ENV, raising=False)
    else:
        monkeypatch.setenv(guardian.PROVIDER_QOS_ENV, env)
    assert guardian.provider_qos(request_) == given


def test_d_qos_booked1_no_request_is_utility(monkeypatch):
    """The guardian's default stays `utility`: a launch that asks nothing is clamped."""
    monkeypatch.delenv(guardian.PROVIDER_QOS_ENV, raising=False)
    assert guardian.provider_qos() == "utility"
    assert guardian.PROVIDER_QOS_CHOICES == ("utility", "inherit")


def parsed(monkeypatch, tmp_path, *flags):
    """`main`'s parse of a guardian argv, with `run_guardian` replaced by a recorder."""
    seen = {}

    def record(command, **options):
        seen.update(options, command=command)
        return 0
    monkeypatch.setattr(guardian, "run_guardian", record)
    rc = guardian.main(["--attempt-dir", str(tmp_path), "--cwd", str(tmp_path),
                        "--stdout-path", str(tmp_path / "stdout"), "--stderr-path", str(tmp_path / "stderr"),
                        *flags, "--", "provider", "--its-flag"])
    assert rc == 0
    return seen


def test_d_qos_booked1_main_takes_the_launch_request(monkeypatch, tmp_path):
    assert parsed(monkeypatch, tmp_path)["qos_request"] == "utility"
    assert parsed(monkeypatch, tmp_path, "--provider-qos", "inherit")["qos_request"] == "inherit"
    seen = parsed(monkeypatch, tmp_path, "--provider-qos", "utility")
    assert seen["qos_request"] == "utility" and seen["command"] == ["provider", "--its-flag"]


@pytest.mark.parametrize("value", ["default", "background", "INHERIT", ""])
def test_d_qos_booked1_main_refuses_any_other_request(monkeypatch, tmp_path, value, capsys):
    with pytest.raises(SystemExit) as exc:
        parsed(monkeypatch, tmp_path, "--provider-qos", value)
    assert exc.value.code == 2
    assert "--provider-qos" in capsys.readouterr().err


@pytest.mark.parametrize("request_, env, given", [
    ("utility", None, "utility"), ("inherit", None, "inherit"),
    # The operator's inherit: asked utility, given inherit. A start.json that recorded
    # the request would say utility here.
    ("utility", "inherit", "inherit"), ("inherit", "inherit", "inherit"),
])
def test_d_qos_booked1_start_json_records_the_qos_the_provider_was_given(
        tmp_path, monkeypatch, guardian_identity, request_, env, given):
    if env is None:
        monkeypatch.delenv(guardian.PROVIDER_QOS_ENV, raising=False)
    else:
        monkeypatch.setenv(guardian.PROVIDER_QOS_ENV, env)
    spawned = []
    real = guardian.subprocess.Popen

    def popen(command, **options):
        spawned.append(list(command))
        return real(command, **options)
    monkeypatch.setattr(guardian.subprocess, "Popen", popen)
    argv = [sys.executable, "-c", "pass"]
    rc = guardian.run_guardian(argv, attempt_dir=tmp_path, cwd=str(tmp_path), stdin_path=None,
                               stdout_path=str(tmp_path / "stdout"), stderr_path=str(tmp_path / "stderr"),
                               qos_request=request_)
    assert rc == 0
    start = json.loads((tmp_path / "start.json").read_text())
    assert start["provider_qos"] == given
    # What start.json says is what the provider was spawned under.
    assert spawned == [[guardian.TASKPOLICY, "-c", "utility", *argv] if given == "utility" else argv]


def test_d_qos_booked1_a_host_without_taskpolicy_records_inherit(tmp_path, monkeypatch, guardian_identity):
    """No taskpolicy(8), no clamp (C-5.1): the provider was given the guardian's QoS."""
    monkeypatch.delenv(guardian.PROVIDER_QOS_ENV, raising=False)
    real = os.access
    monkeypatch.setattr(guardian.os, "access",
                        lambda path, mode: False if path == guardian.TASKPOLICY else real(path, mode))
    rc = guardian.run_guardian([sys.executable, "-c", "pass"], attempt_dir=tmp_path, cwd=str(tmp_path),
                               stdin_path=None, stdout_path=str(tmp_path / "stdout"),
                               stderr_path=str(tmp_path / "stderr"))
    assert rc == 0
    assert json.loads((tmp_path / "start.json").read_text())["provider_qos"] == "inherit"
