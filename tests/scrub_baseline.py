"""The C-23.14 scrub list as `main` had it before PR #26 (`db46dca`), frozen as a test oracle.

C-23.14 says the header rules PR #26 added "only add to what is replaced". This is
what they add to: the rules and their order exactly as `subfleet/sessions/handoff.py`
had them. The property tests in `tests/unit/test_sessions_handoff.py` check the live
scrubber against it: it hides every credential this list hides, and it changes
nothing where this list changes nothing. Do not edit these rules to follow the live
ones; a change here moves the floor the live list is held to.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

PEM_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY(?: BLOCK)?-----.*?"
    r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY(?: BLOCK)?-----",
    re.DOTALL,
)
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_PREFIXED_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_-]{16,}|"
    r"github_pat_[A-Za-z0-9_]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|"
    r"glpat-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{16,}|"
    r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,}|"
    r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|"
    r"npm_[A-Za-z0-9]{16,}|hf_[A-Za-z0-9]{16,}"
    r")(?![A-Za-z0-9_-])"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*")
_DATA_URI_RE = re.compile(
    r"(?i)data:[a-z0-9.+/-]+(?:;[a-z0-9=.+/-]+)*;base64,[A-Za-z0-9+/=\s]{32,}"
)
_LONG_BASE64_RE = re.compile(
    r"(?<![A-Za-z0-9+/])(?:[A-Za-z0-9+/]{160,}={0,2})(?![A-Za-z0-9+/])"
)
_URL_PASSWORD_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s/@]+)(@)")
_HEADER_RE = re.compile(r"(?im)^(\s*(?:authorization|cookie|set-cookie)\s*:\s*).+$")
_SENSITIVE_KEY = (
    r"(?:(?:api[_-]?key|token|secret|password|passwd|authorization|cookie|"
    r"credential|credentials|private[_-]?key|signing[_-]?key|"
    r"secret[_-]?access[_-]?key|access[_-]?key[_-]?id|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|oauth[_-]?token|auth[_-]?token)|"
    r"(?:[A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*)[_-](?:api[_-]?key|token|secret|"
    r"password|passwd|private[_-]?key|signing[_-]?key))"
)
_QUOTED_ASSIGN_RE = re.compile(
    rf"(?im)(?P<prefix>[\"']?{_SENSITIVE_KEY}[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>[^\r\n]*?)(?P=quote)"
)
_PLAIN_ASSIGN_RE = re.compile(
    rf"(?im)(?P<prefix>[\"']?{_SENSITIVE_KEY}[\"']?\s*[:=]\s*)"
    r"(?P<value>[^\s,;\"']+)"
)


def scrub_secrets(text: str) -> tuple[str, int]:
    """`scrub_secrets` as `main` had it at `db46dca`."""
    total = 0
    for pattern, replacement in (
        (PEM_RE, "[PRIVATE KEY REDACTED]"),
        (_DATA_URI_RE, "[BASE64 DATA OMITTED]"),
        (_JWT_RE, REDACTED),
        (_PREFIXED_TOKEN_RE, REDACTED),
        (_BEARER_RE, "Bearer " + REDACTED),
        (_LONG_BASE64_RE, "[BASE64 OMITTED]"),
        (_HEADER_RE, lambda match: match.group(1) + REDACTED),
        (_URL_PASSWORD_RE, lambda match: match.group(1) + REDACTED + match.group(3)),
        (_QUOTED_ASSIGN_RE,
         lambda match: (match.group("prefix") + match.group("quote")
                        + REDACTED + match.group("quote"))),
        (_PLAIN_ASSIGN_RE, lambda match: match.group("prefix") + REDACTED),
    ):
        text, count = pattern.subn(replacement, text)
        total += count
    return text, total
