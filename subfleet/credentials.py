"""Resolve credential references directly into launch environment additions."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .adapters.base import AdapterError
from .contracts import Credential


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
        result = subprocess.run(["security", "find-generic-password", "-s", credential.ref, "-w"],
                                capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        raise AdapterError("could not resolve lane keychain credential", code=7,
                           fix="unlock the login keychain and verify the lane credential reference") from None
    if result.returncode or not result.stdout.strip():
        raise AdapterError("could not resolve lane keychain credential", code=7,
                           fix="unlock the login keychain and verify the lane credential reference")
    return {"CLAUDE_CODE_OAUTH_TOKEN": result.stdout.rstrip("\r\n")}


resolve = resolve_credential
