"""No test report can print a credential the session inherited (C-20.6, D-PRR1).

On 2026-10-21 a failing restore test's report printed a lane's session token.
pytest's long traceback, its default for a failing test, prints every entry's
function arguments, so `subprocess.run`'s `kwargs`, and with them a copied
environment, reached the report; `-l` printed the same frame's locals, and `-l`
with `-vv` prints every variable in full. So `tests/conftest.py` removes every
credential-shaped variable before collection, and puts each back exactly at
unconfigure. A test that needs one sets a dummy itself. Every value here is a
dummy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import types
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import home_paths
from subfleet.home_paths import PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID, SAFE_HOME_PATHS
from tests.credential_leak_child import DUMMY_NAMES

REPO = Path(__file__).resolve().parents[2]
PREFIX = "SECRET7prr1"
EMPTY_NAME = "PRR1_DUMMY_EMPTY_TOKEN"

#: Starts a session in this process and records, as digests, what the dummies
#: were before it and after unconfigure.
DRIVER = r"""
import hashlib, json, os, sys
import pytest
result, names = sys.argv[1], sys.argv[2].split(",")
def seen():
    return {name: hashlib.sha256(os.environ[name].encode()).hexdigest()
            for name in names if name in os.environ}
before = seen()
code = pytest.main(sys.argv[3:])
with open(result, "w") as stream:
    json.dump({"code": int(code), "before": before, "after": seen()}, stream)
"""


@pytest.mark.parametrize("name", [
    "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "CLIENT_SECRET", "DB_PASSWORD", "SESSION_COOKIE",
    "GOOGLE_APPLICATION_CREDENTIALS", "CREDENTIAL_HELPER", "SSH_AUTH_SOCK", "NGROK_AUTHTOKEN",
    "github_token", "npm_config__auth", "SUBFLEET_AGENT_SECRET", "CLAUDE_LANE_AGENT_SECRET",
])
def test_c20_6_credential_shaped_names(name):
    """C-20.6, D-PRR1: a name ending in _TOKEN, _KEY, _SECRET, _PASSWORD or
    _COOKIE, or holding CREDENTIAL or _AUTH, in any case, is credential-shaped."""
    assert home_paths.credential_shaped(name)


@pytest.mark.parametrize("name", [
    "PATH", "HOME", "TOKEN", "MONKEY", "KEYCHAIN_PATH", "OAUTH_CLIENT_ID", "TOKENS_DIR",
    "AUTHOR", "state_root", PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID,
])
def test_c20_6_names_that_are_not_credential_shaped(name):
    """C-20.6, D-PRR1: other names, and the session's own markers, stay."""
    assert not home_paths.credential_shaped(name)


_STEMS = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,10}", fullmatch=True)
_NAMES = st.one_of(
    _STEMS,
    st.tuples(_STEMS, st.sampled_from(["_TOKEN", "_KEY", "_SECRET", "_PASSWORD", "_COOKIE",
                                       "_token", "CREDENTIAL", "_AUTH", "_auth_x", "KEY"]))
    .map("".join),
    st.sampled_from([PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID, "PATH", "HOME",
                     "SSH_AUTH_SOCK", *SAFE_HOME_PATHS]),
)
_ENVIRONMENTS = st.dictionaries(_NAMES, st.text(max_size=8), max_size=12)


@settings(max_examples=100, deadline=None)
@given(environ=_ENVIRONMENTS, changes=_ENVIRONMENTS, dropped=st.sets(_NAMES, max_size=4))
def test_c20_6_removal_takes_exactly_the_credentials_and_restore_puts_each_back(
        environ, changes, dropped):
    """C-20.6, D-PRR1 invariant 4 over plain mappings: removal takes every
    credential-shaped name and nothing else, and restoring puts each back with
    its value, whatever the session set or dropped meanwhile."""
    original = dict(environ)
    removed = home_paths.remove_credentials(environ)
    assert removed == {name: value for name, value in original.items()
                       if home_paths.credential_shaped(name)}
    assert environ == {name: value for name, value in original.items()
                       if not home_paths.credential_shaped(name)}
    environ.update(changes)
    for name in dropped:
        environ.pop(name, None)
    session = dict(environ)
    home_paths.restore_credentials(environ, removed)
    assert environ == {**session, **removed}


def test_c20_6_this_session_holds_no_inherited_credential(request):
    """C-20.6, D-PRR1: configure kept what it removed to put back, and only the
    session's own safe paths are credential-shaped now."""
    removed = request.config._subfleet_removed_credentials
    assert all(home_paths.credential_shaped(name) for name in removed)
    present = {name for name in os.environ if home_paths.credential_shaped(name)}
    assert present <= set(SAFE_HOME_PATHS)


def test_c20_6_unconfigure_puts_back_exactly_what_configure_removed(monkeypatch):
    """C-20.6, D-PRR1 invariant 4, through the session's own unconfigure hook:
    each removed name returns with its value, an empty one included."""
    from tests import conftest

    removed = {"PRR1_RESTORE_TOKEN": PREFIX + "-restore", "PRR1_RESTORE_EMPTY_KEY": ""}
    for name in removed:
        monkeypatch.setenv(name, "set during the session")
    monkeypatch.delenv("PRR1_RESTORE_EMPTY_KEY")
    config = types.SimpleNamespace(_subfleet_removed_credentials=dict(removed))

    conftest.pytest_unconfigure(config)

    assert {name: os.environ.get(name) for name in removed} == removed
    assert not hasattr(config, "_subfleet_removed_credentials")


def _leaked(value: str, report: str) -> bool:
    """Whether the report holds `value`, its fixed prefix, or any ten
    characters of its random tail."""
    tail = value[len(PREFIX):]
    return (value in report or PREFIX in report
            or any(tail[start:start + 10] in report for start in range(len(tail) - 9)))


@pytest.mark.parametrize("options", [("-l", "--tb=long"), ("-l", "--tb=long", "-vv")],
                         ids=["showlocals", "showlocals-vv"])
def test_c20_6_a_failing_session_prints_no_part_of_an_inherited_credential(tmp_path, options):
    """C-20.6, D-PRR1 invariants 3 and 4: a session started with dummy credentials
    in a minimal environment fails in every way a report prints values, and its
    whole report holds no part of any dummy; after unconfigure, each is back."""
    values = {name: PREFIX + secrets.token_hex(12) for name in DUMMY_NAMES if name != EMPTY_NAME}
    for name in ("home", "tmp"):
        (tmp_path / name).mkdir()
    # The dummies come first, where even a truncated repr of the whole
    # environment shows its first entries.
    environment = {**values, EMPTY_NAME: "", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                   "HOME": str(tmp_path / "home"), "TMPDIR": f"{tmp_path / 'tmp'}/",
                   "HYPOTHESIS_STORAGE_DIRECTORY": str(tmp_path / "hypothesis")}
    environment.update((name, os.environ[name]) for name in ("LANG", "LC_ALL")
                       if name in os.environ)
    result = tmp_path / "result.json"

    completed = subprocess.run(
        [sys.executable, "-c", DRIVER, str(result), ",".join(DUMMY_NAMES),
         "-p", "no:cacheprovider", "tests/credential_leak_child.py", *options],
        cwd=REPO, env=environment, capture_output=True, text=True, timeout=600)
    report = completed.stdout + completed.stderr

    assert re.search(r"\b2 failed, 1 passed\b", report), report[-2000:]
    # Each channel printed something, so each was searched.
    assert "kwargs = {'env': {" in report
    assert re.search(r"^values +=.*'SUBFLEET_DUMMY_TOKEN': None", report, re.MULTILINE)
    assert "Captured stdout call" in report
    assert "environment={" in report
    assert [name for name, value in values.items() if _leaked(value, report)] == []

    outcome = json.loads(result.read_text())
    digests = {name: hashlib.sha256(value.encode()).hexdigest()
               for name, value in {**values, EMPTY_NAME: ""}.items()}
    assert outcome["code"] == 1
    assert outcome["before"] == digests
    assert outcome["after"] == digests
