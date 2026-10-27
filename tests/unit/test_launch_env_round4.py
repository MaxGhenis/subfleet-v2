"""Round 4 of #222: D-ENV1's allowlist meets D-ST1's test session (#198).

Every value here is a dummy, and failures name variables, never values. No
test reads a credential store: readers run only temporary stand-in scripts.
"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import cli, credentials, doctor, home_paths, launch_env
from subfleet.adapters import claude
from subfleet.adapters.claude import ClaudeAdapter
from subfleet.contracts import Credential
from subfleet.home_paths import PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID, SAFE_HOME_PATHS
from subfleet.launch_env import build_environment, environment_report
from subfleet.policy import PolicyError
from subfleet.provider_env import ProviderEnvironment
from tests.unit.test_doctor import stub_probes  # noqa: F401 - the fixture
from tests.unit.test_launch_env_round3 import env_lane, write_lanes, write_policy

MARKERS = (PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID)
D_ST1_NAMES = frozenset({*MARKERS, *SAFE_HOME_PATHS})
CODEX_POLICY = {"providers": {"codex": {"ca_bundle": None}}}
REAL_RUN = subprocess.run


def session_base() -> Path:
    return Path(os.environ[PYTEST_SESSION])


# --- classification -----------------------------------------------------------------


def test_every_d_st1_name_is_admitted_by_exact_name():
    """Each marker and isolated path is configuration or a test-session name, never a prefix."""
    assert D_ST1_NAMES <= launch_env.ALLOWED_NAMES | launch_env.TEST_SESSION_NAMES
    assert launch_env.TEST_SESSION_NAMES == D_ST1_NAMES - launch_env.ALLOWED_NAMES
    assert all(launch_env.valid_name(name) for name in launch_env.TEST_SESSION_NAMES)
    # Disjoint from every other class: a session name is neither launch-owned,
    # retired, a harness hook nor ambient configuration.
    others = launch_env.ALLOWED_NAMES | launch_env.LAUNCH_NAMES | launch_env.RETIRED_NAMES
    assert not launch_env.TEST_SESSION_NAMES & others
    assert not any(launch_env.harness_name(name) for name in launch_env.TEST_SESSION_NAMES)
    assert not any(launch_env.allowed_name(name) for name in launch_env.TEST_SESSION_NAMES)
    assert all(launch_env.session_name(name) for name in launch_env.TEST_SESSION_NAMES)


def test_session_names_obey_the_credential_backstop():
    """SUBFLEET_AGENT_SECRET passes only as a reviewed path; no other session name trips a word."""
    trips = {name for name in launch_env.TEST_SESSION_NAMES
             if launch_env.CREDENTIAL_PATTERN.search(name)}
    assert trips == {"SUBFLEET_AGENT_SECRET"} <= launch_env.CREDENTIAL_EXCEPTIONS
    # The removed helpers stay out: both are credential-shaped or the person's own build.
    assert not any(launch_env.inherited_name(name) for name in home_paths.SAFE_ENV_REMOVALS
                   if name not in launch_env.ALLOWED_NAMES)


def test_live_session_values_hold_no_credential(tmp_path_factory):
    """Verify the values the session sets: a flag, its directory, an identity, isolated paths.

    The session's baseline lies under its own base and each test's under
    pytest's base temporary directory (`no_desktop_store`); neither is a store.
    """
    base = session_base()
    assert os.environ[PYTEST_MARKER] == "1"
    assert (base / home_paths.PYTEST_SENTINEL).is_file()
    found = (base / home_paths.PYTEST_SENTINEL).stat()
    assert re.fullmatch(rf"{found.st_dev}:{found.st_ino}:[0-9a-f]{{64}}", os.environ[PYTEST_SESSION_ID])
    roots = (base, tmp_path_factory.getbasetemp())
    for name in SAFE_HOME_PATHS:
        value = Path(os.environ[name])
        assert value.is_absolute() and any(value.is_relative_to(root) for root in roots), name


# --- selection ----------------------------------------------------------------------


def test_session_names_reach_every_child_while_the_session_lives(tmp_path):
    """D-ST1: daemon, providers, helpers and the guardian's second selection all keep them."""
    expected = {name: os.environ[name] for name in D_ST1_NAMES}
    environment = ProviderEnvironment(CODEX_POLICY)
    children = {
        "helper": build_environment(),
        "daemon": cli.daemon_env(tmp_path / "daemon"),
        "claude": environment.build("claude", {"CLAUDE_CODE_OAUTH_TOKEN": "dummy"}),
        "codex": environment.build("codex", {"CODEX_HOME": str(tmp_path)}),
    }
    children["guardian-exec"] = build_environment(environ=children["claude"])
    for where, child in children.items():
        missing = {name for name in expected if child.get(name) != expected[name]}
        assert missing == {"SUBFLEET_HOME"} if where == "daemon" else missing == set(), where
    # The session's copy is its own: a launch's own config directory still wins.
    assert environment.build("claude", {"CLAUDE_CONFIG_DIR": str(tmp_path)})["CLAUDE_CONFIG_DIR"] == str(tmp_path)


@pytest.mark.parametrize("state", ["absent", "expired", "wrong-identity"])
def test_outside_a_live_session_production_selection_is_unchanged(tmp_path, state):
    """No live session: every session name is dropped and reported, exactly as before round 4."""
    parent = {"PATH": "/usr/bin:/bin", **{name: str(tmp_path / name) for name in launch_env.TEST_SESSION_NAMES}}
    if state == "absent":
        for name in MARKERS:
            parent.pop(name)
    else:
        parent[PYTEST_MARKER] = "1"
        parent[PYTEST_SESSION] = str(tmp_path / "expired-session")
        if state == "wrong-identity":
            parent[PYTEST_SESSION] = str(session_base())
        parent[PYTEST_SESSION_ID] = "0:0:" + "0" * 64
    assert not home_paths.under_pytest(parent)
    assert build_environment(environ=parent) == {"PATH": "/usr/bin:/bin"}
    for provider in ("claude", "codex"):
        child = ProviderEnvironment(CODEX_POLICY).build(provider, environ=parent)
        assert not set(child) & launch_env.TEST_SESSION_NAMES
    assert environment_report(parent)["extra_names"] == sorted(set(parent) - {"PATH"})


def test_claude_config_dir_stays_a_lane_addition_in_production(tmp_path):
    """D-ENV1, C-12.4: outside a session the daemon's CLAUDE_CONFIG_DIR reaches no launch."""
    parent = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "CLAUDE_CONFIG_DIR": str(tmp_path / "other")}
    child = ProviderEnvironment(CODEX_POLICY).build("claude", {"CLAUDE_CODE_OAUTH_TOKEN": "dummy"}, environ=parent)
    assert "CLAUDE_CONFIG_DIR" not in child
    passed = ProviderEnvironment({**CODEX_POLICY, "env": {"pass": ["CLAUDE_CONFIG_DIR"]}})
    assert passed.build("claude", environ=parent)["CLAUDE_CONFIG_DIR"] == str(tmp_path / "other")


# `getattr` keeps a replay against a head without the class meaningful: its
# cases then fail one by one instead of the module failing to import.
NAMES = sorted(launch_env.ALLOWED_NAMES | getattr(launch_env, "TEST_SESSION_NAMES", D_ST1_NAMES)
               | {"OTHER", "SUBFLEET_UNREAD", "GH_TOKEN", "SUBFLEET_FAKE_SCENARIO", "PYTEST_CURRENT_TEST"})


@settings(max_examples=60, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(names=st.sets(st.sampled_from(NAMES), max_size=25),
       state=st.sampled_from(["none", "bare", "live", "expired"]),
       passed=st.sets(st.sampled_from(["OTHER", "GH_TOKEN", "CLAUDE_CONFIG_DIR"]), max_size=3))
def test_bounded_property_a_session_changes_only_session_names(names, state, passed):
    """For all parents: the session state adds exactly the present session names, nothing else.

    Invariants: selection is a subset of the parent with its values; without a
    live session it is release #222's selection; with one it adds precisely the
    session names present; explicit names pass in both; reports and selection agree.
    Round 5 (review r4, P3-4): only the marker's own session selects them, so a
    bare `PYTEST_CURRENT_TEST` (the `none` state with that name drawn), which
    `under_pytest` still calls live for its other callers, selects none.
    """
    parent = {name: f"dummy-{index}" for index, name in enumerate(sorted(names))}
    for name in MARKERS:
        parent.pop(name, None)
    if state != "none":
        parent[PYTEST_MARKER] = "1"
    if state == "live":
        parent[PYTEST_SESSION] = os.environ[PYTEST_SESSION]
        parent[PYTEST_SESSION_ID] = os.environ[PYTEST_SESSION_ID]
    elif state == "expired":
        parent[PYTEST_SESSION] = os.environ[PYTEST_SESSION]
        parent[PYTEST_SESSION_ID] = "0:0:" + "0" * 64
    live = home_paths.under_pytest(parent)
    assert live == (state in ("bare", "live") or (state == "none" and "PYTEST_CURRENT_TEST" in parent))
    live = PYTEST_MARKER in parent and live
    child = build_environment(environ=parent, pass_names=passed)
    assert all(parent[name] == value for name, value in child.items())
    production = {name for name in parent if launch_env.allowed_name(name) or name in passed}
    session = {name for name in parent if launch_env.session_name(name)}
    assert set(child) == (production | session if live else production)
    default = {name for name in parent if launch_env.allowed_name(name)}
    assert set(environment_report(parent)["extra_names"]) == set(parent) - default - (session if live else set())


@pytest.mark.parametrize("home", ["", "/", "/fake/home", "/fake/home/", "~/fake-home", "relative-home"])
def test_launch_home_is_spelled_as_release_spelled_it(monkeypatch, home):
    """The projects folder's fallback is release/217's `Path.home()`, refusals included."""
    monkeypatch.setenv("HOME", home)
    released = outcome(Path.home)
    assert outcome(lambda: claude._launch_home({"HOME": home})) == released
    assert outcome(lambda: claude._launch_home({})) == released


def outcome(call):
    try:
        return ("value", call())
    except Exception as error:
        return ("exception", type(error).__name__, str(error))


# --- the keychain runner rule ---------------------------------------------------------


def reader_script(tmp_path):
    """A stand-in reader: records its environment's names and prints a dummy blob."""
    names = tmp_path / "reader-env-names.json"
    script = tmp_path / "standin-reader"
    script.write_text(f"#!{sys.executable}\nimport json, os\n"
                      f"open({str(names)!r}, 'w').write(json.dumps(sorted(os.environ)))\n"
                      "print('{\"claudeAiOauth\":{\"accessToken\":\"dummy-token\"}}')\n")
    script.chmod(0o700)
    return script, names


@pytest.mark.parametrize("runner", ["none", "native"])
@pytest.mark.parametrize("reader", ["credential", "adapter-token", "blob"])
def test_the_native_reader_gets_the_allowlisted_environment(tmp_path, monkeypatch, runner, reader):
    """D-ENV1: whether no runner or `subprocess.run` itself is supplied, the reader's
    environment is the selection; an unlisted daemon variable never reaches it."""
    script, names = reader_script(tmp_path)
    monkeypatch.setenv("SUBFLEET_TEST_OAUTH", "dummy-lane-value")
    monkeypatch.setenv("UNRELATED_API_TOKEN", "dummy-unrelated")
    supplied = None if runner == "none" else REAL_RUN
    if reader == "credential":
        monkeypatch.setenv("CLAUDE_LANE_AGENT_SECRET", str(script))
        credentials.resolve_credential(Credential("claude", "claude-quota-dummy", "keychain-token"),
                                       runner=supplied)
    elif reader == "adapter-token":
        ClaudeAdapter(runner=supplied, security_bin=str(script))._resolve_keychain_token("dummy-ref")
    else:
        assert claude._keychain_blob("dummy-ref", security_bin=str(script), runner=supplied)
    seen = set(json.loads(names.read_text()))
    assert not {"SUBFLEET_TEST_OAUTH", "UNRELATED_API_TOKEN", "CLAUDE_LANE_AGENT_SECRET"} & seen
    assert seen <= set(build_environment()) | {"PWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING"}
    assert "PATH" in seen and PYTEST_MARKER in seen


@pytest.mark.parametrize("reader", ["credential", "adapter-token", "blob"])
def test_a_supplied_stand_in_runner_builds_its_own_environment(monkeypatch, reader):
    """D-ST1's runners (#198) keep their call shape: no `env` is imposed on them."""
    calls = []

    def standin(argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, '{"claudeAiOauth":{"accessToken":"dummy"}}', "")

    monkeypatch.delenv("CLAUDE_LANE_AGENT_SECRET", raising=False)
    if reader == "credential":
        credentials.resolve_credential(Credential("claude", "dummy-ref", "keychain-token"), runner=standin)
    elif reader == "adapter-token":
        ClaudeAdapter(runner=standin)._resolve_keychain_token("dummy-ref")
    else:
        claude._keychain_blob("dummy-ref", runner=standin)
    assert len(calls) == 1 and "env" not in calls[0]


def test_native_runner_is_none_or_subprocess_run_itself():
    assert credentials.native_runner(None) and credentials.native_runner(REAL_RUN)
    assert not credentials.native_runner(lambda *args, **kwargs: REAL_RUN(*args, **kwargs))
    assert ClaudeAdapter(runner=REAL_RUN)._native_runner and ClaudeAdapter()._native_runner
    assert not ClaudeAdapter(runner=lambda *args, **kwargs: None)._native_runner


# --- review r3 P3-1: a lane variable every launch inherits -------------------------------


INHERITED_REFERENCES = ["HOME", "LANG", "SUBFLEET_NOTIFY_MODE", "SUBFLEET_FAKE_LANE_NINE",
                        "SUBFLEET_HOST_HOME", "SUBFLEET_PYTEST_SESSION"]


@pytest.mark.parametrize("reference", INHERITED_REFERENCES)
def test_daemon_start_refuses_a_lane_variable_every_launch_inherits(reference, tmp_path, monkeypatch, capsys):
    """Refused before any spawn, naming the variable and never its value (review r3, P3-1)."""
    write_lanes(tmp_path, env_lane("claude-9", reference))
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", "SUBFLEET_HOME": str(tmp_path),
                                       reference: "dummy-nine"})
    with pytest.raises(PolicyError) as error:
        cli.daemon_env(tmp_path)
    assert error.value.key == "credential_ref" and reference in str(error.value)
    monkeypatch.setattr(cli, "_daemon_alive", lambda *args, **kwargs: False)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("refused roster spawned"))
    assert cli.cmd_daemon_start(SimpleNamespace(state_root=str(tmp_path))) == 2
    err = capsys.readouterr().err
    assert reference in err and "dummy-nine" not in err
    # The resolve guard stays the second line of defence for a lane added later.
    from subfleet.adapters.base import AdapterError
    with pytest.raises(AdapterError) as refused:
        credentials.resolve_credential(Credential("claude", reference, "env"))
    assert refused.value.reason == "credential-env-inherited"


def test_lane_variables_off_the_allowlist_still_start(tmp_path, monkeypatch):
    write_lanes(tmp_path, env_lane("claude-1", "SUBFLEET_TEST_OAUTH"))
    write_policy(tmp_path, ["GIT_CONFIG_GLOBAL"])
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", "SUBFLEET_TEST_OAUTH": "dummy"})
    assert cli.daemon_env(tmp_path)["SUBFLEET_TEST_OAUTH"] == "dummy"


# --- review r3 P3-2: doctor lists lane variables on their own row ---------------------


def test_report_lists_lane_credentials_apart():
    parent = {"PATH": "dummy", "SUBFLEET_TEST_OAUTH": "dummy", "GH_TOKEN": "dummy"}
    assert environment_report(parent) == {"extra_names": ["GH_TOKEN", "SUBFLEET_TEST_OAUTH"]}
    assert environment_report(parent, references=["SUBFLEET_TEST_OAUTH", "ABSENT_LANE_VARIABLE"]) == {
        "extra_names": ["GH_TOKEN"], "lane_credentials": ["SUBFLEET_TEST_OAUTH"]}


@pytest.mark.parametrize("extra", [[], ["GH_TOKEN"]])
def test_doctor_lists_lane_variables_without_warning(tmp_path, monkeypatch, extra):
    status = {"environment": {"extra_names": extra, "lane_credentials": ["SUBFLEET_TEST_OAUTH"]}}
    monkeypatch.setattr(doctor.Client, "call", lambda *args, **kwargs: status)
    allowlist = doctor.check_daemon_environment(tmp_path)
    lanes = doctor.check_daemon_lane_credentials(tmp_path)
    assert allowlist["status"] == (doctor.WARN if extra else doctor.PASS)
    assert "SUBFLEET_TEST_OAUTH" not in allowlist["detail"]
    assert lanes["status"] == doctor.PASS and "SUBFLEET_TEST_OAUTH" in lanes["detail"]
    rendered = doctor.render([allowlist, lanes])
    assert "dummy" not in rendered
    assert sum("SUBFLEET_TEST_OAUTH" in line for line in rendered.splitlines()) == 1


@pytest.mark.parametrize("status", [None, {}, {"environment": {"extra_names": []}},
                                    {"environment": {"extra_names": [], "lane_credentials": [7]}}])
def test_doctor_lane_row_is_unknown_for_an_older_or_malformed_daemon(tmp_path, monkeypatch, status):
    monkeypatch.setattr(doctor.Client, "call", lambda *args, **kwargs: status)
    assert doctor.check_daemon_lane_credentials(tmp_path)["status"] == doctor.UNKNOWN


def test_doctor_table_has_the_lane_row(daemon, root, stub_probes):
    """The row is in the offline table, beside the allowlist row (C-17.8)."""
    daemon({"daemon.status": lambda request: {"environment": {
        "extra_names": [], "lane_credentials": ["SUBFLEET_TEST_OAUTH"]}}})
    rows = {item["check"]: item for item in doctor.checks(root)}
    assert rows["daemon environment allowlist"]["status"] == doctor.PASS
    lanes = rows["daemon lane credential variables"]
    assert lanes["status"] == doctor.PASS and "SUBFLEET_TEST_OAUTH" in lanes["detail"]
