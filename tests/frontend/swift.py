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
#: way on 2026-10-12, D-SW1). This relies on inheritance: a descendant that
#: replaces its environment wholesale (`env -i`, `execve` with a fresh
#: environment) and also leaves the build's group is not found. The Swift
#: toolchain's children inherit their environment, which is the scope here.
RUN_MARKER = "SUBFLEET_SWIFT_RUN"
#: How long cleanup waits for a killed build's pipes to close, and for each
#: killed process to be gone.
DRAIN_TIMEOUT_S = 30
#: How long the cleanup's own `ps` may take before it is killed and skipped.
SCAN_TIMEOUT_S = 10

#: The real Popen, kept so the cleanup's own `ps` is never a test's patched one.
_POPEN = subprocess.Popen


def marked_pids(token: str) -> list[int]:
    """The pids of this user's processes whose environment carries `token`.
    Bounded: a `ps` that does not finish in SCAN_TIMEOUT_S is killed, reaped
    for at most SCAN_TIMEOUT_S more, and the scan finds nothing."""
    try:
        ps = _POPEN(["/bin/ps", "-axEww", "-o", "pid=,command="], stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return []
    try:
        listing, _ = ps.communicate(timeout=SCAN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        ps.kill()
        try:
            ps.communicate(timeout=SCAN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            pass
        return []
    needle = f"{RUN_MARKER}={token}"
    pids = []
    for line in listing.splitlines():
        head, _, rest = line.strip().partition(" ")
        if head.isdigit() and needle in rest.split():
            pids.append(int(head))
    return pids


def _kill_marked(token: str, own: int | None = None) -> None:
    """Kill every process carrying `token` and wait, at most DRAIN_TIMEOUT_S,
    until each is gone: a killed descendant that launchd adopted is reaped by
    launchd, not by us, so it can still answer `kill(pid, 0)` for a moment.
    `own` is our own child, the compiler: it is killed but not waited for here,
    because it stays a zombie until we reap it, which the caller does next."""
    pids = marked_pids(token)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + DRAIN_TIMEOUT_S
    for pid in pids:
        if pid == own:
            continue
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)


def _end_build(group: int, token: str) -> None:
    """Kill what is left of a build: its process group, then every process
    carrying its marker. Never raises an ordinary exception, so a failed kill
    or scan cannot skip the rest of the cleanup or replace the exception that
    brought us here. The group id is the compiler's pid. While any member of
    the build's group lives, the kernel will not hand that number out as a new
    process or group id. Once the group is empty, reusing the number would
    need the pid space to wrap between the compiler being reaped and this
    kill, which this narrows but does not rule out."""
    try:
        os.killpg(group, signal.SIGKILL)
    except OSError:
        pass
    try:
        _kill_marked(token, own=group)
    except Exception:
        pass


def _drain(compiler: subprocess.Popen) -> None:
    """Reap a build that was killed, waiting at most DRAIN_TIMEOUT_S for its
    pipes and then at most DRAIN_TIMEOUT_S for the process. Never raises an
    ordinary exception: the caller is already handling the one that matters."""
    try:
        compiler.communicate(timeout=DRAIN_TIMEOUT_S)
        return
    except Exception:
        pass
    for pipe in (compiler.stdout, compiler.stderr):
        try:
            if pipe:
                pipe.close()
        except Exception:
            pass
    try:
        compiler.wait(timeout=DRAIN_TIMEOUT_S)
    except Exception:
        pass


def run_swift(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a compiler or app/build.sh. However it ends (by itself, on timeout,
    or on any other exception), kill its group and every process carrying this
    build's marker. On an abnormal end, reap it, waiting at most
    DRAIN_TIMEOUT_S for its pipes."""
    token = uuid.uuid4().hex
    # errors="replace": a compiler or a child that writes bytes that are not
    # UTF-8 must not turn reading its output into a UnicodeDecodeError.
    compiler = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                errors="replace", start_new_session=True,
                                env={**os.environ, RUN_MARKER: token})
    try:
        output, errors = compiler.communicate(timeout=timeout)
    except BaseException:
        _end_build(compiler.pid, token)
        _drain(compiler)
        raise
    _end_build(compiler.pid, token)  # a child may outlive a compiler that exited by itself
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
