"""C-20.6 (D-ST3, review r1): fresh children, for what one pytest process cannot show.

- Decision 6: `python -m subfleet.daemon` with no SUBFLEET_HOME. A plain process,
  with neither pytest loaded nor a test marker, resolves HOME/.subfleet as
  release/217 does; a test process refuses in one line, not a traceback.
- Decision 7: importing every `subfleet` module in a fresh, minimal child loads
  neither pytest, _pytest nor a `pytest_*` plugin.
- Invariant 2: in a plain child, every function D-ST3 changes returns what
  release/217's own body returns (`tests/fixtures/home_resolvers_release_217_st3.py`),
  over the same environments, live-root overrides included.
- Invariant 5: the property's draws are the same under every PYTHONHASHSEED, and
  in a minimal environment.

Every child gets a temporary HOME. The differential's child also gets a
stand-in account (pwd patched inside it), so a "live root" there is a temporary
directory and no child names the account's own paths.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from subfleet.home_paths import PYTEST_MARKER

REPO = Path(__file__).resolve().parents[2]
REFUSAL = "subfleet daemon: Refusing personal path fallback under pytest; set SUBFLEET_HOME to an isolated test path"


def minimal_env(home: Path, **extra: str) -> dict[str, str]:
    """A child's whole environment: a temporary HOME, a system PATH and this checkout."""
    return {"HOME": str(home), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1",
            **extra}


# --- decision 6: the daemon's default state root -------------------------------------------------


DAEMON_CHILDREN = {
    "plain": ({}, False),
    "bare-marker": ({PYTEST_MARKER: "1"}, False),
    "current-test": ({"PYTEST_CURRENT_TEST": "tests/unit/x.py::test (call)"}, False),
    "pytest-loaded": ({}, True),
}
DAEMON_MAIN = "import runpy, sys; {imports}runpy.run_module('subfleet.daemon', run_name='__main__', alter_sys=True)"


@pytest.mark.parametrize("child", DAEMON_CHILDREN)
def test_the_daemon_without_subfleet_home(child, tmp_path):
    """C-20.6 (D-ST3, review r1 check 2): only a test process refuses, and in one line.

    The stand-in store's `daemon.lock` is held here, so a daemon that resolves
    HOME/.subfleet exits 69 at its lock, as release/217's does, before it serves
    anything. A test process (a marker, or pytest loaded) refuses before it
    resolves any root. At `aa74cd62a` that refusal was a RuntimeError traceback.
    """
    extra, loads_pytest = DAEMON_CHILDREN[child]
    home = tmp_path / "home"
    store = home / ".subfleet"
    store.mkdir(parents=True)
    held = os.open(store / "daemon.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code = DAEMON_MAIN.format(imports="import pytest; " if loads_pytest else "")
        done = subprocess.run([sys.executable, "-c", code], env=minimal_env(home, **extra), cwd=tmp_path,
                              capture_output=True, text=True, timeout=120)
    finally:
        os.close(held)
    if child == "plain":
        assert (done.returncode, done.stderr.strip()) == (69, "another daemon holds daemon.lock"), done.stderr
    else:
        assert done.returncode == 7, done.stderr
        assert done.stderr.strip().splitlines() == [REFUSAL]
        assert "Traceback" not in done.stderr
    assert sorted(path.name for path in store.iterdir()) == ["daemon.lock"]


def test_the_daemon_program_reports_a_refusal_in_one_line(monkeypatch, capsys):
    """C-20.6 (D-ST3): the program entry turns the refusal into exit 7 and one line; `main` raises it."""
    from subfleet import daemon
    from subfleet.home_paths import PersonalPathRefused
    monkeypatch.delenv("SUBFLEET_HOME")
    monkeypatch.setattr(daemon, "Daemon", lambda *a, **k: pytest.fail("a daemon was built"))
    with pytest.raises(PersonalPathRefused, match="SUBFLEET_HOME"):
        daemon.main([])
    assert daemon.run_program([]) == 7
    assert capsys.readouterr().err.strip().splitlines() == [REFUSAL]


# --- decision 7: no subfleet module loads pytest ---------------------------------------------------


IMPORT_ALL = textwrap.dedent('''
    import importlib, json, pkgutil, sys
    import subfleet
    names = sorted(found.name for found in pkgutil.walk_packages(subfleet.__path__, "subfleet.")
                   if found.name.rsplit(".", 1)[-1] != "__main__")
    failed = {}
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as error:
            failed[name] = repr(error)[:200]
    loaded = sorted(name for name in sys.modules
                    if name.split(".")[0] in ("pytest", "_pytest") or name.split(".")[0].startswith("pytest_"))
    print(json.dumps({"modules": names, "failed": failed, "loaded": loaded}))
''')


def test_importing_every_subfleet_module_loads_no_pytest(tmp_path):
    """C-20.6 (D-ST3, decision 7): the runtime check beside the static one.

    A fresh child with a minimal environment imports every module under
    `subfleet/` (but `__main__`, which runs the CLI); none loads pytest, _pytest
    or a plugin, however it imports. This also covers the dynamic imports the
    static check cannot read (a computed name, an aliased `import_module`, `exec`).
    """
    (tmp_path / "home").mkdir()
    done = subprocess.run([sys.executable, "-c", IMPORT_ALL], env=minimal_env(tmp_path / "home"), cwd=tmp_path,
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout.splitlines()[-1])
    expected = sorted(".".join(path.relative_to(REPO).with_suffix("").parts).removesuffix(".__init__")
                      for path in (REPO / "subfleet").rglob("*.py")
                      if path.name != "__main__.py" and path != REPO / "subfleet" / "__init__.py")
    assert report["modules"] == expected
    assert report["failed"] == {}
    assert report["loaded"] == []


# --- invariant 2: production resolves as release/217 ----------------------------------------------


DIFFERENTIAL = textwrap.dedent('''
    import ast, importlib, json, os, pwd, sys, types
    from pathlib import Path
    from types import SimpleNamespace
    assert "pytest" not in sys.modules and "_pytest" not in sys.modules
    scratch = Path(sys.argv[1])
    account = scratch / "Users" / "acct"
    for store in (".subfleet", ".claude", ".codex"):
        (account / store).mkdir(parents=True)
    (account / ".claude.json").write_text("{}")
    other = scratch / "home"
    other.mkdir()
    bindir = scratch / "bin"
    bindir.mkdir()
    for name in ("subfleet", "subfleetd"):
        (bindir / name).write_text("#!/bin/sh\\nexit 9\\n")
        (bindir / name).chmod(0o755)
    # The stand-in account: pwd names it for this child's uid and for `~acct`.
    entry = SimpleNamespace(pw_dir=str(account), pw_name="acct")
    def by_name(name):
        if name == "acct":
            return entry
        raise KeyError(name)
    pwd.getpwuid = lambda uid: entry
    pwd.getpwnam = by_name

    from tests.fixtures.home_resolvers_release_217_st3 import SOURCES
    from subfleet import home_paths
    from subfleet.conversations.service import ConversationService
    from subfleet import prune_decisions

    def outcome(call):
        try:
            value = call()
        except BaseException as error:
            return ["exception", type(error).__name__, str(error)]
        if isinstance(value, (set, frozenset)):
            value = sorted(value)
        return ["value", repr(value)]

    # Release's home_paths, every body in one namespace, so its functions call each other.
    base_paths = types.ModuleType("subfleet.home_paths")
    base_paths.__dict__.update(home_paths.__dict__)
    for (module, name), source in SOURCES.items():
        if module == "subfleet.home_paths":
            exec("from __future__ import annotations\\n" + source, base_paths.__dict__)

    def release(module_name, qualname):
        """Release's body of `qualname`, bound to release's home_paths."""
        module = importlib.import_module(module_name)
        if module_name == "subfleet.home_paths":
            return base_paths.__dict__[qualname]
        namespace = module.__dict__.copy()
        for name, value in list(namespace.items()):
            if getattr(home_paths, name, None) is value and name in base_paths.__dict__:
                namespace[name] = base_paths.__dict__[name]
        source = SOURCES[(module_name, qualname)]
        exec("from __future__ import annotations\\n" + source, namespace)
        return namespace[qualname.rsplit(".", 1)[-1]]

    def with_release_paths(call):
        """Run `call` with release's home_paths for an import inside a function."""
        saved = sys.modules["subfleet.home_paths"]
        sys.modules["subfleet.home_paths"] = base_paths
        try:
            return outcome(call)
        finally:
            sys.modules["subfleet.home_paths"] = saved

    service = SimpleNamespace(root=scratch / "dev-root")
    recorded = []
    def stop_at_prune(root, **_):
        recorded.append(str(root))
        raise SystemExit(0)
    prune_decisions.prune = stop_at_prune

    def run_prune(function):
        recorded.clear()
        try:
            code = function(["--no-report"])
        except SystemExit:
            code = "stopped at prune"
        return code, list(recorded)

    OPTIONS = ({}, {"test_only": True}, {"accept_empty": True}, {"expand_override": True})

    def calls(module, name, function):
        """How the differential calls each changed function."""
        if name.endswith("_app_executables"):
            return lambda: function(service)
        if name == "_daemond_argv":
            return lambda: function(scratch / "root")
        if name == "keychain_command":
            return lambda: function("claude-quota-fixture@example.test", runner=lambda *a, **k: None)
        if module == "subfleet.prune_decisions":
            return lambda: run_prune(function)
        if name == "global_config_path":
            return lambda: function(None)
        if name == "home_path":
            return lambda: [outcome(lambda: function(variable, lambda: f"<default {variable}>", **options))
                            for variable in ("SUBFLEET_HOME", "CLAUDE_CONFIG_DIR", "SUBFLEET_V1_BIN", "SUBFLEET_PROVIDER_HOME")
                            for options in OPTIONS]
        return function

    # A test-only helper: production calls it only once under_pytest() has said
    # this is a test process, and D-ST3 changed what it answers there.
    TEST_ONLY = {("subfleet.home_paths", "is_account_home")}

    live = {"SUBFLEET_HOME": account / ".subfleet", "CLAUDE_CONFIG_DIR": account / ".claude",
            "SUBFLEET_CODEX_APP_HOME": account / ".codex", "SUBFLEET_DESKTOP_LOGIN": account / ".claude.json",
            "SUBFLEET_PROVIDER_HOME": account, "SUBFLEET_V1_BIN": account / ".subfleet" / "bin" / "subfleet",
            "SUBFLEET_GUARD_TRUST": account / ".subfleet" / "guard" / "TRUST",
            "SUBFLEET_CODEX_GUARD_CACHE": account / ".subfleet" / "cache", "CODEX_HOME": account / ".codex",
            "CLAUDE_LANE_AGENT_SECRET": account / "bin" / "agent-secret",
            "SUBFLEET_DAEMON_BIN": account / ".subfleet" / "bin" / "subfleetd",
            "SUBFLEET_DEV_APP_EXECUTABLE": account / ".subfleet" / "dev-app", "TMPDIR": account / ".subfleet" / "tmp"}
    (scratch / "tmp").mkdir()
    base = {"PATH": str(bindir) + ":/usr/bin:/bin", "TMPDIR": str(scratch / "tmp")}
    environments = {
        "account-home": {**base, "HOME": str(account)},
        "test-home": {**base, "HOME": str(other)},
        "no-home": dict(base),
        "tilde-account-home": {**base, "HOME": "~acct"},
        "tilde-home": {**base, "HOME": "~"},
        "home-in-store": {**base, "HOME": str(account / ".codex")},
        "live-overrides": {**base, "HOME": str(other), **{name: str(path) for name, path in live.items()}},
        "live-overrides-account-home": {**base, "HOME": str(account), **{name: str(path) for name, path in live.items()}},
        "dot-dot-overrides": {**base, "HOME": str(other), **{name: "~/" + "../" * len(other.parts) + str(path).lstrip("/")
                                                              for name, path in live.items()}},
        "tilde-overrides": {**base, "HOME": str(account), **{name: "~/" + str(path.relative_to(account))
                                                              if path != account else "~" for name, path in live.items()}},
        "tilde-user-overrides": {**base, "HOME": str(other), **{name: "~acct/" + str(path.relative_to(account))
                                                                 if path != account else "~acct" for name, path in live.items()}},
        "empty-overrides": {**base, "HOME": str(other), **{name: "" for name in live}},
        "relative-overrides": {**base, "HOME": str(other), **{name: "relative/" + name.lower() for name in live}},
    }
    compared = {}
    os.chdir(scratch)
    for label, environment in environments.items():
        os.environ.clear()
        os.environ.update(environment)
        for (module, qualname) in SOURCES:
            if (module, qualname) in TEST_ONLY:
                continue
            name = qualname.rsplit(".", 1)[-1]
            target = importlib.import_module(module)
            head = getattr(ConversationService, name) if qualname.startswith("ConversationService.") else getattr(target, name)
            after = outcome(calls(module, qualname, head))
            before = with_release_paths(calls(module, qualname, release(module, qualname)))
            assert after == before, (label, qualname, before, after)
            compared[f"{label}:{qualname}"] = after[0]
    assert "pytest" not in sys.modules and "_pytest" not in sys.modules
    print(json.dumps(compared))
''')


def test_production_resolves_as_release_217(tmp_path):
    """C-20.6 (D-ST3, invariant 2): in a plain child every changed function answers as release/217's body.

    The environments include HOMEs spelled `~acct`, `~` and inside a live root, and
    overrides naming the stand-in account's live roots directly, through `..`,
    through `~` and `~acct`, empty and relative: production refuses none of them,
    exactly as release does. Every value and exception must agree.
    """
    (tmp_path / "home").mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    done = subprocess.run([sys.executable, "-c", DIFFERENTIAL, str(scratch.resolve())],
                          env=minimal_env(tmp_path / "home"), cwd=tmp_path, capture_output=True, text=True,
                          timeout=300)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    compared = json.loads(done.stdout.splitlines()[-1])
    assert len(compared) == 13 * 16
    # Not vacuous: release's own answers include values and exceptions both.
    assert {"value", "exception"} <= set(compared.values())


# --- invariant 5: the property's draws ------------------------------------------------------------


DRAWS = textwrap.dedent('''
    import hashlib, json, sys
    from hypothesis import HealthCheck, given, settings, strategies as st
    from tests.unit import test_live_store_guard_st3 as st3
    # The session's values are this process's own; fix them so a digest compares the choices alone.
    st3.SESSION = {name: f"<session {name}>" for name in st3.NAMES[::2]}
    seen = []
    @settings(max_examples=40, derandomize=True, database=None, deadline=None, suppress_health_check=list(HealthCheck))
    @given(env=st3.environments(st3.Path("/st3-scratch")))
    def draw(env):
        seen.append(sorted(env.items()) if sys.argv[1] == "sorted" else list(env.items()))
    draw()
    print(hashlib.sha256(json.dumps(seen).encode()).hexdigest(), len(seen))
''')


def draws_digest(tmp_path, seed: int, *, minimal: bool) -> str:
    env = (minimal_env(tmp_path / "home", PYTHONHASHSEED=str(seed)) if minimal
           else {**os.environ, "PYTHONPATH": str(REPO), "PYTHONHASHSEED": str(seed)})
    env["HYPOTHESIS_STORAGE_DIRECTORY"] = str(tmp_path / "hypothesis")
    done = subprocess.run([sys.executable, "-c", DRAWS, "ordered"], env=env, cwd=tmp_path, capture_output=True,
                          text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-3000:]
    digest, count = done.stdout.split()
    assert int(count) == 40
    return digest


def test_the_property_draws_the_same_environments_in_every_process(tmp_path):
    """C-20.6 (D-ST3, invariant 5, review r1's P1): the same draws under PYTHONHASHSEED 0, 4 and 7, in this
    session's environment and in a minimal one.

    The draws had followed set order, which the per-process string hash decides,
    so a derandomized run replayed different environments in each process.
    """
    (tmp_path / "home").mkdir()
    digests = {(seed, minimal): draws_digest(tmp_path, seed, minimal=minimal)
               for seed in (0, 4, 7) for minimal in (False, True)}
    assert len(set(digests.values())) == 1, digests
