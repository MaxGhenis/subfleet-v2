"""Bounded, credential-scrubbed handoffs from a Claude session to a fresh agent.

Handoff is the default recovery of a cold session (plan decision 7): rather than
starting a second writer inside a conversation the desktop app may restart under
us, it builds a brief and dispatches it as an ordinary job. The source transcript
stays the durable record; the brief is a bounded excerpt that points at it
(C-23.36).

What a handoff may carry is C-23.14, and it is a safety rule, not a formatting
one. Three separate mechanisms, in this order:

1. **Suppression by pattern.** The result of any tool call whose *input* matches
   a credential-reading pattern — `agent-secret get`, a keychain read, `env` or
   `printenv`, `auth.json`, `.env`, a credentials file — is omitted wholesale,
   and so is the input. This runs before any redaction, so a secret that no regex
   would recognise never reaches the excerpt at all.
2. **Scrubbing by value.** Private keys, JWTs, prefixed API tokens, `Bearer`
   values, `authorization`/`cookie` headers, URL passwords and
   `key = "value"`-shaped assignments are replaced; encoded binary (data URIs,
   long base64 runs, non-printable output) is omitted.
3. **Retention.** Everything else is kept verbatim. Ordinary code, commands and
   tool output are the entire value of a handoff; a lossy rewrite would destroy
   the continuity the brief exists to carry.

Every section is bounded by an explicit character cap from
`policy.json`'s `sessions.handoff_caps` (C-23.36), and the whole brief is
scrubbed once more after assembly so nothing a section boundary spliced together
escapes.

Dispatch is one `subfleet run` submission (C-23.54), detached, with the caller's
session recorded so the completion notice comes back to the session that asked.
The prompt is the job's own `jobs/<job id>/prompt.md` at mode 0600 inside the
0700 state root — it is written once and retained as the immutable record rather
than unlinked, because it no longer sits in the system temp directory (ledger row
212's replacement).

Ported from v1 `subfleet/handoff.py`; the regexes and the section order are its.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts import Sandbox
from ..protocol import SubmitArgs
from . import registry, transcripts

REDACTED = "[REDACTED]"
OMITTED_BINARY = "[binary/base64 tool result omitted]"
OMITTED_SENSITIVE = "[credential-reading tool result omitted]"
OMITTED_SENSITIVE_INPUT = "[credential-reading input omitted]"
OMITTED_UNMATCHED = "[tool result omitted because its input is outside this excerpt]"

MAX_JSON_LINE_CHARS = 4 * 1024 * 1024
FULL_SCAN_BYTES = 64 * 1024 * 1024
LAST_SCAN_BYTES = 2 * 1024 * 1024
PROGRESS_READ_BYTES = 128 * 1024

# --- the scrub list (C-23.14) -------------------------------------------------

_PEM_RE = re.compile(
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
_SYSTEM_REMINDER_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>", re.DOTALL | re.IGNORECASE
)

#: Tool calls whose RESULT is omitted by pattern rather than redacted (C-23.14).
#: Suppression beats redaction here because the value a keychain read returns has
#: no shape a regex can rely on.
#: `MULTILINE`, the leading `^\s*`, and `_tool_corpus` below are v2's, and they
#: close a hole v1 had. v1 matched these against `json.dumps` of the tool input,
#: which turns a real newline into the two characters `\` and `n` — so an `env`
#: on the SECOND line of a Bash command sat behind neither `^` (the rendering
#: starts with `{`) nor a `;&|` separator, and escaped suppression entirely.
#: A multi-line script that dumps the environment is not an exotic input.
_SENSITIVE_TOOL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL | re.MULTILINE)
    for pattern in (
        r"\bagent-secret\s+(?:get|show)\b",
        r"\bsecurity\s+(?:dump-keychain|find-generic-password|find-internet-password)\b",
        r"(?:^[ \t]*|[;&|(]\s*|\bsudo\s+|[\"']command[\"']\s*:\s*[\"'])"
        r"(?:env|printenv)(?=[\s;&|)\"']|$)",
        r"(?:auth\.json|credentials(?:\.json)?|(?:^|[/\s])\.env"
        r"(?:\.[A-Za-z0-9_-]+)?(?=[\s\"']|$))",
    )
)


class HandoffError(ValueError):
    """A user-facing handoff selection or source error.

    Exit 2 (invalid input) by default; a refusal — a request naming something
    the contract forbids continuing — carries 7 instead (C-17.3).
    """

    code = 2

    def __init__(self, message: str, code: int | None = None,
                 fix: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        self.fix = fix


def scrub_secrets(text: str) -> tuple[str, int]:
    """Remove credential values and encoded binary; retain ordinary text."""
    total = 0
    for pattern, replacement in (
        (_PEM_RE, "[PRIVATE KEY REDACTED]"),
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


#: What a section becomes when the budget left for it is smaller than the marker
#: that would explain the truncation. Carried in full so a reader knows the
#: excerpt stops here rather than that the work did.
ELIDED = "… [omitted] …"


def truncate(text: str, limit: int) -> str:
    """C-23.36: bound a section, keeping its head and its tail and saying so.

    The bound is hard. v1 computed `usable = max(0, limit - len(marker))` and
    then sliced `text[-(usable - head):]`, which for `usable == 0` is
    `text[-0:]` — the WHOLE string. So a section whose remaining allowance was
    smaller than the marker came back complete, and the caller's budget
    accounting then subtracted a number far larger than it had. A tool result
    landing on the last few characters of `recent` could carry the entire file.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    marker = f"\n… [{len(text) - limit:,} characters omitted] …\n"
    usable = limit - len(marker)
    if usable <= 0:
        # No room to explain the truncation: say only that there was one.
        return ELIDED[:limit] if limit < len(ELIDED) else ELIDED
    head = int(usable * 0.6)
    tail = usable - head
    return text[:head].rstrip() + marker + (text[-tail:].lstrip() if tail else "")


def clean(text: str, limit: int) -> tuple[str, int]:
    text = _SYSTEM_REMINDER_RE.sub("", text)
    text, redactions = scrub_secrets(text)
    return truncate(text, limit), redactions


def _parse(line: str) -> dict[str, Any] | None:
    if len(line) > MAX_JSON_LINE_CHARS:
        return None
    try:
        value = json.loads(line)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _synthetic(text: str) -> bool:
    """The app's stubs and subfleet's own nudges are not conversation."""
    stripped = text.strip()
    return bool(
        stripped == transcripts.RESUME_STUB_USER
        or stripped == transcripts.RESUME_STUB_ASSISTANT
        or stripped.startswith("[Request interrupted by user")
        or transcripts.MARKER in stripped[:400]
        or transcripts.MUSTER_MARKER in stripped[:400]
    )


def _tool_corpus(value: Any) -> str:
    """What the credential-reading patterns are matched against.

    Two renderings, because neither alone is enough. The JSON one carries the
    key names (`"command":`) and any shape that is not a string. The strings'
    own text carries the LINE STRUCTURE that JSON escaping destroys — and a
    command whose second line is `env` is invisible in the first and obvious in
    the second.
    """
    strings: list[str] = []

    def walk(item: Any, depth: int = 0) -> None:
        if depth > 8:                       # a self-referential input is a bug,
            return                          # not a reason to recurse forever
        if isinstance(item, str):
            strings.append(item)
        elif isinstance(item, dict):
            for key, child in item.items():
                strings.append(str(key))
                walk(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child, depth + 1)

    walk(value)
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(value)
    return "\n".join([rendered, *strings])


def sensitive_tool_call(name: str, value: Any) -> bool:
    """C-23.14: does this tool call read a credential?"""
    if "agent-secret" in name.casefold() or "keychain" in name.casefold():
        return True
    return any(pattern.search(_tool_corpus(value))
               for pattern in _SENSITIVE_TOOL_PATTERNS)


def _tool_result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or "") if content.get("type") == "text" else ""
    if isinstance(content, list):
        return "\n".join(str(item.get("text") or "") for item in content
                         if isinstance(item, dict) and item.get("type") == "text").strip()
    return ""


def _tool_input_text(name: str, value: Any) -> str:
    """Readable input context without committing to any provider's tool schema."""
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, str) and (name.casefold() in {"bash", "shell", "shell_command"}
                                         or "exec" in name.casefold()):
            return command
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def looks_binary(text: str) -> bool:
    if "\x00" in text:
        return True
    sample = text[:16_384]
    if not sample:
        return False
    printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in sample)
    return printable / len(sample) < 0.85


def first_task(path: Path, cap: int) -> tuple[str, str | None, int]:
    """The session's original instruction: the first real human turn."""
    try:
        stream = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HandoffError(f"cannot read transcript {path}: {exc}") from exc
    with stream:
        for line in stream:
            entry = _parse(line)
            if not transcripts.is_main(entry) or entry.get("type") != "user":
                continue
            origin = entry.get("origin") if isinstance(entry.get("origin"), dict) else {}
            if origin.get("kind") in {"task-notification", "peer"}:
                continue
            text = transcripts.text_of(transcripts.blocks(entry.get("message")))
            if not text.strip() or _synthetic(text):
                continue
            cleaned, redactions = clean(text, cap)
            return cleaned, entry.get("uuid"), redactions
    raise HandoffError(f"no user task text found in transcript {path}")


def _reverse_main_entries(path: Path, limit: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in transcripts.lines_reversed(path, max_bytes=FULL_SCAN_BYTES):
        entry = _parse(line)
        if not transcripts.is_main(entry):
            continue
        entries.append(entry)
        if len(entries) >= limit:
            break
    entries.reverse()
    return entries


def recent_excerpt(path: Path, first_uuid: str | None,
                   caps: dict[str, int]) -> tuple[str, int]:
    """The bounded main-chain tail: text, tool inputs, and tool results.

    Tool inputs and results have their own total budgets on top of the section
    cap, so one enormous file read cannot crowd out the conversation that
    explains it. Segments are selected newest-first and re-ordered, so a brief
    that must drop something drops the oldest.
    """
    entries = _reverse_main_entries(path, caps["recent_records"])
    tools: dict[str, tuple[str, bool]] = {}
    segments: list[tuple[str, int, str | None]] = []

    def add(label: str, body: str, *, tool_kind: str | None = None,
            limit: int = 8_000) -> None:
        if not body or _synthetic(body):
            return
        cleaned, redactions = clean(body, limit)
        if cleaned:
            segments.append((f"{label}\n{cleaned}", redactions, tool_kind))

    for entry in entries:
        role = "Claude user:" if entry.get("type") == "user" else "Claude assistant:"
        blocks = transcripts.blocks(entry.get("message"))
        text = transcripts.text_of(blocks)
        if text and entry.get("uuid") != first_uuid:
            add(role, text)
        for block in blocks:
            kind = block.get("type")
            if kind == "tool_use":
                tool_id = str(block.get("id") or "")
                name = str(block.get("name") or "tool")
                sensitive = sensitive_tool_call(name, block.get("input"))
                tools[tool_id] = (name, sensitive)
                if sensitive:
                    segments.append((f"Claude tool call ({name}):\n"
                                     f"{OMITTED_SENSITIVE_INPUT}", 1, "input"))
                else:
                    add(f"Claude tool call ({name}):",
                        _tool_input_text(name, block.get("input")),
                        tool_kind="input", limit=caps["tool_input"])
            elif kind == "tool_result":
                matched = tools.get(str(block.get("tool_use_id") or ""))
                if matched is None:
                    # Its input is outside this excerpt, so its sensitivity is
                    # unknown; unknown means omitted.
                    segments.append((f"Claude tool result:\n{OMITTED_UNMATCHED}", 1, "result"))
                    continue
                name, sensitive = matched
                if sensitive:
                    segments.append((f"Claude tool result ({name}):\n"
                                     f"{OMITTED_SENSITIVE}", 1, "result"))
                    continue
                result = _tool_result_text(block)
                if not result:
                    continue
                if looks_binary(result):
                    segments.append((f"Claude tool result ({name}):\n"
                                     f"{OMITTED_BINARY}", 1, "result"))
                    continue
                add(f"Claude tool result ({name}):", result,
                    tool_kind="result", limit=caps["tool_result"])

    chosen: list[tuple[str, int, str | None]] = []
    remaining = caps["recent"]
    budgets = {"input": caps["tool_inputs_total"], "result": caps["tool_results_total"]}
    for text, redactions, tool_kind in reversed(segments):
        separator = 2 if chosen else 0
        allowance = remaining - separator
        if tool_kind:
            allowance = min(allowance, budgets[tool_kind])
        if allowance <= 0:
            continue
        selected = truncate(text, allowance)
        if not selected:
            continue
        chosen.append((selected, redactions, tool_kind))
        consumed = len(selected)
        remaining -= consumed + separator
        if tool_kind:
            budgets[tool_kind] -= consumed
        if remaining <= 0:
            break
    chosen.reverse()
    return "\n\n".join(item[0] for item in chosen), sum(item[1] for item in chosen)


def _read_bounded(path: Path, max_bytes: int = PROGRESS_READ_BYTES) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size <= max_bytes:
                raw = stream.read()
            else:
                half = max_bytes // 2
                raw = stream.read(half)
                stream.seek(max(0, size - half))
                raw += b"\n... [middle omitted] ...\n" + stream.read(half)
    except OSError:
        return ""
    return raw.decode("utf-8", "replace")


def _run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                              text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return None


def repository_context(cwd: Path, cap: int) -> tuple[str, int]:
    probe = _run_git(cwd, ["rev-parse", "--show-toplevel"])
    if probe is None or probe.returncode != 0:
        return truncate("Not a Git worktree.", cap), 0
    sections = []
    for title, args, empty in (
        ("Status", ["status", "--short", "--branch", "--untracked-files=normal"], "Clean."),
        ("Recent commits", ["log", "-5", "--oneline", "--decorate"], "No commits."),
        ("Salvage refs",
         ["for-each-ref", "--sort=-creatordate", "--count=12",
          "--format=%(refname) %(objectname:short)",
          "refs/codex-salvage", "refs/claude-salvage"], "None."),
    ):
        completed = _run_git(cwd, args)
        body = (completed.stdout.strip()
                if completed is not None and completed.returncode == 0 else "")
        sections.append(f"### {title}\n{body or empty}")
    return clean("\n\n".join(sections), cap)


def latest_metadata(path: Path, *,
                    max_bytes: int = LAST_SCAN_BYTES) -> tuple[str | None, str | None]:
    """The last main-chain entry's timestamp and cwd.

    `max_bytes` is 2 MB for ranking `--last`, where a transcript with nothing in
    its tail simply ranks low. Resolving the WORKDIR is different: a session
    whose last 2 MB happen to be sidechain and tool-result rows has a cwd, and
    v1 scanned the whole file (64 MB) to find it rather than telling the caller
    to pass `-C`. `resolve_workdir` asks for that.
    """
    stamp = cwd = None
    for line in transcripts.lines_reversed(path, chunk=64 * 1024, max_bytes=max_bytes):
        entry = _parse(line)
        if not transcripts.is_main(entry):
            continue
        if cwd is None and isinstance(entry.get("cwd"), str) and entry["cwd"]:
            cwd = entry["cwd"]
        if stamp is None and isinstance(entry.get("timestamp"), str):
            stamp = entry["timestamp"]
        if stamp is not None and cwd is not None:
            break
    return stamp, cwd


def canonical_session_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise HandoffError(f"invalid Claude session id: {value!r}") from exc
    canonical = str(parsed)
    if value.casefold() != canonical:
        raise HandoffError(f"Claude session id must be a canonical UUID: {value!r}")
    return canonical


def _candidate_transcripts() -> list[Path]:
    projects = transcripts.projects_dir()
    found: dict[str, Path] = {}
    for pattern in ("*.jsonl", "*/*.jsonl"):
        try:
            for path in projects.glob(pattern):
                if path.is_file():
                    found[str(path)] = path
        except OSError:
            continue
    return list(found.values())


def resolve_source(session_id: str | None, last: bool, *,
                   current: str | None = None) -> tuple[str, Path]:
    """Which transcript this handoff comes from: a named session, or `--last`."""
    if bool(session_id) == bool(last):
        raise HandoffError("provide exactly one of SESSION_ID or --last")
    if session_id:
        canonical = canonical_session_id(session_id)
        path = transcripts.transcript_path(canonical)
        if path is None:
            raise HandoffError(f"transcript not found for Claude session {canonical}")
        return canonical, path
    if current:
        try:
            canonical = canonical_session_id(current)
        except HandoffError:
            canonical = ""
        if canonical and (path := transcripts.transcript_path(canonical)) is not None:
            return canonical, path
    ranked: list[tuple[tuple, str, Path]] = []
    for path in _candidate_transcripts():
        try:
            canonical = canonical_session_id(path.stem)
            stamp, _cwd = latest_metadata(path)
            mtime = path.stat().st_mtime
        except (HandoffError, OSError):
            continue
        ranked.append(((1 if stamp else 0, stamp or "", mtime, str(path)), canonical, path))
    if not ranked:
        raise HandoffError("no Claude session transcript found for --last")
    _key, canonical, path = max(ranked, key=lambda item: item[0])
    return canonical, path


def resolve_workdir(path: Path, override: str | Path | None) -> tuple[Path, str | None]:
    _stamp, source_cwd = latest_metadata(path, max_bytes=FULL_SCAN_BYTES)
    chosen = (Path(override).expanduser() if override is not None
              else Path(source_cwd).expanduser() if source_cwd else None)
    if chosen is None:
        raise HandoffError("source transcript has no cwd; pass -C DIR")
    try:
        chosen = chosen.resolve()
    except OSError as exc:
        raise HandoffError(f"cannot resolve workdir {chosen}: {exc}") from exc
    if not chosen.is_dir():
        raise HandoffError(f"workdir is not a directory: {chosen}")
    return chosen, source_cwd


@dataclass
class Brief:
    """The dispatched text, plus what the dispatcher needs to route it."""

    text: str
    original: str
    session_id: str
    transcript: str
    workdir: str
    source_cwd: str | None
    redactions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "transcript": self.transcript,
                "workdir": self.workdir, "source_cwd": self.source_cwd,
                "redactions": self.redactions, "characters": len(self.text)}


def build_brief(session_id: str, transcript: Path, cwd: Path, source_cwd: str | None,
                caps: dict[str, int]) -> Brief:
    """The brief itself (C-23.14, C-23.36). Sections in v1's order."""
    original, first_uuid, redactions = first_task(transcript, caps["original_task"])
    recent, recent_redactions = recent_excerpt(transcript, first_uuid, caps)
    if not recent:
        recent = truncate("No additional text or safe tool-result context was available.",
                          caps["recent"])
    redactions += recent_redactions

    progress_path = cwd / "PROGRESS.md"
    if progress_path.is_file():
        progress, count = clean(_read_bounded(progress_path), caps["progress"])
        redactions += count
    else:
        progress = truncate("Not present.", caps["progress"])

    repository, count = repository_context(cwd, caps["repository"])
    redactions += count

    text = f"""# Cross-agent handoff

Continue the source session's work in the target worktree. Inspect the actual
workspace before acting: this is a bounded excerpt, not an authoritative state
snapshot. Ordinary tool inputs and textual results were bounded and credential-
scrubbed; thinking, binary payloads, and credential-reading inputs/results were
omitted. The full transcript may contain sensitive raw material; consult it only
when necessary and never expose credentials.

- Source provider: Claude Code
- Source session: {session_id}
- Source transcript: {transcript}
- Source cwd: {source_cwd or "unknown"}
- Target cwd: {cwd}
- Credential/binary redactions in this brief: {redactions}

## Original task

{original}

## Recent main-chain excerpt

{recent}

## PROGRESS.md

{progress}

## Repository state

{repository}
"""
    # One last pass over the assembled brief: a section boundary can splice two
    # halves into a shape no individual section matched.
    text, final_count = scrub_secrets(text)
    if final_count:
        text = text.replace(
            f"Credential/binary redactions in this brief: {redactions}",
            f"Credential/binary redactions in this brief: {redactions + final_count}")
        redactions += final_count
    return Brief(text=text.rstrip() + "\n", original=original, session_id=session_id,
                 transcript=str(transcript), workdir=str(cwd),
                 source_cwd=source_cwd, redactions=redactions)


@dataclass
class Dispatched:
    brief: Brief
    job_id: str | None = None
    request_id: str = ""
    model: str | None = None
    task: str | None = None
    tier: str | None = None
    sandbox: str | None = None
    caller_session: str | None = None
    prompt_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {**self.brief.to_dict(), "job_id": self.job_id,
                "request_id": self.request_id, "model": self.model,
                "task": self.task, "tier": self.tier, "sandbox": self.sandbox,
                "caller_session": self.caller_session,
                "prompt_path": self.prompt_path}


def sandbox_for(policy: dict[str, Any], task: str | None,
                override: str | None = None) -> str:
    """The handoff's sandbox: the operator's `-s`, else the task's permission.

    v1 derived the task class from the original instruction text and let the
    dispatcher's `permissions` map decide. v2 keeps the map and drops the guess:
    a handoff with no `--task` is read-only, because a writable job carries
    consequences a text classifier should not choose (C-6.5 refuses one on
    `main`, and requires a committed repository).
    """
    if override:
        return override
    permissions = policy.get("permissions", {})
    return permissions.get(task) or permissions.get("*") or Sandbox.READ_ONLY.value


def handoff(sessions, policy: dict[str, Any], *, session_id: str | None, last: bool,
            model: str | None, stage_prompt, workdir: str | Path | None = None,
            task: str | None = None, tier: str | None = None,
            sandbox: str | None = None, caller_session: str | None = None,
            caller_pid: int | None = None, out_path: str | None = None,
            current_session: str | None = None, request_id: str | None = None,
            lane_ids: Any = None, dry_run: bool = False) -> Dispatched:
    """Build one brief and submit it through the ordinary path (C-23.54).

    `--to` is a routing pin, so the brief inherits the same model resolution,
    lane picking, guard, salvage, ledger and notices as any other job. The
    caller's session is recorded so the completion notice comes back to it.
    """
    caps = {**policy.get("sessions", {}).get("handoff_caps", {})}
    canonical, transcript = resolve_source(session_id, last, current=current_session)
    if registry.is_lane_run(canonical, lane_ids=lane_ids or (), transcript=transcript):
        # C-23.31: a headless lane run is never continued, and a request naming
        # one is refused with the reason. Its transcript is one brief and one
        # answer; there is no conversation to hand to anybody.
        raise HandoffError(
            f"{canonical} is a headless lane run (claude -p), not a session", 7,
            "`subfleet runs show <job>` for what that lane produced")
    target, source_cwd = resolve_workdir(transcript, workdir)
    brief = build_brief(canonical, transcript, target, source_cwd, caps)
    identity = request_id or str(uuid.uuid4())
    chosen = sandbox_for(policy, task, sandbox)
    if dry_run:
        return Dispatched(brief=brief, request_id=identity, model=model,
                          task=task, tier=tier, sandbox=chosen,
                          caller_session=caller_session)
    prompt_path = str(stage_prompt(brief.text))
    args = SubmitArgs(
        request_id=identity,
        kind="handoff",
        workdir=str(target),
        prompt_path=prompt_path,
        sandbox=chosen,
        task=task,
        tier=tier,
        pinned_model=model,
        out_path=out_path,
        name=f"handoff-{canonical[:8]}",
        caller_session=caller_session,
        caller_pid=caller_pid,
    )
    result = sessions.submit(args)
    return Dispatched(brief=brief, job_id=result.get("job_id"), request_id=identity,
                      model=model, task=task, tier=tier, sandbox=chosen,
                      caller_session=caller_session, prompt_path=prompt_path)


__all__ = ["Brief", "Dispatched", "HandoffError", "build_brief", "canonical_session_id",
           "clean", "first_task", "handoff", "latest_metadata", "looks_binary",
           "recent_excerpt", "repository_context", "resolve_source", "resolve_workdir",
           "sandbox_for", "scrub_secrets", "sensitive_tool_call", "truncate"]
