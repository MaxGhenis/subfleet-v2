"""Round 3 (review r2 of #222): exact Subfleet names and per-lane credentials (D-ENV1).

Every value here is a dummy, and failures name variables, never values. The
package scans read source text only; they import nothing and run nothing.
"""

import ast
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import cli, doctor, launch_env
from subfleet.adapters.base import AdapterError
from subfleet.contracts import Credential, Lane, LaneOwner
from subfleet.credentials import resolve_credential
from subfleet.launch_env import build_environment, environment_report
from subfleet.policy import DEFAULT_POLICY_PATH, PolicyError
from subfleet.provider_env import ProviderEnvironment
from subfleet.store import Store

PACKAGE = Path(launch_env.__file__).resolve().parent
REPO = PACKAGE.parent
TESTS = REPO / "tests"
CODEX_POLICY = {"providers": {"codex": {"ca_bundle": None}}}

# Review r2, P2: each reached providers through round 2's `SUBFLEET_` prefix
# rule with no policy, and doctor listed none of them.
REVIEW_R2_ADMITTED = (
    "SUBFLEET_TEST_OAUTH", "SUBFLEET_OAUTH", "SUBFLEET_AUTH", "SUBFLEET_AUTH_HEADER",
    "SUBFLEET_GHTOKEN", "SUBFLEET_APITOKEN", "SUBFLEET_ACCESSTOKEN", "SUBFLEET_OPENAIKEY",
    "SUBFLEET_SECRETKEY", "SUBFLEET_PRIVKEY", "SUBFLEET_GH_PAT", "SUBFLEET_CREDS",
    "SUBFLEET_PASSPHRASE", "SUBFLEET_JWT", "SUBFLEET_WEBHOOK_URL",
)
# The hub's backstop words, matched as substrings in any case.
WORDS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "PASSPHRASE", "PASS", "PWD",
         "CREDENTIAL", "CRED", "AUTH", "OAUTH", "BEARER", "COOKIE", "JWT", "PAT",
         "PRIVKEY", "WEBHOOK")
# Reviewed: admitted names a word matches by accident, each holding a path.
REVIEWED_EXCEPTIONS = {"PATH", "SSH_AUTH_SOCK", "SUBFLEET_FAKE_DIAGNOSTICS_PATH",
                       "SUBFLEET_FAKE_RELEASE_PATH"}
SUBFLEET_NAME = re.compile(r"SUBFLEET_[A-Z0-9_]*[A-Z0-9]", re.ASCII)
SHELL_READ = re.compile(r"\$\{?(SUBFLEET_[A-Z0-9_]*[A-Z0-9])", re.ASCII)


def spelled_names(paths, *, shell=()):
    """{name: files} for each whole `SUBFLEET_*` string or keyword, and prefix pieces.

    A piece is a string constant that begins a name without completing one
    (`"SUBFLEET_"`, an f-string's head): the only way to build one at run time.
    """
    names, pieces = {}, {}
    for path in paths:
        where = path.relative_to(REPO).as_posix()
        for node in ast.walk(ast.parse(path.read_text(), where)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
            elif isinstance(node, ast.keyword) and node.arg:
                value = node.arg
            else:
                continue
            if SUBFLEET_NAME.fullmatch(value):
                names.setdefault(value, set()).add(where)
            elif value.startswith("SUBFLEET_") and re.fullmatch(r"SUBFLEET_[A-Z0-9_]*", value):
                pieces.setdefault(value, set()).add(where)
    for path in shell:
        for match in SHELL_READ.finditer(path.read_text(errors="replace")):
            names.setdefault(match.group(1), set()).add(path.relative_to(REPO).as_posix())
    return names, pieces


def production():
    """Package source and repository executables; launch_env's own lists excluded."""
    sources = sorted(path for path in PACKAGE.rglob("*.py") if path != PACKAGE / "launch_env.py")
    shell = sorted(path for path in (REPO / "bin").iterdir() if path.is_file())
    return spelled_names(sources, shell=shell)


def harness_used():
    """Harness names the fake providers, fake daemons and e2e/live harnesses spell."""
    paths = [TESTS / "fake_adapter.py"]
    for directory in ("bin", "e2e", "fake", "live"):
        paths += [path for path in (TESTS / directory).rglob("*") if path.is_file()
                  and path.suffix in ("", ".py") and "__pycache__" not in path.parts]
    return {name for path in paths for name in SUBFLEET_NAME.findall(path.read_text(errors="replace"))
            if name.startswith(launch_env.HARNESS_PREFIXES) or name in launch_env.HARNESS_NAMES}


# --- review r2 P2: the prefix rule admitted credentials -----------------------------


@pytest.mark.parametrize("name", REVIEW_R2_ADMITTED)
def test_review_r2_admitted_names_reach_no_child_and_doctor_names_them(name, tmp_path, monkeypatch):
    """All fifteen of the review's names: dropped at every boundary, named by doctor."""
    parent = {"PATH": "/usr/bin:/bin", "SUBFLEET_FAKE_SCENARIO": "dummy", name: "dummy-r2-value"}
    monkeypatch.setattr(os, "environ", parent)
    assert set(build_environment()) == {"PATH", "SUBFLEET_FAKE_SCENARIO"}
    for provider in ("codex", "claude"):
        assert name not in ProviderEnvironment(CODEX_POLICY).build(provider)
    started = cli.daemon_env(tmp_path)
    assert name not in started
    assert name not in ProviderEnvironment(CODEX_POLICY).build("claude", environ=started)
    report = environment_report()
    assert report == {"extra_names": [name]}
    monkeypatch.setattr(doctor.Client, "call", lambda *args, **kwargs: {"environment": report})
    item = doctor.check_daemon_environment(tmp_path)
    rendered = doctor.render([item])
    assert item["status"] == doctor.WARN and name in rendered
    assert "dummy-r2-value" not in rendered + json.dumps(report)


def test_review_r2_real_case_env_kind_lane_credential_reaches_no_other_launch(tmp_path, monkeypatch):
    """The repo's own env-kind example, set in the shell with no policy (review r2)."""
    (tmp_path / "lanes.json").write_text(json.dumps([{
        "lane_id": "claude-1", "provider": "claude", "account_key": "claude:one",
        "credential_kind": "env", "credential_ref": "SUBFLEET_TEST_OAUTH", "owner": "v2"}]))
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", "SUBFLEET_TEST_OAUTH": "dummy-oauth"})
    started = cli.daemon_env(tmp_path)
    # The daemon holds it, so the lane resolves; no launch inherits it.
    assert started.get("SUBFLEET_TEST_OAUTH") == "dummy-oauth", "the daemon holds the lane's credential"
    for provider in ("codex", "claude"):
        child = ProviderEnvironment(CODEX_POLICY).build(provider, environ=started)
        assert "SUBFLEET_TEST_OAUTH" not in set(child)
        assert not any(value == "dummy-oauth" for value in child.values())
    assert "SUBFLEET_TEST_OAUTH" in environment_report(started)["extra_names"]


def test_every_subfleet_name_the_package_reads_is_listed():
    """Generated, not remembered: each spelled name is configuration, a marker or retired."""
    found, _ = production()
    listed = {name for name in launch_env.CONFIG_NAMES if name.startswith("SUBFLEET_")}
    classified = listed | launch_env.LAUNCH_NAMES | launch_env.RETIRED_NAMES
    missing = {name: sorted(found[name]) for name in set(found) - classified}
    assert missing == {}, "add each to launch_env.CONFIG_NAMES, LAUNCH_NAMES or RETIRED_NAMES"
    assert (listed | launch_env.RETIRED_NAMES) - set(found) == set(), "a listed name nothing reads any more"
    # A marker may be named only in a guard's command text (`SUBFLEET_ATTACHED_OK=1`).
    text = "".join(path.read_text() for path in PACKAGE.rglob("*.py") if path != PACKAGE / "launch_env.py")
    assert [name for name in launch_env.LAUNCH_NAMES if name not in text] == []
    assert not (listed & launch_env.LAUNCH_NAMES or listed & launch_env.RETIRED_NAMES
                or launch_env.LAUNCH_NAMES & launch_env.RETIRED_NAMES)


def test_only_compat_builds_a_subfleet_name_at_run_time():
    """v1's CARPOOL_* aliasing is the one constructed name; no CARPOOL_* name is admitted."""
    _, pieces = production()
    assert pieces == {"SUBFLEET_": {"subfleet/compat.py"}}
    assert not any(launch_env.allowed_name(name) for name in ("CARPOOL_HOME", "CARPOOL_TICKLE"))


def test_configuration_allowlist_is_exact_names_only():
    assert not any(name.endswith("_") for name in launch_env.ALLOWED_NAMES)
    assert all(launch_env.valid_name(name) for name in launch_env.ALLOWED_NAMES)
    assert launch_env.CONFIG_NAMES <= launch_env.ALLOWED_NAMES
    unread = "SUBFLEET_UNREAD_SETTING"
    assert not launch_env.allowed_name(unread)
    assert environment_report({unread: "dummy"}) == {"extra_names": [unread]}


def test_retired_names_are_dropped_and_reported():
    parent = dict.fromkeys(launch_env.RETIRED_NAMES, "1")
    assert build_environment(environ=parent) == {}
    assert environment_report(parent) == {"extra_names": sorted(launch_env.RETIRED_NAMES)}


def test_no_production_code_reads_a_harness_name():
    found, pieces = production()
    harness = {name: sorted(files) for name, files in (*found.items(), *pieces.items())
               if name.startswith(launch_env.HARNESS_PREFIXES) or name in launch_env.HARNESS_NAMES}
    assert harness == {}
    assert launch_env.HARNESS_PREFIXES == ("SUBFLEET_FAKE_", "SUBFLEET_E2E_")
    assert launch_env.HARNESS_NAMES == {"SUBFLEET_LIVE"}


def test_fakes_and_harnesses_need_only_admitted_names():
    """The fake providers and the e2e harness keep working: their hooks are admitted."""
    used = harness_used()
    assert {"SUBFLEET_FAKE_SCENARIO", "SUBFLEET_E2E_HOLD_AT", "SUBFLEET_LIVE"} <= used
    assert [name for name in used if not launch_env.allowed_name(name)] == []


@pytest.mark.parametrize("name,admitted", [
    ("SUBFLEET_LIVE", True), ("SUBFLEET_LIVE_STATE_ROOT", False), ("SUBFLEET_LIVEX", False),
    ("SUBFLEET_FAKE_SCENARIO", True), ("SUBFLEET_E2E_HOLD_AT", True),
    ("SUBFLEET_FAKE_", False), ("subfleet_fake_scenario", False), ("SUBFLEET_FAKE_X.Y", False),
    ("SUBFLEET_FAKE_TOKEN", False), ("SUBFLEET_E2E_AUTH_HEADER", False), ("SUBFLEET_FAKE_PATCH", False),
    ("SUBFLEET_TEST_REPORT", False), ("SUBFLEET_TEST_DAEMON_LOG", False),
    ("SUBFLEET_HOME", True), ("SUBFLEET_NO_AUTOPICK", True), ("SUBFLEET_DAEMON_BIN", True),
])
def test_harness_prefixes_and_exact_names(name, admitted):
    assert launch_env.allowed_name(name) is admitted
    assert (name in build_environment(environ={name: "dummy"})) is admitted


# --- the substring backstop ---------------------------------------------------------


def test_backstop_words_are_the_reviewed_list():
    assert tuple(launch_env.CREDENTIAL_WORDS) == WORDS


def test_backstop_exceptions_are_exact_reviewed_and_necessary():
    exceptions = launch_env.CREDENTIAL_EXCEPTIONS
    assert exceptions == REVIEWED_EXCEPTIONS
    trips = launch_env.CREDENTIAL_PATTERN.search
    # Each exception is admitted otherwise and does trip a word: none is dead.
    assert all(trips(name) for name in exceptions)
    assert all(name in launch_env.ALLOWED_NAMES or name.startswith(launch_env.HARNESS_PREFIXES)
               for name in exceptions)
    # Every admitted name that trips a word was reviewed into the set.
    assert {name for name in launch_env.ALLOWED_NAMES | harness_used() if trips(name)} <= exceptions


@pytest.mark.parametrize("word", WORDS)
def test_an_allowlisted_name_holding_a_word_is_refused_until_reviewed(word, monkeypatch):
    name = "SUBFLEET_CONFIG_" + word.lower() + "_FILE"
    monkeypatch.setattr(launch_env, "ALLOWED_NAMES", launch_env.ALLOWED_NAMES | {name})
    assert not launch_env.allowed_name(name)
    assert name in environment_report({name: "dummy"})["extra_names"]
    monkeypatch.setattr(launch_env, "CREDENTIAL_EXCEPTIONS", launch_env.CREDENTIAL_EXCEPTIONS | {name})
    assert launch_env.allowed_name(name)


AFFIXES = st.from_regex(r"[A-Za-z0-9_]{0,8}", fullmatch=True)


@settings(max_examples=60, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(base=st.sampled_from(["SUBFLEET_FAKE_", "SUBFLEET_E2E_", "SUBFLEET_LIVE", "SUBFLEET_", ""]),
       head=AFFIXES, word=st.sampled_from(WORDS), tail=AFFIXES,
       case=st.sampled_from([str.upper, str.lower, str.title, str.swapcase]))
def test_bounded_property_no_admitted_name_holds_a_credential_word(base, head, word, tail, case):
    """For all names: a word anywhere, in any case, refuses it unless it is a reviewed exception."""
    name = base + head + case(word) + tail
    if name in launch_env.CREDENTIAL_EXCEPTIONS:
        return
    assert not launch_env.allowed_name(name)
    assert name not in build_environment(environ={name: "dummy"})
    # Exact policy authority is still exact: the same name passes when named.
    if launch_env.valid_name(name):
        assert name in build_environment(environ={name: "dummy"}, pass_names=[name])


# --- review r2 P3: env-kind lane credentials crossed lanes --------------------------


def env_lane(lane_id, ref):
    return Lane(lane_id, "claude", "claude:" + lane_id, Credential("claude", ref, "env"), None, LaneOwner.V2, False)


def write_lanes(root, *lanes):
    (root / "lanes.json").write_text(json.dumps([
        {"lane_id": lane.lane_id, "provider": lane.provider, "account_key": lane.account_key,
         "credential_kind": lane.credential.kind, "credential_ref": lane.credential.ref, "owner": "v2"}
        for lane in lanes]))


def write_policy(root, passed):
    policy = json.loads(DEFAULT_POLICY_PATH.read_bytes())
    policy["env"] = {"pass": list(passed)}
    (root / "policy.json").write_text(json.dumps(policy))


def test_daemon_start_holds_lane_credentials_from_both_rosters_without_policy(tmp_path, monkeypatch):
    """The daemon alone holds each env-kind reference: the seed roster's and the store's."""
    write_lanes(tmp_path, env_lane("claude-1", "E2E_LANE_ONE_TOKEN"))
    with Store(tmp_path / "state.sqlite3") as store:
        store.put_lane(env_lane("claude-2", "E2E_LANE_TWO_TOKEN"))
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", "E2E_LANE_ONE_TOKEN": "dummy-one",
                                       "E2E_LANE_TWO_TOKEN": "dummy-two", "UNRELATED_TOKEN": "dummy"})
    started = cli.daemon_env(tmp_path)
    assert set(started) == {"PATH", "SUBFLEET_HOME", "E2E_LANE_ONE_TOKEN", "E2E_LANE_TWO_TOKEN"}
    assert started.get("E2E_LANE_TWO_TOKEN") == "dummy-two", "the store's lane"


@pytest.mark.parametrize("roster", ["seed", "store"])
def test_daemon_start_refuses_env_pass_naming_a_lane_credential(roster, tmp_path, monkeypatch, capsys):
    """Policy validation refuses it before any spawn; the value is never printed."""
    lane = env_lane("claude-2", "E2E_LANE_TWO_TOKEN")
    if roster == "seed":
        write_lanes(tmp_path, lane)
    else:
        with Store(tmp_path / "state.sqlite3") as store:
            store.put_lane(lane)
    write_policy(tmp_path, ["GIT_CONFIG_GLOBAL", "E2E_LANE_TWO_TOKEN"])
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", "SUBFLEET_HOME": str(tmp_path),
                                       "E2E_LANE_TWO_TOKEN": "dummy-two"})
    with pytest.raises(PolicyError) as error:
        cli.daemon_env(tmp_path)
    assert error.value.key == "env.pass[1]"
    monkeypatch.setattr(cli, "_daemon_alive", lambda *args: False)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("refused policy spawned"))
    assert cli.cmd_daemon_start(SimpleNamespace(state_root=str(tmp_path))) == 2
    err = capsys.readouterr().err
    assert "env.pass[1]" in err and "dummy-two" not in err


def test_env_pass_for_noncredential_names_still_starts(tmp_path, monkeypatch):
    write_lanes(tmp_path, env_lane("claude-1", "E2E_LANE_ONE_TOKEN"))
    write_policy(tmp_path, ["GIT_CONFIG_GLOBAL"])
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null"})
    assert cli.daemon_env(tmp_path)["GIT_CONFIG_GLOBAL"] == "/dev/null"


@pytest.mark.parametrize("passed", [False, True])
def test_provider_environment_withholds_every_lane_reference(passed):
    """Beneath validation too: neither the parent nor a policy name carries a reference."""
    references = ("E2E_LANE_ONE_TOKEN", "E2E_LANE_TWO_TOKEN")
    parent = {"PATH": "/usr/bin:/bin", "E2E_LANE_ONE_TOKEN": "dummy-one", "E2E_LANE_TWO_TOKEN": "dummy-two"}
    policy = {**CODEX_POLICY, "env": {"pass": list(references) if passed else []}}
    environment = ProviderEnvironment(policy, references=lambda: references)
    for provider, own in (("claude", {"CLAUDE_CODE_OAUTH_TOKEN": "dummy-one"}), ("codex", {"CODEX_HOME": "/fake"})):
        additions = environment.additions(provider, own, environ=parent)
        child = environment.build(provider, own, environ=parent)
        for env in (additions, child):
            assert not set(references) & set(env)
            assert not any(value == "dummy-two" for value in env.values())
        assert set(child) == {"PATH", *own} and all(child[name] == own[name] for name in own)


def test_an_allowlisted_reference_cannot_be_a_lane_credential(monkeypatch):
    """Every launch inherits an allowlisted name, so it can never be one lane's."""
    monkeypatch.setattr(os, "environ", {"SUBFLEET_FAKE_LANE_ONE": "dummy-one", "E2E_LANE_ONE_TOKEN": "dummy-one"})
    with pytest.raises(AdapterError) as error:
        resolve_credential(Credential("claude", "SUBFLEET_FAKE_LANE_ONE", "env"))
    assert error.value.reason == "credential-env" and "dummy-one" not in str(error.value) + error.value.fix
    assert resolve_credential(Credential("claude", "E2E_LANE_ONE_TOKEN", "env")) == {
        "CLAUDE_CODE_OAUTH_TOKEN": "dummy-one"}


# --- review r2 P3: the stale precedence comment ---------------------------------------


def test_launch_removal_comment_says_removals_win_and_names_its_test():
    """Editing the code to match a stale comment would reopen round 1's memory finding."""
    lines = (PACKAGE / "contracts.py").read_text().splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip().startswith("env_remove:"))
    end = next(index for index in range(start + 1, len(lines))
               if not lines[index].strip().startswith("#") and ":" in lines[index].split("#")[0])
    comment = " ".join(line.split("#", 1)[1].strip() for line in lines[start:end] if "#" in line)
    assert "precedence" not in comment
    assert "applied last" in comment
    test = "test_mandatory_removal_also_beats_selected_additions"
    assert test in comment
    assert f"def {test}(" in (TESTS / "unit" / "test_launch_env_round2.py").read_text()
