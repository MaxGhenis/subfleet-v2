"""Redacted fixtures for the sessions kit: a `~/.claude` and a desktop store.

Built in code rather than checked in as files, for the reason the lane brief
gives: the shapes come from a handful of real records and nothing else. No test
in this suite reads or writes the operator's own `~/.claude`,
`~/Library/Application Support/Claude`, or `~/chief-of-staff` — every path is
under `tmp_path` and reached through `SUBFLEET_CLAUDE_DIR` and
`SUBFLEET_SESSION_STORE`, which are the same overrides v1 uses.

Nothing here contains a real credential. `FAKE_SECRET` is a syntactically valid
token that matches the scrub list's `sk-ant-` pattern and is not, and has never
been, a key.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

#: Every fixture assertion is pinned here; `tests/conftest.py` uses the same
#: instant, so a transcript written for one suite reads the same age in both.
NOW = datetime(2026, 9, 5, 11, 30, 0, tzinfo=timezone.utc)

#: A token shaped exactly like the ones the scrub list catches. Not a key.
FAKE_SECRET = "sk-ant-FAKEFAKEFAKEFAKEFAKEFAKE1234"    # noqa: S105 - fixture

WORKDIR = "/Users/fixture/repo"


def iso(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def ago(seconds: float, *, now: datetime = NOW) -> str:
    return iso(now - timedelta(seconds=seconds))


def project_slug(cwd: str = WORKDIR) -> str:
    """The `~/.claude/projects` folder name for a cwd, as the app encodes it."""
    import re
    return re.sub(r"[^A-Za-z0-9-]", "-", cwd)


# --- `~/.claude` --------------------------------------------------------------

def claude_home(tmp_path: Path, monkeypatch) -> Path:
    """A `~/.claude` the whole kit reads through `SUBFLEET_CLAUDE_DIR`."""
    home = tmp_path / "claude"
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    (home / "projects").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SUBFLEET_CLAUDE_DIR", str(home))
    return home


def register(home: Path, session_id: str, pid: int, *,
             socket_path: str | None = None, started_at: float = 1000.0,
             name: str = "a session", cwd: str = WORKDIR) -> Path:
    """One `~/.claude/sessions/<pid>.json`, in the harness's own shape.

    The file is keyed by pid, which is why a restarted session leaves a second
    row behind and why C-23.30 has to say which of them speaks.
    """
    path = home / "sessions" / f"{pid}.json"
    path.write_text(json.dumps({
        "sessionId": session_id, "pid": pid, "name": name,
        "messagingSocketPath": socket_path, "startedAt": started_at,
        "cwd": cwd}), encoding="utf-8")
    return path


def transcript(home: Path, session_id: str, entries: Sequence[dict[str, Any]], *,
               cwd: str = WORKDIR) -> Path:
    """Write a transcript where `transcripts.transcript_path` will find it."""
    directory = home / "projects" / project_slug(cwd)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries),
                    encoding="utf-8")
    return path


# --- transcript records -------------------------------------------------------
#
# Only the fields the readers actually look at. A real record carries far more;
# every extra field is noise a fixture should not pretend to know.

def _entry(kind: str, content: Any, *, uuid: str, at: str, **extra: Any) -> dict[str, Any]:
    return {"type": kind, "uuid": uuid, "timestamp": at, "cwd": WORKDIR,
            "message": {"role": kind, "content": content}, **extra}


def user_text(text: str, *, uuid: str = "u1", at: str = ago(60),
              **extra: Any) -> dict[str, Any]:
    return _entry("user", [{"type": "text", "text": text}], uuid=uuid, at=at, **extra)


def assistant_text(text: str, *, uuid: str = "a1", at: str = ago(60),
                   model: str = "claude-fable-5-1", **extra: Any) -> dict[str, Any]:
    entry = _entry("assistant", [{"type": "text", "text": text}], uuid=uuid, at=at,
                   **extra)
    entry["message"]["model"] = model
    return entry


def assistant_tool_use(name: str = "Bash", *, uuid: str = "a1", at: str = ago(60),
                       tool_id: str = "t1", tool_input: Any = None,
                       model: str = "claude-fable-5-1") -> dict[str, Any]:
    """An assistant turn whose tool result never arrived: interrupted (C-23.33)."""
    entry = _entry("assistant", [{"type": "tool_use", "id": tool_id, "name": name,
                                  "input": tool_input or {"command": "git status"}}],
                   uuid=uuid, at=at)
    entry["message"]["model"] = model
    return entry


def user_tool_result(text: str = "ok", *, uuid: str = "u2", at: str = ago(60),
                     tool_id: str = "t1") -> dict[str, Any]:
    """A tool result the model never continued from: interrupted (C-23.33)."""
    return _entry("user", [{"type": "tool_result", "tool_use_id": tool_id,
                            "content": text}], uuid=uuid, at=at)


def resume_stub(*, user_uuid: str = "stub-u", assistant_uuid: str = "stub-a",
                at: str = ago(5)) -> list[dict[str, Any]]:
    """The desktop app's synthetic restart pair (C-23.34).

    A hidden user line and a fake assistant reply with the same timestamp,
    written into every restarted session about 0.7 s after the new process
    starts. Each pair carries a fresh uuid, which is what makes one nudge per
    restart possible.
    """
    from subfleet.sessions.transcripts import RESUME_STUB_ASSISTANT, RESUME_STUB_USER
    user = user_text(RESUME_STUB_USER, uuid=user_uuid, at=at)
    user["isMeta"] = True
    assistant = assistant_text(RESUME_STUB_ASSISTANT, uuid=assistant_uuid, at=at,
                               model="<synthetic>")
    return [user, assistant]


def limit_banner(*, uuid: str = "banner", at: str = ago(90)) -> dict[str, Any]:
    """A provider-limit banner: an assistant entry that is not a model turn."""
    entry = assistant_text("You've reached your Fable 5 limit · resets 3pm",
                           uuid=uuid, at=at, model="<synthetic>")
    entry["isApiErrorMessage"] = True
    return entry


def headless_prompt(text: str = "the lane brief", *, uuid: str = "h1",
                    at: str = ago(60)) -> dict[str, Any]:
    """A `claude -p` prompt: one text turn, `promptSource: sdk` (C-23.31)."""
    entry = user_text(text, uuid=uuid, at=at)
    entry["promptSource"] = "sdk"
    return entry


def typed_prompt(text: str = "do the thing", *, uuid: str = "t1",
                 at: str = ago(60)) -> dict[str, Any]:
    entry = user_text(text, uuid=uuid, at=at)
    entry["promptSource"] = "typed"
    return entry


# --- ready-made transcripts, one per turn state -------------------------------

def interrupted(age_s: float = 1800, *, stub: bool = False,
                uuid: str = "cut") -> list[dict[str, Any]]:
    """A tool call whose result never arrived, optionally behind a resume stub."""
    entries = [typed_prompt("start the work", uuid="p0", at=ago(age_s + 60)),
               assistant_text("working", uuid="a0", at=ago(age_s + 30)),
               assistant_tool_use(uuid=uuid, at=ago(age_s))]
    return entries + (resume_stub() if stub else [])


def completed(age_s: float = 1800) -> list[dict[str, Any]]:
    return [typed_prompt("start the work", uuid="p0", at=ago(age_s + 60)),
            assistant_text("done, nothing pending", uuid="a0", at=ago(age_s))]


def stopped(age_s: float = 1800) -> list[dict[str, Any]]:
    return [typed_prompt("start", uuid="p0", at=ago(age_s + 60)),
            assistant_text("working", uuid="a0", at=ago(age_s + 30)),
            user_text("[Request interrupted by user for tool use]",
                      uuid="esc", at=ago(age_s))]


def headless(age_s: float = 1800) -> list[dict[str, Any]]:
    """A lane run: one sdk-sourced prompt and an answer (C-23.31)."""
    return [headless_prompt(at=ago(age_s + 60)),
            assistant_tool_use(uuid="lane-cut", at=ago(age_s))]


def with_mode(entries: Sequence[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    """Stamp a permission mode on the last user turn (C-23.35's candidate filter)."""
    rows = [dict(entry) for entry in entries]
    for entry in reversed(rows):
        if entry.get("type") == "user":
            entry["permissionMode"] = mode
            break
    return rows


# --- the desktop session store ------------------------------------------------

def desktop_store(tmp_path: Path, monkeypatch) -> Path:
    """`~/Library/Application Support/Claude/claude-code-sessions`, relocated."""
    store = tmp_path / "claude-code-sessions"
    store.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SUBFLEET_SESSION_STORE", str(store))
    return store


def index_entry(store: Path, account: str, org: str, session_id: str, *,
                name: str | None = None, cwd: str = WORKDIR,
                mode: str = "bypassPermissions", model: str = "claude-fable-5-1",
                title: str = "a session", archived: bool = False,
                starred: bool = False, last_activity: int = 1000,
                title_source: str = "auto",
                settings: dict[str, Any] | None = None,
                **extra: Any) -> Path:
    """One `local_<id>.json`, in the desktop app's own shape."""
    folder = store / account / org
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name or f"local_{session_id}.json")
    body: dict[str, Any] = {
        "sessionId": path.stem, "cliSessionId": session_id, "cwd": cwd,
        "permissionMode": mode, "model": model, "title": title,
        "titleSource": title_source, "isArchived": archived,
        "isStarred": starred, "lastActivityAt": last_activity, **extra}
    if settings is not None:
        body["sessionSettings"] = settings
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


# --- the daemon double --------------------------------------------------------

class FakeSessions:
    """A recording stand-in for `sessions.client.Sessions`.

    It answers `state`, records every `record_nudge`, `ping` and `submit`, and
    enforces the dedupe and cooldown the daemon enforces, so a unit test proves
    the same "at most once" the store does without starting a daemon.
    """

    def __init__(self, *, retired: dict[str, dict] | None = None,
                 nudges: dict[str, dict] | None = None,
                 revives: dict[str, dict] | None = None,
                 lane_sessions: Iterable[str] = (),
                 revive_holders: dict[str, str] | None = None,
                 now: datetime = NOW):
        self.retired = dict(retired or {})
        self.nudges = dict(nudges or {})
        self.revives = dict(revives or {})
        self.lane_sessions = list(lane_sessions)
        self.revive_holders = dict(revive_holders or {})
        self.now = now
        self.pings: list[tuple[str, str]] = []
        self.submits: list[Any] = []
        self.minted: list[bool] = []
        self.records: list[dict[str, Any]] = []
        self.state_calls: list[Any] = []
        self.notice_id = 0

    # the `sessions` op
    def state(self, session_ids: list[str] | None = None) -> dict[str, Any]:
        self.state_calls.append(session_ids)
        keys = session_ids if session_ids is not None else sorted(
            set(self.retired) | set(self.nudges) | set(self.revives)
            | set(self.revive_holders))
        return {"sessions": {key: {"retired": self.retired.get(key),
                                   "last_nudge": self.nudges.get(key),
                                   "last_revive": self.revives.get(key),
                                   "revive_holder": self.revive_holders.get(key)}
                             for key in keys},
                "lane_sessions": list(self.lane_sessions)}

    def record_nudge(self, session_id: str, *, dedupe_key: str | None,
                     cooldown_s: float | None, kind: str = "nudge",
                     force: bool = False,
                     detail: dict[str, Any] | None = None) -> dict[str, Any]:
        self.records.append({"session_id": session_id, "dedupe_key": dedupe_key,
                             "cooldown_s": cooldown_s, "kind": kind, "force": force,
                             "detail": dict(detail or {})})
        previous = self.nudges.get(session_id)
        if previous and not force:
            if dedupe_key and previous.get("dedupe_key") == dedupe_key:
                return {"recorded": False, "session_id": session_id,
                        "reason": "already nudged at this interruption point"}
            if cooldown_s and previous.get("at"):
                elapsed = (self.now - datetime.fromisoformat(
                    previous["at"].replace("Z", "+00:00"))).total_seconds()
                if elapsed < cooldown_s:
                    return {"recorded": False, "session_id": session_id,
                            "reason": f"nudged {int(elapsed)}s ago"}
        self.nudges[session_id] = {"dedupe_key": dedupe_key, "kind": kind,
                                   "at": iso(self.now)}
        return {"recorded": True, "session_id": session_id, "dedupe_key": dedupe_key}

    def record_revive(self, session_id: str, *, dedupe_key: str | None,
                      detail: dict[str, Any] | None = None) -> dict[str, Any]:
        self.revives[session_id] = {"dedupe_key": dedupe_key, "at": iso(self.now),
                                    **dict(detail or {})}
        return {"session_id": session_id, "recorded": True}

    def retire(self, session_id: str, reason: str | None = None) -> dict[str, Any]:
        self.retired[session_id] = {"reason": reason, "at": iso(self.now)}
        return {"session_id": session_id, "action": "retire", "recorded": True}

    def unretire(self, session_id: str) -> dict[str, Any]:
        self.retired.pop(session_id, None)
        return {"session_id": session_id, "action": "unretire", "recorded": True}

    # delivery and dispatch
    def ping(self, session_id: str, text: str) -> dict[str, Any]:
        self.pings.append((session_id, text))
        self.notice_id -= 1
        return {"pong": True, "session_id": session_id, "notice_id": self.notice_id}

    def submit(self, args: Any, *, minted: bool = False) -> dict[str, Any]:
        self.submits.append(args)
        self.minted.append(minted)                  # C-16.3: whose request id it is
        return {"job_id": f"job-{len(self.submits)}", "created": True}


def policy(**overrides: Any) -> dict[str, Any]:
    """The shipped defaults, with the caps a test wants to move."""
    from subfleet.policy import DEFAULT_POLICY_PATH, load_policy
    value = load_policy(DEFAULT_POLICY_PATH)
    value["sessions"] = {**value["sessions"], **overrides}
    return value
