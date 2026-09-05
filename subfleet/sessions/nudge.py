"""Tickle and muster: waking a live session that a restart cut off.

A Claude account switch, or an app relaunch, restarts every open session. The
conversation comes back, but a session that was mid-turn just sits there until
Max opens it and types "." — one session at a time. Tickle pushes a *continue*
message into the session's own inbox, which starts a turn exactly as the "."
would. Muster is the roll call after a switch that killed nothing: it also
reaches recently-idle *completed* sessions and asks them to check for standing
work.

Tickle stays automatic (plan B, "Session continuity, kept beside the fleet")
precisely because it creates no second writer: it pushes a message into a live
session's own inbox and nothing else. Everything expensive or exclusive lives in
`revive.py`.

The rules, all of them from C-23.33 and C-23.34:

* only an interrupted turn is nudged, and only one younger than
  `sessions.nudge_max_age_h` (8 h) — an old abandoned turn is not resumed just
  because a tab reopened;
* a `SessionStart` wake is honoured only for sources `startup` and `resume`,
  never `compact` or `clear` — compaction is not a restart;
* at most one nudge per interruption point, and no more often than
  `sessions.nudge_cooldown_min` allows;
* the hook records the wake and decides nothing: eligibility is re-decided here,
  against the transcript as it reads after the delay, and a session whose real
  last turn changed during the wait is skipped;
* a sweep started by hand waits for a longer quiet window than a `SessionStart`
  wake does, because outside a restart an interrupted tail is often just a long
  tool call.

Delivery is one `ping` — a notice row the daemon's delivery ladder carries
(C-15.2). This package never opens a session's socket.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from . import registry, transcripts
from .transcripts import MARKER, MUSTER_MARKER, TurnState

#: A `SessionStart` whose source is not a restart never earns a nudge (C-23.33).
RESTART_SOURCES = frozenset({"startup", "resume"})

#: v1's operator kill switch, kept because it is the one thing a person reaches
#: for when an automatic sweep misbehaves at 07:00 and the skills document it.
TICKLE_ENV = "SUBFLEET_TICKLE"
OFF = frozenset({"off", "0", "false", "no"})

#: The scopes `sessions continue` accepts. `interrupted` is v1's `tickle`,
#: `idle` is v1's `muster`, and `cold` is v1's `revive` — which in v2 defaults
#: to a handoff (`revive.py`) unless `--revive` opts in.
SCOPES = ("interrupted", "idle", "cold")


def nudge_text(state: TurnState) -> str:
    """v1 `tickle.message`, unchanged in substance."""
    limit_line = ("The previous account or model hit its usage limit mid-task; "
                  "you are on a fresh one now.\n" if state.limit_banner else "")
    detail = state.detail or "its last turn was interrupted"
    return (
        f"{MARKER} (Claude account switch or app relaunch) with its last turn cut "
        f"off — {detail}. Continue where you left off.\n{limit_line}"
        "Before redoing anything: `git log --oneline -5` in your worktree and "
        "`subfleet runs --mine` — detached runs survived the restart and may "
        "already be finished (their completion notices arrive separately).\n"
        "(automated resume nudge from subfleet; no reply needed)"
    )


def muster_text(state: TurnState) -> str:
    """v1 `tickle.muster_message`, unchanged in substance."""
    return (
        f"{MUSTER_MARKER} after an account or model switch (nothing was killed — "
        "turns ended normally, e.g. the previous account moved to usage credits). "
        "Check for standing or pending work: your last instructions, any task "
        "notifications above, and `subfleet runs --mine` for detached runs that "
        "finished meanwhile. If something is pending, continue it now; if you are "
        "genuinely done, say so in one line and stand by.\n"
        "(automated roll call from subfleet; no reply beyond that is needed)"
    )


@dataclass
class Outcome:
    """What one session got, and why."""

    session_id: str
    scope: str
    state: TurnState = field(default_factory=TurnState)
    eligible: bool = False
    reason: str = ""
    recorded: bool = False
    delivered: bool = False
    notice_id: int | None = None
    name: str | None = None
    pid: int | None = None
    live_pids: tuple[int, ...] = ()
    duplicate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "scope": self.scope,
                "eligible": self.eligible, "reason": self.reason,
                "recorded": self.recorded, "delivered": self.delivered,
                "notice_id": self.notice_id, "name": self.name, "pid": self.pid,
                "live_pids": list(self.live_pids), "duplicate": self.duplicate,
                "state": self.state.to_dict()}


def caps(policy: dict[str, Any]) -> dict[str, float]:
    """The C-6.4 caps this module reads, in seconds."""
    settings = policy.get("sessions", {})
    return {
        "max_age_s": float(settings.get("nudge_max_age_h", 8)) * 3600,
        "cooldown_s": float(settings.get("nudge_cooldown_min", 1.5)) * 60,
        "delay_s": float(settings.get("nudge_delay_s", 8)),
        "sample_s": float(settings.get("nudge_sample_s", 3)),
        "sweep_quiet_s": float(settings.get("sweep_quiet_s", 120)),
        "muster_max_age_s": float(settings.get("muster_max_age_h", 2)) * 3600,
        "muster_quiet_s": float(settings.get("muster_quiet_s", 120)),
    }


def enabled(env: dict[str, str] | None = None) -> bool:
    """`SUBFLEET_TICKLE=off` stops every automatic nudge (v1's own switch)."""
    values = os.environ if env is None else env
    return (values.get(TICKLE_ENV) or "on").strip().lower() not in OFF


def source_allows(source: str | None) -> bool:
    """C-23.33: only `startup` and `resume` are restarts."""
    return source is None or source in RESTART_SOURCES


def spawn(session_id: str, *, source: str | None, transcript: str | Path | None,
          delay_s: float, root: str | Path | None = None,
          popen=None) -> int | None:
    """Start the detached worker a `SessionStart` hook needs (C-23.34).

    The hook must return at once — the inbox binds a moment after SessionStart,
    and a hook that blocks blocks the session — so it records the wake by
    handing the session id, its source and the transcript to a worker that
    re-decides everything after the delay. `python -m` rather than a console
    script: the guardian is launched the same way, and neither depends on PATH.
    """
    import subprocess
    launcher = popen or subprocess.Popen
    command = [sys.executable, "-m", "subfleet.sessions.cli", "continue",
               "--scope", "interrupted", "--session", session_id,
               "--delay", str(delay_s)]
    if source:
        command += ["--source", source]
    if transcript:
        command += ["--transcript", str(transcript)]
    environment = dict(os.environ)
    if root:
        environment["SUBFLEET_HOME"] = str(root)
    package = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = (package + os.pathsep + environment["PYTHONPATH"]
                                 if environment.get("PYTHONPATH") else package)
    try:
        process = launcher(command, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           start_new_session=True, close_fds=True, env=environment)
    except OSError:
        return None
    return process.pid


def decide(session_id: str, state: TurnState, *, scope: str, limits: dict[str, float],
           retired: dict | None = None, last_nudge: dict | None = None,
           source: str | None = None, force: bool = False,
           now: datetime | None = None) -> tuple[bool, str]:
    """Is this session eligible for a nudge right now? (C-23.33)

    The store's dedupe and cooldown are re-checked by the daemon inside the
    transaction that records the nudge; checking them here too is not
    redundancy, it is what lets a `--dry-run` explain itself and what keeps a
    sweep from asking the daemon about forty sessions it already knows to skip.
    """
    now = now or datetime.now(timezone.utc)
    if retired:
        # C-23.35: a retired session is absent from every listing and is never a
        # candidate until the operator clears it.
        reason = retired.get("reason") or "no reason recorded"
        return False, f"retired by the operator ({reason})"
    if not source_allows(source) and not force:
        return False, f"source {source!r} is not a restart"

    if scope == "idle":
        if state.state not in ("interrupted", "completed"):
            return False, f"{state.state}: {state.detail}"
        window = limits["muster_max_age_s"]
        if state.age_s is not None and state.age_s > window and not force:
            return False, (f"last turn {state.age_s}s ago, outside the "
                           f"{int(window)}s roll-call window")
    else:
        if state.state != "interrupted":
            return False, f"{state.state}: {state.detail}"
        if state.age_s is not None and state.age_s > limits["max_age_s"] and not force:
            return False, (f"interrupted {state.age_s}s ago, older than the "
                           f"{int(limits['max_age_s'])}s cap")
    if force:
        return True, f"{state.state}: {state.detail} (forced)"
    if last_nudge and last_nudge.get("dedupe_key") and state.dedupe_key \
            and last_nudge["dedupe_key"] == state.dedupe_key:
        return False, "already nudged at this interruption point"
    if last_nudge and last_nudge.get("at"):
        elapsed = _age_s(last_nudge["at"], now)
        if elapsed is not None and elapsed < limits["cooldown_s"]:
            return False, (f"nudged {int(elapsed)}s ago "
                           f"(cooldown {int(limits['cooldown_s'])}s)")
    return True, f"{state.state}: {state.detail}"


def _age_s(stamp: str, now: datetime) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds()


@dataclass
class Report:
    """One sweep. `duplicates` names both live pids of every doubled session."""

    scope: str
    outcomes: list[Outcome] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    skipped_lanes: list[str] = field(default_factory=list)

    @property
    def delivered(self) -> list[Outcome]:
        return [item for item in self.outcomes if item.delivered]

    @property
    def recorded(self) -> list[Outcome]:
        return [item for item in self.outcomes if item.recorded]

    def to_dict(self) -> dict[str, Any]:
        return {"scope": self.scope, "duplicates": list(self.duplicates),
                "skipped_lanes": list(self.skipped_lanes),
                "sessions": [item.to_dict() for item in self.outcomes]}


def sweep(sessions, policy: dict[str, Any], *, scope: str = "interrupted",
          only: Sequence[str] = (), transcript: str | Path | None = None,
          source: str | None = None, force: bool = False, dry_run: bool = False,
          delay_s: float | None = None, manual: bool = True,
          caller: str | None = None,
          now: Callable[[], datetime] | None = None,
          sleep: Callable[[float], None] = time.sleep) -> Report:
    """Nudge every eligible session once (C-23.33, C-23.34).

    `sessions` is a `client.Sessions`. `manual` marks a sweep a person started,
    which requires the longer quiet window; a `SessionStart` wake passes
    `manual=False` and its own `source`.

    The wait before the re-check is per session and differs by path, exactly as
    v1's did: a `SessionStart` wake waits `nudge_delay_s` (8 s) because the
    inbox binds a moment after the hook runs, and a sweep waits the much shorter
    `nudge_sample_s` (3 s) because it is only sampling for activity and would
    otherwise spend eight seconds per session on a fleet of twenty.

    `caller` is the session running the sweep, and a sweep never nudges it: a
    long tool call writes no turns, so the sweeping session looks interrupted to
    itself. Naming it explicitly still works — that is a person's decision.
    """
    clock = now or (lambda: datetime.now(timezone.utc))
    limits = caps(policy)
    wanted = {value for value in only if value}
    report = Report(scope=scope)
    if not force and not enabled():
        # v1's kill switch. `--force` is a person naming one session, and a
        # person who typed the command outranks the switch they set this morning.
        report.outcomes.append(Outcome(session_id="", scope=scope,
                                       reason=f"disabled ({TICKLE_ENV}=off)"))
        return report

    # One `state` call, before the registry is read: with no ids it answers for
    # every session that carries a retirement or nudge record, which is exactly
    # the set that can be skipped — a session with no record has nothing to say.
    facts = sessions.state(sorted(wanted) if wanted else None)
    lane_ids = lane_ids_of(facts)
    state_by_id = facts.get("sessions") or {}
    # A caller who names a session gets an answer about it even when it is a
    # lane run, so the refusal can name the reason (C-23.31).
    listing = registry.sessions(lane_ids=lane_ids, include_lanes=bool(wanted),
                                live_only=True)
    if wanted:
        listing = [item for item in listing if item.session_id in wanted]
        for session_id in sorted(wanted - {item.session_id for item in listing}):
            report.outcomes.append(Outcome(
                session_id=session_id, scope=scope,
                reason="not a live registered session (no inbox to reach)"))
    report.duplicates = registry.duplicate_report(listing)

    quiet_s = limits["muster_quiet_s"] if scope == "idle" else limits["sweep_quiet_s"]
    default_wait = limits["sample_s"] if manual else limits["delay_s"]
    wait_s = default_wait if delay_s is None else float(delay_s)

    for item in listing:
        if caller and item.session_id == caller and item.session_id not in wanted:
            # v1's rule: a long tool call writes no turns, so the session running
            # the sweep can look dead to itself and nudge itself mid-work.
            report.outcomes.append(Outcome(
                session_id=item.session_id, scope=scope, name=item.row.name,
                pid=item.pid, reason="this session (a sweep never nudges itself)"))
            continue
        if item.lane:
            # C-23.31: a headless lane run is never a notice target; its
            # deliverable is its last message and a nudge would become it.
            report.skipped_lanes.append(item.session_id)
            report.outcomes.append(Outcome(
                session_id=item.session_id, scope=scope, name=item.row.name,
                pid=item.pid, reason="headless lane run — never nudged (C-23.31)"))
            continue
        # `--transcript` overrides the lookup for a single named session, which
        # is how v1's `tickle --session ID --transcript P` reached a transcript
        # the registry could not locate.
        path = (Path(transcript) if transcript and len(wanted) == 1
                else transcripts.transcript_path(item.session_id))
        before = transcripts.turn_state(path, now=clock())
        facts_for = state_by_id.get(item.session_id) or {}
        outcome = Outcome(session_id=item.session_id, scope=scope, state=before,
                          name=item.row.name, pid=item.pid,
                          live_pids=item.live_pids, duplicate=item.duplicate)
        eligible, reason = decide(
            item.session_id, before, scope=scope, limits=limits,
            retired=facts_for.get("retired"), last_nudge=facts_for.get("last_nudge"),
            source=source, force=force, now=clock())
        outcome.eligible, outcome.reason = eligible, reason
        if not eligible:
            report.outcomes.append(outcome)
            continue
        if dry_run:
            outcome.reason = f"{reason} (dry run: nothing sent)"
            report.outcomes.append(outcome)
            continue

        if not force:
            # C-23.34: a session that is actually working keeps producing turns;
            # one cut off by a restart does not — the app's resume stub is not a
            # turn. So the real last turn must survive the wait.
            if wait_s > 0:
                sleep(wait_s)
            after = transcripts.turn_state(path, now=clock())
            if after.fingerprint != before.fingerprint:
                outcome.eligible = False
                outcome.reason = "session is active (a new turn appeared during the wait)"
                report.outcomes.append(outcome)
                continue
            outcome.state = after
            if manual and quiet_s and after.age_s is not None and after.age_s < quiet_s:
                outcome.eligible = False
                outcome.reason = (f"last activity {after.age_s}s ago; a hand-started "
                                  f"sweep waits {int(quiet_s)}s of quiet")
                report.outcomes.append(outcome)
                continue

        record = sessions.record_nudge(
            item.session_id, dedupe_key=outcome.state.dedupe_key,
            cooldown_s=None if force else limits["cooldown_s"], force=force,
            kind="muster" if scope == "idle" else "nudge",
            detail={"turn_uuid": outcome.state.last_uuid,
                    "restart_stubs": outcome.state.restart_stubs,
                    "pid": item.pid, "live_pids": list(item.live_pids)})
        outcome.recorded = bool(record.get("recorded"))
        if not outcome.recorded:
            outcome.eligible = False
            outcome.reason = record.get("reason") or "another sweep recorded this nudge"
            report.outcomes.append(outcome)
            continue

        body = (muster_text(outcome.state)
                if scope == "idle" and outcome.state.state == "completed"
                else nudge_text(outcome.state))
        result = sessions.ping(item.session_id, body)
        outcome.notice_id = result.get("notice_id")
        outcome.delivered = outcome.notice_id is not None
        if not outcome.delivered:
            outcome.reason = f"{reason}; the daemon parked no notice"
        report.outcomes.append(outcome)
    return report


def render(report: Report) -> str:
    """The operator's view of a sweep: what was sent, what was not, and why."""
    lines = []
    for item in report.outcomes:
        mark = "sent" if item.delivered else "held"
        who = item.name or item.session_id[:8]
        pid = f" pid {item.pid}" if item.pid else ""
        lines.append(f"  {mark:<5} {item.session_id[:8]} {who[:36]:<36}{pid}")
        lines.append(f"        {item.reason}")
    header = (f"{len(report.delivered)} nudged, "
              f"{len(report.outcomes) - len(report.delivered)} held "
              f"({report.scope})")
    body = "\n".join(lines) if lines else "  no live sessions in scope"
    duplicates = ("\n" + "\n".join(f"  duplicate {line}" for line in report.duplicates)
                  if report.duplicates else "")
    return f"{header}\n{body}{duplicates}"


def wake(sessions, policy: dict[str, Any], session_id: str, *, source: str | None,
         transcript: str | Path | None = None,
         **kwargs: Any) -> Report:
    """The `SessionStart` path: one session, its own source, no manual quiet gate.

    C-23.34: the hook records the wake and decides nothing. It calls this, which
    re-reads the transcript after the delay and re-applies every rule.
    """
    return sweep(sessions, policy, scope="interrupted", only=[session_id],
                 transcript=transcript, source=source, manual=False, **kwargs)


def lane_ids_of(facts: dict[str, Any]) -> set[str]:
    return set(facts.get("lane_sessions") or [])


__all__ = ["Outcome", "Report", "SCOPES", "caps", "decide", "lane_ids_of",
           "muster_text", "nudge_text", "render", "source_allows", "sweep", "wake"]
