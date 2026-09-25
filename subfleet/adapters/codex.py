"""Codex subscription adapter (C-9, C-10, C-12); no store or process ownership."""
from __future__ import annotations

import base64
import json
import math
import os
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .base import Adapter, AdapterError
from ..guardian import atomic_publish
from ..contracts import (
    Attestation, AttestationResult, ClockSource, Closure, ClosureReason, Credential,
    ExitInfo, GUESSED_CLOSURE_S, JobSpec, Lane, LaneInfo, Launch, Outcome,
    OutcomeClass, Reading, ReadingLabel, Sandbox, WINDOW_KEYS,
)

WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
WHAM_RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
WHAM_RESET_CREDITS_CONSUME_URL = WHAM_RESET_CREDITS_URL + "/consume"
RESET_CREDIT_URLS = frozenset({WHAM_RESET_CREDITS_URL, WHAM_RESET_CREDITS_CONSUME_URL})
USER_AGENT = "subfleet/2 (codex_cli_rs compatible)"
ROLLOUT_THREAD_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.I,
)
ATTESTATION_CANDIDATE_LIMIT = 32
AUTH_RE = re.compile(
    r"refresh[ _-]token.{0,60}revoked|"
    r"(?:organi[sz]ation|organization_id).{0,60}(?:blocked|disabled|deactivated)|"
    r"(?:wham/usage|usage endpoint).{0,100}\b401\b|"
    r"\b401\b.{0,100}(?:wham/usage|usage endpoint)", re.I,
)
CREDITS_RE = re.compile(
    r"(?:insufficient|not enough|out of|exhausted|no remaining)[ _-]credits|"
    r"credits?.{0,35}(?:exhausted|depleted|insufficient|required)|"
    r"credits?[ _-]rejection", re.I,
)
LIMIT_RE = re.compile(
    r"hit your usage limit|usage limit reached|usage_limit_reached|"
    r"(?:model|account).{0,60}(?:quota|usage limit)|quota exceeded|" + CREDITS_RE.pattern, re.I,
)
CONTENT_RE = re.compile(r"content[ _-]filter|trusted access|can('|’)t (help|assist) with", re.I)
OLD_CLI_RE = re.compile(
    r"cli.{0,45}(?:too old|outdated)|"
    r"(?:upgrade|update)\s+(?:(?:your|the)\s+)?(?:codex|cli)\b|"
    r"(?:unsupported|unrecognized|unexpected|unknown) (?:argument|option|flag)|"
    r"minimum.{0,30}(?:codex|cli|version)|requires? (?:codex )?version", re.I,
)
TRANSIENT_RE = re.compile(
    r"\b5\d\d\b|capacity|disconnect|overloaded|timed? ?out|temporarily|"
    r"rate limit|too many requests|\b429\b|\b401\b|unauthorized|"
    r"\bDNS\b|name (?:or service not known|resolution)|nodename nor servname|"
    r"\bTLS\b|\bSSL\b|certificate verify|connection (?:reset|refused|closed)|"
    r"error sending request|stream.{0,30}(?:closed|ended|failed)", re.I,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clock(value: object) -> str | None:
    try:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return _iso(datetime.fromtimestamp(value, timezone.utc))
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return _iso(parsed)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _claims(token: object) -> dict:
    if not isinstance(token, str):
        return {}
    try:
        part = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, UnicodeError):
        return {}


def _read_auth(home: Path) -> dict:
    try:
        raw = json.loads((home / "auth.json").read_bytes())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _api_key(raw: dict) -> bool:
    mode = str(raw.get("auth_mode") or "").lower().replace("_", "").replace("-", "")
    return mode == "apikey" or any(raw.get(key) for key in ("OPENAI_API_KEY", "CODEX_API_KEY"))


def api_key_login(home: Path | str) -> bool:
    """v1's API-key detection, including mixed OAuth/API-key homes (C-10.2)."""
    return _api_key(_read_auth(Path(home).expanduser()))


def _identity(raw: dict) -> dict:
    tokens = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else {}
    access = _claims(tokens.get("access_token"))
    identity = _claims(tokens.get("id_token"))
    auth = access.get("https://api.openai.com/auth")
    id_auth = identity.get("https://api.openai.com/auth")
    if not isinstance(auth, dict):
        auth = {}
    if not isinstance(id_auth, dict):
        id_auth = {}
    return {
        "token": tokens.get("access_token"),
        "account_id": (tokens.get("account_id") or auth.get("account_id") or auth.get("chatgpt_account_id")
                       or id_auth.get("account_id") or id_auth.get("chatgpt_account_id")),
        "email": identity.get("email") or access.get("email") or raw.get("email"),
        "plan": auth.get("chatgpt_plan_type") or id_auth.get("chatgpt_plan_type"),
    }


def _events(path: Path) -> Iterator[dict]:
    """Stream complete JSONL records, tolerating truncated/malformed lines."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield event
    except FileNotFoundError:
        return


def _stream_path(attempt_dir: Path, launch: Launch) -> Path:
    for path in (launch.raw_stream_path, str(attempt_dir / "stream.jsonl"), launch.stdout_path):
        if path and Path(path).is_file() and Path(path).stat().st_size:
            return Path(path)
    return attempt_dir / "stdout"


def _objects(value: object) -> Iterator[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _reset(event: dict, text: str, now: datetime) -> str | None:
    for obj in _objects(event):
        for key in ("resets_at", "reset_at", "resetsAt", "reset_time", "reset_timestamp"):
            if result := _clock(obj.get(key)):
                return result
    # CLI rejection messages include either an absolute ISO clock or a local wall clock.
    for match in re.finditer(
        r"(?:try again at|resets?(?: at)?)\s+(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"
        r"(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2}| UTC))", text, re.I,
    ):
        if result := _clock(match.group(1).replace(" UTC", "+00:00")):
            return result
    match = re.search(
        r"(?:try again at|resets?(?: at)?)\s+(\d{1,2}):(\d{2})\s*([AP])\.?M\.?"
        r"(?:\s*\(([A-Za-z_]+/[A-Za-z_/]+)\))?", text, re.I,
    )
    if match:
        hour, minute, meridian, zone = match.groups()
        hour, minute = int(hour), int(minute)
        if not (1 <= hour <= 12 and 0 <= minute < 60):
            return None
        try:
            local = now.astimezone(ZoneInfo(zone)) if zone else now.astimezone()
        except ZoneInfoNotFoundError:
            return None
        result = local.replace(hour=hour % 12 + (12 if meridian.upper() == "P" else 0), minute=minute, second=0, microsecond=0)
        # v1 prints an optional date in the process locale after the clock.
        date_match = re.search(r"(?:on\s+|\()(\w{3,9}\s+\d{1,2},?\s+\d{4})", text[match.end():])
        if date_match:
            for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
                try:
                    day = datetime.strptime(date_match.group(1), fmt)
                    return _iso(result.replace(year=day.year, month=day.month, day=day.day))
                except ValueError:
                    pass
        if result <= local:
            result += timedelta(days=1)
        return _iso(result)
    return None


class CodexAdapter(Adapter):
    provider = "codex"

    def __init__(self, codex_bin: str = "codex", *, opener: Callable | None = None,
                 now: Callable[[], datetime] = _utcnow, timeout: float = 15.0):
        self.codex_bin = str(codex_bin)
        self._opener = opener
        self._now = now
        self.timeout = timeout

    def _payload(self, raw: dict) -> dict:
        identity = _identity(raw)
        if _api_key(raw) or not identity["token"]:
            return {}
        request = urllib.request.Request(WHAM_USAGE_URL, headers={
            "Authorization": f"Bearer {identity['token']}",
            "chatgpt-account-id": identity["account_id"] or "",
            "User-Agent": USER_AGENT, "Accept": "application/json",
        })
        try:
            if self._opener:
                status, body = self._opener(request, self.timeout)
            else:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    status, body = response.status, response.read()
            if status != 200:
                return {}
            payload = json.loads(body)
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, urllib.error.URLError):
            return {}

    def _readings(self, payload: dict, lane_id: str) -> tuple[Reading, ...]:
        limits = payload.get("rate_limit")
        if not isinstance(limits, dict):
            return ()
        readings = []
        observed_at = _iso(self._now())
        for slot in ("primary_window", "secondary_window"):
            window = limits.get(slot)
            if not isinstance(window, dict):
                continue
            minutes = window.get("window_minutes")
            seconds = window.get("limit_window_seconds")
            if minutes is None and isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
                minutes = seconds / 60
            used = window.get("used_percent")
            if (isinstance(minutes, bool) or not isinstance(minutes, (int, float))
                    or not math.isfinite(minutes) or minutes <= 0 or int(minutes) != minutes
                    or isinstance(used, bool) or not isinstance(used, (int, float))
                    or not math.isfinite(used) or not 0 <= used <= 100):
                continue
            readings.append(Reading(
                lane_id=lane_id, scope="account", window=WINDOW_KEYS.get(int(minutes), str(int(minutes))),
                utilization=used / 100, resets_at=_clock(window.get("reset_at")),
                label=ReadingLabel.PROVIDER, source="wham", observed_at=observed_at,
            ))
        return tuple(readings)

    def enroll(self, credential: Credential) -> LaneInfo:
        if credential.provider != self.provider or credential.kind != "home":
            raise AdapterError("Codex enrollment requires a subscription home", fix="Use a Codex home signed into a paid ChatGPT subscription.")
        home = Path(credential.ref).expanduser().resolve()
        raw = _read_auth(home)
        fix = f"Sign into a paid ChatGPT subscription in CODEX_HOME={home} and enroll again."
        if _api_key(raw):
            raise AdapterError("API-key Codex homes are refused", code=7, fix=fix)
        identity = _identity(raw)
        if not identity["token"]:
            raise AdapterError("Codex home has no readable subscription token", fix=fix)
        if str(identity["plan"]).strip().lower() == "free":
            raise AdapterError("Free ChatGPT plans are refused", code=7, fix=fix)
        payload = self._payload(raw)
        plan = payload.get("plan_type") or identity["plan"]
        if str(plan).strip().lower() == "free":
            raise AdapterError("Free ChatGPT plans are refused", code=7, fix=fix)
        account = identity["account_id"] or identity["email"] or payload.get("email")
        if not account:
            raise AdapterError("Codex home has no account id or email", fix=fix)
        # Enrollment precedes lane allocation; the daemon rebinds these readings to its lane id.
        return LaneInfo(f"codex:{account}", plan, str(home), self._readings(payload, ""))

    def probe(self, lane: Lane, credential_env: dict[str, str]) -> tuple[Reading, ...]:
        return self.probe_status(lane, credential_env)["readings"]

    def probe_status(self, lane: Lane, credential_env: dict[str, str]) -> dict:
        """C-9.3, C-23.47: retain one usage verdict without refreshing auth."""
        home = Path(lane.home or credential_env.get("CODEX_HOME") or lane.credential.ref).expanduser()
        raw = _read_auth(home)
        identity = _identity(raw)
        base = {"readings": (), "checked_at": _iso(self._now()),
                "credential_epoch": raw.get("last_refresh"),
                "account_key": "codex:" + str(identity["account_id"] or identity["email"]) if identity["account_id"] or identity["email"] else None,
                "email": identity["email"], "plan_type": identity["plan"]}
        if _api_key(raw) or not identity["token"]:
            return {**base, "status": "no-auth"}
        result = self._request(raw, WHAM_USAGE_URL)
        if result["status"] != "ok":
            status = "network-error" if result["status"] == "timeout" else result["status"]
            error = str(result.get("error_code", "")).lower()
            message = str(result.get("detail", "")).lower()
            if "revok" in error or "refresh token was revoked" in message:
                status = "revoked"
            elif re.search(r"(?:organi[sz]ation|organization_id).{0,60}(?:blocked|disabled|deactivated)",
                           error + " " + message, re.I):
                status = "auth-dead"
            elif result.get("http_status") == 401:
                expires = _claims(identity["token"]).get("exp")
                if "expir" in error or "expir" in message or (
                    isinstance(expires, (int, float)) and not isinstance(expires, bool)
                    and expires <= self._now().timestamp()
                ):
                    status = "expired-token"
                else:
                    status = "auth-dead"
            return {**base, **result, "status": status}
        payload = result["payload"]
        limits = payload.get("rate_limit")
        limits = limits if isinstance(limits, dict) else {}
        counts = payload.get("rate_limit_reset_credits")
        counts = counts if isinstance(counts, dict) else {}
        def count(key):
            value = counts.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        return {**base, "status": "limited" if limits.get("limit_reached") is True else "ok",
                "allowed": limits.get("allowed"), "limit_reached": limits.get("limit_reached"),
                "readings": self._readings(payload, lane.lane_id),
                "reset_credits": {"available": count("available_count"),
                                  "applicable": count("applicable_available_count")}}

    def _request(self, raw: dict, url: str, *, payload: dict | None = None,
                 timeout: float | None = None) -> dict:
        """Subscription-only GET or a persisted reset intent; never refresh auth."""
        if url not in RESET_CREDIT_URLS and url != WHAM_USAGE_URL:
            raise ValueError("endpoint is not allowlisted")
        identity = _identity(raw)
        if _api_key(raw) or not identity["token"]:
            return {"status": "no-auth"}
        headers = {"Authorization": f"Bearer {identity['token']}",
                   "chatgpt-account-id": identity["account_id"] or "",
                   "User-Agent": USER_AGENT, "Accept": "application/json"}
        data = None if payload is None else json.dumps(payload).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        timeout = self.timeout if timeout is None else timeout
        try:
            if self._opener:
                status, body = self._opener(request, timeout)
            else:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    status, body = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read()
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            timed_out = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
            return {"status": "timeout" if timed_out else "network-error", "error_type": type(exc).__name__}
        try:
            decoded = json.loads(body)
            if not isinstance(decoded, dict):
                raise ValueError("expected object")
        except (UnicodeError, ValueError):
            decoded = {}
            if 200 <= status < 300:
                return {"status": "invalid-response", "error_type": "ValueError"}
        if not 200 <= status < 300:
            error = decoded.get("error")
            error = error if isinstance(error, dict) else {}
            return {"status": "network-error" if status >= 500 else "http-error",
                    "http_status": status, "error_code": error.get("code"),
                    "detail": error.get("message", f"HTTP {status}")}
        return {"status": "ok", "payload": decoded}

    def list_reset_credits(self, lane: Lane, credential_env: dict[str, str] | None = None,
                           *, timeout: float | None = None) -> dict:
        """C-23.7: this entitlement endpoint lists gifted credits only."""
        result = self._request(_read_auth(Path(lane.home or lane.credential.ref).expanduser()),
                               WHAM_RESET_CREDITS_URL, timeout=timeout)
        if result["status"] != "ok":
            return result
        credits = result["payload"].get("credits", [])
        return {"status": "ok", "credits": [credit for credit in credits
                if isinstance(credit, dict) and credit.get("reset_type") == "codex_rate_limits"
                and credit.get("status") == "available" and isinstance(credit.get("id"), str)
                and credit["id"] and credit.get("source") not in ("purchase", "purchased", "paid")
                and credit.get("gifted") is not False] if isinstance(credits, list) else []}

    def consume_reset_credit(self, lane: Lane, credit: dict, redeem_request_id: str,
                             credential_env: dict[str, str] | None = None,
                             *, timeout: float | None = None) -> dict:
        """C-23.7, C-23.16: consume one concrete gift using the durable UUID4."""
        request_id = uuid.UUID(redeem_request_id)
        if request_id.version != 4 or str(request_id) != redeem_request_id:
            raise ValueError("redeem_request_id must be a UUID4")
        if (credit.get("reset_type") != "codex_rate_limits" or credit.get("status") != "available"
                or not isinstance(credit.get("id"), str) or not credit["id"]
                or credit.get("source") in ("purchase", "purchased", "paid") or credit.get("gifted") is False):
            raise ValueError("a concrete available gifted entitlement is required")
        result = self._request(_read_auth(Path(lane.home or lane.credential.ref).expanduser()),
                               WHAM_RESET_CREDITS_CONSUME_URL, timeout=timeout,
                               payload={"credit_id": credit["id"], "redeem_request_id": redeem_request_id})
        if result["status"] != "ok":
            return result
        return {**result["payload"], "status": "ok", "redeem_request_id": redeem_request_id}

    def _launch(self, job: JobSpec, attempt_id: str, attempt_dir: Path, lane: Lane,
                credential_env: dict[str, str], prompt_path: Path, guard_override: str | None,
                model_id: str | None, effort: str | None, session_id: str | None) -> Launch:
        sandbox = Sandbox(job.sandbox)
        if sandbox == Sandbox.WORKSPACE_WRITE and not guard_override:
            raise AdapterError("Writable Codex jobs require the never-rules guard", fix="Run the guard trust preflight and supply its hooks override.")
        home = lane.home or lane.credential.ref
        if not home or (credential_env.get("CODEX_HOME") and Path(credential_env["CODEX_HOME"]).expanduser().resolve() != Path(home).expanduser().resolve()):
            raise AdapterError("Codex credential home does not match the lane", fix="Resolve CODEX_HOME from this lane's original home.")
        argv = [self.codex_bin, "exec", "--json"]
        if job.isolated_review:
            from .isolation import codex_args, validate_isolated_review
            env = {**os.environ, **credential_env}
            validate_isolated_review(sandbox, job.review_root, env)
            argv += ["--skip-git-repo-check"]  # The gate's required neutral cwd is not a repository.
            argv += codex_args(self.codex_bin, home=home, workdir=job.workdir, env=env,
                               inspector=getattr(self, "isolation_inspector", None))
        elif sandbox == Sandbox.READ_ONLY:
            # A read-only job may read a directory that is not a repository (a
            # folder of repositories, a review folder). Without this, `codex exec`
            # exits at once (12 jobs on 2026-09-24: "Not inside a trusted
            # directory"). The sandbox is unchanged; writable jobs keep C-13.2's
            # repository requirement.
            argv += ["--skip-git-repo-check"]
        if model_id:
            argv += ["-m", model_id]
        if effort:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        argv += ["--sandbox", sandbox.value]
        network = job.network and sandbox == Sandbox.WORKSPACE_WRITE and not job.isolated_review
        if network:
            # d260: gh, curl and git push reach the network, as in a writable
            # Claude job (verified live 2026-09-25: HTTP 200 with this key, "Could
            # not resolve host" without). The never-rules guard still judges
            # every command; its preflight refuses a launch without jq.
            argv += ["-c", "sandbox_workspace_write.network_access=true"]
        if network or os.environ.get("SUBFLEET_CODEX_UNIFIED_EXEC") == "off":
            # C-23.6: unified exec's write_stdin feeds a running shell text the
            # never-rules guard never sees; with the network open that hole
            # matters, so `shell_command` is the only shell tool (network still
            # verified live with this switch: HTTP 200).
            argv += ["-c", "features.unified_exec=false"]
        if guard_override and not job.isolated_review:
            argv += ["-c", guard_override if guard_override.startswith("hooks=") else f"hooks={guard_override}"]
        argv += ["--output-last-message", str(attempt_dir / "last.md")]
        if session_id:
            argv += ["resume", session_id, "-"]
        env = dict(credential_env)
        env["CODEX_HOME"] = str(Path(home).expanduser())
        env["SUBFLEET_ATTEMPT"] = attempt_id
        env["SUBFLEET_JOB"] = attempt_id.rsplit("/", 1)[0]
        attempt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        sent_path = attempt_dir / "prompt.sent.md"
        atomic_publish(sent_path, prompt_path.read_bytes())
        return Launch(tuple(argv), env, ("CODEX_API_KEY", "OPENAI_API_KEY"), job.workdir,
                      str(sent_path), str(attempt_dir / "stdout"), str(attempt_dir / "stderr"),
                      str(attempt_dir / "stream.jsonl"), session_id, lane.lane_id)

    def build_launch(self, job: JobSpec, attempt_id: str, attempt_dir: Path, lane: Lane,
                     credential_env: dict[str, str], model_id: str, effort: str | None,
                     prompt_path: Path, guard_override: str | None) -> Launch:
        return self._launch(job, attempt_id, attempt_dir, lane, credential_env, prompt_path,
                            guard_override, model_id, effort, None)

    def resume_launch(self, job: JobSpec, attempt_id: str, attempt_dir: Path, lane: Lane,
                      credential_env: dict[str, str], native_session_id: str,
                      prompt_path: Path, guard_override: str | None,
                      model_id: str | None = None) -> Launch | None:
        # `model_id` is accepted for the base signature and ignored: a Codex
        # thread already holds its resolved model (see below).
        if job.isolated_review:
            raise AdapterError("isolated review cannot resume a contextual Codex thread",
                               fix="submit a fresh isolated review job")
        if not native_session_id or native_session_id.startswith("-"):
            raise AdapterError("Codex resume requires a thread id", fix="Use the original attempt's native_session_id and lane.")
        # Job pins are policy aliases; the native thread already holds its resolved model.
        return self._launch(job, attempt_id, attempt_dir, lane, credential_env, prompt_path,
                            guard_override, None, None, native_session_id)

    def deliverable(self, attempt_dir: Path, launch: Launch, outcome: Outcome) -> bytes | None:
        try:
            data = (attempt_dir / "last.md").read_bytes()
            if data.strip():
                return data
        except FileNotFoundError:
            pass
        last = None
        for event in _events(_stream_path(attempt_dir, launch)):
            item = event.get("item")
            if (event.get("type") == "item.completed" and isinstance(item, dict)
                    and item.get("type") == "agent_message" and isinstance(item.get("text"), str)):
                last = item["text"].encode()
        return last if last and last.strip() else None

    def classify(self, attempt_dir: Path, launch: Launch, exit_info: ExitInfo) -> Outcome:
        session_id = launch.native_session_id
        failures = []
        for event in _events(_stream_path(attempt_dir, launch)):
            if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                session_id = event["thread_id"]
            if event.get("type") in ("turn.failed", "error"):
                failures.append(event)
        stderr = self.read_text(Path(launch.stderr_path))
        signals = [(event, json.dumps(event, ensure_ascii=False)) for event in failures]
        signals.extend(({}, line) for line in stderr.splitlines() if line.strip())
        evidence = {"rc": exit_info.rc, "signal": exit_info.signal,
                    "authentication": None, "admission": None, "quota": None}
        if exit_info.spawn_error:
            evidence["spawn_error"] = exit_info.spawn_error
        def result(cls: OutcomeClass, detail: str, closure: Closure | None = None) -> Outcome:
            return Outcome(cls, detail, evidence=dict(evidence), closure=closure, native_session_id=session_id)
        for event, text in signals:
            if AUTH_RE.search(text):
                evidence["authentication"] = event or text
                return result(OutcomeClass.AUTH_DEAD, "Subscription authentication rejected")
        # A completed admission can recover a nonterminal stream error. A terminal
        # turn.failed always wins over a leftover last.md or earlier assistant text.
        terminal = any(event.get("type") == "turn.failed" for event in failures)
        if exit_info.rc == 0 and not terminal and not exit_info.spawn_error:
            if self.deliverable(attempt_dir, launch, result(OutcomeClass.UNKNOWN, "")):
                evidence["admission"] = "deliverable with exit 0"
                return result(OutcomeClass.OK, "Codex completed with a deliverable")
        for regex, cls, detail in ((OLD_CLI_RE, OutcomeClass.CLI_TOO_OLD, "Codex CLI must be upgraded"),
                                   (CONTENT_RE, OutcomeClass.CONTENT_FILTER, "Content-filter rejection; prompt reconciliation required")):
            for event, text in signals:
                if regex.search(text):
                    evidence["admission"] = event or text
                    return result(cls, detail)
        for event, text in signals:
            if not LIMIT_RE.search(text):
                continue
            evidence["admission"] = event or text
            evidence["quota"] = event or text
            objects = list(_objects(event))
            # Explicit account scope wins over incidental requested-model metadata.
            scope = next((obj["scope"] for obj in objects
                          if isinstance(obj.get("scope"), str) and obj["scope"]
                          and obj["scope"] != "model"), None)
            if scope is None:
                scope = next((candidate for obj in objects
                              for candidate in (obj.get("model_id"), obj.get("model"))
                              if isinstance(candidate, str) and candidate
                              and candidate not in ("model", "account")), "account")
            now = self._now()
            until = _reset(event, text, now)
            source = ClockSource.REPORTED if until else ClockSource.GUESSED
            until = until or _iso(now + timedelta(seconds=GUESSED_CLOSURE_S))
            lane_id = launch.lane_id or launch.env_add.get("SUBFLEET_LANE_ID", "")
            if not lane_id:
                try:
                    lane_id = json.loads((attempt_dir / "start.json").read_bytes()).get("lane_id", "")
                except (OSError, ValueError, AttributeError):
                    pass
            closure = Closure(lane_id, scope, until,
                              ClosureReason.CREDITS if CREDITS_RE.search(text) else ClosureReason.PROVIDER_LIMIT,
                              source, json.dumps(event, ensure_ascii=False) if event else text)
            evidence.update(scope=scope, clock_source=source.value, resets_at=until)
            return result(OutcomeClass.LIMITED, "Codex subscription limit reached", closure)
        for event, text in signals:
            if TRANSIENT_RE.search(text):
                evidence["admission"] = event or text
                return result(OutcomeClass.TRANSIENT, "Temporary Codex transport or capacity failure")
        evidence["admission"] = failures[-1] if failures else "no successful deliverable"
        return result(OutcomeClass.UNKNOWN, "Provider could not spawn" if exit_info.spawn_error else "Codex exited without a verified deliverable")

    def attest(self, attempt_dir: Path, launch: Launch, outcome: Outcome,
               model_id: str) -> AttestationResult:
        session_id = outcome.native_session_id or launch.native_session_id
        home = launch.env_add.get("CODEX_HOME")
        if outcome.evidence.get("spawn_error"):
            return AttestationResult(Attestation.UNATTESTED, None, "Provider did not spawn")
        if not session_id or not home:
            return AttestationResult(Attestation.UNATTESTED, None, "No thread id or CODEX_HOME")
        interval = None
        if launch.native_session_id:
            # Resumes share a persistent rollout. The guardian's existing receipts
            # bound this invocation without adding another shared interface field.
            try:
                start = json.loads((attempt_dir / "start.json").read_bytes())
                end = json.loads((attempt_dir / "exit.json").read_bytes())
                started = _clock(start.get("started_at"))
                finished = _clock(end.get("finished_at"))
                if started and finished and started < finished:
                    interval = (started, finished)
            except (OSError, ValueError, AttributeError):
                pass
            if interval is None:
                return AttestationResult(Attestation.UNATTESTED, None,
                                         "Native resume needs valid start/exit receipt clocks")
        # Codex names rollouts with the native thread UUID. Enumerate paths,
        # but do not open thousands of unrelated historical transcripts merely
        # to rediscover their IDs. Filename matches still need session_meta and
        # model evidence below; the filename never supplies an attestation.
        candidates = []
        for path in (Path(home).expanduser() / "sessions").rglob("*.jsonl"):
            named = ROLLOUT_THREAD_RE.fullmatch(path.name)
            if named and named[1].casefold() != session_id.casefold():
                continue
            candidates.append(path)
            # Keep opaque legacy filenames usable, but never assert uniqueness
            # from a truncated search of a large or ambiguous candidate set.
            if len(candidates) > ATTESTATION_CANDIDATE_LIMIT:
                return AttestationResult(Attestation.UNATTESTED, None,
                                         "Rollout candidate limit exceeded; thread evidence is ambiguous")
        matches = []
        for path in sorted(candidates):
            records = _events(path)
            first = next(records, {})
            payload = first.get("payload", {})
            if first.get("type") != "session_meta" or not isinstance(payload, dict) or payload.get("id") != session_id:
                continue
            models = []
            if interval is None and isinstance(payload.get("model"), str):
                models.append(payload["model"])
            for event in records:
                if interval is not None:
                    observed = _clock(event.get("timestamp"))
                    # Receipt timestamps have second precision (C-1.7). Boundary
                    # seconds are ambiguous; prefer unattested to borrowing a turn.
                    if observed is None or not interval[0] < observed < interval[1]:
                        continue
                payload = event.get("payload", {})
                if event.get("type") == "turn_context" and isinstance(payload, dict) and isinstance(payload.get("model"), str):
                    models.append(payload["model"])
            matches.append((path, models))
        if len(matches) != 1 or not matches[0][1]:
            evidence = "Expected exactly one matching rollout with model evidence"
            if "--ephemeral" in launch.argv:
                evidence = ("Isolated --ephemeral Codex produced no unique persisted served-model evidence; "
                            "requested model and startup header cannot attest a peer verdict")
            return AttestationResult(Attestation.UNATTESTED, None, evidence)
        path, models = matches[0]
        served = next((model for model in models if model != model_id), models[-1])
        return AttestationResult(Attestation.ATTESTED if served == model_id else Attestation.MISMATCH,
                                 served, str(path))
