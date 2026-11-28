"""Properties of the pytest exec guard for the login keychain's reader (D-ST1 round 6).

For every spelling that names the real `/usr/bin/security` on this volume, both
the early refusal and the launch guard refuse. For every spelling of another
program, neither does. PATH search agrees with `shutil.which`, but bare
`security` is always refused independently of the search result. Every
launch here is a synthetic audit event, which starts no program, and the
safety net in `tests.keychain_standin` stands behind the production guard.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from subfleet import credentials
from subfleet.adapters.base import AdapterError
from tests.keychain_standin import REAL_BINARY, SAFETY_HITS, StandIn

PROPERTIES = settings(max_examples=int(os.environ.get("SUBFLEET_EXEC_GUARD_EXAMPLES", "150")),
                      deadline=None, database=None, derandomize=True,
                      suppress_health_check=[HealthCheck.function_scoped_fixture,
                                             HealthCheck.filter_too_much])
REFUSAL = "refusing personal keychain read under pytest"


def identity(path):
    try:
        found = os.stat(path)
    except (OSError, ValueError):
        return None
    return found.st_dev, found.st_ino


def detours(directory):
    """Real directories (not symlinks) inside `directory`: `<name>/..` returns to it."""
    names = ["usr", "bin", "sbin", "System", "Library", "lib", "libexec", "share"]
    return [name for name in names if os.path.isdir(os.path.join(directory, name))
            and not os.path.islink(os.path.join(directory, name))]


def letter(character):
    # APFS folds case, and the long s (U+017F) case-folds to "s".
    return st.sampled_from([character, character.upper(), *(["ſ"] if character == "s" else [])])


@st.composite
def spellings(draw, components):
    out, here = "/" * draw(st.integers(1, 3)), "/"
    for level, name in enumerate(components):
        for _ in range(draw(st.integers(0, 2))):
            options = ["dot", *(["detour"] if detours(here) else [])]
            if draw(st.sampled_from(options)) == "dot":
                out += "." + "/" * draw(st.integers(1, 2))
            else:
                out += draw(st.sampled_from(detours(here))) + "/.." + "/" * draw(st.integers(1, 2))
        out += "".join(draw(letter(character)) for character in name)
        here = os.path.join(here, name)
        if level < len(components) - 1:
            out += "/" * draw(st.integers(1, 3))
    return out


def launches(path):
    return [("subprocess.Popen", (path, [path], None, None)),
            ("os.exec", (path, [path], None)),
            ("os.posix_spawn", (path, [path], {}))]


@PROPERTIES
@given(spelling=spellings(("usr", "bin", "security")))
def test_every_spelling_of_the_real_reader_is_refused(spelling):
    real = identity(REAL_BINARY)
    if real is None:
        pytest.skip("no /usr/bin/security on this host")
    if identity(spelling) != real:
        return  # A case variant on a case-sensitive volume names nothing.
    hits = len(SAFETY_HITS)
    with pytest.raises(AdapterError, match=REFUSAL):
        credentials.keychain_command("fixture-ref", spelling)
    for event, details in launches(spelling):
        with pytest.raises(credentials.KeychainExecRefused, match=REFUSAL):
            sys.audit(event, *details)
    assert SAFETY_HITS[hits:] == []


@PROPERTIES
@given(spelling=spellings(("usr", "bin", "true")))
def test_no_spelling_of_another_program_is_refused(spelling):
    if identity(spelling) is None:
        return
    assert credentials.keychain_command("fixture-ref", spelling)[0] == spelling
    for event, details in launches(spelling):
        sys.audit(event, *details)


@pytest.fixture(scope="module")
def search_dirs(tmp_path_factory):
    base = tmp_path_factory.mktemp("exec-guard-path")
    found = StandIn(base / "standin")
    unexecutable = base / "unexecutable"
    unexecutable.mkdir()
    (unexecutable / "security").write_text("#!/bin/sh\n", encoding="utf-8")
    directory = base / "directory"
    (directory / "security").mkdir(parents=True)
    return {"standin": str(found.path.parent), "real": "/usr/bin", "bin": "/bin",
            "missing": str(base / "missing"), "unexecutable": str(unexecutable),
            "directory": str(directory)}, found


@PROPERTIES
@given(order=st.lists(st.sampled_from(["standin", "real", "bin", "missing", "unexecutable",
                                       "directory"]), max_size=6),
       name=st.sampled_from(["security", "true", "no-such-program-sf-r6"]))
def test_a_bare_name_is_found_as_which_finds_it(search_dirs, order, name):
    """The PATH finder agrees with which; bare security cannot be approved even
    when another hook could change the search result after this check."""
    dirs, found = search_dirs
    path = [dirs[key] for key in order]
    expected = shutil.which(name, path=os.pathsep.join(path))
    assert credentials._find_program(name, path) == expected
    details = (name, [name], None, {"PATH": os.pathsep.join(path)})
    if name == "security" or (expected is not None and identity(expected) == identity(REAL_BINARY)):
        with pytest.raises(credentials.KeychainExecRefused, match=REFUSAL):
            sys.audit("subprocess.Popen", *details)
    else:
        sys.audit("subprocess.Popen", *details)
    assert found.count() == 0
