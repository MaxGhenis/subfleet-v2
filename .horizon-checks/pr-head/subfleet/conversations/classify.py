"""Classification, attestation and deliverable of a turn attempt (C-26.8, design §7).

`_finalize` treats a turn like any attempt through `TurnAdapter`, which wraps
the provider's adapter. The class comes from the driver's recorded outcome
(`turn.json`, written by the runner) and structured provider evidence only:
never from what the model wrote (review F-01, F-02, IR-9, IR-10).

- Claude: `ClaudeAdapter.classify` over the attempt's stream-json stdout
  supplies readings (rate_limit_event), a closure with the provider's clock,
  and auth or CLI evidence from provider-marked error text (C-9.2 as amended by
  PR #39). The driver's outcome then decides: `complete` is `ok`; `limited` is
  `limited` with the adapter's closure or a guessed one; any other failure
  keeps only an auth-dead or cli-too-old verdict the adapter found in error
  text, and is `unknown` otherwise.
- Codex: the app-server stream's `account/rateLimits/updated` windows become
  readings; `usageLimitExceeded` / `rateLimitExceeded` is `limited` with a
  closure at the reached window's reset; `unauthorized` is `unknown` (C-9.3
  needs a usage-endpoint 401, not a turn error).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..adapters.claude import ClaudeAdapter
from ..contracts import (
    Attestation, AttestationResult, ClockSource, Closure, ClosureReason, ExitInfo, Launch, Outcome,
    OutcomeClass, Reading, ReadingLabel,
)
from ..sessions import transcripts
from ..state_files import open_state

GUESSED_S = 3600
CODEX_WINDOWS = {300: "five_hour", 10080: "seven_day"}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _epoch_iso(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = value / 1000 if value > 10**11 else value
    return _iso(datetime.fromtimestamp(seconds, UTC))


def read_turn(attempt_dir: Path) -> dict | None:
    try:
        with open_state(Path(attempt_dir) / "turn.json") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


class TurnAdapter:
    """What `_finalize` calls for a `kind='turn'` attempt."""

    def __init__(self, provider: str, inner=None):
        self.provider = provider
        self.inner = inner if inner is not None else (ClaudeAdapter() if provider == "claude" else None)

    # --- classification --------------------------------------------------------

    def classify(self, attempt_dir: Path, launch: Launch, exit_info: ExitInfo) -> Outcome:
        turn = read_turn(attempt_dir) or {}
        notes = dict(launch.notes or {})
        lane_id = str(notes.get("lane_id") or launch.lane_id or "")
        model_id = str(notes.get("model_id") or "")
        native = turn.get("native_session_id") or launch.native_session_id
        state, reason = turn.get("state"), turn.get("reason")
        evidence = {"turn_state": state, "turn_reason": reason, "accepted": turn.get("accepted"),
                    "answered": turn.get("answered"), "rc": exit_info.rc, "signal": exit_info.signal}
        if self.provider == "claude":
            base = self.inner.classify(attempt_dir, launch, exit_info)
            readings, base_closure, base_cls = base.readings, base.closure, base.cls
            transcript = base.transcript_path
            evidence["adapter"] = {"class": base.cls.value, "detail": base.detail}
        else:
            readings, reached = codex_readings(Path(attempt_dir) / "stdout", lane_id=lane_id,
                                               attempt_id=notes.get("attempt_id"))
            base_closure, base_cls, transcript = None, None, None
        served = (turn.get("served") or {}).get("model") or turn.get("served_model")

        def outcome(cls: OutcomeClass, detail: str, closure: Closure | None = None) -> Outcome:
            return Outcome(cls=cls, detail=detail, evidence=evidence, readings=tuple(readings), closure=closure,
                           native_session_id=native, transcript_path=transcript, served_model=served)

        if not turn:
            return outcome(OutcomeClass.UNKNOWN, "turn ended with no recorded outcome")
        if state == "complete":
            return outcome(OutcomeClass.OK, "turn complete" + ("; stop requested too late" if turn.get("stop_too_late") else ""))
        if reason == "limited" or turn.get("limited"):
            if self.provider == "claude":
                closure = base_closure if base_cls == OutcomeClass.LIMITED and base_closure else _guessed(
                    lane_id, "account", "limit reported on the turn")
            else:
                closure = _codex_closure(lane_id, reached, Path(attempt_dir) / "stdout")
            return outcome(OutcomeClass.LIMITED, f"limited: {turn.get('detail') or 'the provider refused for quota'}",
                           closure)
        if self.provider == "claude" and base_cls in (OutcomeClass.AUTH_DEAD, OutcomeClass.CLI_TOO_OLD):
            return outcome(base_cls, f"{base_cls.value}: {evidence['adapter']['detail']}")
        return outcome(OutcomeClass.UNKNOWN, f"turn {state or 'ended'}: {reason or 'no reason'}")

    # --- attestation -----------------------------------------------------------

    def attest(self, attempt_dir: Path, launch: Launch, outcome: Outcome, model_id: str) -> AttestationResult:
        if self.provider == "claude":
            return self.inner.attest(attempt_dir, launch, outcome, model_id)
        turn = read_turn(attempt_dir) or {}
        notes = dict(launch.notes or {})
        return codex_attest(notes.get("codex_home"), turn.get("native_session_id") or notes.get("thread_id"),
                            turn.get("turn_id"), model_id)

    # --- deliverable -----------------------------------------------------------

    def deliverable(self, attempt_dir: Path, launch: Launch, outcome: Outcome) -> bytes | None:
        turn = read_turn(attempt_dir) or {}
        text = turn.get("final_text")
        return text.encode("utf-8") if isinstance(text, str) and text else None


def _guessed(lane_id: str, scope: str, source: str) -> Closure:
    return Closure(lane_id=lane_id, scope=scope, until_at=_iso(_now() + timedelta(seconds=GUESSED_S)),
                   reason=ClosureReason.PROVIDER_LIMIT, clock_source=ClockSource.GUESSED, source_event=source)


def codex_readings(stdout: Path, *, lane_id: str, attempt_id: str | None) -> tuple[list[Reading], dict | None]:
    """`account/rateLimits/updated` windows as provider readings, and the last
    snapshot (whose reached window dates a closure)."""
    readings: dict[tuple[str, str], Reading] = {}
    last: dict | None = None
    observed = _iso(_now())
    try:
        with open_state(stdout) as stream:
            lines = stream.read().splitlines()
    except OSError:
        return [], None
    for raw in lines:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(msg, dict) or msg.get("method") != "account/rateLimits/updated":
            continue
        snapshot = (msg.get("params") or {}).get("rateLimits") or {}
        last = snapshot
        for slot in ("primary", "secondary"):
            window = snapshot.get(slot)
            if not isinstance(window, dict):
                continue
            minutes, used = window.get("windowDurationMins"), window.get("usedPercent")
            if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
                continue
            if not isinstance(used, (int, float)) or isinstance(used, bool) or not 0 <= used <= 100:
                continue
            key = CODEX_WINDOWS.get(minutes, str(minutes))
            readings[("account", key)] = Reading(
                lane_id=lane_id, scope="account", window=key, utilization=used / 100,
                resets_at=_epoch_iso(window.get("resetsAt")), label=ReadingLabel.PROVIDER,
                source="app-server", observed_at=observed, attempt_id=attempt_id)
    return list(readings.values()), last


def _codex_closure(lane_id: str, snapshot: dict | None, stdout: Path) -> Closure:
    """The reached window's reset, else a guessed hour (C-9.4)."""
    windows = [w for w in ((snapshot or {}).get(s) for s in ("primary", "secondary")) if isinstance(w, dict)]
    full = [w for w in windows if isinstance(w.get("usedPercent"), (int, float)) and w["usedPercent"] >= 100]
    candidates = full or windows
    resets = [_epoch_iso(w.get("resetsAt")) for w in candidates if _epoch_iso(w.get("resetsAt"))]
    credits = bool((snapshot or {}).get("credits")) and not full
    if resets:
        return Closure(lane_id=lane_id, scope="account", until_at=max(resets),
                       reason=ClosureReason.CREDITS if credits else ClosureReason.PROVIDER_LIMIT,
                       clock_source=ClockSource.REPORTED, source_event="account/rateLimits/updated")
    return _guessed(lane_id, "account", "turn/completed usage limit")


def codex_attest(home: str | None, thread_id: str | None, turn_id: str | None, model_id: str) -> AttestationResult:
    """The served model of this turn: `turn_context` records carrying our turn id
    in this thread's rollout (review: per-turn attestation, not a time window)."""
    if not home or not thread_id or not turn_id:
        return AttestationResult(Attestation.UNATTESTED, None, "no thread or turn id to attest")
    sessions = Path(home) / "sessions"
    matches = list(sessions.rglob(f"rollout-*{thread_id}.jsonl")) if sessions.is_dir() else []
    if len(matches) != 1:
        return AttestationResult(Attestation.UNATTESTED, None, f"{len(matches)} rollouts for {thread_id}")
    served = []
    try:
        with transcripts.open_regular(matches[0]) as stream:     # a lane's rollout: never a FIFO's open()
            for raw in stream:
                if b'"turn_context"' not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                payload = record.get("payload") or {}
                if record.get("type") == "turn_context" and payload.get("turn_id") == turn_id and payload.get("model"):
                    served.append(payload["model"])
    except OSError as exc:
        return AttestationResult(Attestation.UNATTESTED, None, f"rollout unreadable: {exc}")
    if not served:
        return AttestationResult(Attestation.UNATTESTED, None, f"no turn_context for turn {turn_id}")
    wrong = next((m for m in served if m != model_id), None)
    if wrong:
        return AttestationResult(Attestation.MISMATCH, wrong, str(matches[0]))
    return AttestationResult(Attestation.ATTESTED, served[-1], str(matches[0]))
