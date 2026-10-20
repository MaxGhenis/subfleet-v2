"""D-ENV1: the actual daemon spawn sites cannot bypass environment selection."""

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import cli, daemon as daemon_module, doctor, protocol, scheduler
from subfleet.adapters import registry
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Credential, Exit, Lane, LaneOwner
from subfleet.policy import PolicyError
from subfleet.provider_env import ProviderEnvironment
from subfleet.store import Store
from tests.claude_code import claude_code_active
from tests.fake.conftest import Harness
from tests.fake.test_probe_recovery import reserved_probe
from tests.fake.test_routing_end_to_end import routing_state  # noqa: F401
from tests.fake_adapter import FakeAdapter
from tests.unit.test_launch_env_allowlist import ALLOWED, POLICY, POLLUTION


def daemon_spawn_environment(routing_state, monkeypatch, mode, *, pythonpath=None, extra=None):
    service, harness = routing_state
    parent = {"PATH": "/usr/bin:/bin", **POLLUTION, **(extra or {})}
    if pythonpath is not None:
        parent["PYTHONPATH"] = pythonpath
    monkeypatch.setattr(os, "environ", parent)
    policy = {**POLICY, "env": {"pass": ["PYTHONPATH"]}} if pythonpath is not None else POLICY
    service._provider_environment = ProviderEnvironment(policy)
    seen = []
    original_popen = daemon_module.subprocess.Popen
    def stopped(argv, **kwargs):
        if "subfleet.guardian" not in argv:
            return original_popen(argv, **kwargs)
        seen.append((argv, kwargs["env"]))
        raise OSError("fake guardian spawn stopped")
    monkeypatch.setattr(daemon_module.subprocess, "Popen", stopped)
    job_id = service.dispatch("submit", harness.submit_args())["job_id"]
    if mode in {"detached", "turn"}:
        service._admit()
        attempt = service.store.list_attempts(job_id)[0]
        if mode == "turn":
            service.store.conn.execute("UPDATE jobs SET kind='turn' WHERE job_id=?", (job_id,))
            manifest_path = service.root / "jobs" / job_id / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["turn"] = {"conversation_id": "fake-conversation", "message_id": "fake-message",
                                "provider": "codex", "cwd": str(harness.workdir),
                                "settings": {"model": "astra", "permission": "read-only"}}
            manifest_path.write_text(json.dumps(manifest))
            # Exercise the daemon's relay branch; turn builders have their own oracle.
            from tests.fake_adapter import FakeAdapter
            monkeypatch.setattr(service.conversations, "launch", lambda job, attempt, lane, creds, adir, model, guard:
                FakeAdapter().build_launch(service._spec(job), attempt["attempt_id"], adir, lane,
                                           creds, model, None, daemon_module.Path(job["prompt_path"]), None))
            monkeypatch.setattr(service, "_guard_override", lambda *args, **kwargs: None)
        monkeypatch.setattr(service, "_launch_failure", lambda *args, **kwargs: None)
        service._launch(attempt)
    elif mode == "probe":
        record = reserved_probe(service, job_id, state="reserved")
        service._execute_probe(service._job(job_id), service.store.get_lane("codex-1"),
                               service.policy["models"]["astra"], record["holder"])
    else:
        with pytest.raises(OSError):
            service._enrollment_turn("codex-1", "enroll:fake", ["fake-provider"],
                cwd=str(service.root), env={"PATH": "/usr/bin:/bin", "CLAUDE_CONFIG_DIR": "/fake"}, timeout=1)
    assert seen, "daemon did not reach its guardian spawn"
    argv, env = seen[0]
    assert not (set(env) & set(POLLUTION))
    # Guardian authorization carries names only and covers launch-owned variables.
    passed = {argv[i + 1] for i, item in enumerate(argv) if item == "--env-pass"}
    bootstrap = {"PYTHONPATH", "SUBFLEET_EXIT_SETTLE_S"}
    assert set(env) - ALLOWED - bootstrap <= passed
    return argv, env


@pytest.mark.parametrize("mode", ["detached", "turn", "probe", "enrollment"])
def test_actual_daemon_spawn_rejects_pollution(routing_state, monkeypatch, mode):
    daemon_spawn_environment(routing_state, monkeypatch, mode)


def test_daemon_status_and_doctor_expose_only_extra_names(routing_state, monkeypatch):
    from subfleet import doctor
    service, _ = routing_state
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", **POLLUTION})
    status = service.dispatch("daemon.status", {})
    # Round 4 (review r3, P3-2): the lanes' own variables are listed apart; this
    # roster has no env-kind lane.
    assert status["environment"] == {"extra_names": sorted(POLLUTION), "lane_credentials": []}
    assert "dummy" not in json.dumps(status["environment"])
    monkeypatch.setattr(doctor.Client, "call", lambda *args, **kwargs: status)
    output = doctor.render([doctor.check_daemon_environment(service.root)])
    assert all(name in output for name in POLLUTION)
    assert "dummy" not in output


@pytest.mark.parametrize("mode", ["detached", "turn", "probe", "enrollment"])
@pytest.mark.parametrize("relay", [False, True], ids=["stdin", "relay"])
@pytest.mark.parametrize("pythonpath", [None, "", "/dummy/policy-python"])
def test_guardian_bootstrap_preserves_provider_pythonpath(routing_state, monkeypatch, tmp_path, mode, relay, pythonpath):
    """Run each real daemon launch through the guardian's actual provider seam."""
    from subfleet import guardian
    with monkeypatch.context() as patch:
        argv, environment = daemon_spawn_environment(
            routing_state, patch, mode, pythonpath=pythonpath)
    passed = tuple(argv[i + 1] for i, item in enumerate(argv) if item == "--env-pass")
    # Even exact policy authority cannot export the guardian's private setting.
    passed += ("SUBFLEET_EXIT_SETTLE_S",)
    seen = []
    monkeypatch.setattr(guardian.os, "environ", environment)
    monkeypatch.setattr(guardian.os, "setsid", lambda: None)
    monkeypatch.setattr(guardian.signal, "signal", lambda *args: None)
    monkeypatch.setattr(guardian, "boot_id", lambda: "fake")
    monkeypatch.setattr(guardian, "proc_start", lambda pid: "fake")
    monkeypatch.setattr(guardian, "linger", lambda: None)
    monkeypatch.setattr(guardian.subprocess, "Popen", lambda *args, **kwargs:
                        seen.append(kwargs["env"]) or type("Child", (), {"pid": 123, "wait": lambda self: 0})())
    if relay:
        from subfleet import relay as relay_module
        from types import SimpleNamespace
        monkeypatch.setattr(relay_module, "RelayServer", lambda *args, **kwargs: SimpleNamespace(
            bind=lambda: None, serve=lambda fd, **kwargs: os.close(fd), stop=lambda: None))
    previous = os.umask(0o077)
    try:
        guardian.run_guardian(["fake-provider"], attempt_dir=tmp_path, cwd=str(tmp_path), stdin_path=None,
                              stdout_path=str(tmp_path / "stdout"), stderr_path=str(tmp_path / "stderr"),
                              env_pass=passed, control_socket=str(tmp_path / "relay.sock") if relay else None)
    finally:
        os.umask(previous)
    if pythonpath is None:
        assert "PYTHONPATH" not in seen[0]
    else:
        assert seen[0]["PYTHONPATH"] == pythonpath
    assert "SUBFLEET_EXIT_SETTLE_S" not in seen[0]
    assert not any(name.startswith("_SUBFLEET_") for name in seen[0])


def test_review_r2_real_case_reaches_no_codex_guardian(routing_state, monkeypatch):
    """Review r2: the env-kind example set in the daemon's shell reached a Codex lane."""
    service, _ = routing_state
    argv, env = daemon_spawn_environment(routing_state, monkeypatch, "detached",
                                         extra={"SUBFLEET_TEST_OAUTH": "dummy-oauth"})
    assert "SUBFLEET_TEST_OAUTH" not in set(env) | set(argv)
    assert not any(value == "dummy-oauth" for value in env.values())
    assert "SUBFLEET_TEST_OAUTH" in service.dispatch("daemon.status", {})["environment"]["extra_names"]


# Each way a lane's reference reached other launches before round 3.
CHANNELS = {
    # Round 2's e2e policy passed both references. Validation now refuses that
    # policy; this sets it beneath validation, where the launch still withholds them.
    "policy": ("E2E_LANE_ONE_TOKEN", "E2E_LANE_TWO_TOKEN"),
    # Round 2's prefix rule admitted these to every launch.
    "prefix": ("SUBFLEET_TEST_OAUTH", "SUBFLEET_LANE_TWO_OAUTH"),
    # A harness name every launch inherits can never be one lane's credential.
    "harness": ("SUBFLEET_FAKE_LANE_ONE", "SUBFLEET_FAKE_LANE_TWO"),
}


@pytest.mark.parametrize("channel", sorted(CHANNELS))
def test_env_kind_lane_credentials_reach_only_their_own_lane(routing_state, monkeypatch, channel):
    """Review r2 P3: lane 1's launch holds neither lane 2's name nor value; Codex holds neither."""
    service, harness = routing_state
    references, values = CHANNELS[channel], ("dummy-lane-one", "dummy-lane-two")
    lanes = [Lane(f"claude-{index + 1}", "claude", f"claude:lane-{index + 1}",
                  Credential("claude", reference, "env"), None, LaneOwner.V2, False)
             for index, reference in enumerate(references)]
    for lane in lanes:
        service.store.put_lane(lane)
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", **dict(zip(references, values))})
    if channel == "policy":
        service._provider_environment.pass_names = references
    prompt = harness.workdir / "prompt.md"
    prompt.write_text("Reply OK")

    def launched(lane):
        """The daemon's own seams: credential, launch and the relaunch after a restart."""
        credential = service._credential_environment(lane)
        launch = FakeAdapter().build_launch(SimpleNamespace(workdir=str(harness.workdir)), "fake/a1",
                                            harness.workdir, lane, credential, "fake", None, prompt, None)
        return service._launch_environment(lane, launch), service._relaunch_env(lane.lane_id)

    for env in launched(service.store.get_lane("codex-1")):
        assert not set(references) & set(env)
        assert not any(value in values for value in env.values())
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in set(env)
    for own, lane in enumerate(lanes):
        if channel == "harness":
            with pytest.raises(AdapterError) as error:
                service._credential_environment(lane)
            assert error.value.reason == "credential-env-inherited"
            continue
        for env in launched(lane):
            assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == values[own], "its own credential"
            assert not set(references) & set(env)
            assert not any(value == values[1 - own] for value in env.values())


def test_daemon_refuses_a_policy_passing_a_lane_credential(tmp_path, monkeypatch):
    """Policy validation at startup, before the roster is seeded; the lock is released."""
    root = tmp_path / "state"
    root.mkdir()
    Harness(root)
    # As `routing_state` starts its daemon: no host identity, keychain or provider.
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
    monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
    monkeypatch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
    claude_code_active(monkeypatch, tmp_path / "claude")
    roster = json.loads((root / "lanes.json").read_text())
    roster.append({"lane_id": "claude-1", "provider": "claude", "account_key": "claude:lane-1",
                   "credential_kind": "env", "credential_ref": "E2E_LANE_ONE_TOKEN", "owner": "v2"})
    (root / "lanes.json").write_text(json.dumps(roster))
    policy = json.loads((root / "policy.json").read_text())
    policy["env"] = {"pass": ["E2E_LANE_ONE_TOKEN"]}
    (root / "policy.json").write_text(json.dumps(policy))
    try:
        started = daemon_module.Daemon(root)
    except PolicyError as exc:
        assert exc.key == "env.pass[0]"
    else:
        started.close()
        pytest.fail("a policy passing a lane credential started the daemon")
    policy["env"] = {"pass": []}
    (root / "policy.json").write_text(json.dumps(policy))
    daemon_module.Daemon(root).close()


def startup_root(tmp_path, monkeypatch, reference, passed=()):
    """A seeded root whose roster adds one env-kind Claude lane, as `routing_state` starts it."""
    root = tmp_path / "state"
    root.mkdir()
    Harness(root)
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
    monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
    monkeypatch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
    claude_code_active(monkeypatch, tmp_path / "claude")
    roster = json.loads((root / "lanes.json").read_text())
    roster.append({"lane_id": "claude-9", "provider": "claude", "account_key": "claude:lane-9",
                   "credential_kind": "env", "credential_ref": reference, "owner": "v2"})
    (root / "lanes.json").write_text(json.dumps(roster))
    policy = json.loads((root / "policy.json").read_text())
    policy["env"] = {"pass": list(passed)}
    (root / "policy.json").write_text(json.dumps(policy))
    return root, policy


@pytest.mark.parametrize("reference", ["HOME", "SUBFLEET_FAKE_LANE_NINE", "SUBFLEET_HOST_HOME"])
def test_daemon_startup_refuses_a_lane_variable_every_launch_inherits(tmp_path, monkeypatch, reference):
    """Review r3, P3-1: the start check is the first line of defence, at daemon startup too."""
    root, policy = startup_root(tmp_path, monkeypatch, reference)
    with pytest.raises(PolicyError) as error:
        daemon_module.Daemon(root).close()
    assert error.value.key == "credential_ref" and reference in str(error.value)
    # The refusal released the lock: a second start reaches the same check,
    # not "already running", and still from lanes.json: nothing was stored.
    with pytest.raises(PolicyError) as again:
        daemon_module.Daemon(root).close()
    assert again.value.key == "credential_ref"


@pytest.mark.parametrize("case", ["env-pass", "inherited"])
def test_daemon_main_exits_two_on_a_refused_startup(tmp_path, monkeypatch, capsys, case):
    """Review r3, P3-3 (C-11.1): exit 2 with the message, never a traceback in daemon.log."""
    if case == "env-pass":
        root, _ = startup_root(tmp_path, monkeypatch, "E2E_LANE_NINE_TOKEN",
                               passed=["GIT_CONFIG_GLOBAL", "E2E_LANE_NINE_TOKEN"])
        key = "env.pass[1]"
    else:
        root, _ = startup_root(tmp_path, monkeypatch, "LANG")
        key = "credential_ref"
    monkeypatch.setenv("E2E_LANE_NINE_TOKEN", "dummy-nine")
    monkeypatch.setattr(daemon_module.descriptors, "raise_open_file_limit", lambda: (256, 256, 256))
    monkeypatch.setattr(daemon_module.Daemon, "serve_forever",
                        lambda self: pytest.fail("a refused startup served"))
    assert daemon_module.main(["--state-root", str(root)]) == 2
    err = capsys.readouterr().err
    assert key in err and "Traceback" not in err and "dummy-nine" not in err


def test_daemon_status_lists_a_lane_variable_apart(tmp_path, monkeypatch):
    """Review r3, P3-2: the daemon holds its lane's variable, and says so apart from extras."""
    root, _ = startup_root(tmp_path, monkeypatch, "SUBFLEET_TEST_OAUTH")
    service = daemon_module.Daemon(root)
    try:
        monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", "SUBFLEET_TEST_OAUTH": "dummy-oauth",
                                           "GH_TOKEN": "dummy-gh"})
        status = service.dispatch("daemon.status", {})["environment"]
    finally:
        service.close()
    assert status == {"extra_names": ["GH_TOKEN"], "lane_credentials": ["SUBFLEET_TEST_OAUTH"]}
    assert "dummy" not in json.dumps(status)


# --- review r4, P2-1: a refused lane is never stored, and a stored one is held ------------

# Off the launch allowlist: "AUTH" is a credential word, so no launch inherits it.
TOKEN = "SUBFLEET_TEST_OAUTH"
# Review r4's two inputs: a harness hook and a listed name, each one every launch inherits.
STEP_REFERENCES = ["SUBFLEET_FAKE_LANE_NINE", "HOME"]


def nine_row(reference, account="claude:lane-9"):
    return {"lane_id": "claude-9", "provider": "claude", "account_key": account,
            "credential_kind": "env", "credential_ref": reference, "owner": "v2"}


def roster(root):
    return json.loads((root / "lanes.json").read_text())


def write_roster(root, rows):
    (root / "lanes.json").write_text(json.dumps(rows))


def daemon_fixture(tmp_path, monkeypatch):
    """As `routing_state` starts its daemon: no host identity, keychain or provider."""
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "fixture-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "fixture-start")
    monkeypatch.setattr(daemon_module.capacity, "read_desktop_account", lambda: None)
    monkeypatch.setattr(registry, "_factories", {"codex": FakeAdapter, "claude": FakeAdapter})
    claude_code_active(monkeypatch, tmp_path / "claude")


def nine_root(tmp_path, monkeypatch, reference):
    """Review r4's root: the fake roster (`codex-1`) and env-kind `claude-9` on `reference`."""
    root = tmp_path / "state"
    root.mkdir()
    harness = Harness(root)
    daemon_fixture(tmp_path, monkeypatch)
    # `daemon start` takes its root from SUBFLEET_HOME alone, never its arguments.
    monkeypatch.setenv("SUBFLEET_HOME", str(root))
    assert cli._root(SimpleNamespace()).resolve() == root.resolve()
    write_roster(root, [*roster(root), nine_row(reference)])
    if reference != "HOME":
        monkeypatch.setenv(reference, "dummy-nine")
    return root, harness


def store_as_round_four_left_it(root):
    """What a start at `b27681e16` left: it seeded every lanes.json row, claude-9
    included, and only then refused (review r4, P2-1)."""
    with Store(root / "state.sqlite3") as store:
        for row in roster(root):
            if store.get_lane(row["lane_id"]) is None:
                store.put_lane(Lane(
                    row["lane_id"], row["provider"], row["account_key"],
                    Credential(row["provider"], row["credential_ref"], row["credential_kind"],
                               row.get("credential_epoch", 1)),
                    row.get("home"), LaneOwner(row.get("owner", "v2")),
                    bool(row.get("desktop", False)), bool(row.get("enabled", True))))


def stored(root):
    """{lane id: (credential variable or home, credential epoch)}: names only."""
    with Store(root / "state.sqlite3") as store:
        return {lane.lane_id: (lane.credential.ref, lane.credential.epoch) for lane in store.list_lanes()}


def launchd_start(root, monkeypatch, check=None):
    """`daemon.main(["--state-root", root])`, launchd's path: (exit code, stderr, served).

    `check(daemon)` runs where serving would begin, then the daemon closes. The
    process's own SIGTERM and SIGINT handlers are put back afterwards.
    """
    served = []

    def serve(self):
        try:
            served.append(check(self) if check else "served")
        finally:
            self.close()

    monkeypatch.setattr(daemon_module.descriptors, "raise_open_file_limit", lambda: (256, 256, 256))
    monkeypatch.setattr(daemon_module.Daemon, "serve_forever", serve)
    monkeypatch.setattr(daemon_module, "watch_stop", lambda *args: (lambda: None))
    monkeypatch.setattr(daemon_module, "stop_request", lambda *args: (lambda *signal_args: None))
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            code = daemon_module.main(["--state-root", str(root)])
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    return code, err.getvalue(), served


def cli_start(root, monkeypatch):
    """`subfleet daemon start` up to a spawn that answers: (exit code, stderr, environments)."""
    assert cli._root(SimpleNamespace()).resolve() == root.resolve(), "daemon start would leave the test's root"
    seen, alive = [], iter([False])

    class Child:
        pid, returncode = 4242, None

        def poll(self):
            return None

    err = io.StringIO()
    # `cli.subprocess` is the process's own `subprocess`: the stand-in is undone
    # before any daemon of the test runs Git or a guardian.
    with monkeypatch.context() as patch:
        patch.setattr(cli, "_daemon_alive", lambda client: next(alive, True))
        patch.setattr(cli.Client, "lock_info", lambda self: None)
        patch.setattr(cli.subprocess, "Popen", lambda argv, **kwargs: seen.append(kwargs["env"]) or Child())
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = cli.cmd_daemon_start(SimpleNamespace(state_root=str(root)))
    return code, err.getvalue(), seen


def launch(service, harness, monkeypatch, **pins):
    """Submit, admit and launch one job: (job id, the guardian spawns it reached)."""
    seen = []
    original_popen = daemon_module.subprocess.Popen

    def stopped(argv, **kwargs):
        if "subfleet.guardian" not in argv:
            return original_popen(argv, **kwargs)
        seen.append((argv, kwargs["env"]))
        raise OSError("fake guardian spawn stopped")

    with monkeypatch.context() as patch:
        patch.setattr(daemon_module.subprocess, "Popen", stopped)
        patch.setattr(service, "_launch_failure", lambda *args, **kwargs: None)
        job_id = service.dispatch("submit", harness.submit_args(**pins))["job_id"]
        service._admit()
        for attempt in service.store.list_attempts(job_id):
            service._launch(attempt)
    return job_id, seen


def lane_launch_environment(service, harness, lane_id):
    """A launch on one lane through the daemon's own seams: its credential, then its environment."""
    lane = service.store.get_lane(lane_id)
    prompt = harness.workdir / "prompt.md"
    prompt.write_text("Reply OK")
    credential = service._credential_environment(lane)
    launch = FakeAdapter().build_launch(SimpleNamespace(workdir=str(harness.workdir)), "fake/a1",
                                        harness.workdir, lane, credential, "fake", None, prompt, None)
    return service._launch_environment(lane, launch)


def other_lane_launches(service, harness, monkeypatch, reference, *, held):
    """codex-1 launches with the parent's names and no lane's credential. A held
    lane's variable is withheld only when it is a harness hook, never a name every
    launch needs (HOME); once repaired, no lane names it at all."""
    _, spawns = launch(service, harness, monkeypatch)
    assert len(spawns) == 1, "codex-1 reached its guardian"
    _, env = spawns[0]
    assert TOKEN not in env and "dummy-lane-nine" not in env.values(), "no lane's credential"
    if reference == "HOME":
        assert env["HOME"] == os.environ["HOME"], "every other lane keeps HOME"
    elif held:
        assert reference not in env and "dummy-nine" not in env.values(), "a held lane's hook is withheld"


@pytest.mark.parametrize("reference", STEP_REFERENCES)
def test_review_r4_a_refused_first_start_stores_nothing(tmp_path, monkeypatch, reference):
    """Step 1: launchd's start exits 2 naming the lane, its file and the variable,
    before any lanes.json row is stored; `daemon start` refuses alike, before any spawn."""
    root, _ = nine_root(tmp_path, monkeypatch, reference)
    code, err, served = launchd_start(root, monkeypatch, lambda service: pytest.fail("a refused roster served"))
    assert (code, served) == (2, [])
    assert all(text in err for text in ("claude-9", reference, "lanes.json")), err
    assert "Traceback" not in err and "dummy-nine" not in err
    assert stored(root) == {}, "the check runs before lanes.json is seeded"
    assert cli._root(SimpleNamespace()).resolve() == root.resolve(), "daemon start would leave the test's root"
    err = io.StringIO()
    with monkeypatch.context() as patch:
        patch.setattr(cli, "_daemon_alive", lambda *args: False)
        patch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("a refused roster spawned"))
        with contextlib.redirect_stderr(err):
            assert cli.cmd_daemon_start(SimpleNamespace(state_root=str(root))) == 2
    assert all(text in err.getvalue() for text in ("claude-9", reference, "lanes.json"))
    assert stored(root) == {}


@pytest.mark.parametrize("reference", STEP_REFERENCES)
def test_review_r4_a_fixed_variable_repairs_the_stored_lane(tmp_path, monkeypatch, reference):
    """Step 2: lanes.json points claude-9 at a variable off the allowlist, as the
    refusal advises. Both starts succeed; the stored lane takes the variable (a new
    credential epoch), claude-9 launches with its own token, and codex-1 launches."""
    root, harness = nine_root(tmp_path, monkeypatch, reference)
    store_as_round_four_left_it(root)
    assert stored(root)["claude-9"] == (reference, 1)
    write_roster(root, [nine_row(TOKEN) if row["lane_id"] == "claude-9" else row for row in roster(root)])
    monkeypatch.setenv(TOKEN, "dummy-lane-nine")
    code, err, seen = cli_start(root, monkeypatch)
    assert code == 0 and "claude-9" not in err, err
    assert seen[0][TOKEN] == "dummy-lane-nine", "the daemon holds the lane's new variable"

    def check(service):
        assert service.store.get_lane("claude-9").credential == Credential("claude", TOKEN, "env", 2)
        own = lane_launch_environment(service, harness, "claude-9")
        assert own["CLAUDE_CODE_OAUTH_TOKEN"] == "dummy-lane-nine" and TOKEN not in own
        other_lane_launches(service, harness, monkeypatch, reference, held=False)
        return "served"

    code, err, served = launchd_start(root, monkeypatch, check)
    assert (code, served) == (0, ["served"]), err
    assert stored(root)["claude-9"] == (TOKEN, 2)
    with Store(root / "state.sqlite3") as store:
        events = store.query("SELECT data_json FROM events WHERE kind='lane.reference_repaired' "
                             "AND lane_id='claude-9' AND data_json != '{}'")
    assert [json.loads(row["data_json"]) for row in events] == [{"from": reference, "to": TOKEN, "epoch": 2}]
    # A second start finds nothing to repair.
    code, err, _ = launchd_start(root, monkeypatch)
    assert code == 0, err
    assert stored(root)["claude-9"] == (TOKEN, 2)


@pytest.mark.parametrize("reference", STEP_REFERENCES)
def test_review_r4_a_removed_lane_is_held_and_never_blocks_startup(tmp_path, monkeypatch, reference):
    """Step 3: with claude-9 gone from lanes.json, both starts succeed and say so,
    naming the lane, the store and the variable; nothing launches on claude-9
    (held for good, and resolution refuses it), and every other lane launches."""
    root, harness = nine_root(tmp_path, monkeypatch, reference)
    store_as_round_four_left_it(root)
    write_roster(root, [row for row in roster(root) if row["lane_id"] != "claude-9"])
    code, err, seen = cli_start(root, monkeypatch)
    assert code == 0 and len(seen) == 1, err
    assert all(text in err for text in ("claude-9", reference, "the store")) and "dummy-nine" not in err

    def check(service):
        lane = service.store.get_lane("claude-9")
        assert lane.credential.ref == reference, "nothing in lanes.json repairs it"
        job_id, spawns = launch(service, harness, monkeypatch, pinned_model="haiku", pinned_lane="claude-9")
        assert spawns == [] and service.store.list_attempts(job_id) == []
        held = service.store.query("SELECT data_json FROM events WHERE kind='job.pin_unadmittable' "
                                   "AND job_id=? AND data_json != '{}'", (job_id,))
        assert [json.loads(row["data_json"])["reasons"] for row in held] == [["credential-refused"]]
        with pytest.raises(AdapterError) as refused:
            service._credential_environment(lane)
        assert refused.value.reason == "credential-env-inherited"
        other_lane_launches(service, harness, monkeypatch, reference, held=True)
        return "served"

    code, err, served = launchd_start(root, monkeypatch, check)
    assert (code, served) == (0, ["served"]), err
    log = (root / "daemon.log").read_text()
    assert all(text in log for text in ("claude-9", reference, "the store")) and "dummy-nine" not in log
    # launchd's KeepAlive restart starts again, with the lane still held.
    assert launchd_start(root, monkeypatch)[0] == 0
    assert stored(root)["claude-9"] == (reference, 1)


# --- review r5, P3-1: a refused row for a stored lane is ignored -----------------------------


def naming(text, lane_id="claude-9"):
    """The lines of `text` that name the lane."""
    return [line for line in text.splitlines() if lane_id in line]


@pytest.mark.parametrize("reference", STEP_REFERENCES)
def test_review_r5_a_refused_row_for_a_stored_lane_is_ignored(tmp_path, monkeypatch, reference):
    """Review r5, P3-1, input 1: the store round 4 left, with lanes.json unchanged.
    Both starts exited 2 and the lane was never held. Now both succeed and name
    claude-9 once, with its variable and never a value; claude-9 stays held at
    every start, `lanes transfer` refuses it, doctor names it with its fix, and
    codex-1 launches."""
    root, harness = nine_root(tmp_path, monkeypatch, reference)
    store_as_round_four_left_it(root)
    assert stored(root)["claude-9"] == (reference, 1) and nine_row(reference) in roster(root)
    code, err, seen = cli_start(root, monkeypatch)
    assert code == 0 and len(seen) == 1, err
    [line] = naming(err)
    assert all(text in line for text in (reference, "the store", "the lane is held", "the row is ignored")), line
    assert "dummy-nine" not in err

    def check(service):
        [line] = naming((root / "daemon.log").read_text())
        assert all(text in line for text in (reference, "the lane is held", "the row is ignored")), line
        lane = service.store.get_lane("claude-9")
        assert lane.credential.ref == reference, "the ignored row repairs nothing"
        job_id, spawns = launch(service, harness, monkeypatch, pinned_model="haiku", pinned_lane="claude-9")
        assert spawns == [] and service.store.list_attempts(job_id) == []
        held = service.store.query("SELECT data_json FROM events WHERE kind='job.pin_unadmittable' "
                                   "AND job_id=? AND data_json != '{}'", (job_id,))
        assert [json.loads(row["data_json"])["reasons"] for row in held] == [["credential-refused"]]
        with pytest.raises(AdapterError) as refused:
            service._credential_environment(lane)
        assert refused.value.reason == "credential-env-inherited"
        other_lane_launches(service, harness, monkeypatch, reference, held=True)
        # Input 3: the daemon's `lanes transfer` op refuses the held lane, writing nothing.
        before = (root / "lanes.json").read_bytes()
        with pytest.raises(protocol.ProtocolError) as transfer:
            service.dispatch("lanes", {"action": "transfer", "lane_id": "claude-9", "owner": "v1",
                                       "confirm_v1_edit": True})
        assert transfer.value.reason == "transfer-held" and int(transfer.value.code) == int(Exit.REFUSED)
        assert (root / "lanes.json").read_bytes() == before
        # P3-2: doctor's lane-variable row warns, naming the lane and its lanes.json edit.
        status = service.dispatch("daemon.status", {})
        with monkeypatch.context() as patch:
            patch.setattr(doctor.Client, "call", lambda *args, **kwargs: status)
            row = doctor.check_daemon_lane_credentials(service.root)
        assert row["status"] == doctor.WARN
        assert f"claude-9 (credential-refused={reference})" in row["detail"]
        assert '"lane_id": "claude-9"' in row["fix"] and '"account_key": "claude:lane-9"' in row["fix"]
        assert "dummy" not in json.dumps(row)
        return "served"

    code, err, served = launchd_start(root, monkeypatch, check)
    assert (code, served) == (0, ["served"]), err
    assert "dummy-nine" not in (root / "daemon.log").read_text()
    # launchd's KeepAlive restart starts again, the lane still held and the row still ignored.
    size = len((root / "daemon.log").read_text())
    code, err, served = launchd_start(root, monkeypatch)
    assert (code, served) == (0, ["served"]), err
    assert len(naming((root / "daemon.log").read_text()[size:])) == 1
    assert stored(root)["claude-9"] == (reference, 1) and nine_row(reference) in roster(root)


def test_review_r5_a_refused_row_never_displaces_a_healthy_stored_lane(tmp_path, monkeypatch):
    """Review r5, P3-1, input 2: claude-9 stored and healthy on its own variable,
    its lanes.json row changed to HOME. Both starts exited 2, though seeding
    never stores that row. Now both succeed with one warning that the row is
    ignored, and claude-9 launches with its own token."""
    root, harness = nine_root(tmp_path, monkeypatch, TOKEN)
    store_as_round_four_left_it(root)
    assert stored(root)["claude-9"] == (TOKEN, 1)
    write_roster(root, [nine_row("HOME") if row["lane_id"] == "claude-9" else row for row in roster(root)])
    code, err, seen = cli_start(root, monkeypatch)
    assert code == 0, err
    [line] = naming(err)
    assert "HOME" in line and "the row is ignored" in line and "the lane is held" not in line
    assert seen[0][TOKEN] == "dummy-nine", "the daemon still holds the stored lane's variable"
    assert "dummy-nine" not in err

    def check(service):
        [line] = naming((root / "daemon.log").read_text())
        assert "HOME" in line and "the row is ignored" in line and "the lane is held" not in line
        own = lane_launch_environment(service, harness, "claude-9")
        assert own["CLAUDE_CODE_OAUTH_TOKEN"] == "dummy-nine" and TOKEN not in own
        view = {"lanes": service.store.lane_rows(), "closures": [], "now": "2026-10-20T12:00:00Z"}
        stuck = scheduler.pin_unadmittable(service.policy, view, {"pinned_lane": "claude-9", "pinned_model": "haiku"})
        assert "credential-refused" not in ((stuck or {}).get("reasons") or ())
        return "served"

    code, err, served = launchd_start(root, monkeypatch, check)
    assert (code, served) == (0, ["served"]), err
    assert stored(root)["claude-9"] == (TOKEN, 1)


# --- invariant: stored lanes never refuse startup --------------------------------------

REFUSED = ("HOME", "SUBFLEET_FAKE_LANE_NINE", "SUBFLEET_HOST_HOME")
CREDENTIALS = ("SUBFLEET_TEST_OAUTH", "E2E_LANE_TWO_TOKEN")
LANES = st.lists(st.tuples(st.sampled_from(["claude-7", "claude-8", "claude-9"]),
                           st.sampled_from(REFUSED + CREDENTIALS),
                           st.sampled_from(["claude:lane-a", "claude:lane-b"])),
                 max_size=3, unique_by=lambda lane: lane[0])


def expected_startup(roster_lanes, store_lanes, passed):
    """The model, from the rule as review r4's decision states it, not from the code.

    A lanes.json row is checked before anything is stored, and refuses startup if
    its variable is refused and the store has no lane by its id; for a stored
    lane the row is ignored (review r5, P3-1). A stored lane whose variable is
    refused takes a lanes.json row's variable for the same lane and account when
    that one is not refused; otherwise it is held. `env.pass` may name no lane's
    credential.
    """
    refused = set(REFUSED).__contains__
    before = {lane_id: (ref, account) for lane_id, ref, account in store_lanes}
    repaired = {lane_id: ref for lane_id, ref, account in roster_lanes
                if lane_id in before and refused(before[lane_id][0]) and not refused(ref)
                and account == before[lane_id][1]}
    effective = {lane_id: repaired.get(lane_id, ref) for lane_id, (ref, _) in before.items()}
    credentials = ({ref for _, ref, _ in roster_lanes if not refused(ref)}
                   | {ref for ref in effective.values() if not refused(ref)})
    refuses = (any(refused(ref) and lane_id not in before for lane_id, ref, _ in roster_lanes)
               or bool(set(passed) & credentials))
    return refuses, repaired, effective


@pytest.fixture
def startup_property(tmp_path, monkeypatch):
    daemon_fixture(tmp_path, monkeypatch)
    for name in REFUSED[1:] + CREDENTIALS:
        monkeypatch.setenv(name, "dummy-" + name.lower())
    policy = json.loads((Path(daemon_module.__file__).with_name("default_policy.json")).read_text())
    policy.setdefault("reserve", {})["models"] = []
    policy.setdefault("admission", {})["prove_idle_s"] = None
    policy.setdefault("conversations", {})["catalog_interval_s"] = 0
    return tmp_path, policy


@settings(max_examples=25, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(roster_lanes=LANES, store_lanes=LANES,
       passed=st.sets(st.sampled_from(["GIT_CONFIG_GLOBAL", "E2E_LANE_TWO_TOKEN", "HOME"])))
def test_bounded_property_stored_lanes_never_refuse_startup(startup_property, roster_lanes, store_lanes, passed):
    """Invariants (review r4, P2-1), for every lanes.json and store pair:
    the daemon starts unless the policy or a lanes.json row is refused, whatever
    the store holds; a refused lane never launches; no refused row is newly
    stored; and `daemon start` reaches the same verdict before the daemon runs."""
    base, policy = startup_property
    root = Path(tempfile.mkdtemp(dir=base))
    (root / "policy.json").write_text(json.dumps({**policy, "env": {"pass": sorted(passed)}}))
    write_roster(root, [{**nine_row(ref, account), "lane_id": lane_id} for lane_id, ref, account in roster_lanes])
    with Store(root / "state.sqlite3") as store:
        for lane_id, ref, account in store_lanes:
            store.put_lane(Lane(lane_id, "claude", account, Credential("claude", ref, "env"), None, LaneOwner.V2, False))
    refuses, repaired, effective = expected_startup(roster_lanes, store_lanes, passed)
    before = stored(root)
    try:
        cli.daemon_env(root)
    except PolicyError:
        assert refuses, "daemon start refused a startup the rule allows"
    else:
        assert not refuses, "daemon start allowed a startup the rule refuses"
    try:
        service = daemon_module.Daemon(root)
    except PolicyError:
        assert refuses, "the daemon refused a startup the rule allows"
        assert stored(root) == before, "a refused startup stores and repairs nothing"
        return
    try:
        assert not refuses, "the daemon started past a refused policy or lanes.json row"
        rows = service.store.lane_rows()
        after = {row["lane_id"]: row for row in rows}
        new = {lane_id: ref for lane_id, ref, _ in roster_lanes if lane_id not in before}
        assert set(after) == set(before) | set(new)
        view = {"lanes": rows, "closures": [], "now": "2026-10-18T12:00:00Z"}
        for lane_id, row in after.items():
            ref = effective[lane_id] if lane_id in before else new[lane_id]
            epoch = before[lane_id][1] + (lane_id in repaired) if lane_id in before else 1
            assert (row["credential_ref"], row["credential_epoch"]) == (ref, epoch), lane_id
            held = ref in REFUSED
            assert not held or lane_id in before, "no refused row is newly stored"
            stuck = scheduler.pin_unadmittable(service.policy, view, {"pinned_lane": lane_id, "pinned_model": "haiku"})
            assert ("credential-refused" in ((stuck or {}).get("reasons") or ())) == held, lane_id
            if held:
                with pytest.raises(AdapterError):
                    service._credential_environment(Store.lane_from_row(row))
    finally:
        service.close()


@pytest.mark.parametrize("where", ["lanes.json", "the store"])
def test_review_r4_daemon_main_names_the_lane_an_env_pass_entry_belongs_to(tmp_path, monkeypatch, where):
    """Review r4, P3-1, at daemon startup: `env.pass[i]`, the variable and the lane
    with where it came from; never the value, and nothing is stored."""
    root, _ = nine_root(tmp_path, monkeypatch, "E2E_LANE_NINE_TOKEN")
    if where == "the store":
        store_as_round_four_left_it(root)
        write_roster(root, [row for row in roster(root) if row["lane_id"] != "claude-9"])
    policy = json.loads((root / "policy.json").read_text())
    (root / "policy.json").write_text(json.dumps({**policy, "env": {"pass": ["GIT_CONFIG_GLOBAL", "E2E_LANE_NINE_TOKEN"]}}))
    before = stored(root)
    code, err, served = launchd_start(root, monkeypatch, lambda service: pytest.fail("a refused policy served"))
    assert (code, served) == (2, [])
    assert all(text in err for text in ("env.pass[1]", "E2E_LANE_NINE_TOKEN", "claude-9", where)), err
    assert "dummy-nine" not in err and "Traceback" not in err
    assert stored(root) == before
