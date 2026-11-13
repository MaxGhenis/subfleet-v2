"""C-5.5 against the real `ps`: how it prints an environment, and the marker source.

Every child here is started with an environment of the test's own making (PATH and
test variables only, never the caller's), so what `ps -E` prints about it carries
no credential and may appear in an assertion message. The children are Python, not
`/bin/sleep`: `ps -E` prints no environment for Apple's own executables at all. Each
is read only once it runs Python code. Once, on 2026-09-27 at a load average near
130, a read taken as soon as `Popen` returned also printed the kernel's `apple[]`
strings (`ptr_munge=`, `stack_guard=` and the like) after the environment, and a
rerun of the same example did not; the review's 801 early reads (2026-09-27, some of
a child still suspended before its first instruction) never did. Why is not
established. It would cost the census nothing, since those strings carry no
marker, but it would change the line compared exactly here.
"""

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
VALUE = st.binary(max_size=40).map(lambda raw: raw.replace(b"\0", b""))


@pytest.fixture(scope="module", autouse=True)
def macos_inspection():
    if sys.platform != "darwin":
        pytest.skip("C-5.5 reads BSD ps's environment notation")
    try:
        if procs.identity(os.getpid()) is None:
            pytest.skip("C-5.3 process identity unavailable")
    except procs.InspectionError:
        pytest.skip("C-5.3 host sandbox denies ps/sysctl; no ownership bypass")


def token(pid):
    """A synthetic audit token for `pid` (C-25.6 reads its pid; no socket to re-read)."""
    return peers.AuditToken(struct.pack("=8I", 0, 0, 0, 0, 0, pid, 0, 1))


@contextmanager
def child(env):
    """A sleeping child in a session of its own (outside this process's group and,
    to the census, outside any walk), with exactly `env`, in order, once it runs."""
    process = subprocess.Popen(SLEEPER, env=dict(env), start_new_session=True, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert process.stdout.read(1) == b"r"
        yield process
    finally:
        process.kill()
        process.wait(timeout=5)
        process.stdout.close()


def printed(pid):
    """What `ps -E` prints for `pid`, run as the census runs it (`procs._read`)."""
    return procs._read(["/bin/ps", "-Ewwp", str(pid), "-o", "command="]).rstrip("\n")


def expected(env):
    """The same line from `peers.ps_text`: the argv, then each variable, spaced."""
    words = [*SLEEPER, *(os.fsdecode(name + b"=" + value) for name, value in env)]
    return " ".join(peers.ps_text(word) for word in words)


def test_ps_prints_every_byte_as_procs_expects():
    """C-5.5: the real `ps`, in the C locale `procs._read` gives it, prints each of the
    255 bytes an environment can hold as `peers.PS_BYTES` says, and as the recorded
    measurement (tests/fixtures/ps_vis_bytes.json) did. On a macOS whose `ps` prints
    differently this test fails; nothing runs it before a daemon there starts."""
    env = [PATH, *((b"B%02X" % value, b"<" + bytes([value]) + b">") for value in range(1, 256))]
    with child(env) as process:
        line = printed(process.pid)
    assert line == expected(env)
    measured = json.loads(FIXTURE.read_text())["bytes"]
    assert all(f" B{name}=<{shown}>" in line for name, shown in measured.items())


@hypothesis.settings(max_examples=40, deadline=None,
                     suppress_health_check=[hypothesis.HealthCheck.too_slow])
@hypothesis.given(values=st.lists(VALUE, min_size=1, max_size=12))
def test_ps_prints_any_environment_as_ps_text_says(values):
    """C-5.5, differential: for arbitrary environment values (every byte but NUL, in
    any sequence, trailing spaces included), the real `ps` prints the whole line as
    `peers.ps_text` does, so a value's bytes are printed one at a time."""
    env = [PATH, *((b"V%02d" % index, value) for index, value in enumerate(values))]
    with child(env) as process:
        assert printed(process.pid) == expected(env)


def test_the_person_check_finds_a_root_marker_as_ps_prints_it():
    """C-25.6 with C-5.5: a caller whose environment carries SUBFLEET_ROOT, last and
    ending in a space, under a root that is not printable ASCII, is refused for its
    markers. The chain is cut to the child, so a test run inside a Subfleet attempt
    judges the child and not its own ancestors."""
    root = "/tmp/subfleet-José-root "
    with child([PATH, (b"SUBFLEET_ROOT", os.fsencode(root))]) as process:
        verdict = peers.judge(token(process.pid), chain=lambda pid: peers.process_chain(pid)[:1], root=root,
                              executable=lambda pid: None)
    assert not verdict.person
    assert verdict.reason == "the caller carries Subfleet's attempt markers"
