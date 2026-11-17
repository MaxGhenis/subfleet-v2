"""Frozen caller oracle from release/217 at adf66f2e5f53b7f4dfd6786568ee18a1e4b09b83.

The complete original cli.py was SHA-256 checked before extracting these AST
source segments. SOURCE_SHA256 pins that provenance; FUNCTIONS_SHA256 checks
this self-contained fixture without requiring git history. in_claude_session
is included unchanged because launch_mode calls it.
"""

from __future__ import annotations

import argparse
import os

SOURCE_COMMIT = "adf66f2e5f53b7f4dfd6786568ee18a1e4b09b83"
SOURCE_SHA256 = "a635c61b8d17e073958586bdde7dbd3370773d05460a38e5a40c3c25946fb2fb"
FUNCTIONS_SHA256 = "0a9607457ad53e54401971ac08326e6fc0a0a10c9620e1c8c66ce7c269203d02"

def session_id(env: dict[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    return (env.get("CLAUDE_CODE_SESSION_ID") or env.get("SUBFLEET_SESSION_ID") or "").strip() or None


def in_claude_session(env: dict[str, str] | None = None) -> bool:
    """True inside a Claude Code session's tool shell, exactly as v1 decides.

    The harness exports `CLAUDECODE=1` and `CLAUDE_CODE_SESSION_ID` to every
    Bash tool process, and both are inherited by anything it spawns; that is
    the process tree that dies on an account switch (v1 `notify.in_claude_session`).
    """
    env = os.environ if env is None else env
    return bool((env.get("CLAUDECODE") or "").strip()) or bool(
        (env.get("CLAUDE_CODE_SESSION_ID") or "").strip())


def launch_mode(args: argparse.Namespace,
                env: dict[str, str] | None = None) -> tuple[str, str]:
    """('detached' | 'sync', why), following v1's precedence (C-17.6).

    Inside a Claude Code session the tool shell's process tree dies on an
    account switch or the desktop app's idle SIGTERM, so `run` returns as soon
    as the daemon has the job and the caller re-joins with `subfleet wait`.
    Outside a session (a terminal, a launchd lane) `run` blocks as v1 did.
    """
    env = os.environ if env is None else env
    if getattr(args, "dry_run", False) or getattr(args, "why", False):
        return "sync", "dry-run"
    if getattr(args, "detach", False):
        return "detached", "-d"
    override = (env.get("SUBFLEET_RUN_DETACH") or "").strip().lower()
    if override in {"0", "false", "no", "off"}:
        return "sync", "SUBFLEET_RUN_DETACH=0"
    if override in {"1", "true", "yes", "on"}:
        return "detached", "SUBFLEET_RUN_DETACH=1"
    if in_claude_session(env):
        if getattr(args, "attach", False):
            return "detached", "inside a Claude session; --attach waits inline"
        return "detached", "inside a Claude session — the job must outlive it"
    return "sync", "outside a Claude session"
