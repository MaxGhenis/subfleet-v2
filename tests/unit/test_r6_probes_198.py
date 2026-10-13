"""Review r6 probes for #198 (not part of the PR).

Each probe asserts the guard's promise: no test reaches the native `security`.
The native binary is stood in for by a recording executable named `security`,
first on PATH; `/usr/bin/security` appears only as a string. The r6 audit hook
(r6_audit_plugin, imported first) refuses any exec that resolves to the system
binary, so a bypass can only ever reach the stand-in.
"""

import functools
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest import mock

import r6_audit_plugin  # noqa: F401  (installs the safety hook before any probe)
import pytest

import subfleet
from subfleet import credentials
from subfleet.adapters import claude, registry
from subfleet.adapters.base import AdapterError
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.contracts import Credential
from subfleet.daemon import enrollment_runner

REAL_RUN = subprocess.run
RESULTS = os.environ.get("R6_RESULTS")
BLOB = '{"claudeAiOauth":{"accessToken":"standin-token","subscriptionType":"standin-plan"}}'


def report(name, **fields):
    if RESULTS:
        with open(RESULTS, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"probe": name, "tree": subfleet.__file__, **fields}) + "\n")


class StandIn:
    def __init__(self, directory):
        directory.mkdir()
        self.log = directory / "invocations.log"
        self.path = directory / "security"
        self.path.write_text("#!/bin/sh\n"
                             f"printf '%s\\n' \"$*\" >> '{self.log}'\n"
                             f"printf '%s' '{BLOB}'\n", encoding="utf-8")
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)

    def count(self):
        try:
            return len(self.log.read_text(encoding="utf-8").splitlines())
        except FileNotFoundError:
            return 0


@pytest.fixture
def standin(tmp_path, monkeypatch):
    found = StandIn(tmp_path / "standin-bin")
    monkeypatch.setenv("PATH", f"{found.path.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("CLAUDE_LANE_AGENT_SECRET", raising=False)
    # Safety: bare `security` must resolve to the stand-in before any probe runs.
    assert shutil.which("security") == str(found.path)
    return found


def run_reader(name, call, standin):
    before = standin.count()
    error = None
    try:
        value = call()
    except (AdapterError, OSError, subprocess.SubprocessError) as exc:
        value, error = None, f"{type(exc).__name__}: {exc}"
    ran = standin.count() - before
    return {"ran": ran, "error": error, "value": repr(value)[:60]}


def readers(tmp_path):
    return {
        "credential": lambda: credentials.resolve_credential(
            Credential("claude", "fixture-ref", "keychain-token")),
        "blob": lambda: claude._keychain_blob("fixture-ref"),
        "home-login": lambda: claude.home_login(tmp_path / "empty-home"),
        "token": lambda: ClaudeAdapter()._resolve_keychain_token("fixture-ref"),
        "plan": lambda: ClaudeAdapter()._plan_from_keychain("fixture-ref"),
    }


def wrappers(seen):
    def recording(argv, **kwargs):
        seen.append(list(argv))
        return REAL_RUN(argv, **kwargs)

    def selective(cmd, *args, **kwargs):          # the suite's own idiom (e.g. test_salvage_finalization)
        if "symbolic-ref" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return REAL_RUN(cmd, *args, **kwargs)

    def popen_based(argv, **kwargs):
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
            out, err = proc.communicate(timeout=kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    return {
        "recording-passthrough": recording,
        "selective-passthrough": selective,
        "lambda": lambda *a, **k: REAL_RUN(*a, **k),
        "partial": functools.partial(REAL_RUN),
        "partial-env": functools.partial(REAL_RUN, env=dict(os.environ)),
        "mock-wraps": mock.Mock(wraps=REAL_RUN),
        "magicmock-wraps": mock.MagicMock(wraps=REAL_RUN),
        "popen-based": popen_based,
    }


WRAPPERS = list(wrappers([]))
READERS = ["credential", "blob", "home-login", "token", "plan"]


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_p1a_patched_global_subprocess_run_delegating(standin, tmp_path, monkeypatch, wrapper, reader):
    """A monkeypatched subprocess.run that delegates to the real one (runner=None callers)."""
    seen = []
    w = wrappers(seen)[wrapper]
    if wrapper == "partial-env":
        w = functools.partial(REAL_RUN, env=dict(os.environ))   # rebuilt after PATH points at the stand-in
    monkeypatch.setattr(subprocess, "run", w)
    out = run_reader(reader, readers(tmp_path)[reader], standin)
    report("p1a", wrapper=wrapper, reader=reader, **out)
    assert out["ran"] == 0, out


def test_p1a_mock_patch_wraps(standin, tmp_path):
    """mock.patch.object(subprocess, 'run', wraps=subprocess.run)."""
    with mock.patch.object(subprocess, "run", wraps=REAL_RUN) as patched:
        out = run_reader("credential", readers(tmp_path)["credential"], standin)
    report("p1a", wrapper="mock.patch.object(wraps=)", reader="credential", **out, mock_calls=patched.call_count)
    assert out["ran"] == 0, out


@pytest.mark.parametrize("wrapper", ["partial", "mock-wraps", "lambda"])
def test_p1b_explicit_delegating_runner(standin, wrapper):
    """runner= given explicitly (round 4 also trusted explicit runners)."""
    w = wrappers([])[wrapper]
    outs = {
        "credential": run_reader("credential", lambda: credentials.resolve_credential(
            Credential("claude", "fixture-ref", "keychain-token"), runner=w), standin),
        "blob": run_reader("blob", lambda: claude._keychain_blob("fixture-ref", runner=w), standin),
        "token": run_reader("token", lambda: ClaudeAdapter(runner=w)._resolve_keychain_token("fixture-ref"), standin),
    }
    report("p1b", wrapper=wrapper, outs=outs)
    assert all(o["ran"] == 0 for o in outs.values()), outs


def test_p1c_fake_bound_then_undone_mid_call(standin, monkeypatch):
    """subprocess.run is a non-delegating fake when `execute` binds; undone before the call."""
    fake_calls = []
    inner = pytest.MonkeyPatch()
    inner.setattr(subprocess, "run", lambda argv, **k: fake_calls.append(argv) or
                  subprocess.CompletedProcess(argv, 0, "fake-token", ""))
    original = credentials.keychain_command

    def undo_then_build(*args, **kwargs):
        inner.undo()
        assert subprocess.run is REAL_RUN
        return original(*args, **kwargs)

    monkeypatch.setattr(credentials, "keychain_command", undo_then_build)
    outs = {"credential": run_reader("credential", lambda: credentials.resolve_credential(
        Credential("claude", "fixture-ref", "keychain-token")), standin)}
    inner.setattr(subprocess, "run", lambda argv, **k: fake_calls.append(argv) or
                  subprocess.CompletedProcess(argv, 0, "fake-token", ""))
    outs["blob"] = run_reader("blob", lambda: claude._keychain_blob("fixture-ref"), standin)
    inner.undo()
    report("p1c", outs=outs, fake_calls=len(fake_calls))
    assert all(o["ran"] == 0 for o in outs.values()) and len(fake_calls) == 2, (outs, fake_calls)


def test_p1d_native_bound_then_patched_mid_call(standin, monkeypatch):
    """`execute` binds the native run; a stub is applied before the call."""
    fake_calls = []
    original = credentials.keychain_command
    inner = pytest.MonkeyPatch()

    def patch_then_build(*args, **kwargs):
        inner.setattr(subprocess, "run", lambda argv, **k: fake_calls.append(argv) or
                      subprocess.CompletedProcess(argv, 0, "fake-token", ""))
        return original(*args, **kwargs)

    monkeypatch.setattr(credentials, "keychain_command", patch_then_build)
    try:
        out = run_reader("credential", lambda: credentials.resolve_credential(
            Credential("claude", "fixture-ref", "keychain-token")), standin)
    finally:
        inner.undo()
    report("p1d", **out, fake_calls=len(fake_calls))
    assert out["ran"] == 0 and "refusing personal keychain" in (out["error"] or ""), out


def test_p1e_adapter_built_under_delegating_patch_survives_undo(standin):
    """The adapter captures a delegating subprocess.run; the fixture is undone; a later read runs."""
    inner = pytest.MonkeyPatch()
    inner.setattr(subprocess, "run", lambda *a, **k: REAL_RUN(*a, **k))
    adapter = ClaudeAdapter()
    inner.undo()
    assert subprocess.run is REAL_RUN
    out = run_reader("token", lambda: adapter._resolve_keychain_token("fixture-ref"), standin)
    report("p1e", **out)
    assert out["ran"] == 0, out


def test_p1f_production_enrollment_runner_wraps_native(standin):
    """daemon.enrollment_runner wraps a default adapter's native runner (no test patch at all)."""
    from tests.conftest import profile_opener
    turned = []
    adapter = ClaudeAdapter(profile_opener=profile_opener())
    assert adapter._runner is credentials._NATIVE_RUNNER
    adapter._runner = enrollment_runner(adapter._runner, lambda argv, **k: turned.append(argv) or
                                        subprocess.CompletedProcess(argv, 0, "", ""))
    outs = {"token": run_reader("token", lambda: adapter._resolve_keychain_token("claude-quota-fixture"), standin),
            "plan": run_reader("plan", lambda: adapter._plan_from_keychain("claude-quota-fixture"), standin)}
    report("p1f", outs=outs, turned=len(turned))
    assert all(o["ran"] == 0 for o in outs.values()), outs


def test_p1g_daemon_reenrolls_keychain_lane_with_default_adapter(standin, tmp_path, monkeypatch):
    """Through Daemon._enroll_lane_locked: re-enrolling a keychain-token lane with the default adapter."""
    from tests.unit.test_claude_adapter import NOW, _enroll_stream, _Runner
    from tests.conftest import profile_opener
    from tests.unit.test_enrollment_runner import _info
    from tests.fake.conftest import Harness
    from subfleet import daemon as daemon_module, protocol
    from subfleet.daemon import Daemon
    from subfleet.procs import Containment

    root = tmp_path / "state"
    root.mkdir()
    Harness(root)
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "unit-test-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "unit-test-start")
    monkeypatch.setattr(daemon_module.procs, "same_process", lambda *args: False)
    monkeypatch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment())
    ref = "claude-quota-max@axiom.org"
    stub = _Runner(stdout=_enroll_stream(info=_info()))
    monkeypatch.setitem(registry._factories, "claude", lambda: ClaudeAdapter(
        runner=stub, now=lambda: NOW, projects_dir=tmp_path, profile_opener=profile_opener()))
    daemon = Daemon(root)
    try:
        first = daemon.dispatch("lanes", {"action": "enroll", "credential": ref})["enrolled"]
        daemon.store.update_lane(first["lane_id"], enabled=0)
        # The default adapter: its runner is the native subprocess.run.
        monkeypatch.setitem(registry._factories, "claude", lambda: ClaudeAdapter(
            now=lambda: NOW, projects_dir=tmp_path, profile_opener=profile_opener()))

        def turn(lane_id, holder, argv, *, cwd, env, timeout, **_):
            return subprocess.CompletedProcess(argv, 0, _enroll_stream(info=_info()), "")
        monkeypatch.setattr(daemon, "_enrollment_turn", turn)
        before = standin.count()
        error = None
        try:
            value = daemon.dispatch("lanes", {"action": "enroll", "credential": ref})["enrolled"]["lane_id"]
        except protocol.ProtocolError as exc:
            value, error = None, f"ProtocolError: {exc}"
        out = {"ran": standin.count() - before, "error": error, "value": value}
    finally:
        daemon.close()
    report("p1g", **out)
    assert out["ran"] == 0, out


@pytest.mark.parametrize("spelling", ["//usr/bin/security", "/usr/bin/./security",
                                      "/usr/bin/../bin/security", "/usr/../usr/bin/security"])
@pytest.mark.parametrize("runner_kind", ["none", "native"])
def test_p1h_native_binary_spellings_by_string_only(spelling, runner_kind):
    """No execution: does keychain_command build argv for another spelling of the system binary?"""
    runner = None if runner_kind == "none" else credentials._NATIVE_RUNNER
    try:
        argv = credentials.keychain_command("fixture-ref", spelling, runner=runner)
        outcome = f"built {argv[0]}"
    except AdapterError as exc:
        outcome = f"refused: {exc}"
    report("p1h", spelling=spelling, runner=runner_kind, outcome=outcome,
           same_target=os.path.normpath(spelling).lstrip("/") == "usr/bin/security")
    assert outcome.startswith("refused"), outcome


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_p1k_proposed_exec_layer_rule_blocks_every_delegate(standin, tmp_path, monkeypatch, wrapper):
    """Proposed rule, demonstrated on the stand-in: refuse by what the call would execute."""
    monkeypatch.setattr(r6_audit_plugin, "EXTRA", {os.path.realpath(standin.path)})
    seen = []
    w = wrappers(seen)[wrapper]
    if wrapper == "partial-env":
        w = functools.partial(REAL_RUN, env=dict(os.environ))
    monkeypatch.setattr(subprocess, "run", w)
    outs = {r: run_reader(r, readers(tmp_path)[r], standin) for r in READERS}
    report("p1k", wrapper=wrapper, outs=outs)
    assert all(o["ran"] == 0 for o in outs.values()), outs


def test_p1k_proposed_rule_keeps_the_ci_stub_shape(standin, tmp_path, monkeypatch):
    """test_policy_support's stub never execs, so an exec-layer rule leaves it passing."""
    monkeypatch.setattr(r6_audit_plugin, "EXTRA", {os.path.realpath(standin.path)})
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "private-token\n", "")
    monkeypatch.setattr("subfleet.credentials.subprocess.run", run)
    result = credentials.resolve_credential(Credential("claude", "test-service", "keychain-token"))
    report("p1k-ci", result=sorted(result), calls=calls)
    assert result == {"CLAUDE_CODE_OAUTH_TOKEN": "private-token"} and standin.count() == 0
