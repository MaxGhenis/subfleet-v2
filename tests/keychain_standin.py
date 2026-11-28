"""A recording stand-in for macOS's `security`, and an independent exec safety net.

Keychain probes put the stand-in first on PATH, so a reader that gets past every
guard runs the stand-in and is counted. Behind that, the audit hook installed at
import refuses, and records, any launch that still resolves to the real binary,
or whose check cannot finish. It shares no code with `subfleet.credentials`, so a defect in the production
guard cannot disable it too. Nothing here ever starts `/usr/bin/security`.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import unicodedata

import pytest

REAL_BINARY = "/usr/bin/security"
STANDIN_BLOB = '{"claudeAiOauth":{"accessToken":"standin-token","subscriptionType":"standin-plan"}}'
_NET = "_subfleet_test_keychain_safety_net"


class SafetyNetTripped(AssertionError):
    """A launch resolved to the real binary past every production guard."""


def _folded(path: str) -> str:
    folded = unicodedata.normalize("NFD", os.path.realpath(path)).casefold()
    return unicodedata.normalize("NFD", folded)


def _real_identity():
    try:
        found = os.stat(REAL_BINARY)
    except OSError:
        return None
    return found.st_dev, found.st_ino


def _first_executable(name: str, path: str, base: str) -> str | None:
    for directory in path.split(os.pathsep):
        candidate = os.path.join(base, directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _candidates(event: str, details: tuple) -> list[str]:
    if event == "subprocess.Popen":
        name, _argv, cwd, env = details
        environments = [env]
    elif event == "os.posix_spawn":
        name, _argv, env = details
        cwd, environments = None, [env, None]
    elif event in ("os.exec", "os.spawn"):
        name, cwd, environments = details[0 if event == "os.exec" else 1], None, []
    else:
        return []
    name = os.fsdecode(name)
    if not os.path.dirname(name):
        folded = unicodedata.normalize("NFD", name).casefold()
        if unicodedata.normalize("NFD", folded) == "security":
            return [REAL_BINARY]  # Refuse before a later hook can change PATH.
    base = "" if cwd is None else os.fsdecode(cwd)
    found = [os.path.join(base, name)]
    if not os.path.dirname(name):
        # A launch searches PATH only for a bare name, so only then read env's.
        found += [_first_executable(name, os.pathsep.join(os.get_exec_path(env)), base)
                  for env in environments]
    return [target for target in found if target]


def _is_real(target: str, identity) -> bool:
    folded = _folded(target)
    if folded == REAL_BINARY or (os.path.basename(folded) == "security"
                                 and folded.startswith(("/usr/", "/.vol/"))):
        return True
    try:
        found = os.stat(target)
    except (OSError, ValueError):
        return False
    return identity is not None and (found.st_dev, found.st_ino) == identity


_IDENTITY = _real_identity()


def refusal(event: str, details: tuple) -> str | None:
    """What the safety net refuses for an audit event: the real binary's path, a
    note that the check cannot finish, or None. A check that cannot finish
    refuses, as the production guard's does."""
    try:
        return next((target for target in _candidates(event, details) if _is_real(target, _IDENTITY)), None)
    except Exception as error:  # noqa: BLE001 - any failure to judge is a refusal
        return f"{event}: the check cannot finish ({type(error).__name__})"


def _install() -> list[tuple[str, str]]:
    hits = getattr(sys, _NET, None)
    if hits is not None:
        return hits
    hits = []

    def safety(event, details, *, _refusal=refusal):
        target = _refusal(event, details)
        if target is not None:
            hits.append((event, target))
            raise SafetyNetTripped(f"test safety net: refusing {target}")

    sys.addaudithook(safety)
    setattr(sys, _NET, hits)
    return hits


#: Every launch the safety net refused in this process. Probes assert none.
SAFETY_HITS = _install()


class StandIn:
    """An executable named `security` that records each argv and prints a login blob."""

    def __init__(self, directory):
        directory.mkdir()
        self.log = directory / "invocations.log"
        self.path = directory / "security"
        self.path.write_text("#!/bin/sh\n"
                             f"printf '%s\\n' \"$*\" >> '{self.log}'\n"
                             f"printf '%s' '{STANDIN_BLOB}'\n", encoding="utf-8")
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)

    def count(self) -> int:
        try:
            return len(self.log.read_text(encoding="utf-8").splitlines())
        except FileNotFoundError:
            return 0

    def search_path(self, path: str | None = None) -> str:
        return f"{self.path.parent}{os.pathsep}{os.environ.get('PATH', '') if path is None else path}"


@pytest.fixture
def standin(tmp_path, monkeypatch):
    """The stand-in, first on PATH, with no inherited agent helper."""
    found = StandIn(tmp_path / "standin-bin")
    monkeypatch.setenv("PATH", found.search_path())
    monkeypatch.delenv("CLAUDE_LANE_AGENT_SECRET", raising=False)
    # A bare `security` must reach the stand-in before any probe runs.
    assert shutil.which("security") == str(found.path)
    hits = len(SAFETY_HITS)
    yield found
    assert SAFETY_HITS[hits:] == []


def treat_as_keychain_binary(monkeypatch, found: StandIn) -> None:
    """Have the production exec guard refuse the stand-in as the real binary.

    `raising=False` keeps a replay against a head without the guard meaningful:
    the probe then fails because the stand-in ran, not on a missing attribute.
    """
    from subfleet import credentials
    monkeypatch.setattr(credentials, "_STANDIN_KEYCHAIN_BINARIES", (str(found.path),),
                        raising=False)
