"""The launch of one conversation turn (C-26.1, C-26.11; design §7).

A turn job's `manifest.json` carries a `turn` block written by the dispatcher.
This module turns it into the same `Launch` record every attempt has, so the
guardian, containment, adoption and finalization paths are the ordinary ones.
What differs is the process: the provider's bidirectional mode, stdin through
the guardian relay (`stdin_path` None), and no prompt file on stdin.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..adapters.claude import ENV_REMOVE, ClaudeAdapter
from ..contracts import Launch, Sandbox
from . import claude_turn, codex_turn
from .turn import Image, TurnSpec

TURN_MANIFEST_KEY = "turn"


def lane_email(lane) -> str | None:
    """The account a lane claims, in the form the provider reports it: the
    email label (C-1.4). `identity` is `<account>:<org>` uuids, which neither
    provider's turn protocol reports."""
    for value in (getattr(lane, "label", None), getattr(lane, "identity", None)):
        if isinstance(value, str) and "@" in value and ":" not in value:
            return value
    return None


def spec_from_manifest(turn: dict[str, Any], *, lane_email: str | None, guard_hash: str | None = None,
                       unified_exec_off: bool = False, model_ref: str | None = None) -> TurnSpec:
    settings = turn["settings"]
    model_ref = model_ref or turn.get("model_ref")
    # Codex has no aliases: the server is always asked for the model id admission
    # routed to. Claude gets the conversation's catalog value (`opus[1m]`), which
    # `--model` accepts and `initialize` lists (design D-19).
    model_id = model_ref if turn["provider"] == "codex" and model_ref else settings["model"]
    return TurnSpec(
        provider=turn["provider"], message_id=turn["message_id"], text=turn["text"],
        model_id=model_id, permission=settings["permission"],
        native_session_id=turn.get("native_session_id"), new_session_id=turn.get("new_session_id"),
        effort=settings.get("effort"), effort_default=bool(turn.get("effort_default")),
        fast=bool(settings.get("fast")),
        images=tuple(Image(i["sha256"], i["media_type"], i["path"]) for i in turn.get("images", ())),
        cwd=turn["cwd"], lane_email=lane_email, guard_hash=guard_hash,
        unified_exec_off=unified_exec_off, model_ref=model_ref, network=bool(turn.get("network")),
    )


def claude_launch(turn: dict[str, Any], *, attempt_id: str, attempt_dir: Path, lane, credential_env: dict[str, str],
                  model_id: str, adapter: ClaudeAdapter | None = None, chip_host: dict[str, str] | None = None) -> Launch:
    adapter = adapter or ClaudeAdapter()
    spec = spec_from_manifest(turn, lane_email=lane_email(lane), model_ref=model_id)
    read_only = adapter.permission_args(Sandbox.READ_ONLY)
    from .chip_host import mcp_config
    argv = claude_turn.argv(spec, claude_bin=adapter.claude_bin, read_only_flags=read_only,
                           chip_mcp_config=mcp_config(chip_host) if chip_host is not None else None)
    session_id = spec.native_session_id or spec.new_session_id
    env_remove: tuple[str, ...] = ENV_REMOVE
    if spec.permission == "read-only":
        from ..adapters.isolation import claude_env_remove
        env_remove = (*ENV_REMOVE, *claude_env_remove({**os.environ, **credential_env}))
    transcript = adapter.expected_transcript_path(spec.cwd, session_id, credential_env)
    notes = adapter._launch_notes(lane=lane, attempt_id=attempt_id, model_id=model_id, session_id=session_id,
                                  workdir=spec.cwd, transcript=transcript,
                                  sandbox="read-only" if spec.permission == "read-only" else "workspace-write",
                                  resumed_from=spec.native_session_id)
    notes.update(turn=True, conversation_id=turn["conversation_id"], message_id=turn["message_id"],
                 provider="claude")
    return Launch(
        argv=tuple(argv), env_add={**credential_env, **claude_turn.environment()}, env_remove=env_remove,
        cwd=spec.cwd, stdin_path=None, stdout_path=str(attempt_dir / "stdout"),
        stderr_path=str(attempt_dir / "stderr"), raw_stream_path=None, native_session_id=session_id,
        lane_id=lane.lane_id, notes=notes)


def codex_launch(turn: dict[str, Any], *, attempt_id: str, attempt_dir: Path, lane, credential_env: dict[str, str],
                 model_id: str, executable: str, override: str, unified_exec_off: bool = False) -> Launch:
    spec = spec_from_manifest(turn, lane_email=lane_email(lane), unified_exec_off=unified_exec_off, model_ref=model_id)
    home = lane.home or lane.credential.ref
    if not home:
        raise ValueError("a Codex turn needs its lane's home")
    argv = codex_turn.argv(executable, override, unified_exec_off=unified_exec_off)
    env = dict(credential_env)
    env["CODEX_HOME"] = str(Path(home).expanduser())
    notes = {"turn": True, "conversation_id": turn["conversation_id"], "message_id": turn["message_id"],
             "provider": "codex", "lane_id": lane.lane_id, "attempt_id": attempt_id, "model_id": model_id,
             "identity": lane.identity, "label": lane.label, "codex_home": env["CODEX_HOME"],
             "thread_id": spec.native_session_id, "workdir": spec.cwd}
    return Launch(
        argv=tuple(argv), env_add=env, env_remove=("CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_THREAD_ID",
                                                     "CODEX_SESSION_ID"),
        cwd=spec.cwd, stdin_path=None, stdout_path=str(attempt_dir / "stdout"),
        stderr_path=str(attempt_dir / "stderr"), raw_stream_path=None, native_session_id=spec.native_session_id,
        lane_id=lane.lane_id, notes=notes)
