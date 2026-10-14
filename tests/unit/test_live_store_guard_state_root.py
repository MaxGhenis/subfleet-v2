"""D-ST1 with D-21: the person's production state root under pytest.

A daemon on any state root but `~/.subfleet` also accepts the development build
(D-21). D-ST1 round 1 spelled `~` there through the provider-home seam, so no
test resolved the person's live root. pytest points that seam at the session's
own directory, though, so every test root counted as a development root. #191's
test names its fake person by HOME, and it saw the development build accepted on
`HOME/.subfleet` (CI at dbd9c4112). A test's own HOME now names the root, as on
release/217. A HOME that is still the account's own is refused before anything
beneath it is touched. Here `pwd` names a stand-in account, except where a test
says it checks the real one; that test refuses every access before it happens.
"""

from __future__ import annotations

import contextlib
import os
import pwd
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import home_paths
from subfleet.conversations import service
from subfleet.conversations.peers import APP_EXECUTABLES
from subfleet.home_paths import SAFE_ENV_REMOVALS

DEV = "/Users/x/build/Subfleet Dev.app/Contents/MacOS/Subfleet"
REFUSAL = "set HOME to an isolated test home"
PROPERTIES = settings(max_examples=int(os.environ.get("SUBFLEET_STATE_ROOT_EXAMPLES", "120")),
                      deadline=None, database=None, derandomize=True,
                      suppress_health_check=[HealthCheck.function_scoped_fixture])


def app_executables(root):
    return service.ConversationService._app_executables(SimpleNamespace(root=Path(root)))


def identity(path):
    try:
        found = os.stat(path)
    except (OSError, ValueError):
        return None
    return found.st_dev, found.st_ino


def as_account(patch, home):
    """Make `pwd` name `home` as this uid's own home; other fields stay real."""
    real = pwd.getpwuid

    def lookup(uid):
        entry = real(uid)
        return pwd.struct_passwd((*entry[:5], str(home), *entry[6:])) if uid == os.getuid() else entry

    patch.setattr(pwd, "getpwuid", lookup)


@contextlib.contextmanager
def untouchable(patch, root):
    """Refuse, before it happens, any stat, lstat, readlink or listing at or
    beneath `root`, by any case. Path.resolve and Path.samefile go through these."""
    forbidden = os.path.normpath(os.fspath(root)).casefold()
    touched = []

    def reaches(path):
        # Every directory the lookup walks, so `root/..` counts as well.
        walked = ""
        for part in os.fsdecode(path).casefold().split(os.sep):
            walked = os.path.normpath(os.path.join(walked or os.sep, part)) if part else walked
            if walked == forbidden or walked.startswith(forbidden + os.sep):
                return True
        return False

    def guarded(function):
        def call(path, *args, **kwargs):
            if not isinstance(path, int) and reaches(os.path.join(os.getcwd(), os.fsdecode(path))):
                touched.append(os.fsdecode(path))
                raise AssertionError(f"touched {os.fsdecode(path)}")
            return function(path, *args, **kwargs)
        return call

    with patch.context() as inner:
        for name in ("stat", "lstat", "readlink", "listdir", "scandir"):
            inner.setattr(os, name, guarded(getattr(os, name)))
        yield touched


# --- The defect CI found at dbd9c4112 ------------------------------------------

def test_the_provider_home_seam_does_not_name_the_production_root(tmp_path, monkeypatch):
    """#191's D-21 test with HOME and the session's provider seam both set."""
    monkeypatch.setenv("SUBFLEET_DEV_APP_EXECUTABLE", DEV)
    monkeypatch.setenv("HOME", str(tmp_path / "person"))
    monkeypatch.setenv("SUBFLEET_PROVIDER_HOME", str(tmp_path / "providers"))
    for root in (tmp_path / "person" / ".subfleet", tmp_path / "providers" / ".subfleet"):
        root.mkdir(parents=True)
    assert app_executables(tmp_path / "person" / ".subfleet") == APP_EXECUTABLES
    assert app_executables(tmp_path / "providers" / ".subfleet") == (*APP_EXECUTABLES, DEV)
    assert app_executables(tmp_path / "person" / "dev-state") == (*APP_EXECUTABLES, DEV)


# --- A test's own HOME names the root -------------------------------------------

@pytest.mark.parametrize("spelling", ["plain", "trailing-slash", "dot-segment", "parent-segment",
                                      "symlink", "missing"])
def test_a_test_home_names_the_production_root(tmp_path, monkeypatch, spelling):
    person = tmp_path / "person"
    (person / ".subfleet").mkdir(parents=True)
    (tmp_path / "alias").symlink_to(person)
    home = {"plain": str(person), "trailing-slash": f"{person}/", "dot-segment": f"{tmp_path}/./person",
            "parent-segment": f"{person}/../person", "symlink": str(tmp_path / "alias"),
            "missing": str(tmp_path / "nobody")}[spelling]
    monkeypatch.setenv("HOME", home)
    expected = (Path(os.path.realpath(home)) / ".subfleet")
    assert home_paths.production_state_root() == expected
    assert home_paths.production_state_root() == (Path.home() / ".subfleet").resolve()


# --- The account's own home is refused before its root is touched ---------------

def account_spelling(tmp_path, account, spelling):
    alias = tmp_path / "account-alias"
    if spelling == "symlink" and not alias.exists():
        alias.symlink_to(account)
    return {"same": str(account), "trailing-slash": f"{account}/", "dot-segment": f"{tmp_path}/./account",
            "parent-segment": f"{account}/../account", "symlink": str(alias),
            "case": str(tmp_path / "ACCOUNT")}[spelling]


@pytest.mark.parametrize("spelling", ["unset", "same", "trailing-slash", "dot-segment",
                                      "parent-segment", "symlink", "case"])
def test_the_accounts_own_home_is_refused_before_its_root_is_touched(tmp_path, monkeypatch, spelling):
    account = tmp_path / "account"
    (account / ".subfleet").mkdir(parents=True)
    as_account(monkeypatch, account)
    if spelling == "unset":
        monkeypatch.delenv("HOME", raising=False)
    else:
        home = account_spelling(tmp_path, account, spelling)
        if identity(home) != identity(account):
            pytest.skip("a case variant names nothing on a case-sensitive volume")
        monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("SUBFLEET_DEV_APP_EXECUTABLE", DEV)
    with untouchable(monkeypatch, account / ".subfleet") as touched:
        with pytest.raises(RuntimeError, match=REFUSAL):
            home_paths.production_state_root()
        with pytest.raises(RuntimeError, match=REFUSAL):
            app_executables(tmp_path / "dev-state")
    assert touched == []


@pytest.mark.parametrize("spelling", ["unset", "same", "trailing-slash"])
def test_the_real_account_home_is_refused_without_touching_the_live_root(monkeypatch, spelling):
    """The person's own account: every access to `~/.subfleet` is refused before
    it happens, so even a regression here cannot reach the live store."""
    real = pwd.getpwuid(os.getuid()).pw_dir
    if spelling == "unset":
        monkeypatch.delenv("HOME", raising=False)
    else:
        monkeypatch.setenv("HOME", real + ("/" if spelling == "trailing-slash" else ""))
    monkeypatch.setenv("SUBFLEET_DEV_APP_EXECUTABLE", DEV)
    with untouchable(monkeypatch, Path(real) / ".subfleet") as touched:
        with pytest.raises(RuntimeError, match=REFUSAL):
            home_paths.production_state_root()
        with pytest.raises(RuntimeError, match=REFUSAL):
            service.ConversationService._app_executables(SimpleNamespace(root=Path("/nonexistent/dev-state")))
    assert touched == []


@pytest.mark.parametrize("configured", [None, ""])
def test_without_a_development_build_the_root_is_never_asked(monkeypatch, configured):
    """D-21 only widens: no development build named, nothing to decide."""
    if configured is None:
        monkeypatch.delenv("SUBFLEET_DEV_APP_EXECUTABLE", raising=False)
    else:
        monkeypatch.setenv("SUBFLEET_DEV_APP_EXECUTABLE", configured)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr(service, "production_state_root", lambda: pytest.fail("the state root was asked"))
    assert app_executables("/nonexistent/dev-state") == APP_EXECUTABLES


def test_an_inherited_development_build_is_removed_for_the_session():
    """A development build exported where pytest started would otherwise widen
    every test service; tests that mean one name their own."""
    assert "SUBFLEET_DEV_APP_EXECUTABLE" in SAFE_ENV_REMOVALS


# --- Properties ----------------------------------------------------------------

@pytest.fixture(scope="module")
def layout(tmp_path_factory):
    """A test home and a stand-in account, each with a `.subfleet`, real
    subdirectories for detours, and a symlink alias."""
    base = tmp_path_factory.mktemp("state-root").resolve()
    for name in ("person", "account"):
        for sub in (".subfleet", "dev-state", "projects", ".subfleet-dev", ".subfleet/nested"):
            (base / name / sub).mkdir(parents=True, exist_ok=True)
        (base / f"{name}-alias").symlink_to(base / name)
    (base / "other" / ".subfleet").mkdir(parents=True)
    (base / "other-alias").symlink_to(base / "other")
    return base


def segments(draw, here, avoid=()):
    """`.` segments and `<real dir>/..` detours that return to `here`."""
    out = ""
    for _ in range(draw(st.integers(0, 2))):
        real = sorted(child.name for child in here.iterdir()
                      if child.is_dir() and not child.is_symlink() and child.name not in avoid) \
            if here.is_dir() else []
        if real and draw(st.booleans()):
            out += "/" + draw(st.sampled_from(real)) + "/.."
        else:
            out += "/."
    return out


@st.composite
def spelled_paths(draw, base, start, names, avoid=()):
    """`base/<start or its alias>/<names...>` with doubled slashes, `.` segments
    and detours. No case variants: on a case-insensitive volume a case variant
    of the root is the same file but not the same resolved string, a gap
    release/217 already has (reported, not changed here)."""
    here = base / start
    out = str(base) + "/" * draw(st.integers(1, 2)) + draw(st.sampled_from([start, f"{start}-alias"]))
    out += segments(draw, here, avoid)
    for name in names:
        here = here / name
        out += "/" * draw(st.integers(1, 2)) + name + segments(draw, here, avoid)
    return out + draw(st.sampled_from(["", "/"]))


ROOTS = [(".subfleet",), ("dev-state",), (".subfleet-dev",), (".subfleet", "nested"),
         ("projects",), ("missing",)]


@PROPERTIES
@given(data=st.data(), names=st.sampled_from(ROOTS), start=st.sampled_from(["person", "other"]))
def test_the_development_build_is_refused_exactly_on_the_test_homes_root(layout, data, names, start):
    """Differential: the resolved-path comparison agrees with file identity. No
    spelling of the test home's `.subfleet` admits the development build, and
    every other root does."""
    home = data.draw(spelled_paths(layout, "person", ()), label="home")
    root = data.draw(spelled_paths(layout, start, names), label="root")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("HOME", home)
        patch.setenv("SUBFLEET_DEV_APP_EXECUTABLE", DEV)
        widened = app_executables(root) == (*APP_EXECUTABLES, DEV)
    production = identity(layout / "person" / ".subfleet")
    assert widened == (identity(root) != production), (home, root)


@PROPERTIES
@given(data=st.data())
def test_no_spelling_of_the_accounts_home_reaches_its_root(layout, data):
    # A HOME spelled through the root itself would name the store; not drawn.
    home = data.draw(spelled_paths(layout, "account", (), avoid=(".subfleet",)), label="home")
    with pytest.MonkeyPatch.context() as patch:
        as_account(patch, layout / "account")
        patch.setenv("HOME", home)
        patch.setenv("SUBFLEET_DEV_APP_EXECUTABLE", DEV)
        with untouchable(patch, layout / "account" / ".subfleet") as touched:
            with pytest.raises(RuntimeError, match=REFUSAL):
                app_executables(layout / "person" / "dev-state")
        assert touched == []
