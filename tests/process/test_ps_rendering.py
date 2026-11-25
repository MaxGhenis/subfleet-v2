"""C-25.6 against the real `ps`: how it prints an environment, which `peers.ps_text`
renders so the person check can find a state root in a caller's line.

The census (C-5.5) reads raw argument buffers since D-CEN1 and needs no notation;
its tests for every root are in tests/process/test_census_root_bytes_process.py.

Every child here is started with an environment of the test's own making (PATH and
test variables only, never the caller's), so what `ps -E` prints about it carries
no credential and may appear in an assertion message. The children are Python, not
`/bin/sleep`: `ps -E` prints no environment for Apple's own executables at all. Each
is read only once it runs Python code, through `peers._ps`, the reader C-25.6 uses.

A framework build of Python (python.org's, which CI installs, and Homebrew's) runs
`bin/python3.x` as a launcher that executes `Python.app/Contents/MacOS/Python` and
adds `__CF_USER_TEXT_ENCODING=` and `__PYVENV_LAUNCHER__=` after the environment it
was given (review r2 finding 1). Start the currently loaded Mach-O directly, as
reported by dyld, so that launcher cannot append variables. The given environment
then ends the environment block under framework Python too; a native-buffer test
checks that precondition even where the sandbox denies the `ps` executable.

Once, on 2026-09-27 at a load average near 130, a read taken as soon as `Popen`
returned also printed the kernel's `apple[]` strings (`ptr_munge=`, `stack_guard=`
and the like) after the environment, and a rerun of the same example did not; the
review's 801 early reads (2026-09-27, some of a child still suspended before its
first instruction) never did. Why is not established. Those strings would follow
the given run, so they would not change the contiguous-run comparisons. The
root-last case separately requires the root to end the printed line.
"""

import ctypes
import json
import os
import struct
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import hypothesis
import pytest
from hypothesis import strategies as st

from subfleet import procs
from subfleet.conversations import peers

SLEEPER = [sys.executable, "-c", "import os, time; os.write(1, b'r'); time.sleep(60)"]
PATH = (b"PATH", b"/usr/bin:/bin")
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ps_vis_bytes.json"
VALUE = st.binary(max_size=300).map(lambda raw: raw.replace(b"\0", b""))


@pytest.fixture(scope="module")
def macos_inspection():
    if sys.platform != "darwin":
        pytest.skip("C-25.6 reads BSD ps's environment notation")
    try:
        if procs.identity(os.getpid()) is None:
            pytest.skip("C-5.3 process identity unavailable")
    except procs.InspectionError:
        pytest.skip("C-5.3 host sandbox denies ps/sysctl; no ownership bypass")


def token(pid):
    """A synthetic audit token for `pid` (C-25.6 reads its pid; no socket to re-read)."""
    return peers.AuditToken(struct.pack("=8I", 0, 0, 0, 0, 0, pid, 0, 1))


def runtime_python():
    """The loaded Mach-O, which bypasses framework Python's environment-changing
    launcher. `sys.executable` can name that launcher after it has re-executed."""
    get_path = ctypes.CDLL(None)._NSGetExecutablePath
    get_path.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    get_path.restype = ctypes.c_int
    size = ctypes.c_uint32()
    assert get_path(None, ctypes.byref(size)) == -1
    buffer = ctypes.create_string_buffer(size.value)
    assert get_path(buffer, ctypes.byref(size)) == 0
    return os.fsdecode(buffer.value)


@contextmanager
def child(env):
    """A sleeping child in a session of its own (outside this process's group and,
    to the census, outside any walk), with exactly `env`, in order, once it runs."""
    process = subprocess.Popen([runtime_python(), *SLEEPER[1:]], env=dict(env), start_new_session=True, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert process.stdout.read(1) == b"r"
        yield process
    finally:
        process.kill()
        process.wait(timeout=5)
        process.stdout.close()


def printed(pid):
    """What `ps -E` prints for `pid`, read as C-25.6 reads it (`peers._ps`, which
    gives `ps` the C locale), with only the newline stripped (`process_chain`)."""
    return peers._ps(["/bin/ps", "-Ewwp", str(pid), "-o", "command="]).rstrip("\n")


def given_run(env):
    """The argv after argv[0], then each given variable, as `peers.ps_text` prints
    them, spaced: what the line must hold whatever path dyld gives for argv[0]."""
    words = [*SLEEPER[1:], *(os.fsdecode(name + b"=" + value) for name, value in env)]
    return " ".join(peers.ps_text(word) for word in words)


def holds_run(line, env):
    """The given run is in `line` as one contiguous run bounded by spaces: a space
    before it, and a space or the line's end after it."""
    return f" {given_run(env)} " in f"{line} "


@pytest.mark.skipif(sys.platform != "darwin", reason="KERN_PROCARGS2 is specific to macOS")
def test_child_keeps_the_given_environment_last():
    """The child's initial environment ends with the synthetic variables, in order,
    even under framework Python. Read only our child through the kernel so this
    precondition runs even when the sandbox denies the `ps` executable."""
    env = [PATH, (b"SUBFLEET_ROOT", os.fsencode(ROOT))]
    with child(env) as process:
        raw = procs.Darwin().arguments(process.pid, ctypes.create_string_buffer(256 * 1024))
    if raw is None:
        pytest.skip("the host sandbox denies inspection of our child's initial environment")
    # Two NULs end the environment; apple[] strings can follow. Keep the raw
    # buffer out of assertions so launcher-generated values cannot be printed.
    given_environment_is_last = b"\0".join(name + b"=" + value for name, value in env) + b"\0\0" in raw
    assert given_environment_is_last


@pytest.mark.usefixtures("macos_inspection")
def test_ps_prints_every_byte_as_ps_text_says():
    """Invariant 3: the real `ps`, in the C locale `peers._ps` gives it, prints each of
    the 255 bytes an environment can hold as `peers.PS_BYTES` says, and as the
    recorded measurement (tests/fixtures/ps_vis_bytes.json) did. On a macOS whose `ps`
    prints differently this test fails; nothing runs it before a daemon there starts."""
    env = [PATH, *((b"B%02X" % value, b"<" + bytes([value]) + b">") for value in range(1, 256))]
    with child(env) as process:
        line = printed(process.pid)
    assert holds_run(line, env), line
    measured = json.loads(FIXTURE.read_text())["bytes"]
    assert all(f" B{name}=<{shown}>" in line for name, shown in measured.items())


@pytest.mark.usefixtures("macos_inspection")
@hypothesis.settings(max_examples=40, deadline=None,
                     suppress_health_check=[hypothesis.HealthCheck.too_slow])
@hypothesis.given(values=st.lists(VALUE, min_size=1, max_size=12))
def test_ps_prints_any_environment_as_ps_text_says(values):
    """Invariant 3, differential: for arbitrary environment values (every byte but NUL,
    in any sequence, trailing spaces included, up to 300 bytes), the real `ps` prints
    the given run as `peers.ps_text` does, so a value's bytes are printed one at a
    time."""
    env = [PATH, *((b"V%02d" % index, value) for index, value in enumerate(values))]
    with child(env) as process:
        line = printed(process.pid)
    assert holds_run(line, env), line


ROOT = "/tmp/subfleet-José-root "


@pytest.mark.usefixtures("macos_inspection")
def test_the_person_check_finds_a_root_marker_with_a_variable_after_it():
    """Invariant 5, one real process: a caller whose environment carries SUBFLEET_ROOT
    under a root that is not printable ASCII and ends in a space, with another
    variable after it, is refused for its markers even though its image is taken for
    the app's. The chain is cut to the child, so a test run inside a Subfleet attempt
    judges the child and not its own ancestors."""
    env = [PATH, (b"SUBFLEET_ROOT", os.fsencode(ROOT)), (b"HOME", b"/Users/fixture")]
    with child(env) as process:
        line = printed(process.pid)
        verdict = peers.judge(token(process.pid), chain=lambda pid: peers.process_chain(pid)[:1], root=ROOT,
                              executable=lambda token: peers.APP_EXECUTABLES[0])
    assert holds_run(line, env), line
    assert (verdict.person, verdict.reason) == (False, "the caller carries Subfleet's attempt markers")


@pytest.mark.usefixtures("macos_inspection")
def test_the_person_check_finds_a_root_marker_that_ends_the_line():
    """Invariant 5 with the root last: its trailing space ends the line, and
    `process_chain` keeps it, including when the test runs under framework Python."""
    env = [PATH, (b"SUBFLEET_ROOT", os.fsencode(ROOT))]
    with child(env) as process:
        line = printed(process.pid)
        assert line.endswith(f"SUBFLEET_ROOT={peers.ps_text(ROOT)}"), line
        verdict = peers.judge(token(process.pid), chain=lambda pid: peers.process_chain(pid)[:1], root=ROOT,
                              executable=lambda token: peers.APP_EXECUTABLES[0])
    assert holds_run(line, env), line
    assert (verdict.person, verdict.reason) == (False, "the caller carries Subfleet's attempt markers")
