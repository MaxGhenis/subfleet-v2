"""C-31: the API credit lane's adapter, provider `claude-api`.

A credit lane runs agent work on one Max plan's monthly Claude API credit
(docs/decisions/2026-11-03-api-credit-lanes.md). Its provider process is
Subfleet's own Messages API tool loop, `python -I -m subfleet.api_agent` (the
harness), which another build supplies. This adapter and the harness meet only
at the interface that record fixes: the argv, the environment, the files the
harness writes in the attempt folder and its exit codes.

The adapter builds the harness's launch and reads its records. It never spawns
a process, never reads the store and never runs the `claude` binary
(invariant 1). The daemon hands it what only the store knows, as attributes:
`credit_policy` (the policy's `claude_api` section) and `credit_budget` (the
lane's credit, ledger and grants in flight, C-31.4).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Iterator

from .. import credit_lane
from ..contracts import (
    API_PROVIDER, GUESSED_CLOSURE_S, IDENTITY_EVIDENCE, Attestation, AttestationResult, ClockSource, Closure,
    ClosureReason, Credential, ExitInfo, IdentityStatus, JobSpec, Lane, LaneInfo, Launch, Outcome, OutcomeClass,
    Reading, Sandbox, launch_capability,
)
from ..guardian import atomic_publish
from ..review_publish import protect_launch, publishing_denied
from ..sessions.transcripts import NotRegularFile, read_regular
from .base import Adapter, AdapterError, open_stream

#: C-31.7's harness module, run with `-I` so no PYTHON* variable or user site reaches it.
HARNESS_MODULE = "subfleet.api_agent"
#: C-31.1: the variables the interface removes from every credit launch. The
#: daemon's `_launch_environment` removes `ANTHROPIC_API_KEY` too, then puts back
#: only this launch's resolved credit key.
ENV_REMOVE = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_API_KEY",
              "OPENAI_API_KEY")
#: C-31.4: the policy defaults, when a policy predates the `claude_api` section.
DEFAULT_MAX_USD = 25.0
DEFAULT_MAX_TURNS = 400
#: The harness's exit codes, read only when the stream has no `end` event.
EXIT_OUTCOMES = {0: "done", 3: "credit", 4: "refused-by-kind", 5: "workspace-limit", 6: "budget"}
END_OUTCOMES = frozenset({"done", "credit", "workspace-limit", "refused-by-kind", "budget", "turns", "error"})
#: An attempt's history, which a resume continues from (C-31.7).
HISTORY = "messages.jsonl"
STREAM = "stream.jsonl"
LAST = "last.md"
HISTORY_MAX_BYTES = 256 << 20
#: A native session id is the attempt id, `<job>/a<seq>` (C-1.2).
_ATTEMPT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/a[1-9][0-9]*", re.ASCII)
#: The `alert` key a refused-by-kind attempt puts in its evidence.
REFUSED_BY_KIND_ALERT = credit_lane.REFUSED_BY_KIND_ALERT


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _instant(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        found = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo is not None else found.replace(tzinfo=dt.timezone.utc)


def _usd(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def events(path: Path) -> Iterator[dict]:
    """The harness's `stream.jsonl` records (`"v": 1`), read only as a regular file."""
    try:
        with open_stream(path, "r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("v") == 1 and isinstance(event.get("type"), str):
                    yield event
    except (FileNotFoundError, NotRegularFile):
        return


def model_matches(served: Any, requested: str | None) -> bool:
    """C-12.5: the served model is the requested one, or a dated snapshot of it."""
    return (isinstance(served, str) and isinstance(requested, str) and requested != ""
            and re.fullmatch(re.escape(requested) + r"(-[0-9]{8})?", served) is not None)


def parse_guard(guard_override: str | None) -> dict[str, str]:
    """The daemon's guard override for a credit launch: `{"guard_hook", "publish_hook"}` as JSON."""
    if not guard_override:
        return {}
    try:
        found = json.loads(guard_override)
    except ValueError:
        raise AdapterError("the credit lane's guard override is not the daemon's JSON", code=7,
                           fix="rerun the launch; the daemon builds this override", reason="api-guard-override") from None
    if not isinstance(found, dict):
        raise AdapterError("the credit lane's guard override is not the daemon's JSON", code=7,
                           fix="rerun the launch; the daemon builds this override", reason="api-guard-override")
    return {key: value for key in ("guard_hook", "publish_hook")
            if isinstance(value := found.get(key), str) and value}


def _source_effort(source_dir: Path) -> str | None:
    """The `--effort` the attempt a resume continues launched with (its `launch.json`)."""
    try:
        argv = json.loads(read_regular(source_dir / "launch.json", limit=1 << 20)).get("argv") or []
    except (OSError, ValueError, AttributeError, NotRegularFile):
        return None
    for flag, value in zip(argv, argv[1:]):
        if flag == "--effort" and isinstance(value, str):
            return value
    return None


class ClaudeApiAdapter(Adapter):
    """C-31.1: the `claude-api` provider."""

    provider = API_PROVIDER

    def __init__(self, *, now=None, python: str | None = None, registry_reader=None):
        self.now = now or _now
        self.python = python or sys.executable
        self.registry_reader = registry_reader or credit_lane.read_registry
        #: The policy's `claude_api` section; the daemon sets it (`Daemon._adapter`).
        self.credit_policy: dict[str, Any] = {}
        #: C-31.4: `{"credit_usd", "ledger_usd", "in_flight_usd"}`, which the daemon
        #: reads from the store and sets before each launch. None refuses the launch.
        self.credit_budget: dict[str, Any] | None = None

    # --- enrolment and probes ---------------------------------------------------------------

    def enroll(self, credential: Credential) -> LaneInfo:
        """C-31.1, C-31.2: a credit lane for an email the registry holds with a key
        fingerprint and an organization. The organization becomes the lane's identity
        (C-10.6). No key is read here and no request is sent: the first attempt's
        first request is the lane's check (C-31.5)."""
        email = credit_lane.reference_email(credential.ref)
        if credential.provider != API_PROVIDER or credential.kind != "keychain-token" or email is None:
            raise credit_lane.refused("an API credit lane is enrolled from claude-api-<email>",
                                      "enroll with the plan's email: subfleet lanes enroll --api <email>", code=2)
        entry = credit_lane.registry_entry(email, self.registry_reader())
        org = credit_lane.entry_org(entry)
        if credit_lane.entry_fingerprint(entry) is None or org is None:
            raise credit_lane.refused("the claude-credits registry holds no key fingerprint and organization for "
                                      "that email", "store the plan's Console API key with `claude-credits add` first",
                                      code=5)
        if org in credit_lane.denied_org_ids({"claude_api": self.credit_policy}):
            raise credit_lane.refused("that email's organization is denied by policy (claude_api.denied_org_ids)",
                                      "enroll a plan whose organization is not denied")
        return LaneInfo(account_key=credit_lane.account_key(email), plan=credit_lane.PLAN, home=None, readings=(),
                        identity=org, identity_status=IdentityStatus.VERIFIED.value, label=email)

    def probe(self, lane: Lane, credential_env: dict[str, str]) -> tuple[Reading, ...]:
        """C-31.5: a standard key cannot read its balance, so there is no sensor."""
        return ()

    # --- launches ---------------------------------------------------------------------------

    def _limits(self) -> tuple[float, int]:
        policy = self.credit_policy or {}
        cap = _usd(policy.get("max_usd_per_attempt"))
        turns = policy.get("max_turns_per_attempt")
        return (DEFAULT_MAX_USD if cap is None else cap,
                turns if isinstance(turns, int) and not isinstance(turns, bool) and turns > 0 else DEFAULT_MAX_TURNS)

    def _grant(self, lane: Lane, renews_day: int | None) -> tuple[float, dict[str, Any]]:
        """C-31.4, invariant 8: this launch's `--max-usd` and the record of it."""
        budget = self.credit_budget
        credit = _usd((budget or {}).get("credit_usd"))
        if budget is None or not credit:
            raise AdapterError("the credit lane has no cycle ledger or credit for this launch", code=7,
                               fix="re-enroll the credit lane with --credit-usd", reason="api-credit-ledger")
        cap, _ = self._limits()
        ledger, in_flight = _usd(budget.get("ledger_usd")) or 0.0, _usd(budget.get("in_flight_usd")) or 0.0
        value = credit_lane.grant(credit, cap, ledger, in_flight)
        record = {"credit_usd": credit, "ledger_usd": ledger, "in_flight_usd": in_flight, "cap_usd": cap,
                  "credit_grant_usd": value}
        if value < credit_lane.MIN_GRANT_USD:
            today = credit_lane.local_date(self.now())
            until = credit_lane.next_renewal(renews_day, today) or today + dt.timedelta(days=1)
            raise AdapterError("the credit lane's cycle budget is spent", code=4,
                               fix="the lane reopens at the plan's next renewal", reason="api-credit-spent",
                               details={**record, "until_at": _iso(credit_lane.local_midnight(until)),
                                        "clock_source": (ClockSource.REPORTED if renews_day else
                                                         ClockSource.GUESSED).value})
        return value, record

    def _renews_day(self, lane: Lane) -> int | None:
        email = credit_lane.reference_email(lane.credential.ref)
        return credit_lane.renews_day(credit_lane.registry_entry(email, self.registry_reader())) if email else None

    def _launch(self, job: JobSpec, attempt_id: str, attempt_dir: Path, lane: Lane, credential_env: dict[str, str],
                model_id: str, effort: str | None, prompt_path: Path, guard_override: str | None,
                resume: bool) -> Launch:
        sandbox = Sandbox(job.sandbox)
        if job.isolated_review:
            # C-31.1: the harness offers no isolated-review mode; a gate review runs elsewhere.
            raise AdapterError("an API credit lane cannot run an isolated review", code=7,
                               fix="route the review to a Codex or Claude lane", reason="api-isolated-review")
        if job.kind == "turn":
            raise AdapterError("an API credit lane cannot run a conversation turn", code=7,
                               fix="continue the conversation on its own provider", reason="api-turn")
        key = credential_env.get("ANTHROPIC_API_KEY")
        if not key:
            # Invariant 1: a credit launch carries its lane's resolved credit key or none starts.
            raise credit_lane.refused("an API credit lane launches only with its resolved credit key",
                                      "resolve the lane's credential with credentials.resolve_credential")
        hooks = parse_guard(guard_override)
        if sandbox == Sandbox.WORKSPACE_WRITE and not hooks.get("guard_hook"):
            raise AdapterError("Writable credit-lane jobs require the never-rules guard", code=7,
                               fix="Run the guard trust preflight (subfleet doctor) and stage the reviewed hook.",
                               reason="api-guard-required")
        if sandbox == Sandbox.WORKSPACE_WRITE and publishing_denied(job) and not hooks.get("publish_hook"):
            raise AdapterError("Writable credit-lane reviews require the review publish hook", code=7,
                               fix="Install the review publish hook under the state root.",
                               reason="api-publish-hook-required")
        renews_day = self._renews_day(lane)
        grant, record = self._grant(lane, renews_day)
        _, turns = self._limits()
        attempt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        sent_path = attempt_dir / "prompt.sent.md"
        atomic_publish(sent_path, read_regular(prompt_path))
        argv = [self.python, "-I", "-m", HARNESS_MODULE, "--attempt-dir", str(attempt_dir),
                "--workdir", job.workdir, "--prompt", str(sent_path), "--model", model_id,
                "--effort", effort or "high", "--sandbox", sandbox.value,
                "--max-usd", f"{grant:.2f}", "--max-turns", str(turns)]
        if job.network and sandbox == Sandbox.WORKSPACE_WRITE:
            argv.append("--network")
        if hooks.get("guard_hook"):
            argv += ["--guard-hook", hooks["guard_hook"]]
        if publishing_denied(job) and hooks.get("publish_hook"):
            argv += ["--publish-hook", hooks["publish_hook"]]
        if resume:
            argv.append("--resume")
        # The daemon sets TMPDIR itself; names the policy's `env.pass` selected ride along, as on Codex.
        env = {**{name: value for name, value in credential_env.items() if name != "TMPDIR"},
               "ANTHROPIC_API_KEY": key, "SUBFLEET_API_LANE": lane.lane_id}
        notes = {"capability": launch_capability(API_PROVIDER, sandbox).value, "sandbox": sandbox.value,
                 "attempt_id": attempt_id, "lane_id": lane.lane_id, "identity": lane.identity, "label": lane.label,
                 "model_requested": model_id, "renews_day": renews_day, "max_turns": turns, **record}
        launch = Launch(tuple(argv), env, ENV_REMOVE, job.workdir, None, str(attempt_dir / "stdout"),
                        str(attempt_dir / "stderr"), str(attempt_dir / STREAM), attempt_id, lane.lane_id, notes=notes)
        return protect_launch(job, launch, attempt_dir)

    def build_launch(self, job: JobSpec, attempt_id: str, attempt_dir: Path, lane: Lane,
                     credential_env: dict[str, str], model_id: str, effort: str | None,
                     prompt_path: Path, guard_override: str | None) -> Launch:
        """C-31.1: exactly the interface's argv, `env_add` and `env_remove`."""
        return self._launch(job, attempt_id, attempt_dir, lane, credential_env, model_id, effort, prompt_path,
                            guard_override, resume=False)

    def resume_launch(self, job: JobSpec, attempt_id: str, attempt_dir: Path, lane: Lane,
                      credential_env: dict[str, str], native_session_id: str,
                      prompt_path: Path, guard_override: str | None,
                      model_id: str | None = None) -> Launch | None:
        """C-31.1: the same launch plus `--resume`, from the source attempt's history.

        The native session id is the source attempt's id (`<job>/a<seq>`, which
        `classify` records), so its folder is beside this one under `jobs/`. Its
        `messages.jsonl` is copied into this attempt's folder for the harness."""
        if not model_id:
            raise AdapterError("a credit-lane resume needs the source attempt's model", code=7,
                               fix="resubmit the resume from the original job", reason="api-resume-model")
        if not isinstance(native_session_id, str) or not _ATTEMPT_ID.fullmatch(native_session_id) \
                or ".." in native_session_id:
            raise AdapterError("a credit-lane resume names the attempt it continues", code=7,
                               fix="resume from the original job's attempt id", reason="api-resume-session")
        jobs = attempt_dir.parent.parent
        source = jobs.joinpath(*native_session_id.split("/")) / HISTORY
        try:
            history = read_regular(source, limit=HISTORY_MAX_BYTES)
        except (OSError, NotRegularFile):
            history = b""
        if not history.strip():
            raise AdapterError("the attempt this resume continues left no conversation history", code=7,
                               fix="submit the work as a new job", reason="api-resume-history")
        attempt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_publish(attempt_dir / HISTORY, history)
        return self._launch(job, attempt_id, attempt_dir, lane, credential_env, model_id,
                            _source_effort(source.parent), prompt_path, guard_override, resume=True)

    # --- classification ---------------------------------------------------------------------

    def classify(self, attempt_dir: Path, launch: Launch, exit_info: ExitInfo) -> Outcome:
        """C-31.4: the harness's `end` event decides; its exit code only when there is none."""
        notes = launch.notes or {}
        found = list(events(Path(launch.raw_stream_path or attempt_dir / STREAM)))
        responses = [event for event in found if event["type"] == "response"]
        errors = [event for event in found if event["type"] == "error"]
        credits = [event for event in found if event["type"] == "credit"]
        ends = [event for event in found if event["type"] == "end"]
        end = ends[-1] if ends else None
        outcome = end.get("outcome") if end else EXIT_OUTCOMES.get(exit_info.rc, "error")
        if outcome not in END_OUTCOMES:
            outcome = "error"
        responses_usd = sum(_usd(event.get("usd")) or 0.0 for event in responses)
        identity = notes.get("identity")
        foreign = [event for event in responses if identity and event.get("org_id") != identity]
        evidence: dict[str, Any] = {
            "rc": exit_info.rc, "signal": exit_info.signal, "lane": launch.lane_id,
            "end": bool(end), "end_outcome": outcome, "turns": end.get("turns") if end else None,
            # C-31.4: the ledger reads `usd`; `responses_usd` bounds an attempt that ended without it.
            "usd": _usd(end.get("usd")) if end else None, "responses_usd": round(responses_usd, 6),
            "responses": len(responses), "model_answered": bool(responses) if found else None,
            "credit_grant_usd": _usd(notes.get("credit_grant_usd")),
        }
        if credits:
            evidence["credit"] = {key: credits[-1].get(key) for key in ("verdict", "check_status")}
        if errors:
            evidence["error"] = {key: errors[-1].get(key) for key in ("status", "error_type")}
        if exit_info.spawn_error:
            evidence["spawn_error"] = exit_info.spawn_error
        if foreign:
            # C-10.6, invariant 7: a response from another organization holds the lane
            # until an operator enrolls it again (`Daemon._record_identity`).
            evidence["identity"] = {"status": IDENTITY_EVIDENCE[IdentityStatus.MISMATCH],
                                    "responses": len(foreign)}
        session = notes.get("attempt_id") or launch.native_session_id

        def result(cls: OutcomeClass, detail: str, closure: Closure | None = None) -> Outcome:
            return Outcome(cls, detail, evidence=dict(evidence), closure=closure, native_session_id=session)

        seen_at = _instant((credits[-1] if credits else end or {}).get("ts")) or self.now()
        seen = credit_lane.local_date(seen_at)
        renews_day = credit_lane.renews_day({"renews_day": notes.get("renews_day")})
        if foreign:
            return result(OutcomeClass.UNKNOWN, "a response came from another organization than the lane's")
        if outcome == "done":
            if exit_info.spawn_error or not self.deliverable(attempt_dir, launch, result(OutcomeClass.UNKNOWN, "")):
                return result(OutcomeClass.UNKNOWN, "the harness ended without a deliverable")
            return result(OutcomeClass.OK, "the harness completed with a deliverable")
        if outcome == "credit":
            # C-31.4, invariant 5: closed until the plan's next renewal (one day when
            # that is unknown or falls today), when new credit arrives.
            until = credit_lane.exhausted_until(renews_day, seen)
            renewal = credit_lane.renewal_on_or_after(renews_day, seen) if renews_day else None
            clock = ClockSource.REPORTED if renewal is not None and renewal > seen else ClockSource.GUESSED
            return result(OutcomeClass.LIMITED, "the plan's API credit is exhausted", Closure(
                launch.lane_id, "account", _iso(credit_lane.local_midnight(until)), ClosureReason.CREDITS, clock,
                "api-credit:exhausted"))
        if outcome == "workspace-limit":
            # C-31.4: a workspace's spend limit, which its holder can raise at any time.
            until = credit_lane.exhausted_until(renews_day, seen, workspace=True)
            return result(OutcomeClass.LIMITED, "the workspace's API usage limit is reached", Closure(
                launch.lane_id, "account", _iso(credit_lane.local_midnight(until)), ClosureReason.CREDITS,
                ClockSource.GUESSED, "api-credit:workspace-limit"))
        if outcome == "refused-by-kind":
            # C-31.4: the credit refused the request's kind; our own system prompt should
            # make that impossible. The lane stays open and the daemon raises an alert.
            evidence["alert"] = {"key": REFUSED_BY_KIND_ALERT, "severity": "critical",
                                 "title": "claude-api: the credit refused a request by kind",
                                 "body": "The API credit refused a request that a neutral check accepted. "
                                         "Check the harness's system prompt; the lane stays open."}
            return result(OutcomeClass.UNKNOWN, REFUSED_BY_KIND_ALERT)
        status = (evidence.get("error") or {}).get("status")
        if outcome == "error" and status == 429:
            # C-9.4: a rate limit, as the Claude adapter treats a rejected rate_limit_event:
            # a guessed short closure.
            return result(OutcomeClass.LIMITED, "the API rate-limited the lane", Closure(
                launch.lane_id, "account", _iso(self.now() + dt.timedelta(seconds=GUESSED_CLOSURE_S)),
                ClosureReason.PROVIDER_LIMIT, ClockSource.GUESSED, "api-credit:429"))
        if outcome == "error" and status == 401:
            return result(OutcomeClass.AUTH_DEAD, "the API refused the lane's key (401)")
        if outcome in ("budget", "turns"):
            return result(OutcomeClass.UNKNOWN, f"the attempt reached its {'dollar grant' if outcome == 'budget' else 'turn cap'}")
        if exit_info.signal:
            return result(OutcomeClass.UNKNOWN, f"the harness ended on signal {exit_info.signal}")
        return result(OutcomeClass.UNKNOWN, "the harness ended with an error"
                      + (f" (HTTP {status})" if isinstance(status, int) else ""))

    def attest(self, attempt_dir: Path, launch: Launch, outcome: Outcome, model_id: str) -> AttestationResult:
        """C-12.5: every response's served `model` is the requested one, from the lane's
        organization. Never a false positive: no response is unattested."""
        notes = launch.notes or {}
        responses = [event for event in events(Path(launch.raw_stream_path or attempt_dir / STREAM))
                     if event["type"] == "response"]
        if not responses:
            return AttestationResult(Attestation.UNATTESTED, None, "no response event")
        identity = notes.get("identity")
        if (outcome.evidence or {}).get("identity") or any(
                not identity or event.get("org_id") != identity for event in responses):
            return AttestationResult(Attestation.MISMATCH, None, "a response came from another organization")
        served = [event.get("model") for event in responses]
        wrong = next((model for model in served if not model_matches(model, model_id)), None)
        if wrong is not None or any(not isinstance(model, str) for model in served):
            return AttestationResult(Attestation.MISMATCH, wrong if isinstance(wrong, str) else None,
                                     "a response named another model")
        return AttestationResult(Attestation.ATTESTED, served[-1], "response.model")

    def deliverable(self, attempt_dir: Path, launch: Launch, outcome: Outcome) -> bytes | None:
        """C-12.6: the bytes of the harness's `last.md`."""
        try:
            data = read_regular(attempt_dir / LAST)
        except (OSError, NotRegularFile):
            return None
        return data if data.strip() else None

    def model_answered(self, event: object) -> bool:
        """C-6.14: a `response` event is the model answering on the lane's key."""
        return isinstance(event, dict) and event.get("v") == 1 and event.get("type") == "response"
