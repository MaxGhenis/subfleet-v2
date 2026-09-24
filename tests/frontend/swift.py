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
    command = ["xcrun", "swiftc", "-D", flag, "-parse-as-library", "-target", f"{platform.machine()}-apple-macos14.0",
               *map(str, app_sources()), *map(str, probes), "-o", str(binary)]
    compiled = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    assert compiled.returncode == 0, compiled.stderr[-20000:]
    return binary
