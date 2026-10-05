"""Claude Code hook entry points: notice delivery layers 2 and 3 (C-15.2).

`subfleet hook <event>` is the one binary all three entries call, with the
harness's hook JSON on stdin. The events, and why each behaves the way it does,
follow `docs/reference/claude-hooks.md` (fetched 2026-09-05) — not memory:

* **SessionStart** and **UserPromptSubmit** — layer 3. Print every
  unacknowledged notice for this session in v1's `render_pending` shape and
  mark them `surfaced` (C-15.3). Both events add plain-text stdout to Claude's
  context on exit 0, and exit 2 on either of them is destructive (SessionStart
  blocks session startup; UserPromptSubmit blocks the prompt AND ERASES IT), so
  these two events exit 0 always — even on an error.

  **SessionStart** additionally hands the wake to the sessions kit (C-23.34):
  it records the session id, the harness's `source`, and the transcript path,
  spawns a detached worker, and decides nothing itself. It cannot decide: the
  app writes this restart's resume stub about 0.7 s AFTER the hook runs, so a
  dedupe verdict computed here is keyed to the PREVIOUS restart, which is
  exactly what blocked a fresh restart's nudge on 2026-08-24. The worker
  re-reads the transcript after the delay and applies the age cap, the dedupe,
  the cooldown and the liveness re-check against what it can actually see.

  Inside a process Subfleet launched (C-26.13) — a conversation turn, a lane
  run, or a probe — both events behave differently. Every such process carries
  the C-5.1 markers (`launched_by_subfleet`), and Claude Code runs these hook
  commands with the provider's own environment. A conversation turn is a
  `claude -p` Subfleet starts once per message, with source `startup` under
  `--session-id` and `resume` under `--resume`. So there:

  - `SessionStart` hands no wake to the sessions kit, which would otherwise
    treat every turn as a restart that cut the conversation's session off;
  - both events surface, and mark, only notices that name a job (C-15.1).
    A job an agent in turn 1 dispatched with `subfleet run` carries the
    conversation's session as `caller_session` (`cli.session_id` reads
    `CLAUDE_CODE_SESSION_ID`), and when it ends after the turn does, this is
    the layer that delivers it: layer 2 asks only for jobs still running
    (`_candidates`), and a turn's process is stopped once it outlives its
    result by `conversations.runner.AFTER_RESULT_S` (C-26.5).
    A notice that names no job — a `ping`, a sessions-kit nudge, a timer
    alert (the daemon's `service_notices`, returned with `job_id` None), or an
    imported v1 continuation — is left pending and unprinted: it is addressed
    to a person's session, and a turn's prompt is the message the person sent
    from the app. A pin's notice (C-11.8: a job this session dispatched whose
    pinned lane can never admit it) is a service notice that names its job, so
    it is surfaced here too: it is about work the session is waiting on.

* **PostToolUse** on Bash — layer 2. Ask the daemon which of this session's
  jobs are still running, take a file lease so two hooks never wait on one job,
  long-poll it, and exit 2 with the notice on stderr when it finishes inside
  the hook's timeout; otherwise exit 0 silently and let the session's next tool
  call re-arm. Exit 2 is right here and nowhere else: PostToolUse cannot block
  (the tool already ran) and exit 2 is the documented way to show stderr to
  Claude. The entry is installed with `asyncRewake: true`, and an asyncRewake
  hook "can only wake Claude or provide a system message on failure — [it]
  cannot add context or influence decisions on successful completion", so a
  silent exit 0 reaches nobody, which is exactly the intent on timeout.

The correlation between a Bash call and a job is the session's own unfinished
job set (plan B rev 4, "Notices and waiting", layer 2: the `if` filter is a
cost saver rather than the correlation). When the Bash command actually ran
`subfleet run`, the request id and job id it printed narrow that set to the one
job the submission created, which is the precise case the lane brief names.

Nothing here writes to `~/.claude/settings.json` unless
`subfleet daemon install --hooks` is asked to, and never without printing the
diff first.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import render
from .client import Client, DaemonError, DaemonUnavailable, busy_pause, state_root
from .contracts import Exit, JobState, WAIT_POLL_MAX_S
from .protocol import ProtocolError, service_notice_on_wire

#: The three events, in the harness's spelling and in v1's `bin/subfleet-hook`
#: argument spelling, which stays accepted so a half-migrated settings.json
#: still resolves (`~/.claude/settings.json` points at v1 today).
EVENTS = {
    "sessionstart": "SessionStart",
    "session-start": "SessionStart",
    "userpromptsubmit": "UserPromptSubmit",
    "user-prompt": "UserPromptSubmit",
    "posttooluse": "PostToolUse",
    "post-tool-use": "PostToolUse",
    "post-bash": "PostToolUse",
    "pretooluse": "PreToolUse",
    "pre-tool-use": "PreToolUse",
    "pre-bash": "PreToolUse",
}
SESSION_EVENTS = ("SessionStart", "UserPromptSubmit")

#: The C-5.1 markers that name a process Subfleet launched. Every launch path
#: sets `SUBFLEET_ATTEMPT`: an attempt, turns included (`Daemon._launch`, with
#: `SUBFLEET_JOB` and `SUBFLEET_ROOT`), an admission probe
#: (`Daemon._execute_probe`, the same three), and an enrollment turn
#: (`Daemon._enrollment_turn`, `SUBFLEET_ATTEMPT` and `SUBFLEET_ROOT`). The
#: guardian starts the provider with the environment it was given
#: (`subprocess.Popen` without `env=` in `guardian.run_guardian`), and Claude
#: Code 2.1.280 runs its `SessionStart` and `UserPromptSubmit` hook commands
#: with all three set, in `-p` and in `--input-format stream-json` mode, for a
#: new session and a `--resume`d one (`docs/desktop/reviews/
#: 2026-09-24-live-probes.md`, "Hooks inside a Subfleet launch").
#: `SUBFLEET_ROOT` is not used: every path that sets it also sets
#: `SUBFLEET_ATTEMPT`.
LAUNCH_MARKERS = ("SUBFLEET_ATTEMPT", "SUBFLEET_JOB")

#: Documented default for a command hook is 600 s; plan B rev 4 requires it be
#: set explicitly rather than inherited. `SUBFLEET_HOOK_TIMEOUT_S` moves both
#: the written entry and this process's own budget together.
HOOK_TIMEOUT_S = 600
#: Margin between our budget and the entry's timeout, so the hook always exits
#: on its own terms rather than being cancelled mid-write.
HOOK_MARGIN_S = 5.0
SETTINGS_ENV = "SUBFLEET_CLAUDE_SETTINGS"
MARKER = "subfleet hook"                  # how `daemon install --hooks` finds its own
V1_MARKER = "subfleet-hook"               # v1's entries, which v2 never rewrites
TERMINAL_STATES = {state.value for state in JobState if state.terminal}

#: `run` prints the job id on stdout (C-17.4) and `request=<id>` on stderr;
#: `--json` prints both as fields. A job id is C-1.1's `YYYYMMDD-HHMMSS-<slug>`.
JOB_ID_RE = re.compile(r"\b\d{8}-\d{6}-[a-z0-9][a-z0-9-]{0,39}\b")
REQUEST_RE = re.compile(r"request(?:_id)?[=\"':\s]+([A-Za-z0-9][\w.:-]{0,127})")
SUBFLEET_RUN_RE = re.compile(
    r"(?:^|[;&|(]\s*|\bnohup\s+|\bexec\s+|\btime\s+)"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=[^\s]*\s+)*"
    r"(?:[^\s]*/)?subfleet\s+run\b")


def timeout_s() -> int:
    """The hook entry's `timeout`, and the budget this process spends."""
    raw = (os.environ.get("SUBFLEET_HOOK_TIMEOUT_S") or "").strip()
    try:
        value = int(float(raw))
    except ValueError:
        return HOOK_TIMEOUT_S
    return value if value > 0 else HOOK_TIMEOUT_S


def launched_by_subfleet(env: Any = None) -> str | None:
    """The C-5.1 marker naming this process as one Subfleet launched, or None.

    A hook runs in the environment of the Claude Code process that invoked it,
    so a marker here means that process is a conversation turn, a lane run or a
    probe the daemon started (C-26.13).
    """
    values = os.environ if env is None else env
    for name in LAUNCH_MARKERS:
        if str(values.get(name) or "").strip():
            return name
    return None


def settings_path() -> Path:
    override = os.environ.get(SETTINGS_ENV)
    return (Path(override).expanduser() if override
            else Path("~/.claude/settings.json").expanduser())


def hook_command() -> str:
    """The command the entries run. `sys.executable -m subfleet` keeps a
    virtualenv's interpreter, which a bare `subfleet` on PATH would not."""
    override = os.environ.get("SUBFLEET_HOOK_COMMAND")
    if override:
        return override
    return f"{sys.executable} -m subfleet hook"


# --- reading the harness's payload -------------------------------------------

def read_payload(stream: Any = None) -> dict[str, Any]:
    """The hook JSON on stdin; `{}` for anything unparseable (never raises)."""
    stream = sys.stdin if stream is None else stream
    try:
        text = stream.read()
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def payload_session(payload: dict[str, Any]) -> str | None:
    value = payload.get("session_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    env = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    return env or None


def tool_command(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        command = tool_input.get("command")
        if isinstance(command, str):
            return command
    return ""


def tool_output(payload: dict[str, Any]) -> str:
    """The Bash tool's output.

    The hooks page's field table names it `tool_response` and its own
    PostToolUse example names it `tool_result`; both literals are in the
    installed 2.1.260 binary, so both keys are read
    (`docs/reference/claude-hooks.md` sections 2 and 5).
    """
    chunks: list[str] = []
    for key in ("tool_response", "tool_result"):
        value = payload.get(key)
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, dict):
            for field in ("stdout", "stderr", "text", "output", "content"):
                item = value.get(field)
                if isinstance(item, str):
                    chunks.append(item)
            if not chunks:
                chunks.append(json.dumps(value, default=str))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    chunks.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    chunks.append(item["text"])
    return "\n".join(chunks)


def ran_subfleet_run(command: str) -> bool:
    """True when `subfleet run` was in command position, as v1's guard decides.

    A heredoc or a `bash -n bin/x` that merely mentions the words is not a
    launch (`bin/subfleet-hook:41-45`).
    """
    return bool(SUBFLEET_RUN_RE.search(command or ""))


def ids_in_output(text: str) -> tuple[set[str], set[str]]:
    """(job ids, request ids) mentioned in a Bash call's output."""
    job_ids = set(JOB_ID_RE.findall(text or ""))
    request_ids = {m.group(1) for m in REQUEST_RE.finditer(text or "")}
    return job_ids, request_ids - job_ids


# --- the waiter lease ---------------------------------------------------------

class Lease:
    """One waiter per job, enforced by `flock` on a file in the state root.

    `flock` is the right primitive here because the kernel drops it when the
    holder dies: a hook killed with its session leaves no lease to reap, and no
    identity check (C-5.3) is needed to tell a live holder from a dead one. The
    file's contents are for a human reading `$SUBFLEET_HOME/hooks/waiters/`,
    never for the decision.
    """

    def __init__(self, root: Path, job_id: str):
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", job_id).strip("-") or "unknown"
        self.path = Path(root) / "hooks" / "waiters" / f"{safe}.lock"
        self.job_id = job_id
        self._fd: int | None = None

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        try:
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({"job_id": self.job_id, "pid": os.getpid(),
                                     "since": time.time()}).encode() + b"\n")
        except OSError:
            pass
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# --- rendering ----------------------------------------------------------------

def is_message(row: dict[str, Any]) -> bool:
    """C-15.3: a row that does not report a run's end, so not a "detached run".

    A service notice (negated wire id), even one that names a job (the release
    line's pin notice, C-11.8), and any notice that names no job (a v1 outbox
    message the importer carried into `notices` with `job_id` NULL).
    """
    notice_id = row.get("notice_id")
    negated = isinstance(notice_id, int) and not isinstance(notice_id, bool) and notice_id < 0
    return negated or row.get("job_id") is None


def render_pending(rows: Sequence[dict[str, Any]]) -> str:
    """v1's `notify.render_pending` shape, byte for byte where it can be.

    v1 keys the header off "detached run(s) ... finished while it was not
    running" and closes with the two follow-up commands; the surfaced text is
    the notice row's own `text`, which C-15.1 already fills with the job id,
    the job's state and rc, the deliverable and `-o` path of an accepted job,
    a summary naming the final attempt's class and rc, and any uncertainty.

    A row that is not a run's end (`is_message`: a service notice, such as a
    restart nudge, or a notice that names no job) gets its own header after the
    runs, with the time it was queued: on 2026-09-29 a two-day-old restart
    nudge was surfaced as "1 detached run dispatched by this session finished
    while it was not running".
    """
    messages = [row for row in rows if is_message(row)]
    runs = [row for row in rows if not is_message(row)]
    blocks: list[str] = []
    if runs:
        blocks.append(f"subfleet: {len(runs)} detached run{'s' if len(runs) != 1 else ''} "
                      "dispatched by this session finished while it was not running:")
        for row in runs:
            text = str(row.get("text") or "").strip()
            blocks.append(text or f"run {row.get('job_id')} finished")
        blocks.append("List: subfleet runs --mine · details: subfleet runs show <id>")
    if messages:
        blocks.append(f"subfleet: {len(messages)} message{'s' if len(messages) != 1 else ''} "
                      "for this session:")
        for row in messages:
            text = str(row.get("text") or "").strip() or "(no text)"
            queued = row.get("created_at")
            blocks.append(f"queued {queued}:\n{text}" if queued else text)
    return "\n\n".join(blocks)


def job_summary(job: dict[str, Any], root: Path) -> str:
    """A factual line built from the job row when C-15.1's notice row is absent.

    Nothing is inferred: the header is `render.notice_header` over the row the
    daemon returned, the function the daemon's own notice uses, so the fallback
    and the notice cannot name one terminal state two ways (incident:
    2026-09-24, a cancelled job's notice said `ok; rc=0` and this line said
    `cancelled; rc=130`). `root` is resolved as the daemon resolves its own, so
    an accepted job's deliverable path is the one the notice would have named,
    and it is named only when the file is there (a job imported from v1 keeps
    its deliverable in the v1 run directory).
    """
    return (render.notice_header(job, Path(root).resolve(), require_file=True) + "\n"
            f"no notice row for this job — subfleet runs show {job.get('job_id')}")


# --- talking to the daemon ----------------------------------------------------

def _pending(client: Client, session: str) -> list[dict[str, Any]]:
    result = client.call("notice.pending", {"session_id": session})
    rows = result.get("notices")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _mark(client: Client, session: str, notice_ids: Iterable[int], state: str,
          transport: str | None = None) -> None:
    ids = [int(item) for item in notice_ids]
    if not ids:
        return
    client.call("notice.mark", {"session_id": session, "notice_ids": ids,
                                "state": state, "transport": transport})


def _offline_pending(root: Path, session: str) -> list[dict[str, Any]]:
    """Read-only fallback when no daemon is listening.

    C-17.5 does not extend offline mode to the hooks, and a hook cannot write to
    the store (the daemon is the only writer), so an offline surface prints the
    same rows and marks nothing — the notice is surfaced again once the daemon
    is back, which is the safe direction to be wrong in.
    """
    try:
        from .offline import Offline
        conn = Offline(root).connect()
    except Exception:                                   # noqa: BLE001 - never block
        return []
    rows: list[dict[str, Any]] = []
    try:
        # The rows and ids `notice.pending` would return: job notices, then
        # service notices negated (C-15.3).
        for table, on_wire in (("notices", dict), ("service_notices", service_notice_on_wire)):
            try:
                rows += [on_wire(dict(row)) for row in conn.execute(
                    f"SELECT * FROM {table} WHERE session_id=? AND "
                    "state IN ('pending','offered') ORDER BY notice_id", (session,))]
            except Exception:                           # noqa: BLE001 - never block
                pass            # e.g. a store from before schema v2 has no service_notices
    finally:
        conn.close()
    return rows


# --- the events ---------------------------------------------------------------

def names_a_job(row: dict[str, Any]) -> bool:
    """True for a C-15.1 completion notice: a row that names the job it reports.

    The daemon's `notice.pending` returns `notices` rows beside
    `service_notices` rows, the latter with a negated `notice_id` and `job_id`
    None (`ping`, a sessions-kit nudge, a timer alert), except a pin's notice
    (C-11.8), which names the job it is about; an imported v1 outbox
    continuation is a `notices` row with no job (`importer.import_outbox`).
    """
    job_id = row.get("job_id")
    return isinstance(job_id, str) and bool(job_id.strip())


def session_event(event: str, payload: dict[str, Any], root: Path,
                  *, client: Client | None = None,
                  stdout: Any = None, env: Any = None) -> int:
    """SessionStart / UserPromptSubmit: surface and mark (C-15.2 layer 3, C-15.3).

    Always exits 0. Exit 2 on SessionStart blocks the session from starting and
    on UserPromptSubmit erases the user's prompt, so nothing this hook can go
    wrong with is worth either outcome (`docs/reference/claude-hooks.md` §3).

    Inside a process Subfleet launched (C-26.13) there is no wake, and only
    notices that name a job are surfaced and marked; the rest stay pending.
    That session is a conversation's or a lane's: the kit may not nudge it, and
    a `ping` or a nudge is not for a turn's prompt, but the completion of a job
    the session itself dispatched is exactly what its next turn needs.
    """
    launched = launched_by_subfleet(env)
    stdout = sys.stdout if stdout is None else stdout
    session = payload_session(payload)
    if not session:
        return int(Exit.OK)
    if event == "SessionStart" and not launched:
        try:
            wake_worker(session, payload, root)
        except Exception:                               # noqa: BLE001 - see below
            pass    # Exit 2 here blocks the session from starting; a missed
                    # nudge is recoverable and a blocked session is not.
    marked = True
    try:
        # C-15.6: no lock check. C-16.7: a busy daemon is read offline below at
        # once; waiting out busy answers would only delay the prompt or the start.
        client = Client(root, verify_lock=False, retry_busy=False) if client is None else client
        rows = _pending(client, session)
    except (DaemonUnavailable, DaemonError, ProtocolError, OSError):
        rows, marked = _offline_pending(root, session), False
    if launched:
        rows = [row for row in rows if names_a_job(row)]
    if not rows:
        return int(Exit.OK)
    context = render_pending(rows)
    if not context:
        return int(Exit.OK)
    stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": event, "additionalContext": context}}) + "\n")
    if marked:
        try:
            _mark(client, session, [row["notice_id"] for row in rows
                                    if row.get("notice_id") is not None],
                  "surfaced", f"hook:{event}")
        except (DaemonUnavailable, DaemonError, ProtocolError, OSError):
            pass
    return int(Exit.OK)


def wake_worker(session: str, payload: dict[str, Any], root: Path,
                *, spawn=None) -> int | None:
    """Hand a `SessionStart` to the sessions kit and return at once (C-23.34).

    Records the wake — the session, its source, its transcript — and decides
    nothing. Every guard, including C-23.33's "only `startup` and `resume`" and
    the `SUBFLEET_TICKLE=off` switch, is applied by the worker against the
    transcript as it reads after the delay, so a wake this hook cannot judge is
    still a wake the worker can.

    Never raises and never blocks: exit 2 here would block the session from
    starting, and a slow spawn would delay every restart.
    """
    try:
        from .sessions import nudge
        from .policy import DEFAULT_POLICY_PATH, PolicyError, load_policy
        try:
            policy = load_policy(root / "policy.json")
        except (PolicyError, OSError):
            try:
                policy = load_policy(DEFAULT_POLICY_PATH)
            except (PolicyError, OSError):
                policy = {}
        delay_s = nudge.caps(policy)["delay_s"]
        source = payload.get("source") if isinstance(payload, dict) else None
        transcript = payload.get("transcript_path") if isinstance(payload, dict) else None
        return (spawn or nudge.spawn)(
            session, source=source if isinstance(source, str) else None,
            transcript=transcript if isinstance(transcript, str) else None,
            delay_s=delay_s, root=root)
    except Exception:                                   # noqa: BLE001 - see above
        return None


def _candidates(client: Client, session: str, payload: dict[str, Any]
                ) -> list[dict[str, Any]]:
    """This session's unfinished jobs, narrowed by what the Bash call printed."""
    result = client.call("list", {"mine": session, "running": True})
    rows = result.get("jobs")
    jobs = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    if not jobs or not ran_subfleet_run(tool_command(payload)):
        return jobs
    job_ids, request_ids = ids_in_output(tool_output(payload))
    named = [job for job in jobs
             if job.get("job_id") in job_ids or job.get("request_id") in request_ids]
    # An unmatched submission (quiet `--json` consumed by a pipe, a job id the
    # tool output truncated) still leaves the session-wide arm, which is the
    # composition plan B rev 4 specifies for layer 2.
    return named or jobs


def post_tool_use(payload: dict[str, Any], root: Path, *,
                  client: Client | None = None,
                  budget_s: float | None = None,
                  stderr: Any = None,
                  now=time.monotonic, sleep=time.sleep) -> int:
    """PostToolUse[Bash]: arm one waiter and deliver its notice (C-15.2 layer 2).

    Returns 2 with the notice on stderr when a job finishes inside the budget,
    and 0 silently otherwise — including on every error, because a hook that
    fails loudly on every Bash call is worse than a missed notice that layer 3
    surfaces at the next prompt.
    """
    stderr = sys.stderr if stderr is None else stderr
    if payload.get("tool_name") not in (None, "Bash"):
        return int(Exit.OK)
    session = payload_session(payload)
    if not session:
        return int(Exit.OK)
    deadline = now() + (timeout_s() - HOOK_MARGIN_S if budget_s is None else budget_s)
    try:
        client = Client(root, verify_lock=False) if client is None else client     # C-15.6
        jobs = _candidates(client, session, payload)
    except (DaemonUnavailable, DaemonError, ProtocolError, OSError):
        return int(Exit.OK)
    for job in jobs:
        job_id = job.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            continue
        lease = Lease(root, job_id)
        if not lease.acquire():
            continue                                    # another hook has this job
        with lease:
            return _wait_and_deliver(client, session, job_id, deadline,
                                     stderr=stderr, now=now, sleep=sleep)
    return int(Exit.OK)


#: Shortest pause between two `wait` calls that both came back at once. A
#: server-side long poll normally spends the whole deadline, so this only fires
#: when it does not — and then it is the difference between one call a second
#: and a hook spinning on the socket for its whole 600 s budget.
_RETRY_FLOOR_S = 0.25


def _wait_and_deliver(client: Client, session: str, job_id: str, deadline: float,
                      *, stderr: Any, now, sleep) -> int:
    busy = 0
    while True:
        remaining = deadline - now()
        if remaining <= 0:
            return int(Exit.OK)                         # silent timeout
        poll = min(remaining, float(WAIT_POLL_MAX_S))
        started = now()
        try:
            result = client.call("wait", {"job_ids": [job_id], "deadline_s": poll},
                                 timeout=poll + 10, retry_busy=False)
        except DaemonError as exc:
            if not exc.busy:
                return int(Exit.OK)
            # C-16.7: busy is an empty poll; ask again within the budget, and the
            # next poll has its whole deadline (as #55 on main).
            busy += 1
            sleep(min(busy_pause(busy), max(0.0, deadline - now())))
            continue
        except (DaemonUnavailable, ProtocolError, OSError):
            return int(Exit.OK)
        busy = 0
        # A `wait` that returns early — a shorter server-side cap, a job the
        # daemon no longer has — must not turn this loop into a busy wait on
        # the socket. C-15.4 makes the deadline a server-side maximum, not a
        # promise about how long the call takes.
        idle = poll - (now() - started)
        if idle > 0:
            sleep(min(max(idle, _RETRY_FLOOR_S), max(0.0, deadline - now())))
        if result.get("timeout"):
            continue
        jobs = result.get("jobs")
        rows = ([row for row in jobs if isinstance(row, dict)] if isinstance(jobs, list)
                else [row for row in jobs.values() if isinstance(row, dict)]
                if isinstance(jobs, dict) else [])
        job = next((row for row in rows if row.get("job_id") == job_id),
                   rows[0] if rows else None)
        if job is None or job.get("state") not in TERMINAL_STATES:
            continue                        # not a finish; the pause above ran
        return _deliver(client, session, job, stderr=stderr)


def _deliver(client: Client, session: str, job: dict[str, Any], *, stderr: Any) -> int:
    """Show the notice to Claude: stderr plus exit 2 (`claude-hooks.md` §3)."""
    job_id = job.get("job_id")
    try:
        rows = [row for row in _pending(client, session)
                if row.get("job_id") == job_id]
    except (DaemonUnavailable, DaemonError, ProtocolError, OSError):
        rows = []
    text = ("\n\n".join(str(row.get("text") or "").strip() for row in rows)
            if rows else job_summary(job, client.root))
    stderr.write(text.rstrip() + "\n")
    if rows:
        # `offered`, not `surfaced`: this transport gets no acknowledgement from
        # the harness, so an unacknowledged notice is offered again by layer 3
        # (plan B rev 4, "Notices and waiting"; C-15.3).
        try:
            _mark(client, session, [row["notice_id"] for row in rows
                                    if row.get("notice_id") is not None],
                  "offered", "hook:PostToolUse")
        except (DaemonUnavailable, DaemonError, ProtocolError, OSError):
            pass
    return int(Exit.INVALID_INPUT)          # 2: the harness's "show this to Claude"


def attached_runner(command: str) -> str | None:
    """Recognise direct runner launches, not quoted mentions or file arguments.

    This is a convenience guard for ordinary shell commands, not a shell
    sandbox. Indirect launches through scripts remain the caller's responsibility.
    """
    # Ignore heredoc bodies: prompt examples are data, not commands. Match only
    # literal delimiters; the shell's broader grammar is intentionally not run.
    lines, delimiters = [], []
    for line in command.splitlines(keepends=True):
        if delimiters:
            if line.strip() == delimiters[0]:
                delimiters.pop(0)
            continue
        lines.append(line)
        delimiters.extend(match[1] for match in re.findall(
            r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", line))
    lexer = shlex.shlex(''.join(lines), posix=True, punctuation_chars=';&|()\n')
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments, words = [], []
    for token in tokens:
        if token and all(ch in ';&|()\n' for ch in token):
            if words:
                segments.append(words)
            words = []
        else:
            words.append(token)
    if words:
        segments.append(words)
    for words in segments:
        override = False
        while words:
            first = words[0]
            if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', first, re.S):
                override = override or first == 'SUBFLEET_ATTACHED_OK=1'
                words = words[1:]
            elif Path(first).name in ('env', 'nohup', 'exec', 'time', 'command'):
                words = words[1:]
                while words and words[0] == '--':
                    words = words[1:]
            else:
                break
        if not words or override:
            continue
        binary = Path(words[0]).name
        offender = None
        if binary == 'subfleet' and len(words) > 1 and words[1] in ('codex', 'claude'):
            offender = 'subfleet ' + words[1]
        elif binary in ('codex-run', 'claude-lane', 'subfleet-codex', 'subfleet-claude'):
            offender = binary
        elif binary == 'codex' and len(words) > 1 and words[1] in ('exec', 'e', 'review'):
            offender = 'codex ' + words[1]
        if offender and (binary == 'codex' or '-d' not in words[1:]):
            return offender
    return None


def pre_tool_use(payload: dict[str, Any], *, stdout=None) -> int:
    if payload.get('tool_name', 'Bash') != 'Bash':
        return 0
    offender = attached_runner(tool_command(payload))
    if offender:
        reason = (f"{offender} runs attached to this session and can die when it restarts. "
                  "Use subfleet run --task build|review|research --tier trivial|easy|standard|hard "
                  "-C <dir> -p prompt.md -o out.md, then subfleet wait <job-id>. "
                  "For an intentional short attached call, prefix that command with SUBFLEET_ATTACHED_OK=1.")
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                                'permissionDecision': 'deny',
                                                'permissionDecisionReason': reason}}), file=stdout or sys.stdout)
    return 0


def run(event: str, argv: Sequence[str] = (), *, stream: Any = None,
        root: Path | None = None, client: Client | None = None) -> int:
    """`subfleet hook <event>` — dispatch one hook invocation."""
    name = EVENTS.get((event or "").strip().lower())
    if name is None:
        sys.stderr.write(f"subfleet hook: unknown event {event!r} "
                         f"({'|'.join(sorted(set(EVENTS.values())))})\n")
        return int(Exit.INVALID_INPUT)
    payload = read_payload(stream)
    if name == "PostToolUse" and not payload.get("hook_event_name"):
        payload = {**payload, "hook_event_name": name}
    root = state_root() if root is None else root
    if name == "PreToolUse":
        return pre_tool_use(payload)
    if name in SESSION_EVENTS:
        return session_event(name, payload, root, client=client)
    return post_tool_use(payload, root, client=client)


# --- settings entries ---------------------------------------------------------

def desired_groups(command: str | None = None,
                   timeout: int | None = None) -> dict[str, dict[str, Any]]:
    """The four entries `daemon install --hooks` writes.

    `timeout` is written explicitly on every entry rather than inherited: the
    documented `command` default is 600 s and this hook's own budget is derived
    from the same number (plan B rev 4, layer 2).
    """
    command = command or hook_command()
    seconds = timeout_s() if timeout is None else timeout
    return {
        "PreToolUse": {"matcher": "Bash", "hooks": [
            {"type": "command", "command": f"{command} PreToolUse", "timeout": min(seconds, 5)}]},
        "SessionStart": {"hooks": [
            {"type": "command", "command": f"{command} SessionStart",
             "timeout": min(seconds, 30)}]},
        "UserPromptSubmit": {"hooks": [
            {"type": "command", "command": f"{command} UserPromptSubmit",
             # The harness lowers the command default to 30 s on this event.
             "timeout": min(seconds, 30)}]},
        "PostToolUse": {"matcher": "Bash", "hooks": [
            {"type": "command", "command": f"{command} PostToolUse",
             "asyncRewake": True, "timeout": seconds}]},
    }


def _is_ours(hook: Any, event: str, command: str) -> bool:
    """Is this entry one `daemon install --hooks` wrote?

    Three ways to say yes, narrowest first, because the cost of a wrong yes is
    deleting somebody else's hook:

    1. it is character-for-character what this invocation would write;
    2. it carries `MARKER`, the default command's own spelling, so an entry
       written before `SUBFLEET_HOOK_COMMAND` was pointed somewhere else is
       still recognised;
    3. its last two tokens are `hook <Event>`, which is the shape every entry
       we write has and nothing else on this machine has: the `hook` verb takes
       the event as its only argument, so `... hook SessionStart` is a subfleet
       entry written under some other path (an old `SUBFLEET_HOOK_COMMAND`, a
       moved virtualenv). A false positive here removes one entry, and the diff
       that removal appears in is printed before anything is written.

    v1's entries end in `session-start` / `user-prompt` / `pre-bash`, never in a
    v2 event name, and carry `V1_MARKER` rather than `MARKER`, so none of the
    three matches them. That is deliberate: installation owns only v2 entries. The native
    PreToolUse guard now preserves the attached-runner protection independently
    of any old entries an operator has retained.
    """
    if not isinstance(hook, dict):
        return False
    text = str(hook.get("command") or "")
    if not text:
        return False
    if text == f"{command} {event}":
        return True
    if V1_MARKER in text:
        return False
    if MARKER in text:
        return True
    tokens = text.split()
    return len(tokens) >= 3 and tokens[-1] == event and tokens[-2] == "hook"


def _strip_ours(groups: list[Any], event: str, command: str) -> list[Any]:
    """Remove v2's own entries for `event` and nothing else."""
    kept: list[Any] = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept.append(group)
            continue
        remaining = [hook for hook in group["hooks"]
                     if not _is_ours(hook, event, command)]
        if remaining or not group["hooks"]:
            kept.append({**group, "hooks": remaining})
    return kept


def load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def plan(path: Path | None = None, *, command: str | None = None,
         timeout: int | None = None, remove: bool = False) -> dict[str, Any]:
    """What `--hooks` would do: the current file, the proposed file, the diff.

    Nothing is written here. `daemon install --hooks` prints this diff first and
    `--dry-run` prints it instead of writing (the lane's own rule, and the one
    that keeps `~/.claude/settings.json` out of accidental rewrites).
    """
    path = settings_path() if path is None else path
    try:
        current = load_settings(path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "path": str(path), "error": str(exc)}
    hooks = current.get("hooks")
    hooks = dict(hooks) if isinstance(hooks, dict) else {}
    proposed_hooks: dict[str, Any] = {key: value for key, value in hooks.items()}
    changed: list[str] = []
    resolved = command or hook_command()
    for event, group in desired_groups(command, timeout).items():
        existing = hooks.get(event) if isinstance(hooks.get(event), list) else []
        stripped = _strip_ours(list(existing), event, resolved)
        updated = stripped if remove else [*stripped, group]
        if updated != list(existing):
            changed.append(event)
        if updated or event in hooks:
            proposed_hooks[event] = updated
    proposed = {**current, "hooks": proposed_hooks}
    return {
        "ok": True, "path": str(path), "changed_events": changed,
        "current": current, "proposed": proposed,
        "diff": _diff(current, proposed, str(path)),
        "v1_entries": v1_entries(current),
    }


def v1_entries(settings: dict[str, Any]) -> dict[str, list[str]]:
    """Which v1 `bin/subfleet-hook` entries are still installed, by event.

    They are reported, never rewritten; uninstalling v2 does not claim ownership
    of a separately installed v1 hook.
    """
    found: dict[str, list[str]] = {}
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return found
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        commands = [str(hook.get("command"))
                    for group in groups if isinstance(group, dict)
                    for hook in (group.get("hooks") or [])
                    if isinstance(hook, dict)
                    and V1_MARKER in str(hook.get("command") or "")]
        if commands:
            found[event] = commands
    return found


def _diff(current: dict[str, Any], proposed: dict[str, Any], label: str) -> str:
    import difflib
    def render(data: dict[str, Any]) -> list[str]:
        return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").splitlines(True)
    return "".join(difflib.unified_diff(render(current), render(proposed),
                                        fromfile=f"{label} (current)",
                                        tofile=f"{label} (proposed)"))


def apply(path: Path | None = None, *, command: str | None = None,
          timeout: int | None = None, remove: bool = False) -> dict[str, Any]:
    """Write the proposed settings, keeping a timestamped backup as v1 does."""
    from datetime import datetime
    path = settings_path() if path is None else path
    report = plan(path, command=command, timeout=timeout, remove=remove)
    if not report.get("ok") or not report["changed_events"]:
        return {**report, "written": False}
    backup = None
    try:
        if path.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = path.with_name(f"{path.name}.bak-{stamp}")
            backup.write_bytes(path.read_bytes())
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(report["proposed"], indent=2, ensure_ascii=False)
                       + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        return {**report, "written": False, "ok": False, "error": str(exc)}
    return {**report, "written": True, "backup": str(backup) if backup else None}


def installed(path: Path | None = None, *, command: str | None = None,
              timeout: int | None = None) -> dict[str, Any]:
    """Whether the file already matches what `--hooks` would write (for doctor)."""
    report = plan(path, command=command, timeout=timeout)
    if not report.get("ok"):
        return {"ok": False, "path": report["path"], "error": report.get("error")}
    return {"ok": True, "path": report["path"],
            "matches": not report["changed_events"],
            "missing_events": report["changed_events"],
            "v1_entries": report["v1_entries"]}
