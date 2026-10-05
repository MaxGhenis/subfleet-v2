"""Compile the app's real Swift sources with one probe, the way `app/build.sh` compiles them.

Every frontend test builds its probe from `app/Sources/*.swift` (all of them,
so a file split never leaves a test compiling a stale subset) plus one probe
file that holds the only `@main`. `SUBFLEET_MODEL_TEST` compiles Foundation-only
code (no AppKit or SwiftUI); `SUBFLEET_VIEW_TEST` compiles the views without the
app's entry point.
"""

from __future__ import annotations

from pathlib import Path
import platform
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "app" / "Sources"


def app_sources() -> list[Path]:
    sources = sorted(SOURCES.rglob("*.swift"))
    assert sources, f"no Swift sources under {SOURCES}"
    return sources


def compile_probe(binary: Path, probe: Path | list[Path], flag: str, *, timeout: int = 900) -> Path:
    """Build `binary` from the app's sources and the probe file(s); the machine
    may be loaded, so the timeout is generous."""
    probes = probe if isinstance(probe, list) else [probe]
    # Exercise UIModel with AppKit and notifications, while excluding SwiftUI
    # view bodies. The full view probe retains SUBFLEET_VIEW_TEST separately.
    flags = [flag] if flag != "SUBFLEET_UI_MODEL_TEST" else [flag, "SUBFLEET_MODEL_TEST", "SUBFLEET_VIEW_TEST"]
    defines = [arg for name in flags for arg in ("-D", name)]
    command = ["xcrun", "swiftc", *defines, "-parse-as-library", "-target", f"{platform.machine()}-apple-macos14.0",
               *map(str, app_sources()), *map(str, probes), "-o", str(binary)]
    compiled = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    assert compiled.returncode == 0, compiled.stderr[-20000:]
    return binary
