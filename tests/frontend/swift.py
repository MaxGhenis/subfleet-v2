"""Compile the app's real Swift sources with one probe, the way `app/build.sh` compiles them.

Every frontend test builds its probe from `app/Sources/*.swift` (all of them,
so a file split never leaves a test compiling a stale subset) plus one probe
file that holds the only `@main`. `SUBFLEET_MODEL_TEST` compiles Foundation-only
code (no AppKit or SwiftUI); `SUBFLEET_VIEW_TEST` compiles the views without the
app's entry point.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import os
import platform
import shutil
import signal
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "app" / "Sources"


def app_sources() -> list[Path]:
    sources = sorted(SOURCES.rglob("*.swift"))
    assert sources, f"no Swift sources under {SOURCES}"
    return sources


def run_swift(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a compiler or app/build.sh, killing and reaping its group on timeout."""
    compiler = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
    try:
        output, errors = compiler.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(compiler.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        compiler.communicate()
        raise
    return subprocess.CompletedProcess(command, compiler.returncode, output, errors)


def compile_probe(binary: Path, probe: Path | list[Path], flag: str, *, timeout: int = 540) -> Path:
    """Build `binary` from the app's sources and the probe file(s); the machine
    may be loaded, so the timeout is generous."""
    probes = probe if isinstance(probe, list) else [probe]
    # Exercise UIModel with AppKit and notifications, while excluding SwiftUI
    # view bodies. The full view probe retains SUBFLEET_VIEW_TEST separately.
    # One frontend invocation (whole-module) keeps the full-source probes inside a
    # foreground slice; without WMO each primary file reparses the entire source list.
    flags = [flag] if flag != "SUBFLEET_UI_MODEL_TEST" else [flag, "SUBFLEET_MODEL_TEST", "SUBFLEET_VIEW_TEST"]
    defines = [arg for name in flags for arg in ("-D", name)]
    command = ["xcrun", "swiftc", "-whole-module-optimization", *defines, "-parse-as-library", "-target", f"{platform.machine()}-apple-macos14.0",
               *map(str, app_sources()), *map(str, probes), "-o", str(binary)]
    if os.environ.get("SUBFLEET_SWIFT_NESTED_SANDBOX") == "off":
        command[2:2] = ["-Xfrontend", "-disable-sandbox"]
    # Separate foreground slices reuse only an exact source/flag match.
    inputs = [*app_sources(), *probes]
    digest = hashlib.sha256("\0".join(command[:-2]).encode())
    for path in inputs:
        digest.update(path.read_bytes())
    cached = ROOT / "build/frontend-probes" / digest.hexdigest()
    if cached.is_file():
        shutil.copy2(cached, binary)
        return binary
    # Keep the compiler and its frontend children tied to this foreground run.
    # subprocess.run's timeout kills only swiftc, leaving its children compiling.
    result = run_swift(command, timeout=timeout)
    assert result.returncode == 0, result.stderr[-20000:]
    cached.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, cached)
    return binary
