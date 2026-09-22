"""Test-only guard overlay. Never an installed runtime fallback or real policy."""

import hashlib
import json
from pathlib import Path

from subfleet.guard.preflight import hooks_trust_hash, override_string


HOOK = b'''#!/bin/sh
# Synthetic guard for isolated fake-provider tests only.
# Its denial branch makes fixture behavior observable without personal rules.
case "$*" in
  --fixture-deny) exit 2 ;;
esac
exit 0
'''
REFERENCE = "/fixture/reviewed/never-rules-hook.sh"


def install_guard(root: Path) -> tuple[Path, Path]:
    directory = root / "guard"
    directory.mkdir(parents=True, exist_ok=True)
    hook, pin = directory / "never-rules-hook.sh", directory / "TRUST"
    hook.write_bytes(HOOK)
    hook.chmod(0o755)
    pin.write_text(json.dumps({
        "hook_sha256": hashlib.sha256(HOOK).hexdigest(),
        "codex_version": "codex-cli 0.153.3",
        "reference_hook_path": REFERENCE,
        "hooks_trust_hash": hooks_trust_hash(REFERENCE),
        "override": override_string(REFERENCE),
        "provenance": "Synthetic test-only fixture; not operator security policy.",
    }))
    pin.chmod(0o600)
    return hook, pin
