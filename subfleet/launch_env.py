"""C-12.3/4, D-ENV1: select ambient names before adding launch authority.

Ambient authority is a finite list of exact names, plus the test harness's
own prefixes, plus a live test session's own names (D-ST1). A credential word
anywhere in a name refuses it, listed or not, unless a reviewed exception says
why that name holds no secret.
"""

from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
import os
import re

from .home_paths import PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID, SAFE_HOME_PATHS, under_pytest


# The twenty names observed in the working launchd daemon on 2026-10-14.
OBSERVED_NAMES = frozenset({
    "HOME", "LOGNAME", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "OMP_NUM_THREADS",
    "OMP_WAIT_POLICY", "OPENBLAS_NUM_THREADS", "OSLogRateLimit", "PATH",
    "PYTEST_XDIST_AUTO_NUM_WORKERS", "SHELL", "SSH_AUTH_SOCK", "SSL_CERT_FILE",
    "SUBFLEET_HOME", "SUBFLEET_RETENTION_DORMANT", "TMPDIR", "USER",
    "VECLIB_MAXIMUM_THREADS", "XPC_FLAGS", "XPC_SERVICE_NAME",
})
LOCALE_NAMES = frozenset({
    "LANG", "LC_ALL", "LC_COLLATE", "LC_CTYPE", "LC_MESSAGES", "LC_MONETARY",
    "LC_NUMERIC", "LC_TIME", "TERM", "TZ", "__CF_USER_TEXT_ENCODING",
})
NETWORK_NAMES = frozenset({
    "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
})
# Every SUBFLEET_* configuration name the package reads (bin/codex included),
# and the guard preflight's timeout. tests/unit/test_launch_env_round3.py fails
# when the package spells a SUBFLEET_* name this, TEST_SESSION_NAMES,
# LAUNCH_NAMES or RETIRED_NAMES lacks.
CONFIG_NAMES = frozenset({
    "SUBFLEET_CLAUDE_DIR", "SUBFLEET_CLAUDE_SETTINGS", "SUBFLEET_CODEX_GUARD_CACHE",
    "SUBFLEET_CODEX_HOMES", "SUBFLEET_CODEX_UNIFIED_EXEC", "SUBFLEET_DAEMON_BIN",
    "SUBFLEET_DESKTOP_LOG", "SUBFLEET_DEV_APP_EXECUTABLE", "SUBFLEET_GIT_TIMEOUT_S",
    "SUBFLEET_GUARD_TRUST", "SUBFLEET_HOME", "SUBFLEET_HOOK_COMMAND",
    "SUBFLEET_HOOK_TIMEOUT_S", "SUBFLEET_NO_AUTOPICK", "SUBFLEET_NOTIFY_MODE",
    "SUBFLEET_PING_STDIN_WAIT_S", "SUBFLEET_PROVIDER_QOS", "SUBFLEET_RESET_INHIBIT",
    "SUBFLEET_RETENTION_DORMANT",
    "SUBFLEET_RETENTION_QOS", "SUBFLEET_SESSION_STORE", "SUBFLEET_TICKLE",
    "SUBFLEET_V1_BIN", "CODEX_GUARD_PREFLIGHT_TIMEOUT",
})
ALLOWED_NAMES = OBSERVED_NAMES | LOCALE_NAMES | NETWORK_NAMES | CONFIG_NAMES
# D-ST1 (C-20.6): a test session's markers and isolated store paths, as
# tests/conftest.py and the e2e harness set them: a flag, the session directory,
# its sentinel's identity, or a path beneath the session's own base. None holds
# a credential. Each is selected only while the parent carries the session
# marker and that session lives (home_paths.under_pytest), so every child a
# test launches keeps refusing the person's stores and expires with that
# session. Outside one none is selected: production keeps exactly the lists
# above, and CLAUDE_CONFIG_DIR stays a lane's own addition (C-12.4).
TEST_SESSION_NAMES = frozenset({PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID,
                                *SAFE_HOME_PATHS}) - ALLOWED_NAMES
# The names home_paths.under_pytest reads to decide whether an environment is
# in a live test session; selection reads these values and no others first.
SESSION_PROBE_NAMES = frozenset({PYTEST_MARKER, PYTEST_SESSION, PYTEST_SESSION_ID,
                                 "PYTEST_CURRENT_TEST"})
# Read only to say they are ignored: v1's state root and the retired API-lane
# override. Never inherited, so doctor names them.
RETIRED_NAMES = frozenset({"SUBFLEET_ALLOW_API_LANE", "SUBFLEET_STATE_DIR"})
# Desktop and repository tools read these locally. They are classified for
# discovery, but grant no provider/helper authority and are never ambient
# configuration. Swift's SUBFLEET_* compilation defines are not environment names.
LOCAL_TOOL_NAMES = frozenset({
    "SUBFLEET_OPEN_CONVERSATION",      # desktop-only initial navigation
    "SUBFLEET_SWIFT_NESTED_SANDBOX",  # local compiler invocation
    "SUBFLEET_FIX4_MUTANTS",          # frontend mutation probe directory
})
# Test-only hooks for the fake providers and the e2e and live harnesses. No
# production code reads a name under these (tests/unit/test_launch_env_round3.py).
HARNESS_PREFIXES = ("SUBFLEET_FAKE_", "SUBFLEET_E2E_")
HARNESS_NAMES = frozenset({"SUBFLEET_LIVE"})
# Backstop: a name holding one of these anywhere, in any case, is never ambient
# authority, listed or not. Exact policy names stay explicit authority.
CREDENTIAL_WORDS = (
    "TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "PASSPHRASE", "PASS", "PWD",
    "CREDENTIAL", "CRED", "AUTH", "OAUTH", "BEARER", "COOKIE", "JWT", "PAT",
    "PRIVKEY", "WEBHOOK",
)
CREDENTIAL_PATTERN = re.compile("|".join(CREDENTIAL_WORDS), re.IGNORECASE | re.ASCII)
# Reviewed: admitted names a word matches by accident. Each holds a path.
CREDENTIAL_EXCEPTIONS = frozenset({
    "PATH",                            # executable search path ("PAT")
    "SSH_AUTH_SOCK",                   # the agent's socket; its keys stay in the agent
    "SUBFLEET_FAKE_DIAGNOSTICS_PATH",  # where the fake Codex writes its report
    "SUBFLEET_FAKE_RELEASE_PATH",      # a file whose creation releases a fake provider
    # A test session's stand-in agent-secret executable ("SECRET"). Only a live
    # session selects it, and production never reads it (home_path test_only).
    "SUBFLEET_AGENT_SECRET",
})
_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
# These identify a particular launch, not ambient daemon configuration.
LAUNCH_NAMES = frozenset({
    "SUBFLEET_ATTACHED_OK", "SUBFLEET_ATTEMPT", "SUBFLEET_JOB", "SUBFLEET_LANE",
    "SUBFLEET_LANE_ID", "SUBFLEET_PROBE", "SUBFLEET_PUSH_BUNDLE", "SUBFLEET_ROOT",
    "SUBFLEET_RUN_DETACH", "SUBFLEET_SESSION_ID", "SUBFLEET_TURN_JOB",
    "SUBFLEET_EXIT_SETTLE_S",  # guardian-only lifetime setting
    # C-12.4: a publish-denied (review) launch's marker, which `subfleet hook
    # PreToolUse` reads; `review_publish.pinned_environment` adds it with the
    # git denial, so a parent's value never reaches any launch.
    "SUBFLEET_PUBLISH_DENIED",
})
_HELPER_PASS: ContextVar[tuple[str, ...]] = ContextVar("subfleet_helper_env_pass", default=())
# D-ENV2 (review r2 of #248, P3-2): each enclosing helper scope's source of the
# daemon's withheld names, outermost first; read at every `build_environment`.
_HELPER_WITHHELD: ContextVar[tuple[Callable[[], Iterable[str]], ...]] = ContextVar(
    "subfleet_helper_env_withheld", default=())


def valid_name(name: object) -> bool:
    return isinstance(name, str) and _VARIABLE_NAME.fullmatch(name) is not None


def harness_name(name: str) -> bool:
    return name in HARNESS_NAMES or (valid_name(name) and any(
        name.startswith(prefix) and len(name) > len(prefix) for prefix in HARNESS_PREFIXES))


def _no_credential_word(name: str) -> bool:
    return name in CREDENTIAL_EXCEPTIONS or CREDENTIAL_PATTERN.search(name) is None


def allowed_name(name: str) -> bool:
    """Ambient authority: a listed name or a harness hook, never a credential word."""
    return (name in ALLOWED_NAMES or harness_name(name)) and _no_credential_word(name)


def session_name(name: str) -> bool:
    """A test session's name (D-ST1), admitted only while that session lives."""
    return name in TEST_SESSION_NAMES and _no_credential_word(name)


def inherited_name(name: str) -> bool:
    """Whether some launch may inherit `name` from its parent without `env.pass`."""
    return allowed_name(name) or session_name(name)


#: Where a lane the startup checks name came from (review r4, P2-1). A refused
#: lanes.json row for a lane the store already holds is IGNORED (review r5, P3-1).
ROSTER, STORE, IGNORED = "lanes.json", "the store", "lanes.json, ignored"


def refused_reference(name: object) -> bool:
    """D-ENV1: an env-kind lane variable that can never hold that lane's credential.

    Some launch inherits it without policy (the allowlist, a harness hook or a
    test session's name), so it would reach every helper; or selection reads it
    to find a test session (`SESSION_PROBE_NAMES`, review r5, P3-3), before any
    name is withheld. Such a lane is refused in lanes.json and held in the
    store; it is no lane's credential reference.
    """
    return valid_name(name) and (inherited_name(name) or name in SESSION_PROBE_NAMES)


def refused_phrase(name: str) -> str:
    """Why `refused_reference(name)` holds, in words. A name, never a value."""
    if allowed_name(name):
        return "a variable every launch inherits"
    if session_name(name):
        return "a test session's variable, which every launch in that session inherits"
    return "a variable launch selection reads to find a test session"


def lane_reference(lane) -> tuple[str | None, str] | None:
    """An env-kind lane's (lane id, variable), from a Lane, a store row or a
    lanes.json row; None for any other kind or a variable that is no exact name."""
    if isinstance(lane, Mapping):
        credential = lane.get("credential")
        if not isinstance(credential, Mapping):
            credential = {"kind": lane.get("credential_kind"), "ref": lane.get("credential_ref")}
        lane_id, kind, ref = lane.get("lane_id"), credential.get("kind"), credential.get("ref")
    else:
        lane_id, kind, ref = lane.lane_id, lane.credential.kind, lane.credential.ref
    return (lane_id, ref) if kind == "env" and valid_name(ref) else None


def _present(parent: Mapping[str, str], names: Iterable[str]) -> dict[str, str]:
    """Each named value the parent still holds when it is read.

    `os.environ` copies its keys to iterate, so a name another thread pops
    between that copy and the read raises `KeyError`; it is skipped as absent
    (review r4, P3-2). Only the names given are read.
    """
    values = {}
    for name in names:
        try:
            values[name] = parent[name]
        except KeyError:
            continue
    return values


def _selected(parent: Mapping[str, str]):
    """The parent names the default selection admits, a live session's included.

    Whether the parent is in a live session is decided from the probe's own
    names alone, so no other parent value is read before its name is accepted.
    Session names need the session marker itself (review r4, P3-4): a bare
    `PYTEST_CURRENT_TEST` selects none, though `under_pytest` still counts it
    as a test process for its other callers, where that refuses more.
    """
    probe = _present(parent, (name for name in parent if name in SESSION_PROBE_NAMES))
    session = PYTEST_MARKER in probe and under_pytest(probe)
    return lambda name: allowed_name(name) or (session and session_name(name))


@contextmanager
def helper_policy(policy: Mapping, *, withheld: Callable[[], Iterable[str]] | None = None):
    """Select helper authority for one operation; never mutate process globals.

    Workers enter their own scope. Providers always supply their policy's
    explicit names, so a helper scope cannot authorize a provider credential.

    `withheld` returns the daemon's withheld names (D-ENV2, review r2 of #248,
    P3-2): its lanes' credential references and startup's ignored hooks. Every
    `build_environment` in the scope calls it and withholds what it returns, so
    a lane stored or held after the scope opened is covered too. A nested scope
    replaces the policy's names but keeps every outer scope's withheld names:
    `pass` grants authority, and withholding only removes it. A source an outer
    scope already carries (the gate's inside `Daemon.dispatch`) is read once per
    build, not once per scope (review r1 of #262).
    """
    token = _HELPER_PASS.set(tuple(policy.get("env", {}).get("pass", ())))
    outer = _HELPER_WITHHELD.get()
    sources = _HELPER_WITHHELD.set(outer + ((withheld,) if withheld is not None and withheld not in outer else ()))
    try:
        yield
    finally:
        _HELPER_WITHHELD.reset(sources)
        _HELPER_PASS.reset(token)


def _scope_withheld() -> frozenset[str]:
    """The withheld names of every enclosing helper scope, read now; none outside one."""
    return frozenset().union(*(source() for source in _HELPER_WITHHELD.get()))


def build_environment(additions: Mapping[str, str] | None = None,
                      removals: Iterable[str] = (), *,
                      pass_names: Iterable[str] | None = None,
                      environ: Mapping[str, str] | None = None,
                      withheld: Iterable[str] = ()) -> dict[str, str]:
    """Select parent names and explicit authority, then mandatory removals.

    Callers supply only their own additions; never pass an ambient environment
    as additions. Explicit policy names authorize inheritance, not values or
    patterns. `withheld` names, and inside a helper scope the daemon's withheld
    names (`helper_policy`), are never taken from the parent, even when
    allowlisted or passed: lane credential references, which reach their own
    lane only as an addition. Reject malformed programmatic callers as well as
    policy files.
    """
    passed = frozenset(_HELPER_PASS.get() if pass_names is None else pass_names)
    if any(not valid_name(name) for name in passed):
        raise ValueError("environment pass-through requires exact variable names")
    withheld = frozenset(withheld) | _scope_withheld()
    parent = os.environ if environ is None else environ
    selected = _selected(parent)
    result = _present(parent, (name for name in parent
                               if (selected(name) or name in passed) and name not in withheld))
    result.update(additions or {})
    for name in removals:
        result.pop(name, None)
    return result


def environment_report(environ: Mapping[str, str] | None = None, *,
                       references: Iterable[str] | None = None) -> dict[str, list[str]]:
    """Doctor's daemon-side diagnostic: names only, no environment snapshot.

    Every name the allowlist drops is listed, including each unlisted
    SUBFLEET_* name. Given the lanes' credential references, the ones the
    daemon holds are listed apart, as `lane_credentials`: they belong in the
    daemon, which withholds each from every launch but its own lane's.
    """
    parent = os.environ if environ is None else environ
    selected = _selected(parent)
    held = frozenset(() if references is None else references)
    report = {"extra_names": sorted(name for name in parent
                                    if not selected(name) and name not in held)}
    if references is not None:
        report["lane_credentials"] = sorted(name for name in parent if name in held)
    return report


def guardian_pass_args(environment: Mapping[str, str]) -> list[str]:
    """Authorize the already-selected child names across the guardian boundary.

    The guardian imports from its own cwd, without rewriting PYTHONPATH. A
    provider inherits it only if the launch or policy supplied it. Values
    never appear in argv, launch records or diagnostics.
    """
    return [arg for name in sorted(environment)
            for arg in ("--env-pass", name)]
