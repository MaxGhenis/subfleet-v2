#!/usr/bin/env python3
"""Stage an explicitly reviewed guard pair without replacing existing policy.

Run from this private checkout before installing the first core-only release.
No daemon, provider, active attempt, or existing release is touched.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import tempfile

from subfleet.guard.preflight import load_guard


def stage(source: Path, state_root: Path) -> Path:
    hook, pin, _ = load_guard(hook_path=source / "never-rules-hook.sh", trust_path=source / "TRUST")
    contents = {"never-rules-hook.sh": hook.read_bytes(), "TRUST": pin.read_bytes()}
    destination = state_root / "guard"
    if destination.is_symlink():
        raise ValueError(f"refusing to stage through an existing overlay symlink at {destination}")
    if destination.exists():
        # Idempotent staging is allowed, but neither policy nor permissions on
        # an existing installation are silently repaired or replaced.
        load_guard(hook_path=destination / "never-rules-hook.sh", trust_path=destination / "TRUST")
        if any((destination / name).read_bytes() != data for name, data in contents.items()):
            raise ValueError(f"refusing to replace a different overlay at {destination}")
        return destination
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    scratch = Path(tempfile.mkdtemp(prefix=".guard-stage-", dir=state_root))
    try:
        for name, data in contents.items():
            path = scratch / name
            path.write_bytes(data)
            path.chmod(0o755 if name.endswith(".sh") else 0o600)
        # Validate the exact staged bytes, not just the earlier source read.
        load_guard(hook_path=scratch / "never-rules-hook.sh", trust_path=scratch / "TRUST")
        os.rename(scratch, destination)
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        destination = stage(args.source.expanduser().resolve(), args.state_root.expanduser().resolve())
    except (ValueError, OSError) as exc:
        parser.exit(7, f"Guard overlay staging refused: {exc}\n")
    print(f"Reviewed overlay staged unchanged at {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
