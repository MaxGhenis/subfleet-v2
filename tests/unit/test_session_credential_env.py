"""A test session keeps the credentials it inherited out of its reports (C-20.6, D-PRR1).

On 2026-10-21 a failing restore test's report printed a lane's session token.
pytest's long traceback, its default for a failing test, prints every entry's
function arguments, so `subprocess.run`'s `kwargs`, and with them a copied
environment, reached the report; `-l` printed the same frame's locals, and `-l`
with `-vv` prints every variable in full. So `tests/conftest.py` removes every
credential-shaped variable before collection, and puts each back exactly at
unconfigure. The store that keeps them shows names only, and tests bind names
only (#244 review r1: a test that bound the store's values printed one under
`-l`), and pickling or copying the store raises (#244 review r2). A test that
reads a value out of the store, or copies one by hand, can still print it. A
test that needs a credential sets a dummy itself. Every value here is a dummy.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import json
import os
import pickle
import pprint
import re
import reprlib
import secrets
import subprocess
import sys
import types
from pathlib import Path

import pytest
from _pytest._io.pprint import PrettyPrinter
from _pytest._io.saferepr import safeformat, saferepr
from hypothesis import given, settings, strategies as st
from hypothesis.vendor.pretty import pretty

from subfleet import home_paths
from subfleet.home_paths import PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID, SAFE_HOME_PATHS
from tests.credential_leak_child import DUMMY_NAMES

REPO = Path(__file__).resolve().parents[2]
PREFIX = "SECRET7prr1"
EMPTY_NAME = "PRR1_DUMMY_EMPTY_TOKEN"
#: A dummy the child sets once configure has run, as a plugin or a test could.
LATE_NAME = "PRR1_LATE_TOKEN"

#: Starts a session in this process, sets the variables a JSON file holds once
#: configure has run, and records, as digests, what the named dummies were
#: before the session and after unconfigure.
DRIVER = r"""
import hashlib, json, os, sys
import pytest
result, names, late = sys.argv[1], sys.argv[2].split(","), sys.argv[3]
class SetAfterConfigure:
    def pytest_sessionstart(self, session):
        with open(late) as stream:
            os.environ.update(json.load(stream))
def seen():
    return {name: hashlib.sha256(os.environ[name].encode()).hexdigest()
            for name in names if name in os.environ}
before = seen()
code = pytest.main(sys.argv[4:], plugins=[SetAfterConfigure()])
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
    assert dict(removed) == {name: value for name, value in original.items()
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
    session's own safe paths are credential-shaped now. It binds the removed
    names only, so a failing check prints no value (#244 review r1)."""
    removed = set(request.config._subfleet_removed_credentials)
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
    config = types.SimpleNamespace(
        _subfleet_removed_credentials=home_paths.RemovedCredentials(removed))

    conftest.pytest_unconfigure(config)

    assert {name: os.environ.get(name) for name in removed} == removed
    assert not hasattr(config, "_subfleet_removed_credentials")


def _leaked(value: str, report: str) -> bool:
    """Whether the report holds `value`, its fixed prefix, or any ten
    characters of its random tail."""
    tail = value[len(PREFIX):]
    return (value in report or PREFIX in report
            or any(tail[start:start + 10] in report for start in range(len(tail) - 9)))


def _dummies(*names: str) -> dict[str, str]:
    """A fresh dummy for each name; the empty one stays empty."""
    return {name: "" if name == EMPTY_NAME else PREFIX + secrets.token_hex(12) for name in names}


def _leaks(dummies: dict[str, str], report: str) -> list[str]:
    """The names, never the values, of the dummies any part of which is in the report."""
    return [name for name, value in dummies.items() if value and _leaked(value, report)]


def _digests(dummies: dict[str, str]) -> dict[str, str]:
    return {name: hashlib.sha256(value.encode()).hexdigest() for name, value in dummies.items()}


def _child_session(tmp_path: Path, inherited: dict[str, str], late: dict[str, str],
                   *args: str) -> tuple[str, Path]:
    """Run `pytest args` in a child whose environment holds only `inherited` and
    what a session needs, and which sets `late` once configure has run. Returns
    the whole report, and the file the driver writes its outcome to."""
    for name in ("home", "tmp"):
        (tmp_path / name).mkdir()
    # The dummies come first, where even a truncated repr of the whole
    # environment shows its first entries.
    environment = {**inherited, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                   "HOME": str(tmp_path / "home"), "TMPDIR": f"{tmp_path / 'tmp'}/",
                   "HYPOTHESIS_STORAGE_DIRECTORY": str(tmp_path / "hypothesis")}
    environment.update((name, os.environ[name]) for name in ("LANG", "LC_ALL")
                       if name in os.environ)
    late_file = tmp_path / "late.json"
    late_file.write_text(json.dumps(late))
    result = tmp_path / "result.json"
    completed = subprocess.run(
        [sys.executable, "-c", DRIVER, str(result), ",".join(inherited), str(late_file),
         "-p", "no:cacheprovider", *args],
        cwd=REPO, env=environment, capture_output=True, text=True, timeout=600)
    return completed.stdout + completed.stderr, result


@pytest.mark.parametrize("options", [("-l", "--tb=long"), ("-l", "--tb=long", "-vv")],
                         ids=["showlocals", "showlocals-vv"])
def test_c20_6_a_failing_session_prints_no_part_of_an_inherited_credential(tmp_path, options):
    """C-20.6, D-PRR1 invariants 3 and 4: a session started with dummy credentials
    in a minimal environment fails in every way a report prints values, and its
    whole report holds no part of any dummy; after unconfigure, each is back."""
    inherited = _dummies(*DUMMY_NAMES)

    report, result = _child_session(tmp_path, inherited, {},
                                    "tests/credential_leak_child.py", *options)

    assert _leaks(inherited, report) == []
    assert re.search(r"\b2 failed, 1 passed\b", report), report[-2000:]
    # Each channel printed something, so each was searched.
    assert "kwargs = {'env': {" in report
    # -l's locals: on one line, or with -vv pretty-printed over several.
    assert re.search(r"^values +=", report, re.MULTILINE)
    assert "'SUBFLEET_DUMMY_TOKEN': None" in report
    assert "Captured stdout call" in report
    assert "environment={" in report

    outcome = json.loads(result.read_text())
    assert outcome["code"] == 1
    assert outcome["before"] == _digests(inherited)
    assert outcome["after"] == _digests(inherited)


#: #244 review r1's options (`-vvv` is verbosity 2 after `-q`, so `-l` prints
#: whole locals through `safeformat`), and `-l` alone, which cuts them through
#: `saferepr`.
REPORT_OPTIONS = pytest.mark.parametrize(
    "options", [("-q", "-l", "--tb=long", "-vvv"), ("-l", "--tb=long")],
    ids=["review-r1", "showlocals"])


@REPORT_OPTIONS
def test_c20_6_the_session_check_failing_prints_no_part_of_a_dummy(tmp_path, options):
    """C-20.6, D-PRR1 fix round 1, invariants 1 and 3: #244 review r1's case. A
    child inherits dummy credentials, and a second dummy is set after configure,
    so this module's session check fails on that one; the whole report, its
    locals included, holds no part of either, and unconfigure puts back each
    inherited one."""
    inherited = _dummies(*DUMMY_NAMES)
    late = _dummies(LATE_NAME)
    check = f"{Path(__file__).relative_to(REPO)}::test_c20_6_this_session_holds_no_inherited_credential"

    report, result = _child_session(tmp_path, inherited, late, check, *options)

    assert _leaks({**inherited, **late}, report) == []
    assert re.search(r"\b1 failed\b", report), report[-2000:]
    # It failed at the sanity check, on the late dummy's name, and -l printed
    # the check's locals, so the locals were searched.
    assert ">       assert present <= set(SAFE_HOME_PATHS)" in report
    assert f"'{LATE_NAME}'" in report
    assert re.search(r"^removed +=", report, re.MULTILINE)
    outcome = json.loads(result.read_text())
    assert outcome["code"] == 1
    assert outcome["before"] == _digests(inherited)
    assert outcome["after"] == _digests(inherited)


@REPORT_OPTIONS
def test_c20_6_a_test_holding_the_store_prints_no_part_of_a_dummy(tmp_path, options):
    """C-20.6, D-PRR1 fix round 1, invariants 1 and 3: tests that hold the
    session's store of removed credentials, or its baseline snapshot, fail in
    each way a report prints them, and the whole report holds no part of any
    dummy, an inherited one or one set after configure. Two of the dummies,
    `CLAUDE_LANE_AGENT_SECRET` and `SUBFLEET_AGENT_SECRET`, are names the
    baseline snapshot restores as well. Comparing the snapshot with a dict
    prints those two entries under `-l` alone, where a cut local stops before
    them (#244 review r2's P3-3)."""
    inherited = _dummies(*DUMMY_NAMES, "CLAUDE_LANE_AGENT_SECRET", "SUBFLEET_AGENT_SECRET")
    late = _dummies(LATE_NAME)

    report, result = _child_session(tmp_path, inherited, late,
                                    "tests/credential_store_child.py", *options)

    assert _leaks({**inherited, **late}, report) == []
    assert re.search(r"\b7 failed\b", report), report[-2000:]
    # Each way a report prints a value printed the store, so each was searched:
    # -l's locals, a helper's argument, captured output, an assertion's
    # operand, a dict diff of the snapshot and Hypothesis's draw.
    assert re.search(r"^store +=", report, re.MULTILINE)
    assert re.search(r"^snapshot +=", report, re.MULTILINE)
    assert "store = <RemovedCredentials names=[" in report
    assert "Captured stdout call" in report
    # Below -vv, an assertion's operand is cut to 30 characters.
    assert re.search(r"assert (not )?<RemovedCred", report)
    assert "Draw 1 (store): <RemovedCredentials names=[" in report
    # The dict diff lists the snapshot's entries sorted, these two among the first.
    assert re.search(r"Left contains \d+ more items:", report)
    assert "'CLAUDE_LANE_AGENT_SECRET': None," in report
    assert "'SUBFLEET_AGENT_SECRET': None," in report
    outcome = json.loads(result.read_text())
    assert outcome["code"] == 1
    assert outcome["before"] == _digests(inherited)
    assert outcome["after"] == _digests(inherited)


_CREDENTIAL_NAMES = st.tuples(_STEMS, st.sampled_from(
    ["_TOKEN", "_KEY", "_SECRET", "_PASSWORD", "_COOKIE", "_token", "CREDENTIAL", "_AUTH"]),
).map("".join)


@st.composite
def _removed_values(draw) -> dict[str, str]:
    """Credential-shaped names, each holding a dummy, an empty value, or a part
    of a name, its own or another's."""
    names = draw(st.lists(_CREDENTIAL_NAMES, unique=True, max_size=8))
    values = {}
    for name in names:
        source = draw(st.sampled_from(names))
        start = draw(st.integers(0, len(source)))
        values[name] = draw(st.one_of(
            st.text(max_size=12).map(lambda tail: PREFIX + tail),
            st.just(""),
            st.integers(start, len(source)).map(lambda end: source[start:end])))
    return values


#: Each way a report renders an object: `repr` and `str`; pytest's traceback
#: arguments (`saferepr`, cut or not), its `-l` locals (`saferepr`, or
#: `safeformat` from `-vv`) and its assertion diffs' printer, all pytest 9.1's
#: own; Hypothesis's report; and the standard library's printers.
PRINTERS = {
    "repr": repr, "str": str, "format": "{}".format, "ascii": ascii,
    "saferepr": saferepr, "saferepr, not cut": lambda value: saferepr(value, maxsize=None),
    "safeformat": safeformat, "pytest's pformat": PrettyPrinter().pformat,
    "hypothesis": pretty, "pprint": pprint.pformat, "reprlib": reprlib.repr,
}


@settings(max_examples=100, deadline=None)
@given(values=_removed_values())
def test_c20_6_the_store_of_removed_credentials_shows_names_only(values):
    """C-20.6, D-PRR1 fix round 1, invariant 2: whatever the removed variables
    hold, dummies, empty values or parts of names, every printer renders the
    store from its names alone; restore still puts back each value."""
    environ = {**values, "PATH": "/usr/bin", "HOME": "/home"}
    store = home_paths.remove_credentials(environ)
    # The same names, each holding another value.
    twin = home_paths.remove_credentials({name: value + "!" for name, value in values.items()})
    shown = f"<RemovedCredentials names={sorted(values)!r}, values withheld>"

    assert repr(store) == str(store) == shown
    for printer, show in PRINTERS.items():
        text = show(store)
        assert text == show(twin), printer
        # A value is in the text only where it is part of the names-only text.
        assert [value for value in values.values()
                if value in text and value not in shown] == [], printer
    assert sorted(store) == sorted(values) and len(store) == len(values)
    home_paths.restore_credentials(environ, store)
    assert environ == {**values, "PATH": "/usr/bin", "HOME": "/home"}


#: Each way the standard library pickles or copies an object.
CARRIERS = {
    **{f"pickle protocol {protocol}": functools.partial(pickle.dumps, protocol=protocol)
       for protocol in range(pickle.HIGHEST_PROTOCOL + 1)},
    "copy": copy.copy, "deepcopy": copy.deepcopy,
    "deepcopy, inside a dict": lambda store: copy.deepcopy({"store": store}),
    "__reduce__": lambda store: store.__reduce__(),
    "__reduce_ex__": lambda store: store.__reduce_ex__(pickle.HIGHEST_PROTOCOL),
}


@pytest.mark.parametrize("carrier", CARRIERS)
def test_c20_6_the_store_cannot_be_pickled_or_copied(carrier):
    """C-20.6, D-PRR1 fix round 2: #244 review r2's P3-1. A failing test that
    pickled the store printed its values under `-vv`, and its `__reduce_ex__`
    handed them out; a copy carried them on. Each raises now, and its message
    holds no value."""
    values = _dummies("PRR1_PICKLE_TOKEN", "PRR1_PICKLE_KEY")
    store = home_paths.remove_credentials({**values, "PATH": "/usr/bin"})

    with pytest.raises(TypeError, match="cannot be pickled or copied") as raised:
        CARRIERS[carrier](store)

    assert _leaks(values, str(raised.value)) == []


@pytest.mark.documented_limitation
def test_c20_6_a_value_read_out_of_the_store_by_hand_is_the_value_documented_limitation():
    """C-20.6's stated limit (#244 review r2's P3-1): the store keeps each value
    to put it back, so a test that reads one out of it by name, `dict()`,
    `_values` or `__getstate__` holds the value, and could print it."""
    values = _dummies("PRR1_BY_HAND_TOKEN")
    store = home_paths.RemovedCredentials(values)

    assert store["PRR1_BY_HAND_TOKEN"] == values["PRR1_BY_HAND_TOKEN"]
    assert dict(store) == store._values == values
    assert store.__getstate__() == (None, {"_values": values})
