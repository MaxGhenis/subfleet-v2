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
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "app" / "Sources"


def app_sources() -> list[Path]:
    sources = sorted(SOURCES.rglob("*.swift"))
    assert sources, f"no Swift sources under {SOURCES}"
    return sources


#: Every process a build starts inherits this variable with a value unique to
#: that build, so cleanup can find a descendant that left the build's process
#: group and was adopted by launchd (a swift-frontend outlived its build that
#: way on 2026-10-12, D-SW1).
RUN_MARKER = "SUBFLEET_SWIFT_RUN"
#: How long cleanup waits for a killed build's pipes to close.
DRAIN_TIMEOUT_S = 30


#: The real Popen, kept so the cleanup's own `ps` is never a test's patched one.
_POPEN = subprocess.Popen


def marked_pids(token: str) -> list[int]:
    """The pids of this user's processes whose environment carries `token`."""
    with _POPEN(["/bin/ps", "-axEww", "-o", "pid=,command="], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True) as ps:
        listing, _ = ps.communicate(timeout=10)
    needle = f"{RUN_MARKER}={token}"
    pids = []
    for line in listing.splitlines():
        head, _, rest = line.strip().partition(" ")
        if head.isdigit() and needle in rest.split():
            pids.append(int(head))
    return pids


def _kill_marked(token: str) -> None:
    """Kill every process carrying `token` and wait, at most DRAIN_TIMEOUT_S,
    until each is gone: a killed descendant that launchd adopted is reaped by
    launchd, not by us, so it can still answer `kill(pid, 0)` for a moment."""
    pids = marked_pids(token)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + DRAIN_TIMEOUT_S
    for pid in pids:
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)


def run_swift(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a compiler or app/build.sh. On timeout or any other exit, kill its
    group and every process carrying this build's marker, then reap it, waiting
    at most DRAIN_TIMEOUT_S for its pipes."""
    token = uuid.uuid4().hex
    compiler = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True, env={**os.environ, RUN_MARKER: token})
    try:
        output, errors = compiler.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(compiler.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _kill_marked(token)
        try:
            compiler.communicate(timeout=DRAIN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            for pipe in (compiler.stdout, compiler.stderr):
                if pipe:
                    pipe.close()
            compiler.wait(timeout=DRAIN_TIMEOUT_S)
        raise
    _kill_marked(token)             # a child may outlive a compiler that exited by itself
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
