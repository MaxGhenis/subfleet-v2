"""The never-rules guard and the publish hook, run before a tool acts (C-31.14).

Each hook gets the input shape Codex's PreToolUse hooks get (`tool_name`, `cwd`,
`tool_input.command`), on the host, outside the sandbox, with the shell's
environment. It allows with no output and exit 0; it denies with
`hookSpecificOutput.permissionDecision` `deny` on stdout, or with exit 2 and a
reason on stderr. Anything else fails closed: a hook that cannot run, times out,
exits otherwise, or prints what is not a decision denies the call.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

HOOK_TIMEOUT_S = 60.0
REASON_CAP = 2_000


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str | None = None


ALLOW = Verdict(True)


def payload(tool_name: str, command: str, cwd: str, tool_use_id: str, model: str) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": {"command": command},
            "cwd": cwd, "tool_use_id": tool_use_id, "model": model}


def _decision(stdout: str) -> Verdict | None:
    """The verdict a hook printed, or None for output that is not one."""
    try:
        answer = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(answer, dict):
        return None
    specific = answer.get("hookSpecificOutput")
    decision = specific.get("permissionDecision") if isinstance(specific, dict) else None
    reason = specific.get("permissionDecisionReason") if isinstance(specific, dict) else None
    if decision is None and answer.get("decision") in ("block", "deny"):
        decision, reason = "deny", answer.get("reason")
    if decision == "deny":
        return Verdict(False, str(reason or "the hook denied this call")[:REASON_CAP])
    if decision == "ask":
        return Verdict(False, "the hook asked for approval, and nobody approves calls in a job")
    if decision in (None, "allow") and answer.get("continue", True) is not False:
        return ALLOW
    return None


class Hook:
    """One hook executable. `check` never raises."""

    def __init__(self, path: Path, label: str, env: Mapping[str, str]) -> None:
        self.path = path
        self.label = label
        self._env = dict(env)

    def check(self, call: dict, cwd: str) -> Verdict:
        work = cwd if os.path.isdir(cwd) else "/"
        try:
            proc = subprocess.Popen([str(self.path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=self._env, cwd=work, close_fds=True,
                                    process_group=0)
        except OSError as exc:
            return Verdict(False, f"the {self.label} hook could not run ({exc.strerror or type(exc).__name__}), "
                                  "so the call was refused")
        try:
            out, err = proc.communicate(json.dumps(call).encode(), timeout=HOOK_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.communicate()
            return Verdict(False, f"the {self.label} hook timed out after {HOOK_TIMEOUT_S:g} s, "
                                  "so the call was refused")
        stdout = out.decode("utf-8", "replace").strip()
        if proc.returncode == 2:
            reason = err.decode("utf-8", "replace").strip()
            return Verdict(False, (reason or f"the {self.label} hook denied this call")[:REASON_CAP])
        if proc.returncode != 0:
            return Verdict(False, f"the {self.label} hook failed (exit {proc.returncode}), "
                                  "so the call was refused")
        if not stdout:
            return ALLOW
        verdict = _decision(stdout)
        if verdict is None:
            return Verdict(False, f"the {self.label} hook printed no decision it could read, "
                                  "so the call was refused")
        return verdict


def validate(path: Path) -> str | None:
    """Why `path` cannot be a hook, or None."""
    try:
        real = Path(os.path.realpath(path))
        if not real.is_file():
            return "is not a file"
        if not os.access(real, os.X_OK):
            return "is not executable"
    except OSError as exc:
        return exc.strerror or type(exc).__name__
    return None


def patch_text(path: str, added: str, removed: str | None = None) -> str:
    """An apply_patch-format patch, so the guard judges an editor write as it judges
    Codex's edits (its hf-dest rule reads the added lines)."""
    lines = ["*** Begin Patch"]
    if removed is None:
        lines.append(f"*** Add File: {path}")
    else:
        lines.append(f"*** Update File: {path}")
        lines.append("@@")
        lines.extend("-" + line for line in removed.splitlines())
    lines.extend("+" + line for line in added.splitlines())
    lines.append("*** End Patch")
    return "\n".join(lines) + "\n"
