"""Round 2: configuration inheritance never overrides launch isolation."""

import json
import os
from types import SimpleNamespace

import pytest

from subfleet import cli, doctor
from subfleet.launch_env import build_environment
from subfleet.provider_env import ProviderEnvironment
from tests.unit.test_launch_env_allowlist import launch_for


def test_daemon_start_preserves_harness_configuration_not_credentials(tmp_path, monkeypatch):
    parent = {"PATH": "/usr/bin:/bin", "SUBFLEET_HOME": str(tmp_path), "SUBFLEET_FAKE_SCENARIO": "dummy",
              "SUBFLEET_LIVE": "0", "SUBFLEET_TEST_TOKEN": "dummy"}
    monkeypatch.setattr(os, "environ", parent)
    states = iter([False, True])
    monkeypatch.setattr(cli, "_daemon_alive", lambda client: next(states))
    monkeypatch.setattr(cli, "_daemond_argv", lambda root: ["fake-daemon"])
    monkeypatch.setattr(cli.Client, "lock_info", lambda self: {})
    seen = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs:
                        seen.append(kwargs["env"]) or SimpleNamespace(pid=123))
    assert cli.cmd_daemon_start(SimpleNamespace(state_root=str(tmp_path))) == 0
    child = ProviderEnvironment({}).build("claude", environ=seen[0])
    assert child["SUBFLEET_FAKE_SCENARIO"] == "dummy"
    assert child["SUBFLEET_LIVE"] == "0"
    assert "SUBFLEET_TEST_TOKEN" not in child


def test_e2e_harness_explicitly_authorizes_nonprefix_hooks(tmp_path, monkeypatch):
    from tests.e2e.conftest import E2E
    harness = E2E(tmp_path)
    try:
        monkeypatch.setattr(os, "environ", harness.env)
        daemon = cli.daemon_env(tmp_path)
        policy = json.loads((tmp_path / "policy.json").read_text())
        child = ProviderEnvironment(policy).build("claude", environ=daemon)
        required = {"PYTHONPATH", "CLAUDE_FAKE_PROJECTS_DIR", "GIT_CONFIG_NOSYSTEM",
                    "GIT_CONFIG_GLOBAL", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"}
        assert required <= child.keys()
        # Round 3: the lanes' references are the daemon's alone (D-ENV1).
        references = {"E2E_CLAUDE_TOKEN_1", "E2E_CLAUDE_TOKEN_2"}
        assert references <= daemon.keys()
        assert not references & child.keys()
    finally:
        harness.close()


@pytest.mark.parametrize("word", [
    "TOKEN", "KEY", "SECRET", "PASSWORD", "tOkEn", "key", "secret", "password",
    "APIKEY", "CREDENTIAL", "CREDENTIALS", "AUTHORIZATION", "BEARER", "COOKIE",
    "COOKIES", "PASSWD", "TOKENS", "KEYS", "SECRETS", "PASSWORDS", "TOKEN2", "KEY2",
])
@pytest.mark.parametrize("suffix", ["", "_FILE"])
def test_configuration_prefix_refuses_credential_words(word, suffix):
    name = "SUBFLEET_TEST_" + word + suffix
    parent = {name: "dummy", "SUBFLEET_FAKE_SCENARIO": "dummy"}
    assert set(build_environment(environ=parent)) == {"SUBFLEET_FAKE_SCENARIO"}
    assert name in build_environment(environ=parent, pass_names=[name])


def test_selection_reads_values_only_after_accepting_names():
    from collections.abc import Mapping
    from subfleet.launch_env import environment_report
    class Parent(Mapping):
        def __iter__(self):
            return iter(("SUBFLEET_FAKE_SCENARIO", "SUBFLEET_TEST_TOKEN", "OTHER"))
        def __len__(self):
            return 3
        def __getitem__(self, name):
            assert name == "SUBFLEET_FAKE_SCENARIO", "rejected value was read"
            return "dummy"
    parent = Parent()
    assert build_environment(environ=parent) == {"SUBFLEET_FAKE_SCENARIO": "dummy"}
    assert environment_report(parent) == {"extra_names": ["OTHER", "SUBFLEET_TEST_TOKEN"]}


def test_live_session_selection_reads_only_the_probe_and_accepted_values():
    """The session probe reads only its own names, even in a live session (#222 CI at
    fe5db7273: the probe's membership test read every value it looked up)."""
    from collections.abc import Mapping
    from subfleet.home_paths import PYTEST_MARKER, under_pytest
    from subfleet.launch_env import SESSION_PROBE_NAMES, TEST_SESSION_NAMES
    live = {name: os.environ[name] for name in SESSION_PROBE_NAMES if name in os.environ}
    assert PYTEST_MARKER in live and under_pytest(live)
    readable = set(live) | {"SUBFLEET_FAKE_SCENARIO"}
    class Parent(Mapping):
        def __iter__(self):
            return iter((*live, "SUBFLEET_FAKE_SCENARIO", "SUBFLEET_TEST_TOKEN", "OTHER"))
        def __len__(self):
            return len(live) + 3
        def __getitem__(self, name):
            assert name in readable, f"rejected value was read: {name}"
            return live.get(name, "dummy")
    selected = build_environment(environ=Parent())
    assert selected["SUBFLEET_FAKE_SCENARIO"] == "dummy"
    assert set(live) & TEST_SESSION_NAMES <= set(selected)
    assert not {"SUBFLEET_TEST_TOKEN", "OTHER"} & set(selected)


def test_configuration_prefix_keeps_launch_markers_explicit():
    names = {"SUBFLEET_JOB", "SUBFLEET_ATTEMPT", "SUBFLEET_SESSION_ID", "SUBFLEET_TURN_JOB",
             "SUBFLEET_ROOT", "SUBFLEET_PROBE", "SUBFLEET_PUSH_BUNDLE", "SUBFLEET_RUN_DETACH",
             "SUBFLEET_ATTACHED_OK", "SUBFLEET_LANE", "SUBFLEET_LANE_ID", "SUBFLEET_EXIT_SETTLE_S"}
    parent = dict.fromkeys(names, "dummy")
    assert build_environment(environ=parent) == {}
    assert build_environment(parent, environ={}) == parent


def test_guardian_settle_setting_requires_launch_authority():
    assert build_environment(environ={"SUBFLEET_EXIT_SETTLE_S": "0"}) == {}


@pytest.mark.parametrize("disclaim", [False, True])
def test_guardian_merge_forwards_selected_environment(monkeypatch, disclaim):
    from subfleet import daemon, procs
    selected = {"PATH": "/usr/bin:/bin", "SUBFLEET_ATTEMPT": "dummy/a1"}
    seen = []
    child = SimpleNamespace(pid=123)
    def spawn(argv, **options):
        seen.append((argv, options))
        return child
    monkeypatch.setattr(procs, "spawn_disclaimed", spawn)
    monkeypatch.setattr(daemon.subprocess, "Popen", spawn)
    assert daemon.spawn_guardian(["dummy"], disclaim=disclaim, env=selected,
                                 cwd="/dummy", pass_fds=(9,)) is child
    assert seen == [(["dummy"], {"env": selected, "cwd": "/dummy", "pass_fds": (9,)})]


@pytest.mark.parametrize("mode", ["detached", "resume", "turn", "isolated"])
def test_memory_removal_beats_exact_policy(mode, tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin",
                                       "CLAUDE_MEMORY_STORES": "/dummy/private-memory"})
    lane, launch = launch_for("claude", mode, tmp_path, monkeypatch)
    from subfleet.daemon import Daemon
    daemon = Daemon.__new__(Daemon)
    daemon._provider_environment = ProviderEnvironment({"env": {"pass": ["CLAUDE_MEMORY_STORES"]}})
    assert "CLAUDE_MEMORY_STORES" in launch.env_remove
    assert "CLAUDE_MEMORY_STORES" not in daemon._launch_environment(lane, launch)


def test_mandatory_removal_also_beats_selected_additions():
    environment = ProviderEnvironment({"env": {"pass": ["CLAUDE_MEMORY_STORES"]}})
    assert environment.build("claude", {"CLAUDE_MEMORY_STORES": "dummy"},
                             ("CLAUDE_MEMORY_STORES",), environ={}) == {}


def test_doctor_invalid_timeout_names_variable_without_value(tmp_path, monkeypatch):
    marker = "DUMMY_INVALID_TIMEOUT_222"
    monkeypatch.setenv("CODEX_GUARD_PREFLIGHT_TIMEOUT", marker)
    item = doctor.check_guard_preflight(tmp_path)
    assert item["status"] == doctor.FAIL
    rendered = doctor.render([item])
    assert "CODEX_GUARD_PREFLIGHT_TIMEOUT" in rendered
    assert marker not in rendered


@pytest.mark.parametrize("path", ["gate", "salvage", "finalization", "retention"])
def test_policy_reaches_helpers_with_mandatory_git_removals(path, tmp_path, monkeypatch):
    from subfleet import launch_env, retention_git, salvage
    from subfleet.gate import merge
    monkeypatch.setattr(os, "environ", {"GH_TOKEN": "dummy", "OTHER_TOKEN": "dummy",
                                       "GIT_DIR": "/dummy/other-repo", "GIT_TRACE": "dummy"})
    policy = {"env": {"pass": ["GH_TOKEN", "GIT_DIR", "GIT_TRACE"]}}
    seen = []
    with launch_env.helper_policy(policy):
        if path == "gate":
            merge.run_command(["fake-gh"], cwd=tmp_path, runner=lambda *args, **kwargs:
                              seen.append(kwargs["env"]))
        elif path == "retention":
            seen.append(retention_git.environment())
        else:
            monkeypatch.setattr(salvage.subprocess, "run", lambda *args, **kwargs:
                                seen.append(kwargs["env"]) or SimpleNamespace(returncode=0, stdout=b"", stderr=b""))
            if path == "salvage":
                salvage._git(tmp_path, "rev-parse", "HEAD")
            else:
                from subfleet.conversations import diff
                diff._git(tmp_path, "diff", "--binary")
    assert seen
    for env in seen:
        assert env["GH_TOKEN"] == "dummy"
        assert "OTHER_TOKEN" not in env
        if path != "gate":
            assert "GIT_DIR" not in env and "GIT_TRACE" not in env
    assert "GH_TOKEN" not in build_environment()


def test_helper_policy_is_nested_and_provider_policy_stays_explicit(monkeypatch):
    from subfleet.launch_env import helper_policy
    monkeypatch.setattr(os, "environ", {"FIRST_TOKEN": "dummy", "SECOND_TOKEN": "dummy"})
    with helper_policy({"env": {"pass": ["FIRST_TOKEN"]}}):
        assert set(build_environment()) == {"FIRST_TOKEN"}
        assert set(ProviderEnvironment({"env": {"pass": ["SECOND_TOKEN"]}}).build("claude")) == {"SECOND_TOKEN"}
        with pytest.raises(RuntimeError), helper_policy({"env": {"pass": ["SECOND_TOKEN"]}}):
            assert set(build_environment()) == {"SECOND_TOKEN"}
            raise RuntimeError("dummy")
        assert set(build_environment()) == {"FIRST_TOKEN"}
    assert build_environment() == {}


def test_worker_helper_policy_is_per_daemon_and_resets(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from subfleet.daemon import Daemon
    monkeypatch.setattr(os, "environ", {"FIRST_TOKEN": "dummy", "SECOND_TOKEN": "dummy"})
    services = []
    for name in ("FIRST_TOKEN", "SECOND_TOKEN"):
        daemon = Daemon.__new__(Daemon)
        daemon.policy = {"env": {"pass": [name]}}
        services.append(daemon)
    with ThreadPoolExecutor(max_workers=1) as pool:
        for daemon, name in zip(services, ("FIRST_TOKEN", "SECOND_TOKEN")):
            assert set(pool.submit(daemon._run_worker, build_environment).result()) == {name}
            assert pool.submit(build_environment).result() == {}


@pytest.mark.parametrize("entry", ["request", "lane", "reader"])
def test_timer_helpers_use_owning_policy_across_threads(tmp_path, monkeypatch, entry):
    from concurrent.futures import ThreadPoolExecutor
    from subfleet.launch_env import helper_policy
    from subfleet.store import Store
    from subfleet.timers import Timers
    baseline = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "user-home")}
    monkeypatch.setattr(os, "environ", {**baseline, "GH_TOKEN": "dummy", "OTHER_TOKEN": "dummy"})
    policy = {"env": {"pass": ["GH_TOKEN"]}, "models": {}, "caps": {},
              "sessions": {"mirror_interval_s": 0}, "reset_credits": {"enabled": False}}
    with Store(tmp_path / "state.sqlite3") as store:
        timer = Timers(store, tmp_path, policy)
        attribute = "_cycles" if entry == "request" else "_lanes"
        getattr(timer, attribute).shutdown(wait=True)
        pool = ThreadPoolExecutor(max_workers=1)
        setattr(timer, attribute, pool)
        seen = []
        try:
            assert pool.submit(build_environment).result(timeout=30) == baseline
            if entry == "request":
                monkeypatch.setattr(timer, "keepalive_cycle", lambda: seen.append(build_environment()))
                timer.start()
            elif entry == "lane":
                lane = SimpleNamespace(provider="claude", enabled=True, owner="v2", desktop=False)
                monkeypatch.setattr(store, "list_lanes", lambda: [lane])
                monkeypatch.setattr(timer, "_keepalive_lane", lambda lane: seen.append(build_environment()))
            with helper_policy({"env": {"pass": ["OTHER_TOKEN"]}}):
                if entry == "request":
                    assert timer.request("keepalive")["status"] == "scheduled"
                    pool.submit(lambda: None).result(timeout=30)
                elif entry == "lane":
                    timer.keepalive_cycle()
                else:
                    adapter = SimpleNamespace(probe_status=lambda lane, env:
                        seen.append(build_environment()) or {"status": "ok"})
                    timer._read_probe(adapter, SimpleNamespace(lane_id="dummy"), {})
                assert build_environment() == {**baseline, "OTHER_TOKEN": "dummy"}
            assert seen == [{**baseline, "GH_TOKEN": "dummy"}]
            assert pool.submit(build_environment).result(timeout=30) == baseline
        finally:
            timer.stop()
