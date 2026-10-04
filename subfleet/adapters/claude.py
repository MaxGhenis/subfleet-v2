"""The Claude provider adapter (C-12.4 to C-12.6, C-9.8, C-10.2).

This adapter makes Claude lane capacity knowable. Every headless `claude -p` run
emits a `rate_limit_event` carrying the account's `unifiedWindows`, and that event
— not a cached table, not a token estimate — is the sensor. Everything else here
exists to get that event, to say honestly what it means, and to prove which model
actually served the turn.

Three rules hold throughout:

* **Fractions, never percentages.** A `Reading` carries `utilization` exactly as
  the server sent it, in [0, 1] (and legitimately above 1 when usage runs past a
  window's cap). Nothing in this module multiplies by 100 or formats a `%`;
  rendering belongs to the routing lane, and it must find nothing else to render.
* **Never a false positive.** Attestation returns `attested` only when every
  assistant message inside the attempt's own transcript range names the requested
  model. Ambiguity — no transcript, two transcripts, an unreadable one, no
  assistant turn — is `unattested`.
* **The credential is a value only inside a child's environment.** It is resolved
  in a private helper, placed in `Launch.env_add`, and never logged, never put in
  argv, never written to an artifact (C-10.5).

Classification order is authentication, then admission, then quota (C-9.2), with
one thing ahead of all three: the host CLI's own version gate. A CLI too old for a
model is the host's fault, never the lane's, and cooling a healthy lane for it
would be a lie about capacity.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..contracts import (
    GUESSED_CLOSURE_S, HEADLESS_MARKER, IDENTITY_EVIDENCE, READING_TTL_S, Attestation,
    AttestationResult, ClockSource, Closure, ClosureReason, Credential, ExitInfo,
    IdentityStatus, JobSpec, Lane, LaneInfo, Launch, Outcome, OutcomeClass, Reading,
    ReadingLabel, Sandbox,
)
from ..sessions.transcripts import NotRegularFile, open_regular, read_regular
from .base import Adapter, AdapterError
from .claude_stream import (
    AUTH_ERROR_KINDS, TRANSIENT_ERROR_KINDS, RateLimitInfo, StreamSummary, message_text,
    is_synthetic_api_error, model_answered, parse_lines, parse_stream,
)

# --- constants ---------------------------------------------------------------

PROVIDER = "claude"

#: The enrolment / probe turn (C-10.2), exactly as experiment-0 ran it.
ENROLL_MODEL = "claude-haiku-4-5-20251001"
ENROLL_PROMPT = "Reply with exactly: ok"
ENROLL_TIMEOUT_S = 180

#: The keychain item name pattern v1 established and v2 keeps (C-10.1).
KEYCHAIN_PREFIX = "claude-quota-"

#: C-10.6: the one endpoint that can say whose credential this is. Claude Code's
#: own profile loader reads `account.{email,uuid}` and `organization.uuid` here.
OAUTH_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
PROFILE_TIMEOUT_S = 15.0
#: C-9.9: the usage sensor that costs no model turn, read with the lane's own token.
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_USAGE_BETA = "oauth-2025-04-20"
SOURCE_OAUTH_USAGE = "oauth-usage"
USAGE_TIMEOUT_S = 15.0
#: `limits[].scope.model.display_name` -> policy model id for the model-scoped weekly
#: windows the usage endpoint reports (C-9.9, C-11.7). An unknown name keeps its
#: lower-cased display name as scope; the scheduler ignores scopes it has no model for.
SCOPED_MODEL_IDS = {"fable": "claude-fable-5-1", "opus": "claude-opus-5-5",
                    "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5-20251001"}
# Claude Code 2.1.278's own limit labels identify this as the Fable bucket,
# distinct from the all-model seven_day window (verified 2026-09-20).
SCOPED_RATE_LIMITS = {
    "seven_day_overage_included": SCOPED_MODEL_IDS["fable"],
    "seven_day_opus": SCOPED_MODEL_IDS["opus"],
    "seven_day_sonnet": SCOPED_MODEL_IDS["sonnet"],
}
PROFILE_MAX_BYTES = 1 << 20

#: The keychain item the Claude desktop app keeps its own login in — the same item
#: v1's `claude.keychain_credentials` reads, and read-only here (C-10.3).
DESKTOP_KEYCHAIN_REF = "Claude Code-credentials"

#: `ProfileResult.status`. Exactly four, so every caller can be exhaustive.
PROFILE_OK = "ok"                    # 200 with an account and an organization
PROFILE_NO_SCOPE = "no-scope"        # 403: a setup token, which cannot ask
PROFILE_UNAVAILABLE = "unavailable"  # network, timeout, 5xx, or any other status
PROFILE_INVALID = "invalid"          # 200 without the fields that name an account

#: `Reading.source` for anything the stream sensor produced.
SOURCE_RATE_LIMIT_EVENT = "rate_limit_event"

#: `Reading.window` for an `admission-observed` reading. Admission is not a quota
#: window: the reading says "this model was admitted (or refused) on this lane
#: just now", and carries no utilization at all (C-9.1, C-9.8).
ADMISSION_WINDOW = "admission"

#: Read-only tool surface, as v1 `bin/subfleet-claude` builds it.
READ_ONLY_TOOLS_BASE = "Read,Glob,Grep"
READ_ONLY_TOOLS_WEB = "Read,Glob,Grep,WebSearch,WebFetch"
EMPTY_MCP_CONFIG = '{"mcpServers":{}}'
#: C-12.9: the servers a writable attempt starts, written in its attempt directory.
ATTEMPT_MCP_CONFIG = "mcp-config.json"
#: The most of a job's MCP config a launch reads.
MCP_CONFIG_MAX_BYTES = 4 << 20

#: Removed from the child's environment: a lane bills the subscription through the
#: pinned OAuth token, never the API meter (C-12.4).
ENV_REMOVE: tuple[str, ...] = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

HEADLESS_BLOCK = f"""{HEADLESS_MARKER}
## HEADLESS EXECUTION (binding — read before anything else)
You are running as a headless `claude -p` session: there are NO task notifications,
NO background-task completions, and NO later turns. The moment you say "standing by"
or "waiting on notifications" that message becomes your final output and every piece
of background work is orphaned. Therefore:
- NEVER use run_in_background, Monitor, or any "wait for a notification" pattern. Run
  every command synchronously (foreground, with an adequate timeout) and read its
  result in the same step.
- Resume from your own journal (PROGRESS.md and the workspace's current state,
  including any staged or uncommitted work) rather than resetting it.
- Your FINAL message must be the completed deliverable, never a status update.
"""


# --- small shared helpers ----------------------------------------------------


def iso_utc(when: datetime) -> str:
    """C-1.7: ISO 8601 UTC with a `Z` suffix and second precision."""
    return when.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def iso_from_epoch(epoch: int | float | None) -> str | None:
    """Provider epoch seconds to the store's timestamp form (C-1.7)."""
    if epoch is None:
        return None
    try:
        return iso_utc(datetime.fromtimestamp(float(epoch), tz=timezone.utc))
    except (ValueError, OSError, OverflowError):
        return None


def encode_project_dir(workdir: str | Path) -> str:
    """Claude Code's `~/.claude/projects/` directory name for a working directory.

    Each of `/`, `.` and `_` becomes `-`; case is preserved. For example,
    `/workspace/team/project_name` maps to `-workspace-team-project-name`.
    """
    text = str(workdir)
    for char in ("/", ".", "_"):
        text = text.replace(char, "-")
    return text


_RESET_CLOCK_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*([ap])\.?m\.?(?:\s*\(([A-Za-z_]+/[A-Za-z_]+)\))?",
    re.IGNORECASE,
)


def parse_reset_clock(text: str, event_time: datetime) -> datetime | None:
    """`resets 6:40pm (America/New_York)` to an absolute instant (ported from v1
    `subfleet/util.py`): the same day as the event in the stated zone, else the
    local zone, rolled forward a day when that clock already passed."""
    match = _RESET_CLOCK_RE.search(text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 12 or minute > 59:
        return None
    meridiem, zone_name = match.group(3).lower(), match.group(4)
    if hour == 12:
        hour = 0
    if meridiem == "p":
        hour += 12
    try:
        zone = ZoneInfo(zone_name) if zone_name else None
    except Exception:
        zone = None
    local_event = event_time.astimezone(zone) if zone else event_time.astimezone()
    reset = local_event.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset < local_event:
        reset += timedelta(days=1)
    return reset


_EPOCH_LIMIT_RE = re.compile(r"usage limit reached\|(\d{9,11})", re.IGNORECASE)

# The host CLI refuses a model newer than itself. Both wordings are real: the first
# is quoted in v1 `bin/subfleet-claude` from Claude Code 2.1.228, the second is the
# current copy in the installed 2.1.260 string table.
CLI_TOO_OLD_RE = re.compile(
    r"does not support this model|Update Claude Code to use this model"
    r"|is older than the minimum version required by your organization",
    re.IGNORECASE,
)

# An explicit organisation block: the account exists and still cannot serve lanes.
ORG_BLOCK_RE = re.compile(
    r"organization has disabled Claude subscription access"
    r"|organization has disabled|subscription access for Claude Code"
    r"|does not have access to Claude",
    re.IGNORECASE,
)

# Credential-shaped signatures. On their own these are NOT enough: C-9.3 requires a
# 401 from a usage endpoint, an organisation block, or a revoked refresh token, and
# a limit-looking phrase alongside a successful `system/init` is `limited`.
AUTH_SIGNATURE_RE = re.compile(
    r"\b401\b|unauthoriz|authentication_error|authentication failed"
    r"|oauth token|token (?:has )?(?:been )?(?:revoked|expired)"
    r"|refresh token was revoked|invalid[ _-]?api[ _-]?key|not logged in|please run /login",
    re.IGNORECASE,
)

# A model-scoped exhaustion: the account still serves other models.
CREDITS_RE = re.compile(
    r"out of usage credits|monthly spend limit|out of extra usage"
    r"|requires usage credits|usage credit limit",
    re.IGNORECASE,
)

# An account-scoped window limit.
LIMIT_RE = re.compile(
    r"hit your limit|reached your limit|usage limit|session limit|weekly limit"
    r"|5-hour limit|subscription limit|limit reached",
    re.IGNORECASE,
)

# Sentences that contain limit vocabulary while denying a limit. Removed before the
# limit patterns run, so a throttled server never cools a healthy lane. The first is
# verbatim from the Claude Code 2.1.260 string table.
NON_LIMIT_PHRASES = (
    "(not your usage limit)",
    "not your usage limit",
)

TRANSIENT_RE = re.compile(
    r"\b5\d\d\b|\b429\b|too many requests|overload|at capacity|temporarily"
    r"|disconnect|connection|ECONN\w*|ETIMEDOUT|timed ?out|internal server error"
    r"|service unavailable|stream (?:closed|ended|interrupted)|socket hang up",
    re.IGNORECASE,
)

# Never a lane fault, never a retry: the model declined.
REFUSAL_STOP_REASON = "refusal"


def _scrub_non_limit(text: str) -> str:
    for phrase in NON_LIMIT_PHRASES:
        text = re.sub(re.escape(phrase), " ", text, flags=re.IGNORECASE)
    return text


def _first_line_containing(corpus: str, match: re.Match[str]) -> str:
    """The line the match landed on, trimmed, so a `detail` names its own evidence."""
    start = corpus.rfind("\n", 0, match.start()) + 1
    end = corpus.find("\n", match.end())
    line = corpus[start : end if end != -1 else len(corpus)].strip()
    return line[:300] if line else match.group(0)


def model_matches_requested(served: str | None, requested: str | None) -> bool:
    """v1's `model_matches_requested`, ported.

    A served id matches when it is the requested id, when it is the requested id
    with a dated or versioned suffix, or — when the request used a short alias
    (`opus`, `fable`) — when it is that alias with the `claude-` prefix and any
    suffix. Anything else is a mismatch; `None` never matches.
    """
    if not served or not requested:
        return False
    if served == requested:
        return True
    if served.startswith(f"{requested}-"):
        return True
    if requested in ("fable", "opus", "sonnet", "haiku"):
        return served in (requested, f"claude-{requested}") or served.startswith(
            f"claude-{requested}-"
        )
    return False


def _atomic_write_text(path: Path, text: str) -> None:
    """Temp file in the destination directory, fsync, rename, fsync the directory
    (C-8.1). The prompt actually sent is an artifact; a torn one is not acceptable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def apply_headless_block(prompt: str) -> str:
    """C-6.7: prepend the headless block unless the prompt already carries the marker.

    `prompt.md` is never touched; this produces the bytes written to
    `prompt.sent.md`, which is the launch's `stdin_path`.
    """
    if HEADLESS_MARKER in prompt:
        return prompt
    return f"{HEADLESS_BLOCK}\n{prompt}"


# --- identity (C-1.4, C-10.3, C-10.6, C-10.7) --------------------------------


def _urlopen(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    """The one place this module reaches the network; replaced whole in tests.

    Returns `(status, body)` so an opener can be a two-line function. A response
    body is capped: a profile is a few hundred bytes and nothing here should be
    able to spend memory on an unexpected reply.
    """
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https URL)
        return int(response.status), response.read(PROFILE_MAX_BYTES)


@dataclass(frozen=True)
class UsageResult:
    """What the usage endpoint said about a lane's windows (C-9.9). Never carries
    the token or an exception's text."""

    status: str                 # ok | rate-limited | auth-dead | no-scope | unavailable | identity-unbound
    readings: tuple[Reading, ...] = ()
    limit_reached: bool | None = None
    retry_after_s: int | None = None
    detail: str | None = None

    def as_probe(self) -> dict[str, Any]:
        """The shape `timers._read_probe` stores: status, readings, limit_reached."""
        return {"status": self.status, "readings": self.readings,
                "limit_reached": self.limit_reached, "retry_after_s": self.retry_after_s,
                "detail": self.detail}


#: Claude Code on macOS keeps a config directory's login in the keychain, not in
#: `.credentials.json`: service `Claude Code-credentials-<sha256(CLAUDE_CONFIG_DIR)[:8]>`,
#: same JSON shape as the file (observed 2026-09-06 with three fresh logins).
KEYCHAIN_HOME_PREFIX = "Claude Code-credentials-"


def keychain_service_for_home(home: str | Path) -> str:
    digest = hashlib.sha256(str(Path(home).expanduser()).encode("utf-8")).hexdigest()[:8]
    return KEYCHAIN_HOME_PREFIX + digest


def _keychain_blob(service: str, *, security_bin: str = "security") -> str | None:
    """One targeted keychain read of a login blob; None when absent or unreadable.
    The value is returned to the caller and never logged (C-10.5)."""
    try:
        done = subprocess.run([security_bin, "find-generic-password", "-s", service, "-w"],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    value = done.stdout.strip()
    return value or None


def home_login(home: str | Path) -> dict[str, Any] | None:
    """The `claudeAiOauth` block of a config directory's login, from its
    `.credentials.json` or, on macOS, its keychain item. None when there is none."""
    path = Path(home).expanduser()
    text: str | None = None
    try:
        # Only a regular file, never waiting in open(): a keepalive or heal turn
        # reads it on the timers' worker, which Timers.stop() waits for.
        text = read_regular(path / ".credentials.json").decode("utf-8")
    except (OSError, UnicodeError):
        text = _keychain_blob(keychain_service_for_home(path))
    if not text:
        return None
    try:
        blob = json.loads(text)
    except ValueError:
        return None
    oauth = blob.get("claudeAiOauth") if isinstance(blob, dict) else None
    return oauth if isinstance(oauth, dict) else None


def login_expired(oauth: Mapping[str, Any] | None, now_ms: float) -> bool:
    """True when the login's access token has an `expiresAt` (ms epoch) in the past."""
    if not oauth:
        return False
    expires = oauth.get("expiresAt")
    return isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires <= now_ms


def _iso_or_none(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return iso_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


@dataclass(frozen=True)
class ProfileResult:
    """What `https://api.anthropic.com/api/oauth/profile` said about a credential.

    Never carries the token, and never carries an exception's text: a urllib
    exception can quote the request headers, and those hold the bearer (C-10.5).
    """

    status: str
    email: str | None = None
    account_uuid: str | None = None
    org_uuid: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == PROFILE_OK

    @property
    def identity(self) -> str | None:
        """C-1.4, C-10.6: `<account_uuid>:<org_uuid>`, or nothing at all."""
        if not self.ok or not self.account_uuid or not self.org_uuid:
            return None
        return f"{self.account_uuid}:{self.org_uuid}"

    @property
    def triple(self) -> dict[str, str | None]:
        """The identity as the incident record wrote it (email, account, org)."""
        return {"email": self.email, "account_uuid": self.account_uuid,
                "org_uuid": self.org_uuid}


def identity_pair(account_uuid: str | None, org_uuid: str | None) -> str | None:
    if not account_uuid or not org_uuid:
        return None
    return f"{account_uuid}:{org_uuid}"


@dataclass(frozen=True)
class IdentityCheck:
    """One answer to "does this credential belong to the lane it is bound to?"

    Every Claude lane is checked on every reading it produces. A lane that has
    recorded no identity and no label is `unverified` without a request being
    made: there is nothing an answer could be compared against (C-10.6).
    `status` is `None` only for a provider that has no identity binding at all.
    """

    status: IdentityStatus | None
    profile_status: str | None
    expected: str | None
    observed: str | None
    observed_email: str | None
    observed_account_uuid: str | None
    observed_org_uuid: str | None
    checked_at: str

    @property
    def checked(self) -> bool:
        return self.status is not None

    @property
    def binds(self) -> bool:
        """C-10.6: may the readings this run produced be stored as capacity?"""
        return self.status in (None, IdentityStatus.VERIFIED, IdentityStatus.ENROLLED)

    def evidence(self) -> dict[str, Any] | None:
        """The record C-10.6 keeps instead of a reading, in the clause's words."""
        if self.status is None:
            return None
        return {
            "status": IDENTITY_EVIDENCE[self.status],
            "profile_status": self.profile_status,
            "checked_at": self.checked_at,
            "expected": self.expected,
            "identity": {"email": self.observed_email,
                         "account_uuid": self.observed_account_uuid,
                         "org_uuid": self.observed_org_uuid},
        }


# --- the adapter -------------------------------------------------------------


class ClaudeAdapter(Adapter):
    """Claude Code, driven headless on one enrolled subscription lane."""

    provider = PROVIDER

    def __init__(
        self,
        *,
        claude_bin: str = "claude",
        runner: Callable[..., Any] = subprocess.run,
        now: Callable[[], datetime] | None = None,
        new_session_id: Callable[[], str] | None = None,
        projects_dir: str | Path | None = None,
        security_bin: str = "security",
        profile_opener: Callable[[urllib.request.Request, float], tuple[int, bytes]] | None = None,
        reading_ttl_s: int = READING_TTL_S,
        usage_opener: Callable[[urllib.request.Request, float], tuple[int, bytes]] | None = None,
    ) -> None:
        self.claude_bin = claude_bin
        self._runner = runner
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._new_session_id = new_session_id or (lambda: str(uuid.uuid4()))
        self._projects_dir = Path(projects_dir) if projects_dir else None
        self._security_bin = security_bin
        self._profile_opener = profile_opener
        self._reading_ttl_s = reading_ttl_s
        self._usage_opener = usage_opener
        # C-10.6: one profile request per credential per reading window, so the
        # identity beside a reading was fetched in the same probe cycle. Keyed by
        # a digest of the token: the cache never holds the credential itself.
        self._profile_cache: dict[str, tuple[datetime, ProfileResult]] = {}

    def model_answered(self, event: object) -> bool:
        """C-6.14, C-4.5: `claude_stream.model_answered`."""
        return model_answered(event)

    # --- credentials (C-10.5) ------------------------------------------------

    def _resolve_keychain_token(self, ref: str) -> str:
        """One targeted keychain item read. The value is returned to the caller and
        never logged, never stored, never placed in argv."""
        from ..credentials import keychain_command
        try:
            done = self._runner(
                keychain_command(ref, self._security_bin),
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            raise AdapterError(
                f"claude: could not read the keychain item {ref}",
                code=5,
                fix=f"agent-secret get {ref}",
            ) from None
        if getattr(done, "returncode", 1) != 0:
            raise AdapterError(
                f"claude: no keychain token for {ref}",
                code=5,
                fix=(
                    "claude setup-token while signed into the lane account, then store "
                    f"it as the keychain item {ref}"
                ),
            )
        payload = (done.stdout or "").strip()
        if not payload:
            raise AdapterError(
                f"claude: the keychain item {ref} is empty", code=5,
                fix=f"re-enrol the lane: claude setup-token, then store it as {ref}",
            )
        # v1 stores Claude Code's own OAuth blob under one item name and a bare
        # setup-token under the per-lane `claude-quota-<email>` item. Accept both.
        if payload.startswith("{"):
            try:
                blob = json.loads(payload)
            except ValueError:
                return payload
            oauth = blob.get("claudeAiOauth") if isinstance(blob, dict) else None
            if isinstance(oauth, dict) and oauth.get("accessToken"):
                return str(oauth["accessToken"])
        return payload

    def _plan_from_keychain(self, ref: str) -> str | None:
        """The subscription tier, when the stored blob carries one. Best effort:
        a bare setup-token says nothing about the plan."""
        from ..credentials import keychain_command
        try:
            done = self._runner(
                keychain_command(ref, self._security_bin),
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if getattr(done, "returncode", 1) != 0:
            return None
        payload = (done.stdout or "").strip()
        if not payload.startswith("{"):
            return None
        try:
            blob = json.loads(payload)
        except ValueError:
            return None
        oauth = blob.get("claudeAiOauth") if isinstance(blob, dict) else None
        if isinstance(oauth, dict):
            value = oauth.get("subscriptionType")
            return str(value) if value else None
        return None

    def credential_env(self, credential: Credential) -> dict[str, str]:
        """The environment addition that authenticates one lane (C-12.4).

        The daemon normally builds this itself and hands it to `build_launch`; the
        adapter needs its own copy for `enroll`, which receives only the reference.
        """
        if credential.kind == "home":
            return {"CLAUDE_CONFIG_DIR": str(Path(credential.ref).expanduser())}
        if credential.kind == "keychain-token":
            return {"CLAUDE_CODE_OAUTH_TOKEN": self._resolve_keychain_token(credential.ref)}
        raise AdapterError(
            f"claude: unknown credential kind {credential.kind!r}",
            code=7,
            fix="a Claude lane is either kind 'keychain-token' or kind 'home'",
        )

    # --- identity (C-1.4, C-10.6) --------------------------------------------

    @staticmethod
    def _bearer(credential_env: Mapping[str, str] | None) -> str | None:
        """The token a profile request must carry, from the lane's own credential.

        A keychain or environment lane already has it under
        `CLAUDE_CODE_OAUTH_TOKEN`; a home lane keeps it in the config directory's
        own `.credentials.json` or, on macOS, in that directory's keychain item
        (`keychain_service_for_home`), both the provider CLI's store and only ever
        read here (C-23.47). Returns None rather than raising: an
        unanswerable profile is `unavailable`, not a crash.
        """
        token = (credential_env or {}).get("CLAUDE_CODE_OAUTH_TOKEN")
        if isinstance(token, str) and token.strip():
            return token.strip()
        home = (credential_env or {}).get("CLAUDE_CONFIG_DIR")
        if not home:
            return None
        oauth = home_login(home)
        value = oauth.get("accessToken") if oauth else None
        return str(value) if isinstance(value, str) and value.strip() else None

    def _fetch_profile(self, token: str) -> ProfileResult:
        """One GET, standard library only, 15 s, and nothing logged (C-10.5)."""
        request = urllib.request.Request(OAUTH_PROFILE_URL, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        })
        opener = self._profile_opener or _urlopen
        try:
            status, body = opener(request, PROFILE_TIMEOUT_S)
        except urllib.error.HTTPError as error:
            status, body = int(error.code), b""
        except (OSError, ValueError, TypeError, AttributeError) as error:
            # Deliberately only the exception's type: its text can quote the
            # request headers, and those hold the bearer.
            return ProfileResult(PROFILE_UNAVAILABLE, detail=type(error).__name__)
        if status == 403:
            # C-9.3: expected scope on a setup token, and never auth evidence.
            return ProfileResult(PROFILE_NO_SCOPE, detail="http-403")
        if status != 200:
            # 401 included: C-9.3 reserves `auth-dead` for a 401 from a usage
            # endpoint, and this is not one. An unanswered profile is unverified.
            return ProfileResult(PROFILE_UNAVAILABLE, detail=f"http-{status}")
        try:
            payload = json.loads(body)
        except ValueError:
            return ProfileResult(PROFILE_INVALID, detail="unparseable body")
        account = payload.get("account") if isinstance(payload, dict) else None
        organization = payload.get("organization") if isinstance(payload, dict) else None
        values = [
            (account or {}).get("email") if isinstance(account, dict) else None,
            (account or {}).get("uuid") if isinstance(account, dict) else None,
            (organization or {}).get("uuid") if isinstance(organization, dict) else None,
        ]
        if not all(isinstance(value, str) and value.strip() for value in values):
            return ProfileResult(PROFILE_INVALID, detail="no account and organization")
        email, account_uuid, org_uuid = (str(value).strip() for value in values)
        return ProfileResult(PROFILE_OK, email=email, account_uuid=account_uuid,
                             org_uuid=org_uuid)

    def probe_profile(self, credential_env: Mapping[str, str] | None,
                      *, refresh: bool = False) -> ProfileResult:
        """C-10.6: who does this credential belong to, right now?

        Cached per credential for `READING_TTL_S`, so the probe cycle that reads a
        lane's usage and the identity check beside it are one question asked once.
        """
        token = self._bearer(credential_env)
        if not token:
            return ProfileResult(PROFILE_UNAVAILABLE, detail="no-token")
        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = self._now()
        cached = self._profile_cache.get(key)
        if cached is not None and not refresh:
            when, result = cached
            if 0 <= (now - when).total_seconds() <= self._reading_ttl_s:
                return result
        result = self._fetch_profile(token)
        self._profile_cache[key] = (now, result)
        return result

    def identity_check(self, identity: str | None, label: str | None,
                       credential_env: Mapping[str, str] | None) -> IdentityCheck:
        """C-10.6: compare the lane's recorded identity with the credential's own.

        "A usage reading is recorded only when the profile identity equals the
        lane's recorded identity" is a necessary condition, so a lane that has
        recorded nothing can never satisfy it: it is `identity-unverified`, and
        no request is made, because there is nothing an answer could be compared
        against. Re-enrolment is what binds such a lane (C-23.44).

        With something to compare against:

        * `no-scope` is C-10.6's setup-token carve-out and applies only to a lane
          that never recorded an identity — a lane that has one and now cannot
          answer has had its credential changed under it, which is the incident's
          own shape, and is `identity-unverified`.
        * An answer naming another account is `identity-mismatch`; anything the
          endpoint could not answer is `identity-unverified`. Neither is capacity.
        * A lane holding only a label is judged on that label, the one claim it
          has, and the identity it observes is returned so the caller can bind it
          (C-1.4) and judge the next cycle on uuids rather than on an email.
        """
        checked_at = iso_utc(self._now())
        if not identity and not label:
            return IdentityCheck(IdentityStatus.UNVERIFIED, None, None, None, None,
                                 None, None, checked_at)
        profile = self.probe_profile(credential_env or {})
        observed = profile.identity
        if profile.status == PROFILE_NO_SCOPE and not identity:
            status = IdentityStatus.ENROLLED
        elif profile.status != PROFILE_OK:
            status = IdentityStatus.UNVERIFIED
        elif identity:
            status = (IdentityStatus.VERIFIED if observed == identity
                      else IdentityStatus.MISMATCH)
        elif profile.email and label and profile.email.casefold() == label.casefold():
            status = IdentityStatus.VERIFIED
        else:
            status = IdentityStatus.MISMATCH
        return IdentityCheck(status, profile.status, identity or label, observed,
                             profile.email, profile.account_uuid, profile.org_uuid,
                             checked_at)

    def lane_identity_check(self, lane: Lane,
                            credential_env: Mapping[str, str] | None) -> IdentityCheck:
        return self.identity_check(lane.identity, lane.label, credential_env)

    @staticmethod
    def desktop_credential() -> Credential:
        """C-10.3: the desktop app's own login, as a reference and never a value."""
        return Credential(provider=PROVIDER, ref=DESKTOP_KEYCHAIN_REF,
                          kind="keychain-token")

    def probe_desktop_profile(self) -> ProfileResult:
        """C-10.3: ask the desktop app's credential who it is, every cycle.

        The keychain item is read, never written, and a missing or unreadable one
        is `unavailable` — an unknown desktop identity keeps the recorded desktop
        flags rather than silently removing protection.
        """
        try:
            env = self.credential_env(self.desktop_credential())
        except AdapterError as error:
            return ProfileResult(PROFILE_UNAVAILABLE, detail=f"keychain: {error.code}")
        return self.probe_profile(env)

    @staticmethod
    def account_from_reference(credential: Credential) -> str | None:
        """`claude-quota-<email>` yields the email; a home yields nothing (C-1.4)."""
        if credential.kind == "keychain-token" and credential.ref.startswith(KEYCHAIN_PREFIX):
            return credential.ref[len(KEYCHAIN_PREFIX):] or None
        return None

    @staticmethod
    def account_from_home(home: str | Path) -> str | None:
        """The account a Claude config directory is signed into, when it says so.

        `~/.claude.json` carries `oauthAccount.emailAddress` (C-10.3 reads the same
        file to find the desktop login). A keychain-token lane has no such file of
        its own, so it has nothing to verify against.
        """
        path = Path(home).expanduser() / ".claude.json"
        try:
            blob = json.loads(read_regular(path).decode("utf-8"))
        except (OSError, ValueError):                  # a UnicodeDecodeError is a ValueError
            return None
        account = blob.get("oauthAccount") if isinstance(blob, dict) else None
        if isinstance(account, dict):
            email = account.get("emailAddress") or account.get("email")
            return str(email) if email else None
        return None

    # --- the enrolment / probe turn -----------------------------------------

    def _turn_argv(self, model_id: str) -> list[str]:
        return [
            self.claude_bin, "-p", ENROLL_PROMPT,
            "--model", model_id,
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", "1",
        ]

    def _run_turn(self, env_add: dict[str, str], model_id: str,
                  timeout: int = ENROLL_TIMEOUT_S) -> tuple[int, str, str]:
        """One Haiku-sized turn under a lane credential, in a throwaway directory.

        Nothing is written anywhere but that directory by us; Claude Code keeps its
        own session transcript under its config directory, which is the provider's
        state and not ours to place.
        """
        env = {k: v for k, v in os.environ.items() if k not in ENV_REMOVE}
        env.update(env_add)
        with tempfile.TemporaryDirectory(prefix="subfleet-claude-probe-") as workdir:
            try:
                done = self._runner(
                    self._turn_argv(model_id),
                    cwd=workdir, env=env, capture_output=True, text=True,
                    timeout=timeout, stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as exc:
                return 124, _decode(getattr(exc, "output", "")), (
                    f"claude: probe timed out after {timeout}s"
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return 127, "", f"claude: could not run {self.claude_bin}: {exc}"
        return (
            int(getattr(done, "returncode", 1) or 0),
            _decode(getattr(done, "stdout", "")),
            _decode(getattr(done, "stderr", "")),
        )

    def enroll(self, credential: Credential) -> LaneInfo:
        """C-10.2: one Haiku turn under the credential, then read the sensor.

        Refuses with exit 5 when `system/init` never arrives: without it the
        credential never authenticated, whatever else the output says (C-9.3).
        """
        if credential.provider != PROVIDER:
            raise AdapterError(
                f"claude: credential is for provider {credential.provider!r}", code=2,
                fix="enrol a Claude credential with the Claude adapter",
            )
        env_add = self.credential_env(credential)
        rc, stdout, stderr = self._run_turn(env_add, ENROLL_MODEL)
        summary = parse_stream(stdout)
        corpus = f"{stderr}\n{chr(10).join(summary.texts())}"

        if not summary.has_init:
            detail = _first_auth_phrase(corpus) or f"rc {rc}, no system/init in the stream"
            raise AdapterError(
                f"claude: the credential did not authenticate ({detail})",
                code=5,
                fix=(
                    "claude setup-token while signed into the lane account, then store it "
                    f"as the keychain item {credential.ref}"
                ),
            )
        if ORG_BLOCK_RE.search(corpus):
            raise AdapterError(
                "claude: the organisation has disabled Claude Code subscription access "
                "for this account",
                code=5,
                fix="ask the account's admin to enable Claude Code access",
            )

        home = env_add.get("CLAUDE_CONFIG_DIR")
        account = self.account_from_reference(credential)
        observed = self.account_from_home(home) if home else None
        if account and observed and account.lower() != observed.lower():
            raise AdapterError(
                f"claude: the credential reference names {account} but the home is signed "
                f"into {observed}",
                code=7,
                fix="rebind the lane to the account its credential really holds (C-1.3)",
            )
        account = account or observed
        if not account:
            raise AdapterError(
                f"claude: cannot determine the account for credential {credential.ref!r}",
                code=2,
                fix=(
                    "name the keychain item claude-quota-<email>, or point the lane at a "
                    "home whose .claude.json carries oauthAccount.emailAddress"
                ),
            )

        plan = (
            self._plan_from_keychain(credential.ref)
            if credential.kind == "keychain-token" else None
        )

        # C-10.6: ask the credential itself who holds it, with the token the turn
        # above just used. The profile is the authority; the operator's label and
        # the keychain item's name are not. That is the whole lesson of the
        # 2026-09-05 incident, applied at the one moment an operator is watching.
        profile = self.probe_profile(env_add, refresh=True)
        if profile.status == PROFILE_INVALID:
            raise AdapterError(
                "claude: the profile endpoint answered without naming an account",
                code=7,
                fix=(
                    "retry enrolment; if it persists, the credential is not a Claude "
                    "Code OAuth token and cannot be bound to an account (C-10.6)"
                ),
            )
        if profile.ok:
            # C-1.4: the account key is the identity, and the email is a label.
            identity, label = profile.identity, profile.email
            identity_status = IdentityStatus.VERIFIED
            account_key = f"{PROVIDER}:{identity}"
        else:
            identity, label = None, account
            identity_status = (IdentityStatus.ENROLLED
                               if profile.status == PROFILE_NO_SCOPE
                               else IdentityStatus.UNVERIFIED)
            account_key = f"{PROVIDER}:{account}"

        # The lane does not exist yet, so its id is empty here; the daemon stamps the
        # id it assigns onto these readings when it inserts the lane row (C-10.1).
        readings = self.readings_from_summary(
            summary, lane_id="", model_id=ENROLL_MODEL, observed_at=iso_utc(self._now()),
        )
        if identity_status is IdentityStatus.UNVERIFIED:
            # C-10.6: a reading nobody can attribute is not capacity, at enrolment
            # exactly as during a probe cycle.
            readings = ()
        return LaneInfo(
            account_key=account_key, plan=plan, home=home, readings=readings,
            identity=identity, identity_status=identity_status.value, label=label,
        )

    # --- readings (C-9.1, C-9.8) --------------------------------------------

    def readings_from_rate_limit(
        self, info: RateLimitInfo | None, *, lane_id: str, model_id: str,
        observed_at: str, attempt_id: str | None = None,
    ) -> tuple[Reading, ...]:
        """C-9.8, exactly.

        `status: allowed` (and `allowed_warning`) yields one `provider` reading per
        `unifiedWindows` entry, scope `account`, `utilization` as the fraction the
        server sent and `resetsAt` converted to ISO 8601 UTC.

        `status: rejected` yields no utilization reading at all — only an
        `admission-observed` rejection for the requested model, carrying the event's
        `resetsAt` as its clock. A rejection's window numbers describe the window that
        did the refusing, and reporting them as headroom would be a lie; they are kept
        in the outcome's evidence instead.

        `overageStatus` is parsed (in `claude_stream`) and never read here: it is not
        admission evidence.
        """
        if info is None or info.status is None:
            return ()
        if info.rejected:
            return (
                Reading(
                    lane_id=lane_id,
                    # C-9.1: an admission-observed reading is about the model that was
                    # refused. With no model recorded, `account` is the only honest
                    # scope left; a reading never excludes a lane on its own (C-11.2
                    # excludes on closures), so this cannot over-reach.
                    scope=model_id or "account",
                    window=ADMISSION_WINDOW,
                    utilization=None,
                    resets_at=iso_from_epoch(info.resets_at),
                    label=ReadingLabel.ADMISSION_OBSERVED,
                    source=SOURCE_RATE_LIMIT_EVENT,
                    observed_at=observed_at,
                    attempt_id=attempt_id,
                ),
            )
        if not info.allowed:
            return ()
        readings = []
        for window in sorted(info.windows):
            value = info.windows[window]
            if (value.utilization is None or not math.isfinite(value.utilization)
                    or value.utilization < 0):
                continue
            scope = SCOPED_RATE_LIMITS.get(window, "account")
            readings.append(
                Reading(
                    lane_id=lane_id,
                    scope=scope,
                    window="seven_day" if window in SCOPED_RATE_LIMITS else window,
                    # The ledger represents exhaustion as 1; preserve the raw
                    # over-cap value in classification evidence and the stream.
                    utilization=min(1.0, value.utilization),
                    resets_at=iso_from_epoch(value.resets_at),
                    label=ReadingLabel.PROVIDER,
                    source=SOURCE_RATE_LIMIT_EVENT,
                    observed_at=observed_at,
                    attempt_id=attempt_id,
                )
            )
        return tuple(readings)

    def readings_from_summary(
        self, summary: StreamSummary, *, lane_id: str, model_id: str,
        observed_at: str, attempt_id: str | None = None,
    ) -> tuple[Reading, ...]:
        return self.readings_from_rate_limit(
            summary.rate_limit, lane_id=lane_id, model_id=model_id,
            observed_at=observed_at, attempt_id=attempt_id,
        )

    # --- the usage endpoint (C-9.9) ------------------------------------------

    def probe_usage(self, lane: Lane, credential_env: Mapping[str, str] | None, *,
                    scoped_models: Mapping[str, str] | None = None) -> UsageResult:
        """C-9.9: the lane's windows from `/api/oauth/usage`, no model turn spent.

        Account windows `five_hour` and `seven_day` become `provider` readings with
        scope `account`; every `limits[]` row of kind `weekly_scoped` becomes a
        `provider` reading whose scope is the policy model id for the row's
        display name (window `seven_day`). Utilization is normalised to a fraction.
        HTTP 429 yields no reading and the server's `Retry-After`; 401 is
        `auth-dead`; 403 is `no-scope`; anything else `unavailable`. C-10.6 holds:
        readings are returned only when the profile endpoint, asked with the same
        credential, binds the lane.
        """
        token = self._bearer(credential_env)
        if not token:
            return UsageResult("unavailable", detail="no-token")
        home = (credential_env or {}).get("CLAUDE_CONFIG_DIR")
        login = home_login(home) if home else None
        if login_expired(login, self._now().timestamp() * 1_000):
            # C-23.47: the CLI refreshes its own store; a heal turn under this home
            # does it. No request is sent with a token known to be expired.
            return UsageResult("expired-token", detail="access token past expiresAt")
        check = self.lane_identity_check(lane, credential_env)
        if not check.binds:
            return UsageResult("identity-unbound",
                               detail=check.status.value if check.status else None)
        request = urllib.request.Request(OAUTH_USAGE_URL, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_USAGE_BETA,
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        })
        opener = self._usage_opener or _urlopen
        retry_after: int | None = None
        try:
            status, body = opener(request, USAGE_TIMEOUT_S)
        except urllib.error.HTTPError as error:
            status, body = int(error.code), b""
            header = error.headers.get("Retry-After") if error.headers else None
            retry_after = int(header) if isinstance(header, str) and header.strip().isdigit() else None
        except (OSError, ValueError, TypeError, AttributeError) as error:
            # Only the exception's type: its text can quote the request headers.
            return UsageResult("unavailable", detail=type(error).__name__)
        if status == 429:
            return UsageResult("rate-limited", retry_after_s=retry_after, detail="HTTP 429")
        if status == 401:
            if login and login.get("refreshToken"):
                # A home lane with a refresh token: the access token lapsed, the
                # login did not. One heal turn (C-23.47) is the answer, not a latch.
                return UsageResult("expired-token", detail="HTTP 401 with a refresh token on file")
            return UsageResult("auth-dead", detail="HTTP 401")
        if status == 403:
            return UsageResult("no-scope", detail="HTTP 403")
        if status != 200:
            return UsageResult("unavailable", detail=f"HTTP {status}")
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return UsageResult("unavailable", detail="malformed")
        if not isinstance(payload, dict):
            return UsageResult("unavailable", detail="malformed")
        # Reserve admission treats a newer usage response as a complete snapshot:
        # an omitted scoped bucket means the account no longer has that bucket.
        # Never publish the shared quota from an incomplete/malformed snapshot,
        # which could otherwise erase the reserved model's headroom.
        limits = payload.get("limits")
        if not isinstance(limits, list) or any(
                not isinstance(item, dict) or item.get("kind") not in ("session", "weekly_all", "weekly_scoped")
                for item in limits):
            return UsageResult("unavailable", detail="malformed limits")
        observed_at = iso_utc(self._now())
        readings: list[Reading] = []
        for window in ("five_hour", "seven_day"):
            entry = payload.get(window)
            value = entry.get("utilization") if isinstance(entry, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 100:
                readings.append(Reading(lane.lane_id, "account", window, float(value) / 100.0,
                                        _iso_or_none(entry.get("resets_at")), ReadingLabel.PROVIDER,
                                        SOURCE_OAUTH_USAGE, observed_at))
        names = {k.lower(): v for k, v in (scoped_models or SCOPED_MODEL_IDS).items()}
        for limit in limits:
            if limit.get("kind") != "weekly_scoped":
                continue
            scope = limit.get("scope") if isinstance(limit.get("scope"), dict) else {}
            model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
            raw_name = model.get("display_name")
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            percent = limit.get("percent")
            if (name.lower() not in names or not isinstance(percent, (int, float)) or isinstance(percent, bool)
                    or not math.isfinite(percent) or not 0 <= percent <= 100):
                return UsageResult("unavailable", detail="malformed scoped window")
            readings.append(Reading(lane.lane_id, names[name.lower()], "seven_day",
                                    float(percent) / 100.0, _iso_or_none(limit.get("resets_at")),
                                    ReadingLabel.PROVIDER, SOURCE_OAUTH_USAGE, observed_at))
        limit_reached = any(r.scope == "account" and r.utilization is not None and r.utilization >= 1.0
                            for r in readings)
        return UsageResult("ok", tuple(readings), limit_reached)

    def probe_status(self, lane: Lane, credential_env: Mapping[str, str] | None) -> dict[str, Any]:
        """What the timer's probe cycle reads for a Claude lane: the usage endpoint
        (C-9.9), never a model turn. Same shape as the Codex adapter's."""
        return self.probe_usage(lane, credential_env).as_probe()

    def probe(self, lane: Lane, credential_env: dict[str, str]) -> tuple[Reading, ...]:
        """C-9.1, C-11.4: one Haiku turn on the lane, read the sensor, return
        readings. An unusable credential returns nothing rather than raising: the
        daemon's classifier, not the prober, decides a lane is dead."""
        return self.probe_with_model(lane, credential_env, ENROLL_MODEL)

    def probe_with_model(
        self, lane: Lane, credential_env: dict[str, str], model_id: str,
    ) -> tuple[Reading, ...]:
        """The same probe pinned to the model a job actually wants (C-11.4): before
        expensive work goes to an unmeasured lane, ask about *that* model, because a
        model-scoped exhaustion is invisible to a Haiku turn.

        C-10.6: the readings are returned only when the profile endpoint, asked
        with this same credential in this same cycle, names the lane's own
        account. A caller that needs to know *why* it got nothing uses
        `probe_outcome`, which carries the evidence."""
        _rc, stdout, _stderr = self._run_turn(credential_env, model_id)
        if not self.lane_identity_check(lane, credential_env).binds:
            return ()
        summary = parse_stream(stdout)
        return self.readings_from_summary(
            summary, lane_id=lane.lane_id, model_id=model_id,
            observed_at=iso_utc(self._now()),
        )

    def probe_outcome(
        self, lane: Lane, credential_env: dict[str, str], model_id: str,
    ) -> Outcome:
        """C-11.4: a probe the daemon can act on, not just read.

        "A `limited` result closes the scope; `ok` records `admission-observed`" needs
        a class and a closure, not a bare list of readings, so this runs the same turn
        and puts it through the same `classify` — one classification path, so a probe
        and a real attempt can never disagree about the same evidence.
        """
        rc, stdout, stderr = self._run_turn(credential_env, model_id)
        with tempfile.TemporaryDirectory(prefix="subfleet-claude-probe-out-") as tmp:
            attempt_dir = Path(tmp)
            (attempt_dir / "stream.jsonl").write_text(stdout, encoding="utf-8")
            (attempt_dir / "stderr").write_text(stderr, encoding="utf-8")
            summary = parse_stream(stdout)
            session_id = summary.session_id
            launch = Launch(
                argv=tuple(self._turn_argv(model_id)),
                env_add=dict(credential_env),
                env_remove=ENV_REMOVE,
                cwd=tmp,
                stdin_path=None,
                stdout_path=str(attempt_dir / "stdout"),
                stderr_path=str(attempt_dir / "stderr"),
                raw_stream_path=str(attempt_dir / "stream.jsonl"),
                native_session_id=session_id,
                notes={
                    "lane_id": lane.lane_id,
                    "account_key": lane.account_key,
                    "model_id": model_id,
                    "session_id": session_id,
                    "identity": lane.identity,     # C-10.6
                    "label": lane.label,
                    "probe": True,
                },
            )
            return self.classify(
                attempt_dir, launch,
                ExitInfo(rc=rc, signal=None, wall_s=0.0, child_pid=None),
            )

    # --- launch (C-12.4, C-6.7) ---------------------------------------------

    @staticmethod
    def permission_args(
        sandbox: Sandbox | str, *, isolated: bool = False, review_root: str | None = None,
        mcp_config: str = EMPTY_MCP_CONFIG,
    ) -> tuple[str, ...]:
        """`PERM_ARGS` as v1 `bin/subfleet-claude` builds them, in v1's order, with
        one documented change.

        `workspace-write` takes the bypass flag and, unlike v1 (C-12.9, d714),
        `--strict-mcp-config --mcp-config <mcp_config>`: no MCP server from any
        settings file, only the ones `mcp_config` names, and it names none unless
        the job did. `read-only` fails closed even when the operator's own settings
        default to `bypassPermissions`: plan mode plus a named tool surface, with
        settings sources, Chrome, MCP and slash commands all removed so nothing
        settings-driven can reintroduce a writing tool; `mcp_config` is never read
        there. An isolated review drops web access and gains the review root.
        """
        value = sandbox.value if isinstance(sandbox, Sandbox) else str(sandbox)
        if value == Sandbox.WORKSPACE_WRITE.value:
            return ("--dangerously-skip-permissions", "--strict-mcp-config", "--mcp-config", mcp_config)
        tools = READ_ONLY_TOOLS_BASE if isolated else READ_ONLY_TOOLS_WEB
        args = [
            "--permission-mode", "plan",
            "--tools", tools,
            "--allowedTools", tools,
            "--setting-sources", "",
            "--safe-mode",
            "--no-chrome",
            "--strict-mcp-config",
            "--mcp-config", EMPTY_MCP_CONFIG,
            "--disable-slash-commands",
        ]
        if isolated and review_root:
            args += ["--add-dir", review_root]
        return tuple(args)

    @staticmethod
    def mcp_config_arg(job: JobSpec, sandbox: str, attempt_dir: Path) -> str:
        """C-12.9: the `--mcp-config` value of this launch.

        No servers unless the job is writable and named some. Then exactly those,
        taken from the copy the daemon kept with the job (`job.mcp_config`) and
        written to this attempt's own file: a name the copy lacks fails the launch,
        and an entry the job did not name never reaches it.
        """
        names = tuple(job.mcp_servers or ())
        if sandbox != Sandbox.WORKSPACE_WRITE.value or not names:
            return EMPTY_MCP_CONFIG
        try:
            if not job.mcp_config:
                raise ValueError("the job records no MCP config")
            document = json.loads(read_regular(job.mcp_config, MCP_CONFIG_MAX_BYTES))
            servers = document["mcpServers"]
            missing = [name for name in names if not isinstance(servers.get(name), dict)]
            if missing:
                raise ValueError(f"it has no entry for {', '.join(missing)}")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise AdapterError(
                f"the job's MCP config cannot give the servers it names ({', '.join(names)}): {exc}",
                code=7, fix="submit the job again with --mcp naming servers its workdir offers",
            ) from None
        path = Path(attempt_dir) / ATTEMPT_MCP_CONFIG
        _atomic_write_text(path, json.dumps({"mcpServers": {name: servers[name] for name in names}},
                                            sort_keys=True) + "\n")
        return str(path)

    def expected_transcript_path(
        self, workdir: str | Path, session_id: str, credential_env: dict[str, str],
    ) -> Path:
        """`<config dir>/projects/<encoded workdir>/<session id>.jsonl`."""
        return (
            self._config_projects_dir(credential_env)
            / encode_project_dir(Path(workdir).resolve() if Path(workdir).exists()
                                 else workdir)
            / f"{session_id}.jsonl"
        )

    def _config_projects_dir(self, credential_env: dict[str, str] | None = None) -> Path:
        if self._projects_dir is not None:
            return self._projects_dir
        config = (credential_env or {}).get("CLAUDE_CONFIG_DIR") or os.environ.get(
            "CLAUDE_CONFIG_DIR"
        )
        base = Path(config).expanduser() if config else Path.home() / ".claude"
        return base / "projects"

    def _write_prompt_sent(self, attempt_dir: Path, prompt_path: Path) -> Path:
        # The task itself, as `read_text` read it (4 MB; none when missing), but a
        # prompt that is not a regular file fails the launch (as Codex's prepare
        # does), never a turn started with no task.
        try:
            with open_regular(prompt_path) as handle:
                prompt = handle.read(4_000_000).decode("utf-8", "replace")
        except FileNotFoundError:
            prompt = ""
        sent = attempt_dir / "prompt.sent.md"
        _atomic_write_text(sent, apply_headless_block(prompt))
        return sent

    def _launch_notes(
        self, *, lane: Lane, attempt_id: str, model_id: str, session_id: str,
        workdir: str, transcript: Path, sandbox: str, resumed_from: str | None = None,
    ) -> dict[str, Any]:
        try:
            offset = transcript.stat().st_size
        except OSError:
            offset = 0
        notes: dict[str, Any] = {
            "lane_id": lane.lane_id,
            "account_key": lane.account_key,
            # C-10.6: what this lane claims, so `classify` can ask the credential
            # itself whether the claim holds before any reading becomes capacity.
            "identity": lane.identity,
            "label": lane.label,
            "attempt_id": attempt_id,
            "model_id": model_id,
            "session_id": session_id,
            "workdir": workdir,
            "sandbox": sandbox,
            "transcript_path": str(transcript),
            "transcript_offset": offset,
            "projects_dir": str(transcript.parent.parent),
        }
        if resumed_from:
            notes["resumed_from"] = resumed_from
        return notes

    def build_launch(
        self, job: JobSpec, attempt_id: str, attempt_dir: Path, lane: Lane,
        credential_env: dict[str, str], model_id: str, effort: str | None,
        prompt_path: Path, guard_override: str | None,
    ) -> Launch:
        """C-12.4 and C-6.7.

        `guard_override` is accepted and unused: a Claude launch relies on the global
        never-rules hook in `~/.claude/settings.json`, and `doctor` reports when that
        hook is missing (C-14.3). There is no per-launch hook injection to make.
        """
        attempt_dir = Path(attempt_dir)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        session_id = self._new_session_id()
        sandbox = job.sandbox.value if isinstance(job.sandbox, Sandbox) else str(job.sandbox)

        argv: list[str] = [
            self.claude_bin, "-p",
            "--model", model_id,
            "--session-id", session_id,
            "--output-format", "stream-json",
            "--verbose",
        ]
        if effort:
            argv += ["--effort", effort]
        if job.isolated_review:
            from .isolation import validate_isolated_review
            validate_isolated_review(sandbox, job.review_root, {**os.environ, **credential_env})
        argv += list(self.permission_args(sandbox, isolated=job.isolated_review,
                                         review_root=job.review_root,
                                         mcp_config=self.mcp_config_arg(job, sandbox, attempt_dir)))
        from .isolation import claude_env_remove
        env_remove = (*ENV_REMOVE, *claude_env_remove({**os.environ, **credential_env})) if sandbox == "read-only" else ENV_REMOVE

        stdin_path = self._write_prompt_sent(attempt_dir, Path(prompt_path))
        transcript = self.expected_transcript_path(job.workdir, session_id, credential_env)
        return Launch(
            argv=tuple(argv),
            env_add=dict(credential_env),
            env_remove=env_remove,
            cwd=str(job.workdir),
            stdin_path=str(stdin_path),
            stdout_path=str(attempt_dir / "stdout"),
            stderr_path=str(attempt_dir / "stderr"),
            raw_stream_path=str(attempt_dir / "stream.jsonl"),
            native_session_id=session_id,
            notes=self._launch_notes(
                lane=lane, attempt_id=attempt_id, model_id=model_id,
                session_id=session_id, workdir=str(job.workdir), transcript=transcript,
                sandbox=sandbox,
            ),
        )

    def resume_launch(
        self, job: JobSpec, attempt_id: str, attempt_dir: Path, lane: Lane,
        credential_env: dict[str, str], native_session_id: str,
        prompt_path: Path, guard_override: str | None, model_id: str | None = None,
    ) -> Launch | None:
        """`claude -p --resume <session id>` on the same lane, same model, same
        permission flags. `--session-id` is not passed: `--resume` names the session.

        `model_id` is keyword-optional because the base signature has none; an attempt
        never changes model (C-4.6), so the daemon passes the attempt's model and the
        fallbacks are the job's pin, then the session's own recorded model.
        """
        attempt_dir = Path(attempt_dir)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        model = model_id or job.pinned_model
        sandbox = job.sandbox.value if isinstance(job.sandbox, Sandbox) else str(job.sandbox)

        argv: list[str] = [self.claude_bin, "-p", "--resume", native_session_id]
        if model:
            argv += ["--model", model]
        argv += ["--output-format", "stream-json", "--verbose"]
        if job.isolated_review:
            raise AdapterError("isolated review cannot resume a contextual Claude session",
                               fix="submit a fresh isolated review job")
        argv += list(self.permission_args(
            sandbox, mcp_config=self.mcp_config_arg(job, sandbox, attempt_dir)))
        from .isolation import claude_env_remove
        env_remove = (*ENV_REMOVE, *claude_env_remove({**os.environ, **credential_env})) if sandbox == "read-only" else ENV_REMOVE

        stdin_path = self._write_prompt_sent(attempt_dir, Path(prompt_path))
        transcript = self.expected_transcript_path(
            job.workdir, native_session_id, credential_env
        )
        return Launch(
            argv=tuple(argv),
            env_add=dict(credential_env),
            env_remove=env_remove,
            cwd=str(job.workdir),
            stdin_path=str(stdin_path),
            stdout_path=str(attempt_dir / "stdout"),
            stderr_path=str(attempt_dir / "stderr"),
            raw_stream_path=str(attempt_dir / "stream.jsonl"),
            native_session_id=native_session_id,
            notes=self._launch_notes(
                lane=lane, attempt_id=attempt_id, model_id=model or "",
                session_id=native_session_id, workdir=str(job.workdir),
                transcript=transcript, sandbox=sandbox, resumed_from=native_session_id,
            ),
        )

    # --- reading an attempt's artifacts --------------------------------------

    def raw_stream_text(self, attempt_dir: Path, launch: Launch | None = None) -> str:
        """The attempt's stream.

        `--output-format stream-json` puts the stream on stdout, so `stream.jsonl` and
        `stdout` are the same bytes; whichever the daemon actually produced is read,
        the dedicated stream file first.
        """
        attempt_dir = Path(attempt_dir)
        candidates: list[Path] = []
        if launch is not None and launch.raw_stream_path:
            candidates.append(Path(launch.raw_stream_path))
        candidates.append(attempt_dir / "stream.jsonl")
        if launch is not None and launch.stdout_path:
            candidates.append(Path(launch.stdout_path))
        candidates.append(attempt_dir / "stdout")
        for path in candidates:
            text = self.read_text(path)
            if text.strip():
                return text
        return ""

    def stream_summary(self, attempt_dir: Path, launch: Launch | None = None) -> StreamSummary:
        """Parse the complete attempt, including a terminal result beyond 4 MB.

        `Adapter.read_text` is a bounded diagnostic prefix, not an authoritative
        stream reader. Never use it to decide whether a long attempt succeeded.
        """
        attempt_dir = Path(attempt_dir)
        candidates = [Path(launch.raw_stream_path)] if launch and launch.raw_stream_path else []
        candidates.append(attempt_dir / "stream.jsonl")
        if launch and launch.stdout_path:
            candidates.append(Path(launch.stdout_path))
        candidates.append(attempt_dir / "stdout")
        for path in dict.fromkeys(candidates):
            try:
                with open_regular(path, "r", encoding="utf-8", errors="replace") as handle:
                    summary = parse_lines(handle)
            except (FileNotFoundError, NotRegularFile):
                continue
            if summary.lines_total:
                return summary
        return parse_stream("")

    def stderr_text(self, attempt_dir: Path, launch: Launch | None = None) -> str:
        attempt_dir = Path(attempt_dir)
        if launch is not None and launch.stderr_path:
            text = self.read_text(Path(launch.stderr_path))
            if text:
                return text
        return self.read_text(attempt_dir / "stderr")

    # --- classification (C-9.2 to C-9.5, C-9.8) ------------------------------

    def classify(self, attempt_dir: Path, launch: Launch, exit_info: ExitInfo) -> Outcome:
        """Authentication, then admission, then quota — with the host CLI's version
        gate ahead of all three (C-9.2).

        The raw rc and signal ride along with every class (C-9.2), and the readings the
        `rate_limit_event` yielded are attached whatever the class: a limited lane's
        capacity is exactly what the router most needs to know.
        """
        attempt_dir = Path(attempt_dir)
        notes = dict(launch.notes) if launch is not None else {}
        lane_id = str(notes.get("lane_id", ""))
        attempt = notes.get("attempt_id")
        model_id = str(notes.get("model_id") or "")
        now = self._now()
        observed_at = iso_utc(now)

        stderr = self.stderr_text(attempt_dir, launch)
        summary = self.stream_summary(attempt_dir, launch)
        info = summary.rate_limit

        readings = self.readings_from_rate_limit(
            info, lane_id=lane_id, model_id=model_id, observed_at=observed_at,
            attempt_id=attempt if isinstance(attempt, str) else None,
        )
        # C-10.6: the same credential that produced those readings is asked whose
        # it is. A lane that claims no identity is not asked and is unaffected; a
        # lane whose claim fails keeps the evidence and loses the capacity.
        identity = self.identity_check(
            notes.get("identity"), notes.get("label"),
            dict(launch.env_add) if launch is not None else {},
        )
        if not identity.binds:
            readings = ()
        session_id = (
            summary.session_id
            or (launch.native_session_id if launch is not None else None)
        )
        transcript = self._find_transcript(notes, session_id)
        served_model = _single_served_model(summary)

        corpus = "\n".join([stderr, *summary.texts()])
        scrubbed = _scrub_non_limit(corpus)

        evidence: dict[str, Any] = {
            "rc": exit_info.rc,
            "signal": exit_info.signal,
            "stream_lines": summary.lines_total,
            "stream_bad_lines": summary.bad_lines,
            "stream_truncated_tail": summary.truncated_tail,
            "system_init": summary.has_init,
            # C-4.5, C-6.14: whether the model answered at all, whatever the class;
            # `system_init` is no such evidence (the CLI writes it before any request).
            # None when no event was read: then nobody can say, and C-4.5 takes a
            # writable attempt to have answered.
            "model_answered": summary.answered if summary.lines_parsed else None,
            "error_kinds": list(summary.error_kinds),
            "unknown_event_types": list(summary.unknown_types),
        }
        if exit_info.spawn_error:
            evidence["spawn_error"] = exit_info.spawn_error
        if identity.checked:
            evidence["identity"] = identity.evidence()
        if info is not None:
            evidence["rate_limit"] = {
                "status": info.status,
                "rate_limit_type": info.rate_limit_type,
                "resets_at": iso_from_epoch(info.resets_at),
                "error_code": info.error_code,
                # Parsed independently and never treated as admission evidence (C-9.8).
                "overage_status": info.overage_status,
                "overage_disabled_reason": info.overage_disabled_reason,
                "is_using_overage": info.is_using_overage,
                "windows": {
                    key: {
                        "utilization": window.utilization,
                        "resets_at": iso_from_epoch(window.resets_at),
                    }
                    for key, window in sorted(info.windows.items())
                },
            }

        def finish(cls: OutcomeClass, detail: str, *, closure: Closure | None = None,
                   **more: Any) -> Outcome:
            evidence.update(more)
            return Outcome(
                cls=cls, detail=detail, evidence=evidence, readings=readings,
                closure=closure, native_session_id=session_id,
                transcript_path=str(transcript) if transcript else None,
                served_model=served_model,
            )

        # 0. The host CLI, not the lane. Cooling a healthy lane for this would be a
        #    lie about capacity, and rotating lanes cannot help (C-9.2 keeps the rc).
        match = CLI_TOO_OLD_RE.search(corpus)
        if match:
            return finish(
                OutcomeClass.CLI_TOO_OLD,
                f"cli-too-old: {_first_line_containing(corpus, match)}",
                answered={"cli": "version gate in the provider's own output"},
            )

        # 1. Authentication (C-9.3), from what the CLI and the provider wrote only
        #    (`cli_texts`): a model quoting these phrases is not the lane refusing,
        #    and an `auth-dead` job moves on to the next lane, disabling this one (C-4.5).
        cli_corpus = "\n".join([stderr, *summary.cli_texts()])
        match = ORG_BLOCK_RE.search(cli_corpus)
        if match:
            return finish(
                OutcomeClass.AUTH_DEAD,
                f"auth-dead: {_first_line_containing(cli_corpus, match)}",
                answered={"auth": "explicit organisation block"},
            )
        auth_kind = next(
            (kind for kind in summary.error_kinds if kind in AUTH_ERROR_KINDS), None
        )
        if auth_kind is not None:
            return finish(
                OutcomeClass.AUTH_DEAD,
                f"auth-dead: the provider reported error {auth_kind}",
                answered={"auth": f"provider error kind {auth_kind}"},
            )
        auth_signature = AUTH_SIGNATURE_RE.search(corpus)
        auth_false_positive: str | None = None
        if auth_signature is not None:
            cli_signature = AUTH_SIGNATURE_RE.search(cli_corpus)
            if not summary.has_init and cli_signature is not None:
                return finish(
                    OutcomeClass.AUTH_DEAD,
                    f"auth-dead: {_first_line_containing(cli_corpus, cli_signature)}",
                    answered={"auth": "no system/init and a credential failure signature"},
                )
            # C-9.3: the credential authenticated. Something else answered 401.
            auth_false_positive = auth_signature.group(0)
            evidence["auth_signature_false_positive"] = auth_false_positive

        # 2. Admission (C-9.8): the server's own verdict on this request.
        if info is not None and info.rejected:
            reported = iso_from_epoch(info.resets_at)
            until, clock_source = _closure_clock(reported, now)
            if info.error_code == "credits_required":
                scope, reason = (model_id or "account"), ClosureReason.CREDITS
                detail = (
                    f"limited: rate_limit_event status=rejected errorCode=credits_required "
                    f"for {scope}"
                )
            else:
                scope = SCOPED_RATE_LIMITS.get(info.rate_limit_type, "account")
                reason = ClosureReason.PROVIDER_LIMIT
                window = info.rate_limit_type or "account"
                detail = (
                    f"limited: rate_limit_event status=rejected rateLimitType={window}"
                )
            return finish(
                OutcomeClass.LIMITED, detail,
                closure=Closure(
                    lane_id=lane_id, scope=scope, until_at=until, reason=reason,
                    clock_source=clock_source, source_event=SOURCE_RATE_LIMIT_EVENT,
                ),
                answered={
                    "auth": "system/init" if summary.has_init else "not observed",
                    "admission": f"rate_limit_event.status={info.status}",
                },
            )

        # 3. Quota in words. The event may have been emitted before the refusal, or
        #    never: the text is then the only clock we have.
        match = CREDITS_RE.search(scrubbed)
        if match:
            line = _first_line_containing(scrubbed, match)
            until, clock_source = _closure_clock(
                _clock_from_text(scrubbed, now), now
            )
            scope = model_id or "account"
            return finish(
                OutcomeClass.LIMITED,
                f"limited: {line}",
                closure=Closure(
                    lane_id=lane_id, scope=scope, until_at=until,
                    reason=ClosureReason.CREDITS, clock_source=clock_source,
                    source_event="result-text",
                ),
                answered={
                    "auth": "system/init" if summary.has_init else "not observed",
                    "admission": (
                        f"rate_limit_event.status={info.status}" if info else
                        "no rate_limit_event"
                    ),
                    "quota": "model-scoped credit exhaustion in the provider's text",
                },
            )
        match = LIMIT_RE.search(scrubbed)
        if match:
            line = _first_line_containing(scrubbed, match)
            until, clock_source = _closure_clock(_clock_from_text(scrubbed, now), now)
            return finish(
                OutcomeClass.LIMITED,
                f"limited: {line}",
                closure=Closure(
                    lane_id=lane_id, scope="account", until_at=until,
                    reason=ClosureReason.PROVIDER_LIMIT, clock_source=clock_source,
                    source_event="result-text",
                ),
                answered={
                    "auth": "system/init" if summary.has_init else "not observed",
                    "admission": (
                        f"rate_limit_event.status={info.status}" if info else
                        "no rate_limit_event"
                    ),
                    "quota": "account-scoped window limit in the provider's text",
                },
            )

        # 4. The model declined. Not a lane fault and never a retry (C-4.5).
        if _refused(summary):
            return finish(
                OutcomeClass.CONTENT_FILTER,
                "content-filter: the turn ended with stop_reason refusal",
                answered={"content": "stop_reason=refusal"},
            )

        # 5. Success, and the one shape that looks like success and is not (C-12.6).
        text = self._final_text(summary, transcript, notes, session_id)
        if exit_info.rc == 0 and summary.result is not None and not summary.result.is_error:
            if text.strip():
                sensed = (
                    ", ".join(sorted(info.windows)) if info and info.windows else "none"
                )
                return finish(
                    OutcomeClass.OK,
                    f"ok: {len(text.encode('utf-8'))} bytes delivered; "
                    f"rate_limit_event windows: {sensed}",
                    answered={
                        "auth": "system/init",
                        "admission": (
                            f"rate_limit_event.status={info.status}" if info else
                            "no rate_limit_event"
                        ),
                    },
                )
            return finish(
                OutcomeClass.UNKNOWN,
                "unknown: empty deliverable with rc 0",
                answered={"deliverable": "empty with rc 0 (C-12.6)"},
            )

        # 6. Retryable (C-9.5). Never writes a closure.
        transient_kind = next(
            (kind for kind in summary.error_kinds if kind in TRANSIENT_ERROR_KINDS), None
        )
        if auth_false_positive is not None:
            return finish(
                OutcomeClass.TRANSIENT,
                f"transient: auth signature {auth_false_positive!r} with system/init "
                f"present — the credential authenticated (C-9.3)",
                answered={"auth": "system/init present; the 401 was not the lane's"},
            )
        match = TRANSIENT_RE.search(corpus)
        if transient_kind is not None or match is not None:
            # Name both the provider's own error kind and the text it came with: one
            # says what class of failure it was, the other says what actually happened.
            parts = []
            if transient_kind is not None:
                parts.append(f"provider error kind {transient_kind}")
            if match is not None:
                parts.append(_first_line_containing(corpus, match))
            reason = "; ".join(parts)
            return finish(
                OutcomeClass.TRANSIENT, f"transient: {reason}",
                answered={"transient": reason},
            )
        if summary.truncated_tail and summary.result is None:
            return finish(
                OutcomeClass.TRANSIENT,
                "transient: the stream ended mid-line with no result event",
                answered={"transient": "truncated stream"},
            )

        # 7. No evidence answered anything. The rc rides along (C-9.2).
        spawn = f" ({exit_info.spawn_error})" if exit_info.spawn_error else ""
        return finish(
            OutcomeClass.UNKNOWN,
            f"unknown: rc {exit_info.rc}{spawn}, no classifying evidence",
        )

    # --- attestation (C-12.5) ------------------------------------------------

    def _candidate_transcripts(
        self, notes: dict[str, Any], session_id: str | None,
    ) -> list[Path]:
        """Every transcript that could be this session's. Primary transcripts live
        exactly one project directory below `projects/`; nothing walks a worktree."""
        if not session_id:
            return []
        roots: list[Path] = []
        recorded = notes.get("projects_dir")
        if isinstance(recorded, str) and recorded:
            roots.append(Path(recorded))
        roots.append(self._config_projects_dir())
        found: dict[str, Path] = {}
        for root in roots:
            for pattern in (f"{session_id}.jsonl", f"*/{session_id}.jsonl"):
                try:
                    for path in root.glob(pattern):
                        if path.is_file():
                            found[str(path.resolve())] = path
                except OSError:
                    continue
        return sorted(found.values(), key=str)

    def _find_transcript(
        self, notes: dict[str, Any], session_id: str | None,
    ) -> Path | None:
        candidates = self._candidate_transcripts(notes, session_id)
        return candidates[0] if len(candidates) == 1 else None

    def _assistant_rows(
        self, transcript: Path, session_id: str | None, offset: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Assistant rows of this session that lie after the attempt's start offset.

        The offset is the transcript's size when the attempt launched, so a resumed
        session's earlier turns are excluded: an attempt is attested and delivered
        from its own range, never from a predecessor's (C-12.5, C-12.6).
        """
        try:
            with open_regular(transcript) as handle:
                if offset > 0:
                    size = transcript.stat().st_size
                    start = min(offset, size)
                    if start > 0:
                        handle.seek(start - 1)
                        if handle.read(1) != b"\n":
                            handle.readline()   # discard a partial line
                    else:
                        handle.seek(0)
                data = handle.read()
        except OSError as exc:
            return [], f"transcript unreadable: {exc}"
        rows: list[dict[str, Any]] = []
        for line in data.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("type") != "assistant":
                continue
            if session_id and row.get("sessionId") not in (None, session_id):
                continue
            rows.append(row)
        return rows, None

    def attest(
        self, attempt_dir: Path, launch: Launch, outcome: Outcome, model_id: str,
    ) -> AttestationResult:
        """C-12.5. Never a false positive: every path that cannot see the whole truth
        returns `unattested` and says in `evidence` why."""
        notes = dict(launch.notes) if launch is not None else {}
        session_id = (
            (launch.native_session_id if launch is not None else None)
            or outcome.native_session_id
        )
        if not session_id:
            return AttestationResult(
                Attestation.UNATTESTED, None, "no session id was recorded for the attempt"
            )
        candidates = self._candidate_transcripts(notes, session_id)
        if not candidates:
            return AttestationResult(
                Attestation.UNATTESTED, None,
                f"no transcript found for session {session_id}",
            )
        if len(candidates) > 1:
            listed = ", ".join(str(path) for path in candidates)
            return AttestationResult(
                Attestation.UNATTESTED, None,
                f"ambiguous session transcript: {len(candidates)} files match "
                f"{session_id}.jsonl ({listed})",
            )
        transcript = candidates[0]
        offset = notes.get("transcript_offset")
        rows, error = self._assistant_rows(
            transcript, session_id, int(offset) if isinstance(offset, int) else 0
        )
        if error:
            return AttestationResult(Attestation.UNATTESTED, None, error)

        served: list[str] = []
        synthetic = 0
        missing = 0
        for row in rows:
            if is_synthetic_api_error(row):
                synthetic += 1
                continue
            message = row.get("message")
            model = message.get("model") if isinstance(message, dict) else None
            if isinstance(model, str) and model:
                served.append(model)
            else:
                missing += 1
        ignored = f"; ignored {synthetic} provider synthetic API-error frame(s)" if synthetic else ""
        if not served:
            return AttestationResult(
                Attestation.UNATTESTED, None,
                f"no assistant turn with a model field in {transcript} after byte "
                f"{offset or 0}{ignored}",
            )
        mismatch = next(
            (m for m in served if not model_matches_requested(m, model_id)), None
        )
        if mismatch is not None:
            return AttestationResult(
                Attestation.MISMATCH, mismatch,
                f"requested {model_id}; {transcript} records assistant models "
                f"{', '.join(dict.fromkeys(served))}{ignored}",
            )
        if missing:
            return AttestationResult(
                Attestation.UNATTESTED, None,
                f"{missing} assistant turn(s) without a model field in {transcript}{ignored}",
            )
        return AttestationResult(
            Attestation.ATTESTED, served[-1],
            f"{len(served)} assistant turn(s) in {transcript} all served by {model_id}{ignored}",
        )

    # --- deliverable (C-12.6) ------------------------------------------------

    def _transcript_final_text(
        self, transcript: Path | None, session_id: str | None, offset: int,
    ) -> str:
        if transcript is None:
            return ""
        rows, error = self._assistant_rows(transcript, session_id, offset)
        if error:
            return ""
        for row in reversed(rows):
            text = message_text(row.get("message"))
            if text:
                return text
        return ""

    def _final_text(
        self, summary: StreamSummary, transcript: Path | None,
        notes: dict[str, Any], session_id: str | None,
    ) -> str:
        """v1's `prefer_transcript_text` rule.

        The JSON envelope's `result` is a *rendering* of the final message; the
        transcript holds the message's text blocks verbatim. On 2026-09-04 a 64,613 B
        envelope lost 1,912 interior characters while every frame header stayed intact.
        So: when the transcript's final assistant text is longer than the envelope's
        and differs from it, the transcript's bytes win.
        """
        envelope = summary.final_text
        offset = notes.get("transcript_offset")
        transcript_text = self._transcript_final_text(
            transcript, session_id, int(offset) if isinstance(offset, int) else 0
        )
        if transcript_text and len(transcript_text.encode("utf-8")) > len(
            envelope.encode("utf-8")
        ) and transcript_text != envelope:
            return transcript_text
        return envelope

    def deliverable(
        self, attempt_dir: Path, launch: Launch, outcome: Outcome,
    ) -> bytes | None:
        """C-12.6: the attempt's own final assistant text, transcript first."""
        notes = dict(launch.notes) if launch is not None else {}
        summary = self.stream_summary(Path(attempt_dir), launch)
        session_id = (
            summary.session_id
            or (launch.native_session_id if launch is not None else None)
            or outcome.native_session_id
        )
        transcript = (
            Path(outcome.transcript_path) if outcome.transcript_path
            else self._find_transcript(notes, session_id)
        )
        text = self._final_text(summary, transcript, notes, session_id)
        return text.encode("utf-8") if text.strip() else None


# --- module-private helpers --------------------------------------------------


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


def _first_auth_phrase(corpus: str) -> str | None:
    match = AUTH_SIGNATURE_RE.search(corpus) or ORG_BLOCK_RE.search(corpus)
    return _first_line_containing(corpus, match) if match else None


def _closure_clock(
    reported: str | datetime | None, now: datetime,
) -> tuple[str, ClockSource]:
    """C-9.4: the provider's reset clock when it reported one, else now + 3600 s
    marked `guessed`. A reported clock already in the past is still reported — the
    daemon expires it by clock (C-9.6) rather than inventing a longer one."""
    if isinstance(reported, datetime):
        return iso_utc(reported), ClockSource.REPORTED
    if isinstance(reported, str) and reported:
        return reported, ClockSource.REPORTED
    return iso_utc(now + timedelta(seconds=GUESSED_CLOSURE_S)), ClockSource.GUESSED


def _clock_from_text(text: str, now: datetime) -> datetime | None:
    """A reset clock the provider stated in prose, in either form it uses."""
    match = _EPOCH_LIMIT_RE.search(text)
    if match:
        try:
            return datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    return parse_reset_clock(text, now)


def _single_served_model(summary: StreamSummary) -> str | None:
    if any(not message.model and not is_synthetic_api_error(message.raw)
           for message in summary.assistants):
        return None
    models = tuple(dict.fromkeys(summary.assistant_models))
    return models[0] if len(models) == 1 else None


def _refused(summary: StreamSummary) -> bool:
    if summary.result is not None and summary.result.stop_reason == REFUSAL_STOP_REASON:
        return True
    return any(m.stop_reason == REFUSAL_STOP_REASON for m in summary.assistants)


def link_raw_stream(attempt_dir: Path, launch: Launch) -> Path | None:
    """Publish the attempt's stdout as its raw stream.

    `--output-format stream-json` writes the stream to stdout, so there is no second
    descriptor to redirect: `stdout` and `stream.jsonl` are the same bytes. The daemon
    may call this at finalization to give the stream its own artifact role (C-8.2); a
    hard link is used so the bytes are not copied, with a copy as the fallback across
    filesystems. Everything in this adapter reads either file, so calling it is
    optional.
    """
    if launch is None or not launch.raw_stream_path:
        return None
    stream = Path(launch.raw_stream_path)
    stdout = Path(launch.stdout_path) if launch.stdout_path else Path(attempt_dir) / "stdout"
    if stream.exists() or not stdout.exists():
        return stream if stream.exists() else None
    try:
        os.link(stdout, stream)
    except OSError:
        try:
            data = read_regular(stdout)
            fd = os.open(stream, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with open(fd, "wb") as out:            # a new file of its own, never one standing there
                out.write(data)
        except OSError:
            return None
    return stream


def reconstruct_v1_argv(
    claude_bin: str, model: str, session_id: str, sandbox: str, *,
    isolated: bool = False, review_root: str | None = None,
    output_format: str = "stream-json", verbose: bool = True,
    mcp_config: str = EMPTY_MCP_CONFIG,
) -> tuple[str, ...]:
    """v1 `bin/subfleet-claude`'s launch line, rebuilt here from its own source.

    v1 runs, at `bin/subfleet-claude:806-811`:

        "$CLAUDE_BIN" -p --model "$MODEL" --session-id "$SID" \\
            --output-format json ${PERM_ARGS[@]+"${PERM_ARGS[@]}"}

    v2 differs in exactly three documented places: the output format is `stream-json`
    (C-12.4, so the `rate_limit_event` is readable), `--verbose` accompanies it, and
    a writable launch adds `--strict-mcp-config --mcp-config <config>` after v1's
    bypass flag (C-12.9, d714), so it starts only the MCP servers its job named.
    Read-only `PERM_ARGS` is reproduced verbatim, in v1's order. The parity test
    compares this against `ClaudeAdapter.build_launch`, so a drift in either fails
    loudly.
    """
    argv = [claude_bin, "-p", "--model", model, "--session-id", session_id,
            "--output-format", output_format]
    if verbose:
        argv.append("--verbose")
    if sandbox == Sandbox.WORKSPACE_WRITE.value:
        argv += ["--dangerously-skip-permissions", "--strict-mcp-config", "--mcp-config", mcp_config]
    else:
        tools = READ_ONLY_TOOLS_BASE if isolated else READ_ONLY_TOOLS_WEB
        argv += [
            "--permission-mode", "plan",
            "--tools", tools,
            "--allowedTools", tools,
            "--setting-sources", "",
            "--safe-mode",
            "--no-chrome",
            "--strict-mcp-config",
            "--mcp-config", EMPTY_MCP_CONFIG,
            "--disable-slash-commands",
        ]
        if isolated and review_root:
            argv += ["--add-dir", review_root]
    return tuple(argv)


__all__ = [
    "ClaudeAdapter",
    "ENROLL_MODEL",
    "ENROLL_PROMPT",
    "ENV_REMOVE",
    "KEYCHAIN_PREFIX",
    "HEADLESS_BLOCK",
    "ADMISSION_WINDOW",
    "SOURCE_RATE_LIMIT_EVENT",
    "SOURCE_OAUTH_USAGE",
    "home_login",
    "keychain_service_for_home",
    "login_expired",
    "UsageResult",
    "OAUTH_USAGE_URL",
    "SCOPED_MODEL_IDS",
    "apply_headless_block",
    "encode_project_dir",
    "iso_from_epoch",
    "iso_utc",
    "link_raw_stream",
    "model_matches_requested",
    "parse_reset_clock",
    "reconstruct_v1_argv",
]
