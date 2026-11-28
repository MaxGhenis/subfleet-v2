"""Compile the app's real Swift sources with one probe, the way `app/build.sh` compiles them.

Every frontend test builds its probe from `app/Sources/*.swift` (all of them,
so a file split never leaves a test compiling a stale subset) plus one probe
file that holds the only `@main`. `SUBFLEET_MODEL_TEST` compiles Foundation-only
code (no AppKit or SwiftUI); `SUBFLEET_VIEW_TEST` compiles the views without the
app's entry point.
"""

from __future__ import annotations

from pathlib import Path
import contextlib
import hashlib
import os
import platform
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "app" / "Sources"


def app_sources() -> list[Path]:
    sources = sorted(SOURCES.rglob("*.swift"))
    assert sources, f"no Swift sources under {SOURCES}"
    return sources


def compile_probe(binary: Path, probe: Path | list[Path], flag: str, *, timeout: int = 540,
                  sources: list[Path] | None = None) -> Path:
    """Build `binary` from the app's sources (or `sources`, such as a mutant's) and the probe
    file(s); the machine may be loaded, so the timeout is generous."""
    probes = probe if isinstance(probe, list) else [probe]
    sources = app_sources() if sources is None else sources
    # Exercise UIModel with AppKit and notifications, while excluding SwiftUI
    # view bodies. The full view probe retains SUBFLEET_VIEW_TEST separately.
    # One frontend invocation (whole-module) keeps the full-source probes inside a
    # foreground slice; without WMO each primary file reparses the entire source list.
    flags = [flag] if flag != "SUBFLEET_UI_MODEL_TEST" else [flag, "SUBFLEET_MODEL_TEST", "SUBFLEET_VIEW_TEST"]
    defines = [arg for name in flags for arg in ("-D", name)]
    command = ["xcrun", "swiftc", "-whole-module-optimization", *defines, "-parse-as-library", "-target", f"{platform.machine()}-apple-macos14.0",
               *map(str, sources), *map(str, probes), "-o", str(binary)]
    if os.environ.get("SUBFLEET_SWIFT_NESTED_SANDBOX") == "off":
        command[2:2] = ["-Xfrontend", "-disable-sandbox"]
    # Separate foreground slices reuse only an exact source/flag match.
    inputs = [*sources, *probes]
    digest = hashlib.sha256("\0".join(command[:-2]).encode())
    for path in inputs:
        digest.update(path.read_bytes())
    cached = ROOT / "build/frontend-probes" / digest.hexdigest()
    if cached.is_file():
        shutil.copy2(cached, binary)
        return binary
    compiled = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    assert compiled.returncode == 0, compiled.stderr[-20000:]
    cached.parent.mkdir(parents=True, exist_ok=True)
    # Another hold in this checkout may read the cache at any moment, and a copy in progress is
    # already a file (#238 review r1 saw is_file() true at 0 bytes). Fill it under a name of its own in
    # the same folder, then rename it into place: the final name only ever holds a whole probe.
    descriptor, partial = tempfile.mkstemp(dir=cached.parent, prefix=f".{cached.name}.", suffix=".partial")
    os.close(descriptor)
    try:
        shutil.copy2(binary, partial)
        os.replace(partial, cached)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(partial)
    return binary
