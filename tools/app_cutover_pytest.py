"""Verification-only content cache for foreground Swift probes.

Load with pytest -p tools.app_cutover_pytest. Each binary is compiled from the
exact production/probe contents and flags; changing a mutation changes its
key. This avoids repeating expensive Swift compilation across short slices.
"""
import hashlib
import os
from pathlib import Path

from tests.frontend import swift

original_compile = swift.compile_probe


def cached_compile(binary, probe, flag, *, timeout=900):
    cache = Path(os.environ.get("SF_CUTOVER_PROBE_CACHE", "build/app-cutover-evidence/probes")).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    probes = probe if isinstance(probe, list) else [probe]
    digest = hashlib.sha256((flag + os.environ.get("PATH", "")).encode())
    for path in [*swift.app_sources(), *probes]:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    target = cache / digest.hexdigest()
    if not target.is_file():
        original_compile(target, probe, flag, timeout=timeout)
    return target


swift.compile_probe = cached_compile
