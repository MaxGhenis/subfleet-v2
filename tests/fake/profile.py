"""The fake profile endpoint (C-10.6), shared by every harness.

`subfleet.adapters.claude` reaches `https://api.anthropic.com/api/oauth/profile`
through one function, `_urlopen(request, timeout) -> (status, body)`. Everything
here builds a stand-in for it, so no test ever sends a request or reads a real
credential. The fake `claude` binary serves the same answers, so an operator can
see by hand what a scenario will do.

Two ways to decide what a credential is told:

* By default the answer is *derived from the token*: a bearer ending in `-<n>`
  is account `e2e-account-<n>` in organisation `e2e-org-<n>`, labelled
  `lane<n>@example.test`. A roster whose lanes record those identities verifies,
  with no fixture to keep in step.
* `SUBFLEET_FAKE_PROFILE` overrides that with a fixture under
  `tests/fixtures/claude/identity/`. It is a comma-separated list of entries,
  each `<fixture>` (for every credential) or `<key>=<fixture>` (only for a bearer
  whose last `-`-separated segment is `<key>`). So `1=profile-mismatch` makes one
  lane's credential report another account and leaves the rest alone — the shape
  of the 2026-09-05 incident, where one credential in a healthy fleet belonged to
  somebody else.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

IDENTITY_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "claude" / "identity"
ENV_VAR = "SUBFLEET_FAKE_PROFILE"


def token_suffix(token: str) -> str:
    return str(token).rsplit("-", 1)[-1]


def derived_body(token: str) -> bytes:
    """The account a bearer belongs to when no fixture says otherwise."""
    number = token_suffix(token)
    return json.dumps({
        "account": {"email": f"lane{number}@example.test", "uuid": f"e2e-account-{number}"},
        "organization": {"uuid": f"e2e-org-{number}", "name": f"Lane {number}"},
    }).encode("utf-8")


def derived_identity(number: str | int) -> tuple[str, str]:
    """`(identity, label)` for a roster entry, as C-10.6 and C-1.4 record them."""
    return f"e2e-account-{number}:e2e-org-{number}", f"lane{number}@example.test"


def fixture_response(name: str) -> tuple[int, bytes]:
    """`(status, body)` for one fixture; raises OSError for the unreachable one."""
    payload = IDENTITY_DIR / f"{name}.json"
    if payload.is_file():
        return 200, payload.read_bytes()
    plain = IDENTITY_DIR / name
    if not plain.is_file():
        raise AssertionError(f"no profile fixture {name!r} under {IDENTITY_DIR}")
    text = plain.read_text(encoding="utf-8").strip()
    if text.isdigit():
        return int(text), b""
    raise OSError(text or "profile unavailable")


def chosen_fixture(token: str, spec: str | None) -> str | None:
    """Which fixture, if any, `SUBFLEET_FAKE_PROFILE` picks for this bearer."""
    if not spec:
        return None
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        key, sep, name = entry.partition("=")
        if not sep:
            return key
        if key == token_suffix(token):
            return name
    return None


def response_for(token: str, spec: str | None = None) -> tuple[int, bytes]:
    name = chosen_fixture(token, spec)
    return fixture_response(name) if name else (200, derived_body(token))


USAGE_ENV = "SUBFLEET_FAKE_USAGE"
USAGE_PATH = "/api/oauth/usage"


def usage_body(shared: float, fable: float | None = None) -> bytes:
    """The usage endpoint's payload as observed on 2026-09-06, in percent."""
    limits = [
        {"kind": "session", "group": "session", "percent": 10, "severity": "normal",
         "resets_at": "2026-09-06T17:00:00+00:00", "scope": None, "is_active": True},
        {"kind": "weekly_all", "group": "weekly", "percent": shared, "severity": "warning",
         "resets_at": "2026-09-10T16:00:00+00:00", "scope": None, "is_active": False},
    ]
    if fable is not None:
        limits.append({"kind": "weekly_scoped", "group": "weekly", "percent": fable, "severity": "normal",
                       "resets_at": "2026-09-10T16:00:00+00:00",
                       "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
                       "is_active": False})
    return json.dumps({
        "five_hour": {"utilization": 10.0, "resets_at": "2026-09-06T17:00:00+00:00"},
        "seven_day": {"utilization": float(shared), "resets_at": "2026-09-10T16:00:00+00:00"},
        "seven_day_opus": None, "nimbus_quill": {"utilization": 0.0, "resets_at": None},
        "extra_usage": {"utilization": None}, "limits": limits,
    }).encode("utf-8")


def usage_response(token: str, spec: str | None) -> tuple[int, bytes]:
    """What the usage endpoint (C-9.9) tells a bearer.

    `SUBFLEET_FAKE_USAGE` is a comma-separated list of `<key>=<value>` (or a bare
    `<value>` for every bearer): `<shared>/<fable>` percentages, `<shared>` alone
    for an account with no Fable window, or a status `401`, `403`, `429` (the
    last raises HTTPError with `Retry-After: 3035`, as the real endpoint did on
    2026-09-06). With no entry a bearer gets 403: a setup token has no usage
    scope, which is what every enrolled lane answered that day.
    """
    value = chosen_fixture(token, spec)
    if value is None or value == "403":
        return 403, b""
    if value == "401":
        return 401, b""
    if value == "429":
        import urllib.error
        from email.message import Message
        headers = Message()
        headers["Retry-After"] = "3035"
        raise urllib.error.HTTPError("https://api.anthropic.com" + USAGE_PATH, 429, "rate limited", headers, None)
    shared, _, fable = value.partition("/")
    return 200, usage_body(float(shared), float(fable) if fable else None)


def bearer(request) -> str:
    value = request.headers.get("Authorization") or ""
    return value.partition(" ")[2] or value


def opener(spec: str | None = None, *, seen: list | None = None):
    """A drop-in for `subfleet.adapters.claude._urlopen`."""
    if spec is None:
        spec = os.environ.get(ENV_VAR)

    usage_spec = os.environ.get(USAGE_ENV)

    def open_profile(request, timeout):
        token = bearer(request)
        if seen is not None:
            seen.append((request.full_url, token_suffix(token)))
        if request.full_url.endswith(USAGE_PATH):
            return usage_response(token, usage_spec)
        return response_for(token, spec)

    return open_profile


def install(spec: str | None = None) -> None:
    """Replace the adapter's one network call, in this process, for good."""
    from subfleet.adapters import claude

    claude._urlopen = opener(spec)
