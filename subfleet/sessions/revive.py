"""Revive: continuing a session whose process died — the exclusive one.

A cold session cannot be nudged: the inbox needs a process. Something has to
start one. v1 did it directly, `claude -p --resume <id>` detached under setsid,
and on 2026-09-04 that produced the twin — a headless revive running alongside a
desktop session the app had restarted between the census and the launch, both of
them writing the same worktree and re-dispatching the same lanes.

v2 changes three things and keeps everything else:

* **It is off by default for sessions the desktop app owns** (plan decision 7,
  policy `sessions.auto_revive_desktop_owned`). A lease in subfleet's store binds
  only launches subfleet makes; the desktop app never takes it, so no lease can
  make a headless revive exclusive against a desktop restart. Recovery of a cold
  session therefore defaults to an explicit handoff — a fresh native session with
  the continuity brief — and `--revive` is how the operator says otherwise.
* **It is a job**, of kind `revive`, submitted through the ordinary path
  (C-23.54), so it inherits routing, the guard, salvage, the ledger and notices,
  and so the daemon — not this process — owns the launch.
* **The exclusion is a lease**, `session:<id>:revive`, taken in the transaction
  that admits the attempt (C-23.55). The census the sweep skips on is the lease
  rows read inside that transaction, not a snapshot taken at the start of a pass.

Everything v1 filtered on survives: the retirement marker (C-23.35), the
permission-mode check (C-23.35), the original-model rule (C-23.39), the age cap,
the headless-lane exclusion (C-23.31), and a live lane probe before the launch
(C-23.20) — which in v2 the daemon performs, because `scheduler.probe_required`
is unconditional for a revive and the decision it records carries the reading.

The job's `caller_session` is the session being revived, not the operator's.
That is deliberate: it makes C-6.5 — "a writable job for a session id that
already has one running from another instance" — refuse exactly the twin, and it
puts the completion notice where the continued work lives.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..contracts import Sandbox
from ..policy import RETIRED_MODELS
from ..protocol import SubmitArgs
from . import registry, transcripts
from .transcripts import TurnState

#: The prompt a revived session receives. Carried over from v1 `REVIVE_MESSAGE`
#: including its exception, which exists because a session whose last message
#: asked Max a question must not answer it on his behalf.
REVIVE_MESSAGE = (
    "subfleet: this session was cut off (usage limit or account switch) and its "
    "process died; you are on a fresh account now. Continue where you left off. "
    "Before redoing anything: `git log --oneline -5` in your worktree and "
    "`subfleet runs --mine` — detached runs survived and may be finished. "
    "EXCEPTION — if your last message asked Max a question or offered him a "
    "decision (a design gate, a freeze, a go/no-go), do NOT proceed past it: "
    "re-state the open question in one line and stop; the interruption was not "
    "his answer. (automated resume from subfleet revive; no reply needed)"
)

#: v1's own lane-probe prompt. A transcript whose last turn is this one is
#: subfleet's own capacity probe, not resumable work.
PROBE_PROMPT = "Reply with exactly: ok"

#: The permission mode a revive admits, and only this one (C-23.35). A headless
#: `-p` run in any other mode auto-denies its own tools.
REQUIRED_MODE = "bypassPermissions"

OPT_IN_FIX = ("pass --revive to launch a headless continuation anyway, or "
              "`subfleet handoff <session> --to <model>` to continue the work in "
              "a fresh session")


class ReviveRefused(Exception):
    """A refusal with the fix named (C-6.5's shape, exit 7)."""

    code = 7

    def __init__(self, message: str, fix: str | None = None):
        super().__init__(message)
        self.fix = fix


@dataclass
class Candidate:
    """One session considered for revival, with everything the decision used."""

    session_id: str
    transcript: str | None = None
    state: TurnState = field(default_factory=TurnState)
    cwd: str | None = None
    permission_mode: str | None = None
    model: str | None = None
    desktop_owned: bool = True
    lane: bool = False
    retired: dict | None = None
    last_revive: dict | None = None
    live_pids: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "transcript": self.transcript,
                "cwd": self.cwd, "permission_mode": self.permission_mode,
                "model": self.model, "desktop_owned": self.desktop_owned,
                "lane": self.lane, "retired": self.retired,
                "last_revive": self.last_revive,
                "live_pids": list(self.live_pids), "state": self.state.to_dict()}


@dataclass
class Attempted:
    """What happened to one candidate."""

    session_id: str
    admitted: bool = False
    job_id: str | None = None
    reason: str = ""
    fix: str | None = None
    model: str | None = None
    candidate: Candidate | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "admitted": self.admitted,
                "job_id": self.job_id, "reason": self.reason, "fix": self.fix,
                "model": self.model,
                "candidate": self.candidate.to_dict() if self.candidate else None}


def session_store_dir() -> Path:
    """The desktop app's per-account Claude Code session store.

    `SUBFLEET_SESSION_STORE` is v1's own override and the tests' seam; nothing
    here writes to it.
    """
    override = os.environ.get("SUBFLEET_SESSION_STORE")
    if override:
        return Path(override).expanduser()
    return (Path.home() / "Library" / "Application Support" / "Claude"
            / "claude-code-sessions")


def store_metadata(session_id: str) -> dict[str, Any]:
    """`cwd`, `permissionMode` and `model` for a session, from any account copy.

    The desktop app writes one index file per session per account; the session's
    own identity (`cliSessionId`) is the same in all of them, so the first copy
    that names a cwd answers. A session present here at all is a session the
    desktop app owns.
    """
    base = session_store_dir()
    try:
        paths = sorted(base.glob("*/*/local_*.json"))
    except OSError:
        return {}
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("cliSessionId") != session_id:
            continue
        if not data.get("cwd"):
            continue
        model = data.get("model")
        return {"cwd": data["cwd"], "mode": data.get("permissionMode"),
                "model": model if isinstance(model, str) and model.strip() else None,
                "desktop_owned": True, "index": str(path)}
    return {}


def inspect(session_id: str, *, lane_ids: set[str], facts: dict[str, Any],
            transcript: str | Path | None = None,
            now: datetime | None = None) -> Candidate:
    """Everything the revive decision needs, read once."""
    path = Path(transcript) if transcript else transcripts.transcript_path(session_id)
    state = transcripts.turn_state(path, now=now or datetime.now(timezone.utc))
    meta = store_metadata(session_id)
    mode = meta.get("mode") or transcripts.last_permission_mode(path)
    model = meta.get("model") or transcripts.last_assistant_model(path)
    live = registry.find(session_id, lane_ids=lane_ids)
    return Candidate(
        session_id=session_id,
        transcript=str(path) if path else None,
        state=state,
        cwd=meta.get("cwd") or transcripts.last_cwd(path),
        permission_mode=mode,
        model=model,
        # A session the desktop store knows is one the app can restart under us;
        # one it does not know (a tmux CLI session, a lane) is not.
        desktop_owned=bool(meta.get("desktop_owned")),
        lane=registry.is_lane_run(session_id, lane_ids=lane_ids, transcript=path),
        retired=(facts.get("retired") if facts else None),
        last_revive=(facts.get("last_revive") if facts else None),
        live_pids=live.live_pids if live else (),
    )


def admits(candidate: Candidate, *, policy: dict[str, Any], opt_in: bool,
           force: bool = False) -> tuple[bool, str, str | None]:
    """Which sessions revive admits (C-23.31, C-23.35, plan decision 7).

    Returns `(admitted, reason, fix)`. The order matters: the refusals that name
    something the operator can act on come before the ones that do not.
    """
    settings = policy.get("sessions", {})
    if candidate.lane:
        # C-23.31: 2026-09-04, the sweep revived five dead `claude -p` lane runs
        # as untracked continuations on lane tokens — a lane's continuation has
        # no reader; it only burns a window.
        return False, "headless lane run (claude -p) — not resumable work", None
    if candidate.retired:
        reason = candidate.retired.get("reason") or "no reason recorded"
        return False, f"retired by the operator ({reason})", \
            f"subfleet sessions unretire {candidate.session_id}"
    if candidate.live_pids:
        return False, (f"already running (pid "
                       f"{', '.join(str(p) for p in candidate.live_pids)}) — "
                       f"a live session is nudged, never revived"), \
            "subfleet sessions continue --scope interrupted"
    if candidate.state.state == "empty":
        return False, "no transcript to continue", None
    if candidate.state.assistant_turns == 0:
        # A one-shot husk (our own lane probes, or a `-p` that never answered):
        # there is no conversation to resume.
        return False, "no assistant history — one-shot session, not resumable work", None
    if PROBE_PROMPT in (candidate.state.detail or ""):
        return False, "subfleet's own lane probe", None
    if candidate.state.state != "interrupted" and not force:
        return False, f"{candidate.state.state}: {candidate.state.detail}", None
    if not candidate.cwd:
        return False, "no cwd found (desktop store and transcript)", \
            "pass -C DIR to name the worktree"
    if candidate.permission_mode != REQUIRED_MODE:
        # C-23.35: a headless run in any other mode would deny its own tools.
        return False, (f"permission mode {candidate.permission_mode or 'unknown'} — "
                       f"a headless run would deny its own tools"), \
            f"only a {REQUIRED_MODE} session is revived; use handoff instead"
    minimum = float(settings.get("revive_min_age_s", 120))
    if candidate.state.age_s is not None and candidate.state.age_s < minimum and not force:
        return False, (f"only {int(candidate.state.age_s)}s old; the app may still "
                       f"restart it"), "wait, or pass --force"
    if candidate.desktop_owned and not opt_in \
            and not settings.get("auto_revive_desktop_owned", False):
        # Plan decision 7. This is the 2026-09-04 twin's clause: subfleet's lease
        # cannot exclude the desktop app, so the default recovery is a handoff.
        return False, ("the desktop app owns this session; automatic headless "
                       "revival is off (sessions.auto_revive_desktop_owned)"), OPT_IN_FIX
    return True, f"interrupted: {candidate.state.detail}", None


def model_for(candidate: Candidate, policy: dict[str, Any],
              override: str | None = None) -> tuple[str | None, str]:
    """C-23.39: revive keeps the session's own tier unless `--model` is given.

    Returns `(pinned model, why)`. A substitution is always recorded: on
    2026-08-26 Max ruled a Fable-grade session on Opus worse than a parked one,
    and although Fable itself is retired (2026-09-27, "opus 5.5 is strictly
    better than fable"), a silent tier change is still not revive's to make.
    """
    if override:
        return override, f"operator substituted {override} for {candidate.model or 'unknown'}"
    if not candidate.model:
        return None, "the session records no model; routing picks its own"
    retired = policy.get("retired", {})
    if candidate.model in retired:
        # A session last served by a retired pin revives on the current id of
        # that tier, never on the retired model.
        return retired[candidate.model], (f"{candidate.model} is retired; its tier "
                                          f"is now {retired[candidate.model]}")
    successor = _retired_successor(candidate.model, policy)
    if successor:
        # An older policy may still list a model retired from dispatch (C-17.2);
        # a revive, which runs unattended, must not be the route back to it.
        return successor, f"{candidate.model} is retired; its tier is now {successor}"
    return candidate.model, "the session's own recorded model"


def _retired_successor(model: str, policy: dict[str, Any]) -> str | None:
    """The successor of a recorded model (short name or exact id) retired from dispatch."""
    if model in RETIRED_MODELS:
        return RETIRED_MODELS[model]
    for short, entry in (policy.get("models") or {}).items():
        if short in RETIRED_MODELS and isinstance(entry, dict) and entry.get("id") == model:
            return RETIRED_MODELS[short]
    return None


def submit_args(candidate: Candidate, *, model: str | None, request_id: str,
                prompt_path: str, workdir: str | None = None,
                task: str | None = None, tier: str | None = None) -> SubmitArgs:
    """The revive job (C-23.54). Writable, in place, and no write preamble.

    * `workspace-write` because the session was doing work and a read-only
      resume would plan rather than act; it is also what makes C-6.5's
      writable-job rules — the main/master refusal, the committed-repository
      requirement, the worktree hold, and the refusal of a second instance of
      the session (which a revive always is) — apply to a revive.
    * `in_place` because a revive continues the session in its own worktree; a
      fresh worktree would resume the conversation somewhere it has never been.
    * `no_preamble` because the prompt IS the continuation instruction.
    * `caller_session` is the revived session, not the operator: see the module
      docstring.
    """
    return SubmitArgs(
        request_id=request_id,
        kind="revive",
        workdir=str(workdir or candidate.cwd),
        prompt_path=prompt_path,
        sandbox=Sandbox.WORKSPACE_WRITE.value,
        task=task,
        tier=tier,
        pinned_model=model,
        name=f"revive-{candidate.session_id[:8]}",
        in_place=True,
        no_preamble=True,
        caller_session=candidate.session_id,
        max_attempts=1,             # a retry would be a second continuation
    )


def revive(sessions, policy: dict[str, Any], session_id: str, *,
           stage_prompt, opt_in: bool = False, force: bool = False,
           model: str | None = None, workdir: str | None = None,
           dry_run: bool = False, transcript: str | Path | None = None,
           task: str | None = None, tier: str | None = None,
           request_id: str | None = None, minted: bool | None = None,
           now: datetime | None = None) -> Attempted:
    """Admit one session and submit its revive job, or say why not.

    `stage_prompt(text) -> path` writes the prompt where the daemon can read it;
    the CLI supplies `cli.stage_prompt` so a revive stages exactly as `run` does.
    `minted` says the caller made `request_id` up for this call rather than
    taking it from the operator (C-16.3); the CLI mints one before calling,
    because the staged prompt's name needs it, so it must say so. Left None,
    an id is minted here exactly when none was given.
    """
    facts = sessions.state([session_id])
    lane_ids = set(facts.get("lane_sessions") or [])
    candidate = inspect(session_id, lane_ids=lane_ids,
                        facts=(facts.get("sessions") or {}).get(session_id) or {},
                        transcript=transcript, now=now)
    admitted, reason, fix = admits(candidate, policy=policy, opt_in=opt_in, force=force)
    if not admitted:
        return Attempted(session_id=session_id, reason=reason, fix=fix,
                         candidate=candidate)
    pinned, why = model_for(candidate, policy, model)
    if pinned is None and not task and not tier:
        # `submit` requires at least one routing input; a session with no
        # recorded model gets the configured default tier rather than silence.
        task, tier = task or "build", tier or "standard"
        why = f"{why}; routed as {task}/{tier}"
    if dry_run:
        return Attempted(session_id=session_id, admitted=False, model=pinned,
                         reason=f"would revive on {pinned or f'{task}/{tier}'}: {why}",
                         candidate=candidate)
    prompt_path = stage_prompt(REVIVE_MESSAGE)
    args = submit_args(candidate, model=pinned,
                       request_id=request_id or str(uuid.uuid4()),
                       prompt_path=str(prompt_path), workdir=workdir,
                       task=task, tier=tier)
    result = sessions.submit(args, minted=request_id is None if minted is None else minted)
    job_id = result.get("job_id")
    # Record only accepted submissions; refused requests did not change models.
    sessions.record_revive(
        session_id, dedupe_key=candidate.state.dedupe_key,
        detail={"job_id": job_id, "model": pinned,
                "recorded_model": candidate.model,
                "substituted": bool(model), "task": task, "tier": tier,
                "why": why})
    return Attempted(session_id=session_id, admitted=True,
                     job_id=job_id, model=pinned,
                     reason=why, candidate=candidate)


def cold_candidates(sessions, policy: dict[str, Any], *,
                    only: Sequence[str] = (),
                    now: datetime | None = None) -> list[Candidate]:
    """Every cold interrupted session a `--scope cold` pass would consider.

    Live sessions are excluded here rather than in `admits` because a cold sweep
    is a directory scan and a live session simply is not in it; `admits` still
    refuses a named live session, which is the path a person takes.
    """
    instant = now or datetime.now(timezone.utc)
    facts = sessions.state(sorted(only) if only else None)
    lane_ids = set(facts.get("lane_sessions") or [])
    by_id = facts.get("sessions") or {}
    if only:
        return [inspect(session_id, lane_ids=lane_ids,
                        facts=by_id.get(session_id) or {}, now=instant)
                for session_id in only]
    live_ids = {item.session_id for item in
                registry.sessions(lane_ids=lane_ids, include_lanes=True, live_only=True)}
    window = float(policy.get("sessions", {}).get("muster_max_age_h", 2)) * 3600
    rows = transcripts.cold_sessions(live_ids=live_ids, lane_ids=lane_ids,
                                     max_age_s=window, now=instant)
    return [inspect(row.session_id, lane_ids=lane_ids,
                    facts=by_id.get(row.session_id) or {},
                    transcript=row.transcript, now=instant) for row in rows]


def render(attempts: Sequence[Attempted]) -> str:
    lines = []
    for item in attempts:
        mark = "revive" if item.admitted else "held"
        lines.append(f"  {mark:<7} {item.session_id[:8]} "
                     f"{item.job_id or '-':<24} {item.reason}")
        if item.fix:
            lines.append(f"          fix: {item.fix}")
    admitted = sum(1 for item in attempts if item.admitted)
    header = f"{admitted} revived, {len(attempts) - admitted} held"
    return header + "\n" + ("\n".join(lines) if lines else "  no cold sessions in scope")


__all__ = ["Attempted", "Candidate", "OPT_IN_FIX", "PROBE_PROMPT", "REQUIRED_MODE",
           "REVIVE_MESSAGE", "ReviveRefused", "admits", "cold_candidates", "inspect",
           "model_for", "render", "revive", "session_store_dir",
           "store_metadata", "submit_args"]
