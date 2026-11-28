"""Round 5 of #222 (review r4): stored lanes, named refusals and session selection.

Every value here is a dummy, and failures name variables, never values. No test
reads a credential store, the live store or a live daemon. The daemon-side
regressions for review r4's P2-1 are in tests/fake/test_launch_env_daemon.py.
"""

from collections import ChainMap
from collections.abc import Mapping
import os
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

from hypothesis import HealthCheck, given, settings, strategies as st
import pytest

from subfleet import cli, home_paths, launch_env
from subfleet.home_paths import PYTEST_MARKER, PYTEST_SENTINEL, PYTEST_SESSION, PYTEST_SESSION_ID
from subfleet.launch_env import build_environment, environment_report, guardian_pass_args
from subfleet.policy import PolicyError
from subfleet.provider_env import ProviderEnvironment
from subfleet.store import Store
from tests.unit.test_launch_env_round3 import env_lane, write_lanes, write_policy

CODEX_POLICY = {"providers": {"codex": {"ca_bundle": None}}}
MARKERS = (PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID)
SESSION_NAMES = frozenset(getattr(launch_env, "TEST_SESSION_NAMES", ()))
# The value pytest gives PYTEST_CURRENT_TEST, copied or not.
CURRENT_TEST = "tests/unit/test_dummy.py::test_dummy (call)"


# --- sessions in each state the review tabled ------------------------------------------


def make_session(base: Path, state: str) -> dict[str, str]:
    """A session folder and identity in one state; only the probe's own names.

    `recreated` writes the same token under a new inode: the replacement exists
    beside the original before it takes its name, so the two inodes differ.
    """
    folder = base / f"session-{state}"
    folder.mkdir()
    if state == "empty":
        return {PYTEST_SESSION: ""}
    sentinel = folder / PYTEST_SENTINEL
    token = "ab" * 32
    sentinel.write_text(token, encoding="ascii")
    found = sentinel.stat()
    identity = f"{found.st_dev}:{found.st_ino}:{token}"
    if state == "deleted":
        sentinel.unlink()
    elif state == "recreated":
        replacement = folder / "replacement"
        replacement.write_text(token, encoding="ascii")
        os.replace(replacement, sentinel)
    elif state == "no-identity":
        return {PYTEST_SESSION: str(folder)}
    elif state == "foreign":
        identity = "0:0:" + "0" * 64
    return {PYTEST_SESSION: str(folder), PYTEST_SESSION_ID: identity}


# Review r4's table of parent shapes: (marker, session state, PYTEST_CURRENT_TEST,
# whether `under_pytest` calls it live, whether session names are selected).
SHAPES = {
    "marker-live-sentinel": ("1", "live", None, True, True),
    "marker-deleted-sentinel": ("1", "deleted", None, False, False),
    "marker-recreated-sentinel": ("1", "recreated", None, False, False),
    "marker-session-without-identity": ("1", "no-identity", None, False, False),
    "bare-marker-1": ("1", None, None, True, True),
    "bare-marker-0": ("0", None, None, True, True),
    # P3-4: live for `under_pytest`'s other callers, but no marker selects nothing.
    "bare-pytest-current-test": (None, None, CURRENT_TEST, True, False),
    "marker-expired-session-copied-current-test": ("1", "deleted", CURRENT_TEST, False, False),
    # P3-4: round 4 selected the 14 session names present here.
    "expired-session-copied-current-test-no-marker": (None, "deleted", CURRENT_TEST, True, False),
    "none-of-these": (None, None, None, False, False),
}


def shaped_parent(base: Path, shape: str) -> dict[str, str]:
    """Every session name present, as conftest-built copies carry them, in one shape."""
    marker, state, current, _, _ = SHAPES[shape]
    parent = {"PATH": "/usr/bin:/bin", "OTHER_VARIABLE": "dummy-other",
              **{name: f"/dummy/{name.lower()}" for name in SESSION_NAMES if name not in MARKERS}}
    if marker is not None:
        parent[PYTEST_MARKER] = marker
    if state is not None:
        parent.update(make_session(base, state))
    if current is not None:
        parent["PYTEST_CURRENT_TEST"] = current
    return parent


def children(parent: Mapping[str, str], tmp_path: Path) -> dict[str, dict[str, str]]:
    """Each consumer's selection from one parent: helpers, both providers, the guardian."""
    environment = ProviderEnvironment(CODEX_POLICY)
    found = {
        "helper": build_environment(environ=parent),
        "claude": environment.build("claude", {"CLAUDE_CODE_OAUTH_TOKEN": "dummy-own"}, environ=parent),
        "codex": environment.build("codex", {"CODEX_HOME": str(tmp_path)}, environ=parent),
    }
    for provider in ("claude", "codex"):
        child = found[provider]
        # The guardian is authorized the selected names by name and selects again
        # for its provider exec; a bare reselection follows the child's own probe.
        found[f"guardian-{provider}"] = build_environment(environ=child, pass_names=guardian_pass_args(child)[1::2])
        found[f"reselection-{provider}"] = build_environment(environ=child)
    return found


# --- P3-4: session names only with the marker ------------------------------------------


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_session_names_need_the_marker_and_a_live_session(tmp_path, shape):
    """Review r4, P3-4: each shape of the review's table, through every consumer.

    A bare `PYTEST_CURRENT_TEST` selects no session name, whatever else is copied;
    the marker with a live session (or a bare marker) selects every one present.
    """
    parent = shaped_parent(tmp_path, shape)
    selected = SHAPES[shape][4]
    present = set(parent) & SESSION_NAMES
    assert present >= SESSION_NAMES - set(MARKERS)
    for where, child in children(parent, tmp_path).items():
        names = set(child) & SESSION_NAMES
        assert names == (present if selected else set()), where
        assert all(child[name] == parent[name] for name in names), where
        assert "PYTEST_CURRENT_TEST" not in child, where
    extra = set(environment_report(parent)["extra_names"])
    assert present & extra == (set() if selected else present)


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_under_pytest_keeps_its_meaning_for_its_other_callers(tmp_path, shape):
    """Only selection keys on the marker. `under_pytest` still calls a bare
    `PYTEST_CURRENT_TEST` a test process, which refuses more there (review r4's table)."""
    assert home_paths.under_pytest(shaped_parent(tmp_path, shape)) is SHAPES[shape][3]


def test_a_bare_pytest_current_test_still_refuses_a_personal_fallback(monkeypatch):
    """The home-path refusal is one of `under_pytest`'s other callers."""
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", "PYTEST_CURRENT_TEST": CURRENT_TEST})
    with pytest.raises(RuntimeError, match="SUBFLEET_SESSION_STORE"):
        home_paths.home_path("SUBFLEET_SESSION_STORE", lambda: pytest.fail("the personal default was read"))


class StrictMapping(Mapping):
    """A read-only parent that is not a dict, as a reviewer's differential used."""

    def __init__(self, values):
        self._values = dict(values)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __getitem__(self, name):
        return self._values[name]


MAPPINGS = {"dict": dict, "proxy": MappingProxyType, "chain": lambda values: ChainMap({}, dict(values)),
            "mapping": StrictMapping}
OTHER_NAMES = sorted(launch_env.ALLOWED_NAMES | {"OTHER_VARIABLE", "GH_TOKEN", "SUBFLEET_FAKE_SCENARIO",
                                                 "SUBFLEET_UNREAD"})


@pytest.fixture(scope="module")
def sessions(tmp_path_factory):
    base = tmp_path_factory.mktemp("round5-sessions")
    return {state: make_session(base, state)
            for state in ("live", "deleted", "recreated", "no-identity", "foreign", "empty")}


@settings(max_examples=120, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(marker=st.sampled_from([None, "1", "0", ""]),
       session=st.sampled_from([None, "live", "deleted", "recreated", "no-identity", "foreign", "empty"]),
       current=st.sampled_from([None, CURRENT_TEST, ""]),
       session_names=st.sets(st.sampled_from(sorted(SESSION_NAMES - set(MARKERS))), max_size=15),
       others=st.sets(st.sampled_from(OTHER_NAMES), max_size=10),
       kind=st.sampled_from(sorted(MAPPINGS)))
def test_bounded_property_session_names_iff_marker_and_live_session(sessions, marker, session, current,
                                                                     session_names, others, kind):
    """Invariant (review r4, P3-4): across the parent-shape table, session names
    are selected iff the marker is present and its session lives (a bare marker
    has no session to expire), for every mapping type and every consumer."""
    values = {name: f"dummy-{index}" for index, name in enumerate(sorted(others | session_names))}
    if marker is not None:
        values[PYTEST_MARKER] = marker
    if session is not None:
        values.update(sessions[session])
    if current is not None:
        values["PYTEST_CURRENT_TEST"] = current
    parent = MAPPINGS[kind](values)
    live = marker is not None and session in (None, "live")
    present = set(values) & SESSION_NAMES
    selected = build_environment(environ=parent)
    assert set(selected) & SESSION_NAMES == (present if live else set())
    assert selected == build_environment(environ=dict(values)), "the mapping type changes nothing"
    production = {name for name in values if launch_env.allowed_name(name)}
    assert set(selected) == production | (present if live else set())
    environment = ProviderEnvironment(CODEX_POLICY)
    for provider, own in (("claude", {"CLAUDE_CODE_OAUTH_TOKEN": "dummy-own"}), ("codex", {"CODEX_HOME": "/fake"})):
        child = environment.build(provider, own, environ=parent)
        assert set(child) & SESSION_NAMES == (present if live else set()), provider
    report = set(environment_report(parent)["extra_names"])
    assert report == set(values) - production - (present if live else set())


# --- P3-2: a name that disappears between iterating and reading ------------------------


class Vanishing(Mapping):
    """A parent that yields names and has lost `vanished` by the time each is read.

    Deterministically what `os.environ` does when another thread pops a name
    between `__iter__`'s copy of its keys and the read (review r4, P3-2: pytest
    sets and pops PYTEST_CURRENT_TEST between tests). Reads are recorded.
    """

    def __init__(self, values, vanished):
        self.values, self.vanished, self.reads = dict(values), tuple(vanished), []

    def __iter__(self):
        return iter((*self.values, *self.vanished))

    def __len__(self):
        return len(self.values) + len(self.vanished)

    def __getitem__(self, name):
        self.reads.append(name)
        if name in self.vanished:
            raise KeyError(name)
        return self.values[name]


def live_probe() -> dict[str, str]:
    probe = {name: os.environ[name] for name in MARKERS if name in os.environ}
    assert PYTEST_MARKER in probe and home_paths.under_pytest(probe), "this test runs in a live session"
    return probe


@pytest.mark.parametrize("vanished", [("PYTEST_CURRENT_TEST",), (PYTEST_SESSION_ID,), ("HOME",),
                                      ("PYTEST_CURRENT_TEST", "HOME", "SUBFLEET_HOST_HOME")])
def test_a_name_popped_between_iterating_and_reading_is_skipped(tmp_path, vanished):
    """Selection skips the name as if it were absent, and still reads only the
    probe's names and accepted names (`bd3cb8095`'s strictness)."""
    values = {**live_probe(), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"),
              "SUBFLEET_FAKE_SCENARIO": "dummy", "SUBFLEET_TEST_TOKEN": "dummy-token",
              **{name: str(tmp_path / name.lower()) for name in SESSION_NAMES - set(MARKERS)}}
    for name in vanished:
        values.pop(name, None)
    parent = Vanishing(values, vanished)
    selected = build_environment(environ=parent)
    assert selected == build_environment(environ=values)
    assert set(parent.reads) <= set(launch_env.SESSION_PROBE_NAMES) | set(selected) | set(vanished)
    assert "SUBFLEET_TEST_TOKEN" not in parent.reads
    assert environment_report(Vanishing(values, vanished))["extra_names"]
    environment = ProviderEnvironment({**CODEX_POLICY, "env": {"pass": ["GIT_CONFIG_GLOBAL", *vanished]}})
    for provider in ("claude", "codex"):
        child = environment.build(provider, environ=Vanishing(values, vanished))
        assert child == environment.build(provider, environ=values), provider


# --- P3-3: no withheld value is read ---------------------------------------------------


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("live", [False, True], ids=["no-session", "live-session"])
def test_provider_environment_never_reads_a_withheld_value(provider, live):
    """Review r4, P3-3: with a policy both start checks refuse, `additions` used to
    read the lane variable's value before rejecting it; nothing reads it now."""
    values = {**(live_probe() if live else {}), "PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null",
              "SUBFLEET_TEST_OAUTH": "dummy-lane-value"}

    class Strict(StrictMapping):
        def __getitem__(self, name):
            assert name != "SUBFLEET_TEST_OAUTH", "a withheld lane variable's value was read"
            return super().__getitem__(name)

    environment = ProviderEnvironment({**CODEX_POLICY, "env": {"pass": ["GIT_CONFIG_GLOBAL", "SUBFLEET_TEST_OAUTH"]}},
                                      references=lambda: ["SUBFLEET_TEST_OAUTH"])
    own = {"CLAUDE_CODE_OAUTH_TOKEN": "dummy-own"} if provider == "claude" else {"CODEX_HOME": "/fake"}
    for env in (environment.additions(provider, own, environ=Strict(values)),
                environment.build(provider, own, environ=Strict(values))):
        assert "SUBFLEET_TEST_OAUTH" not in env and env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert "dummy-lane-value" not in env.values()


# --- P3-1 and P2-1 at `daemon start` -----------------------------------------------------


def daemon_start_environ(monkeypatch, root, **names):
    """`os.environ` as a plain dict that still names the test's own state root.

    `daemon start` resolves its root from SUBFLEET_HOME alone (`cli._root`
    ignores its arguments), and without the session's marker its default is the
    person's `~/.subfleet`. So every replaced environment names `root`, and this
    checks that the command would act on it before any test calls it.
    """
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", "SUBFLEET_HOME": str(root), **names})
    assert cli._root(SimpleNamespace()).resolve() == root.resolve(), "daemon start would leave the test's root"


def no_spawn(monkeypatch):
    monkeypatch.setattr(cli, "_daemon_alive", lambda *args, **kwargs: False)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("a refused start spawned"))


def spawned(monkeypatch):
    """`daemon start` up to its spawn: the environment it hands the daemon, and a child that answers."""
    seen, alive = [], iter([False])

    class Child:
        pid, returncode = 4242, None

        def poll(self):
            return None

    monkeypatch.setattr(cli, "_daemon_alive", lambda client, *, timeout=None: next(alive, True))
    monkeypatch.setattr(cli.Client, "lock_info", lambda self: None)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda argv, **kwargs: seen.append(kwargs["env"]) or Child())
    return seen


@pytest.mark.parametrize("where", ["lanes.json", "the store"])
def test_env_pass_refusal_names_its_entry_variable_and_lane(where, tmp_path, monkeypatch, capsys):
    """Review r4, P3-1: `env.pass[i]`, the variable (a name) and the lane, never a value."""
    lane = env_lane("claude-9", "E2E_LANE_NINE_TOKEN")
    if where == "lanes.json":
        write_lanes(tmp_path, lane)
    else:
        with Store(tmp_path / "state.sqlite3") as store:
            store.put_lane(lane)
    write_policy(tmp_path, ["GIT_CONFIG_GLOBAL", "E2E_LANE_NINE_TOKEN"])
    daemon_start_environ(monkeypatch, tmp_path, E2E_LANE_NINE_TOKEN="dummy-nine")
    with pytest.raises(PolicyError) as error:
        cli.daemon_env(tmp_path)
    assert error.value.key == "env.pass[1]"
    for text in ("E2E_LANE_NINE_TOKEN", "claude-9", where):
        assert text in str(error.value)
    no_spawn(monkeypatch)
    assert cli.cmd_daemon_start(SimpleNamespace(state_root=str(tmp_path))) == 2
    err = capsys.readouterr().err
    assert all(text in err for text in ("env.pass[1]", "E2E_LANE_NINE_TOKEN", "claude-9", where))
    assert "dummy-nine" not in err


@pytest.mark.parametrize("reference", ["SUBFLEET_FAKE_LANE_NINE", "HOME", "SUBFLEET_HOST_HOME"])
def test_a_refused_lanes_json_row_still_refuses_daemon_start_and_names_its_lane(reference, tmp_path,
                                                                                monkeypatch, capsys):
    """Review r4, P2-1: as before, refused before any spawn; now the lane and its file are named."""
    write_lanes(tmp_path, env_lane("claude-1", "E2E_LANE_ONE_TOKEN"), env_lane("claude-9", reference))
    daemon_start_environ(monkeypatch, tmp_path, **{reference: "dummy-nine"})
    with pytest.raises(PolicyError) as error:
        cli.daemon_env(tmp_path)
    assert error.value.key == "credential_ref"
    assert all(text in str(error.value) for text in ("claude-9", reference, "lanes.json"))
    assert "claude-1" not in str(error.value)
    no_spawn(monkeypatch)
    assert cli.cmd_daemon_start(SimpleNamespace(state_root=str(tmp_path))) == 2
    err = capsys.readouterr().err
    assert all(text in err for text in ("claude-9", reference, "lanes.json")) and "dummy-nine" not in err


@pytest.mark.parametrize("reference", ["SUBFLEET_FAKE_LANE_NINE", "HOME", "SUBFLEET_HOST_HOME"])
def test_daemon_start_warns_of_a_stored_refused_lane_and_starts(reference, tmp_path, monkeypatch, capsys):
    """Review r4, P2-1: a lane only the store holds warns, naming the lane and the
    store, and the daemon starts; the refused variable is passed to no one by name."""
    with Store(tmp_path / "state.sqlite3") as store:
        store.put_lane(env_lane("claude-9", reference))
        store.put_lane(env_lane("claude-1", "E2E_LANE_ONE_TOKEN"))
    daemon_start_environ(monkeypatch, tmp_path, E2E_LANE_ONE_TOKEN="dummy-one", **{reference: "dummy-nine"})
    seen = spawned(monkeypatch)
    assert cli.cmd_daemon_start(SimpleNamespace(state_root=str(tmp_path))) == 0
    err = capsys.readouterr().err
    assert all(text in err for text in ("claude-9", reference, "the store")) and "dummy" not in err
    assert "claude-1" not in err
    [environment] = seen
    assert environment["E2E_LANE_ONE_TOKEN"] == "dummy-one", "the daemon still holds its lanes' credentials"
    if reference == "SUBFLEET_HOST_HOME":
        assert reference not in environment, "a refused variable is no credential the daemon holds"


def test_daemon_start_takes_a_fixed_lanes_json_row_for_a_stored_refused_lane(tmp_path, monkeypatch, capsys):
    """Review r4, P2-1 recovery: the store's stale variable is replaced at startup, so no warning."""
    with Store(tmp_path / "state.sqlite3") as store:
        store.put_lane(env_lane("claude-9", "SUBFLEET_FAKE_LANE_NINE"))
    write_lanes(tmp_path, env_lane("claude-9", "SUBFLEET_TEST_OAUTH"))
    daemon_start_environ(monkeypatch, tmp_path, SUBFLEET_TEST_OAUTH="dummy-oauth")
    seen = spawned(monkeypatch)
    assert cli.cmd_daemon_start(SimpleNamespace(state_root=str(tmp_path))) == 0
    assert "claude-9" not in capsys.readouterr().err
    assert seen[0]["SUBFLEET_TEST_OAUTH"] == "dummy-oauth"
