"""Subfleet's own system prompt and the two tool definitions (C-31.8).

The system prompt is constant text chosen only by the attempt's sandbox and network
setting; the job's prompt never enters it, it goes in the first user turn. It never
holds an identity line, "Claude Code" or "Agent SDK" (invariant 2).
"""
from __future__ import annotations

import re

from subfleet.api_agent.cli import READ_ONLY, WORKSPACE_WRITE

#: Invariant 2's phrases, compared after `normalize`.
FORBIDDEN = ("you are claude code", "claude code", "agent sdk")
#: The editor's `view` output cap, which the tool definition also declares.
VIEW_MAX_CHARS = 60_000

_INTRO = (
    "You are an autonomous software agent running one job for Subfleet, a job runner. "
    "You work alone: nobody watches the job while it runs or answers questions, so make "
    "reasonable choices and keep going until the job is done.\n\n"
    "You work in a git worktree with two tools.\n"
)
_SHELL = {
    WORKSPACE_WRITE: (
        "- bash: a persistent shell that starts in the worktree and keeps its working "
        "directory and variables between commands. It runs in a sandbox. You can write "
        "inside the worktree and in $TMPDIR, and nowhere else. The repository's .git "
        "directory is read-only, so git commands that write (commit, add, stash, checkout) "
        "fail.\n"),
    READ_ONLY: (
        "- bash: a persistent shell that starts in the worktree and keeps its working "
        "directory and variables between commands. It runs in a read-only sandbox: you "
        "can read files, and you can write only in $TMPDIR.\n"),
}
_NETWORK = {
    True: "  The shell can reach the network.\n",
    False: "  The shell has no network access.\n",
}
_GUARD = (
    "  Each command first passes a guard that refuses some commands; a refusal comes "
    "back as the command's result. A command that runs too long is stopped.\n"
)
_EDITOR = {
    WORKSPACE_WRITE: (
        "- str_replace_based_edit_tool: views, creates and edits files in the worktree. "
        "Paths are absolute or relative to the worktree's top folder.\n\n"),
    READ_ONLY: (
        "- str_replace_based_edit_tool: views files in the worktree; this job cannot "
        "change them. Paths are absolute or relative to the worktree's top folder.\n\n"),
}
_FINISH = {
    WORKSPACE_WRITE: (
        "Leave your work in the worktree's files: you cannot commit it, and Subfleet "
        "collects the changes you leave in the tree when the job ends. Don't push, open "
        "pull requests or publish anything unless the job asks you to. When you are done, "
        "reply with a short final message that says what you changed, how you checked it, "
        "and anything left undone."),
    READ_ONLY: (
        "This job changes nothing: your final message is its result. When you are done, "
        "reply with that message, complete and self-contained, and say what you checked."),
}


def system_prompt(sandbox: str, network: bool) -> str:
    """The system prompt for an attempt: one of four constant texts."""
    return (_INTRO + _SHELL[sandbox] + _NETWORK[bool(network)] + _GUARD + _EDITOR[sandbox]
            + _FINISH[sandbox])


def system_blocks(sandbox: str, network: bool) -> list[dict]:
    """The request's `system` field, with a cache breakpoint on it."""
    return [{"type": "text", "text": system_prompt(sandbox, network), "cache_control": {"type": "ephemeral"}}]


def tools() -> list[dict]:
    """The two client tools, the last with a cache breakpoint on the tool definitions."""
    return [
        {"type": "bash_20250124", "name": "bash"},
        {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool",
         "max_characters": VIEW_MAX_CHARS, "cache_control": {"type": "ephemeral"}},
    ]


def normalize(text: str) -> str:
    """Invariant 2's comparison form: case folded, whitespace runs to one space."""
    return re.sub(r"\s+", " ", text.casefold()).strip()


def identity_free(system: object) -> bool:
    """Whether a request's `system` field holds none of `FORBIDDEN`, normalized."""
    if isinstance(system, str):
        texts = [system]
    elif isinstance(system, list):
        texts = [block.get("text", "") for block in system if isinstance(block, dict)]
    elif system is None:
        texts = []
    else:
        return False
    joined = normalize(" ".join(texts))
    return not any(phrase in joined for phrase in FORBIDDEN)
