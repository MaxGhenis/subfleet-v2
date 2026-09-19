"""Resolve credential references directly into launch environment additions."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .adapters.base import AdapterError
from .contracts import Credential


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
        token = os.environ.get(credential.ref)
        if not token or not token.strip():
            raise AdapterError("could not resolve lane environment credential", code=7,
                               fix=f"set {credential.ref} in the daemon environment to the lane OAuth token")
        return {"CLAUDE_CODE_OAUTH_TOKEN": token}
    if credential.kind != "keychain-token" or credential.provider != "claude":
        raise AdapterError("unsupported credential kind for provider", code=7)
    try:
        result = subprocess.run(keychain_command(credential.ref),
                                capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        raise AdapterError("could not resolve lane keychain credential", code=7,
                           fix="unlock the configured credential store and verify the lane reference with agent-secret") from None
    if result.returncode or not result.stdout.strip():
        raise AdapterError("could not resolve lane keychain credential", code=7,
                           fix="unlock the configured credential store and verify the lane reference with agent-secret")
    return {"CLAUDE_CODE_OAUTH_TOKEN": result.stdout.rstrip("\r\n")}


resolve = resolve_credential
