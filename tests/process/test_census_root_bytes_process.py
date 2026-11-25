"""C-5.5 against the real kernel: the census finds a marked process whatever bytes
the state root holds (PR #177, rebased onto D-CEN1).

The census reads each candidate's raw `KERN_PROCARGS2` buffer and compares
`SUBFLEET_ROOT=` and `SUBFLEET_ATTEMPT=` with the root's and the id's `os.fsencode`
bytes, whole (D-CEN1). Every child here is a Python process this test starts, in a
session of its own (outside this process's group, and so outside any walk the census
makes), with an environment of the test's own making, and it is killed and reaped on
failure too. The census keeps pids only; nothing here prints a buffer. Children are
Python, not Apple's binaries: the kernel returns no environment for a platform,
hardened-runtime binary (C-5.5, D-KILL2).
"""

from __future__ import annotations

import os
import subprocess
import sys
import unicodedata
import uuid
from contextlib import ExitStack, contextmanager

import hypothesis
import pytest
from hypothesis import strategies as st

from subfleet import procs

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="KERN_PROCARGS2 is macOS's")

SLEEPER = [sys.executable, "-c", "import os, time; os.write(1, b'r'); time.sleep(60)"]
PATH = (b"PATH", b"/usr/bin:/bin")
#: PR #177's seven roots, an NFD root (`José` in NFD), and one over 1,000 bytes.
ROOTS = {
    "the report's": "/tmp/subfleet-José-root",
    "newline and tab": "/tmp/subfleet-a\nb\tc-root",
    "no-break space": "/tmp/subfleet-\u00a0-root",
    "backslash and space": "/tmp/subfleet a\\b-root",
    "ends in a space": "/tmp/subfleet-root ",
    "control and DEL": "/tmp/subfleet-\x01\x7f-root",
    "not UTF-8": os.fsdecode(b"/tmp/subfleet-\xff\xfe-root"),
    "NFD": unicodedata.normalize("NFD", "/tmp/subfleet-José-root"),
    "over 1,000 bytes": "/tmp/subfleet-" + "é" * 600 + "-root ",
}
#: Where `SUBFLEET_ROOT` falls among the child's variables.
POSITIONS = ("first", "middle", "last")


@pytest.fixture(scope="module", autouse=True)
def macos_inspection():
    try:
        if procs.identity(os.getpid()) is None:
            pytest.skip("C-5.3 process identity unavailable")
    except procs.InspectionError:
        pytest.skip("C-5.3 host sandbox denies ps/sysctl; no ownership bypass")


@contextmanager
def child(env):
    """A sleeping child in a session of its own with exactly `env`, once it runs."""
    process = subprocess.Popen(SLEEPER, env=dict(env), start_new_session=True, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert process.stdout.read(1) == b"r"
        yield process
    finally:
        process.kill()
        process.wait(timeout=5)
        process.stdout.close()


def marked_env(attempt: str, raw_root: bytes, position: str) -> list[tuple[bytes, bytes]]:
    env = [PATH, (b"SUBFLEET_ATTEMPT", os.fsencode(attempt)), (b"HOME", b"/Users/fixture")]
    env.insert({"first": 0, "middle": 2, "last": 3}[position], (b"SUBFLEET_ROOT", raw_root))
    return env


def other_roots(root: str) -> list[str]:
    """Roots that are not `root` but come close."""
    raw = os.fsencode(root)
    near = ["/tmp/subfleet-Jose-root", os.fsdecode(raw + b"x"), os.fsdecode(raw[:-1]), root.rstrip(" "),
            unicodedata.normalize("NFC", root), unicodedata.normalize("NFD", root)]
    return [other for other in dict.fromkeys(near) if other and other != root]


def attempt_id(tag: str = "marker") -> str:
    return f"20990101-000000-{tag}-{uuid.uuid4().hex[:8]}/a1"


@pytest.mark.parametrize("root", ROOTS.values(), ids=ROOTS.keys())
def test_census_finds_a_marked_process_under_any_root_wherever_its_variable_falls(root):
    """Invariant 1 against the real kernel: a marked process outside the group and the
    walk is found by its markers, with a whole-host census, whatever the state root's
    bytes and with `SUBFLEET_ROOT` first, in the middle or last. A census for a
    near-miss root (one byte longer or shorter, stripped, the other normal form)
    does not count it."""
    attempts = {position: attempt_id() for position in POSITIONS}
    with ExitStack() as stack:
        children = {position: stack.enter_context(child(marked_env(attempts[position], os.fsencode(root), position)))
                    for position in POSITIONS}
        found = {position: procs.containment(None, None, None, attempts[position], root=root)
                 for position in POSITIONS}
        others = {other: procs.containment(None, None, None, attempts["last"], root=other)
                  for other in other_roots(root)}
    for position in POSITIONS:
        assert found[position].marker_pids == {children[position].pid}, (position, found[position].errors)
        assert not found[position].verified_empty
    for other, result in others.items():
        assert children["last"].pid not in result.marker_pids, (other, result.errors)


def test_census_finds_a_non_ascii_attempt_id():
    """Review r1 finding 8, on the raw reader: the attempt half is compared as its
    `os.fsencode` bytes too. Real ids are ASCII; one that is not is still found."""
    attempt, root = f"jéb-{uuid.uuid4().hex[:8]}/a1", ROOTS["the report's"]
    with child(marked_env(attempt, os.fsencode(root), "middle")) as process:
        found = procs.containment(None, None, None, attempt, root=root)
        ascii_lookalike = procs.containment(None, None, None, attempt.replace("é", "e"), root=root)
    assert found.marker_pids == {process.pid}, found.errors
    assert process.pid not in ascii_lookalike.marker_pids


ROOT_BYTES = st.one_of(
    st.binary(min_size=1, max_size=1500).map(lambda raw: raw.replace(b"\0", b"")).filter(bool),
    st.text(st.characters(codec="utf-8", exclude_characters="\0"), min_size=1, max_size=200)
    .map(lambda text: unicodedata.normalize("NFD", text).encode()),
)


@hypothesis.settings(max_examples=25, deadline=None, suppress_health_check=[hypothesis.HealthCheck.too_slow])
@hypothesis.given(raw=ROOT_BYTES, position=st.sampled_from(POSITIONS), trailing=st.integers(min_value=0, max_value=2))
def test_census_finds_the_marker_under_arbitrary_root_bytes(raw, position, trailing):
    """Invariant 1 as a property against the real kernel: whatever bytes the root holds
    (any but NUL, up to 1,500, NFD text, trailing spaces) and wherever its variable
    falls, a whole-host census finds the child that carries both markers, and a
    census for a root one byte longer does not."""
    raw += b" " * trailing
    root, attempt = os.fsdecode(raw), attempt_id("property")
    with child(marked_env(attempt, raw, position)) as process:
        found = procs.containment(None, None, None, attempt, root=root)
        longer = procs.containment(None, None, None, attempt, root=os.fsdecode(raw + b"x"))
    assert found.marker_pids == {process.pid}, found.errors
    assert process.pid not in longer.marker_pids, longer.errors
