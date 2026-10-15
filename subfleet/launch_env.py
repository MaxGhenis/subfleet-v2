"""C-12.3/4, D-ENV1: select ambient names before adding launch authority.

Subfleet configuration includes harness hooks; credential words and launch
markers under that prefix still require explicit authority.
"""

from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
import os
import re


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
# Exact configuration names consumed by Subfleet, never launch/session markers.
CONFIG_NAMES = frozenset({
    "SUBFLEET_CLAUDE_DIR", "SUBFLEET_CLAUDE_SETTINGS", "SUBFLEET_CODEX_GUARD_CACHE",
    "SUBFLEET_CODEX_HOMES", "SUBFLEET_CODEX_UNIFIED_EXEC", "SUBFLEET_DESKTOP_LOG",
    "SUBFLEET_DEV_APP_EXECUTABLE", "SUBFLEET_GIT_TIMEOUT_S", "SUBFLEET_GUARD_TRUST",
    "SUBFLEET_HOOK_COMMAND", "SUBFLEET_HOOK_TIMEOUT_S", "SUBFLEET_NOTIFY_MODE",
    "SUBFLEET_PROVIDER_QOS", "SUBFLEET_RESET_INHIBIT", "SUBFLEET_RETENTION_QOS",
    "SUBFLEET_SESSION_STORE", "SUBFLEET_TICKLE", "CODEX_GUARD_PREFLIGHT_TIMEOUT",
})
ALLOWED_NAMES = OBSERVED_NAMES | LOCALE_NAMES | NETWORK_NAMES | CONFIG_NAMES
_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_CREDENTIAL_WORD = re.compile(r"(?:^|_)(?:TOKEN|KEY|SECRET|PASSWORD)(?:_|$)", re.IGNORECASE | re.ASCII)
# These identify a particular launch, not ambient daemon configuration.
LAUNCH_NAMES = frozenset({
    "SUBFLEET_ATTACHED_OK", "SUBFLEET_ATTEMPT", "SUBFLEET_JOB", "SUBFLEET_LANE",
    "SUBFLEET_LANE_ID", "SUBFLEET_PROBE", "SUBFLEET_PUSH_BUNDLE", "SUBFLEET_ROOT",
    "SUBFLEET_RUN_DETACH", "SUBFLEET_SESSION_ID", "SUBFLEET_TURN_JOB",
    "SUBFLEET_EXIT_SETTLE_S",  # guardian-only lifetime setting
})
_HELPER_PASS: ContextVar[tuple[str, ...]] = ContextVar("subfleet_helper_env_pass", default=())


def valid_name(name: object) -> bool:
    return isinstance(name, str) and _VARIABLE_NAME.fullmatch(name) is not None


def allowed_name(name: str) -> bool:
    return name in ALLOWED_NAMES or (
        name.startswith("SUBFLEET_") and valid_name(name)
        and name not in LAUNCH_NAMES and _CREDENTIAL_WORD.search(name) is None
    )


@contextmanager
def helper_policy(policy: Mapping):
    """Select helper authority for one operation; never mutate process globals.

    Workers enter their own scope. Providers always supply their policy's
    explicit names, so a helper scope cannot authorize a provider credential.
    """
    token = _HELPER_PASS.set(tuple(policy.get("env", {}).get("pass", ())))
    try:
        yield
    finally:
        _HELPER_PASS.reset(token)


def build_environment(additions: Mapping[str, str] | None = None,
                      removals: Iterable[str] = (), *,
                      pass_names: Iterable[str] | None = None,
                      environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Select parent names and explicit authority, then mandatory removals.

    Callers supply only their own additions; never pass an ambient environment
    as additions. Explicit policy names authorize inheritance, not values or
    patterns. Reject malformed programmatic callers as well as policy files.
    """
    passed = frozenset(_HELPER_PASS.get() if pass_names is None else pass_names)
    if any(not valid_name(name) for name in passed):
        raise ValueError("environment pass-through requires exact variable names")
    parent = os.environ if environ is None else environ
    result = {name: value for name, value in parent.items() if allowed_name(name) or name in passed}
    result.update(additions or {})
    for name in removals:
        result.pop(name, None)
    return result


def environment_report(environ: Mapping[str, str] | None = None) -> dict[str, list[str]]:
    """Doctor's daemon-side diagnostic: names only, no environment snapshot."""
    parent = os.environ if environ is None else environ
    return {"extra_names": sorted(name for name in parent if not allowed_name(name))}


def guardian_pass_args(environment: Mapping[str, str]) -> list[str]:
    """Authorize the already-selected child names across the guardian boundary.

    Call this before adding the guardian's PYTHONPATH bootstrap. A provider
    inherits PYTHONPATH only if the launch or policy deliberately supplied it.
    Values never appear in argv, launch records or diagnostics.
    """
    return [arg for name in sorted(environment)
            for arg in ("--env-pass", name)]
