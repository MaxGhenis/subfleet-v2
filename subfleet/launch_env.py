"""C-12.3/4, D-ENV1: select ambient names before adding launch authority.

No prefixes are trusted. In particular, a new SUBFLEET_* or LC_* name cannot
smuggle a credential through an otherwise safe configuration namespace.
"""

from collections.abc import Iterable, Mapping
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


def valid_name(name: object) -> bool:
    return isinstance(name, str) and _VARIABLE_NAME.fullmatch(name) is not None


def build_environment(additions: Mapping[str, str] | None = None,
                      removals: Iterable[str] = (), *,
                      pass_names: Iterable[str] = (),
                      environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Select parent names, honor default removals, then explicit authority.

    Callers supply only their own additions; never pass an ambient environment
    as additions. Explicit policy names authorize inheritance, not values or
    patterns. Reject malformed programmatic callers as well as policy files.
    """
    passed = frozenset(pass_names)
    if any(not valid_name(name) for name in passed):
        raise ValueError("environment pass-through requires exact variable names")
    parent = os.environ if environ is None else environ
    result = {name: parent[name] for name in ALLOWED_NAMES | passed if name in parent}
    for name in removals:
        if name not in passed:
            result.pop(name, None)
    result.update(additions or {})
    return result


def environment_report(environ: Mapping[str, str] | None = None) -> dict[str, list[str]]:
    """Doctor's daemon-side diagnostic: names only, no environment snapshot."""
    parent = os.environ if environ is None else environ
    return {"extra_names": sorted(set(parent) - ALLOWED_NAMES)}


def guardian_pass_args(environment: Mapping[str, str]) -> list[str]:
    """Authorize the already-selected child names across the guardian boundary.

    Call this before adding the guardian's PYTHONPATH bootstrap. A provider
    inherits PYTHONPATH only if the launch or policy deliberately supplied it.
    Values never appear in argv, launch records or diagnostics.
    """
    return [arg for name in sorted(environment)
            for arg in ("--env-pass", name)]
