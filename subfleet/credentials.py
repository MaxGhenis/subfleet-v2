"""Resolve credential references directly into launch environment additions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import os
import subprocess
from pathlib import Path
from typing import Any

from .launch_env import allowed_name, build_environment, valid_name
from .adapters.base import AdapterError
from .contracts import Credential, Lane


def env_references(lanes: Iterable[Lane | Mapping[str, Any]]) -> frozenset[str]:
    """D-ENV1: the daemon variables env-kind lanes name, from lanes or roster rows.

    The daemon alone holds them. Each lane receives only its own, resolved into
    its credential; every launch withholds all of them by name.
    """
    references = set()
    for lane in lanes:
        if isinstance(lane, Mapping):
            credential = lane.get("credential")
            if not isinstance(credential, Mapping):
                credential = {"kind": lane.get("credential_kind"), "ref": lane.get("credential_ref")}
            kind, ref = credential.get("kind"), credential.get("ref")
        else:
            kind, ref = lane.credential.kind, lane.credential.ref
        if kind == "env" and valid_name(ref):
            references.add(ref)
    return frozenset(references)


def keychain_command(reference: str, security_bin: str = "security") -> list[str]:
    """Keep v1's dedicated agent keychain when its helper is installed.

    A failed helper read must not fall through to a same-named, stale credential
    in the login keychain. Hosts without the helper retain the native backend.
    """
    helper = os.environ.get("CLAUDE_LANE_AGENT_SECRET")
    default = Path.home() / "bin" / "agent-secret"
    # Provider-owned desktop/home OAuth blobs stay in their native keychain.
    # Only the explicit per-lane setup-token namespace belongs to agent-secret.
    if reference.startswith("claude-quota-") and (helper or default.is_file()):
        return [helper or str(default), "get", reference]
    return [security_bin, "find-generic-password", "-s", reference, "-w"]


def resolve_credential(credential: Credential) -> dict[str, str]:
    """C-10.5: credential values appear only in the returned child environment."""
    if credential.kind == "home" and credential.provider in ("codex", "claude"):
        key = "CODEX_HOME" if credential.provider == "codex" else "CLAUDE_CONFIG_DIR"
        return {key: str(Path(credential.ref).expanduser().resolve())}
    if credential.kind == "env" and credential.provider == "claude":
        if allowed_name(credential.ref):
            # D-ENV1: every launch inherits an allowlisted name, so it cannot be one lane's.
            raise AdapterError("lane environment credential names a variable every launch inherits", code=7,
                               fix=f"store the lane OAuth token under a daemon variable other than {credential.ref}",
                               reason="credential-env-inherited")
        token = os.environ.get(credential.ref)
        if not token or not token.strip():
            raise AdapterError("could not resolve lane environment credential", code=7,
                               fix=f"set {credential.ref} in the daemon environment to the lane OAuth token",
                               reason="credential-env")
        return {"CLAUDE_CODE_OAUTH_TOKEN": token}
    if credential.kind != "keychain-token" or credential.provider != "claude":
        raise AdapterError("unsupported credential kind for provider", code=7, reason="credential-kind")
    try:
        result = subprocess.run(keychain_command(credential.ref),
                                capture_output=True, text=True, timeout=10, check=False, env=build_environment())
    except (OSError, subprocess.SubprocessError):
        raise AdapterError("could not resolve lane keychain credential", code=7,
                           fix="unlock the configured credential store and verify the lane reference with agent-secret",
                           reason="credential-keychain") from None
    if result.returncode or not result.stdout.strip():
        raise AdapterError("could not resolve lane keychain credential", code=7,
                           fix="unlock the configured credential store and verify the lane reference with agent-secret",
                           reason="credential-keychain")
    return {"CLAUDE_CODE_OAUTH_TOKEN": result.stdout.rstrip("\r\n")}


resolve = resolve_credential
