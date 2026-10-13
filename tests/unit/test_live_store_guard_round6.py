"""D-ST1 round 6: refuse by the binary a launch would start, not by its runner.

Review r6 ran a recording stand-in named `security`, first on PATH, through
runners that hand calls on to the real `subprocess.run`. Every one passed the
runner-identity guard, and the production `enrollment_runner` passed it with no
test patch at all. These regressions register that stand-in with the exec
guard, so each refusal shown is the one the real `/usr/bin/security` gets.
Spellings of the real path are checked by name and by synthetic audit events,
which start no program. Behind both, `tests.keychain_standin` installs a safety
net that refuses anything still resolving to the real binary.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from subfleet import credentials, home_paths
from subfleet.adapters import claude, registry
from subfleet.adapters.base import AdapterError
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.contracts import Credential
from subfleet.daemon import enrollment_runner
from subfleet.home_paths import PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID, SAFE_HOME_PATHS
from tests import conftest
from tests.keychain_standin import (  # noqa: F401 - the fixture
    REAL_BINARY, SAFETY_HITS, STANDIN_BLOB, StandIn, standin, treat_as_keychain_binary,
)

REPO = Path(__file__).resolve().parents[2]
REAL_RUN = subprocess.run
REFUSAL = "refusing personal keychain read under pytest"
MARKERS = (PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID, "PYTEST_CURRENT_TEST")


def run_reader(call, found):
    before = found.count()
    error = value = None
    try:
        value = call()
    except (AdapterError, OSError, subprocess.SubprocessError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    return {"ran": found.count() - before, "error": error, "value": value}


def readers(tmp_path):
    return {
        "credential": lambda: credentials.resolve_credential(
            Credential("claude", "fixture-ref", "keychain-token")),
        "blob": lambda: claude._keychain_blob("fixture-ref"),
        "home-login": lambda: claude.home_login(tmp_path / "empty-home"),
        "token": lambda: ClaudeAdapter()._resolve_keychain_token("fixture-ref"),
        "plan": lambda: ClaudeAdapter()._plan_from_keychain("fixture-ref"),
    }


def assert_refused(reader, out):
    """Nothing ran, and each reader reports the refusal as it reports the early one."""
    assert out["ran"] == 0, out
    if reader in ("credential", "token"):
        assert out["error"] is not None and REFUSAL in out["error"], out
    else:
        assert out["value"] is None and out["error"] is None, out


def wrappers():
    def recording(argv, **kwargs):
        return REAL_RUN(argv, **kwargs)

    def selective(cmd, *args, **kwargs):           # the suite's own pass-through idiom
        if "symbolic-ref" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return REAL_RUN(cmd, *args, **kwargs)

    def popen_based(argv, **kwargs):
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) as proc:
            out, err = proc.communicate(timeout=kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    return {
        "recording-passthrough": recording,
        "selective-passthrough": selective,
        "lambda": lambda *a, **k: REAL_RUN(*a, **k),
        "partial": functools.partial(REAL_RUN),
        # Built after the fixture puts the stand-in first on PATH.
        "partial-env": functools.partial(REAL_RUN, env=dict(os.environ)),
        "mock-wraps": mock.Mock(wraps=REAL_RUN),
        "magicmock-wraps": mock.MagicMock(wraps=REAL_RUN),
        "popen-based": popen_based,
    }


WRAPPERS = ["recording-passthrough", "selective-passthrough", "lambda", "partial",
            "partial-env", "mock-wraps", "magicmock-wraps", "popen-based"]
READERS = ["credential", "blob", "home-login", "token", "plan"]


# --- p1a: a delegating subprocess.run patch, for runner=None callers ----------

@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_delegating_subprocess_patch_cannot_reach_keychain(standin, tmp_path, monkeypatch,
                                                          wrapper, reader):
    treat_as_keychain_binary(monkeypatch, standin)
    monkeypatch.setattr(subprocess, "run", wrappers()[wrapper])
    assert_refused(reader, run_reader(readers(tmp_path)[reader], standin))


def test_mock_patch_wraps_cannot_reach_keychain(standin, tmp_path, monkeypatch):
    treat_as_keychain_binary(monkeypatch, standin)
    with mock.patch.object(subprocess, "run", wraps=REAL_RUN) as patched:
        out = run_reader(readers(tmp_path)["credential"], standin)
    assert patched.call_count == 1
    assert_refused("credential", out)


# --- p1b: an explicit delegating runner (round 4 trusted these by design) ------

@pytest.mark.parametrize("wrapper", ["partial", "mock-wraps", "lambda"])
def test_explicit_delegating_runner_cannot_reach_keychain(standin, monkeypatch, wrapper):
    treat_as_keychain_binary(monkeypatch, standin)
    run = wrappers()[wrapper]
    assert_refused("credential", run_reader(lambda: credentials.resolve_credential(
        Credential("claude", "fixture-ref", "keychain-token"), runner=run), standin))
    assert_refused("blob", run_reader(lambda: claude._keychain_blob("fixture-ref", runner=run),
                                      standin))
    assert_refused("token", run_reader(
        lambda: ClaudeAdapter(runner=run)._resolve_keychain_token("fixture-ref"), standin))


# --- p1e: fixture undo cannot leave a captured delegate behind -------------------

def test_adapter_built_under_delegating_patch_is_refused_after_undo(standin, monkeypatch):
    treat_as_keychain_binary(monkeypatch, standin)
    inner = pytest.MonkeyPatch()
    inner.setattr(subprocess, "run", lambda *a, **k: REAL_RUN(*a, **k))
    adapter = ClaudeAdapter()
    inner.undo()
    assert subprocess.run is REAL_RUN
    assert_refused("token", run_reader(lambda: adapter._resolve_keychain_token("fixture-ref"),
                                       standin))
    assert_refused("plan", run_reader(lambda: adapter._plan_from_keychain("fixture-ref"),
                                      standin))


# --- p1f and p1g: production wrappers, with no test patch at all ------------------

def test_enrollment_runner_around_native_runner_cannot_reach_keychain(standin, monkeypatch):
    from tests.conftest import profile_opener
    treat_as_keychain_binary(monkeypatch, standin)
    turned = []
    adapter = ClaudeAdapter(profile_opener=profile_opener())
    assert adapter._runner is credentials._NATIVE_RUNNER
    adapter._runner = enrollment_runner(adapter._runner, lambda argv, **k: turned.append(argv)
                                        or subprocess.CompletedProcess(argv, 0, "", ""))
    assert_refused("token", run_reader(
        lambda: adapter._resolve_keychain_token("claude-quota-fixture"), standin))
    assert_refused("plan", run_reader(
        lambda: adapter._plan_from_keychain("claude-quota-fixture"), standin))
    assert turned == []


def test_daemon_reenrolment_with_default_adapter_cannot_reach_keychain(standin, tmp_path,
                                                                       monkeypatch):
    """Through Daemon._enroll_lane_locked, which wraps the adapter's native runner."""
    from subfleet import daemon as daemon_module, protocol
    from subfleet.daemon import Daemon
    from subfleet.procs import Containment
    from tests.conftest import profile_opener
    from tests.fake.conftest import Harness
    from tests.unit.test_claude_adapter import NOW, _enroll_stream, _Runner
    from tests.unit.test_enrollment_runner import _info

    treat_as_keychain_binary(monkeypatch, standin)
    root = tmp_path / "state"
    root.mkdir()
    Harness(root)
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "unit-test-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "unit-test-start")
    monkeypatch.setattr(daemon_module.procs, "same_process", lambda *args: False)
    monkeypatch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment())
    reference = "claude-quota-max@axiom.org"
    stub = _Runner(stdout=_enroll_stream(info=_info()))
    monkeypatch.setitem(registry._factories, "claude", lambda: ClaudeAdapter(
        runner=stub, now=lambda: NOW, projects_dir=tmp_path, profile_opener=profile_opener()))
    daemon = Daemon(root)
    try:
        first = daemon.dispatch("lanes", {"action": "enroll", "credential": reference})["enrolled"]
        daemon.store.update_lane(first["lane_id"], enabled=0)
        # The default adapter: its runner is the native subprocess.run.
        monkeypatch.setitem(registry._factories, "claude", lambda: ClaudeAdapter(
            now=lambda: NOW, projects_dir=tmp_path, profile_opener=profile_opener()))

        def turn(lane_id, holder, argv, *, cwd, env, timeout, **_):
            return subprocess.CompletedProcess(argv, 0, _enroll_stream(info=_info()), "")

        monkeypatch.setattr(daemon, "_enrollment_turn", turn)
        before = standin.count()
        with pytest.raises(protocol.ProtocolError, match=REFUSAL):
            daemon.dispatch("lanes", {"action": "enroll", "credential": reference})
        assert standin.count() == before
        assert [row["lane_id"] for row in daemon.store.lane_rows()
                if row["provider"] == "claude"] == [first["lane_id"]]
    finally:
        daemon.close()


# --- p1m: stripping the markers inside the session ---------------------------------

@pytest.mark.parametrize("runner", ["native", "delegating"])
@pytest.mark.parametrize("strip", ["markers", "session-identity"])
def test_stripped_markers_keep_the_session_refusal(standin, monkeypatch, runner, strip):
    """The guard pins the live session; this process's environment cannot end it."""
    treat_as_keychain_binary(monkeypatch, standin)
    if runner == "delegating":
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: REAL_RUN(*a, **k))
    if strip == "markers":
        monkeypatch.delenv(PYTEST_MARKER)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    else:
        monkeypatch.delenv(PYTEST_SESSION_ID)
    assert not home_paths.under_pytest()
    assert_refused("blob", run_reader(lambda: claude._keychain_blob("fixture-ref"), standin))


# --- P3: the real binary by any spelling --------------------------------------------

def volfs_spelling():
    found = os.stat(REAL_BINARY)
    return f"/.vol/{found.st_dev}/{found.st_ino}"


SPELLINGS = ["//usr/bin/security", "/usr/bin/./security", "/usr/bin/../bin/security",
             "/usr/../usr/bin/security", "/USR/BIN/SECURITY", "/usr/bin/ſecurity", "volfs"]


def spelled(spelling):
    if spelling == "volfs":
        spelling = volfs_spelling()
    if not os.path.exists(spelling):
        pytest.skip(f"{spelling!r} does not name the binary on this volume")
    return spelling


@pytest.mark.parametrize("runner", ["none", "native"])
@pytest.mark.parametrize("spelling", SPELLINGS)
def test_native_binary_spellings_are_refused_early(spelling, runner):
    """No launch: the self-explaining refusal compares the file, not the string."""
    path = spelled(spelling)
    with pytest.raises(AdapterError, match=REFUSAL):
        credentials.keychain_command("fixture-ref", path,
                                     runner=None if runner == "none" else credentials._NATIVE_RUNNER)


def launch_events(path, *, cwd=None, env=None):
    return {
        "subprocess.Popen": (path, [path, "find-generic-password"], cwd, env),
        "os.exec": (path, [path, "find-generic-password"], env),
        "os.posix_spawn": (path, [path, "find-generic-password"], env if env is not None else {}),
    }


@pytest.mark.parametrize("event", ["subprocess.Popen", "os.exec", "os.posix_spawn"])
@pytest.mark.parametrize("spelling", [REAL_BINARY, *SPELLINGS])
def test_native_binary_spellings_are_refused_at_launch(spelling, event):
    """Synthetic audit events reach every hook and start no program."""
    path = spelled(spelling)
    refused = credentials.KeychainExecRefused
    hits = len(SAFETY_HITS)
    with pytest.raises(refused, match=REFUSAL) as error:
        sys.audit(event, *launch_events(path)[event])
    assert error.value.code == 7
    assert SAFETY_HITS[hits:] == []


@pytest.mark.parametrize("name,cwd,env,refused", [
    pytest.param("security", None, {"PATH": "/usr/bin"}, True, id="call-path"),
    pytest.param("security", None, {}, True, id="default-search-path"),
    pytest.param("./security", "/usr/bin", None, True, id="child-cwd"),
    pytest.param("bin/security", "/usr", None, True, id="child-cwd-relative"),
    pytest.param("Security", None, {"PATH": "/usr/bin"}, True, id="bare-case-alias"),
    pytest.param("security", None, {"PATH": "/nonexistent-sf-r6"}, False, id="nothing-to-start"),
    pytest.param("true", None, None, False, id="unrelated-program"),
    pytest.param("standin", None, None, False, id="stand-in-first-on-call-path"),
])
def test_launch_target_is_found_as_the_launch_finds_it(standin, name, cwd, env, refused):
    if name == "Security" and not os.path.exists("/usr/bin/Security"):
        pytest.skip("needs a case-insensitive system volume")
    if name == "standin":
        name, env = "security", {"PATH": standin.search_path("/usr/bin")}
    details = launch_events(name, cwd=cwd, env=env)["subprocess.Popen"]
    if refused:
        with pytest.raises(credentials.KeychainExecRefused, match=REFUSAL):
            sys.audit("subprocess.Popen", *details)
    else:
        sys.audit("subprocess.Popen", *details)
    assert standin.count() == 0


# --- What still works --------------------------------------------------------------

def test_a_stub_never_launches_so_it_still_works(standin, tmp_path, monkeypatch):
    """test_policy_support's stub shape, with the stand-in guarded as the binary."""
    treat_as_keychain_binary(monkeypatch, standin)
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "private-token\n", "")

    monkeypatch.setattr("subfleet.credentials.subprocess.run", run)
    result = credentials.resolve_credential(Credential("claude", "test-service", "keychain-token"))
    assert result == {"CLAUDE_CODE_OAUTH_TOKEN": "private-token"}
    assert calls == [["security", "find-generic-password", "-s", "test-service", "-w"]]
    assert standin.count() == 0


def test_probes_detect_a_launch_that_is_not_refused(standin, tmp_path, monkeypatch):
    """Control: an unregistered stand-in is an ordinary program, and it runs."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: REAL_RUN(*a, **k))
    out = run_reader(readers(tmp_path)["blob"], standin)
    assert out == {"ran": 1, "error": None, "value": STANDIN_BLOB}
    assert REAL_RUN(["true"]).returncode == 0


# --- Installation: once per process, inert outside pytest -------------------------

def test_installing_again_adds_no_second_hook(monkeypatch):
    added = []
    monkeypatch.setattr(sys, "addaudithook", added.append)
    assert credentials.install_keychain_exec_guard() is False
    assert credentials.install_keychain_exec_guard() is False
    assert added == []
    pins = getattr(sys, credentials._EXEC_GUARD)
    session = (os.environ[PYTEST_SESSION], os.environ[PYTEST_SESSION_ID])
    assert pins.count(session) == 1


def test_replacing_module_helpers_cannot_weaken_the_installed_hook(monkeypatch):
    for name in ("_fold", "_identity", "_find_program", "_launch_targets",
                 "_is_keychain_binary", "_guard_active", "under_pytest", "session_alive"):
        monkeypatch.setattr(credentials, name, lambda *args, **kwargs: False)
    monkeypatch.setattr(credentials, "_STANDIN_KEYCHAIN_BINARIES", object())
    with pytest.raises(credentials.KeychainExecRefused, match=REFUSAL):
        sys.audit("subprocess.Popen", "/usr/bin/./security", ["security"], None, None)


def child(source, env, *args):
    result = subprocess.run([sys.executable, "-c", textwrap.dedent(source), *args], env=env,
                            cwd=REPO, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_python_child_of_the_session_is_guarded_from_import(standin):
    """A child inherits the markers; importing subfleet.credentials installs the guard."""
    env = {**os.environ, "PYTHONPATH": str(REPO), "PATH": standin.search_path()}
    env.pop("PYTEST_CURRENT_TEST", None)
    source = '''
        import json, os, subprocess, sys
        from subfleet import credentials, home_paths
        import tests.keychain_standin  # the safety net, behind the production guard
        pins = getattr(sys, credentials._EXEC_GUARD, None)
        out = {"pinned": [list(pin) for pin in pins or []],
               "installed_again": credentials.install_keychain_exec_guard()}
        credentials._STANDIN_KEYCHAIN_BINARIES = (sys.argv[1],)

        def attempt():
            try:
                subprocess.run(["security", "find-generic-password", "-s", "fixture-ref", "-w"],
                               capture_output=True, timeout=10)
                return "ran"
            except credentials.KeychainExecRefused as exc:
                return str(exc)

        out["direct"] = attempt()
        try:
            sys.audit("subprocess.Popen", "/usr/bin/security", ["/usr/bin/security"], None, None)
            out["real"] = "allowed"
        except credentials.KeychainExecRefused as exc:
            out["real"] = str(exc)
        os.environ.pop(home_paths.PYTEST_MARKER)
        out["under_pytest_after_strip"] = home_paths.under_pytest()
        out["stripped"] = attempt()
        print(json.dumps(out))
    '''
    out = child(source, env, str(standin.path))
    assert out == {
        "pinned": [[os.environ[PYTEST_SESSION], os.environ[PYTEST_SESSION_ID]]],
        "installed_again": False, "direct": REFUSAL, "real": REFUSAL,
        "under_pytest_after_strip": False, "stripped": REFUSAL,
    }
    assert standin.count() == 0


INERT_PROBE = '''
    import json, sys
    from subfleet import credentials, home_paths
    spellings = json.loads(sys.argv[1])
    out = {"under_pytest": home_paths.under_pytest(),
           "installed_at_import": getattr(sys, credentials._EXEC_GUARD, None) is not None,
           "built": [credentials.keychain_command("fixture-ref", s)[0] for s in spellings]}

    def fire():
        seen = []
        for path in spellings:
            for event, details in (("subprocess.Popen", (path, [path], None, None)),
                                   ("os.exec", (path, [path], None)),
                                   ("os.posix_spawn", (path, [path], {}))):
                try:
                    sys.audit(event, *details)
                    seen.append("allowed")
                except Exception as exc:
                    seen.append(type(exc).__name__)
        return sorted(set(seen))

    out["before"] = fire()
    out["installed"] = credentials.install_keychain_exec_guard()
    out["pins"] = getattr(sys, credentials._EXEC_GUARD)
    out["after"] = fire()
    print(json.dumps(out))
'''


def inert_spellings():
    return [REAL_BINARY, "security", *(s for s in SPELLINGS if s != "volfs"), volfs_spelling()]


def test_outside_pytest_the_guard_is_absent_and_inert(tmp_path):
    """Production: no marker, so nothing installs it, and installing it changes nothing."""
    env = {name: value for name, value in os.environ.items()
           if name not in (*MARKERS, *SAFE_HOME_PATHS)}
    env.update(HOME=str(tmp_path / "home"), PYTHONPATH=str(REPO))
    spellings = inert_spellings()
    out = child(INERT_PROBE, env, json.dumps(spellings))
    assert out == {"under_pytest": False, "installed_at_import": False, "built": spellings,
                   "before": ["allowed"], "installed": True, "pins": [], "after": ["allowed"]}


def test_after_session_cleanup_a_captured_child_guard_is_inert(tmp_path, monkeypatch):
    """A child holding expired markers installs the guard at import, and it does nothing."""
    config = SimpleNamespace()
    with monkeypatch.context() as phase:
        phase.delenv("PYTEST_CURRENT_TEST", raising=False)
        try:
            conftest.pytest_configure(config)
            captured = dict(os.environ)
        finally:
            conftest.pytest_unconfigure(config)
    captured.update(HOME=str(tmp_path / "home"), PYTHONPATH=str(REPO))
    spellings = inert_spellings()
    out = child(INERT_PROBE, captured, json.dumps(spellings))
    assert out == {"under_pytest": False, "installed_at_import": True, "built": spellings,
                   "before": ["allowed"], "installed": False, "pins": [], "after": ["allowed"]}
